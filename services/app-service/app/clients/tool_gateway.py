import uuid
from typing import Any

import httpx

from app.core.errors import ApiError


class ToolGatewayClient:
    """tool-gateway's approval API, used on behalf of handlers (list, decide) and employees (their own).
    Its business errors (403/404/409 with an error body) pass through unchanged; anything else is a 502."""

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def list_approvals(self, tenant_id: uuid.UUID, **filters: Any) -> dict:
        params = {"tenant_id": str(tenant_id), **{k: str(v) for k, v in filters.items() if v is not None}}
        return await self._send("GET", "/v1/approvals", params=params)

    async def get_approval(self, tenant_id: uuid.UUID, approval_id: uuid.UUID) -> dict:
        return await self._send("GET", f"/v1/approvals/{approval_id}", params={"tenant_id": str(tenant_id)})

    async def decide(
        self, tenant_id: uuid.UUID, approval_id: uuid.UUID, *, decision: str, approver_id: uuid.UUID, note: str | None
    ) -> dict:
        body = {"tenant_id": str(tenant_id), "decision": decision, "approver_id": str(approver_id), "note": note}
        return await self._send("POST", f"/v1/approvals/{approval_id}/decision", json=body)

    async def _send(self, method: str, url: str, **kwargs: Any) -> dict:
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(502, "UPSTREAM_UNAVAILABLE", "tool gateway is unavailable") from exc
        if resp.is_success:
            return resp.json()
        error = _error_body(resp)
        if resp.status_code in (400, 403, 404, 409) and error:
            raise ApiError(resp.status_code, error["code"], error["message"])
        raise ApiError(502, "UPSTREAM_ERROR", "tool gateway request failed")


def _error_body(resp: httpx.Response) -> dict | None:
    try:
        error = resp.json()["error"]
    except (ValueError, KeyError, TypeError):
        return None
    return error if isinstance(error, dict) and {"code", "message"} <= error.keys() else None
