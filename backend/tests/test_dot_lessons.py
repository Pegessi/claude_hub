"""Dot's brief loop: brief check, delegate gate, review, lessons and first-pass rate."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.services import workspace_manager
from claude_hub.services.dot import DotService, DotStore, reset_dot_service
from claude_hub.services.dot import service as service_module
from claude_hub.services.dot.context import build_session_pack, build_turn_pack
from claude_hub.services.dot.lessons import (
    LESSON_HALF_LIFE_DAYS,
    RULES_IN_SESSION,
    lesson_score,
    lint_brief,
    match_lessons,
    session_rules,
)
from claude_hub.services.dot.store import utc_now

GOOD: dict[str, Any] = {
    "done_when": ["notes/fa3.md has 5 bullets with numbers", "each bullet cites a section"],
    "output": "notes/fa3.md in the repo",
    "context": "FA3 paper: arxiv 2407.08608",
}


@pytest.fixture()
def service(tmp_path: Path) -> Generator[DotService, None, None]:
    store = DotStore(tmp_path / "dot")
    store.ensure()
    store.update_state(tab_id="tab-1")
    dot = DotService(store)
    reset_dot_service(dot)
    yield dot
    reset_dot_service(None)


# ── pure checks ────────────────────────────────────────────────────────


def test_lint_flags_unverifiable_briefs() -> None:
    assert lint_brief(**GOOD) == []
    warnings = lint_brief(
        done_when=["done", "做好了", "tests pass", "完成任務"], context="", output=""
    )
    assert [w.split(" (")[0] for w in warnings[:2]] == ["Done-when 1", "Done-when 2"]
    assert not any("Done-when 3" in w or "Done-when 4" in w for w in warnings)
    assert any(w.startswith("No output") for w in warnings)
    assert any(w.startswith("No context") for w in warnings)
    many = lint_brief(done_when=[f"file {i} exists" for i in range(8)], context="c", output="o")
    assert many and "one goal" in many[0]


def test_scores_fade_and_only_strong_rules_stay_in_session() -> None:
    now = utc_now()
    fresh = {"id": "a", "rule": "x", "votes": 1, "seen": now.isoformat()}
    old = {
        "id": "b",
        "rule": "y",
        "votes": 4,
        "seen": (now - timedelta(days=LESSON_HALF_LIFE_DAYS * 2)).isoformat(),
    }
    assert lesson_score(fresh, now) == pytest.approx(1)
    assert lesson_score(old, now) == pytest.approx(1)
    faded = {**old, "id": "c", "votes": 1}
    assert lesson_score(faded, now) == pytest.approx(0.25)
    many = [{"id": f"l{i}", "rule": "r", "votes": i + 1, "seen": now.isoformat()} for i in range(8)]
    rules = session_rules([*many, faded], now)
    assert [rule["id"] for rule in rules] == ["l7", "l6", "l5", "l4", "l3"]
    assert len(rules) == RULES_IN_SESSION


def test_match_needs_shared_words() -> None:
    now = utc_now()
    lessons = [
        {"id": "en", "rule": "Paper summaries: name the paper version and the output path",
         "votes": 1, "seen": now.isoformat()},
        {"id": "zh", "rule": "論文摘要要寫明版本和輸出路徑", "votes": 1, "seen": now.isoformat()},
        {"id": "other", "rule": "Training runs: give the GPU budget", "votes": 3,
         "seen": now.isoformat()},
    ]  # fmt: skip
    assert [hit["id"] for hit in match_lessons(lessons, "Summarize the FA3 paper", now)] == []
    hits = match_lessons(lessons, "Write a paper summary; output path notes/", now)
    assert [hit["id"] for hit in hits] == ["en"]
    assert [hit["id"] for hit in match_lessons(lessons, "總結這篇論文，輸出到 docs", now)] == ["zh"]


def test_store_lessons_vote_edit_drop_merge(service: DotService) -> None:
    store = service.store
    first = store.add_lesson("  Say the  output path ", "task-1")
    assert first["rule"] == "Say the output path" and first["votes"] == 1
    with pytest.raises(ValueError):
        store.add_lesson("   ")
    voted = store.vote_lesson(first["id"], "task-2")
    assert voted is not None and voted["votes"] == 2 and voted["tasks"] == ["task-1", "task-2"]
    assert store.vote_lesson("l-none") is None

    second = store.add_lesson("Give the output location", "task-3")
    merged = store.merge_lessons(first["id"], [second["id"]])
    assert merged["votes"] == 3 and merged["tasks"] == ["task-1", "task-2", "task-3"]
    assert [lesson["id"] for lesson in store.list_lessons()] == [first["id"]]
    assert store.get_lesson(second["id"])["merged_into"] == first["id"]  # type: ignore[index]
    with pytest.raises(KeyError):
        store.merge_lessons(first["id"], ["l-none"])

    assert store.update_lesson(first["id"], rule="State the output path")["rule"].startswith("State")  # type: ignore[index]
    assert store.update_lesson(first["id"], dropped=True)["dropped"]  # type: ignore[index]
    assert store.list_lessons() == []
    # The same miss again brings a dropped lesson back.
    assert not store.vote_lesson(first["id"]).get("dropped")  # type: ignore[union-attr]


# ── delegate gate ──────────────────────────────────────────────────────


def _fake_hub(monkeypatch: MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    async def register(ws: str, payload: Any, actor_key: str) -> tuple[Any, bool]:
        sent.append(payload)
        task_id = f"task-{len(sent)}"
        return (
            SimpleNamespace(id=task_id, title=payload.title, status=SimpleNamespace(value="todo")),
            False,
        )

    async def start(task_id: str, request: Any) -> Any:
        return SimpleNamespace(id=task_id, title="t", status=SimpleNamespace(value="queued"))

    monkeypatch.setattr(workspace_manager, "register_task", register)
    monkeypatch.setattr(workspace_manager, "start_task", start)
    return sent


@pytest.mark.asyncio
async def test_delegate_holds_a_weak_brief_until_fixed_or_confirmed(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    sent = _fake_hub(monkeypatch)
    base: dict[str, Any] = {
        "workspace_id": "ws",
        "title": "Paper",
        "goal": "Summarize FA3",
        "actor_key": "a",
    }

    held = await service.delegate(**base, done_when=["done"])
    assert held["held"] and held["work"] is None and sent == []
    assert len(held["warnings"]) == 3

    result = await service.delegate(**base, **GOOD)
    assert not result["held"] and result["warnings"] == [] and len(sent) == 1
    brief = sent[0].prompt
    assert "## Done when (check each one)\n1. notes/fa3.md has 5 bullets" in brief
    assert "\n2. each bullet cites a section" in brief
    assert "## Output\nnotes/fa3.md in the repo" in brief
    assert "`1. ✓ <evidence>`" in brief
    work = result["work"]
    assert work["done_when"] == GOOD["done_when"] and "brief_warnings" not in work

    # A matching lesson holds even a clean brief once, so Dot reads it.
    lesson = service.store.add_lesson("FA3 summaries: cite the section for each bullet")
    again = await service.delegate(**base, **GOOD)
    assert again["held"] and [hit["id"] for hit in again["lessons"]] == [lesson["id"]]
    confirmed = await service.delegate(**base, done_when=["done"], confirm=True)
    assert not confirmed["held"] and len(sent) == 2
    assert len(confirmed["work"]["brief_warnings"]) == 3, "overridden warnings are kept"

    with pytest.raises(ValueError):
        await service.delegate(**base, done_when=["  "])


# ── review loop ────────────────────────────────────────────────────────


def _report(report_id: str, state: str, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=report_id,
        state=SimpleNamespace(value=state),
        message=message,
        validation="",
        risks="",
        changed_files=[],
        artifact_refs=[],
    )


@pytest.mark.asyncio
async def test_review_loop_learns_from_brief_misses(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    tasks: dict[str, Any] = {}
    reports: dict[str, list[Any]] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(
        workspace_manager, "reports_for_task", lambda _ws, tid: reports.get(tid, [])
    )

    def task(status: str) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(value=status), title="t")

    for task_id in ("task-1", "task-2", "task-3"):
        store.upsert_work(
            task_id, workspace_id="ws", title=task_id, status="working", done_when=GOOD["done_when"]
        )
        tasks[task_id] = task("working")
    store.upsert_work("task-w", workspace_id="ws", title="watched", status="working", watched=True)
    tasks["task-w"] = task("working")

    with pytest.raises(ValueError, match="not come back"):
        await service.review("task-1", verdict="ok")

    # task-1 asks a question, then all four come back.
    reports["task-1"] = [_report("r1", "needs_input", "Which version?")]
    await service.check_work()
    for task_id in tasks:
        tasks[task_id] = task("review")
        reports.setdefault(task_id, []).append(_report(f"{task_id}-r", "done", "1. ✓ written"))
    await service.check_work()

    card = store.get_card(store.get_work("task-2")["result_card"])  # type: ignore[index]
    assert card is not None and "## Done when\n1. notes/fa3.md has 5 bullets" in card.body
    assert [item["task_id"] for item in store.pending_reviews()] == ["task-1", "task-2", "task-3"]
    pack = build_turn_pack(store, None, "", first_turn=False, session_turn=2).text
    assert "## Waiting for your review" in pack and "the worker had to ask a question" in pack

    with pytest.raises(KeyError):
        await service.review("nope", verdict="ok")
    with pytest.raises(ValueError, match="needs a cause"):
        await service.review("task-1", verdict="redo")
    with pytest.raises(ValueError, match="needs a lesson"):
        await service.review("task-1", verdict="redo", cause="brief")
    with pytest.raises(ValueError, match="only for brief"):
        await service.review("task-1", verdict="redo", cause="env", lesson="x")

    first = await service.review(
        "task-1", verdict="ok", cause="brief", lesson="Paper tasks: name the paper version"
    )
    lesson_id = first["lesson"]["id"]
    second = await service.review("task-2", verdict="redo", cause="brief", lesson_id=lesson_id)
    assert second["lesson"]["votes"] == 2
    await service.review("task-3", verdict="ok", note="clean")
    with pytest.raises(ValueError, match="already reviewed"):
        await service.review("task-3", verdict="ok")

    assert store.pending_reviews() == [], "the watched task needs no verdict"
    stats = service.review_stats()
    assert stats == {
        "reviewed": 3,
        "first_pass": 1,  # task-1 asked, task-2 was a redo
        "rate": 0.33,
        "causes": {"brief": 2},
        "pending": 0,
        "window": service_module.STATS_WINDOW,
    }

    # The lesson is now a standing rule, shown in every new session.
    assert f"Paper tasks: name the paper version [{lesson_id}]" in build_session_pack(store)
    rows = service.lessons()
    assert rows[0]["in_session"] and rows[0]["score"] == 2

    # A second round (sent back, returned again) asks for a new verdict but
    # keeps the first one for the first-pass rate.
    tasks["task-2"] = task("working")
    await service.check_work()
    tasks["task-2"] = task("review")
    await service.check_work()
    assert [item["task_id"] for item in store.pending_reviews()] == ["task-2"]
    await service.review("task-2", verdict="ok")
    assert store.get_work("task-2")["first_verdict"] == "redo"  # type: ignore[index]
    assert service.review_stats()["first_pass"] == 1


def test_old_lessons_leave_the_session_pack(service: DotService, monkeypatch: MonkeyPatch) -> None:
    lesson = service.store.add_lesson("Say where the result goes")
    assert "Say where the result goes" in build_session_pack(service.store)
    later = utc_now() + timedelta(days=LESSON_HALF_LIFE_DAYS * 1.5)
    monkeypatch.setattr("claude_hub.services.dot.context.utc_now", lambda: later)
    assert "Say where the result goes" not in build_session_pack(service.store)
    assert service.store.get_lesson(lesson["id"]) is not None, "faded lessons stay as history"
