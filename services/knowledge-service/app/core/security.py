import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings


def require_service_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Knowledge search is service-to-service (tool-gateway), never end users."""
    expected = f"Bearer {settings.knowledge_service_token}".encode()
    if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid or missing service token")
