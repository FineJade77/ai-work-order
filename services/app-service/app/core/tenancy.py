import uuid


def parse_tenant_id(tenant_id: str) -> uuid.UUID:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    try:
        return uuid.UUID(tenant_id)
    except ValueError as exc:
        raise ValueError(f"tenant_id is not a valid UUID: {tenant_id!r}") from exc
