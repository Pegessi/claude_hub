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


# ---------------------------------------------------------------------------
# Workspace-manager prompt handling
# ---------------------------------------------------------------------------


TASK_ASSIGNMENT = (
    "New workspace task assigned.\n\n"
    "Workspace: Claude Hub\n"
    "Task ID: abc-123\n"
    "Task title: codex session选择\n"
    "Task mode: reviewed\n"
    "Task execution complexity: auto\n\n"
    "Task description:\nFix the thing.\n"
)

REVIEW_PROMPT = (
    "Review workspace task.\n\n"
    "Workspace: Claude Hub; Task ID: abc-123\n"
    "Task title: codex session选择\n"
    "Task mode: reviewed\n"
)

CONTINUE_PROMPT = (
    "Continue workspace task from review.\n\n"
    "Task ID: abc-123\n"
    "Task title: codex session选择\n"
    "Follow-up instructions:\nGoal Packet approved.\n"
)

HARD_RECOVERY_WORKER = (
    "⚠️  Your previous context was automatically cleared because the agent encountered a persistent "
    "API error and could not continue. A fresh context has been started for you within the SAME "
    "conversation (session_id preserved).\n\n"
    "Error detected: some error\n\n"
    "Workspace: Claude Hub\n"
    "Task ID: abc-123\n"
    "Task title: codex session选择\n"
    "Task mode: reviewed\n\n"
    "Task description:\nFix the thing.\n"
)

REVISION_RESUME = (
    "⚠️  Context refreshed after error. A fresh context has been started within the same "
    "conversation; prior turns are no longer visible. You are resuming an in-flight task "
    "at a revision step -- do NOT restart from scratch.\n\n"
    "Error: some error\n"
    "Workspace: Claude Hub\n"
    "Task: abc-123 (codex session选择)  mode=reviewed  complexity=auto  iteration=2\n"
    "Task description:\nFix the thing.\n"
)

BOOTSTRAP_AGENT = (
    "You are a resident workspace agent.\n\n"
    "Workspace: Claude Hub\n"
    "Session: cb-agent-1\n"
    "Wait in this terminal for assigned tasks; do not start unrelated work.\n"
)

BOOTSTRAP_REVIEWER = (
    "You are an independent reviewer agent for this workspace. Wait for explicit review "
    "assignments. Stay read-only: do not implement, refactor, format, or edit files.\n\n"
    "Workspace: Claude Hub\n"
    "Session: cb-reviewer-1\n"
)

BOOTSTRAP_DISPATCHER = (
    "You are the dispatcher agent for this workspace.\n\n"
    "Workspace: Claude Hub\n"
    "Session: cb-dispatcher\n"
)

BOOTSTRAP_RESIDENT = (
    "You are this workspace's RESIDENT self-driven maintenance agent. You wake up "
    "periodically to keep the workspace healthy.\n"
)

DISPATCH_PROMPT = (
    "Dispatch decision needed.\n\n"
    "Workspace: Claude Hub\n"
    "Task ID: abc-123\n"
    "Task title: codex session选择\n"
    "Task execution complexity: auto\n"
)


