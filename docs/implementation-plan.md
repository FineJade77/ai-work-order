# 智能工单系统 V1 —— 实现计划（Implementation Plan）

> 本文档是可直接交给 AI 编码 Agent（如 Claude Code）执行的实现指令，用于驱动 V1 范围的代码落地。设计依据见 [architecture-design.md](./architecture-design.md)，本计划不重复架构决策的论证过程，只给出可执行的任务分解与验收标准。若执行中发现本计划与架构文档冲突，以架构文档为准并向人类确认后再继续。

## 角色与目标

你是本项目的实现 Agent。目标：在 V1 范围内，交付一个端到端可运行的"员工提问 → 知识库检索回答 → 未解决则自动转工单"闭环。系统不限于 IT：能自助解答什么由每个租户的知识库决定，代码里不写死领域和类别（演示数据：`demo` 租户是 IT，`northwind` 租户是 HR/行政）。部署为可在本地 docker-compose 和标准 K8s 上运行的服务集合。

**V1 明确不做**（避免过度设计）：向量检索/RAG、企业 IM 机器人接入、外部工单系统（Jira 等）对接、OIDC/SSO 真实对接、多模型路由、schema 级租户隔离。这些是二三期范围，V1 只需在接口/抽象层留好扩展点，不需要提前实现。

## 技术栈约束（已确定，不要重新选型）

- 语言：Python 3.11+，应用服务与 Agent Runtime 统一用 Python，降低集成成本。
- 应用服务框架：FastAPI。
- Agent 编排：LangGraph。
- 队列 + 流式回传：Redis Streams——全局任务队列 `agent_tasks`、全局结果流 `agent_results`、每会话事件流 `conversation:{id}:events`（不用 Pub/Sub：没有订阅者时会丢事件），复用同一个 Redis 实例。
- 数据库：PostgreSQL，同一实例、每服务独立 database，库内 `tenant_id` 行级隔离（RLS）。
- 工具协议：Tool Gateway 对 Agent Runtime 暴露 REST 接口（`/v1/tools/call` 执行；`/v1/tools/list` 返回 OpenAI function 元数据，V1 的固定状态图不用它），详见架构文档 3.4 节。
- 模型供应商：AI Gateway v1 通过 OpenAI-compatible 协议接入，具体供应商为 **DeepSeek**（`base_url=https://api.deepseek.com`），用 `ModelProvider` 抽象 + 通用 `OpenAICompatibleProvider` 实现，不要为 DeepSeek 单独写一套适配代码。
- 容器化：每个服务独立 Dockerfile，提供 `docker-compose.yml` 用于本地一键起环境（Postgres、Redis 与全部服务）。

## 目录结构约定

