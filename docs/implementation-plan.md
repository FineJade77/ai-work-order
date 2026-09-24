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
- 登录：`POST /v1/auth/login {tenant(slug), email, password}` 签发 HS256 JWT（`sub/tid/role`）；`GET /v1/me`。迁移 0003 加 `tenants.slug`、`users.password_hash`、角色约束（`employee/it_staff`）、`turns` 表、`messages.task_id`。
- `POST /v1/conversations/{id}/messages`：同一事务写用户消息和 `turns`（同一会话只能有一个 `queued`，否则 409），再 XADD `agent_tasks`，返回 202 `{task_id, message_id}`。
- `GET /v1/conversations/{id}/stream?task_id=`（SSE）：从会话事件 Stream 开头读、按 `task_id` 过滤，Stream ID 作为 SSE 的 event id，支持 Last-Event-ID 续读，`done`/`error` 后关闭；事件已过期时用数据库里的结果补发。
- 回复落库：后台消费 `agent_results`，幂等地写助手消息、关闭轮次。补偿：`queued` 超过 10 分钟的轮次用同一 `task_id`、`seq` 重新入队，最多 2 次后标记失败。每轮带会话内序号 `seq`，Agent Runtime 据此跳过旧轮次的副本（架构文档 3.3 节）。
- 工单接口：`GET /v1/tickets`（员工只看自己的）、`GET /v1/tickets/{id}`、`POST /v1/tickets/{id}/transitions`，均转调 ticket-service，租户和用户取自 JWT；`start` 固定指派给自己。
- seed：两个 demo 租户（slug `demo`、`northwind`）各一个员工和一个 IT，开发密码 `demo-password`。
- **验收标准**：`tests/unit` 与 `tests/integration`（真 Postgres、Redis、ticket-service）全部通过，覆盖登录失败不可区分、会话私有、并发发消息 409、结果重复投递只存一次、晚到结果覆盖失败提示、SSE 过滤与续读、补偿重投与放弃、工单可见性与角色规则；用 curl 完整跑通"登录 → 提问 → SSE 收到检索中和回答 → 反馈未解决 → 收到 ticket_created → 查询工单 → IT 处理 → 员工关闭"的闭环。

### 阶段 6：端到端联调与本地部署
- `docker compose up -d --build` 一条命令完成建库、迁移、写入演示数据（`*-seed` 一次性服务）和启动；`.env.example` 说明所有配置项。
- 所有服务镜像以 uid 10001 运行，运行时不写文件（可只读根文件系统）。
- `deploy/k8s`：Kustomize。`base`（各服务 Deployment/Service、迁移 Job、ConfigMap）、`overlays/dev`（单实例 Postgres/Redis、db-init、seed、开发凭据）、`components/autoscaling`（HPA + agent-runtime 的 KEDA ScaledObject，默认不启用）、`apply-dev.sh`（按依赖顺序部署）。
- `README.md`：本地启动、4 个演示场景（`scripts/demo.sh`，含场景外直接建单）、curl 完整闭环（含 IT 处理）、审计与用量查询、运行测试（`scripts/test-all.sh`）、排障。
- **验收标准**：在不含 `.venv`/`.env` 的干净副本里严格按 README 操作：一条命令启动、演示脚本和 README 中的 curl 代码块原样执行跑通闭环（真实 DeepSeek）、审计与用量查询有输出、`scripts/test-all.sh` 从零建环境全部通过；K8s 清单做静态校验（本地渲染，检查 Secret/ConfigMap 引用、Service 选择器与端口、环境变量与 compose 一致），未在真实集群部署。

## 工作方式要求

- 每个阶段先给出接口/数据结构定义（可以是简短的代码骨架或类型声明），确认结构合理后再补全实现逻辑，不要一次性输出整个阶段的完整代码而不做中间检查点。
- 涉及架构文档中标注为「【假设，可调整】」的决策（入口渠道、身份认证方式）：按文档默认值实现，不要自行更改这些决策。
- 如果在某一阶段发现需要偏离本提示词（例如某个库不可用、某个设计在实现层面走不通），停下来说明原因和替代方案，等待确认，不要静默改变范围。
- 每个阶段完成后运行对应的验收标准自测，并在回复中明确说明测试结果，而不是仅声明"已完成"。
