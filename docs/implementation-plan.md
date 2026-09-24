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
- 数据库：PostgreSQL，共享库 + `tenant_id` 行级隔离（RLS）。
- 工具协议：Tool Gateway 对 Agent Runtime 暴露 REST 接口（`/v1/tools/list` 返回 OpenAI function 元数据，`/v1/tools/call` 执行），详见架构文档 3.4 节。
- 模型供应商：AI Gateway v1 通过 OpenAI-compatible 协议接入，具体供应商为 **DeepSeek**（`base_url=https://api.deepseek.com`），用 `ModelProvider` 抽象 + 通用 `OpenAICompatibleProvider` 实现，不要为 DeepSeek 单独写一套适配代码。
- 容器化：每个服务独立 Dockerfile，提供 `docker-compose.yml` 用于本地一键起环境（Postgres、Redis、四个服务）。

## 目录结构约定

```
ai-work-order/
├── docs/
├── docker-compose.yml
├── services/
│   ├── app-service/         # FastAPI，会话/工单/租户/用户
│   ├── agent-runtime/        # LangGraph worker，消费 Redis Streams
│   ├── tool-gateway/         # 工具策略网关：权限/校验/幂等/限流/审批/审计/脱敏
│   └── ai-gateway/           # 模型调用统一出口
├── libs/
│   └── shared/               # 跨服务共享的类型定义、tenant 上下文、Redis/PG 客户端封装
└── deploy/
    └── k8s/                  # 每个服务的 Deployment/Service manifest（V1 先给基础版本，不要求生产级 HPA/KEDA 调优）
```

每个服务内部保持标准分层（`api`/`domain`/`infra` 或等价约定即可），不需要引入额外的架构框架。

## 分阶段任务清单

按顺序执行，每阶段完成后自测通过再进入下一阶段；不要跨阶段并行大改。

### 阶段 0：仓库脚手架
- 建立上述目录结构，四个服务各自的 `pyproject.toml`/依赖声明、最小可运行的健康检查端点（`GET /healthz`）。
- `docker-compose.yml` 拉起 Postgres + Redis + 四个服务，服务间通过 compose 网络互通。
- **验收标准**：`docker compose up` 后四个服务健康检查均返回 200。

### 阶段 1：数据模型与迁移
- 按架构文档第 4 节建表：`tenants`、`users`、`conversations`、`messages`、`tickets`、`knowledge_items`。
- 用 Alembic（或等价迁移工具）管理 schema，对涉及租户数据的表启用 PostgreSQL RLS 策略（按 `tenant_id` 过滤）。
- 写一个 seed 脚本，插入 1 个测试租户 + 3 个场景（VPN、账号密码、邮箱）对应的 `knowledge_items`（每个场景至少 3 条 QA）。
- **验收标准**：迁移可重复执行（up/down 幂等），seed 脚本跑完后能查到预置数据，且用非本租户 `tenant_id` 查询取不到数据（验证 RLS 生效）。

### 阶段 2：Tool Gateway（工具策略网关）
> 最初按 MCP 接口实现，后改为 REST 策略网关（架构文档 3.4 节）。以下为当前版本。
- `GET /v1/tools/list?tenant_id&agent_role`：返回角色可用工具的 OpenAI function 元数据，不含密钥/下游地址。
- `POST /v1/tools/call`：`tenant_id, session_id, agent_role, tool_name, arguments`，返回 `success / pending_approval / error`；副作用工具必须带 `Idempotency-Key` 头。
- 审批：`GET /v1/approvals`、`POST /v1/approvals/{id}/resolve`，记录存 `tool_approvals`；审批结果通过 Redis Stream `tool_approval_events` 通知 Agent Runtime 恢复。
- 能力：多租户（RLS）、`role_tool_bindings` 角色工具绑定（含是否需审批）、JSON Schema 参数校验、`tool_calls` 幂等记录、Redis 令牌桶限流（按租户+工具）、`tool_call_logs` 审计、返回体与审计参数脱敏、服务间 Bearer 令牌鉴权。
- 工具：`search_knowledge(query)`（v1 `ILIKE` + `pg_trgm` 排序）、`create_ticket(summary, category)`（会话 ID 与幂等键由网关注入，工单表层面也按幂等键去重）、`update_ticket_status(ticket_id, status)`。`TicketProvider` 抽象，v1 只有 `InternalTicketProvider`。
- **验收标准**：权限、校验、幂等重放与冲突、审批通过/拒绝及恢复、限流、脱敏、审计均有端到端用例覆盖；同一幂等键的并发请求只产生一张工单；`running` 状态孤儿记录超时后可恢复且不重复执行副作用。