```
ai-work-order/
├── docs/
├── docker-compose.yml
├── services/
│   ├── app-service/         # FastAPI，会话/租户/用户，员工与处理人页面的 BFF；库 app_service
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
- 建立上述目录结构，六个服务各自的 `pyproject.toml`/依赖声明、最小可运行的健康检查端点（`GET /healthz`）。（最初是四个服务，knowledge-service、ticket-service 在阶段 2.5 拆出。）
- `docker-compose.yml` 拉起 Postgres + Redis + 六个服务，服务间通过 compose 网络互通。
- **验收标准**：`docker compose up` 后六个服务健康检查均返回 200。

### 阶段 1：数据模型与迁移
> 最初是一个共享库 + 一套迁移（`libs/shared`），后改为每服务独立 database、各自迁移（架构文档第 4 节）。以下为当前版本。
- 按架构文档第 4 节的归属建表，每个服务在自己的库里用 Alembic 管理 schema，对含租户数据的表启用 `FORCE ROW LEVEL SECURITY`（按 `tenant_id` 过滤）。
- 各服务的 seed 脚本用固定的 demo 租户 ID 插入数据：app-service 插两个 demo 租户的租户/用户/会话，tool-gateway 插角色工具绑定；知识内容在 knowledge-service 的静态 JSON 里（见阶段 2.5）。
- **验收标准**：迁移可重复执行（up/down 幂等），seed 脚本跑完后能查到预置数据，且用非本租户 `tenant_id` 查询取不到数据（验证 RLS 生效）。

### 阶段 2：Tool Gateway（工具策略网关）
> 最初按 MCP 接口实现，后改为 REST 策略网关（架构文档 3.4 节）。以下为当前版本。
- `GET /v1/tools/list?tenant_id&agent_role`：返回角色可用工具的 OpenAI function 元数据，不含密钥/下游地址。
- `POST /v1/tools/call`：`tenant_id, session_id, user_id, agent_role, tool_name, arguments`，返回 `success / error`；副作用工具必须带 `Idempotency-Key` 头。
- 能力：多租户（RLS）、`role_tool_bindings` 角色工具绑定、JSON Schema 参数校验、副作用工具的 `tool_calls` 幂等记录、`tool_call_logs` 审计（参数脱敏）、返回体凭据字段屏蔽、服务间 Bearer 令牌鉴权。Redis 限流 V1 暂不做；HITL 审批见阶段 8。
- 工具：`search_knowledge(query)` 调 knowledge-service，结果带每条的 `category`；`create_ticket(summary, category)`（`category` 为 1–50 字的任意字符串，不是固定枚举）调 ticket-service 的 `POST /internal/v1/tickets`（会话 ID、员工 `user_id`、幂等键由网关注入，幂等键透传）。`TicketProvider` 抽象，v1 只有 `InternalTicketProvider`。
- 调用上下文：`/v1/tools/call` 必须带 `user_id`（当前员工 UUID），作为工单提单人，记入审计和幂等指纹。
- Agent 角色为 `service_agent`（迁移 0003 由 `it_support_agent` 改名，临时关闭 FORCE；已有同名绑定时保留新的、删掉旧的）。
- 代码分层：`api/v1`（路由）→ `services`（调用管线，不依赖 FastAPI）→ `repositories`（网关自身的表）/ `tools`（工具定义与 handler）；`core` 放配置、鉴权、错误、脱敏。
- **验收标准**：`tests/unit`（无外部依赖）与 `tests/integration`（需起 tool-gateway 及其依赖的 knowledge-service、ticket-service）全部通过；覆盖权限、校验（含任意租户的类别都能通过、空或超长的被拒）、幂等重放与冲突、脱敏、审计；检索结果带 `category`；同一幂等键的并发请求只产生一张工单；`running` 状态孤儿记录超时后可恢复且不重复执行副作用。

### 阶段 2.5：知识库服务与工单服务
- knowledge-service：只有 `GET /internal/v1/knowledge/search`，两个 demo 租户各自写死的 QA 数据，领域和类别不同（`demo`：IT，`vpn/account/email/other`；`northwind`：HR/行政，`leave/expense/access/facility/payroll`），用来验证租户隔离和"类别由数据决定"，字符二元组匹配；`KnowledgeRepository` 接口留给后续数据库/向量实现。
- ticket-service：自己的库和迁移；建单（幂等）、详情（含处理记录）、列表（游标分页）、状态流转（4 状态状态机、提单人专属动作、`version` 乐观锁、`ticket_events` 只追加）。
- **验收标准**：knowledge-service 单测覆盖匹配、租户隔离（一个租户的问题在另一个租户查不到）、各租户返回自己的类别、鉴权，容器测试确认数据文件随镜像发布；ticket-service 覆盖完整生命周期、非法流转、非提单人拒绝、版本冲突、8 路并发流转只有一个成功、分页无遗漏无重复、跨租户不可见。

### 阶段 3：AI Gateway
- 实现 `ModelProvider` 抽象 + 通用 `OpenAICompatibleProvider` 实现（用 OpenAI SDK 即可，只需把 `base_url` 指向配置项，不要绑定 OpenAI 官方域名），暴露统一的 `generate(messages, tenant_id, system_prompt) -> stream[chunk]` 接口（支持流式）。
- 配置一个具体供应商实例：DeepSeek（`base_url=https://api.deepseek.com`，模型名走配置项，如 `deepseek-chat`）。
- 每次调用记录 `tenant_id + 模型名 + 输入/输出 token 数 + 耗时` 到日志或表。
- API Key（DeepSeek Key）通过环境变量/密钥管理注入，不要硬编码，不要打印到日志。配置统一走 `app/core/config.py` 的 Settings，缺配置时启动即失败。
- `POST /v1/generate` 要求 Bearer 服务令牌（`AI_GATEWAY_SERVICE_TOKEN`）。
- 每日 token 预算：流开始前汇总租户当天（`BUDGET_TIMEZONE`，默认 `Asia/Shanghai`）已用 token，达到预算返回 429 `TOKEN_BUDGET_EXCEEDED`，`Retry-After` 到次日零点。默认值 `DAILY_TOKEN_BUDGET`（1,000,000），迁移 0002 建 `tenant_token_budgets`（按租户覆盖，只开 RLS 不 FORCE，owner 可直接用 SQL 维护）并给 `model_call_logs` 加 `(tenant_id, created_at)` 索引。
- **验收标准**：给定一个测试 prompt，能拿到流式返回的 token，且用量记录可查询；`tests/unit` 覆盖鉴权、租户校验与预算拒绝都发生在流开始前、预算日按时区划分；`tests/integration`（真 Postgres，运行时账号）覆盖用量达到预算即拒、只算当天、不算别的租户、按租户覆盖默认值。

