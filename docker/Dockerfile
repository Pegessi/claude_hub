# Dockerfile for Claude Hub
FROM python:3.11-slim

# Install runtime and build dependencies.
# - tmux is a HARD runtime requirement: terminal tabs are tmux sessions.
# - curl is used to install ttyd and for container healthchecks.
# - git / python3 / build-essential make the container useful for running
#   agents (cloning repos, compiling wheels, invoking system Python).
RUN apt-get update && apt-get install -y \
    curl \
    tmux \
    git \
    python3 \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install ttyd
RUN curl -L https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 -o /usr/local/bin/ttyd && \
    chmod +x /usr/local/bin/ttyd

# Install uv
ADD --chmod=755 https://astral.sh/uv/install.sh /uv-install.sh
RUN sh /uv-install.sh && rm /uv-install.sh
ENV PATH="/root/.cargo/bin:${PATH}"

# Set working directory
WORKDIR /app

# Copy manifest + lockfile first for optimal layer caching (dep layer
# invalidates only when pyproject/README/uv.lock change, not on source changes).
COPY backend/pyproject.toml backend/README.md ./
COPY backend/uv.lock ./

# Install Python dependencies
RUN uv sync --no-dev --frozen

# Source code last: invalidates every commit but the dep layer is reused.
COPY backend/claude_hub ./claude_hub

# Backend HTTP + WebSocket API port (FastAPI/uvicorn).
EXPOSE 8173

# Run the application
CMD ["uv", "run", "uvicorn", "claude_hub.main:app", "--host", "0.0.0.0", "--port", "8173"]
