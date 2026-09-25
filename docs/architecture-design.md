# 智能工单系统 —— 架构设计文档（Architecture Design Document）

> 本文档是本项目的架构设计说明，供实现团队对齐设计决策使用；面向 AI 编码 Agent 的可执行实现计划见 [implementation-plan.md](./implementation-plan.md)。所有标注为「【假设，可调整】」的条目是在需求澄清环节未覆盖到、按行业最佳实践给出的默认方案，正式开工前应与需求方确认。

## 1. 项目背景与目标

构建一个面向企业内部员工的**智能工单系统**：

1. 员工提出问题（如"VPN 连不上"），系统优先通过企业知识库检索并生成回答（RAG 问答）。
2. 若知识库回答无法解决问题，系统引导/自动将问题转化为工单，进入人工处理流程。
3. 不限于 IT：每个租户（企业/部门）的知识库决定它能自助解答哪些问题（IT、人事、行政……），代码里不写死任何领域或类别。V1 的演示数据是 `demo` 租户的 IT 服务台（VPN、账号、邮箱）和 `northwind` 租户的 HR/行政（请假、报销、门禁……），后续逐步扩展知识库覆盖面和自动化能力。
4. 系统需部署到云端，具备**弹性扩缩容**和**多租户**能力，支撑多个企业/多个部门作为独立租户使用同一套系统。

## 2. 总体架构

```
客户端：Web 问答/工单前端【V1 未实现，用 curl / scripts/demo.sh】、企业 IM 机器人（二期）【假设】
  │ HTTPS
  ▼
API Gateway【V1 未部署：JWT 校验和发消息限流由 app-service 自己做】
  │
  ▼
app-service
  ├─ HTTP：员工"我的工单"、处理人处理（查询 / 状态流转）────────▶ ticket-service
  ├─ XADD agent_tasks（同一事务提高会话下限）─────────────────▶ Redis
  ├─ 消费 agent_results：回复落库、关闭轮次 ◀───────────────── Redis
  └─ 读 conversation:{id}:events，经 SSE 推给浏览器 ◀───────── Redis

agent-runtime（LangGraph 固定状态图，无状态 Worker，多副本）
  ├─ XREADGROUP / XAUTOCLAIM 取任务；写事件流、结果流 ────────▶ Redis
  ├─ POST /v1/tools/call ─▶ tool-gateway
  │                           ├─ search_knowledge ──────────▶ knowledge-service
  │                           └─ create_ticket（透传幂等键）─▶ ticket-service
  └─ POST /v1/generate（SSE）─▶ ai-gateway ─▶ DeepSeek（OpenAI 兼容协议）
```

| 组件 | 存储 | 说明 |
|---|---|---|
| app-service | Postgres `app_service` | 登录、会话、轮次、回复落库、补偿重投、工单页面 BFF（3.2 节） |
| agent-runtime | Postgres `agent_runtime`（LangGraph checkpoint） | 不对外提供接口，只消费队列（3.3 节） |
| tool-gateway | Postgres `tool_gateway` | 工具策略执行点：角色绑定、Schema、幂等、审计、脱敏（3.4 节） |
| ai-gateway | Postgres `ai_gateway` | 模型调用唯一出口：服务令牌、每日 token 预算、用量记录（3.5 节） |
| knowledge-service | 无（随镜像发布的静态 JSON） | 只读检索（3.6 节） |
| ticket-service | Postgres `ticket_service` | 工单数据与生命周期的唯一所有者（3.7 节） |
| observability | 容器内（重建即清空） | `grafana/otel-lgtm`：OTel Collector + Tempo + Prometheus + Loki + Grafana，各服务经 OTLP 上报（5 节） |
| Redis | — | 任务队列 `agent_tasks`（含死信）、结果流 `agent_results`、每会话事件流、会话锁、会话下限、发消息限流计数；V1 不做会话缓存 |

- 向量库（知识库 V2）、对象存储（附件）【V1 未实现】。

**调用关系约束**：Agent Runtime 只能通过 Tool Gateway 调用外部能力、通过 AI Gateway 调用模型，不允许绕过网关直连数据库或第三方 API——这是保证多租户隔离、权限、审计、限流和预算集中生效的关键约束。

## 3. 组件职责与接口

### 3.1 API Gateway【V1 未部署】
- V1 现状：没有独立的 API Gateway。JWT 签发与校验、按员工的发消息限流都在 app-service 里（3.2 节）；k8s 清单只有 ClusterIP Service、没有 Ingress，dev 环境用 `kubectl port-forward` 访问 app-service。
- 技术选型：Kong / APISIX / Envoy 均可，云无关方案优先选 **APISIX**（开源、K8s Ingress Controller 生态成熟）。
- 职责：TLS、JWT 校验、租户识别（从 token 或 header 解析 `tenant_id` 并透传下游）、限流（按租户/用户维度）、请求日志。
- 不承载业务逻辑。