### 阶段 4：Agent Runtime
> 最初计划 checkpoint 存 Redis、事件走 Pub/Sub，后改为 checkpoint 存 Postgres、事件写每会话 Redis Stream（架构文档 3.3 节）。以下为当前版本。
- 用 LangGraph 实现固定状态图：`classify_intent → retrieve_knowledge → generate_answer → await_feedback(interrupt) → check_resolution → (end | summarize_ticket → create_ticket)`，另有 `reply`（寒暄/描述不清）分支；`new_question` 回到 `classify_intent`。
- `classify_intent` 只判断意图（`question` / `ticket_request` / `chitchat`），不判断领域和类别；提示词和回复文案不提 IT。明确要求开工单、检索无命中、反馈没解决都自动建单（检索过但没命中的，工单描述带 `[知识库未命中]` 标记），不勉强让模型作答。
- 工单类别取自命中条目的 `category`；没检索、没命中或类别不可用时为 `other`。同一轮里提出新问题时，分诊会清空上一个问题的检索结果和类别。
- `agent_role` 默认 `service_agent`。
- 检索失败不等于无命中：临时性故障（`TOOL_ERROR`、连不上/超时、5xx）节点内最多试 3 次，配置类错误（`TOOL_NOT_ALLOWED` 等、其他 4xx）不重试；仍失败走 `knowledge_unavailable` 固定回复，不建单，记 `knowledge_unavailable …` ERROR 日志。
- AI Gateway 返回 429 `TOKEN_BUDGET_EXCEEDED` 时，runner 以固定回复结束这一轮（outcome `budget_exceeded`），任务 ACK、不重试、不建单，记 `budget_exceeded …` ERROR 日志。
- 作为无状态 Worker：通过 Consumer Group 消费 `agent_tasks`（消息字段 `task_id, seq, conversation_id, tenant_id, user_id, content`；`user_id` 原样传给 Tool Gateway 作为提单人）。历史上下文在 checkpoint 里，不随任务传。
- 跳过旧轮次的副本：拿到会话锁后，`seq` 低于 Redis 会话下限 `agent:floor:{conversation_id}` 的任务直接 ACK；图状态里记录最近一轮的 `seq`，更小的任务也直接 ACK（Redis 丢下限时兜底）。两种情况都不跑图、不写事件和结果。
- 执行事件（`retrieving`、`answer_chunk`、`answer`、`reply`、`ticket_created`、`resumed`、`error`、`done`）写入 `conversation:{conversation_id}:events` Stream。
- checkpoint 存 Postgres `agent_runtime` 库（`agent-runtime-migrate` 任务跑 `setup()` 并授权运行时账号），`durability="sync"`，crash 后从断点续跑。
- 调用 AI Gateway 带服务令牌。
- **验收标准**：`tests/unit`（假网关 + 内存 checkpointer，覆盖每条分支、interrupt/resume、解析失败兜底、崩溃续跑、工单类别来自命中条目（含非 IT 租户、类别缺失或超长时回退 `other`、新问题不继承旧类别）、检索重试与降级、预算拒绝不影响下一轮、旧副本不被当成反馈）与 `tests/integration`（真 Redis、Postgres、tool-gateway、ticket-service，模型用假实现；覆盖旧副本在有 / 没有 Redis 下限时都被跳过、无工具绑定的租户得到降级回复而不是工单、预算拒绝被 ACK 不重试、HR 租户的工单类别为 `leave`）全部通过；向 `agent_tasks` 写入测试任务，能在事件 Stream 上看到预期序列；分别在回答输出途中和生成工单摘要途中 `kill -9` Worker 再重启，任务续跑完成且该会话只有一张工单。

