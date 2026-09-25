import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class ModelCallLog(Base):
    __tablename__ = "model_call_logs"
    __table_args__ = (CheckConstraint("status IN ('ok','error')", name="ck_model_call_logs_status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # No FK: tenants live in app-service's database.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(nullable=False)
    completion_tokens: Mapped[int] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantTokenBudget(Base):
    """A tenant's own daily token budget, overriding Settings.daily_token_budget. V1 has no admin
    API: rows are maintained with SQL as the owner account."""

    __tablename__ = "tenant_token_budgets"
    __table_args__ = (CheckConstraint("daily_tokens >= 0", name="ck_tenant_token_budgets_nonnegative"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    daily_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
