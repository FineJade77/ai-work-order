"""grant_access: give an employee access to a system. The high-risk tool of the HITL demo: whether
a call waits for a handler's approval is tenant policy (role_tool_bindings.requires_approval),
not part of the tool, so the same tool can need approval in one tenant and not in another.
"""

from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import AccessGrant
from app.db.session import session_factory, tenant_scope
from app.tools.base import ToolContext, ToolSpec

ACCESS_LEVELS = ["read", "write", "admin"]


class AccessProvider(ABC):
    @abstractmethod
    async def grant(
        self, *, tenant_id: str, user_id: str, system: str, access_level: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]: ...


class MockAccessProvider(AccessProvider):
    """Records the grant in tool-gateway's own database instead of calling an identity system.
    Deduplicated by the call's idempotency key, as a real downstream must be: a call re-run after
    a crash between granting and recording success must not grant twice."""

    async def grant(
        self, *, tenant_id: str, user_id: str, system: str, access_level: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        Session = session_factory()
        async with Session() as session, tenant_scope(session, tenant_id):
            await session.execute(
                pg_insert(AccessGrant)
                .values(
                    tenant_id=tenant_id, user_id=user_id, system=system, access_level=access_level, reason=reason,
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
            )
            grant = await session.scalar(select(AccessGrant).where(AccessGrant.idempotency_key == idempotency_key))
            await session.commit()
        return {
            "grant_id": str(grant.id), "system": grant.system, "access_level": grant.access_level, "status": "granted",
        }


@lru_cache
def get_access_provider() -> AccessProvider:
    return MockAccessProvider()


async def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await get_access_provider().grant(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        system=args["system"],
        access_level=args["access_level"],
        reason=args["reason"],
        idempotency_key=ctx.idempotency_key,
    )


GRANT_ACCESS = ToolSpec(
    name="grant_access",
    description="为员工开通某个系统的访问权限。是否需要处理人审批由租户策略决定。",
    parameters={
        "type": "object",
        "properties": {
            "system": {
                "type": "string", "minLength": 1, "maxLength": 100, "description": "要开通权限的系统名称",
            },
            "access_level": {"type": "string", "enum": ACCESS_LEVELS, "description": "权限级别"},
            "reason": {
                "type": "string", "minLength": 1, "maxLength": 500, "description": "申请理由，取自员工的原话",
            },
        },
        "required": ["system", "access_level", "reason"],
        "additionalProperties": False,
    },
    handler=_handle,
    side_effect=True,
)