### 3.2 应用服务（Application Service）
- 技术选型建议：Node.js(NestJS) 或 Python(FastAPI)，与 Agent Runtime（Python/LangGraph）同语言时集成成本最低，**推荐 FastAPI**。
- 核心领域模型：
  - `Tenant`：租户，带唯一的 `slug`（登录时用来确定租户）
  - `User`：用户（属于某租户），角色 `employee` / `handler`（处理人：处理本租户工单的人，不限于 IT；V1 可以处理本租户所有工单，按类别分组待做；由 `it_staff` 改名，迁移 0005，改名前签发的 JWT 带旧角色，会被拒绝，需重新登录），scrypt 密码哈希
  - `Conversation` / `Message`：问答会话
  - `Turn`：一条用户消息及其处理状态（`queued/done/failed`），`id` 就是投递给 Agent Runtime 的 `task_id`，`seq` 是它在会话内的序号（1、2、3…，唯一约束），`traceparent` 是创建它的请求的追踪上下文（补偿重投时接回同一条 trace）；部分唯一索引保证同一会话同时只有一轮在处理
  - 工单不在这里：归 ticket-service 所有（见 3.7 节）。员工"我的工单"、处理人处理工单的页面由应用服务调用 ticket-service 的内部接口实现，`start`/`resolve` 这类处理人操作的角色权限也在应用服务里判断。
- 核心业务流程（问答与建单的判断都在 Agent Runtime，应用服务不调用 Tool Gateway）：
  1. 员工发消息 → 在同一事务里写用户消息和 `Turn`，把任务投递到 `agent_tasks` → 返回 202。
  2. Agent Runtime 执行状态图：检索知识库并回答、等员工反馈；要求提单、检索无命中或反馈没解决时，由 Agent 经 Tool Gateway 建单（3.3 节）。
  3. 应用服务把执行事件经 SSE 转给前端，并从 `agent_results` 把这一轮的回复（回答、固定回复或建单通知）落库、关闭轮次。
- 登录与鉴权：`POST /v1/auth/login {tenant, email, password}`，先按 slug 查租户（`tenants` 无 RLS），再在租户作用域内查用户；三种失败统一 401，账号不存在时也做一次假校验，耗时一致。签发 HS256 JWT（`sub/tid/role`，8 小时，无刷新），之后所有接口的租户和用户只取自 JWT。scrypt 在线程里执行，并限制同时最多 4 个（每个约 128 MiB）。以后上 API Gateway 时改 RS256。
- 发消息限流：每个员工每分钟 6 条、每天 200 条（可配置），Redis 固定窗口计数，在写库之前检查，超出返回 429 `RATE_LIMITED` 带 `Retry-After`；只限发消息（每条消息要调 2–3 次模型），建会话不限。Redis 不可用时放行。
- 会话接口：`POST/GET /v1/conversations`、`GET /v1/conversations/{id}/messages`（含进行中的 `active_task_id`，页面刷新后可重新接上 SSE）、`POST .../messages`（202 `{task_id}`；上一轮未结束返回 409 `TURN_IN_PROGRESS`）、`GET .../stream?task_id=`（SSE）。会话只有本人可见，其他人一律 404。
- 回复落库：后台消费 `agent_results`（消费组 `app_service`），按轮次 upsert 唯一的助手消息并关闭轮次，可重复投递；不依赖 SSE 连接。晚到的结果只在该轮仍是会话最新一轮时覆盖此前的失败提示；员工已经发了新消息的，丢弃并记日志，免得旧回答插到新消息后面。
- 补偿：每 30 秒一次（advisory lock 保证只有一个副本在扫），把 `queued` 超过 10 分钟的轮次用同一个 `task_id` 和 `seq` 重新入队，最多 2 次，之后标记失败并写 `error` 事件。阈值必须大于 Agent Runtime 自身重试的最长时间：3 次 ×（接管空闲 90 秒 + 扫描间隔 10 秒 + 任务超时 60 秒）= 480 秒，配置里强制校验；否则只是慢的任务会被复制一份，与原任务赛跑。
- 工单页面（BFF）：员工的列表固定按本人过滤，看别人的工单返回 404；`start`/`resolve` 只允许 `handler`（否则 403 `NOT_HANDLER`），`start` 固定指派给自己；`actor_id` 总是当前用户。ticket-service 的业务错误（400/403/404/409）原样透传。
- 对 Agent Runtime 是**异步任务分发**：应用服务不直接同步调用 Agent Runtime，而是把任务（`task_id`、`seq`、`conversation_id`、`tenant_id`、`user_id` 和这条消息的内容；历史在 Agent 的 checkpoint 里，不随任务传）写入 Redis Streams 队列即返回；随后从该会话的事件 Stream（`conversation:{conversation_id}:events`）读取 Agent Runtime 陆续写入的中间事件（"正在检索知识库…"、增量 token、最终结果）通过 SSE 转发给前端。对前端而言体验仍是"流式返回"，但应用服务与 Agent Runtime 之间是解耦的异步关系，见 3.3 节。

