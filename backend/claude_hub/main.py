from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import sys
from pathlib import Path

from .config import settings
from .api import api_router
from .services import ttyd_manager

# Create logs directory if it doesn't exist
log_dir = Path.home() / ".claude_hub" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "backend.log"

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
async def lifespan(app: FastAPI):
    """Application lifespan - manage startup and shutdown."""
    # Startup
    logger.info("Starting Claude Hub Backend")
    # Start all saved tabs
    await ttyd_manager.start_all_tabs()
    yield
    # Shutdown
    logger.info("Shutting down Claude Hub Backend")
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

# Include API routes
app.include_router(api_router)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Claude Hub API",
        "version": "0.1.0",
        "docs": "/docs",
    }
