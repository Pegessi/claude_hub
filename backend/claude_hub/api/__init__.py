from fastapi import APIRouter

from .auth import router as auth_router
from .filesystem import router as filesystem_router
from .tabs import router as tabs_router
from .terminal import router as terminal_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(tabs_router)
api_router.include_router(terminal_router)
api_router.include_router(filesystem_router)

__all__ = ["api_router"]