### 3.3 Agent Runtime（LangGraph）

**部署模型：异步 Worker + 队列，而非同步长连接服务。**

- 应用服务把任务写入 **Redis Streams**（全局一个 `agent_tasks` 队列），Agent Runtime 作为**无状态 Worker** 通过 Consumer Group 消费任务，一个任务对应一次 LangGraph 图的完整执行。
- 执行过程中，Worker 把中间事件（检索中、增量回答 token、工单已创建、结束）写入该会话自己的 **Redis Stream**（`conversation:{conversation_id}:events`，约 1000 条上限、24 小时过期），应用服务读取后转发给前端，从而在异步执行的同时保留流式体验。
  - 不用 Pub/Sub：Pub/Sub 发布时没有订阅者就丢，应用服务返回 202 到前端连上 SSE 之间的事件会丢失；Stream 可以从任意位置读，SSE 断线后可用 Last-Event-ID 续读，应用服务也能据此可靠地把回答落库。
- 选择这个模型而非"应用服务同步调用、Agent Runtime 常驻等待"的原因：
  1. 一次 Agent 执行常包含多次工具调用 + 一次 LLM 生成，耗时数秒到十几秒；同步模型下每个进行中会话都要独占一个连接/线程直到图跑完，扩缩容单位变成"并发连接数"而非"实际计算负载"，HPA 难以精确伸缩。
  2. Worker 无状态、可随时重启，配合 LangGraph 的 checkpoint 机制，任务在 crash 后可从断点恢复，天然具备重试能力；同步模型下 Pod 重启会直接中断并丢失正在处理的会话。
  3. 队列深度是比 CPU/内存更贴合负载本质的扩缩容信号，可用 **KEDA 的 redis-streams scaler** 按 `agent_tasks` 队列长度精确扩缩容 Worker 副本数。
  4. 代价是多了一跳队列延迟（通常几十毫秒，相对 LLM 秒级延迟可忽略）和一套需要运维的组件；队列、事件流、会话锁都放在同一个 Redis 实例里，不新增基础设施种类。
- 用 LangGraph 构建一个**固定状态图**（不做 ReAct 自由调工具：AI Gateway 只有文本流接口，V1 流程固定，固定图更可测），节点：
  1. `classify_intent`：LLM 只判断意图：`question`（求助/咨询）、`ticket_request`（明确要求提单或转人工）、`chitchat`（寒暄或描述不清）。不判断领域和类别，提示词里不出现任何具体业务。输出解析失败按"描述不清"处理。进入分诊时清空上一个问题的检索结果和类别（同一轮里提出新问题时不继承）。
  2. `retrieve_knowledge`：调用 Tool Gateway 的 `search_knowledge`；无命中直接转工单，不让模型硬答。命中时把第一条的 `category` 记为这个问题的类别——以后若因此建单，就用它作为工单类别。
     - 检索失败不等于无命中：否则知识库故障、或新租户漏配工具绑定，会把每个问题都静默变成工单。临时性故障（`TOOL_ERROR`、连不上/超时、5xx）在节点内最多试 3 次（间隔 0.5s、1s）；配置类错误（`TOOL_NOT_ALLOWED`/`UNKNOWN_TOOL`/`INVALID_ARGUMENTS`、其他 4xx）不重试。仍失败则进入 `knowledge_unavailable`：固定回复"知识库暂时无法访问，可稍后重问或回复「提工单」"，outcome 为 `knowledge_unavailable`，不建单、不等待反馈（下一条消息重新分类）。每次降级记一条 `knowledge_unavailable tenant_id=… code=… attempts=…` 的 ERROR 日志，供告警匹配。
  3. `generate_answer`：调用 AI Gateway 基于检索结果生成回答，流式写出事件。
  4. `await_feedback`：LangGraph `interrupt()`，本轮结束；同一会话的下一条消息作为 resume 值继续。
  5. `check_resolution`：LLM 根据员工回复判断已解决 / 没解决 / 新问题（新问题回到 `classify_intent`）；解析失败按"没解决"处理。
  6. `summarize_ticket` → `create_ticket`：先由 LLM 写工单描述（问题、现象、已尝试的知识库方案），再调用 Tool Gateway 创建工单。两步分开，是为了崩溃重跑时发出的建单请求完全相同。
  - 明确要求开工单、检索无命中、反馈没解决，都**自动建单**；寒暄或描述不清回复固定引导语；检索不可用回复固定说明、不建单。
  - 能自助解答的范围由租户的知识库决定，没有"场景内/场景外"清单：知识库没有的问题检索不到，就转人工。
  - 工单类别：来自命中条目的 `category`（租户自己的标签，如 `vpn`、`leave`、`expense`）；没检索过（直接要求提单）、没命中、或条目的类别为空/超过 50 字时用 `other`。检索过却没命中的工单，描述前加 `[知识库未命中]`。
