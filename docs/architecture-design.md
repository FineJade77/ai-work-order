# 智能工单系统 —— 架构设计文档（Architecture Design Document）

> 本文档是本项目的架构设计说明，供实现团队对齐设计决策使用；面向 AI 编码 Agent 的可执行实现计划见 [implementation-plan.md](./implementation-plan.md)。所有标注为「【假设，可调整】」的条目是在需求澄清环节未覆盖到、按行业最佳实践给出的默认方案，正式开工前应与需求方确认。

## 1. 项目背景与目标

构建一个面向企业内部员工的**智能工单系统**：

1. 员工提出问题（如"VPN 连不上"），系统优先通过企业知识库检索并生成回答（RAG 问答）。
2. 若知识库回答无法解决问题，系统引导/自动将问题转化为 IT 工单，进入人工处理流程。
3. 第一期只需覆盖部分场景（如 VPN、账号、邮箱等常见 IT 问题），后续逐步扩展知识库覆盖面和自动化能力。
4. 系统需部署到云端，具备**弹性扩缩容**和**多租户**能力，支撑多个企业/多个部门作为独立租户使用同一套系统。

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                              客户端层                                  │
│   Web 工单/问答前端  │  企业IM机器人(飞书/企业微信/Slack，二期)【假设】     │
└───────────────────────────────┬──────────────────────────────────────┘
                                 │ HTTPS
┌───────────────────────────────▼──────────────────────────────────────┐
│  API Gateway                                                          │
│  · 统一入口、TLS 终止、鉴权(JWT/OIDC)、限流、路由、多租户识别(tenant_id)  │
└───────────────────────────────┬──────────────────────────────────────┘
                                 │
┌───────────────────────────────▼──────────────────────────────────────┐
│  应用服务 (Application Service)                                       │
│  · 会话管理 / 用户与租户管理 / 权限校验；员工与 IT 页面的 BFF          │
│  · 编排「问答 → 未解决转工单」的业务流程（工单数据在 ticket-service）  │
│  · 对下游 Agent Runtime 发起任务，接收结果落库                          │
└───────┬───────────────────────────────────────────────┬──────────────┘
        │ 任务入队(Redis Streams)  事件读取(每会话 Stream) │
┌───────▼────────────────┐                    ┌──────────▼─────────────┐
│  Redis Streams           │◄──────结果发布─────│   数据层                │
│  · agent_tasks 队列       │                    │  · PostgreSQL(业务数据) │
│  · Consumer Group        │                    │  · 向量库(知识库，二期)  │
└───────┬──────────────────┘                    │  · Redis(会话/缓存/队列)│
        │ 消费任务                                │  · 对象存储(附件/日志)   │
┌───────▼────────────────┐                    └─────────────────────────┘
│  Agent Runtime（Worker，无状态，多副本）              │
│  (LangGraph)            │
│  · 意图识别/状态图编排    │
│  · ReAct 循环调用工具    │
│  · 会话记忆/上下文管理    │
│  · 执行事件写入每会话 Stream（供应用服务转 SSE）│
└───────┬────────────────┘
        │ REST: /v1/tools/list, /v1/tools/call（OpenAI function 元数据）
┌───────▼──────────────────────────────────────────────────────────────┐
│  Tool Gateway（工具策略网关）                                          │
│  · 角色-工具绑定 / JSON Schema 校验 / 幂等 / 审计 / 服务间令牌鉴权       │
│  · search_knowledge → knowledge-service                              │
│  · create_ticket → ticket-service（TicketProvider 接口）               │
│  · 预留：外部系统工具适配位（Jira/ServiceNow/企业IM，未来可插拔）        │
└───────┬────────────────────────────────────────────────┬─────────────┘
        │                                                 │
