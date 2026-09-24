import hmac
import os
from typing import Annotated

from fastapi import Header, HTTPException

# Read at import, like MODEL_API_KEY in providers.py, so a missing token fails at startup.
_EXPECTED = f"Bearer {os.environ['AI_GATEWAY_SERVICE_TOKEN']}".encode()


async def require_service_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Only trusted internal callers (Agent Runtime) may spend model budget, and tenant_id arrives
    in the body, so without this any pod on the network could bill any tenant."""
    if authorization is None or not hmac.compare_digest(authorization.encode(), _EXPECTED):
        raise HTTPException(status_code=401, detail="invalid or missing service token")
