#!/bin/bash

# Claude Hub Stop Script
# This script stops any running Claude Hub services

echo "Stopping Claude Hub services..."

# Stop uvicorn (backend)
pkill -f "uvicorn claude_hub.main:app" 2>/dev/null || true
echo "✓ Backend stopped"

# Stop vite (frontend)
pkill -f "vite" 2>/dev/null || true
echo "✓ Frontend stopped"

# Stop ttyd processes that might have been left behind
pkill -f "ttyd --port 100" 2>/dev/null || true
echo "✓ ttyd processes cleaned up"

echo "All Claude Hub services stopped."
