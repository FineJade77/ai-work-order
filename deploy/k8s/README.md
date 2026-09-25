# Kubernetes 部署

Kustomize 清单，`kubectl` 自带 kustomize，不需要额外工具。

```
base/                      与环境无关：命名空间、ConfigMap（服务地址）、各服务 Deployment/Service、迁移 Job
overlays/dev/              开发集群用：单实例 Postgres/Redis、db-init Job、seed Job、开发凭据、本地镜像名
components/autoscaling/    可选：HTTP 服务的 HPA，agent-runtime 的 KEDA redis-streams ScaledObject
components/observability/  可选：打开六个服务的 OpenTelemetry，部署 grafana/otel-lgtm（看板和告警规则与 compose 同源）
apply-dev.sh               按依赖顺序部署 dev overlay
```

> 这些清单只做过静态校验：本地渲染，检查 Secret/ConfigMap 引用、Service 的 selector 和端口、环境变量与 compose 一致。还没有在真实集群上跑过。镜像在 Docker 里以相同的约束（非 root、只读根文件系统）验证过。

## 部署 dev overlay

前置条件：kubectl 1.27+；集群有默认 StorageClass（Postgres 和 Redis 的 PVC 用）；集群节点能拿到服务镜像。

```bash
# 1. 构建镜像（在仓库根目录）
docker compose build

# 2. 让集群拿到镜像。kind 集群：
for s in app-service agent-runtime tool-gateway ai-gateway knowledge-service ticket-service; do
  kind load docker-image ai-work-order-$s:latest
done
#    其他集群：推到集群能拉取的镜像仓库，并修改 overlays/dev/kustomization.yaml 里的 images.newName

# 3. 模型 Key（deepseek.env 已被 git 忽略）
cp deploy/k8s/overlays/dev/deepseek.env.example deploy/k8s/overlays/dev/deepseek.env   # 填入 MODEL_API_KEY

# 4. 部署到当前 kubectl 上下文，先确认上下文是对的
kubectl config current-context
deploy/k8s/apply-dev.sh

# 5. 访问
kubectl -n ai-work-order port-forward svc/app-service 8001:8001
scripts/demo.sh vpn
```

`apply-dev.sh` 的顺序：配置 → Postgres/Redis → db-init → 各服务迁移 → seed → 服务。直接 `kubectl apply -k` 会同时创建所有资源，服务会在库还不存在时启动。Job 不可变，脚本每次会先删除旧的 Job 再重新创建。

只看渲染结果：

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/dev
```

需要 `--load-restrictor LoadRestrictionsNone`，是因为 db-init 用的 SQL 和 compose 共用 `deploy/postgres/init-databases.sql`，它在 overlay 目录之外。

## 所有服务 Pod 的约束

- 以 uid 10001 运行（`runAsNonRoot`），只读根文件系统，丢弃全部 capabilities，`/tmp` 挂 emptyDir。
- 探针：HTTP 服务的 readiness 和 liveness 都用 `/healthz`。agent-runtime 分开：`/livez`（停机过程中仍返回 200，消费循环自己异常退出时返回 503）、`/readyz`（停机中返回 503），另有 startupProbe。
- agent-runtime 的停机与中断：收到 SIGTERM 后最多等 70 秒让手上的任务跑完，所以 `terminationGracePeriodSeconds: 90`；滚动发布时先起新 Pod（`maxSurge: 1, maxUnavailable: 0`）；PodDisruptionBudget 为 `maxUnavailable: 1`，节点排空时一次只驱逐一个。
- 资源：app-service 内存上限 1Gi（登录时的 scrypt 每次约 128 MiB，最多同时 4 个），agent-runtime 512Mi，其余 256Mi；只设 CPU 请求，不设 CPU 上限。
- 不挂载 ServiceAccount token（服务不访问 Kubernetes API）。

## 上生产前还要做的

- **Secret**：base 只引用 Secret 名称（清单见 `base/config.yaml` 的注释）。生产环境从密钥管理系统注入（例如 External Secrets），不要沿用 dev overlay 里的凭据。
- **Postgres / Redis**：换成 Operator（CloudNativePG、Redis Operator）或托管服务，修改连接串和 `REDIS_URL`；建库建账号交给基础设施，不用 db-init Job。Redis 至少开启 AOF，否则主从切换可能丢任务（app-service 的补偿机制会在 10 分钟后重新投递）。
- **部署顺序**：用 Helm hook 或 Argo CD sync wave 表达"迁移完成后再更新服务"，替代 apply-dev.sh。
- **扩缩容**：在 overlay 里启用 `components/autoscaling`，需要 metrics-server，agent-runtime 还需要 KEDA。
  - agent-runtime 按队列积压（`lagCount`）扩缩容，而不是 CPU，因为它的任务大部分时间在等模型。缩容有 300 秒稳定窗口、每分钟最多减 2 个：被缩掉的 Pod 要先等手上的任务跑完。
  - 数值只是起点，按架构文档第 5 节调优。
- **入口**：清单只有 ClusterIP Service。对外只应暴露 app-service，经 API Gateway（架构文档 3.1 节）或 Ingress；其余服务都是内部接口。
- **可观测性**：`components/observability` 只适合开发集群（一体化容器、数据在 Pod 里、不记录提示词内容）。生产环境用 OTel Operator 或独立的 Collector，Tempo/Loki/Prometheus（或 Mimir）/Grafana 各自持久化部署，把 `otel-env` 里的 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Collector，在 Collector 做尾部采样，告警接值班系统。访问开发集群里的 Grafana：`kubectl -n ai-work-order port-forward svc/observability 3000:3000`。
- **NetworkPolicy**：建议只允许既定的调用方向：app-service → ticket-service、tool-gateway（审批）；agent-runtime → tool-gateway、ai-gateway；tool-gateway → knowledge-service、ticket-service。