- 状态持久化：LangGraph checkpoint 存 **Postgres**（独立的 `agent_runtime` 库，`AsyncPostgresSaver`），`thread_id = conversation_id`，多轮历史就在图状态里，任务里只带最新一条消息。表结构由 langgraph-checkpoint-postgres 管理（迁移任务里跑 `setup()`，不走 Alembic）；表里没有 `tenant_id`，不做 RLS，只有 Agent Runtime 能连这个库。
- 任务契约：`agent_tasks` 消息字段 `task_id, seq, conversation_id, tenant_id, user_id, content`，外加 W3C 追踪上下文 `traceparent`（有时还有 `tracestate`），由应用服务写入；`agent_results` 同样带上 Agent 运行的 `traceparent`；投递时在同一个 Redis 事务里把会话下限 `agent:floor:{conversation_id}` 提高到这个 `seq`（只升不降，24 小时过期）。调用 Tool Gateway 时 `session_id = conversation_id`，`user_id` 原样透传，`agent_role` 取配置（`service_agent`）。
- 崩溃恢复与不重复建单：
  - 每个任务结束时，在写 `done`/`error` 事件的同一个 Redis 事务里，向全局 `agent_results` Stream 写一条结果（状态、outcome、这一轮唯一的助手消息、ticket_id），应用服务用消费组消费并落库，不依赖浏览器是否还连着。
  - 图跑到结束或 interrupt、并写出 `done` 事件后才 XACK；未确认的消息空闲超过阈值后由任一 Worker 用 XAUTOCLAIM 接管，投递超过 3 次进死信 `agent_tasks:dead` 并写 `error` 事件。
  - 接管后按 `thread_id` 从最后一个 checkpoint 续跑（`durability="sync"`，每个节点完成即落盘），只重跑崩溃时正在执行的节点；重跑回答前写 `resumed` 事件，前端丢弃已显示的半截回答。
  - 建单幂等键固定为 `conversation_id:task_id`：Tool Gateway 重放已成功的调用；上次卡在 running 时返回 `IN_PROGRESS`，Worker 不确认，等下次接管；ticket-service 也按同一个键去重。
  - 同一会话串行：应用服务在上一轮未结束时拒绝新消息，Worker 再加会话级 Redis 锁兜底。
  - 旧任务不执行：应用服务放弃一轮后（重新入队用尽），这一轮的副本可能还在队列里，而员工已经发了下一条；副本晚到时，如果线程正停在等待反馈，旧问题会被当成对新回答的反馈。两道检查，命中即 XACK、不跑图、不写事件和结果（该轮在应用服务里早已结束）：
    1. 拿到会话锁后，`seq` 低于 Redis 会话下限的任务跳过——挡住"旧副本先于新任务拿到锁"的情况；
    2. 图状态里记录最近一轮的 `seq`（与 `task_id` 同时写入），`seq` 更小的任务跳过——Redis 丢了下限时的兜底。
  - 时序约束 `任务超时(60s) < 会话锁 TTL(75s) < 接管空闲阈值(90s)`，配置里强制校验：锁要比任何存活的运行更久；被 kill 的 Worker 留下的锁要在任务被接管前过期，否则接管会白白消耗投递次数。接管阈值同时大于 Tool Gateway 的 running 过期时间（60s）。
- 预算用完：AI Gateway 返回 429 `TOKEN_BUDGET_EXCEEDED` 时，runner 用固定回复结束这一轮（outcome `budget_exceeded`），任务 ACK 不重试——当天再试也会被拒；不建单（写工单描述要用模型，且额度耗尽时自动建单会冲垮处理人的队列）。线程停在被拒的节点上，同一会话的下一条消息作为新输入会丢弃它。每次记 `budget_exceeded tenant_id=…` 的 ERROR 日志供告警。
- 进程形态：单进程内按信号量并发跑多个任务（默认 8，都是 IO 等待）；`/healthz` 只给探针用，不建 Service。任务队列放在 `TaskQueue` 接口后面，以后换 RabbitMQ 只改这一层。
- 多租户：图的运行时上下文携带 `tenant_id`，所有工具调用、模型调用都需透传。租户级差异 V1 只有两处：知识库内容（knowledge-service 按租户分组）和工具绑定（Tool Gateway `role_tool_bindings`）；按租户自定义系统提示词、模型档位【V1 未实现】，提示词写死在 `app/graph/prompts.py`。
- 助手身份和回复文案是通用的（"员工服务助手"、"已转给相关同事"），不提 IT；按租户自定义助手名称和开场白【V1 未实现】。

