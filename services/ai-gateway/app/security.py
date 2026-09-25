import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings


async def require_service_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Only trusted internal callers (Agent Runtime) may spend model budget, and tenant_id arrives
    in the body, so without this any pod on the network could bill any tenant."""
    expected = f"Bearer {settings.ai_gateway_service_token}".encode()
    if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid or missing service token")