### 阶段 5：应用服务
> 工单数据与接口已在 ticket-service（阶段 2.5）。本阶段的员工"我的工单"和处理人页面由应用服务调用 ticket-service 实现，`start`/`resolve` 的处理人角色校验在应用服务做。
- 登录：`POST /v1/auth/login {tenant(slug), email, password}` 签发 HS256 JWT（`sub/tid/role`）；`GET /v1/me`。迁移 0003 加 `tenants.slug`、`users.password_hash`、角色约束、`turns` 表、`messages.task_id`。角色为 `employee` / `handler`（迁移 0005 由 `it_staff` 改名，临时关闭 FORCE；改名前签发的令牌失效）；非处理人执行 `start`/`resolve` 返回 403 `NOT_HANDLER`。
- `POST /v1/conversations/{id}/messages`：先按员工限流（每分钟 6 条、每天 200 条，Redis 固定窗口，超出 429 `RATE_LIMITED` 带 `Retry-After`，Redis 不可用时放行）；同一事务写用户消息和 `turns`（分配会话内序号 `seq`，迁移 0004；同一会话只能有一个 `queued`，否则 409），再在一个 Redis 事务里提高会话下限并 XADD `agent_tasks`，返回 202 `{task_id, message_id}`。
- `GET /v1/conversations/{id}/stream?task_id=`（SSE）：从会话事件 Stream 开头读、按 `task_id` 过滤，Stream ID 作为 SSE 的 event id，支持 Last-Event-ID 续读，`done`/`error` 后关闭；事件已过期时用数据库里的结果补发。
- 回复落库：后台消费 `agent_results`，幂等地写助手消息、关闭轮次；已失败的轮次收到晚到的结果时，只在它仍是会话最新一轮时覆盖失败提示。补偿：`queued` 超过 10 分钟的轮次用同一 `task_id`、`seq` 重新入队，最多 2 次后标记失败；阈值配置必须 ≥ 480 秒（Agent Runtime 自身重试的最长时间）。补偿逐个租户扫描，`tenant_scope` 切换租户前先 flush。
- 工单接口：`GET /v1/tickets`（员工只看自己的）、`GET /v1/tickets/{id}`、`POST /v1/tickets/{id}/transitions`，均转调 ticket-service，租户和用户取自 JWT；`start` 固定指派给自己。
- seed：两个 demo 租户（slug `demo`、`northwind`）各一个员工和一个处理人（`it.staff@example.com`、`hr.staff@northwind.example`），开发密码 `demo-password`；重复执行时更新姓名、邮箱、角色。
- **验收标准**：`tests/unit` 与 `tests/integration`（真 Postgres、Redis、ticket-service）全部通过，覆盖登录失败不可区分、会话私有、并发发消息 409、`seq` 递增与会话下限、结果重复投递只存一次、晚到结果仅在最新一轮时覆盖失败提示、SSE 过滤与续读、补偿重投与放弃（含多个租户同时有超时轮次）、发消息限流（分钟/日窗口、被拒请求不落库、Redis 不可用时放行）、工单可见性与角色规则；用 curl 完整跑通"登录 → 提问 → SSE 收到检索中和回答 → 反馈未解决 → 收到 ticket_created → 查询工单 → 处理人处理 → 员工关闭"的闭环。

### 阶段 6：端到端联调与本地部署
- `docker compose up -d --build` 一条命令完成建库、迁移、写入演示数据（`*-seed` 一次性服务）和启动；`.env.example` 说明所有配置项。
- 所有服务镜像以 uid 10001 运行，运行时不写文件（可只读根文件系统）。
- `deploy/k8s`：Kustomize。`base`（各服务 Deployment/Service、迁移 Job、ConfigMap）、`overlays/dev`（单实例 Postgres/Redis、db-init、seed、开发凭据）、`components/autoscaling`（HPA + agent-runtime 的 KEDA ScaledObject，默认不启用）、`apply-dev.sh`（按依赖顺序部署）。
- `README.md`：本地启动、5 个演示场景（`scripts/demo.sh`：demo 租户的 vpn/account/email/printer，其中 printer 是知识库未命中直接建单；northwind 租户的 leave）、curl 完整闭环（含处理人处理）、审计与用量查询、运行测试（`scripts/test-all.sh`）、排障。
- **验收标准**：在不含 `.venv`/`.env` 的干净副本里严格按 README 操作：一条命令启动、演示脚本和 README 中的 curl 代码块原样执行跑通闭环（真实 DeepSeek）、审计与用量查询有输出、`scripts/test-all.sh` 从零建环境全部通过；K8s 清单做静态校验（本地渲染，检查 Secret/ConfigMap 引用、Service 选择器与端口、环境变量与 compose 一致），未在真实集群部署。

