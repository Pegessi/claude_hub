#!/usr/bin/env python3
"""Isolated Claude Hub backend for Task Graph E2E. Does not touch ~/.claude_hub."""

from __future__ import annotations

import os
import pathlib

_E2E_HOME = pathlib.Path(os.environ["CLAUDE_HUB_E2E_HOME"]).resolve()
_E2E_HOME.mkdir(parents=True, exist_ok=True)
pathlib.Path.home = classmethod(lambda cls: _E2E_HOME)  # type: ignore[method-assign, assignment]

from claude_hub.config import settings  # noqa: E402

settings.port = int(os.environ.get("CLAUDE_HUB_E2E_PORT", "19174"))
settings.ttyd_base_port = int(os.environ.get("CLAUDE_HUB_E2E_TTYD_BASE", "19200"))
settings.host = "127.0.0.1"

import json  # noqa: E402
import sys  # noqa: E402

import claude_hub.services.ttyd_manager  # noqa: E402,F401

ttyd_mod = sys.modules["claude_hub.services.ttyd_manager"]
_overlay_json = os.environ.get("CLAUDE_HUB_E2E_LAUNCH_ENV_JSON", "")
_overlay_env = json.loads(_overlay_json) if _overlay_json else {}
if _overlay_env:
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV.clear()
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV.update(_overlay_env)

from claude_hub.main import app  # noqa: E402

if _overlay_env:
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV.clear()
    ttyd_mod.DEFAULT_CLAUDE_LAUNCH_ENV.update(_overlay_env)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
