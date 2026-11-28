"""Offline unit and integration tests for silent Feedback Reaper, inline Top-3 lessons, and CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from claude_hub.cli.main import cli
from claude_hub.main import app
from claude_hub.models import (
    AgentType,
    FeedbackLessonCreate,
    FeedbackSummaryRequest,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StreamModelOption,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTaskStatus,
)
from claude_hub.services.agent_stream.maso_http import MasoServiceUnavailable
from claude_hub.services.feedback.common import (
    INLINE_LESSONS_MAX_CHARS,
    _tokens,
    render_inline_lessons_block,
)
from claude_hub.services.feedback.reaper import (
    _ephemeral_reaper_turn,
    apply_reaper_output,
    parse_reaper_response,
    resolve_reaper_provider,
    select_maso_reaper_model,
)
from claude_hub.services.feedback_lessons import FeedbackLessonStore
from claude_hub.services.goal_run.strict_review import collect_strict_review_context
from claude_hub.services.workspace_manager import WorkspaceManager, workspace_manager

workspace_module = import_module("claude_hub.services.workspace_manager")


def _write_iteration_record(
    state_root: Path,
    workspace_id: str,
    task_id: str,
    *,
    title: str = "Fix async lock deadlock",
    review_failed_count: int = 1,
) -> Path:
    records_dir = state_root / workspace_id / "task_records"
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"2026-11-25T10-00-00-{task_id}.json"
    reports = [{"state": "review_failed", "message": "deadlock under nested lock"}] * (
        review_failed_count
    ) + [{"state": "completed", "message": "released lock before await"}]
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": workspace_id,
                "task": {"id": task_id, "title": title, "status": "done"},
                "reports": reports,
                "artifacts": {
                    "changed_files": ["services/feedback/catalog.py"],
                    "validation": ["pytest passed"],
                    "risks": [],
                },
                "final_summary": "Released lock before awaiting transport.",
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WorkspaceManager, str]:
    now = datetime(2026, 11, 25, 12, 0, 0)
    state_root = tmp_path / "state"
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    manager = object.__new__(WorkspaceManager)
    ws_id = "ws-silent"
    manager.workspaces = {
        ws_id: Workspace(
            id=ws_id,
            name="Silent Test Repo",
            path=str(repo_root),
            default_branch="main",
            session_prefix="sil",
            created_at=now,
            updated_at=now,
        )
    }
    manager.tasks = {}
    manager.sessions = {}
    manager.reports = {}
    manager._feedback_started_at = now
    manager._feedback_summary_locks = {}
    manager._feedback_reaper_jobs = {}
    manager._feedback_reaper_active_runs = {}
    manager._feedback_reaper_caller = None
    monkeypatch.setattr(manager, "_feedback_store", lambda: FeedbackLessonStore(state_root))
    monkeypatch.setattr(
        manager, "_workspace_task_records_dir", lambda wid: state_root / wid / "task_records"
    )
    monkeypatch.setattr(manager, "_runtime_now", lambda: now)
    return manager, ws_id


# ── 1. Model selection & ephemeral session archival ──────────────────────────


def test_select_maso_reaper_model_honors_settings_and_prefers_light_high() -> None:
    catalog = [
        "demo-pro_reasoning_high",
        "demo-flash_reasoning_high",
        "demo-flash",
        "demo-base",
    ]
    assert (
        select_maso_reaper_model(
            catalog,
            feedback_model="demo-pro_reasoning_high",
            patrol_model="demo-base",
        )
        == "demo-pro_reasoning_high"
    )
    assert (
        select_maso_reaper_model(
            catalog,
            feedback_model="not-advertised",
            patrol_model="demo-base",
        )
        == "demo-base"
    )
    assert select_maso_reaper_model(catalog) == "demo-flash_reasoning_high"
    assert (
        select_maso_reaper_model(["demo-alpha_reasoning_high", "demo-base"])
        == "demo-alpha_reasoning_high"
    )
    assert select_maso_reaper_model(["demo-lite", "demo-base"]) == "demo-lite"
    assert select_maso_reaper_model([]) is None


@pytest.mark.asyncio
async def test_resolve_reaper_provider_uses_maso_or_falls_back_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import claude_hub.services.local_runtime_settings as local_settings_mod
    import claude_hub.services.maso_discovery as maso_discovery_mod

    monkeypatch.setattr(local_settings_mod, "local_setting", lambda key, default="": "")

    async def fake_discover(
        _env: dict[str, str],
    ) -> tuple[str, list[StreamModelOption], str]:
        return (
            "http://127.0.0.1:19999",
            [
                StreamModelOption(id="demo-pro_reasoning_high", label="Demo Pro High"),
                StreamModelOption(id="demo-flash_reasoning_high", label="Demo Flash High"),
            ],
            "demo-pro_reasoning_high",
        )

    monkeypatch.setattr(maso_discovery_mod, "discover_maso_service", fake_discover)
    agent_type, env = await resolve_reaper_provider()
    assert agent_type == AgentType.MASO
    assert env["MASO_BASE_URL"] == "http://127.0.0.1:19999"
    assert env["MASO_MODEL"] == "demo-flash_reasoning_high"

    # When MASO is unavailable, fall back to read-only CLI provider.
    async def fail_discover(_env: dict[str, str]) -> tuple[str, list[dict[str, str]], str]:
        raise MasoServiceUnavailable("offline")

    monkeypatch.setattr(maso_discovery_mod, "discover_maso_service", fail_discover)
    monkeypatch.setattr(
        "claude_hub.services.feedback.reaper.shutil.which",
        lambda cmd: "/usr/bin/codex" if cmd == "codex" else None,
    )
    agent_type, env = await resolve_reaper_provider()
    assert agent_type == AgentType.CODEX
    assert env == {"CODEX_REASONING_EFFORT": "low"}

    monkeypatch.setattr(
        "claude_hub.services.feedback.reaper.shutil.which",
        lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
    )
    agent_type, env = await resolve_reaper_provider()
    assert agent_type == AgentType.CLAUDE
    assert env == {}


@pytest.mark.asyncio
async def test_ephemeral_reaper_turn_always_archives_session_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import claude_hub.services.feedback.reaper as reaper_mod

    archived: list[tuple[str, str]] = []
    created_requests: list[Any] = []
    now = datetime(2026, 11, 25, 12, 0, 0)
    fake_session = ManagedSession(
        id="ephemeral-reaper-1",
        workspace_id="ws-local",
        tab_id="tab-ephemeral-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title="Feedback reaper",
        workspace_path="/tmp/repo",
        tmux_session="ephemeral-reaper-1",
        ephemeral=True,
        caller_owned_ephemeral=True,
        created_at=now,
        updated_at=now,
    )

    async def fake_resolve() -> tuple[AgentType, dict[str, str]]:
        return AgentType.MASO, {"MASO_MODEL": "demo-flash_reasoning_high"}

    async def fake_ensure(ws_id: str, request: Any, *, bootstrap: bool = True) -> ManagedSession:
        assert bootstrap is False
        created_requests.append((ws_id, request))
        return fake_session

    async def fake_archive(ws_id: str, session_id: str) -> ManagedSession:
        archived.append((ws_id, session_id))
        return fake_session

    async def fake_run_fail(_session: Any, _prompt: str) -> str:
        raise RuntimeError("synthetic turn failure")

    monkeypatch.setattr(reaper_mod, "resolve_reaper_provider", fake_resolve)
    monkeypatch.setattr(reaper_mod, "_resolve_ephemeral_host_workspace_id", lambda wid: "ws-local")
    monkeypatch.setattr(workspace_manager, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(workspace_manager, "archive_session", fake_archive)
    monkeypatch.setattr(reaper_mod, "_run_ephemeral_reaper_turn", fake_run_fail)

    with pytest.raises(RuntimeError, match="synthetic turn failure"):
        await _ephemeral_reaper_turn("ws-local", "prompt")

    assert len(created_requests) == 1
    assert created_requests[0][1].ephemeral is True
    assert created_requests[0][1].reuse_existing is False
    assert archived == [("ws-local", "ephemeral-reaper-1")]


# ── 2. Structured JSON parsing, fallback, and single-flight execution ────────


def test_parse_reaper_response_handles_raw_fenced_and_cli_fallback() -> None:
    raw_json = json.dumps(
        {
            "lessons": [
                {
                    "title": "Release lock before await",
                    "summary": "Release asyncio locks before awaiting transport callbacks.",
                    "applies_when": ["async transport", "并发锁"],
                    "do": "Release lock before awaiting I/O.",
                    "avoid": "Holding asyncio.Lock across await.",
                    "tags": ["execution", "asyncio", "死锁"],
                    "evidence_task_ids": ["task-1"],
                    "confidence": 0.6,
                }
            ],
            "skipped_reason": None,
        }
    )
    parsed = parse_reaper_response(raw_json)
    assert len(parsed.lessons) == 1
    assert parsed.lessons[0]["tags"] == ["execution", "asyncio", "死锁"]
    assert parsed.skipped_reason is None

    fenced = f"Here are the distilled lessons:\n```json\n{raw_json}\n```\nDone."
    parsed_fenced = parse_reaper_response(fenced)
    assert len(parsed_fenced.lessons) == 1

    cli_fallback = "created_lesson_ids=lesson-a,lesson-b | skipped_reason=none"
    parsed_cli = parse_reaper_response(cli_fallback)
    assert parsed_cli.lessons == []
    assert parsed_cli.created_lesson_ids == ["lesson-a", "lesson-b"]
    assert parsed_cli.skipped_reason is None

    with pytest.raises(ValueError, match="unparseable"):
        parse_reaper_response("I looked at the records and nothing looks structured here.")


@pytest.mark.asyncio
async def test_silent_reaper_single_flight_persists_lessons_and_commits_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, ws_id = _make_manager(tmp_path, monkeypatch)
    _write_iteration_record(tmp_path / "state", ws_id, "task-deadlock-1")

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_caller(workspace_id: str, prompt: str) -> str:
        calls.append(prompt)
        started.set()
        await release.wait()
        return json.dumps(
            {
                "lessons": [
                    {
                        "title": "Release lock before await",
                        "summary": "Release asyncio locks before awaiting transport calls.",
                        "applies_when": ["async transport", "异步锁"],
                        "do": "Copy data and release the lock before awaiting transport.",
                        "avoid": "Awaiting network or transport while holding a lock.",
                        "tags": ["execution", "asyncio", "死锁"],
                        "scope": "workspace",
                        "confidence": 0.9,
                        "evidence_task_ids": ["task-deadlock-1"],
                    }
                ],
                "skipped_reason": None,
            }
        )

    manager._feedback_reaper_caller = fake_caller

    run1 = await manager.summarize_workspace_feedback(ws_id, FeedbackSummaryRequest())
    await started.wait()

    # Concurrent trigger coalesces cleanly onto the in-flight run without spawning a second turn.
    run2 = await manager.summarize_workspace_feedback(ws_id, FeedbackSummaryRequest())
    assert run2.id == run1.id
    assert run1.task_id is None
    assert run1.running is True
    assert manager.tasks == {}
    assert manager.feedback_automation_status(ws_id)["running"] is True

    release.set()
    completed = await manager.wait_for_feedback_reaper(ws_id)
    assert completed is not None
    assert len(calls) == 1
    assert completed.running is False
    assert completed.error is None
    assert len(completed.created_lesson_ids) == 1
    assert manager.tasks == {}

    status = manager.feedback_automation_status(ws_id)
    assert status["running"] is False
    assert status["last_error"] is None
    assert status["last_summarized_at"] is not None

    # Lesson is persisted with capped single-evidence confidence and bilingual tags.
    lessons = manager._feedback_store().list_lessons(ws_id)
    assert len(lessons) == 1
    assert lessons[0].confidence == 0.6
    assert "死锁" in lessons[0].tags

    # Echo-merge works when a second forced run echoes the existing fingerprint verbatim.
    fp = lessons[0].fingerprint
    assert fp.startswith("workspace:")
    merged_calls = 0

    async def merge_caller(workspace_id: str, prompt: str) -> str:
        nonlocal merged_calls
        merged_calls += 1
        assert fp in prompt
        return json.dumps(
            {
                "lessons": [
                    {
                        "title": "Release lock before await",
                        "summary": "Release asyncio locks before awaiting transport calls.",
                        "applies_when": ["async transport"],
                        "do": "Release lock before awaiting.",
                        "avoid": "Holding lock across await.",
                        "tags": ["execution", "asyncio", "死锁"],
                        "evidence_task_ids": ["task-deadlock-1"],
                        "fingerprint": fp,
                    }
                ]
            }
        )

    manager._feedback_reaper_caller = merge_caller
    run3 = await manager.summarize_workspace_feedback(ws_id, FeedbackSummaryRequest(force=True))
    completed3 = await manager.wait_for_feedback_reaper(ws_id)
    assert completed3 is not None
    assert merged_calls == 1
    assert completed3.created_lesson_ids == []
    assert completed3.merged_lesson_ids == [lessons[0].id]
    assert len(manager._feedback_store().list_lessons(ws_id)) == 1


@pytest.mark.asyncio
async def test_apply_reaper_output_skips_invalid_candidates_and_persists_valid(
    tmp_path: Path,
) -> None:
    store = FeedbackLessonStore(tmp_path)
    _write_iteration_record(tmp_path, "ws", "task-good")
    parsed = parse_reaper_response(
        json.dumps(
            {
                "lessons": [
                    {
                        "title": "Bad missing evidence",
                        "summary": "Missing evidence task",
                        "applies_when": ["always"],
                        "do": "Do x",
                        "avoid": "Avoid y",
                        "evidence_task_ids": ["does-not-exist"],
                    },
                    {
                        "title": "Good grounded lesson",
                        "summary": "Always verify checkout path.",
                        "applies_when": ["worktree"],
                        "do": "Check pwd before edit.",
                        "avoid": "Editing main checkout.",
                        "tags": ["governance", "worktree", "工作树"],
                        "evidence_task_ids": ["task-good"],
                        "confidence": 0.6,
                    },
                ]
            }
        )
    )
    applied = apply_reaper_output(store, "ws", parsed)
    assert applied.rejected_count == 1
    assert len(applied.created_lesson_ids) == 1
    assert applied.skipped_reason is None


# ── 3. Bilingual CJK bigram retrieval, Top-3 <= 1200 chars, deduplicated hits ─


def test_bilingual_cjk_bigrams_inline_top3_budget_and_zero_when_irrelevant(
    tmp_path: Path,
) -> None:
    tokens_mixed = _tokens("排查 services/feedback/catalog.py 异步死锁")
    assert "services/feedback/catalog.py" in tokens_mixed
    assert "catalog" in tokens_mixed
    assert "异步" in tokens_mixed
    assert "死锁" in tokens_mixed
    assert "排查" in tokens_mixed

    store = FeedbackLessonStore(tmp_path)
    ws_id = "ws-inline"

    # Seed 5 lessons: 4 matching high-confidence lessons + 1 low-confidence lesson.
    for idx in range(4):
        store.create_lesson(
            ws_id,
            FeedbackLessonCreate(
                id=f"deadlock-rule-{idx}",
                title=f"Async lock guardrail {idx}: keep transport awaits outside locks",
                summary=f"Detailed summary {idx} for async lock safety and concurrency diagnostics.",
                applies_when=[
                    f"services/feedback/catalog.py",
                    f"异步死锁排查场景-{idx}",
                ],
                do=(
                    f"Step {idx}: snapshot mutable state and release asyncio.Lock before awaiting "
                    "any transport or subprocess I/O so callbacks never deadlock."
                ),
                avoid=(
                    f"Never await network, RPC, or terminal delivery callbacks while holding "
                    f"workspace lock {idx} across suspension points."
                ),
                tags=["execution", "asyncio", "死锁", "并发"],
                confidence=0.8,
            ),
            enforce_iteration_signal=False,
            allow_manual_without_evidence=True,
        )

    store.create_lesson(
        ws_id,
        FeedbackLessonCreate(
            id="low-confidence-deadlock",
            title="Low confidence async lock note",
            summary="Weak unconfirmed note about deadlocks.",
            applies_when=["死锁"],
            do="Maybe check locks.",
            avoid="Guessing.",
            tags=["死锁"],
            confidence=0.2,
        ),
        enforce_iteration_signal=False,
        allow_manual_without_evidence=True,
    )

    # Irrelevant query -> 0 items and 0 characters!
    assert store.lesson_context_payload(ws_id, "completely unrelated typography font") == []
    assert render_inline_lessons_block([], workspace_id=ws_id, role="worker") == ""
    assert render_inline_lessons_block([], workspace_id=ws_id, role="review") == ""

    # Chinese query matches bilingual lessons via CJK bigrams, capped strictly to Top 3,
    # excluding the low-confidence (< 0.4) lesson.
    matched = store.lesson_context_payload(ws_id, "排查异步死锁问题")
    assert len(matched) == 3
    matched_ids = [item["id"] for item in matched]
    assert "low-confidence-deadlock" not in matched_ids
    for item in matched:
        assert item["do"]
        assert item["avoid"]
        assert item["applies_when"]

    worker_block = render_inline_lessons_block(matched, workspace_id=ws_id, role="worker")
    assert len(worker_block) <= INLINE_LESSONS_MAX_CHARS
    assert "Workspace execution guardrails" in worker_block
    assert "Do:" in worker_block
    assert "Avoid:" in worker_block
    assert "When:" in worker_block
    assert f"claude-hub lessons get <id> --workspace-id {ws_id}" in worker_block

    review_block = render_inline_lessons_block(matched, workspace_id=ws_id, role="review")
    assert len(review_block) <= INLINE_LESSONS_MAX_CHARS
    assert "Workspace review checkpoints" in review_block
    assert "Do:" in review_block
    assert "Avoid:" in review_block

    # Deduplicated hit recording per (lesson_id, task_id).
    first_hits = store.record_lesson_hit(ws_id, matched_ids, task_id="task-1")
    assert first_hits == matched_ids
    # Retry / re-dispatch / review on same task_id must NOT inflate hit_count!
    retry_hits = store.record_lesson_hit(ws_id, matched_ids, task_id="task-1")
    assert retry_hits == []
    for lid in matched_ids:
        lesson = store.get_lesson(ws_id, lid)
        assert lesson.hit_count == 1
        assert lesson.success_count == 0

    # Completion records success_count=1 without double-counting hit_count.
    store.record_lesson_success(ws_id, matched_ids, task_id="task-1")
    store.record_lesson_success(ws_id, matched_ids, task_id="task-1")
    for lid in matched_ids:
        lesson = store.get_lesson(ws_id, lid)
        assert lesson.hit_count == 1
        assert lesson.success_count == 1

    # Different task_id DOES increment hit_count cleanly.
    second_hits = store.record_lesson_hit(ws_id, matched_ids, task_id="task-2")
    assert second_hits == matched_ids
    for lid in matched_ids:
        assert store.get_lesson(ws_id, lid).hit_count == 2


@pytest.mark.asyncio
async def test_goal_strict_review_inlines_top3_bilingual_lessons_and_records_deduped_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = FeedbackLessonStore(tmp_path / "state")
    ws_id = "ws-strict"
    store.create_lesson(
        ws_id,
        FeedbackLessonCreate(
            id="strict-cjk-guard",
            title="验证工作树隔离",
            summary="所有改动必须在独立工作树执行。",
            applies_when=["工作树", "worktree"],
            do="修改前先确认 cwd 指向独立工作树。",
            avoid="直接修改主仓 main 分支。",
            tags=["governance", "worktree", "工作树"],
            confidence=0.8,
        ),
        enforce_iteration_signal=False,
        allow_manual_without_evidence=True,
    )

    fake_ws = SimpleNamespace(id=ws_id, path=str(repo))
    monkeypatch.setattr(workspace_manager, "workspaces", {ws_id: fake_ws})
    monkeypatch.setattr(workspace_manager, "_feedback_store", lambda: store)

    ctx1 = await collect_strict_review_context(
        str(repo),
        objective="检查工作树隔离逻辑是否正确",
        assistant_text="已完成独立工作树校验。",
    )
    assert "strict-cjk-guard" in ctx1.lessons_summary
    assert "修改前先确认 cwd 指向独立工作树。" in ctx1.lessons_summary
    assert "直接修改主仓 main 分支。" in ctx1.lessons_summary
    assert len(ctx1.lessons_summary) <= INLINE_LESSONS_MAX_CHARS
    assert "Workspace Review Checkpoints" in ctx1.render_prompt_block()
    assert store.get_lesson(ws_id, "strict-cjk-guard").hit_count == 1

    # Second strict-review round for the same objective deduplicates the hit.
    await collect_strict_review_context(
        str(repo),
        objective="检查工作树隔离逻辑是否正确",
        assistant_text="第二轮复核完成。",
    )
    assert store.get_lesson(ws_id, "strict-cjk-guard").hit_count == 1


# ── 4. CLI `lessons create` & `lessons summarize` end-to-end ─────────────────


def test_cli_lessons_create_auto_derives_summary_and_persists_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_write_snapshot", lambda _wid: None)
    monkeypatch.setattr(workspace_manager, "workspaces", {})
    monkeypatch.setattr(workspace_manager, "tasks", {})
    monkeypatch.setattr(workspace_manager, "sessions", {})
    monkeypatch.setattr(workspace_manager, "reports", {})

    api_client = TestClient(app)
    ws = api_client.post(
        "/api/workspaces",
        json={"name": "CLI Lesson Repo", "path": str(repo), "session_prefix": "clil"},
    ).json()

    # Wire CLI httpx client directly to the in-memory FastAPI app via TestClient.
    def _transport_handler(request: httpx.Request) -> httpx.Response:
        resp = api_client.request(
            request.method,
            str(request.url.raw_path.decode("utf-8")),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

    import claude_hub.cli.main as cli_main_mod
    from claude_hub.cli.client import HubClient

    monkeypatch.setattr(
        cli_main_mod,
        "get_client",
        lambda _ctx: HubClient(
            "http://test.local",
            transport=httpx.MockTransport(_transport_handler),
        ),
    )

    runner = CliRunner()

    # 1. Direct Dot -> Workspace persistence WITHOUT --summary and WITHOUT --evidence-task-id!
    res = runner.invoke(
        cli,
        [
            "--json",
            "lessons",
            "create",
            ws["id"],
            "--title",
            "Run verify before commit",
            "--applies-when",
            "git commit",
            "--do",
            "Run ./scripts/verify.sh check-staged before committing.",
            "--avoid",
            "Committing unstaged or unformatted changes.",
            "--tag",
            "verification",
            "--tag",
            "提交检查",
        ],
    )
    assert res.exit_code == 0, res.output
    created = json.loads(res.output)
    assert created["title"] == "Run verify before commit"
    assert "Run ./scripts/verify.sh check-staged" in created["summary"]
    assert created["do"] == "Run ./scripts/verify.sh check-staged before committing."
    assert created["avoid"] == "Committing unstaged or unformatted changes."
    assert "提交检查" in created["tags"]

    # 2. Explicit --summary still works cleanly.
    res_explicit = runner.invoke(
        cli,
        [
            "--json",
            "lessons",
            "create",
            ws["id"],
            "--title",
            "Explicit summary rule",
            "--summary",
            "Keep explicit summaries verbatim.",
            "--applies-when",
            "docs",
            "--do",
            "Pass --summary explicitly.",
            "--avoid",
            "Omitting details.",
            "--tag",
            "docs",
        ],
    )
    assert res_explicit.exit_code == 0, res_explicit.output
    assert json.loads(res_explicit.output)["summary"] == "Keep explicit summaries verbatim."

    # 3. Missing --summary and missing --do raises a clear CLI error.
    res_missing = runner.invoke(
        cli,
        ["lessons", "create", ws["id"], "--title", "Only title"],
    )
    assert res_missing.exit_code != 0
    assert "--summary is required" in res_missing.output

    # 4. `claude-hub lessons reap` works cleanly with silent reaper response (task_id=null).
    res_sum = runner.invoke(cli, ["--json", "lessons", "reap", ws["id"]])
    assert res_sum.exit_code == 0, res_sum.output
    summary_data = json.loads(res_sum.output)
    assert summary_data["task_id"] is None
    assert summary_data["cache_hit"] is True
    assert summary_data["running"] is False


# ── 5. Legacy board reaper cleanup on full _load_state ───────────────────────


def test_load_state_retires_legacy_feedback_reaper_tasks_and_hides_from_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    ws_id = "ws-legacy"
    ws_dir = state_root / ws_id
    ws_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    now = datetime(2026, 11, 25, 11, 0, 0)

    index_path = state_root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "id": ws_id,
                        "name": "Legacy WS",
                        "path": str(repo),
                        "default_branch": "main",
                        "session_prefix": "leg",
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (ws_dir / "state.json").write_text(
        json.dumps(
            {
                "workspace": {
                    "id": ws_id,
                    "name": "Legacy WS",
                    "path": str(repo),
                    "default_branch": "main",
                    "session_prefix": "leg",
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                },
                "tasks": [
                    {
                        "id": "stuck-reaper-todo",
                        "workspace_id": ws_id,
                        "title": "Feedback Reaper: summarize workspace lessons",
                        "prompt": "legacy prompt",
                        "agent_type": "codex",
                        "status": "todo",
                        "system_internal": True,
                        "internal_kind": "feedback_reaper",
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                    {
                        "id": "normal-task-1",
                        "workspace_id": ws_id,
                        "title": "Normal user task",
                        "prompt": "Do work",
                        "agent_type": "codex",
                        "status": "todo",
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                ],
                "sessions": [],
                "reports": [],
            }
        ),
        encoding="utf-8",
    )

    state_mod = sys.modules["claude_hub.services.workspace_manager._state"]
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    monkeypatch.setattr(state_mod, "INDEX_FILE", index_path)
    monkeypatch.setattr(state_mod, "LEGACY_STATE_FILE", state_root / "legacy.json")

    loaded = WorkspaceManager()
    assert loaded.tasks["stuck-reaper-todo"].status == WorkspaceTaskStatus.DONE
    assert loaded.tasks["stuck-reaper-todo"].completed_at is not None
    assert loaded.tasks["normal-task-1"].status == WorkspaceTaskStatus.TODO

    # Persisted workspace document (SQLite-authoritative) reflects DONE status and reloads cleanly.
    from claude_hub.services.local_storage import read_document

    persisted = read_document(ws_dir / "state.json")
    by_id = {item["id"]: item for item in persisted["tasks"]}
    assert by_id["stuck-reaper-todo"]["status"] == "done"
    assert by_id["normal-task-1"]["status"] == "todo"

    reloaded = WorkspaceManager()
    assert reloaded.tasks["stuck-reaper-todo"].status == WorkspaceTaskStatus.DONE
    assert reloaded.tasks["normal-task-1"].status == WorkspaceTaskStatus.TODO

    overview = loaded.get_board_overview()
    overview_task_ids = {t.id for t in overview["tasks"]}
    assert "stuck-reaper-todo" not in overview_task_ids
    assert "normal-task-1" in overview_task_ids
