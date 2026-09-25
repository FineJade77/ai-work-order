import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_bus, get_rate_limiter
from app.clients.agent_bus import AgentBus
from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, current_user
from app.schemas.conversations import (
    ConversationList,
    ConversationView,
    History,
    MessageView,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services import conversation_service, stream_service
from app.services.rate_limit import MessageRateLimiter

router = APIRouter(prefix="/conversations", tags=["conversations"])
User = Annotated[CurrentUser, Depends(current_user)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(user: User) -> ConversationView:
    return ConversationView.model_validate(await conversation_service.create(user))


@router.get("")
async def list_conversations(user: User, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> ConversationList:
    items = await conversation_service.list_mine(user, limit)
    return ConversationList(items=[ConversationView.model_validate(c) for c in items])


@router.get("/{conversation_id}/messages")
async def history(conversation_id: uuid.UUID, user: User) -> History:
    messages, active = await conversation_service.history(user, conversation_id)
    return History(messages=[MessageView.model_validate(m) for m in messages], active_task_id=active.id if active else None)


@router.post("/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    conversation_id: uuid.UUID,
    req: SendMessageRequest,
    user: User,
    bus: Annotated[AgentBus, Depends(get_bus)],
    limiter: Annotated[MessageRateLimiter, Depends(get_rate_limiter)],
) -> SendMessageResponse:
    await limiter.check(user.id)  # before anything is stored: a refused message leaves no trace
    task_id, message_id = await conversation_service.send_message(user, conversation_id, req.content, bus)
    return SendMessageResponse(task_id=task_id, message_id=message_id)


@router.get("/{conversation_id}/stream")
async def stream(
    conversation_id: uuid.UUID,
    task_id: uuid.UUID,
    user: User,
    bus: Annotated[AgentBus, Depends(get_bus)],
    settings: Annotated[Settings, Depends(get_settings)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", pattern=r"^\d+-\d+$")] = None,
) -> StreamingResponse:
    turn = await conversation_service.turn_for_stream(user, conversation_id, task_id)
    return StreamingResponse(
        stream_service.relay(bus, turn, last_event_id, settings.sse_max_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