### 3.4 Tool Gateway（工具策略网关）
- 定位：Agent Runtime 与真实世界之间的**唯一策略执行点**。Agent Runtime 是多副本无状态 Worker，权限、幂等如果放在 Worker 里，每个副本各管一份就失效了，所以集中在这里。
- 接口（REST，取代早期的 MCP 方案——MCP 端点不经过权限校验，保留它等于留一条绕过策略的后门）：
  - `GET /v1/tools/list?tenant_id&agent_role`：返回该角色可用工具的 OpenAI function 元数据，**不含密钥、下游地址、handler**。V1 的固定状态图按名字直接调用工具，没有调用方；留给以后让模型自己选工具时用。
  - `POST /v1/tools/call`：入参 `tenant_id, session_id, user_id, agent_role, tool_name, arguments`，副作用工具必须带 `Idempotency-Key` 头。返回 `success / error` 信封。
- 错误分两类：**模型可处理的**（未知工具、无权限、参数不合法、执行失败）走 200 信封回给模型；**调用方违约的**（非法 tenant、缺幂等键、幂等键冲突、并发执行中）走 HTTP 4xx，只给 Agent Runtime 看。
- 调用管线：租户校验 → 工具存在 → 角色绑定 → JSON Schema → 只读工具直接执行；副作用工具走幂等重放 / 原子占位 → 执行 → 记录结果 → 审计。
- 上下文注入：`session_id`（即会话 ID）和幂等键由网关注入给 handler，不出现在给模型看的参数 schema 里，模型没法伪造或写错。
- 幂等分两层：网关 `tool_calls` 表覆盖普通重试；执行完成但结果未落盘就崩溃的窗口，靠卡住的 `running` 记录超时后可被重新占位、再执行，因此下游也必须幂等（`create_ticket` 把同一个键透传给 ticket-service 的内部建单接口）。只读工具不写 `tool_calls`。
- 下游：`search_knowledge` 调 knowledge-service（3.6 节）；`create_ticket` 调 ticket-service（3.7 节），`TicketProvider` 的内部实现和未来的 Jira 实现都是"调用工单系统的 HTTP 接口"，形态一致。
- 运行时上下文：`/v1/tools/call` 除 `session_id` 外还要求 `user_id`（当前员工），由 Agent Runtime 提供，模型看不到；`create_ticket` 用它作为提单人 `requester_id`，审计日志也记录它，并参与幂等请求指纹。
- 脱敏：返回体只屏蔽 password/token 等凭据字段（知识库内容里的值班电话这类联系方式是员工需要的）；审计记录额外对手机号/邮箱/身份证做正则脱敏。
- 核心工具（v1）：`search_knowledge(query)`（结果含每条的 `category`）、`create_ticket(summary, category)`。`category` 是 1–50 字的任意字符串，不是固定枚举：各租户的类别由其知识库决定。
- Agent 角色：V1 只有一个 `service_agent`（迁移 0003 由 `it_support_agent` 改名；`tool_calls`、`tool_call_logs` 里的历史记录保留当时的角色名）。
- **V1 暂不做、已有设计可恢复**（实现见提交 `f36530d`）：
  - HITL 审批：`pending_approval` 状态、审批接口、`tool_approvals` 表、Redis Stream 恢复事件。等出现重置密码/解锁账号/开通权限这类高风险工具时再加。
  - Redis 令牌桶限流：V1 工具都是 PG 查询、量小；按用户限流 V1 在应用服务的发消息接口（3.2 节），部署 API Gateway 后可移过去；Agent 死循环靠 LangGraph `recursion_limit`。
- 扩展设计：`TicketProvider` 接口预留多实现（`InternalTicketProvider` 默认启用，未来可加 `JiraTicketProvider`、`FeishuTicketProvider`），通过租户级配置选择实现，不改动 Agent/应用服务代码。

### 3.5 AI Gateway
- 职责：模型调用的统一出口——服务令牌鉴权、每日 token 预算、用量记录。
  - Prompt 模板/模型版本集中管理、多租户模型配置（不同租户用不同系统提示词或模型档位）【V1 未实现】：系统提示词由 Agent Runtime 随请求传入，模型只有一个，由环境变量配置。
- 接口：`POST /v1/generate {tenant_id, system_prompt, messages}`，返回 `text/event-stream`：若干 `{"delta"}`，最后 `{"done": true, "usage"}`；中途失败时流在 `done` 之前结束，调用方据此判断不完整。租户校验和预算检查都在流开始前完成，失败返回普通的 4xx。
- **模型接入方式（已确认）**：`ModelProvider` 抽象下实现一个通用的 `OpenAICompatibleProvider`，只需配置 `base_url` + `api_key` + `model` 即可接入任何遵循 OpenAI Chat Completions 接口规范（`/v1/chat/completions`）的供应商，不用为每个供应商单独写适配代码。
  - v1 首个接入的具体供应商是 **DeepSeek**（`base_url=https://api.deepseek.com`，走同一套 OpenAI-compatible 实现），模型名、Key 来自环境变量（`MODEL_BASE_URL`、`MODEL_NAME`、`MODEL_API_KEY`）。
  - 后续如需接入 Anthropic Claude 等非 OpenAI-compatible 供应商，在 `ModelProvider` 抽象下新增一个专用实现（如 `AnthropicProvider`）即可；按租户/模型名分发的路由层【V1 未实现】，V1 只有一个 provider 实例。
