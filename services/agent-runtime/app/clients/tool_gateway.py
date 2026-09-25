from dataclasses import dataclass
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


class ApprovalPending(Exception):
    """The call waits for a handler's approval (tenant policy). Call again with the same
    idempotency key once it is decided: the tool then runs, or the refusal comes back."""

    def __init__(self, approval_id: str, expires_at: str):
        super().__init__(f"waiting for approval {approval_id}")
        self.approval_id = approval_id
        self.expires_at = expires_at


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
        if body["status"] == "pending_approval":
            raise ApprovalPending(body["approval"]["id"], body["approval"]["expires_at"])
        return body["result"]

    async def list_tools(self, task: AgentTask) -> list[dict[str, Any]]:
        """The tools this agent role may call in the task's tenant, as OpenAI function definitions."""
        resp = await self._http.get(
            "/v1/tools/list", params={"tenant_id": str(task.tenant_id), "agent_role": self._agent_role}
        )
        resp.raise_for_status()
        return resp.json()["tools"]


@dataclass(frozen=True)
class CallFailure:
    code: str
    transient: bool  # worth retrying: the same call may succeed in a moment


# Refusals no retry can change: the tenant's tool bindings or the call itself are wrong.
_CONFIG_CODES = frozenset({"TOOL_NOT_ALLOWED", "UNKNOWN_TOOL", "INVALID_ARGUMENTS"})


def classify_failure(exc: Exception) -> CallFailure | None:
    """What a failed call() means, or None if exc is not a call failure (e.g. a bug, or RetryLater).

    TOOL_ERROR (the tool's backend failed), timeouts, connection errors and 5xx are transient.
    Tool refusals and other 4xx mean misconfiguration: the next attempt would fail the same way.
    """
    if isinstance(exc, ToolCallFailed):
        return CallFailure(exc.code, transient=exc.code not in _CONFIG_CODES)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return CallFailure(_error_code(exc.response) or f"HTTP_{status}", transient=status >= 500)
    if isinstance(exc, httpx.TransportError):
        return CallFailure(type(exc).__name__, transient=True)
    return None


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
