"""Per-tenant daily token budget, checked before each model call.

Usage is summed from model_call_logs, so it counts what was actually recorded: calls whose usage
write was lost (see providers.py) are not counted. The check and the count are not atomic, so
concurrent calls can overshoot the budget by what they use between them: a soft cap.
"""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.models import ModelCallLog, TenantTokenBudget
from app.db.session import session_factory, tenant_scope


meter = metrics.get_meter("ai_gateway.budget")
REFUSALS = meter.create_counter("app.budget.refusals", description="Model calls refused: daily token budget used up")
# Share of today's budget each tenant had used at its latest check, reported as a gauge.
_utilization: dict[str, float] = {}


def _observe_utilization(_: CallbackOptions):
    for tenant_id, ratio in list(_utilization.items()):
        yield Observation(ratio, {"app.tenant_id": tenant_id})


meter.create_observable_gauge(
    "app.budget.utilization", callbacks=[_observe_utilization],
    description="Share of the daily token budget used, as of the tenant's latest model call (1 = used up)",
)


class BudgetExceeded(Exception):
    def __init__(self, *, used: int, limit: int, retry_after_seconds: int):
        super().__init__(f"used {used} of {limit} tokens today")
        self.used = used
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds  # until the budget day rolls over


def day_bounds(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    """Start and end of the budget day containing `now` (an aware datetime)."""
    local = now.astimezone(ZoneInfo(timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Add a day to the date, not 24 hours to the instant, so a DST change can't shift midnight.
    end = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


async def check_budget(tenant_id: str, settings: Settings, *, now: datetime | None = None) -> None:
    """Raises BudgetExceeded once the tenant has used its budget for the day."""
    now = now or datetime.now(ZoneInfo(settings.budget_timezone))
    start, end = day_bounds(now, settings.budget_timezone)
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        override = await session.scalar(
            select(TenantTokenBudget.daily_tokens).where(TenantTokenBudget.tenant_id == tenant_id)
        )
        used = await session.scalar(
            select(func.coalesce(func.sum(ModelCallLog.prompt_tokens + ModelCallLog.completion_tokens), 0)).where(
                ModelCallLog.tenant_id == tenant_id, ModelCallLog.created_at >= start
            )
        )
    limit = settings.daily_token_budget if override is None else override
    _utilization[tenant_id] = used / limit if limit else 1.0
    if used >= limit:
        REFUSALS.add(1, {"app.tenant_id": tenant_id})
        raise BudgetExceeded(used=used, limit=limit, retry_after_seconds=math.ceil((end - now).total_seconds()))
