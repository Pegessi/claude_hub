from fastapi import APIRouter

from .agent_stream import router as agent_stream_router
from .auth import router as auth_router
from .clipboard import router as clipboard_router
from .codex import router as codex_router
from .env_presets import router as env_presets_router
from .filesystem import router as filesystem_router
from .remote import router as remote_router
from .system import router as system_router
from .tabs import router as tabs_router
from .terminal import router as terminal_router
from .workspaces import router as workspaces_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(tabs_router)
api_router.include_router(terminal_router)
api_router.include_router(filesystem_router)
api_router.include_router(remote_router)
api_router.include_router(system_router)
api_router.include_router(clipboard_router)
api_router.include_router(workspaces_router)
api_router.include_router(codex_router)
api_router.include_router(env_presets_router)
api_router.include_router(agent_stream_router)

__all__ = ["api_router"]
