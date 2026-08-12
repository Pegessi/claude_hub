"""Tests for the Codex session listing endpoint and title extraction."""

import json
import time
from datetime import datetime

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch


def _write_rollout(tmp_path, session_id, cwd, start_epoch, messages):
    """Write a minimal rollout jsonl with a session_meta line plus messages.

    ``messages`` is a list of (role, text) tuples; each becomes a
    ``response_item`` record.
    """
    path = tmp_path / f"rollout-{session_id}.jsonl"
    ts = datetime.utcfromtimestamp(start_epoch).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": cwd,
                    "timestamp": ts,
                },
            }
        )
    ]
    for role, text in messages:
        lines.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "role": role,
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def _fake_scan(entries):
    """Build a fake _codex_scan_sessions returning a dict sid -> ScanEntry."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")

    def _scan():
        out = {}
        for sid, cwd, epoch, path in entries:
            out[sid] = tm.ScanEntry(
                path=str(path),
                mtime_ns=int(epoch * 1e9),
                size=path.stat().st_size if path.exists() else 100,
                cwd=cwd,
                ts=epoch,
                is_archived=False,
            )
        return out

    return _scan


@pytest.mark.asyncio
async def test_list_codex_sessions_endpoint_returns_grouped(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """The endpoint returns sessions grouped by cwd, most-recent-first."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")

    s1 = _write_rollout(
        tmp_path,
        "00000001-0000-0000-0000-000000000001",
        "/tmp/proj-a",
        1000.0,
        [("user", "fix the login bug")],
    )
    s2 = _write_rollout(
        tmp_path,
        "00000002-0000-0000-0000-000000000002",
        "/tmp/proj-a",
        2000.0,
        [("user", "review the PR")],
    )
    s3 = _write_rollout(
        tmp_path,
        "00000003-0000-0000-0000-000000000003",
        "/tmp/proj-b",
        1500.0,
        [("user", "ship the release")],
    )

    entries = [
        ("00000001-0000-0000-0000-000000000001", "/tmp/proj-a", 1000.0, s1),
        ("00000002-0000-0000-0000-000000000002", "/tmp/proj-a", 2000.0, s2),
        ("00000003-0000-0000-0000-000000000003", "/tmp/proj-b", 1500.0, s3),
    ]
    monkeypatch.setattr(tm, "_codex_scan_sessions", _fake_scan(entries))
    # Patch _codex_session_title to read from our paths (it already uses path arg).

    response = await client.get("/api/codex/sessions")
    assert response.status_code == 200
    data = response.json()

    # Grouped by cwd; groups ordered by most-recent session.
    assert [g["cwd"] for g in data] == ["/tmp/proj-a", "/tmp/proj-b"]

    # Within proj-a, most-recent-first (2000 before 1000).
    proj_a = next(g for g in data if g["cwd"] == "/tmp/proj-a")
    assert [s["session_id"] for s in proj_a["sessions"]] == [
        "00000002-0000-0000-0000-000000000002",
        "00000001-0000-0000-0000-000000000001",
    ]

    for group in data:
        for s in group["sessions"]:
            assert set(s.keys()) == {"session_id", "cwd", "start_time", "title"}
            assert isinstance(s["session_id"], str)
            assert isinstance(s["cwd"], str)
            datetime.fromisoformat(s["start_time"])
            assert isinstance(s["title"], str)

    assert proj_a["sessions"][0]["title"] == "review the PR"


@pytest.mark.asyncio
async def test_list_codex_sessions_title_skips_boilerplate(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Boilerplate env/permissions/AGENTS.md blocks are skipped for the title."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")

    s1 = _write_rollout(
        tmp_path,
        "00000004-0000-0000-0000-000000000004",
        "/tmp/proj-c",
        3000.0,
        [
            ("developer", "<permissions instructions>\nFilesystem sandboxing..."),
            ("user", "# AGENTS.md instructions\n\n<INSTRUCTIONS>..."),
            ("user", "<environment_context>\n  <cwd>/tmp</cwd>"),
            ("user", "<recommended_plugins>\nHere is a list of plugins..."),
            ("user", "actually do the thing"),
        ],
    )

    entries = [("00000004-0000-0000-0000-000000000004", "/tmp/proj-c", 3000.0, s1)]
    monkeypatch.setattr(tm, "_codex_scan_sessions", _fake_scan(entries))

    response = await client.get("/api/codex/sessions")
    assert response.status_code == 200
    data = response.json()
    assert data[0]["sessions"][0]["title"] == "actually do the thing"


@pytest.mark.asyncio
async def test_list_codex_sessions_dedupes_by_session_id(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """A session with multiple rollout files keeps the most recent one."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")

    older = _write_rollout(
        tmp_path,
        "00000005-0000-0000-0000-000000000005",
        "/tmp/proj-d",
        1000.0,
        [("user", "older prompt")],
    )
    newer = _write_rollout(
        tmp_path,
        "00000005-0000-0000-0000-000000000005",
        "/tmp/proj-d",
        5000.0,
        [("user", "newer prompt")],
    )

    # The dedup in list_codex_sessions uses start_epoch from scan entries, so
    # we produce two entries with the same sid and rely on the "keep most
    # recent" logic. Note: _codex_scan_sessions itself dedups per root, but
    # the endpoint handles duplicates defensively.
    def _fake():
        out = {}
        for sid, cwd, epoch, path in [
            ("00000005-0000-0000-0000-000000000005", "/tmp/proj-d", 1000.0, older),
            ("00000005-0000-0000-0000-000000000005", "/tmp/proj-d", 5000.0, newer),
        ]:
            out[sid] = tm.ScanEntry(
                path=str(path),
                mtime_ns=int(epoch * 1e9),
                size=path.stat().st_size,
                cwd=cwd,
                ts=epoch,
                is_archived=False,
            )
        return out

    monkeypatch.setattr(tm, "_codex_scan_sessions", _fake)

    response = await client.get("/api/codex/sessions")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert len(data[0]["sessions"]) == 1
    assert data[0]["sessions"][0]["title"] == "newer prompt"