@pytest.mark.asyncio
async def test_codex_title_extracts_task_title_from_assignment_prompt(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Task assignment prompts show the embedded Task title, not the first line."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000010",
        "/tmp/ws",
        1000.0,
        [("user", TASK_ASSIGNMENT)],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000010", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.status_code == 200
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_extracts_task_title_from_review_prompt(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Review prompts show the embedded Task title."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000011",
        "/tmp/ws",
        1000.0,
        [("user", REVIEW_PROMPT)],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000011", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_extracts_task_title_from_continue_prompt(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Continue prompts show the embedded Task title."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000012",
        "/tmp/ws",
        1000.0,
        [("user", CONTINUE_PROMPT)],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000012", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_extracts_task_title_from_hard_recovery(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Hard-recovery prompts extract Task title from inside the warning block."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000013",
        "/tmp/ws",
        1000.0,
        [("user", HARD_RECOVERY_WORKER)],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000013", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_extracts_inline_task_from_revision_resume(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Revision-resume uses inline ``Task: id (title)`` format."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000014",
        "/tmp/ws",
        1000.0,
        [("user", REVISION_RESUME)],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000014", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_extracts_task_title_from_dispatch_prompt(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Dispatch-decision prompts show the embedded Task title."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000015",
        "/tmp/ws",
        1000.0,
        [("user", DISPATCH_PROMPT)],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000015", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_idle_bootstrap_shows_role_label(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Idle sessions (only bootstrap message, no task) get a role label."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    cases = [
        ("10000000-0000-0000-0000-000000000020", BOOTSTRAP_AGENT, "Agent (Claude Hub)"),
        ("10000000-0000-0000-0000-000000000021", BOOTSTRAP_REVIEWER, "Reviewer (Claude Hub)"),
        ("10000000-0000-0000-0000-000000000022", BOOTSTRAP_DISPATCHER, "Dispatcher (Claude Hub)"),
        ("10000000-0000-0000-0000-000000000023", BOOTSTRAP_RESIDENT, "Resident"),
    ]
    entries = []
    for sid, msg, _ in cases:
        p = _write_rollout(tmp_path, sid, "/tmp/ws", 1000.0, [("user", msg)])
        entries.append((sid, "/tmp/ws", 1000.0, p))
    monkeypatch.setattr(tm, "_codex_scan_sessions", _fake_scan(entries))
    resp = await client.get("/api/codex/sessions")
    sessions = resp.json()[0]["sessions"]
    titles_by_sid = {s["session_id"]: s["title"] for s in sessions}
    for sid, _msg, expected in cases:
        assert (
            titles_by_sid[sid] == expected
        ), f"{sid}: expected {expected!r}, got {titles_by_sid[sid]!r}"


@pytest.mark.asyncio
async def test_codex_title_bootstrap_then_task_shows_task_title(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """A session that received a task after bootstrap shows the task title, not the role label."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000024",
        "/tmp/ws",
        1000.0,
        [
            ("user", BOOTSTRAP_AGENT),
            ("user", TASK_ASSIGNMENT),
        ],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000024", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_plain_user_message_still_works(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Non-workspace sessions (plain user first message) are unchanged."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000025",
        "/tmp/proj",
        1000.0,
        [
            ("user", "<environment_context>\n  <cwd>/tmp/proj</cwd>"),
            ("user", "fix the login flow"),
        ],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000025", "/tmp/proj", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "fix the login flow"


@pytest.mark.asyncio
async def test_codex_title_skips_boilerplate_before_task_prompt(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Codex boilerplate (env/permissions) that precedes a task prompt is skipped."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    s = _write_rollout(
        tmp_path,
        "10000000-0000-0000-0000-000000000026",
        "/tmp/ws",
        1000.0,
        [
            ("user", "<environment_context>\n  <cwd>/tmp/ws</cwd>"),
            ("user", "<permissions instructions>\nSandboxing..."),
            ("user", TASK_ASSIGNMENT),
        ],
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000026", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    assert resp.json()[0]["sessions"][0]["title"] == "codex session选择"


@pytest.mark.asyncio
async def test_codex_title_long_task_title_is_truncated(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Task titles exceeding _CODEX_TITLE_MAX_LEN are truncated with ellipsis."""
    import importlib

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    long_title = "A" * 200
    prompt = (
        "New workspace task assigned.\n\n"
        "Workspace: ws\n"
        "Task ID: x\n"
        f"Task title: {long_title}\n\n"
        "Task description:\nDo it.\n"
    )
    s = _write_rollout(
        tmp_path, "10000000-0000-0000-0000-000000000027", "/tmp/ws", 1000.0, [("user", prompt)]
    )
    monkeypatch.setattr(
        tm,
        "_codex_scan_sessions",
        _fake_scan([("10000000-0000-0000-0000-000000000027", "/tmp/ws", 1000.0, s)]),
    )
    resp = await client.get("/api/codex/sessions")
    title = resp.json()[0]["sessions"][0]["title"]
    assert len(title) == 80
    assert title.endswith("…")
    assert title.startswith("A" * 79)
