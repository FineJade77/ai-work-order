"""Generates the Grafana-managed alert rules (provisioning/alerting/ai-work-order.yaml, committed).
Edit this script, not the YAML, then regenerate:

    python3 deploy/observability/grafana/generate_alerts.py deploy/observability/grafana/provisioning/alerting

Every rule is the same three steps: A, a Prometheus instant query; B, reduce each series to its
last value; C, compare with a threshold. JSON is valid YAML, so the file is written as JSON.
"""

import json
import pathlib
import sys

SLO_TARGET = 0.95  # of turns complete within 30 s
# Multiwindow burn-rate alert (Google SRE workbook): page when the error budget burns 14.4x too
# fast over both 1 h and 5 m, i.e. 2% of a 30-day budget in an hour, and it is still happening.
FAST_BURN = 14.4


def _error_ratio(window: str) -> str:
    return (f'(1 - sum(rate(app_turn_duration_seconds_bucket{{le="30"}}[{window}]))'
            f' / sum(rate(app_turn_duration_seconds_count[{window}])))')


RULES = [
    {
        "uid": "ai-work-order-queue-backlog",
        "title": "任务队列积压",
        "expr": 'max(app_queue_lag{messaging_destination_name="agent_tasks"})',
        "op": "gt", "threshold": 20, "for": "5m", "severity": "warning",
        "summary": "agent_tasks 有 {{ printf \"%.0f\" $values.B.Value }} 条任务还没投递，持续 5 分钟",
        "description": "worker 处理不过来或者没在消费：看 agent-runtime 副本数、KEDA 扩容，以及看板的'单次投递处理耗时'。",
    },
    {
        "uid": "ai-work-order-stuck-task",
        "title": "有任务长时间未确认",
        "expr": 'max(app_queue_oldest_pending_age_seconds{messaging_destination_name="agent_tasks"})',
        "op": "gt", "threshold": 300, "for": "0s", "severity": "warning",
        "summary": "最老的待确认任务已经等了 {{ printf \"%.0f\" $values.B.Value }} 秒",
        "description": "超过接管阈值（90s）很久仍未确认：任务在反复失败重投，或者会话锁冲突。按 task_id 在 Tempo 里查 process agent_tasks。",
    },
    {
        "uid": "ai-work-order-dead-letters",
        "title": "出现新的死信",
        "expr": "max(app_queue_dead_letters) - min_over_time(max(app_queue_dead_letters)[15m:1m])",
        "op": "gt", "threshold": 0, "for": "0s", "severity": "critical",
        "summary": "15 分钟内新增 {{ printf \"%.0f\" $values.B.Value }} 条死信",
        "description": "任务投递超过上限或消息格式错误，员工收到了失败提示。XRANGE agent_tasks:dead - + 查看原因。",
    },
    {
        "uid": "ai-work-order-knowledge-unavailable",
        "title": "知识库检索降级",
        "expr": 'sum(increase(app_knowledge_searches_total{app_result="unavailable"}[10m]))',
        "op": "gt", "threshold": 0, "for": "0s", "severity": "warning",
        # increase() extrapolates over the window, hence a fraction: round it for people.
        "summary": "10 分钟内约 {{ printf \"%.0f\" $values.B.Value }} 次检索失败后降级（员工收到'知识库暂时无法访问'）",
        "description": "knowledge-service 或 tool-gateway 故障，或者租户缺工具绑定。日志搜 knowledge_unavailable 看 code。",
    },
    {
        "uid": "ai-work-order-budget-high",
        "title": "租户 token 预算即将用完",
        "expr": "max by (app_tenant_id) (app_budget_utilization)",
        "op": "gt", "threshold": 0.8, "for": "0s", "severity": "warning",
        "summary": "租户 {{ $labels.app_tenant_id }} 已用掉当天预算的 {{ humanizePercentage $values.B.Value }}",
        "description": "用完后员工会收到'额度已用完'的回复。需要时在 tenant_token_budgets 里调高（README'调整限额'）。",
    },
    {
        "uid": "ai-work-order-slo-burn",
        "title": "轮次 SLO 错误预算燃烧过快",
        "expr": (f"({_error_ratio('1h')} / {1 - SLO_TARGET:.2f})"
                 f" and ({_error_ratio('5m')} / {1 - SLO_TARGET:.2f} > {FAST_BURN})"
                 " and (sum(increase(app_turn_duration_seconds_count[1h])) > 10)"),
        "op": "gt", "threshold": FAST_BURN, "for": "2m", "severity": "critical",
        "summary": "超过 30 秒的轮次太多：错误预算按 {{ printf \"%.1f\" $values.B.Value }} 倍速消耗",
        "description": f"SLO：{SLO_TARGET:.0%} 的轮次 30 秒内完成。1 小时和 5 分钟窗口都超过 {FAST_BURN} 倍燃烧率（且 1 小时内至少 10 轮）才触发。看板'轮次耗时'和 Tempo 里慢的 trace。",
    },
    {
        "uid": "ai-work-order-approval-waiting",
        "title": "有审批超过 1 小时没人处理",
        "expr": "max by (app_tenant_id) (app_approvals_oldest_pending_age_seconds)",
        "op": "gt", "threshold": 3600, "for": "0s", "severity": "warning",
        "summary": "租户 {{ $labels.app_tenant_id }} 最老的待审批已等待 {{ humanizeDuration $values.B.Value }}",
        "description": "员工的操作卡在 HITL 审批上，处理人还没批准或拒绝；24 小时后会自动过期。GET /v1/approvals?status=pending 查看。",
    },
    {
        "uid": "ai-work-order-http-errors",
        "title": "服务 5xx 比例过高",
        "expr": ('sum by (job) (rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[5m]))'
                 " / sum by (job) (rate(http_server_duration_milliseconds_count[5m]))"),
        "op": "gt", "threshold": 0.05, "for": "5m", "severity": "warning",
        "summary": "{{ $labels.job }} 的 5xx 比例 {{ humanizePercentage $values.B.Value }}",
        "description": "Tempo 里查 {status=error} 找出错的请求。",
    },
]


def rule(r: dict) -> dict:
    return {
        "uid": r["uid"],
        "title": r["title"],
        "condition": "C",
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 3600, "to": 0}, "datasourceUid": "prometheus",
             "model": {"refId": "A", "expr": r["expr"], "instant": True, "range": False}},
            {"refId": "B", "datasourceUid": "__expr__",
             "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last",
                       "settings": {"mode": "dropNN"}}},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "B",
                       "conditions": [{"evaluator": {"type": r["op"], "params": [r["threshold"]]}}]}},
        ],
        "for": r["for"],
        "noDataState": "OK",  # no series (no traffic, or nothing has gone wrong yet) is not an alert
        "execErrState": "Error",
        "labels": {"severity": r["severity"], "service": "ai-work-order"},
        "annotations": {"summary": r["summary"], "description": r["description"]},
    }


document = {
    "apiVersion": 1,
    "groups": [{"orgId": 1, "name": "ai-work-order", "folder": "ai-work-order", "interval": "1m",
                "rules": [rule(r) for r in RULES]}],
}

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
(out / "ai-work-order.yaml").write_text(
    "# Generated by deploy/observability/grafana/generate_alerts.py; edit that, not this.\n"
    + json.dumps(document, ensure_ascii=False, indent=2) + "\n"
)
print("written", out / "ai-work-order.yaml", len(RULES), "rules")
