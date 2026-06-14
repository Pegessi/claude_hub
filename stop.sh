#!/bin/bash

# Claude Hub Stop Script
# Stops all Claude Hub services and cleans up orphaned background processes.

echo "Stopping Claude Hub services..."

# Backend port (override with CLAUDE_HUB_PORT if started on a non-default port).
BACKEND_PORT="${CLAUDE_HUB_PORT:-8173}"

# --- Backend (uvicorn) -----------------------------------------------------
# With `--reload` the backend is a 3-process tree:
#   1. `uv run uvicorn ...`            (launcher)
#   2. uvicorn reload supervisor
#   3. multiprocessing-spawned worker  (the process that actually binds the port)
# The worker's command line is `python -c from multiprocessing.spawn import
# spawn_main; ...` — it contains NO "uvicorn" token, so a single pattern pkill
# kills the launcher + supervisor but leaves the worker holding the port. That
# is why a naive restart appears to "not take effect". So: pattern-kill the
# tree, then reap whatever is still LISTENing on the port.
pkill -f "uvicorn claude_hub.main:app" 2>/dev/null || true

# Give the supervisor a moment to tear down its worker gracefully, then force
# anything still bound to the backend port (the spawned worker, or a stale
# process from a previous run).
sleep 1
port_pids=$(lsof -nP -tiTCP:"${BACKEND_PORT}" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "${port_pids}" ]; then
  kill ${port_pids} 2>/dev/null || true
  sleep 1
  still=$(lsof -nP -tiTCP:"${BACKEND_PORT}" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "${still}" ]; then
    kill -9 ${still} 2>/dev/null || true
  fi
fi
echo "✓ Backend stopped"

# --- Frontend (vite) -------------------------------------------------------
pkill -f "vite" 2>/dev/null || true
echo "✓ Frontend stopped"

# --- ttyd ------------------------------------------------------------------
# ttyd is spawned on dynamic ports across the 10xxx and 11xxx ranges. The old
# pattern "ttyd --port 100" only matched 100xx, leaving nearly every ttyd
# process alive. Match the launch pattern instead so all ports are covered.
pkill -f "ttyd --port" 2>/dev/null || true
echo "✓ ttyd processes cleaned up"

echo "All Claude Hub services stopped."
