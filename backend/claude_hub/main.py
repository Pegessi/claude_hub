import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response

from .api import api_router
from .config import settings
from .services import ttyd_manager, workspace_manager
from .services.backend_instance_lock import BackendInstanceLock

# Create logs directory if it doesn't exist
log_dir = Path.home() / ".claude_hub" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "backend.log"
backend_lock_file = Path.home() / ".claude_hub" / "backend.lock"

# Configure logging to both console and file
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Remove any existing handlers
logger.handlers.clear()

# Format for logs
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File handler
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Get our logger
logger = logging.getLogger(__name__)
logger.info(f"Logging to file: {log_file}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan - manage startup and shutdown."""
    with BackendInstanceLock(backend_lock_file):
        # Startup
        logger.info("Starting Claude Hub Backend")
        # Start all saved tabs
        await ttyd_manager.start_all_tabs()
        workspace_manager.start_background_monitor()
        try:
            yield
        finally:
            # Shutdown
            logger.info("Shutting down Claude Hub Backend")
            await workspace_manager.stop_background_monitor()
            await ttyd_manager.cleanup()


app = FastAPI(
    title="Claude Hub API",
    description="Web-based persistent Claude terminal service",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip compression. The workspace board and other large JSON responses are
# highly repetitive and compress ~10x, which is the dominant cost when the
# frontend loads/polls over LAN or mobile. Added after CORS but before the
# CoopCoep middleware so that (later-added middleware runs outermost in
# Starlette) the effective response order is CoopCoep -> GZip -> CORS -> app:
# COOP/COEP headers are still applied over the compressed body, and only HTTP
# responses are touched (the WebSocket/ttyd upgrade scope is passed through).
app.add_middleware(GZipMiddleware, minimum_size=1024)


class CoopCoepMiddleware(BaseHTTPMiddleware):
    """Emit COOP + COEP headers on every response.

    These headers are required for ``SharedArrayBuffer`` to be available to
    cross-origin-isolated browsing contexts (which includes our terminal
    iframes and the main app). Without these headers, the fast SAB + Atomics
    input ring buffer falls back to structured-clone ``postMessage``, which
    adds 10–30 ms of latency per keystroke.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        # Allow iframed ttyd (proxied from the same origin) to be loaded.
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response


app.add_middleware(CoopCoepMiddleware)

# Include API routes
app.include_router(api_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "message": "Claude Hub API",
        "version": "0.1.0",
        "docs": "/docs",
    }