### 阶段 7：可观测性（OpenTelemetry）
> 设计见架构文档第 5 节。分三步，每步单独提交。
- **7.1 追踪与日志（已完成）**：六个服务加 OTel 依赖（distro、OTLP HTTP exporter，以及各自用到的 fastapi/httpx/psycopg/logging 插桩），用 `opentelemetry-instrument` 启动，镜像默认关闭 SDK；FastAPI 在代码里插桩（去掉 send/receive 子 span，跳过 `/healthz`）；日志改为带 `trace_id` 的 JSON；compose 新增 `observability`（`grafana/otel-lgtm:0.33.1`），各服务共用 `x-otel-env`。
  - **验收标准**：每个服务的 `tests/unit/test_telemetry.py`（JSON 字段、span 内带 id、重复配置幂等、一次请求只有一个服务端 span 且不记探针）全部通过；跑一次演示后 Tempo 里六个服务都有 trace，agent-runtime → tool-gateway → knowledge-service / ticket-service 在同一条 trace 里且带数据库 span；Loki 能按 `trace_id` 查到这条 trace 的日志。
- **7.2 跨队列与 Agent/LLM（已完成）**：任务和结果消息携带 `traceparent`，一轮对话一条 trace（`turns.traceparent`，app-service 迁移 0006，让补偿重投接回原 trace；扫描本身不追踪）；结果落库用 link；LangGraph 节点 span；ai-gateway 的 GenAI span（内容记录由 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 控制，compose 打开）；锁冲突、跳过旧副本、续跑、降级、预算拒绝、死信记为 span 事件；`scripts/show-trace.py`。
  - **验收标准**：内存 exporter 测试覆盖——agent-runtime：消费端 span 是生产端的子 span、跳过旧副本的事件、结果带运行的 `traceparent`、节点 span 的名字与决定属性、`interrupt` 不算错误、检索失败事件、失败节点为错误；app-service：生产端 span 挂在请求下且消息和 `turns` 带同一条 trace、重投留在原 trace、结果落库 span 有 link；ai-gateway：GenAI 属性、内容只在开关打开时记录且已脱敏、失败为错误。真实环境（真实模型）跑一次演示后，用 `scripts/show-trace.py` 确认一轮对话是一条 trace，覆盖 app-service、agent-runtime、ai-gateway、tool-gateway 以及 knowledge-service 或 ticket-service，含队列生产/消费、节点和 GenAI span。
- **7.3 指标、看板、告警（已完成）**：OTel metrics 覆盖队列（含 `QueueMonitor` 采样的积压/待确认/最老待确认/死信 gauge）、任务、Agent、LLM（GenAI 标准指标）、预算、轮次耗时（SLO）、限流、SSE、工具调用；看板与告警规则由 `deploy/observability/grafana/generate_*.py` 生成并提交，compose 挂载、k8s 组件用 ConfigMap；SLO 95% 的轮次 30 秒内完成，带多窗口燃烧率告警；k8s 可选组件 `components/observability`（otel-lgtm + 给六个服务打开 OTel 的 patch）。
  - **验收标准**：内存 metric reader 测试覆盖——agent-runtime：节点耗时、意图、检索结果计数，每次投递按结果计数和计时，`QueueMonitor` 读出积压、待确认、最老待确认和死信；app-service：一轮只在第一次完成时记一次耗时，限流拒绝按窗口计数；ai-gateway：token 用量和调用耗时；tool-gateway：每次调用按工具和状态计数。真实环境：看板每个面板的查询在演示流量下都有数据（没有发生的事显示 0 而不是无数据）；人为制造死信、停掉 knowledge-service、把租户预算调到约 90% 后，对应的三条告警进入 Alerting，其余保持正常；k8s 组件本地渲染后六个服务都带上 OTel 配置、所有 ConfigMap 引用都能解析。

