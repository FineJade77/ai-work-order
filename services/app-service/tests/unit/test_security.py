import time
import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import CurrentUser, decode_token, hash_password, issue_token, verify_password, verify_password_async

SETTINGS = Settings(
    database_url="unused", redis_url="unused", jwt_secret="s" * 32,
    ticket_service_url="http://unused", ticket_service_token="unused",
    tool_gateway_url="http://unused", tool_gateway_service_token="unused",
)
USER = CurrentUser(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="employee")


def test_password_round_trip_and_salting():
    first, second = hash_password("pw", n=2**14), hash_password("pw", n=2**14)
    assert first != second  # salted
    assert verify_password("pw", first) and not verify_password("wrong", first)


@pytest.mark.parametrize("stored", ["", "not-a-hash", "bcrypt$1$2$3$4$5"])
def test_malformed_hash_never_verifies(stored):
    assert not verify_password("pw", stored)


@pytest.mark.anyio
async def test_missing_hash_fails_even_with_a_matching_dummy():
    assert not await verify_password_async("pw", None)
    assert await verify_password_async("pw", hash_password("pw", n=2**14))


def test_token_round_trip():
    assert decode_token(issue_token(USER, SETTINGS), SETTINGS) == USER


def _encode(claims, secret="s" * 32):
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.mark.parametrize(
    "token",
    [
        _encode({"sub": str(USER.id), "tid": str(USER.tenant_id), "role": "employee", "exp": int(time.time()) - 1}),
        _encode({"sub": str(USER.id), "tid": str(USER.tenant_id), "role": "employee", "exp": int(time.time()) + 60}, "x" * 32),
        _encode({"sub": str(USER.id), "tid": str(USER.tenant_id), "role": "admin", "exp": int(time.time()) + 60}),
        _encode({"sub": str(USER.id), "role": "employee", "exp": int(time.time()) + 60}),
        _encode({"sub": "not-a-uuid", "tid": str(USER.tenant_id), "role": "employee", "exp": int(time.time()) + 60}),
        "garbage",
    ],
    ids=["expired", "wrong-secret", "unknown-role", "no-tenant", "bad-sub", "garbage"],
)
def test_bad_tokens_are_401(token):
    with pytest.raises(ApiError) as exc:
        decode_token(token, SETTINGS)
    assert exc.value.http_status == 401