┌───────▼─────────────┐                        ┌──────────▼─────────────┐
│  knowledge-service    │                        │   ticket-service        │
│  v1: 按租户写死的数据  │                        │  · 工单 + 4 状态状态机   │
│  只读检索，无数据库    │                        │  · 乐观锁 + 处理记录     │
│  v2: 向量化+RAG检索   │                        │  · ticket_service 库    │
└──────────────────────┘                        └──────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  AI Gateway                                                            │
│  · 统一 LLM 调用出口：鉴权、限流、成本与用量审计、Prompt/模型版本管理     │
│  · 多供应商可插拔路由：v1 通过 OpenAI-compatible 协议适配器接入，        │
│    首个供应商为 DeepSeek                                              │
│  · 被 Agent Runtime 唯一依赖，Agent 不直连任何模型 SDK                 │
└──────────────────────────────────────────────────────────────────────┘
```

**调用关系约束**：Agent Runtime 只能通过 Tool Gateway 调用外部能力、通过 AI Gateway 调用模型，不允许绕过网关直连数据库或第三方 API——这是保证多租户隔权限、审计、限流集中生效的关键约束。

## 3. 组件职责与接口

### 3.1 API Gateway
- 技术选型：Kong / APISIX / Envoy 均可，云无关方案优先选 **APISIX**（开源、K8s Ingress Controller 生态成熟）。
- 职责：TLS、JWT 校验、租户识别（从 token 或 header 解析 `tenant_id` 并透传下游）、限流（按租户/用户维度）、请求日志。
- 不承载业务逻辑。

### 3.2 应用服务（Application Service）
- 技术选型建议：Node.js(NestJS) 或 Python(FastAPI)，与 Agent Runtime（Python/LangGraph）同语言时集成成本最低，**推荐 FastAPI**。
- 核心领域模型：
  - `Tenant`：租户，带唯一的 `slug`（登录时用来确定租户）
  - `User`：用户（属于某租户），角色 `employee` / `it_staff`，scrypt 密码哈希
  - `Conversation` / `Message`：问答会话
  - `Turn`：一条用户消息及其处理状态（`queued/done/failed`），`id` 就是投递给 Agent Runtime 的 `task_id`；部分唯一索引保证同一会话同时只有一轮在处理
  - 工单不在这里：归 ticket-service 所有（见 3.7 节）。员工"我的工单"、IT 人员处理工单的页面由应用服务调用 ticket-service 的内部接口实现，`start`/`resolve` 这类 IT 操作的角色权限也在应用服务里判断。
- 核心业务流程：
  1. 接收用户提问 → 创建/追加 `Conversation`。
  2. 调用 Agent Runtime 执行任务，传入 `tenant_id`、会话上下文。
  3. 若 Agent 返回「已通过知识库解决」→ 落库回答，结束。
  4. 若 Agent 返回「需要转工单」或用户主动要求 → 调用 Tool Gateway 的 `create_ticket` 生成工单，返回工单号给用户。
- 登录与鉴权：`POST /v1/auth/login {tenant, email, password}`，先按 slug 查租户（`tenants` 无 RLS），再在租户作用域内查用户；三种失败统一 401，账号不存在时也做一次假校验，耗时一致。签发 HS256 JWT（`sub/tid/role`，8 小时，无刷新），之后所有接口的租户和用户只取自 JWT。scrypt 在线程里执行，并限制同时最多 4 个（每个约 128 MiB）。以后上 API Gateway 时改 RS256。
- 会话接口：`POST/GET /v1/conversations`、`GET /v1/conversations/{id}/messages`（含进行中的 `active_task_id`，页面刷新后可重新接上 SSE）、`POST .../messages`（202 `{task_id}`；上一轮未结束返回 409 `TURN_IN_PROGRESS`）、`GET .../stream?task_id=`（SSE）。会话只有本人可见，其他人一律 404。
- 回复落库：后台消费 `agent_results`（消费组 `app_service`），按轮次 upsert 唯一的助手消息并关闭轮次，可重复投递；不依赖 SSE 连接。晚到的成功结果会覆盖此前的失败提示。
- 补偿：每 30 秒一次（advisory lock 保证只有一个副本在扫），把 `queued` 超过 5 分钟的轮次用同一个 `task_id` 重新入队，最多 2 次，之后标记失败并写 `error` 事件。5 分钟大于 Agent Runtime 自身重试的最长时间（3 次 × 90 秒）。
- 工单页面（BFF）：员工的列表固定按本人过滤，看别人的工单返回 404；`start`/`resolve` 只允许 `it_staff`，`start` 固定指派给自己；`actor_id` 总是当前用户。ticket-service 的业务错误（400/403/404/409）原样透传。
- 对 Agent Runtime 是**异步任务分发**：应用服务不直接同步调用 Agent Runtime，而是把任务（`conversation_id` + 上下文 + `tenant_id`）写入 Redis Streams 队列即返回；随后从该会话的事件 Stream（`conversation:{conversation_id}:events`）读取 Agent Runtime 陆续写入的中间事件（"正在检索知识库…"、增量 token、最终结果）通过 SSE/WebSocket 转发给前端。对前端而言体验仍是"流式返回"，但应用服务与 Agent Runtime 之间是解耦的异步关系，见 3.3 节。

### 3.3 Agent Runtime（LangGraph）

**部署模型：异步 Worker + 队列，而非同步长连接服务。**

- 应用服务把任务写入 **Redis Streams**（`agent_tasks` 队列，按租户或全局分区均可），Agent Runtime 作为**无状态 Worker** 通过 Consumer Group 消费任务，一个任务对应一次 LangGraph 图的完整执行。
- 执行过程中，Worker 把中间事件（检索中、增量回答 token、工单已创建、结束）写入该会话自己的 **Redis Stream**（`conversation:{conversation_id}:events`，约 1000 条上限、24 小时过期），应用服务读取后转发给前端，从而在异步执行的同时保留流式体验。
  - 不用 Pub/Sub：Pub/Sub 发布时没有订阅者就丢，应用服务返回 202 到前端连上 SSE 之间的事件会丢失；Stream 可以从任意位置读，SSE 断线后可用 Last-Event-ID 续读，应用服务也能据此可靠地把回答落库。
- 选择这个模型而非"应用服务同步调用、Agent Runtime 常驻等待"的原因：
  1. 一次 Agent 执行常包含多次工具调用 + 一次 LLM 生成，耗时数秒到十几秒；同步模型下每个进行中会话都要独占一个连接/线程直到图跑完，扩缩容单位变成"并发连接数"而非"实际计算负载"，HPA 难以精确伸缩。
  2. Worker 无状态、可随时重启，配合 LangGraph 的 checkpoint 机制，任务在 crash 后可从断点恢复，天然具备重试能力；同步模型下 Pod 重启会直接中断并丢失正在处理的会话。
  3. 队列深度是比 CPU/内存更贴合负载本质的扩缩容信号，可用 **KEDA 的 redis-streams scaler** 按 `agent_tasks` 队列长度精确扩缩容 Worker 副本数。
  4. 代价是多了队列 + Pub/Sub 一跳延迟（通常几十毫秒，相对 LLM 秒级延迟可忽略）和一套需要运维的组件；由于 Redis 已经是会话缓存/checkpoint 的依赖，复用同一 Redis 实例做队列，不新增基础设施种类。
- 用 LangGraph 构建一个**固定状态图**（不做 ReAct 自由调工具：AI Gateway 只有文本流接口，V1 流程固定，固定图更可测），节点：
  1. `classify_intent`：LLM 判断意图（三个场景内 / 场景外 / 明确要求开工单 / 寒暄或描述不清）和类别（`vpn/account/email/other`，即 `create_ticket` 的 category 枚举）。输出解析失败按"描述不清"处理。
  2. `retrieve_knowledge`：调用 Tool Gateway 的 `search_knowledge`；无命中直接转工单，不让模型硬答。
  3. `generate_answer`：调用 AI Gateway 基于检索结果生成回答，流式写出事件。
  4. `await_feedback`：LangGraph `interrupt()`，本轮结束；同一会话的下一条消息作为 resume 值继续。
  5. `check_resolution`：LLM 根据员工回复判断已解决 / 没解决 / 新问题（新问题回到 `classify_intent`）；解析失败按"没解决"处理。
  6. `summarize_ticket` → `create_ticket`：先由 LLM 写工单描述（问题、现象、已尝试的知识库方案），再调用 Tool Gateway 创建工单。两步分开，是为了崩溃重跑时发出的建单请求完全相同。
  - 场景外、明确要求开工单、检索无命中、反馈没解决，都**自动建单**；寒暄或描述不清回复固定引导语。
- 状态持久化：LangGraph checkpoint 存 **Postgres**（独立的 `agent_runtime` 库，`AsyncPostgresSaver`），`thread_id = conversation_id`，多轮历史就在图状态里，任务里只带最新一条消息。表结构由 langgraph-checkpoint-postgres 管理（迁移任务里跑 `setup()`，不走 Alembic）；表里没有 `tenant_id`，不做 RLS，只有 Agent Runtime 能连这个库。
- 任务契约：`agent_tasks` 消息字段 `task_id, conversation_id, tenant_id, user_id, content`，由应用服务写入。调用 Tool Gateway 时 `session_id = conversation_id`，`user_id` 原样透传，`agent_role` 取配置（`it_support_agent`）。
- 崩溃恢复与不重复建单：
  - 每个任务结束时，在写 `done`/`error` 事件的同一个 Redis 事务里，向全局 `agent_results` Stream 写一条结果（状态、outcome、这一轮唯一的助手消息、ticket_id），应用服务用消费组消费并落库，不依赖浏览器是否还连着。
  - 图跑到结束或 interrupt、并写出 `done` 事件后才 XACK；未确认的消息空闲超过阈值后由任一 Worker 用 XAUTOCLAIM 接管，投递超过 3 次进死信 `agent_tasks:dead` 并写 `error` 事件。
  - 接管后按 `thread_id` 从最后一个 checkpoint 续跑（`durability="sync"`，每个节点完成即落盘），只重跑崩溃时正在执行的节点；重跑回答前写 `resumed` 事件，前端丢弃已显示的半截回答。
  - 建单幂等键固定为 `conversation_id:task_id`：Tool Gateway 重放已成功的调用；上次卡在 running 时返回 `IN_PROGRESS`，Worker 不确认，等下次接管；ticket-service 也按同一个键去重。
  - 同一会话串行：应用服务在上一轮未结束时拒绝新消息，Worker 再加会话级 Redis 锁兜底。
  - 时序约束 `任务超时(60s) < 会话锁 TTL(75s) < 接管空闲阈值(90s)`，配置里强制校验：锁要比任何存活的运行更久；被 kill 的 Worker 留下的锁要在任务被接管前过期，否则接管会白白消耗投递次数。接管阈值同时大于 Tool Gateway 的 running 过期时间（60s）。
- 进程形态：单进程内按信号量并发跑多个任务（默认 8，都是 IO 等待）；`/healthz` 只给探针用，不建 Service。任务队列放在 `TaskQueue` 接口后面，以后换 RabbitMQ 只改这一层。
- 多租户：图的运行时上下文携带 `tenant_id`，所有工具调用、模型调用都需透传，Tool Gateway/AI Gateway 侧做租户级配置隔离（如某租户自定义知识库、自定义系统提示词）。
- 第一期场景范围：通过配置/路由规则限定只处理"部分场景"（如 VPN、账号密码、邮箱），未命中场景直接引导人工提单，而非勉强让模型作答。

### 3.4 Tool Gateway（工具策略网关）
- 定位：Agent Runtime 与真实世界之间的**唯一策略执行点**。Agent Runtime 是多副本无状态 Worker，权限、幂等如果放在 Worker 里，每个副本各管一份就失效了，所以集中在这里。
- 接口（REST，取代早期的 MCP 方案——MCP 端点不经过权限校验，保留它等于留一条绕过策略的后门）：
  - `GET /v1/tools/list?tenant_id&agent_role`：返回该角色可用工具的 OpenAI function 元数据，**不含密钥、下游地址、handler**。
  - `POST /v1/tools/call`：入参 `tenant_id, session_id, agent_role, tool_name, arguments`，副作用工具必须带 `Idempotency-Key` 头。返回 `success / error` 信封。
- 错误分两类：**模型可处理的**（未知工具、无权限、参数不合法、执行失败）走 200 信封回给模型；**调用方违约的**（非法 tenant、缺幂等键、幂等键冲突、并发执行中）走 HTTP 4xx，只给 Agent Runtime 看。
- 调用管线：租户校验 → 工具存在 → 角色绑定 → JSON Schema → 只读工具直接执行；副作用工具走幂等重放 / 原子占位 → 执行 → 记录结果 → 审计。
- 上下文注入：`session_id`（即会话 ID）和幂等键由网关注入给 handler，不出现在给模型看的参数 schema 里，模型没法伪造或写错。
- 幂等分两层：网关 `tool_calls` 表覆盖普通重试；执行完成但结果未落盘就崩溃的窗口，靠卡住的 `running` 记录超时后可被重新占位、再执行，因此下游也必须幂等（`create_ticket` 把同一个键透传给 app-service 的内部建单接口）。只读工具不写 `tool_calls`。
- 下游：`search_knowledge` 调 knowledge-service（3.6 节）；`create_ticket` 调 ticket-service（3.7 节），`TicketProvider` 的内部实现和未来的 Jira 实现都是"调用工单系统的 HTTP 接口"，形态一致。
- 运行时上下文：`/v1/tools/call` 除 `session_id` 外还要求 `user_id`（当前员工），由 Agent Runtime 提供，模型看不到；`create_ticket` 用它作为提单人 `requester_id`，审计日志也记录它，并参与幂等请求指纹。
- 脱敏：返回体只屏蔽 password/token 等凭据字段（知识库内容里的 IT 值班电话是员工需要的）；审计记录额外对手机号/邮箱/身份证做正则脱敏。
- 核心工具（v1）：`search_knowledge(query)`、`create_ticket(summary, category)`。
- **V1 暂不做、已有设计可恢复**（实现见提交 `f36530d`）：
  - HITL 审批：`pending_approval` 状态、审批接口、`tool_approvals` 表、Redis Stream 恢复事件。等出现重置密码/解锁账号/开通权限这类高风险工具时再加。
  - Redis 令牌桶限流：V1 工具都是 PG 查询、量小；按用户限流放在 API Gateway，Agent 死循环靠 LangGraph `recursion_limit`。
- 扩展设计：`TicketProvider` 接口预留多实现（`InternalTicketProvider` 默认启用，未来可加 `JiraTicketProvider`、`FeishuTicketProvider`），通过租户级配置选择实现，不改动 Agent/应用服务代码。

### 3.5 AI Gateway
- 职责：模型调用的统一出口——鉴权、限流、成本与 token 用量审计、Prompt 模板/模型版本集中管理、多租户模型配置（不同租户可用不同系统提示词或模型档位）。
- **模型接入方式（已确认）**：`ModelProvider` 抽象下实现一个通用的 `OpenAICompatibleProvider`，只需配置 `base_url` + `api_key` + `model` 即可接入任何遵循 OpenAI Chat Completions 接口规范（`/v1/chat/completions`）的供应商，不用为每个供应商单独写适配代码。
  - v1 首个接入的具体供应商是 **DeepSeek**（`base_url=https://api.deepseek.com`，走同一套 OpenAI-compatible 实现），模型档位、Key 通过租户级/环境配置管理。
  - 后续如需接入 Anthropic Claude 等非 OpenAI-compatible 供应商，在 `ModelProvider` 抽象下新增一个专用实现（如 `AnthropicProvider`）即可，路由层按租户/模型名分发，不影响已接入的 OpenAI-compatible 供应商。
