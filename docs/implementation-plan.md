# 智能工单系统 V1 —— 实现计划（Implementation Plan）

> 本文档是可直接交给 AI 编码 Agent（如 Claude Code）执行的实现指令，用于驱动 V1 范围的代码落地。设计依据见 [architecture-design.md](./architecture-design.md)，本计划不重复架构决策的论证过程，只给出可执行的任务分解与验收标准。若执行中发现本计划与架构文档冲突，以架构文档为准并向人类确认后再继续。

## 角色与目标

你是本项目的实现 Agent。目标：在 V1 范围内，交付一个端到端可运行的"员工提问 → 知识库检索回答 → 未解决则自动转工单"闭环，覆盖 VPN、账号密码、邮箱 3 个场景，部署为可在本地 docker-compose 和标准 K8s 上运行的服务集合。

**V1 明确不做**（避免过度设计）：向量检索/RAG、企业 IM 机器人接入、外部工单系统（Jira 等）对接、OIDC/SSO 真实对接、多模型路由、schema 级租户隔离。这些是二三期范围，V1 只需在接口/抽象层留好扩展点，不需要提前实现。

## 技术栈约束（已确定，不要重新选型）

- 语言：Python 3.11+，应用服务与 Agent Runtime 统一用 Python，降低集成成本。
- 应用服务框架：FastAPI。
- Agent 编排：LangGraph。
- 队列 + 流式回传：Redis Streams（任务队列）+ Redis Pub/Sub（执行中事件回传），复用同一个 Redis 实例。
- 数据库：PostgreSQL，同一实例、每服务独立 database，库内 `tenant_id` 行级隔离（RLS）。
- 工具协议：Tool Gateway 对 Agent Runtime 暴露 REST 接口（`/v1/tools/list` 返回 OpenAI function 元数据，`/v1/tools/call` 执行），详见架构文档 3.4 节。
- 模型供应商：AI Gateway v1 通过 OpenAI-compatible 协议接入，具体供应商为 **DeepSeek**（`base_url=https://api.deepseek.com`），用 `ModelProvider` 抽象 + 通用 `OpenAICompatibleProvider` 实现，不要为 DeepSeek 单独写一套适配代码。
- 容器化：每个服务独立 Dockerfile，提供 `docker-compose.yml` 用于本地一键起环境（Postgres、Redis 与全部服务）。

## 目录结构约定

```
ai-work-order/
├── docs/
├── docker-compose.yml
├── services/
│   ├── app-service/         # FastAPI，会话/租户/用户，员工与 IT 页面的 BFF；库 app_service
│   ├── agent-runtime/        # LangGraph worker，消费 Redis Streams
│   ├── tool-gateway/         # 工具策略网关：权限/校验/幂等/审计/脱敏；库 tool_gateway
│   ├── knowledge-service/    # 知识检索，V1 按租户写死的数据，无数据库
│   ├── ticket-service/       # 工单生命周期：建单/详情/列表/状态流转；库 ticket_service
│   └── ai-gateway/           # 模型调用统一出口；库 ai_gateway
└── deploy/
    ├── postgres/             # init-databases.sql：本地创建各服务的库和账号
    └── k8s/                  # 每个服务的 Deployment/Service manifest（V1 先给基础版本，不要求生产级 HPA/KEDA 调优）
```

每个服务自包含：自己的 `app/db` 模型、`migrations/`（Alembic）、`scripts/seed.py`、`tests/`，服务之间不共享代码和表（架构文档第 4 节）。服务内部分层参照 tool-gateway：`api` → `services` → `repositories` / 领域模块，`core` 放配置、鉴权、错误。

## 分阶段任务清单

按顺序执行，每阶段完成后自测通过再进入下一阶段；不要跨阶段并行大改。

### 阶段 0：仓库脚手架
- 建立上述目录结构，四个服务各自的 `pyproject.toml`/依赖声明、最小可运行的健康检查端点（`GET /healthz`）。
- `docker-compose.yml` 拉起 Postgres + Redis + 四个服务，服务间通过 compose 网络互通。
- **验收标准**：`docker compose up` 后四个服务健康检查均返回 200。