- 记录每次调用的 `tenant_id + 模型 + token 数 + 耗时`，为后续按租户计费做准备。客户端中途断开时不记录（写入没有屏蔽取消，有意接受的缺口），这部分用量也就不计入预算。
- 每日 token 预算：流式输出开始前汇总租户当天（`Asia/Shanghai` 自然日）已用 token，达到预算返回 429 `TOKEN_BUDGET_EXCEEDED`（`Retry-After` 到次日零点）。默认值来自配置（1,000,000），`tenant_token_budgets` 表可按租户覆盖（V1 用 SQL 维护）。先检查后计费，并发调用可能共同小幅超出——软上限。
- 服务间令牌鉴权：`/v1/generate` 要求 Bearer 服务令牌（`AI_GATEWAY_SERVICE_TOKEN`），只有 Agent Runtime 能调用；`tenant_id` 在请求体里，没有令牌任何 pod 都能以任意租户消耗模型额度。

### 3.6 知识库服务（knowledge-service）
- 定位：知识检索的唯一出口，V1 **只读、只有检索一个接口**，不做 RAG/向量。
- 接口：`GET /internal/v1/knowledge/search?tenant_id&q&limit`（服务令牌鉴权）→ `{results: [{id, category, question, answer, score}]}`。
- 数据：`app/data/knowledge.json`，按租户 ID 分组、随镜像发布，改内容要重新部署。未知租户返回空列表（租户注册表归应用服务）。每个条目的 `category` 由租户自己定，也就是它的工单类别；演示数据里 `demo` 是 IT（`vpn/account/email/other`），`northwind` 是 HR/行政（`leave/expense/access/facility/payroll`）。
- 匹配：查询的字符二元组在条目问题中的覆盖率，与在答案中的覆盖率乘以权重取较大者；问题中包含完整查询词时记 1.0；低于 0.3 的丢弃（没有命中就走建单）。不需要中文分词，结果确定、可测。
- 扩展点：`KnowledgeRepository` 接口，V1 实现是 `StaticKnowledgeRepository`；换成数据库或向量检索时接口不变，Tool Gateway 无感知。
- 无状态、无数据库，扩容就是加副本。

### 3.7 工单服务（ticket-service）
- 定位：工单数据与生命周期的唯一所有者；Tool Gateway（Agent 建单）和应用服务（员工/处理人页面）都通过它的内部接口操作工单。`category` 是自由文本（最长 50 字），不校验取值。
- 状态机（4 个状态，"重新打开"是动作不是状态）：
  - `open --start(指定处理人)--> in_progress --resolve(必须填写说明)--> resolved --close--> closed`
  - `open --cancel--> closed`、`resolved --reopen--> open`
  - `cancel`/`close`/`reopen` 只能由提单人本人执行（服务自己校验 `actor_id == requester_id`）；`start`/`resolve` 的处理人权限由应用服务判断。
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
| `ai_gateway` | AI Gateway | `model_call_logs`、`tenant_token_budgets`（租户预算覆盖；只开 RLS 不 FORCE，owner 可直接维护） |
| `agent_runtime` | Agent Runtime | LangGraph checkpoint 表（`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`，由库自身管理，无 RLS） |
| （无） | 知识库服务 | V1 为代码仓库里按租户写死的 JSON，不需要数据库 |

- 每个服务有两个数据库账号：`<svc>_owner`（建表、迁移、种子数据）和 `<svc>_app`（运行时账号，不是表的所有者，RLS 对它强制生效），且只能连自己的库。本地由 `deploy/postgres/init-databases.sql` 创建，云上由基础设施创建、密码来自密钥管理。
- 每个服务自带 Alembic 迁移，部署时以独立任务先跑迁移再起服务（compose 用 `<svc>-migrate` 服务，K8s 用 Job/initContainer）。
- 不做跨库外键：非 app-service 的 `tenant_id` 是普通 UUID 列，租户合法性由 app-service 校验 JWT 保证（以后部署 API Gateway 时移过去），下游服务信任服务令牌调用方传来的 `tenant_id`。
- 多租户隔离：库内仍然用 `tenant_id` + PostgreSQL RLS，每次查询都在租户作用域内执行（`tenant_scope`：事务内 `set_config('app.tenant_id')`；切换租户前先 flush，否则未写入的修改会在新租户的作用域下写入、被 RLS 过滤掉）。注意 `FORCE ROW LEVEL SECURITY` 对表的所有者也生效：迁移里要跨租户回填数据时，需临时关闭 FORCE。
- 跨服务写数据走 API：Tool Gateway 的 `create_ticket` 调 ticket-service 的内部接口 `POST /internal/v1/tickets`（透传幂等键），不直接写工单表。
- 代价：跨服务报表（比如把工具调用和工单关联起来）不能再直接写 SQL join，需要时再做数据同步。

