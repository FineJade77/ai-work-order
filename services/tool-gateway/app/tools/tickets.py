from abc import ABC, abstractmethod
from typing import Any

from shared.db.models import Ticket
from shared.db.session import app_session_factory, tenant_scope
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.tools.base import ToolContext, ToolSpec

TICKET_CATEGORIES = ["vpn", "account", "email", "other"]


class TicketProvider(ABC):
    @abstractmethod
    def create(
        self, *, tenant_id: str, summary: str, category: str, conversation_ref: str, idempotency_key: str
    ) -> str: ...


class InternalTicketProvider(TicketProvider):
    """Writes to this project's own tickets table.

    A future provider (Jira/ServiceNow/etc, see architecture-design.md 3.4) implements the
    same interface; which one is active is a per-tenant configuration choice, not a code change.
    """

    def create(
        self, *, tenant_id: str, summary: str, category: str, conversation_ref: str, idempotency_key: str
    ) -> str:
        """Retrying with the same idempotency_key returns the original ticket_id.

        The gateway's tool_calls record covers ordinary retries. This covers the remaining
        window: a crash after the ticket row is written but before the gateway records
        success, after which the orphaned call is reclaimed and this runs a second time.
        """
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            stmt = (
                pg_insert(Ticket)
                .values(
                    tenant_id=tenant_id,
                    conversation_id=conversation_ref,
                    category=category,
                    summary=summary,
                    status="open",
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "idempotency_key"],
                    index_where=Ticket.idempotency_key.is_not(None),
                )
                .returning(Ticket.id)
            )
            # Stay in this one transaction until we have a final id: set_config(..., true)
            # is transaction-local, so committing before the fallback lookup below would
            # drop the tenant context RLS needs and the SELECT would see zero rows.
            inserted_id = session.execute(stmt).scalar_one_or_none()
            if inserted_id is not None:
                session.commit()
                return str(inserted_id)

            existing = (
                session.query(Ticket).filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key).one()
            )
            session.commit()
            return str(existing.id)


ticket_provider: TicketProvider = InternalTicketProvider()


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ticket_id = ticket_provider.create(
        tenant_id=ctx.tenant_id,
        summary=args["summary"],
        category=args["category"],
        conversation_ref=ctx.session_id,
        idempotency_key=ctx.idempotency_key,
    )
    return {"ticket_id": ticket_id}


CREATE_TICKET = ToolSpec(
    name="create_ticket",
    description="知识库方案无法解决员工问题时，为其创建一张 IT 工单。",
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "问题描述，包含已尝试过的知识库方案",
            },
            "category": {"type": "string", "enum": TICKET_CATEGORIES, "description": "问题类别"},
        },
        "required": ["summary", "category"],
        "additionalProperties": False,
    },
    handler=_handle,
    side_effect=True,
)
