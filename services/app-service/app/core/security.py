import base64
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import anyio
import jwt
from fastapi import Depends, Header

from app.core.config import Settings, get_settings
from app.core.errors import ApiError

# scrypt from the standard library, OWASP parameters (N=2^17, r=8, p=1, ~128 MiB). Each hash takes
# a CPU-second or so, which is the point, so callers use the async wrappers that run it in a thread.
SCRYPT_N = 2**17
_SCRYPT_MAXMEM = 256 * 1024 * 1024
# Each scrypt holds ~128 MiB; without a cap, a burst of logins across the 40-thread pool could take ~5 GiB.
_SCRYPT_SLOTS = threading.BoundedSemaphore(4)
JWT_ALGORITHM = "HS256"
# handler: whoever works the tenant's tickets (IT, HR, admin... whatever the tenant covers).
ROLES = ("employee", "handler")


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    """n is stored in the hash, so verification needs no configuration; tests pass a cheaper one."""
    salt = secrets.token_bytes(16)
    with _SCRYPT_SLOTS:
        digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, maxmem=_SCRYPT_MAXMEM)
    return f"scrypt${n}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    with _SCRYPT_SLOTS:
        candidate = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=_SCRYPT_MAXMEM
        )
    return hmac.compare_digest(candidate, base64.b64decode(digest))


@lru_cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


async def verify_password_async(password: str, stored: str | None) -> bool:
    """With no stored hash (unknown tenant or user, or no password set) it still spends the time of
    a real check, so response timing doesn't reveal which accounts exist."""
    ok = await anyio.to_thread.run_sync(verify_password, password, stored or _dummy_hash())
    return ok and stored is not None


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    tenant_id: uuid.UUID
    role: str

    @property
    def is_handler(self) -> bool:
        return self.role == "handler"


def issue_token(user: CurrentUser, settings: Settings) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user.id),
        "tid": str(user.tenant_id),
        "role": user.role,
        "iat": now,
        "exp": now + settings.jwt_ttl_seconds,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_token(token: str, settings: Settings) -> CurrentUser:
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=[JWT_ALGORITHM], options={"require": ["sub", "tid", "role", "exp"]}
        )
        user = CurrentUser(id=uuid.UUID(claims["sub"]), tenant_id=uuid.UUID(claims["tid"]), role=claims["role"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise ApiError(401, "INVALID_TOKEN", "invalid or expired token") from exc
    if user.role not in ROLES:
        raise ApiError(401, "INVALID_TOKEN", "invalid or expired token")
    return user


async def current_user(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """Tenant and user come only from the token, never from request parameters."""
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "INVALID_TOKEN", "missing bearer token")
    return decode_token(authorization.removeprefix("Bearer "), settings)
