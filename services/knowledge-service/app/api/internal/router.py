from fastapi import APIRouter

from app.api.internal import knowledge

# Service-to-service routes. The API Gateway must never route external traffic here.
internal_router = APIRouter(prefix="/internal/v1")
internal_router.include_router(knowledge.router)
