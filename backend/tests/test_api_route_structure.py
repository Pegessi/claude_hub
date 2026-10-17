"""Structural contracts for the feature-oriented API split."""

import ast
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import claude_hub.api.feishu_bot as feishu_bot_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import User

_API_DIR = Path(__file__).resolve().parents[1] / "claude_hub" / "api"
_OWNED_PRODUCTION_FILES = (
    *_API_DIR.glob("_terminal_injection*.py"),
    _API_DIR / "agent_stream.py",
    _API_DIR / "feishu_bot.py",
    _API_DIR / "workspaces.py",
    *(_API_DIR / "routes" / "agent_stream").glob("*.py"),
    *(_API_DIR / "routes" / "feishu_bot").glob("*.py"),
    *(_API_DIR / "routes" / "workspaces").glob("*.py"),
    *(_API_DIR / "terminal_injection").glob("*.py"),
)


def test_owned_api_production_files_stay_within_line_limit() -> None:
    oversized = {
        path.relative_to(_API_DIR): len(path.read_text(encoding="utf-8").splitlines())
        for path in _OWNED_PRODUCTION_FILES
        if len(path.read_text(encoding="utf-8").splitlines()) > 1500
    }

    assert oversized == {}


def test_feature_modules_do_not_import_their_compatibility_facades() -> None:
    forbidden = {
        "agent_stream": "claude_hub.api.agent_stream",
        "feishu_bot": "claude_hub.api.feishu_bot",
        "workspaces": "claude_hub.api.workspaces",
    }
    violations = []
    for feature, facade in forbidden.items():
        for path in (_API_DIR / "routes" / feature).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == facade:
                    violations.append((path.relative_to(_API_DIR), node.lineno, facade))
                if isinstance(node, ast.Import) and any(
                    alias.name == facade for alias in node.names
                ):
                    violations.append((path.relative_to(_API_DIR), node.lineno, facade))

    assert violations == []


def test_feishu_routes_preserve_facade_auth_dependency_identity() -> None:
    app = FastAPI()
    app.include_router(feishu_bot_api.router)

    def reject_base_auth() -> User:
        raise HTTPException(status_code=418, detail="base auth must be bypassed")

    app.dependency_overrides[get_current_user] = reject_base_auth
    app.dependency_overrides[feishu_bot_api.require_hub_user] = lambda: User(
        open_id="override-user",
        name="Override",
        email="override@example.com",
        avatar_url=None,
    )

    response = TestClient(app).get("/api/feishu/bot/bots")

    assert response.status_code == 200
    assert "bots" in response.json()
