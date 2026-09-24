import uuid
from typing import Any

import httpx

from app.core.errors import ApiError


class TicketServiceClient:
    """ticket-service's internal API. Its business errors (403/404/409 with an error body) pass
    through unchanged; anything else is a 502, without upstream details."""

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def list(self, tenant_id: uuid.UUID, **filters: Any) -> dict:
        params = {"tenant_id": str(tenant_id), **{k: str(v) for k, v in filters.items() if v is not None}}
        return await self._send("GET", "/internal/v1/tickets", params=params)

    async def get(self, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> dict:
        return await self._send("GET", f"/internal/v1/tickets/{ticket_id}", params={"tenant_id": str(tenant_id)})

    async def transition(self, tenant_id: uuid.UUID, ticket_id: uuid.UUID, body: dict[str, Any]) -> dict:
        payload = {"tenant_id": str(tenant_id), **{k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()}}
        return await self._send("POST", f"/internal/v1/tickets/{ticket_id}/transitions", json=payload)

    async def _send(self, method: str, url: str, **kwargs: Any) -> dict:
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(502, "UPSTREAM_UNAVAILABLE", "ticket service is unavailable") from exc
        if resp.is_success:
            return resp.json()
        error = _error_body(resp)
        if resp.status_code in (400, 403, 404, 409) and error:
            raise ApiError(resp.status_code, error["code"], error["message"])
        raise ApiError(502, "UPSTREAM_ERROR", "ticket service request failed")


def _error_body(resp: httpx.Response) -> dict | None:
    try:
        error = resp.json()["error"]
    except (ValueError, KeyError, TypeError):
        return None
    return error if isinstance(error, dict) and {"code", "message"} <= error.keys() else None
