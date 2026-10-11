"""Generates the Grafana dashboards. The JSON under dashboards/ is committed and is what Grafana
loads; edit this script, not the JSON, then regenerate:

    python3 deploy/observability/grafana/generate_dashboards.py deploy/observability/grafana/dashboards

Metric names are as Prometheus stores OTLP metrics: dots become underscores, the unit becomes a
suffix (app.turn.duration in s -> app_turn_duration_seconds), counters get _total.
"""

import json
import pathlib
import sys

OUT = pathlib.Path(sys.argv[1])
PROM = {"type": "prometheus", "uid": "prometheus"}
TEMPO = {"type": "tempo", "uid": "tempo"}
LOKI = {"type": "loki", "uid": "loki"}
T = '{app_tenant_id=~"$tenant"}'  # tenant filter for series that carry it
RI = "$__rate_interval"

_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def row(title: str, y: int) -> dict:
    return {"type": "row", "title": title, "id": _id(), "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


def ts(title: str, targets: list[tuple[str, str]], x: int, y: int, w: int = 8, h: int = 8, unit: str = "short",
       description: str = "", stack: bool = False) -> dict:
    return {
        "type": "timeseries", "title": title, "id": _id(), "description": description, "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 10,
                                                              "stacking": {"mode": "normal" if stack else "none"}}},
                        "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": [{"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
                    for i, (expr, legend) in enumerate(targets)],
    }


def stat(title: str, expr: str, x: int, y: int, w: int = 4, h: int = 4, unit: str = "short",
         thresholds: list[tuple[float | None, str]] | None = None, description: str = "") -> dict:
    steps = [{"color": c, "value": v} for v, c in (thresholds or [(None, "green")])]
    return {
        "type": "stat", "title": title, "id": _id(), "description": description, "datasource": PROM,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"datasource": PROM, "expr": expr, "refId": "A", "instant": True}],
    }


def q(p: float, metric: str, by: str = "", selector: str = T) -> str:
    group = f"le, {by}" if by else "le"
    return f"histogram_quantile({p}, sum by ({group}) (rate({metric}_bucket{selector}[{RI}])))"


def _queue_total(metric: str) -> str:
    """A queue gauge per stream. Series of the task queue carry a tenant; the other streams don't."""
    return (f"sum by (messaging_destination_name) "
            f"(max by (messaging_destination_name, app_tenant_id) ({metric}))")


tenant_var = {
    "name": "tenant", "label": "租户", "type": "query", "datasource": PROM,
    "query": {"query": "label_values(app_turn_duration_seconds_count, app_tenant_id)", "refId": "tenant"},
    "definition": "label_values(app_turn_duration_seconds_count, app_tenant_id)",
    "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}, "refresh": 2,
}

SLO_RATIO = ('sum(rate(app_turn_duration_seconds_bucket{le="30", app_tenant_id=~"$tenant"}[1h]))'
             ' / sum(rate(app_turn_duration_seconds_count{app_tenant_id=~"$tenant"}[1h]))')

panels = []
y = 0
panels.append(row("轮次与 SLO（从员工发消息到回复落库）", y)); y += 1
panels += [
    stat("SLO：30 秒内完成的比例（1h）", SLO_RATIO, 0, y, w=6, unit="percentunit",
         thresholds=[(None, "red"), (0.9, "orange"), (0.95, "green")],
         description="目标 95%。低于目标时，燃烧率告警（ai-work-order-slo-burn）会在错误预算消耗过快时触发。"),
    stat("轮次 p95 耗时", q(0.95, "app_turn_duration_seconds"), 6, y, w=6, unit="s",
         thresholds=[(None, "green"), (20, "orange"), (30, "red")]),
    ts("每分钟完成的轮次（按结果）",
       [(f"sum by (app_outcome) (rate(app_turn_duration_seconds_count{T}[{RI}])) * 60", "{{app_outcome}}")],
       12, y, w=12, h=8, stack=True),
]
panels.append(ts("轮次耗时 p50 / p95", [(q(0.5, "app_turn_duration_seconds"), "p50"),
                                        (q(0.95, "app_turn_duration_seconds"), "p95")], 0, y + 4, w=12, h=4, unit="s"))
y += 8

