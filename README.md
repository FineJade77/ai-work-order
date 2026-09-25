# 智能工单系统（ai-work-order）

员工用自然语言描述问题，系统先检索企业知识库、由大模型给出解决步骤；员工反馈没解决时自动转成工单，由处理人在工单里处理。不限于 IT：每个租户的知识库决定它能自助解答什么（IT、人事、行政……），代码里不写死领域和类别，工单类别取自命中的知识条目。多租户：每个租户有自己的知识库和数据，数据库里用 PostgreSQL 行级安全（RLS）隔离。

设计依据见 [架构设计文档](docs/architecture-design.md) 和 [实现计划](docs/implementation-plan.md)。

## 架构

```
浏览器 ──HTTP/SSE──▶ app-service
                       ├─ XADD agent_tasks ──────────────▶ Redis Streams ──▶ agent-runtime（LangGraph Worker）
                       ├─ 读 agent_results、每会话事件 Stream ◀─ Redis ◀──────── agent-runtime 写入
                       └─ 工单页面（查询 / 流转）─────────▶ ticket-service

agent-runtime ─┬─▶ tool-gateway ─┬─▶ knowledge-service
               │                 └─▶ ticket-service（create_ticket）
               └─▶ ai-gateway ──▶ DeepSeek
```

| 服务 | 端口 | 职责 | 数据库 |
|---|---|---|---|
| app-service | 8001 | 登录（JWT）、会话与消息、发消息限流、SSE 推送、回复落库、员工/处理人工单页面的 BFF | `app_service` |
| agent-runtime | 无对外端口 | 消费任务队列，跑 LangGraph 状态图：分类 → 检索 → 回答 → 等反馈 → 建单 | `agent_runtime`（checkpoint） |
| tool-gateway | 8003 | 工具策略网关：角色绑定、参数校验、幂等、审计、脱敏 | `tool_gateway` |
| ai-gateway | 8004 | 模型调用唯一出口（OpenAI 兼容协议，当前接 DeepSeek），记录用量、每日 token 预算 | `ai_gateway` |
| knowledge-service | 8005 | 知识检索，V1 为按租户写死的数据 | 无 |
| ticket-service | 8006 | 工单生命周期：4 状态状态机、乐观锁、处理记录 | `ticket_service` |

一个 Postgres 实例、每个服务一个库，服务之间不共享表和代码；Redis 同时承担任务队列、结果与事件回传、会话锁、会话下限（跳过旧轮次的副本）和发消息限流计数。

## 快速开始

**前置条件**

