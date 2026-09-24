import uuid

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    tenant: str = Field(min_length=1, max_length=100, description="tenant slug")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class UserView(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserView


class ProfileView(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: str
    tenant_id: uuid.UUID
    tenant_name: str
