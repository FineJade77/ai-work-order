from fastapi import APIRouter

from app.api.v1 import tools

api_router = APIRouter(prefix="/v1")
api_router.include_router(tools.router)
