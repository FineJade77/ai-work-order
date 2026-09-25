from fastapi import APIRouter

from app.api.v1 import approvals, auth, conversations, tickets

api_router = APIRouter(prefix="/v1")
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(tickets.router)
api_router.include_router(approvals.router)