### 阶段 3：AI Gateway
- 实现 `ModelProvider` 抽象 + 通用 `OpenAICompatibleProvider` 实现（用 OpenAI SDK 即可，只需把 `base_url` 指向配置项，不要绑定 OpenAI 官方域名），暴露统一的 `generate(messages, tenant_id, system_prompt) -> stream[chunk]` 接口（支持流式）。
- 配置一个具体供应商实例：DeepSeek（`base_url=https://api.deepseek.com`，模型名走配置项，如 `deepseek-chat`）。
- 每次调用记录 `tenant_id + 模型名 + 输入/输出 token 数 + 耗时` 到日志或表。
- API Key（DeepSeek Key）通过环境变量/密钥管理注入，不要硬编码，不要打印到日志。
- **验收标准**：给定一个测试 prompt，能拿到流式返回的 token，且用量记录可查询。

### 阶段 4：Agent Runtime
- 用 LangGraph 实现状态图，节点：`classify_intent → retrieve_knowledge → generate_answer → check_resolution → (end | create_ticket)`。
- `classify_intent` 判断问题是否属于 V1 支持的 3 个场景之一；不属于则直接走 `create_ticket` 分支（附带"未命中知识库场景"标记），不勉强调用模型作答。
- 作为无状态 Worker：通过 Consumer Group 消费 Redis Streams 的 `agent_tasks` 队列（消息体含 `conversation_id`、`tenant_id`、最新用户消息、历史上下文引用）。
- 执行中把事件（`retrieving`、`answer_chunk`、`ticket_created`、`done`）发布到 `conversation:{conversation_id}` 的 Pub/Sub 频道。
- 用 LangGraph checkpoint（存 Redis）保存图执行状态，支持 crash 后从断点恢复而不是从头重跑。
- **验收标准**：向 `agent_tasks` 队列手工写入一条测试任务，Worker 能完整跑完图并在 Pub/Sub 频道观察到预期事件序列；杀掉 Worker 进程重启后，未完成的任务能续跑而不是丢失或重复产生工单。

### 阶段 5：应用服务
- 实现 `POST /conversations/{id}/messages`：写入用户消息，把任务推入 `agent_tasks` 队列，立即返回 202。
- 实现 `GET /conversations/{id}/stream`（SSE）：订阅对应 Pub/Sub 频道，把事件转发给客户端，`done`/`ticket_created` 后关闭连接。
- 实现工单查询接口：`GET /tickets/{id}`、`GET /tickets?tenant_id=`。
- 简单的用户/租户模型 + JWT 鉴权中间件（v1 自建账号体系，登录接口签发 JWT，`tenant_id` 编码进 token）。
- **验收标准**：用 curl/httpie 完整跑通一次"提问 → SSE 收到检索中事件 → 收到回答 → 未解决时收到 ticket_created 事件 → 查询该工单"的闭环。

### 阶段 6：端到端联调与本地部署
- 补齐 `deploy/k8s` 下四个服务的最小 Deployment/Service manifest（不要求 HPA/KEDA 配置齐全，占位注明"生产环境需按架构文档 5 节补充自动扩缩容配置"）。
- 编写一份 `README.md`，说明本地启动步骤、如何跑通 3 个测试场景、如何查看审计日志和用量记录。
- **验收标准**：一个未接触过项目的人，跟着 README 从 0 到能在本地跑通端到端闭环。

## 工作方式要求

- 每个阶段先给出接口/数据结构定义（可以是简短的代码骨架或类型声明），确认结构合理后再补全实现逻辑，不要一次性输出整个阶段的完整代码而不做中间检查点。
- 涉及架构文档中标注为「【假设，可调整】」的决策（入口渠道、租户隔离级别、身份认证方式）：按文档默认值实现，不要自行更改这些决策。
- 如果在某一阶段发现需要偏离本提示词（例如某个库不可用、某个设计在实现层面走不通），停下来说明原因和替代方案，等待确认，不要静默改变范围。
- 每个阶段完成后运行对应的验收标准自测，并在回复中明确说明测试结果，而不是仅声明"已完成"。
