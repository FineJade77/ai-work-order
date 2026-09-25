import uuid

from app.clients.tool_gateway import ToolGatewayClient
from app.core.errors import ApiError
from app.core.security import CurrentUser


class ApprovalAccess:
    """Who may see and decide which approval. Handlers see the tenant's approvals and decide them;
    employees see only their own requests. tool-gateway enforces the rest: pending and not expired,
    and no approving your own request (four-eyes)."""

    def __init__(self, client: ToolGatewayClient):
        self._client = client

    async def list(self, user: CurrentUser, *, status: str | None, limit: int) -> dict:
        requester = None if user.is_handler else user.id
        return await self._client.list_approvals(user.tenant_id, status=status, requester_id=requester, limit=limit)

    async def get(self, user: CurrentUser, approval_id: uuid.UUID) -> dict:
        approval = await self._client.get_approval(user.tenant_id, approval_id)
        if not user.is_handler and approval["requester_id"] != str(user.id):
            # Same answer as an approval that doesn't exist: don't reveal other employees' requests.
            raise ApiError(404, "APPROVAL_NOT_FOUND", "approval not found")
        return approval

    async def decide(self, user: CurrentUser, approval_id: uuid.UUID, *, decision: str, note: str | None) -> dict:
        if not user.is_handler:
            raise ApiError(403, "NOT_HANDLER", "only a handler may decide an approval")
        return await self._client.decide(user.tenant_id, approval_id, decision=decision, approver_id=user.id, note=note)
