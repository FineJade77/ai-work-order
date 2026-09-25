import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import budget

pytestmark = pytest.mark.anyio

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=SHANGHAI)


def use(admin_db, tenant_id, tokens, at):
    admin_db.execute(
        "INSERT INTO model_call_logs (tenant_id, model, prompt_tokens, completion_tokens, duration_ms, status, created_at)"
        " VALUES (%s, 'test-model', %s, %s, 100, 'ok', %s)",
        (tenant_id, tokens - tokens // 4, tokens // 4, at),
    )


async def check(tenant_id, settings, limit):
    await budget.check_budget(tenant_id, settings.model_copy(update={"daily_token_budget": limit}), now=NOW)


async def test_usage_below_the_budget_passes_and_reaching_it_is_refused(admin_db, settings, tenant):
    use(admin_db, tenant, 600, NOW.replace(hour=1))
    use(admin_db, tenant, 399, NOW.replace(hour=9))
    await check(tenant, settings, 1000)

    use(admin_db, tenant, 1, NOW.replace(hour=9, minute=30))
    with pytest.raises(budget.BudgetExceeded) as exc:
        await check(tenant, settings, 1000)
    assert (exc.value.used, exc.value.limit) == (1000, 1000)
    assert exc.value.retry_after_seconds == 14 * 3600  # until midnight in Shanghai


async def test_only_today_counts_in_the_budget_timezone(admin_db, settings, tenant):
    # 23:59 yesterday Shanghai time: the previous budget day, though it is the same UTC date as NOW.
    use(admin_db, tenant, 5000, datetime(2026, 9, 24, 23, 59, tzinfo=SHANGHAI))
    await check(tenant, settings, 1000)


async def test_other_tenants_usage_is_not_counted(admin_db, settings, tenant):
    other = str(uuid.uuid4())
    try:
        use(admin_db, other, 5000, NOW.replace(hour=9))
        await check(tenant, settings, 1000)
    finally:
        admin_db.execute("DELETE FROM model_call_logs WHERE tenant_id = %s", (other,))


async def test_a_tenant_override_replaces_the_default(admin_db, settings, tenant):
    use(admin_db, tenant, 500, NOW.replace(hour=9))
    await check(tenant, settings, 1000)

    admin_db.execute("INSERT INTO tenant_token_budgets (tenant_id, daily_tokens) VALUES (%s, 400)", (tenant,))
    with pytest.raises(budget.BudgetExceeded):
        await check(tenant, settings, 1000)

    admin_db.execute("UPDATE tenant_token_budgets SET daily_tokens = 100000 WHERE tenant_id = %s", (tenant,))
    await check(tenant, settings, 1000)  # also raises it above the default
