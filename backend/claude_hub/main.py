import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import Scope

from .api import api_router
from .auth.proxy import unsafe_http_request_is_allowed
from .config import settings
from .services import ttyd_manager, workspace_manager
from .services.backend_instance_lock import BackendInstanceLock
from .services.runtime_isolation import resolve_runtime_home
from .services.service_restart import INSTANCE_ID

# Worktree backends use an isolated runtime home so they do not share the
# live instance lock, logs, or tabs.json with the 8173 main service.
_RUNTIME_HOME = resolve_runtime_home()
log_dir = _RUNTIME_HOME / "logs"
log_file = log_dir / "backend.log"
backend_lock_file = _RUNTIME_HOME / "backend.lock"

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

# Get our logger
logger = logging.getLogger(__name__)


@contextmanager
def backend_file_logging(
    path: Path, *, max_bytes: int, backup_count: int
) -> Iterator[RotatingFileHandler]:
    """Attach the bounded file log while this process owns the runtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        yield handler
    finally:
        root_logger.removeHandler(handler)
        handler.close()


@asynccontextmanager
async def _backend_runtime_ownership() -> AsyncIterator[None]:
    """Drain submitted cleanup work before releasing single-instance ownership."""
    # A rejected duplicate never opens the active owner's log. Cleanup also
    # drains when startup fails before the lifespan reaches its yield.
    with (
        BackendInstanceLock(backend_lock_file),
        backend_file_logging(
            log_file,
            max_bytes=settings.backend_log_max_bytes,
            backup_count=settings.backend_log_backup_count,
        ),
    ):
        async with ttyd_manager.cleanup_task_lifetime():
            yield


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Defense in depth for direct `uvicorn` invocations (the supervised
    # launcher already enforces this before spawning us). Uvicorn runs
    # lifespan startup before binding its listening sockets, so raising here
    # prevents the port from ever being served.
    from .services.listen_guard import InsecureListenError, enforce_settings_listen_policy

    try:
        enforce_settings_listen_policy()
    except InsecureListenError:
        logger.error("Unsafe listen configuration: startup aborted")
        raise

    async with _backend_runtime_ownership():
        logger.info("Logging to file: %s", log_file)
        # Startup
        logger.info("Starting Claude Hub Backend")
        from .api.agent_stream import _reset_tailer_manager

        _reset_tailer_manager()
        # Schedule authorized cleanup before providers start; durable reserved
        # ids let unrelated providers proceed while a slow disk job is pending.
        ttyd_manager.schedule_pending_stream_cleanup()
        try:
            from .services.feishu_bot_websocket import prewarm_lark_sdk

            prewarm_lark_sdk()
        except Exception:
            logger.exception("Feishu Bot SDK prewarm failed to start")
        try:
            from .services.goal_run import get_goal_manager

            recovered = get_goal_manager().recover()
            if recovered:
                logger.warning("paused %d uncertain Chat Goal dispatches", len(recovered))
        except Exception:
            logger.exception("Chat Goal recovery failed at startup")

        # Run cache GC and prior-process temp cleanup *before* starting any
        # tabs or the workspace monitor. BackendInstanceLock guarantees we
        # are the sole owner of this runtime, so no concurrent turn can be
        # staging files while we clean up. Running cleanup after
        # start_all_tabs / start_background_monitor would race background
        # activity against the cleanup pass.
        #
        # Preview cache GC: enforce quotas, age TTL, and clean orphans from
        # a crashed process. Failures are non-fatal and are retried on the
        # next isolated-backend startup.
        try:
            from .services.agent_stream.attachments import run_global_gc

            await run_global_gc()
        except Exception:
            logger.exception("agent-stream attachment GC failed at startup")
        # Remove any Codex image temp files left by a prior crashed process.
        # BackendInstanceLock guarantees single ownership, so we can remove
        # all of them before any tab can stage files for a new turn.
        try:
            from .services.agent_stream.native import cleanup_codex_temp_dir

            removed = cleanup_codex_temp_dir()
            if removed:
                logger.info("cleaned up %d orphaned Codex image temp files", removed)
        except Exception:
            logger.exception("Codex image temp cleanup failed at startup")

        # Remove any Cursor image temp files left by a prior crashed process.
        # Cursor stages attached images to temp files referenced by path in
        # the prompt; same single-ownership guarantee as Codex above.
        try:
            from .services.agent_stream.native import cleanup_cursor_temp_dir

            removed = cleanup_cursor_temp_dir()
            if removed:
                logger.info("cleaned up %d orphaned Cursor image temp files", removed)
        except Exception:
            logger.exception("Cursor image temp cleanup failed at startup")

        # Start all saved tabs
        await ttyd_manager.start_all_tabs()
        try:
            from .services.subagents import get_subagent_service

            # Before restart continuation: closing an interrupted non-kept
            # subagent archives its tab and vetoes that tab's continuation.
            await get_subagent_service().start()
        except Exception:
            logger.exception("Subagent service failed to start")
        from .services.restart_continuation import (
            capture_interrupted_chats,
            continue_interrupted_chats,
        )

        continuation_task = asyncio.create_task(continue_interrupted_chats())
        from .api.task_notifications import deliver_task_notice

        # Task notices (questions, acceptance, owner loss, plans) go out from the
        # monitor; Feishu routing lives in the API layer.
        workspace_manager.configure_task_notifier(deliver_task_notice)
        workspace_manager.start_background_monitor()
        from .api.routes.agent_stream.messages import start_inbox

        try:
            await start_inbox()
        except Exception:
            # Corrupt delivery evidence disables the inbox, not unrelated Chats.
            logger.exception("Chat message inbox unavailable; refusing inbox delivery")
        try:
            from .services.dot import get_dot_service

            get_dot_service().start()
        except Exception:
            logger.exception("Dot work watcher failed to start")
        try:
            from .api.feishu_bot import start_feishu_websockets

            await start_feishu_websockets()
        except Exception:
            logger.exception("Feishu Bot WebSocket startup failed")
        try:
            yield
        finally:
            # Shutdown
            logger.info("Shutting down Claude Hub Backend")
            from .api.agent_stream import _quiesce_tailer_managers

            _quiesce_tailer_managers()
            from .api.routes.agent_stream.messages import stop_inbox

            try:
                await stop_inbox()
            except Exception:
                logger.exception("Chat inbox shutdown failed")
            try:
                from .services.subagents import get_subagent_service

                await get_subagent_service().stop()
            except Exception:
                logger.exception("Subagent shutdown failed")
            continuation_task.cancel()
            await asyncio.gather(continuation_task, return_exceptions=True)
            try:
                try:
                    from .api.feishu_bot import quiesce_feishu_websockets

                    await quiesce_feishu_websockets()
                except Exception:
                    logger.exception("Failed to quiesce Feishu Bot WebSockets")
                try:
                    from .services.dot import get_dot_service

                    await get_dot_service().stop()
                except Exception:
                    logger.exception("Failed to stop the Dot work watcher")
                # Stop new scheduled dispatch before draining its transports.
                try:
                    await workspace_manager.stop_background_monitor()
                except Exception:
                    logger.exception("Failed to stop the workspace background monitor")
                try:
                    await capture_interrupted_chats()
                except Exception:
                    logger.exception("Could not capture restart Chat continuations")
            finally:
                try:
                    from .api.agent_stream import _stop_all_tailer_managers

                    await _stop_all_tailer_managers()
                except Exception:
                    logger.exception("Failed to stop agent-stream tailers")
                finally:
                    try:
                        await ttyd_manager.cleanup()
                    finally:
                        try:
                            from .api.feishu_bot import stop_feishu_websockets

                            await stop_feishu_websockets(drain_timeout_seconds=0)
                        except Exception:
                            logger.exception("Failed to stop Feishu Bot WebSockets")


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


class BrowserOriginGuardMiddleware:
    """Reject cross-site browser state-changing requests before routing.

    CORS only stops a malicious page from *reading* cross-origin responses;
    "simple" POSTs (form/``text/plain`` bodies) are still delivered and would
    execute against an unauthenticated local/LAN Hub. This pure-ASGI
    middleware applies the same Origin policy as the terminal WebSocket
    guard: safe methods and Origin-less native clients (the CLI, curl,
    server-to-server webhooks) pass; unsafe methods with a browser Origin
    must be same-origin or listed in ``CLAUDE_HUB_ALLOWED_ORIGINS``. Added
    last, so it is the outermost layer and rejects before any work happens.
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] == "http" and not unsafe_http_request_is_allowed(
            method=scope.get("method", ""),
            headers=scope.get("headers", []),
        ):
            origin = next(
                (
                    value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                    if key == b"origin"
                ),
                None,
            )
            host = next(
                (
                    value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                    if key == b"host"
                ),
                None,
            )
            logger.warning(
                "Rejecting cross-origin %s with Origin %r targeting Host %r",
                scope.get("method"),
                origin,
                host,
            )
            response = JSONResponse(
                {"detail": "Cross-origin request rejected"},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(BrowserOriginGuardMiddleware)

# Include API routes
app.include_router(api_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "instance_id": INSTANCE_ID}


# Production frontend serving. When ``serve_frontend`` is enabled (start.sh
# exports ``SERVE_FRONTEND=true``) and a built ``frontend/dist`` exists, FastAPI
# serves the SPA at the same origin as the API — no separate vite dev server,
# no HMR WebSocket (which force-reloads background tabs), no CORS/proxy. The app
# has no vue-router, so static serving handles index.html at "/" and leaves
# unknown paths as 404s except for the HTML-reader compatibility redirect below.
# The API/health/docs routes above are registered first and win route-ordering.
# COOP/COEP headers are added by the middleware above to
# every response, static assets included. When the build is absent (dev/CI),
# fall back to the JSON root so backend tests asserting on it stay green.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _html_reader_redirect(scope: Scope) -> Response | None:
    original = scope["path"]
    if (
        scope["method"] not in {"GET", "HEAD"}
        or original.startswith(("/api/", "/assets/", "/static/", "//"))
        or "\\" in original
        or any(ord(char) < 32 or ord(char) == 127 for char in original)
        or len(original) > 4096
        or ".." in Path(original).parts
        or Path(original).suffix.lower() not in {".html", ".htm"}
    ):
        return None
    # This is only a public shell redirect. The reader's JSON endpoint still
    # authenticates and checks an exact live Task artifact grant.
    query = urlencode({"attachment": original, "registered": "1"})
    return RedirectResponse(f"/?{query}", status_code=307, headers={"Cache-Control": "no-store"})


class FrontendStaticFiles(StaticFiles):
    """Keep static handling, with a reader redirect for missing HTML file links."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                redirect = _html_reader_redirect(scope)
                if redirect is not None:
                    return redirect
            raise
        if response.status_code == 404:
            return _html_reader_redirect(scope) or response
        return response


def register_frontend(fastapi_app: FastAPI, *, serve: bool, dist: Path) -> None:
    """Mount the built SPA on ``fastapi_app`` when enabled and present.

    Falls back to a JSON root endpoint otherwise (dev/CI without a build).
    """
    if serve and dist.is_dir():
        fastapi_app.mount("/", FrontendStaticFiles(directory=dist, html=True), name="frontend")
        return

    @fastapi_app.get("/")
    async def root() -> dict[str, str]:
        """Root endpoint."""
        return {
            "message": "Claude Hub API",
            "version": "0.1.0",
            "docs": "/docs",
        }


register_frontend(app, serve=settings.serve_frontend, dist=FRONTEND_DIST)