panels.append(row("任务队列（Redis Streams）", y)); y += 1
panels += [
    ts("积压与待确认", [(_queue_total("app_queue_lag"), "积压 {{messaging_destination_name}}"),
                   (_queue_total("app_queue_pending"), "待确认 {{messaging_destination_name}}")],
       0, y, description="积压：还没投递给消费组；待确认：已投递、还没 ACK（正在处理，或 worker 崩溃后等接管）。每个副本都上报同样的值，所以先取 max；agent_tasks 是每租户一条 Stream，再把各租户加起来。"),
    ts("最老的待确认消息已等待", [("max by (messaging_destination_name) (app_queue_oldest_pending_age_seconds)",
                          "{{messaging_destination_name}}")], 8, y, unit="s",
       description="超过接管阈值（90s）还在涨，说明任务反复失败或 worker 卡住。"),
    stat("死信", "max(app_queue_dead_letters)", 16, y, w=4, h=8,
         thresholds=[(None, "green"), (1, "red")]),
    stat("排队 p95", q(0.95, "app_queue_wait_seconds"), 20, y, w=4, h=8, unit="s"),
]
y += 8
# Fair scheduling: each tenant has its own stream and a limit on how many of its tasks run at once.
panels += [
    ts("各租户的积压", [(f"max by (app_tenant_id) (app_queue_lag{T})", "{{app_tenant_id}}")], 0, y,
       description="一个租户积压很多、别的租户为 0，是公平调度在起作用：它的任务只排在自己后面。"),
    ts("各租户占用的并发名额", [(f"max by (app_tenant_id) (app_queue_tenant_slots{T})", "{{app_tenant_id}}")], 8, y,
       description="每个租户同时运行的任务数，上限默认 8（TENANT_MAX_CONCURRENCY，可按租户覆盖）。长时间贴着上限且有积压，说明这个租户被自己的上限限住了。"),
    ts("排队 p95（按租户）", [(q(0.95, "app_queue_wait_seconds", by="app_tenant_id"), "{{app_tenant_id}}")], 16, y,
       unit="s", description="从入队到被取走。吵闹的租户排队变长，其他租户应保持平稳。"),
]
y += 8
panels += [
    ts("每分钟投递（按结果）", [(f"sum by (app_result) (rate(app_task_deliveries_total{T}[{RI}])) * 60", "{{app_result}}")],
       0, y, w=12, stack=True,
       description="结果是 run 的 outcome，或 conversation_busy / skipped_stale / retry_later / dead_letter / error。"),
    ts("单次投递处理耗时 p95（按结果）", [(q(0.95, "app_task_duration_seconds", by="app_result"), "{{app_result}}")],
       12, y, w=12, unit="s"),
]
y += 8

panels.append(row("Agent（LangGraph）", y)); y += 1
panels += [
    ts("节点耗时 p95", [(q(0.95, "app_node_duration_seconds", by="app_node", selector=""), "{{app_node}}")], 0, y, unit="s"),
    ts("每分钟分诊意图", [(f"sum by (app_intent) (rate(app_agent_intents_total{T}[{RI}])) * 60", "{{app_intent}}")],
       8, y, stack=True),
    ts("每分钟知识检索（按结果）", [(f"sum by (app_result) (rate(app_knowledge_searches_total{T}[{RI}])) * 60",
                             "{{app_result}}")], 16, y, stack=True,
       description="miss 会自动建单；unavailable 是检索失败后的降级（有告警）。"),
]
y += 8

panels.append(row("大模型（ai-gateway，OTel GenAI 指标）", y)); y += 1
panels += [
    ts("每分钟 token（按类型）",
       [(f"sum by (gen_ai_token_type) (rate(gen_ai_client_token_usage_sum{T}[{RI}])) * 60", "{{gen_ai_token_type}}")],
       0, y, stack=True),
    ts("模型调用耗时与首 token p95", [(q(0.95, "gen_ai_client_operation_duration_seconds"), "整次调用"),
                                 (q(0.95, "app_genai_time_to_first_token_seconds"), "首 token")], 8, y, unit="s"),
    ts("预算使用率（按租户）", [(f"max by (app_tenant_id) (app_budget_utilization{T})", "{{app_tenant_id}}")],
       16, y, w=5, unit="percentunit", description="1 = 当天预算用完；80% 以上告警。"),
    stat("预算拒绝（24h）", f"sum(increase(app_budget_refusals_total{T}[24h])) or vector(0)", 21, y, w=3, h=8,
         thresholds=[(None, "green"), (1, "red")]),
]
y += 8

