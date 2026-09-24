from typing import Any

import httpx

from app.core.errors import RetryLater
from app.schemas.tasks import AgentTask


class ToolCallFailed(Exception):
    """A tool error envelope (status=error): the call reached the tool and it said no."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ToolGatewayClient:
    def __init__(self, http: httpx.AsyncClient, agent_role: str):
        self._http = http
        self._agent_role = agent_role

    async def call(
        self, task: AgentTask, tool_name: str, arguments: dict[str, Any], *, idempotency_key: str | None = None
    ) -> Any:
        resp = await self._http.post(
            "/v1/tools/call",
            json={
                "tenant_id": str(task.tenant_id),
                "session_id": str(task.conversation_id),
                "user_id": str(task.user_id),
                "agent_role": self._agent_role,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else {},
        )
        if resp.status_code == 409 and _error_code(resp) == "IN_PROGRESS":
            # An earlier attempt of this same call is still marked running (e.g. we crashed mid-call).
            raise RetryLater(f"{tool_name} is still in progress under key {idempotency_key}")
        resp.raise_for_status()
        body = resp.json()
        if body["status"] == "error":
            raise ToolCallFailed(body["error"]["code"], body["error"]["message"])
        return body["result"]


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
