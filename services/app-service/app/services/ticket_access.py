import uuid

from app.clients.ticket_service import TicketServiceClient
from app.core.errors import ApiError
from app.core.security import CurrentUser

IT_ACTIONS = frozenset({"start", "resolve"})


class TicketAccess:
    """Who may see and act on which ticket. ticket-service enforces the lifecycle and that only
    the requester may cancel/close/reopen; roles live here, in app-service."""

    def __init__(self, client: TicketServiceClient):
        self._client = client

    async def list(
        self,
        user: CurrentUser,
        *,
        status: str | None,
        assignee_id: uuid.UUID | None,
        requester_id: uuid.UUID | None,
        limit: int,
        cursor: str | None,
    ) -> dict:
        if not user.is_it_staff:
            requester_id, assignee_id = user.id, None  # employees only ever see their own tickets
        return await self._client.list(
            user.tenant_id, status=status, assignee_id=assignee_id, requester_id=requester_id, limit=limit, cursor=cursor
        )

    async def get(self, user: CurrentUser, ticket_id: uuid.UUID) -> dict:
        detail = await self._client.get(user.tenant_id, ticket_id)
        if not user.is_it_staff and detail["ticket"]["requester_id"] != str(user.id):
            # Same answer as a ticket that doesn't exist: don't reveal other employees' tickets.
            raise ApiError(404, "TICKET_NOT_FOUND", "ticket not found")
        return detail

    async def transition(
        self, user: CurrentUser, ticket_id: uuid.UUID, *, action: str, expected_version: int, note: str | None
    ) -> dict:
        if action in IT_ACTIONS and not user.is_it_staff:
            raise ApiError(403, "NOT_IT_STAFF", f"only IT staff may {action} a ticket")
        if not user.is_it_staff:
            await self.get(user, ticket_id)
        body = {"action": action, "actor_id": user.id, "expected_version": expected_version, "note": note}
        if action == "start":
            body["assignee_id"] = user.id  # starting a ticket means taking it on yourself
        return await self._client.transition(user.tenant_id, ticket_id, body)
