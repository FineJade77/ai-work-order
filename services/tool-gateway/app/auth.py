import hmac
import os

from fastapi import Header, HTTPException

_EXPECTED = f"Bearer {os.environ['TOOL_GATEWAY_SERVICE_TOKEN']}".encode()


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """Only trusted internal callers (Agent Runtime, app-service) may reach the gateway.

    tenant_id and agent_role arrive in the request body, so without this any pod on the
    network could claim any tenant and any role.
    """
    if authorization is None or not hmac.compare_digest(authorization.encode(), _EXPECTED):
        raise HTTPException(status_code=401, detail="invalid or missing service token")