- 记录每次调用的 `tenant_id + 模型 + token 数 + 耗时`，为后续按租户计费/限额做准备。客户端中途断开时不记录（写入没有屏蔽取消，有意接受的缺口）。
- 服务间令牌鉴权：`/v1/generate` 要求 Bearer 服务令牌（`AI_GATEWAY_SERVICE_TOKEN`），只有 Agent Runtime 能调用；`tenant_id` 在请求体里，没有令牌任何 pod 都能以任意租户消耗模型额度。

### 3.6 知识库服务（knowledge-service）
- 定位：知识检索的唯一出口，V1 **只读、只有检索一个接口**，不做 RAG/向量。
- 接口：`GET /internal/v1/knowledge/search?tenant_id&q&limit`（服务令牌鉴权）→ `{results: [{id, category, question, answer, score}]}`。
- 数据：`app/data/knowledge.json`，按租户 ID 分组、随镜像发布，改内容要重新部署。未知租户返回空列表（租户注册表归应用服务）。
- 匹配：字符二元组覆盖率，问题中包含完整查询词时记 1.0；不需要中文分词，结果确定、可测。
- 扩展点：`KnowledgeRepository` 接口，V1 实现是 `StaticKnowledgeRepository`；换成数据库或向量检索时接口不变，Tool Gateway 无感知。
- 无状态、无数据库，扩容就是加副本。

