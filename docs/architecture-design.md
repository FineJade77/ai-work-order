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
│  · 会话管理 / 工单 CRUD 与状态机 / 用户与租户管理 / 权限校验             │
│  · 编排「问答 → 未解决转工单」的业务流程                                 │
│  · 对下游 Agent Runtime 发起任务，接收结果落库                          │
└───────┬───────────────────────────────────────────────┬──────────────┘
        │ 任务入队(Redis Streams)      结果订阅(Pub/Sub)   │
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
│  · 执行中事件发布回 Pub/Sub（供应用服务转 SSE） │
└───────┬────────────────┘
        │ REST: /v1/tools/list, /v1/tools/call（OpenAI function 元数据）
┌───────▼──────────────────────────────────────────────────────────────┐
│  Tool Gateway（工具策略网关）                                          │
│  · 角色-工具绑定 / JSON Schema 校验 / 幂等 / 审计 / 服务间令牌鉴权       │
│  · search_knowledge（知识检索工具，可插拔实现）                        │
│  · create_ticket（工单 Provider 接口，默认对接自建工单模块）             │
│  · 预留：外部系统工具适配位（Jira/ServiceNow/企业IM，未来可插拔）        │
└───────┬────────────────────────────────────────────────┬─────────────┘
        │                                                 │
┌───────▼─────────────┐                        ┌──────────▼─────────────┐
│  知识库子系统         │                        │   工单子系统（自建）      │
│  v1: 简单QA/FAQ存储   │                        │  · 工单表 + 状态机       │
│  v2: 向量化+RAG检索   │                        │  · IT 处理人工作台        │
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
  - `Tenant`：租户
  - `User`：用户（属于某租户）
  - `Conversation` / `Message`：问答会话
  - `Ticket`：工单（状态机：`draft → open → in_progress → resolved → closed`，附加 `reopened` 分支）
- 核心业务流程：
  1. 接收用户提问 → 创建/追加 `Conversation`。
  2. 调用 Agent Runtime 执行任务，传入 `tenant_id`、会话上下文。
  3. 若 Agent 返回「已通过知识库解决」→ 落库回答，结束。
  4. 若 Agent 返回「需要转工单」或用户主动要求 → 调用 Tool Gateway 的 `create_ticket` 生成工单，返回工单号给用户。
- 对 Agent Runtime 是**异步任务分发**：应用服务不直接同步调用 Agent Runtime，而是把任务（`conversation_id` + 上下文 + `tenant_id`）写入 Redis Streams 队列即返回；随后订阅该 `conversation_id` 对应的 Pub/Sub 频道，将 Agent Runtime 陆续发布的中间事件（"正在检索知识库…"、增量 token、最终结果）通过 SSE/WebSocket 转发给前端。对前端而言体验仍是"流式返回"，但应用服务与 Agent Runtime 之间是解耦的异步关系，见 3.3 节。

### 3.3 Agent Runtime（LangGraph）

**部署模型：异步 Worker + 队列，而非同步长连接服务。**

- 应用服务把任务写入 **Redis Streams**（`agent_tasks` 队列，按租户或全局分区均可），Agent Runtime 作为**无状态 Worker** 通过 Consumer Group 消费任务，一个任务对应一次 LangGraph 图的完整执行。
- 执行过程中，Worker 把中间事件（工具调用开始/结束、增量回答 token、最终结果）发布到以 `conversation_id` 命名的 **Pub/Sub 频道**，应用服务订阅后转发给前端，从而在异步执行的同时保留流式体验。
- 选择这个模型而非"应用服务同步调用、Agent Runtime 常驻等待"的原因：
  1. 一次 Agent 执行常包含多次工具调用 + 一次 LLM 生成，耗时数秒到十几秒；同步模型下每个进行中会话都要独占一个连接/线程直到图跑完，扩缩容单位变成"并发连接数"而非"实际计算负载"，HPA 难以精确伸缩。
  2. Worker 无状态、可随时重启，配合 LangGraph 的 checkpoint 机制，任务在 crash 后可从断点恢复，天然具备重试能力；同步模型下 Pod 重启会直接中断并丢失正在处理的会话。
  3. 队列深度是比 CPU/内存更贴合负载本质的扩缩容信号，可用 **KEDA 的 redis-streams scaler** 按 `agent_tasks` 队列长度精确扩缩容 Worker 副本数。
  4. 代价是多了队列 + Pub/Sub 一跳延迟（通常几十毫秒，相对 LLM 秒级延迟可忽略）和一套需要运维的组件；由于 Redis 已经是会话缓存/checkpoint 的依赖，复用同一 Redis 实例做队列，不新增基础设施种类。
- 用 LangGraph 构建一个**状态图**，核心节点：
  1. `classify_intent`：判断是纯问答、故障排查、还是明确要求开工单。
  2. `retrieve_knowledge`：调用 Tool Gateway 的 `search_knowledge`。
  3. `generate_answer`：调用 AI Gateway 生成回答，基于检索结果。
  4. `check_resolution`：判断回答是否解决问题（基于用户反馈或规则），未解决则进入下一节点。
  5. `create_ticket`：调用 Tool Gateway 创建工单，附带对话摘要、已尝试的知识库方案（避免 IT 人员重复劳动）。
- 状态持久化：使用 LangGraph 的 checkpoint 机制，存储到 Redis 或 Postgres，支持多轮对话中断恢复。
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
- 幂等分两层：网关 `tool_calls` 表覆盖普通重试；执行完成但结果未落盘就崩溃的窗口，靠卡住的 `running` 记录超时后可被重新占位、再执行，因此有副作用的 handler 自身也必须幂等（`create_ticket` 把同一个键传到工单表）。只读工具不写 `tool_calls`。
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
- 记录每次调用的 `tenant_id + 模型 + token 数 + 耗时`，为后续按租户计费/限额做准备。

## 4. 数据模型（核心表，简化版）

- `tenants(id, name, plan, created_at)`
- `users(id, tenant_id, name, email, role)`
- `conversations(id, tenant_id, user_id, status, created_at)`
- `messages(id, conversation_id, role, content, created_at)`
- `tickets(id, tenant_id, conversation_id, category, summary, status, assignee, created_at, updated_at)`
- `knowledge_items(id, tenant_id, question, answer, tags, source, updated_at)`（v1 简单 QA 表；v2 增加 `embedding` 字段或迁移到独立向量库）

多租户隔离方案【假设，可调整，推荐】：**共享库 + `tenant_id` 行级隔离**，配合 PostgreSQL Row-Level Security（RLS）策略强制每次查询带租户过滤，成本低、运维简单；预留向"独立 schema"迁移的路径供大客户合规诉求使用。

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
2. 多租户隔离级别：默认"共享库 + tenant_id 行级隔离"。
3. 身份认证：默认 v1 自建 JWT 账号体系，预留 OIDC 接入点。

以上假设如与实际需求不符，请指出对应条目，架构将做相应调整。

## 9. 已确认的关键决策（变更记录）

- 部署：云无关，基于标准 Kubernetes。
- Agent Runtime：LangGraph，异步 Worker + Redis Streams 队列 + Pub/Sub 流式回传（见 3.3 节）。
- 工单系统：自建工单模块，`TicketProvider` 抽象预留外部系统扩展点。
- 知识库：v1 简单 QA 表，v2 平滑升级为向量检索。
- 队列服务：Redis Streams（复用会话缓存的同一 Redis 实例）。
- **AI Gateway 模型接入：v1 通过 OpenAI-compatible 协议适配器接入，首个供应商为 DeepSeek；`ModelProvider` 抽象支持后续接入非 OpenAI-compatible 供应商。**
