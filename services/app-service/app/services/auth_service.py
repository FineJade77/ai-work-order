from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import CurrentUser, issue_token, verify_password_async
from app.db.models import Tenant, User
from app.db.session import session_factory, tenant_scope


async def login(tenant_slug: str, email: str, password: str, settings: Settings) -> tuple[str, CurrentUser]:
    user = None
    Session = session_factory()
    async with Session() as session:
        # tenants has no RLS; users does, so the tenant must be known before looking up the user.
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
        if tenant is not None:
            async with tenant_scope(session, tenant.id):
                user = await session.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if not await verify_password_async(password, user.password_hash if user else None):
        # One answer for unknown tenant, unknown email and wrong password.
        raise ApiError(401, "INVALID_CREDENTIALS", "tenant, email or password is incorrect")
    current = CurrentUser(id=user.id, tenant_id=user.tenant_id, role=user.role)
    return issue_token(current, settings), current


async def profile(user: CurrentUser) -> tuple[User, Tenant]:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, user.tenant_id):
        row = (
            await session.execute(
                select(User, Tenant).join(Tenant, Tenant.id == User.tenant_id).where(User.id == user.id)
            )
        ).one_or_none()
    if row is None:  # deleted since the token was issued
        raise ApiError(401, "INVALID_TOKEN", "user no longer exists")
    return row.User, row.Tenant