### 阶段 1：数据模型与迁移
> 最初是一个共享库 + 一套迁移（`libs/shared`），后改为每服务独立 database、各自迁移（架构文档第 4 节）。以下为当前版本。
- 按架构文档第 4 节的归属建表，每个服务在自己的库里用 Alembic 管理 schema，对含租户数据的表启用 `FORCE ROW LEVEL SECURITY`（按 `tenant_id` 过滤）。
- 各服务的 seed 脚本用固定的 demo 租户 ID 插入数据：app-service 插两个 demo 租户的租户/用户/会话，tool-gateway 插角色工具绑定；知识内容在 knowledge-service 的静态 JSON 里（见阶段 2.5）。
- **验收标准**：迁移可重复执行（up/down 幂等），seed 脚本跑完后能查到预置数据，且用非本租户 `tenant_id` 查询取不到数据（验证 RLS 生效）。

### 阶段 2：Tool Gateway（工具策略网关）
> 最初按 MCP 接口实现，后改为 REST 策略网关（架构文档 3.4 节）。以下为当前版本。
- `GET /v1/tools/list?tenant_id&agent_role`：返回角色可用工具的 OpenAI function 元数据，不含密钥/下游地址。
- `POST /v1/tools/call`：`tenant_id, session_id, user_id, agent_role, tool_name, arguments`，返回 `success / error`；副作用工具必须带 `Idempotency-Key` 头。
- 能力：多租户（RLS）、`role_tool_bindings` 角色工具绑定、JSON Schema 参数校验、副作用工具的 `tool_calls` 幂等记录、`tool_call_logs` 审计（参数脱敏）、返回体凭据字段屏蔽、服务间 Bearer 令牌鉴权。HITL 审批与 Redis 限流 V1 暂不做（见架构文档 3.4 节）。
- 工具：`search_knowledge(query)` 调 knowledge-service；`create_ticket(summary, category)` 调 ticket-service 的 `POST /internal/v1/tickets`（会话 ID、员工 `user_id`、幂等键由网关注入，幂等键透传）。`TicketProvider` 抽象，v1 只有 `InternalTicketProvider`。
- 调用上下文：`/v1/tools/call` 必须带 `user_id`（当前员工 UUID），作为工单提单人，记入审计和幂等指纹。
- 代码分层：`api/v1`（路由）→ `services`（调用管线，不依赖 FastAPI）→ `repositories`（网关自身的表）/ `tools`（工具定义与 handler）；`core` 放配置、鉴权、错误、脱敏。
- **验收标准**：`tests/unit`（无外部依赖）与 `tests/integration`（需起 tool-gateway 及其依赖的 knowledge-service、ticket-service）全部通过；覆盖权限、校验、幂等重放与冲突、脱敏、审计；同一幂等键的并发请求只产生一张工单；`running` 状态孤儿记录超时后可恢复且不重复执行副作用。

### 阶段 2.5：知识库服务与工单服务
- knowledge-service：只有 `GET /internal/v1/knowledge/search`，两个 demo 租户各自写死的 QA 数据（同一问题答案不同，用来验证租户隔离），字符二元组匹配；`KnowledgeRepository` 接口留给后续数据库/向量实现。
- ticket-service：自己的库和迁移；建单（幂等）、详情（含处理记录）、列表（游标分页）、状态流转（4 状态状态机、提单人专属动作、`version` 乐观锁、`ticket_events` 只追加）。
- **验收标准**：knowledge-service 单测覆盖匹配、租户隔离、鉴权，容器测试确认数据文件随镜像发布；ticket-service 覆盖完整生命周期、非法流转、非提单人拒绝、版本冲突、8 路并发流转只有一个成功、分页无遗漏无重复、跨租户不可见。

### 阶段 3：AI Gateway
- 实现 `ModelProvider` 抽象 + 通用 `OpenAICompatibleProvider` 实现（用 OpenAI SDK 即可，只需把 `base_url` 指向配置项，不要绑定 OpenAI 官方域名），暴露统一的 `generate(messages, tenant_id, system_prompt) -> stream[chunk]` 接口（支持流式）。
- 配置一个具体供应商实例：DeepSeek（`base_url=https://api.deepseek.com`，模型名走配置项，如 `deepseek-chat`）。
- 每次调用记录 `tenant_id + 模型名 + 输入/输出 token 数 + 耗时` 到日志或表。
- API Key（DeepSeek Key）通过环境变量/密钥管理注入，不要硬编码，不要打印到日志。
- **验收标准**：给定一个测试 prompt，能拿到流式返回的 token，且用量记录可查询。

