"""tool-gateway's metrics (OTel metrics API; exported over OTLP by the OTel agent, like traces).

One count and duration per tool call attempt, labelled like its audit row: the tool, the audit
status (success / rejected / error) and the error code, all from small fixed sets.
"""

from datetime import datetime, timezone

from opentelemetry import metrics

meter = metrics.get_meter("tool_gateway")

TOOL_CALLS = meter.create_counter("app.tool.calls", description="Tool call attempts, by tool, status and error code")
TOOL_CALL_DURATION = meter.create_histogram(
    "app.tool.call.duration", unit="s", description="Tool call attempts, policy checks and audit included",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

# HITL approvals. Pending counts come from the relay, which sees every tenant (see ApprovalRelay.sample).
APPROVALS_REQUESTED = meter.create_counter("app.approvals.requested", description="Calls that had to wait for approval")
APPROVALS_DECIDED = meter.create_counter(
    "app.approvals.decided", description="Approvals decided, by decision: approved, rejected, expired"
)
APPROVAL_DECISION_TIME = meter.create_histogram(
    "app.approval.decision_time", unit="s", description="Approval requested to decided, by decision",
    explicit_bucket_boundaries_advisory=[10, 60, 300, 900, 1800, 3600, 7200, 14400, 43200, 86400],
)


def record_decision(*, tenant_id: str, tool_name: str, decision: str, requested_at: datetime) -> None:
    labels = {"app.decision": decision, "app.tool": tool_name, "app.tenant_id": tenant_id}
    APPROVALS_DECIDED.add(1, labels)
    APPROVAL_DECISION_TIME.record((datetime.now(timezone.utc) - requested_at).total_seconds(), labels)
