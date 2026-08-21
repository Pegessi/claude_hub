#!/usr/bin/env python3
"""Isolated Claude Hub backend for Agent Tree E2E. Does not touch ~/.claude_hub."""

from __future__ import annotations

import os
import pathlib

_E2E_HOME = pathlib.Path(os.environ["CLAUDE_HUB_E2E_HOME"]).resolve()
_E2E_HOME.mkdir(parents=True, exist_ok=True)
pathlib.Path.home = classmethod(lambda cls: _E2E_HOME)  # type: ignore[method-assign, assignment]

from claude_hub.config import settings  # noqa: E402

settings.port = int(os.environ.get("CLAUDE_HUB_E2E_PORT", "19173"))
settings.ttyd_base_port = int(os.environ.get("CLAUDE_HUB_E2E_TTYD_BASE", "19100"))
settings.host = "127.0.0.1"

import json  # noqa: E402

import claude_hub.services.ttyd_manager as ttyd_mod  # noqa: E402

ttyd_mod.TMUX_SESSION_PREFIX = os.environ.get("CLAUDE_HUB_E2E_TMUX_PREFIX", "claude-hub-e2e-")
_overlay = _E2E_HOME / ".claude_hub" / "e2e_launch_env.json"
if _overlay.exists():
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV = json.loads(_overlay.read_text())

from claude_hub.main import app  # noqa: E402

if _overlay.exists():
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV.clear()
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV.update(json.loads(_overlay.read_text()))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="info")