### 阶段 8：HITL 审批
> 设计见架构文档 3.4 节（网关）、3.3 节（Agent）、3.2 节（应用服务）。分三步，每步单独提交。
- **8.1 Tool Gateway 审批状态机（已完成）**：迁移 0004（`role_tool_bindings.requires_approval`、`tool_calls`/`tool_call_logs` 状态加 `pending_approval`、`tool_approvals`、`approval_outbox`、`access_grants`）；`grant_access` 工具与 `MockAccessProvider`；调用管线的审批分支（开审批、按审批状态恢复、拒绝/过期为最终结果）；审批接口（列表、详情、决定，四眼原则，过期后不能决定）；发件箱 relay（发布决定、到期置为 expired）发到 `approval_events`；seed 让 demo 租户的 `grant_access` 需要审批、northwind 不需要；tool-gateway 新增 `REDIS_URL`。
  - **验收标准**：集成测试（真 tool-gateway、Postgres、Redis）覆盖——批准后同一幂等键再调才执行且只执行一次、之后重放；自批 403；拒绝后再调永远是 `APPROVAL_REJECTED`（附备注）且不执行；无人处理时 relay 置为过期并发事件、之后不能再批准、再调为 `APPROVAL_EXPIRED`；截止时间已过但 relay 还没处理时批准也被拒；另一租户的策略下直接执行；等待中的调用审计为 `pending_approval`；每个决定都出现在 `approval_events`。迁移可降级再升级。
- **8.2 Agent 动作路径与应用服务审批（已完成）**：agent-runtime——任务新增 `kind=approval`（`approval_id`、`decision`、`note`）；分诊的 `action` 意图；`plan_action`（`tools/list` + 模型选工具与参数，幂等键在此决定并落盘）、`execute_action`、`await_approval`（interrupt）、`report_action`；runner 按任务类型与中断类型分流（等审批时员工消息只回复状态、不恢复；对不上的审批任务回报空结果）；客户端认识 `pending_approval`、`list_tools`。app-service——迁移 0007（审批轮次）；`ApprovalEventConsumer` 消费 `approval_events` 开审批轮次并投递任务（按审批 id 去重，会话有轮次在处理时稍后重试）；审批 BFF 接口（处理人决定、员工看自己的）；空结果不写助手消息；补偿重投覆盖审批轮次；app-service 新增 `TOOL_GATEWAY_URL`/`TOOL_GATEWAY_SERVICE_TOKEN`。
  - **验收标准**：agent-runtime 单测覆盖——需要审批时暂停、批准后恢复且全程同一幂等键、工具只执行一次；等待中员工消息只得到状态回复、不分诊不恢复；拒绝（附审批意见）与过期都不执行、不建单；直接执行的策略下无审批；没有可用计划或执行失败转人工；对不上的审批任务回报空结果。端到端（真 tool-gateway）：请求 → 暂停 → 网关批准 → 审批任务恢复 → 开通记录恰好一条。app-service 集成测试：决定事件开审批轮次并投递带审批字段的任务、重复事件不重复开、会话有轮次在处理时等它结束后再开、空结果不写回复；审批接口的角色与可见性，网关错误透传。真实模型走通：申请 → 已提交审批 → 等待中追问得到状态 → 员工自批被拒 → 处理人批准 → 员工收到"已通过审批并办理完成"。
- **8.3 可观测性、演示与文档（待做）**：审批的 span、事件与 link，审批指标、看板和告警；`scripts/demo.sh access`。

## 工作方式要求

- 每个阶段先给出接口/数据结构定义（可以是简短的代码骨架或类型声明），确认结构合理后再补全实现逻辑，不要一次性输出整个阶段的完整代码而不做中间检查点。
- 涉及架构文档中标注为「【假设，可调整】」的决策（入口渠道、身份认证方式）：按文档默认值实现，不要自行更改这些决策。
- 如果在某一阶段发现需要偏离本提示词（例如某个库不可用、某个设计在实现层面走不通），停下来说明原因和替代方案，等待确认，不要静默改变范围。
- 每个阶段完成后运行对应的验收标准自测，并在回复中明确说明测试结果，而不是仅声明"已完成"。