### 3.7 工单服务（ticket-service）
- 定位：工单数据与生命周期的唯一所有者；Tool Gateway（Agent 建单）和应用服务（员工/IT 页面）都通过它的内部接口操作工单。
- 状态机（4 个状态，"重新打开"是动作不是状态）：
  - `open --start(指定处理人)--> in_progress --resolve(必须填写说明)--> resolved --close--> closed`
  - `open --cancel--> closed`、`resolved --reopen--> open`
  - `cancel`/`close`/`reopen` 只能由提单人本人执行（服务自己校验 `actor_id == requester_id`）；`start`/`resolve` 的 IT 角色权限由应用服务判断。
- 接口（`/internal/v1`，服务令牌鉴权）：
  - `POST /tickets`（`Idempotency-Key`）：建单，同一幂等键重试返回原工单；201 新建 / 200 重放。
  - `GET /tickets/{id}?tenant_id`：详情 + 完整处理记录。
  - `GET /tickets?tenant_id&requester_id&status&assignee_id&limit&cursor`：按创建时间倒序，游标分页。
  - `POST /tickets/{id}/transitions`：`{action, actor_id, expected_version, assignee_id?, note?}`；非法流转 409、非提单人 403、版本号不一致 409。
- 并发：`tickets.version` 乐观锁，流转时 `UPDATE ... WHERE version = expected` 与写 `ticket_events` 在同一事务，两个操作员同时处理同一版本只有一个成功。
- `ticket_events` 只追加（运行时账号没有 UPDATE/DELETE 权限），同时作为处理记录和审计。
- 暂不做：评论、附件、SLA、状态变更通知（后续可以发 `ticket_events` 到消息队列）。

