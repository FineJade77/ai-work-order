# 智能工单系统（ai-work-order）

员工用自然语言描述 IT 问题，系统先检索企业知识库、由大模型给出解决步骤；员工反馈没解决时自动转成 IT 工单，IT 人员在工单里处理。V1 覆盖 VPN、账号密码、邮箱三个场景，多租户：每个租户有自己的知识库和数据，数据库里用 PostgreSQL 行级安全（RLS）隔离。

设计依据见 [架构设计文档](docs/architecture-design.md) 和 [实现计划](docs/implementation-plan.md)。

## 架构

```
浏览器 ──HTTP/SSE──▶ app-service ──XADD agent_tasks──▶ Redis Streams ──▶ agent-runtime（LangGraph Worker）
   ▲                   │  ▲                                                    │
   │                   │  └── agent_results / 每会话事件 Stream ◀──────────────┤
   │                   │                                                       ├─▶ tool-gateway ─┬─▶ knowledge-service
   └── 工单页面 ────────┴──────────────────────▶ ticket-service ◀──────────────┤                 └─▶ ticket-service
                                                                               └─▶ ai-gateway ──▶ DeepSeek
```

| 服务 | 端口 | 职责 | 数据库 |
|---|---|---|---|
| app-service | 8001 | 登录（JWT）、会话与消息、SSE 推送、回复落库、员工/IT 工单页面的 BFF | `app_service` |
| agent-runtime | 无对外端口 | 消费任务队列，跑 LangGraph 状态图：分类 → 检索 → 回答 → 等反馈 → 建单 | `agent_runtime`（checkpoint） |
| tool-gateway | 8003 | 工具策略网关：角色绑定、参数校验、幂等、审计、脱敏 | `tool_gateway` |
| ai-gateway | 8004 | 模型调用唯一出口（OpenAI 兼容协议，当前接 DeepSeek），记录用量 | `ai_gateway` |
| knowledge-service | 8005 | 知识检索，V1 为按租户写死的数据 | 无 |
| ticket-service | 8006 | 工单生命周期：4 状态状态机、乐观锁、处理记录 | `ticket_service` |

一个 Postgres 实例、每个服务一个库，服务之间不共享表和代码；Redis 同时承担任务队列、事件回传和会话锁。

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
| `demo` | `it.staff@example.com` | IT 人员 |
| `northwind` | `demo.user@northwind.example` | 员工 |
| `northwind` | `it.staff@northwind.example` | IT 人员 |

两个租户的知识库对同一个问题给出不同答案（比如 VPN 客户端不同），可以用来观察租户隔离。

## 跑通三个场景

`scripts/demo.sh` 以员工身份登录、新建会话、逐条发消息，并把 agent 推送的事件打印出来：

```bash
scripts/demo.sh vpn       # 回答 → 员工说"还是连不上" → 自动建单
scripts/demo.sh account   # 回答 → 员工说"好了，谢谢" → 已解决，不建单
scripts/demo.sh email     # 回答 → 员工说"还是收不到" → 自动建单
scripts/demo.sh printer   # 不属于三个场景 → 直接建单
```

VPN 场景的输出大致如下（回答由模型生成，措辞每次不同；极少数情况下分类结果也会不同）：

```
员工> VPN 连不上
  [检索知识库]
请按以下步骤操作：1. 先确认本机网络正常。2. 检查 VPN 客户端版本是否为最新版。3. 重启 VPN 客户端后重试。……请问你的问题是否已经解决？
  [本轮结束: awaiting_feedback]

员工> 按你说的试了，还是连不上
助手> 已为你创建 IT 工单（编号 dc57b7cf-…），IT 同事会尽快处理，处理进展可以在「我的工单」中查看。
  [本轮结束: ticket_created]

我的工单：
  dc57b7cf-…  open  vpn  员工反馈 VPN 无法连接。已尝试知识库方案：……均无效。
```

换租户或账号：`TENANT=northwind EMAIL=demo.user@northwind.example scripts/demo.sh vpn`。

### 用 curl 一步步走，包括 IT 处理

```bash
BASE=http://localhost:8001/v1
login() {
  curl -s $BASE/auth/login -H 'content-type: application/json' \
    -d "{\"tenant\":\"demo\",\"email\":\"$1\",\"password\":\"demo-password\"}" | jq -r .access_token
}
EMP="Authorization: Bearer $(login demo.user@example.com)"
IT="Authorization: Bearer $(login it.staff@example.com)"

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

# 5. IT 接单、解决；员工确认关闭。每次流转都要带上当前版本号（乐观锁）
version() { curl -s $BASE/tickets/$TICKET -H "$EMP" | jq .ticket.version; }
act() { curl -s $BASE/tickets/$TICKET/transitions -H "$1" -H 'content-type: application/json' -d "$2" | jq '{status, assignee_id, version}'; }
act "$IT"  "{\"action\":\"start\",\"expected_version\":$(version)}"
act "$IT"  "{\"action\":\"resolve\",\"expected_version\":$(version),\"note\":\"已重置 VPN 证书\"}"
act "$EMP" "{\"action\":\"close\",\"expected_version\":$(version)}"

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
| `GET /v1/tickets`、`GET /v1/tickets/{id}` | 员工只看得到自己的工单，IT 看全租户 |
| `POST /v1/tickets/{id}/transitions` | `{action, expected_version, note?}`；`start`/`resolve` 仅 IT，`start` 指派给自己 |

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

## 运行测试

```bash
scripts/test-all.sh                 # 所有服务：单元测试 + 集成测试
scripts/test-all.sh app-service     # 只跑指定服务
```

每个服务在自己的 `.venv` 里跑。集成测试需要 `docker compose up -d --build` 起来的环境，用独立的 Redis 库和一次性租户，测完自动清理；环境没起来时集成测试会被跳过，不算失败。集成测试不调用真实模型。

## 部署到 Kubernetes

见 [deploy/k8s/README.md](deploy/k8s/README.md)：Kustomize 清单（base + dev overlay），可选的 HPA / KEDA 扩缩容组件，以及按依赖顺序部署的脚本。

## 排障

- **端口被占用**：compose 用到 8001、8003–8006、25432（Postgres）、26379（Redis）。
- **发消息后迟迟没有回复，或收到 `error` 事件**：多半是模型调用失败。检查 `.env` 里的 `DEEPSEEK_API_KEY`，看 `docker compose logs ai-gateway agent-runtime`。Worker 会自动重试，每次间隔约 90 秒，3 次都失败后任务进入死信：
  `docker compose exec redis redis-cli XRANGE agent_tasks:dead - +`
- **收到"今天 AI 助手的使用额度已经用完"**：租户当天的 token 预算用完了，agent-runtime 日志里有 `budget_exceeded tenant_id=…`。按"调整限额"一节提高预算，不用重启。
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
