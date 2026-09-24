from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, current_user
from app.schemas.auth import LoginRequest, LoginResponse, ProfileView, UserView
from app.services import auth_service

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
async def login(req: LoginRequest, settings: Annotated[Settings, Depends(get_settings)]) -> LoginResponse:
    token, user = await auth_service.login(req.tenant, req.email, req.password, settings)
    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_ttl_seconds,
        user=UserView(id=user.id, tenant_id=user.tenant_id, role=user.role),
    )


@router.get("/me")
async def me(user: Annotated[CurrentUser, Depends(current_user)]) -> ProfileView:
    profile, tenant = await auth_service.profile(user)
    return ProfileView(
        id=profile.id, name=profile.name, email=profile.email, role=profile.role,
        tenant_id=tenant.id, tenant_name=tenant.name,
    )