## 4. 数据模型与数据归属

**每个服务一个数据库，同一个 Postgres 实例**（本地一个容器，云上初期一个托管实例；单个服务要独立扩容时只改它的 `DATABASE_URL`）。服务之间不共享表、不共享模型代码，只通过 API 交互：

| 数据库 | 归属服务 | 表 |
|---|---|---|
| `app_service` | 应用服务 | `tenants`、`users`、`conversations`、`messages`、`turns` |
| `tool_gateway` | Tool Gateway | `role_tool_bindings`、`tool_calls`、`tool_call_logs` |
| `ticket_service` | 工单服务 | `tickets`（含幂等键局部唯一索引、版本号）、`ticket_events`（只追加） |
| `ai_gateway` | AI Gateway | `model_call_logs` |
| `agent_runtime` | Agent Runtime | LangGraph checkpoint 表（`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`，由库自身管理，无 RLS） |
| （无） | 知识库服务 | V1 为代码仓库里按租户写死的 JSON，不需要数据库 |

- 每个服务有两个数据库账号：`<svc>_owner`（建表、迁移、种子数据）和 `<svc>_app`（运行时账号，不是表的所有者，RLS 对它强制生效），且只能连自己的库。本地由 `deploy/postgres/init-databases.sql` 创建，云上由基础设施创建、密码来自密钥管理。
- 每个服务自带 Alembic 迁移，部署时以独立任务先跑迁移再起服务（compose 用 `<svc>-migrate` 服务，K8s 用 Job/initContainer）。
- 不做跨库外键：非 app-service 的 `tenant_id` 是普通 UUID 列，租户合法性由 API Gateway 校验 JWT 保证。
- 多租户隔离：库内仍然用 `tenant_id` + PostgreSQL RLS，每次查询都在租户作用域内执行。
- 跨服务写数据走 API：Tool Gateway 的 `create_ticket` 调 ticket-service 的内部接口 `POST /internal/v1/tickets`（透传幂等键），不直接写工单表。
- 代价：跨服务报表（比如把工具调用和工单关联起来）不能再直接写 SQL join，需要时再做数据同步。

