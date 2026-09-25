from fastapi import Request

from app.clients.agent_bus import AgentBus
from app.services.rate_limit import MessageRateLimiter
from app.services.ticket_access import TicketAccess

# Built once in the lifespan; resolved per request so tests can override them.


def get_bus(request: Request) -> AgentBus:
    return request.app.state.bus


def get_ticket_access(request: Request) -> TicketAccess:
    return request.app.state.ticket_access


def get_rate_limiter(request: Request) -> MessageRateLimiter:
    return request.app.state.rate_limiter
