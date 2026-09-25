import json
import uuid

import httpx
import pytest

from app.clients.ai_gateway import AIGatewayClient, BudgetExceeded, ModelStreamIncomplete
from app.clients.tool_gateway import ToolCallFailed, ToolGatewayClient
from app.core.errors import RetryLater
from tests.fakes import make_task

pytestmark = pytest.mark.anyio

TASK = make_task(uuid.uuid4(), "VPN 连不上")


def client(handler, base_url="http://gw"):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


async def test_ai_gateway_yields_deltas_until_done():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        body = sse({"delta": "你"}, {"delta": "好"}, {"done": True, "usage": {}})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    text = await AIGatewayClient(client(handler)).complete(TASK, system_prompt="sys", messages=[{"role": "user", "content": "x"}])
    assert text == "你好"
    assert seen["body"]["tenant_id"] == str(TASK.tenant_id) and seen["body"]["system_prompt"] == "sys"


async def test_ai_gateway_stream_cut_short_is_an_error():
    def handler(request):
        return httpx.Response(200, text=sse({"delta": "半"}), headers={"content-type": "text/event-stream"})

    with pytest.raises(ModelStreamIncomplete):
        await AIGatewayClient(client(handler)).complete(TASK, system_prompt="", messages=[])


async def test_tool_call_carries_runtime_context_and_idempotency_key():
    seen = {}

    def handler(request):
        seen["body"], seen["key"] = json.loads(request.content), request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"status": "success", "result": {"ticket_id": "T"}})

    tools = ToolGatewayClient(client(handler), "it_support_agent")
    result = await tools.call(TASK, "create_ticket", {"summary": "s", "category": "vpn"}, idempotency_key="k")
    assert result == {"ticket_id": "T"}
    assert seen["key"] == "k"
    assert seen["body"] == {
        "tenant_id": str(TASK.tenant_id),
        "session_id": str(TASK.conversation_id),
        "user_id": str(TASK.user_id),
        "agent_role": "it_support_agent",
        "tool_name": "create_ticket",
        "arguments": {"summary": "s", "category": "vpn"},
    }


async def test_tool_error_envelope_raises_tool_call_failed():
    def handler(request):
        return httpx.Response(200, json={"status": "error", "error": {"code": "TOOL_ERROR", "message": "down"}})

    with pytest.raises(ToolCallFailed) as exc:
        await ToolGatewayClient(client(handler), "r").call(TASK, "search_knowledge", {"query": "q"})
    assert exc.value.code == "TOOL_ERROR"


async def test_in_progress_means_retry_later_but_other_conflicts_are_errors():
    def in_progress(request):
        return httpx.Response(409, json={"error": {"code": "IN_PROGRESS", "message": "running"}})

    def conflict(request):
        return httpx.Response(409, json={"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "reused"}})

    with pytest.raises(RetryLater):
        await ToolGatewayClient(client(in_progress), "r").call(TASK, "create_ticket", {}, idempotency_key="k")
    with pytest.raises(httpx.HTTPStatusError):
        await ToolGatewayClient(client(conflict), "r").call(TASK, "create_ticket", {}, idempotency_key="k")


async def test_budget_refusal_raises_budget_exceeded_but_other_429s_stay_http_errors():
    def over_budget(request):
        return httpx.Response(429, json={"error": {"code": "TOKEN_BUDGET_EXCEEDED", "message": "used up"}})

    def throttled(request):
        return httpx.Response(429, text="slow down")

    with pytest.raises(BudgetExceeded):
        await AIGatewayClient(client(over_budget)).complete(TASK, system_prompt="", messages=[])
    with pytest.raises(httpx.HTTPStatusError):
        await AIGatewayClient(client(throttled)).complete(TASK, system_prompt="", messages=[])
