import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings


def require_service_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Only trusted internal callers (Agent Runtime, app-service) may reach the gateway.

    tenant_id and agent_role arrive in the request body, so without this any pod on the
    network could claim any tenant and any role.
    """
    expected = f"Bearer {settings.tool_gateway_service_token}".encode()
    if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid or missing service token")
