"""tool-gateway's metrics (OTel metrics API; exported over OTLP by the OTel agent, like traces).

One count and duration per tool call attempt, labelled like its audit row: the tool, the audit
status (success / rejected / error) and the error code, all from small fixed sets.
"""

from opentelemetry import metrics

meter = metrics.get_meter("tool_gateway")

TOOL_CALLS = meter.create_counter("app.tool.calls", description="Tool call attempts, by tool, status and error code")
TOOL_CALL_DURATION = meter.create_histogram(
    "app.tool.call.duration", unit="s", description="Tool call attempts, policy checks and audit included",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)