- Docker 24+（含 Docker Compose v2）
- DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com/)）
- `curl` 和 `jq`：演示脚本用
- Python 3.11 和 [uv](https://docs.astral.sh/uv/)：只有跑测试时需要

**启动**

```bash
cp .env.example .env        # 填入 DEEPSEEK_API_KEY
docker compose up -d --build
```

首次构建需要几分钟。一条命令会依次完成：建库建账号 → 各服务迁移 → 写入演示数据 → 启动服务。完成后检查：

```bash
docker compose ps -a
```

`db-init`、`*-migrate`、`*-seed` 应为 `Exited (0)`，其余服务为 `Up`。有 `Exited (1)` 的话看 `docker compose logs <服务名>`。

**演示账号**（密码都是 `demo-password`）

| 租户（登录时的 tenant） | 邮箱 | 角色 |
|---|---|---|
| `demo` | `demo.user@example.com` | 员工 |
| `demo` | `it.staff@example.com` | 处理人（`handler`） |
| `northwind` | `demo.user@northwind.example` | 员工 |
| `northwind` | `hr.staff@northwind.example` | 处理人（`handler`） |

两个租户的知识库是不同领域：`demo` 是 IT 服务台（VPN、账号、邮箱），`northwind` 是 HR/行政（请假、报销、门禁、会议室、工资条）。一个租户的问题在另一个租户查不到，工单类别也各用各的（`vpn`、`leave`……）。

## 跑通演示场景

`scripts/demo.sh` 以员工身份登录、新建会话、逐条发消息，并把 agent 推送的事件打印出来：

```bash
scripts/demo.sh vpn       # 回答 → 员工说"还是连不上" → 自动建单
scripts/demo.sh account   # 回答 → 员工说"好了，谢谢" → 已解决，不建单
scripts/demo.sh email     # 回答 → 员工说"还是收不到" → 自动建单
scripts/demo.sh printer   # 知识库里没有 → 直接建单（描述带 [知识库未命中]，类别 other）
scripts/demo.sh leave     # northwind 租户（HR/行政）：回答 → "还是没通过" → 建单，类别 leave
```

VPN 场景的输出大致如下（回答由模型生成，措辞每次不同；极少数情况下分类结果也会不同）：

```
员工> VPN 连不上
  [检索知识库]
请按以下步骤操作：1. 先确认本机网络正常。2. 检查 VPN 客户端版本是否为最新版。3. 重启 VPN 客户端后重试。……请问你的问题是否已经解决？
  [本轮结束: awaiting_feedback]

员工> 按你说的试了，还是连不上
助手> 已为你创建工单（编号 dc57b7cf-…），相关同事会尽快处理，处理进展可以在「我的工单」中查看。
  [本轮结束: ticket_created]

我的工单：
  dc57b7cf-…  open  vpn  员工反馈 VPN 无法连接。已尝试知识库方案：……均无效。
```

`leave` 场景会自动切到 northwind 租户；其他场景换租户或账号用环境变量，例如 `TENANT=northwind EMAIL=demo.user@northwind.example scripts/demo.sh printer`。

### 用 curl 一步步走，包括处理人处理

```bash
BASE=http://localhost:8001/v1
login() {
  curl -s $BASE/auth/login -H 'content-type: application/json' \
    -d "{\"tenant\":\"demo\",\"email\":\"$1\",\"password\":\"demo-password\"}" | jq -r .access_token
}
EMP="Authorization: Bearer $(login demo.user@example.com)"
HANDLER="Authorization: Bearer $(login it.staff@example.com)"

# 1. 新建会话，提问
CONV=$(curl -s -X POST $BASE/conversations -H "$EMP" | jq -r .id)
TASK=$(curl -s $BASE/conversations/$CONV/messages -H "$EMP" -H 'content-type: application/json' \
  -d '{"content":"VPN 连不上"}' | jq -r .task_id)

# 2. 接收执行事件（SSE），收到 done 后连接自动关闭
curl -sN "$BASE/conversations/$CONV/stream?task_id=$TASK" -H "$EMP"
```

SSE 输出（节选）：

```
id: 1727170000000-0
event: retrieving
data: {}

id: 1727170000001-0
event: answer_chunk
data: {"delta": "请按以下步骤"}
...
event: done
data: {"outcome": "awaiting_feedback"}
```

```bash
# 3. 反馈没解决 → 收到 ticket_created
TASK=$(curl -s $BASE/conversations/$CONV/messages -H "$EMP" -H 'content-type: application/json' \
  -d '{"content":"还是不行"}' | jq -r .task_id)
curl -sN "$BASE/conversations/$CONV/stream?task_id=$TASK" -H "$EMP"

# 4. 会话历史（助手回复由后台落库）和我的工单
curl -s $BASE/conversations/$CONV/messages -H "$EMP" | jq '.messages[] | {role, content}'
TICKET=$(curl -s $BASE/tickets -H "$EMP" | jq -r '.items[0].id')

# 5. 处理人接单、解决；员工确认关闭。每次流转都要带上当前版本号（乐观锁）
version() { curl -s $BASE/tickets/$TICKET -H "$EMP" | jq .ticket.version; }
act() { curl -s $BASE/tickets/$TICKET/transitions -H "$1" -H 'content-type: application/json' -d "$2" | jq '{status, assignee_id, version}'; }
act "$HANDLER" "{\"action\":\"start\",\"expected_version\":$(version)}"
act "$HANDLER" "{\"action\":\"resolve\",\"expected_version\":$(version),\"note\":\"已重置 VPN 证书\"}"
act "$EMP"     "{\"action\":\"close\",\"expected_version\":$(version)}"

# 6. 处理记录
curl -s $BASE/tickets/$TICKET -H "$EMP" | jq '.events[] | {action, to_status, note}'
```

### 接口一览

| 接口 | 说明 |
|---|---|
| `POST /v1/auth/login` | `{tenant, email, password}` → `access_token`（8 小时） |
| `GET /v1/me` | 当前用户 |
| `POST /v1/conversations`、`GET /v1/conversations` | 新建会话、我的会话 |
| `GET /v1/conversations/{id}/messages` | 历史消息；`active_task_id` 是还在处理的那一轮 |
| `POST /v1/conversations/{id}/messages` | `{content}` → 202 `{task_id}`；上一轮没结束时 409 `TURN_IN_PROGRESS` |
| `GET /v1/conversations/{id}/stream?task_id=` | SSE；支持 `Last-Event-ID` 断线续读 |
| `GET /v1/tickets`、`GET /v1/tickets/{id}` | 员工只看得到自己的工单，处理人看全租户 |
| `POST /v1/tickets/{id}/transitions` | `{action, expected_version, note?}`；`start`/`resolve` 仅处理人（否则 403 `NOT_HANDLER`），`start` 指派给自己 |

SSE 事件：`retrieving`（检索中）、`answer_chunk`（增量回答）、`answer`（完整回答）、`reply`（固定回复，如问候或确认已解决）、`ticket_created`、`resumed`（Worker 崩溃后续跑，丢弃已显示的半截回答）、`error`、`done`（`outcome`：`awaiting_feedback` / `resolved` / `replied` / `ticket_created`）。

## 查看审计日志与用量

以下命令用 Postgres 超级用户连接，会绕过 RLS，只用于运维排查。

```bash
# 工具调用审计（参数已脱敏：手机号、邮箱、身份证号、凭据字段）
docker compose exec postgres psql -U work_order -d tool_gateway -c \
  "SELECT created_at, tool_name, status, error_code, left(params_summary, 60) FROM tool_call_logs ORDER BY created_at DESC LIMIT 10"

# 模型调用用量（按租户统计）
docker compose exec postgres psql -U work_order -d ai_gateway -c \
  "SELECT tenant_id, model, count(*), sum(prompt_tokens) AS prompt, sum(completion_tokens) AS completion FROM model_call_logs GROUP BY 1, 2"

# 工单处理记录
docker compose exec postgres psql -U work_order -d ticket_service -c \
  "SELECT t.status, e.action, e.to_status, e.note, e.created_at FROM ticket_events e JOIN tickets t ON t.id = e.ticket_id ORDER BY e.created_at DESC LIMIT 10"

# 每一轮对话的处理结果
docker compose exec postgres psql -U work_order -d app_service -c \
  "SELECT created_at, status, outcome, ticket_id, enqueue_count FROM turns ORDER BY created_at DESC LIMIT 10"
```

### 调整限额

- 每个租户每天的 token 预算默认 1,000,000（prompt + completion，按 `Asia/Shanghai` 的自然日），用完后 ai-gateway 返回 429，员工收到"今日额度已用完"的固定回复。默认值用 ai-gateway 的 `DAILY_TOKEN_BUDGET`、`BUDGET_TIMEZONE` 环境变量改；单个租户在 `tenant_token_budgets` 表里设置，立即生效：

  ```bash
  docker compose exec postgres psql -U ai_gateway_owner -d ai_gateway -c \
    "INSERT INTO tenant_token_budgets (tenant_id, daily_tokens) VALUES ('<租户 ID>', 5000000)
     ON CONFLICT (tenant_id) DO UPDATE SET daily_tokens = EXCLUDED.daily_tokens, updated_at = now()"
  ```

- 每个员工每分钟最多发 6 条消息、每天 200 条，超出返回 429 `RATE_LIMITED`（带 `Retry-After`）。用 app-service 的 `MESSAGE_RATE_PER_MINUTE`、`MESSAGE_RATE_PER_DAY` 调整。

## 可观测性

`docker compose up` 会一起启动 `observability`（`grafana/otel-lgtm`：OTel Collector + Tempo + Prometheus + Loki + Grafana），六个服务把 trace、指标、日志经 OTLP 发给它。数据存在容器里，重建容器就清空。

- Grafana：http://localhost:23000（本地匿名访问），在 Explore 里选数据源：
  - **Tempo**：一轮对话是一条 trace，从发消息的请求开始，经 Redis 队列到 agent-runtime 的每个图节点、每次模型调用（token 数、首 token 延迟，开发环境还有提示词和回复）、工具调用和每条 SQL。常用 TraceQL：`{name="process agent_tasks"}`（所有轮次）、`{span."app.conversation_id"="<会话 id>"}`、`{name=~"chat .*"}`（模型调用）、`{status=error}`；
  - **Loki**：`{service_name="app-service"}` 看日志，每行是 JSON，带 `trace_id`，可以跳到对应的 trace；
  - **Prometheus**：各服务的 HTTP 指标（自动插桩提供）。
- 直接查询的端口：Tempo `23200`、Loki `23100`、Prometheus `29090`；OTLP 接收 `24317`（gRPC）/`24318`（HTTP），本地直接跑服务时可以把 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向 `http://localhost:24318`。
- 不开 Grafana 也能看：`scripts/show-trace.py` 把最近一轮对话打印成调用树（`scripts/show-trace.py <trace_id>` 看指定的一条）：

  ```
  trace b7c8202f920c5c43549ff8337d0bd6dd
         0ms app-service: POST /v1/conversations/{conversation_id}/messages (52ms) sql×6
        48ms   app-service: send agent_tasks (2ms)
        51ms     agent-runtime: process agent_tasks (1896ms) sql×14 delivery_attempt=1 queue_wait_ms=2
        96ms       agent-runtime: node check_resolution (842ms) resolution=unresolved
       107ms           ai-gateway: POST /v1/generate (830ms) sql×7
       148ms             ai-gateway: chat deepseek-chat (774ms) gen_ai.usage.input_tokens=161 ... !gen_ai.first_token
      1741ms       agent-runtime: node create_ticket (174ms) outcome=ticket_created ticket_id=…
      1831ms               ticket-service: POST /internal/v1/tickets (37ms) sql×5
  ```
- 结果落库是另一条 trace（`process agent_results`），用 link 指回 Agent 的那次运行。
- 服务的标准输出也是一行一个 JSON：`docker compose logs app-service`。
- **看板**（Grafana → Dashboards → ai-work-order）：
  - **系统总览**：轮次与 SLO（30 秒内完成的比例、p95）、任务队列（积压、待确认、最老待确认、死信、每次投递的结果）、Agent（节点耗时、意图、检索命中）、大模型（token、调用耗时、首 token、各租户预算使用率）、入口与工具（HTTP、5xx、限流、SSE、工具调用）。顶部可按租户过滤。
  - **单轮排查**：填会话 id 列出它的各轮 trace；填 trace id 看调用树和这一轮在所有服务里的日志。
- **告警**（Grafana → Alerting）：任务积压、任务长时间未确认、新死信、检索降级、预算超过 80%、SLO 快速燃烧、5xx 过高。本地没有接通知渠道，在 Alerting 页面看状态。想看它们触发，可以：
  ```bash
  docker compose exec redis redis-cli XADD agent_tasks '*' task_id not-a-uuid content broken   # 死信
  docker compose stop knowledge-service && scripts/demo.sh vpn; docker compose start knowledge-service   # 检索降级
  ```
  告警每分钟评估一次，一两分钟后变成 Alerting。
- 看板和告警是代码：改 `deploy/observability/grafana/generate_dashboards.py` / `generate_alerts.py`，再生成 JSON/YAML；告警改动要重建 `observability` 容器才生效（`docker compose up -d --force-recreate observability`，数据会清空，Grafana 首次初始化要等几分钟）。

## 运行测试

```bash
scripts/test-all.sh                 # 所有服务：单元测试 + 集成测试
scripts/test-all.sh app-service     # 只跑指定服务
```

每个服务在自己的 `.venv` 里跑。集成测试需要 `docker compose up -d --build` 起来的环境，用独立的 Redis 库和一次性租户，测完自动清理；环境没起来时集成测试会被跳过，不算失败。集成测试不调用真实模型。

## 部署到 Kubernetes

见 [deploy/k8s/README.md](deploy/k8s/README.md)：Kustomize 清单（base + dev overlay），可选的 HPA / KEDA 扩缩容组件，以及按依赖顺序部署的脚本。

## 排障

- **端口被占用**：compose 用到 8001、8003–8006、25432（Postgres）、26379（Redis），以及可观测性的 23000、23100、23200、24317、24318、29090。
- **Grafana 里没有数据**：确认 `observability` 容器是 `Up`；服务是否打开了遥测（`docker compose exec app-service env | grep OTEL_SDK_DISABLED` 应为 `false`）。
- **发消息后迟迟没有回复，或收到 `error` 事件**：多半是模型调用失败。检查 `.env` 里的 `DEEPSEEK_API_KEY`，看 `docker compose logs ai-gateway agent-runtime`。Worker 会自动重试，每次间隔约 90 秒，3 次都失败后任务进入死信：
  `docker compose exec redis redis-cli XRANGE agent_tasks:dead - +`
- **收到"今天 AI 助手的使用额度已经用完"**：租户当天的 token 预算用完了，agent-runtime 日志里有 `budget_exceeded tenant_id=…`。按"调整限额"一节提高预算，不用重启。
- **升级后原来的令牌返回 401**：处理人角色由 `it_staff` 改名为 `handler`，改名前签发的令牌带旧角色，重新登录即可。
- **429 `RATE_LIMITED`**：这个员工发消息太频繁，按 `Retry-After` 等待。
- **409 `TURN_IN_PROGRESS`**：同一会话上一条消息还没处理完。等它的 `done`；如果任务丢了，app-service 最多 10 分钟后会自动重新投递。
- **改了 `.env` 不生效**：`docker compose up -d` 会用新配置重建相关容器。
- **重置所有本地数据**：`docker compose down -v`（会删除数据库和 Redis 的数据卷），再 `docker compose up -d --build`。

## 目录结构

```
services/<服务>/        每个服务自包含：app/、migrations/（Alembic）、scripts/seed.py、tests/、Dockerfile
deploy/postgres/        本地建库建账号脚本（compose 和 k8s dev overlay 共用）
deploy/k8s/             Kubernetes 清单
scripts/                demo.sh（场景演示）、test-all.sh（全部测试）
docs/                   架构设计文档、实现计划
```