## 5. 部署与云原生方案

- 目标：**云无关，基于标准 Kubernetes**，不绑定具体云厂商托管服务，便于跨云/私有化部署。
- 组件容器化，每个逻辑组件（API Gateway 除外，用 Ingress Controller）独立部署：`app-service`（Deployment，处理 HTTP/SSE）、`agent-runtime`（Deployment，作为 **无状态 Worker** 消费队列，不对外暴露 HTTP 端口）、`tool-gateway`、`ai-gateway`。
- 弹性扩缩容：`app-service`/`tool-gateway`/`ai-gateway` 用标准 **HPA**（CPU/内存/QPS）；`agent-runtime` 用 **KEDA 的 redis-streams scaler**，按 `agent_tasks` 队列的 pending 消息数扩缩容 Worker 副本，负载突增（如上班早高峰集中反馈 VPN 问题）时可快速扩容，队列清空后缩容到最小副本数甚至 0。
- 有状态依赖（PostgreSQL、Redis、向量库）优先使用云无关 Operator（如 CloudNativePG、Redis Operator）或直接对接任意云厂商的托管兼容服务，业务代码只依赖标准协议（Postgres wire protocol / Redis protocol），不绑定专有 SDK。
- 可观测性：OpenTelemetry 统一埋点（Trace 贯穿 API Gateway → 应用服务 → Agent Runtime → Tool/AI Gateway），Prometheus + Grafana 做指标，日志集中到 Loki/ELK，按 `tenant_id` 打标签便于按租户排查问题和统计用量。
- CI/CD：镜像构建 + Helm Chart 管理各组件的多环境（dev/staging/prod）部署配置，租户级配置（如启用的工具、模型档位）通过 ConfigMap/数据库配置表管理，不重新部署即可生效。