## 5. 部署与云原生方案

- 目标：**云无关，基于标准 Kubernetes**，不绑定具体云厂商托管服务，便于跨云/私有化部署。
- 组件容器化，每个逻辑组件（API Gateway 除外，用 Ingress Controller）独立部署：`app-service`（Deployment，处理 HTTP/SSE）、`agent-runtime`（Deployment，作为 **无状态 Worker** 消费队列，不建 Service）、`tool-gateway`、`ai-gateway`、`knowledge-service`、`ticket-service`。
- 弹性扩缩容（`deploy/k8s/components/autoscaling`，默认不启用）：五个 HTTP 服务（`app-service`、`tool-gateway`、`ai-gateway`、`knowledge-service`、`ticket-service`）用标准 **HPA**（CPU）；`agent-runtime` 用 **KEDA 的 redis-streams scaler**，按 `agent_tasks` 尚未投递的积压（`lagCount`，而不是 pending 数：每个副本最多同时处理 8 个，pending 会卡在 8 × 副本数）扩缩容 Worker 副本，负载突增（如上班早高峰集中反馈 VPN 问题）时可快速扩容，队列清空后缩回最小副本数（1）。
- 有状态依赖（PostgreSQL、Redis、向量库）优先使用云无关 Operator（如 CloudNativePG、Redis Operator）或直接对接任意云厂商的托管兼容服务，业务代码只依赖标准协议（Postgres wire protocol / Redis protocol），不绑定专有 SDK。
- 可观测性：全部基于 OpenTelemetry，业务代码只依赖 OTel API，后端可换（Jaeger、SigNoz、云厂商）只改 Collector。分三个阶段：
  - **阶段 1（已完成）追踪与日志**：
    - 各服务用 `opentelemetry-instrument uvicorn …` 启动（零代码插桩），由 `OTEL_*` 环境变量配置；镜像默认 `OTEL_SDK_DISABLED=true`，部署时打开并指向 Collector（compose 用 `x-otel-env` 锚点统一配置，协议 `http/protobuf`）。
    - 自动插桩：httpx（跨服务传递 W3C trace context）、psycopg（五个有库的服务都用驱动层插桩；SQLAlchemy 2.1 超出了 SQLAlchemy 插桩支持的版本范围，会被静默跳过，驱动层与 ORM 版本无关）、logging。
    - FastAPI 在代码里插桩（`app/core/telemetry.py: instrument_app`，镜像里关闭了自动的 FastAPI 插桩）：只有这里能去掉 ASGI send/receive 子 span（对 SSE 是每个事件一个 span），`/healthz` 不记。
    - 不自动插桩 Redis：阻塞轮询和 SSE 读会让每条命令各成一条根 trace；队列的 span 在阶段 2 手工埋。
    - 日志：每行一个 JSON 对象（`time/level/service/logger/message` + `extra` 字段），在 span 内时带 `trace_id`/`span_id`；同时经 OTLP 进 Loki（`service_name` 等为标签，`trace_id` 为结构化元数据），日志与 trace 可互查。
    - SQLAlchemy 连接池预检（`pool_pre_ping`）是名为 `;` 的 span。
  - **阶段 2（已完成）跨队列与 Agent/LLM**——员工发一条消息就是一条 trace：
    - 队列按 OTel messaging 约定埋点（`messaging.system=redis`，生产端 `send agent_tasks`、消费端 `process agent_tasks`），消息里带 `traceparent`，消费端 span 是生产端 span 的子 span；每次投递（含重投）都是一个消费端 span，带 `app.delivery_attempt`、`app.queue_wait_ms`（由 Stream id 的毫秒时间戳算出）。
    - 补偿重投：发消息时把请求的 `traceparent` 存进 `turns`（迁移 0006），重投的生产端 span 挂回原来的 trace 并标 `app.requeue`；每 30 秒的扫描本身在 `suppress_instrumentation()` 里执行，不产生 trace。
    - 结果落库：每条结果一个 `process agent_results` 根 span，用 link 指向发出它的 Agent 运行（落库是另一段工作，可能在别的副本、可能很久以后）；处理失败标记为错误。
    - LangGraph：每个节点一个 `node <名字>` span（包装在 `app/tracing.py: traced_node`），记录节点的决定（`app.intent`、`app.category`、`app.knowledge_hits`、`app.outcome`……）；`interrupt()` 记为事件、不算错误。
    - 事件：会话锁冲突、跳过旧副本（Redis 下限或 checkpoint）、续跑、检索失败与降级、预算拒绝、死信、RetryLater 都记在对应 span 上。
    - ai-gateway：每次模型调用一个 `chat <模型>` span（CLIENT），按 GenAI 约定记录 `gen_ai.provider.name`、`gen_ai.request/response.model`、`gen_ai.usage.input/output_tokens`、`gen_ai.response.finish_reasons`、首个 token 事件和 `app.time_to_first_token_ms`；SDK 发出的 HTTP 请求是它的子 span。提示词和回复由 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 控制（compose 开、默认关），记录时截断并对手机号/邮箱/身份证号脱敏。span 是手动开始和结束的：调用在异步生成器里跨 yield，用上下文管理器会在别处恢复上下文。
    - `scripts/show-trace.py` 把一条 trace 打印成缩进的调用树，方便不开 Grafana 时查看。
  - **阶段 3（待做）指标、看板、告警**：队列、任务、Agent、LLM、入口的指标（`tenant_id` 可做标签，`user_id`/`task_id` 不做），Grafana 看板和告警规则放进仓库，示例 SLO"95% 的轮次 30 秒内完成"。
  - 生产环境：Collector 用 OTel Operator 或独立 Deployment，后端各自部署（Tempo、Loki、Prometheus/Mimir、Grafana），在 Collector 做尾部采样（错误和慢请求全留）；k8s 的可选组件 `components/observability` 在阶段 3 加。
