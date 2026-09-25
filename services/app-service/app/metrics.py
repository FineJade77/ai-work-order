"""app-service's metrics (OTel metrics API; exported over OTLP by the OTel agent, like traces).

TURN_DURATION is what the turn SLO is measured on: from storing the employee's message to storing
the reply, whatever happened in between (queueing, retries, requeues). Its buckets include the
SLO's 30 s boundary so the ratio can be read straight off the histogram.
"""

from opentelemetry import metrics

meter = metrics.get_meter("app_service")

TURN_DURATION = meter.create_histogram(
    "app.turn.duration", unit="s", description="Employee message stored to reply stored, by status and outcome",
    explicit_bucket_boundaries_advisory=[1, 2, 5, 10, 15, 20, 30, 45, 60, 120, 300, 600, 1200],
)
TURN_REQUEUES = meter.create_counter("app.turn.requeues", description="Turns re-enqueued by the requeuer, or given up")
RATE_LIMIT_REFUSALS = meter.create_counter(
    "app.rate_limit.refusals", description="Messages refused by the per-employee rate limit, by window",
)
SSE_CONNECTIONS = meter.create_up_down_counter("app.sse.connections", description="Open SSE streams")
