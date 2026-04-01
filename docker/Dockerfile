# Dockerfile for Claude Hub
FROM python:3.11-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
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

# Copy backend files
COPY backend/pyproject.toml backend/README.md ./
COPY backend/claude_hub ./claude_hub

# Install Python dependencies
RUN uv sync --no-dev --frozen

# Expose ports
EXPOSE 8000

# Run the application
CMD ["uv", "run", "uvicorn", "claude_hub.main:app", "--host", "0.0.0.0", "--port", "8000"]
