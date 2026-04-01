#!/bin/bash

# Claude Hub Startup Script
# This script starts both the backend and frontend services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get project root directory
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Starting Claude Hub${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check for dependencies
echo -e "${YELLOW}Checking dependencies...${NC}"

# Check for tmux
if ! command -v tmux &> /dev/null; then
    echo -e "${RED}Error: tmux is not installed${NC}"
    echo "Please install tmux (brew install tmux / apt install tmux)"
    exit 1
fi
echo -e "${GREEN}✓ tmux found${NC}"

# Check for ttyd
if ! command -v ttyd &> /dev/null; then
    echo -e "${RED}Error: ttyd is not installed${NC}"
    echo "Please install ttyd from https://github.com/tsl0922/ttyd"
    exit 1
fi
echo -e "${GREEN}✓ ttyd found${NC}"

# Check for tmux
if ! command -v tmux &> /dev/null; then
    echo -e "${RED}Error: tmux is not installed${NC}"
    echo "Please install tmux (e.g. brew install tmux)"
    exit 1
fi
echo -e "${GREEN}✓ tmux found${NC}"

# Check for uv
if ! command -v uv &> /dev/null; then
    echo -e "${RED}Error: uv is not installed${NC}"
    echo "Please install uv from https://docs.astral.sh/uv/getting-started/installation"
    exit 1
fi
echo -e "${GREEN}✓ uv found${NC}"

# Check for pnpm
if ! command -v pnpm &> /dev/null; then
    echo -e "${RED}Error: pnpm is not installed${NC}"
    echo "Please install pnpm from https://pnpm.io/"
    exit 1
fi
echo -e "${GREEN}✓ pnpm found${NC}"
echo ""

# Install backend dependencies if needed
echo -e "${YELLOW}Setting up backend...${NC}"
cd "$PROJECT_ROOT/backend"
if [ ! -d ".venv" ]; then
    echo "Installing backend dependencies..."
    uv sync --dev
fi
echo -e "${GREEN}✓ backend ready${NC}"
echo ""

# Install frontend dependencies if needed
echo -e "${YELLOW}Setting up frontend...${NC}"
cd "$PROJECT_ROOT/frontend"
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    pnpm install
fi
echo -e "${GREEN}✓ frontend ready${NC}"
echo ""

# Cleanup function to kill background processes
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down services...${NC}"
    if [ -n "$BACKEND_PID" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "$FRONTEND_PID" ]; then
        kill "$FRONTEND_PID" 2>/dev/null || true
    fi
    echo -e "${GREEN}✓ All services stopped${NC}"
    exit 0
}

# Trap SIGINT and SIGTERM
trap cleanup SIGINT SIGTERM

# Start backend
echo -e "${YELLOW}Starting backend server...${NC}"
cd "$PROJECT_ROOT/backend"
uv run uvicorn claude_hub.main:app --reload &
BACKEND_PID=$!
echo -e "${GREEN}✓ Backend started on http://localhost:8000 (PID: $BACKEND_PID)${NC}"

# Wait a bit for backend to start
sleep 2

# Start frontend
echo -e "${YELLOW}Starting frontend server...${NC}"
cd "$PROJECT_ROOT/frontend"
pnpm dev &
FRONTEND_PID=$!
echo -e "${GREEN}✓ Frontend started on http://localhost:5173 (PID: $FRONTEND_PID)${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Claude Hub is now running!${NC}"
echo -e "${GREEN}  Frontend: http://localhost:5173${NC}"
echo -e "${GREEN}  Backend:  http://localhost:8000${NC}"
echo -e "${GREEN}  API Docs: http://localhost:8000/docs${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop all services${NC}"
echo ""

# Wait for background processes
wait
