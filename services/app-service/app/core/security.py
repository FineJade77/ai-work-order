import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings


def require_internal_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guards /internal routes, which are for other services (tool-gateway), never end users."""
    expected = f"Bearer {settings.app_service_internal_token}".encode()
    if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid or missing service token")