### 阶段 4：Agent Runtime
> 最初计划 checkpoint 存 Redis、事件走 Pub/Sub，后改为 checkpoint 存 Postgres、事件写每会话 Redis Stream（架构文档 3.3 节）。以下为当前版本。
- 用 LangGraph 实现固定状态图：`classify_intent → retrieve_knowledge → generate_answer → await_feedback(interrupt) → check_resolution → (end | summarize_ticket → create_ticket)`，另有 `reply`（寒暄/描述不清）分支；`new_question` 回到 `classify_intent`。
- `classify_intent` 判断问题是否属于 V1 的 3 个场景；场景外、明确要求开工单、检索无命中、反馈没解决都自动建单（场景外的工单描述带"未命中知识库场景"标记），不勉强让模型作答。
- 作为无状态 Worker：通过 Consumer Group 消费 `agent_tasks`（消息字段 `task_id, conversation_id, tenant_id, user_id, content`；`user_id` 原样传给 Tool Gateway 作为提单人）。历史上下文在 checkpoint 里，不随任务传。
- 执行事件（`retrieving`、`answer_chunk`、`answer`、`reply`、`ticket_created`、`resumed`、`error`、`done`）写入 `conversation:{conversation_id}:events` Stream。
- checkpoint 存 Postgres `agent_runtime` 库（`agent-runtime-migrate` 任务跑 `setup()` 并授权运行时账号），`durability="sync"`，crash 后从断点续跑。
- 调用 AI Gateway 带服务令牌。
- **验收标准**：`tests/unit`（假网关 + 内存 checkpointer，覆盖每条分支、interrupt/resume、解析失败兜底、崩溃续跑）与 `tests/integration`（真 Redis、Postgres、tool-gateway、ticket-service，模型用假实现）全部通过；向 `agent_tasks` 写入测试任务，能在事件 Stream 上看到预期序列；分别在回答输出途中和生成工单摘要途中 `kill -9` Worker 再重启，任务续跑完成且该会话只有一张工单。

### 阶段 5：应用服务
> 工单数据与接口已在 ticket-service（阶段 2.5）。本阶段的员工"我的工单"和 IT 处理页面由应用服务调用 ticket-service 实现，`start`/`resolve` 的 IT 角色校验在应用服务做。
- 实现 `POST /conversations/{id}/messages`：写入用户消息，把任务推入 `agent_tasks` 队列，立即返回 202。
- 实现 `GET /conversations/{id}/stream`（SSE）：读取该会话的事件 Stream（`conversation:{id}:events`，用 Stream ID 作为 SSE 的 event id，支持 Last-Event-ID 续读），`done`/`error` 后关闭连接。
- 上一轮任务未结束时拒绝同一会话的新消息（409）；记录每个任务的状态，定时把长时间没有 `done` 的任务重新入队，弥补 Redis 丢消息。
- 面向员工/IT 的工单接口：`GET /tickets/{id}`、`GET /tickets`（员工只看自己的）、状态流转，均转调 ticket-service，租户和用户取自 JWT 而不是请求参数。
- 简单的用户/租户模型 + JWT 鉴权中间件（v1 自建账号体系，登录接口签发 JWT，`tenant_id` 编码进 token）。
- **验收标准**：用 curl/httpie 完整跑通一次"提问 → SSE 收到检索中事件 → 收到回答 → 未解决时收到 ticket_created 事件 → 查询该工单"的闭环。

### 阶段 6：端到端联调与本地部署
- 补齐 `deploy/k8s` 下各服务的最小 Deployment/Service manifest（不要求 HPA/KEDA 配置齐全，占位注明"生产环境需按架构文档 5 节补充自动扩缩容配置"）。
- 编写一份 `README.md`，说明本地启动步骤、如何跑通 3 个测试场景、如何查看审计日志和用量记录。
- **验收标准**：一个未接触过项目的人，跟着 README 从 0 到能在本地跑通端到端闭环。

## 工作方式要求

- 每个阶段先给出接口/数据结构定义（可以是简短的代码骨架或类型声明），确认结构合理后再补全实现逻辑，不要一次性输出整个阶段的完整代码而不做中间检查点。
- 涉及架构文档中标注为「【假设，可调整】」的决策（入口渠道、身份认证方式）：按文档默认值实现，不要自行更改这些决策。
- 如果在某一阶段发现需要偏离本提示词（例如某个库不可用、某个设计在实现层面走不通），停下来说明原因和替代方案，等待确认，不要静默改变范围。
- 每个阶段完成后运行对应的验收标准自测，并在回复中明确说明测试结果，而不是仅声明"已完成"。