- 部署清单：V1 用 Kustomize（`deploy/k8s`：base + overlays + 可选的扩缩容组件），kubectl 自带、没有模板语法；"迁移完成后再更新服务"的顺序由 `apply-dev.sh` 保证，以后用 Helm hook 或 Argo CD sync wave 表达。服务容器一律非 root、只读根文件系统。
- CI/CD 流水线、多环境（staging/prod）overlay【V1 未实现】。租户级配置里已经能不重新部署就生效的：工具绑定（`role_tool_bindings`）和 token 预算（`tenant_token_budgets`），都是数据库表。

## 6. 安全与合规要点

- 认证：目标是 API Gateway 统一做，V1 由 app-service 做（3.1 节）；【假设，可调整】v1 用自建账号体系 + JWT，架构预留标准 **OIDC/SAML** 接入点，便于后续对接企业 SSO（Azure AD/Okta/自建 IdP）。
- Tool Gateway 对每次工具调用做审计日志（谁、哪个租户、调用了什么工具、参数摘要），满足事后追溯需求。
- AI Gateway 层面对发送给模型的内容做基本脱敏（如工号、内部 IP 等敏感信息按需过滤），尤其在使用外部模型供应商时【V1 未实现：员工原话会发给 DeepSeek】。
- 服务间调用：每个内部服务校验 Bearer 服务令牌（常量时间比较）；V1 一个服务一个令牌、由所有调用方共用，内部端口没有 NetworkPolicy 限制（见评审 P2-5）。
- 租户间数据隔离通过 RLS + 应用层双重校验，杜绝越权访问。

## 7. 分阶段路线图

| 阶段 | 范围 | 关键交付 |
|---|---|---|
| V1 | 多租户基础框架跑通；领域由各租户知识库决定（演示：IT 与 HR/行政）；知识库为按租户写死的静态 QA 数据；工单走自建模块 | 端到端可用的问答+转工单闭环 |
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
- 领域不写死：系统面向企业内部各类服务，不只 IT。不引入类别目录，由租户的知识库决定能自助解答的范围，工单类别取自命中条目；处理人角色为通用的 `handler`。以后做"按类别分派给处理组"时再引入目录，直接沿用条目里已有的 `category` 作为目录的 key。
- 知识库：独立的 knowledge-service，V1 只提供检索、数据按租户写死；后续在同一接口后面换成数据库/向量检索。
- 工单：独立的 ticket-service，4 状态状态机 + 乐观锁 + 处理记录；应用服务不再持有工单表。
- 数据：同一 Postgres 实例、每服务独立 database，库内 `tenant_id` + RLS 隔离；服务间不共享表和模型代码，没有 `libs/shared`（见第 4 节）。
- Tool Gateway：REST 策略网关；HITL 审批与工具调用限流 V1 暂不做（见 3.4 节）。
- 队列服务：Redis Streams（与事件流、会话锁共用一个 Redis 实例）。对比过 RabbitMQ/RocketMQ：规模用不上、事件回传仍需 Redis，V1 不引入；Redis 可能丢消息的问题由应用服务记录任务状态并重新入队弥补（阶段 5）。
- AI Gateway：服务令牌鉴权，只有 Agent Runtime 可调用。
- 成本防护：应用服务按员工限制发消息频率，AI Gateway 按租户设每日 token 预算（软上限，可按租户覆盖）；预算用完固定回复、不建单（见 3.2、3.3、3.5 节）。
- 部署：V1 用 Kustomize 清单，dev overlay 自带单实例 Postgres/Redis；生产换 Operator 或托管服务（见第 5 节）。
- **AI Gateway 模型接入：v1 通过 OpenAI-compatible 协议适配器接入，首个供应商为 DeepSeek；`ModelProvider` 抽象支持后续接入非 OpenAI-compatible 供应商。**