## 6. 安全与合规要点

- API Gateway 统一做认证；【假设，可调整】v1 用自建账号体系 + JWT，架构预留标准 **OIDC/SAML** 接入点，便于后续对接企业 SSO（Azure AD/Okta/自建 IdP）。
- Tool Gateway 对每次工具调用做审计日志（谁、哪个租户、调用了什么工具、参数摘要），满足事后追溯需求。
- AI Gateway 层面对发送给模型的内容做基本脱敏（如工号、内部 IP 等敏感信息按需过滤），尤其在使用外部模型供应商时。
- 租户间数据隔离通过 RLS + 应用层双重校验，杜绝越权访问。

## 7. 分阶段路线图

| 阶段 | 范围 | 关键交付 |
|---|---|---|
| V1 | 单租户/多租户基础框架跑通；仅覆盖 VPN/账号等 2-3 个高频场景；知识库用简单 QA 表；工单走自建模块 | 端到端可用的问答+转工单闭环 |
| V2 | 知识库升级为向量检索 + RAG；扩大场景覆盖；接入企业 IM 机器人入口 | 检索准确率提升，渠道扩展 |
| V3 | 多租户强隔离选项（schema级）；AI Gateway 多模型路由与成本优化；对接外部工单系统（Jira/ServiceNow）作为可选 Provider | 面向更大规模企业客户的可售卖能力 |

## 8. 需求方需二次确认的假设清单

1. 入口渠道：默认先做 Web，IM 机器人作为二期扩展点。
2. 身份认证：默认 v1 自建 JWT 账号体系，预留 OIDC 接入点。

以上假设如与实际需求不符，请指出对应条目，架构将做相应调整。

## 9. 已确认的关键决策（变更记录）

- 部署：云无关，基于标准 Kubernetes。
- Agent Runtime：LangGraph 固定状态图，异步 Worker + Redis Streams 任务队列 + 每会话 Redis Stream 事件回传（最初为 Pub/Sub，因会丢未订阅时的事件而改）；checkpoint 存 Postgres `agent_runtime` 库；知识库没解决时自动建单（见 3.3 节）。
- 工单系统：自建工单模块，`TicketProvider` 抽象预留外部系统扩展点。
- 知识库：独立的 knowledge-service，V1 只提供检索、数据按租户写死；后续在同一接口后面换成数据库/向量检索。
- 工单：独立的 ticket-service，4 状态状态机 + 乐观锁 + 处理记录；应用服务不再持有工单表。
- 数据：同一 Postgres 实例、每服务独立 database，库内 `tenant_id` + RLS 隔离；服务间不共享表和模型代码，没有 `libs/shared`（见第 4 节）。
- Tool Gateway：REST 策略网关；HITL 审批与 Redis 限流 V1 暂不做（见 3.4 节）。
- 队列服务：Redis Streams（复用会话缓存的同一 Redis 实例）。对比过 RabbitMQ/RocketMQ：规模用不上、事件回传仍需 Redis，V1 不引入；Redis 可能丢消息的问题由应用服务记录任务状态并重新入队弥补（阶段 5）。
- AI Gateway：服务令牌鉴权，只有 Agent Runtime 可调用。
- **AI Gateway 模型接入：v1 通过 OpenAI-compatible 协议适配器接入，首个供应商为 DeepSeek；`ModelProvider` 抽象支持后续接入非 OpenAI-compatible 供应商。**