panels.append(row("入口与工具", y)); y += 1
panels += [
    ts("HTTP 请求/秒（按服务）", [("sum by (job) (rate(http_server_duration_milliseconds_count[$__rate_interval]))", "{{job}}")],
       0, y, w=6),
    ts("HTTP 5xx 比例（按服务）",
       # "or ... * 0": a service with no 5xx at all shows 0 rather than no line.
       [('(sum by (job) (rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[$__rate_interval]))'
         ' or sum by (job) (rate(http_server_duration_milliseconds_count[$__rate_interval])) * 0)'
         ' / sum by (job) (rate(http_server_duration_milliseconds_count[$__rate_interval]))', "{{job}}")],
       6, y, w=6, unit="percentunit"),
    ts("每分钟限流拒绝 / 在线 SSE 连接",
       [("sum by (app_window) (rate(app_rate_limit_refusals_total[$__rate_interval])) * 60"
         ' or label_replace(vector(0), "app_window", "none", "", "")', "限流 {{app_window}}"),
        ("sum(app_sse_connections)", "SSE 连接")], 12, y, w=6),
    ts("每分钟工具调用（按工具与状态）",
       [(f"sum by (app_tool, app_status) (rate(app_tool_calls_total{T}[{RI}])) * 60", "{{app_tool}} {{app_status}}")],
       18, y, w=6, stack=True),
]
y += 8

# HITL: pending counts are sampled by tool-gateway's outbox relay; decisions are counted as they
# happen (by a handler, or by the relay when the approval expires). Every replica samples the same
# database, so the gauges are max'd per tenant before summing, never summed across instances.
panels.append(row("审批（HITL）", y)); y += 1
panels += [
    stat("待审批", f"sum(max by (app_tenant_id) (app_approvals_pending{T})) or vector(0)", 0, y, w=4, h=8,
         thresholds=[(None, "green"), (1, "yellow"), (10, "red")], description="等待处理人审批的工具调用"),
    stat("最久等待", f"max(app_approvals_oldest_pending_age_seconds{T}) or vector(0)", 4, y, w=4, h=8, unit="s",
         thresholds=[(None, "green"), (900, "yellow"), (3600, "red")],
         description="最老的待审批已等待多久；超过 1 小时会告警"),
    ts("每小时审批（申请 / 按结果）",
       [(f"round(sum(increase(app_approvals_requested_total{T}[1h])))", "申请"),
        (f"round(sum by (app_decision) (increase(app_approvals_decided_total{T}[1h])))", "{{app_decision}}")],
       8, y, w=8, description="approved / rejected 由处理人决定，expired 为超时未处理"),
    ts("审批耗时 p50 / p95（申请到决定）",
       [(q(0.5, "app_approval_decision_time_seconds"), "p50"),
        (q(0.95, "app_approval_decision_time_seconds"), "p95")], 16, y, w=8, unit="s"),
]

overview = {
    "uid": "ai-work-order-overview", "title": "ai-work-order 系统总览", "tags": ["ai-work-order"],
    "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"}, "editable": True,
    "templating": {"list": [tenant_var]},
    "links": [{"title": "单轮排查", "type": "link", "url": "/d/ai-work-order-turn", "icon": "dashboard"}],
    "panels": panels,
}

_next_id = 0
turn = {
    "uid": "ai-work-order-turn", "title": "ai-work-order 单轮排查", "tags": ["ai-work-order"],
    "timezone": "browser", "schemaVersion": 39, "version": 1, "time": {"from": "now-6h", "to": "now"},
    "editable": True,
    "description": "输入会话 id 找到它的各轮 trace；再输入 trace id 看这一轮在所有服务里的日志。",
    "templating": {"list": [
        {"name": "conversation_id", "label": "会话 id", "type": "textbox", "query": "", "current": {"text": "", "value": ""}},
        {"name": "trace_id", "label": "trace id", "type": "textbox", "query": "", "current": {"text": "", "value": ""}},
    ]},
    "panels": [
        {"type": "table", "title": "这个会话的每一轮（点 trace id 打开调用树）", "id": _id(), "datasource": TEMPO,
         "gridPos": {"h": 9, "w": 24, "x": 0, "y": 0},
         "targets": [{"datasource": TEMPO, "queryType": "traceql", "refId": "A", "limit": 50, "tableType": "traces",
                      "query": '{span."app.conversation_id"="$conversation_id"}'}]},
        {"type": "traces", "title": "调用树", "id": _id(), "datasource": TEMPO,
         "gridPos": {"h": 14, "w": 24, "x": 0, "y": 9},
         "targets": [{"datasource": TEMPO, "queryType": "traceql", "refId": "A", "query": "$trace_id"}]},
        {"type": "logs", "title": "这一轮在所有服务里的日志", "id": _id(), "datasource": LOKI,
         "gridPos": {"h": 12, "w": 24, "x": 0, "y": 23},
         "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Ascending", "enableLogDetails": True},
         "targets": [{"datasource": LOKI, "refId": "A", "expr": '{service_name=~".+"} | trace_id="$trace_id"'}]},
    ],
}

OUT.mkdir(parents=True, exist_ok=True)
for name, board in (("overview.json", overview), ("turn.json", turn)):
    (OUT / name).write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n")
print("written", [p.name for p in OUT.iterdir()])
