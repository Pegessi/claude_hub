"""Dot's brief loop: brief check, delegate gate, review, lessons and first-pass rate."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator, Optional

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
    same_rule,
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
    many = [
        {"id": f"l{i}", "rule": "r", "votes": i + 1, "seen": now.isoformat()} for i in range(10)
    ]
    rules = session_rules([*many, faded], now)
    assert [rule["id"] for rule in rules] == [f"l{i}" for i in range(9, 1, -1)]
    assert len(rules) == RULES_IN_SESSION


def test_a_bad_hand_edit_does_not_break_lessons(service: DotService) -> None:
    store = service.store
    lesson = store.add_lesson("When summarizing a paper, name the paper version")
    store._change_lesson(
        lesson["id"], lambda item: item.update(weight="high", votes="x", tasks=None)
    )
    assert lesson_score({"weight": float("nan"), "votes": 2}, utc_now()) == pytest.approx(2)
    assert build_session_pack(store), "a bad number must not break every turn"
    voted = store.vote_lesson(lesson["id"], "task-1")
    assert voted is not None and voted["votes"] == 1 and voted["tasks"] == ["task-1"]


def test_same_rule_finds_a_near_copy_only() -> None:
    lessons = [
        {"id": "a", "rule": "When summarizing a paper, name the paper version"},
        {"id": "b", "rule": "When training, give the GPU budget"},
    ]
    assert same_rule(lessons, "When summarizing papers, name the paper version")["id"] == "a"  # type: ignore[index]
    assert same_rule(lessons, "When summarizing a paper, name the output path") is None
    assert same_rule(lessons, "paper") is None, "too short to judge"
    # The same words that say the opposite are not the same rule.
    assert same_rule(lessons, "When summarizing a paper, do not name the paper version") is None
    assert same_rule(lessons, "When summarizing a paper, don’t name the paper version") is None


def test_store_lessons_vote_edit_drop_merge(service: DotService) -> None:
    store = service.store
    first = store.add_lesson("  Say the  output path ", "task-1")
    assert first["rule"] == "Say the output path" and first["votes"] == 1
    with pytest.raises(ValueError):
        store.add_lesson("   ")
    voted = store.vote_lesson(first["id"], "task-2")
    assert voted is not None and voted["votes"] == 2 and voted["tasks"] == ["task-1", "task-2"]
    assert store.vote_lesson("l-none") is None

    again = store.vote_lesson(first["id"], "task-2")
    assert again is not None and again["votes"] == 2, "one task votes a lesson once"

    second = store.add_lesson("Give the output location", "task-3")
    merged = store.merge_lessons(first["id"], [second["id"]])
    assert merged["votes"] == 3 and merged["tasks"] == ["task-1", "task-2", "task-3"]
    assert merged["weight"] == pytest.approx(3, abs=0.01)
    assert [lesson["id"] for lesson in store.list_lessons()] == [first["id"]]
    assert store.get_lesson(second["id"])["merged_into"] == first["id"]  # type: ignore[index]
    with pytest.raises(KeyError):
        store.merge_lessons(first["id"], ["l-none"])
    with pytest.raises(ValueError, match="merged into"):
        store.merge_lessons(second["id"], [first["id"]])

    # A vote for a merged lesson goes to the lesson it was merged into.
    forwarded = store.vote_lesson(second["id"], "task-4")
    assert forwarded is not None and forwarded["id"] == first["id"] and forwarded["votes"] == 4
    assert store.get_lesson(second["id"])["dropped"]  # type: ignore[index]
    assert [lesson["id"] for lesson in store.list_lessons()] == [first["id"]]

    assert store.update_lesson(first["id"], rule="State the output path")["rule"].startswith("State")  # type: ignore[index]
    assert store.update_lesson(first["id"], dropped=True)["dropped"]  # type: ignore[index]
    assert store.list_lessons() == []
    with pytest.raises(ValueError, match="dropped"):
        store.merge_lessons(first["id"], [store.add_lesson("Other rule")["id"]])
    # The user dropped it as wrong: a vote, direct or through a merged lesson, is refused.
    for lesson_id in (first["id"], second["id"]):
        with pytest.raises(ValueError, match="dropped"):
            store.vote_lesson(lesson_id, "task-5")
    assert store.get_lesson(first["id"])["dropped"]  # type: ignore[index]


def test_votes_and_merges_count_old_strength_at_its_faded_value(service: DotService) -> None:
    store = service.store
    long_ago = (utc_now() - timedelta(days=LESSON_HALF_LIFE_DAYS * 4)).isoformat()
    old = store.add_lesson("When writing a paper summary, name the paper version")
    fresh = store.add_lesson("When summarizing, say where the output goes")
    store._change_lesson(old["id"], lambda item: item.update(votes=8, seen=long_ago))
    # 8 votes four half-lives ago are worth 0.5 today, not 8.
    merged = store.merge_lessons(fresh["id"], [old["id"]])
    assert merged["votes"] == 9 and merged["weight"] == pytest.approx(1.5, abs=0.01)
    other = store.add_lesson("When training, give the GPU budget")
    store._change_lesson(other["id"], lambda item: item.update(votes=8, seen=long_ago))
    voted = store.vote_lesson(other["id"], "task-9")
    assert voted is not None and voted["weight"] == pytest.approx(1.5, abs=0.01)
    assert lesson_score(voted, utc_now()) == pytest.approx(1.5, abs=0.01)


def test_lesson_rows_show_reach_from_live_lessons_only(service: DotService) -> None:
    store = service.store
    rules = [store.add_lesson(f"Rule number {index}") for index in range(RULES_IN_SESSION)]
    big = store.add_lesson("Dropped but strong")
    store._change_lesson(big["id"], lambda item: item.update(votes=9))
    store.update_lesson(big["id"], dropped=True)
    weak = store.add_lesson("Weak one")
    store._change_lesson(weak["id"], lambda item: item.update(weight=0.2))
    copy = store.add_lesson("Rule number 0 again")
    store.merge_lessons(rules[0]["id"], [copy["id"]])
    rows = {row["id"]: row for row in service.lessons(include_dropped=True)}
    assert rows[big["id"]]["reach"] == "dropped" and not rows[big["id"]]["in_session"]
    assert rows[copy["id"]]["reach"] == "merged"
    assert rows[weak["id"]]["reach"] == "hidden", "outside the top few"
    assert sum(row["in_session"] for row in rows.values()) == RULES_IN_SESSION
    assert {row["reach"] for row in service.lessons()} == {"always", "hidden"}


# ── delegate gate ──────────────────────────────────────────────────────


def _fake_hub(monkeypatch: MonkeyPatch) -> list[Any]:
    sent: list[Any] = []
    by_key: dict[str, str] = {}

    async def register(ws: str, payload: Any, actor_key: str) -> tuple[Any, bool]:
        if payload.request_key in by_key:
            # A replay returns the task as it is now; it came back since the last poll.
            task_id = by_key[payload.request_key]
            return (
                SimpleNamespace(
                    id=task_id, title=payload.title, status=SimpleNamespace(value="review")
                ),
                True,
            )
        sent.append(payload)
        task_id = f"task-{len(sent)}"
        if payload.request_key:
            by_key[payload.request_key] = task_id
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

    # Lessons never hold a brief; they are in the session pack instead.
    service.store.add_lesson("FA3 summaries: cite the section for each bullet")
    again = await service.delegate(**base, **GOOD)
    assert not again["held"] and "lessons" not in again and len(sent) == 2
    confirmed = await service.delegate(
        **base, done_when=["done"], confirm=True, request_key="retry-1"
    )
    assert not confirmed["held"] and len(sent) == 3
    assert len(confirmed["work"]["brief_warnings"]) == 3, "overridden warnings are kept"
    # A retry with the same request key is not held again, and the task that came
    # back meanwhile keeps its old tracked status so the watcher still sees the change.
    replay = await service.delegate(**base, done_when=["done"], request_key="retry-1")
    assert not replay["held"] and len(sent) == 3
    assert replay["work"]["task_id"] == "task-3" and replay["work"]["status"] == "queued"

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
        reports.setdefault(task_id, []).append(_report(f"{task_id}-r", "completed", "1. ✓ written"))
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
        await service.review("task-1", verdict="redo", cause="env", lesson="x", note="n")
    with pytest.raises(ValueError, match="what went wrong"):
        await service.review("task-1", verdict="redo", cause="env")
    with pytest.raises(ValueError, match="only tasks Dot delegated"):
        await service.review("task-w", verdict="ok")

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
        "judged": 3,
        "returned": 3,
        "confirmed": 0,
        "confirmed_first_pass": 0,
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

    # task-3 was ok, but the user sent it back and it really was wrong: no first pass.
    tasks["task-3"] = task("working")
    await service.check_work()
    tasks["task-3"] = task("review")
    await service.check_work()
    await service.review("task-3", verdict="redo", cause="worker", note="missed check 2")
    assert store.get_work("task-3")["late_redo"]  # type: ignore[index]
    assert service.review_stats()["first_pass"] == 0

    # A result Dot cannot judge can be skipped, with a reason; it counts nowhere.
    store.upsert_work("task-4", workspace_id="ws", title="t4", status="review", result_card="c",
                      review_due=True)  # fmt: skip
    with pytest.raises(ValueError, match="no cause"):
        await service.review("task-4", verdict="skip", cause="env", note="n")
    with pytest.raises(ValueError, match="could not judge"):
        await service.review("task-4", verdict="skip")
    await service.review("task-4", verdict="skip", note="binary output, nothing to read")
    stats = service.review_stats()
    assert store.pending_reviews() == [] and (stats["reviewed"], stats["judged"]) == (3, 4)


@pytest.mark.asyncio
async def test_watcher_catches_quick_questions_redo_rounds_and_restarts(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    tasks: dict[str, Any] = {}
    reports: dict[str, list[Any]] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(
        workspace_manager, "reports_for_task", lambda _ws, tid: reports.get(tid, [])
    )

    def task(status: str, **times: Any) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(value=status), title="t", **times)

    for task_id in ("task-a", "task-b", "task-c"):
        store.upsert_work(task_id, workspace_id="ws", title=task_id, status="working")
        tasks[task_id] = task("working")

    # task-a asks and is answered between two polls, then comes back.
    reports["task-a"] = [_report("a1", "needs_input", "Which?"), _report("a2", "completed", "ok")]
    tasks["task-a"] = task("review")
    # task-b fails.
    tasks["task-b"] = task("failed")
    reports["task-b"] = [_report("b1", "failed", "no GPU")]
    tasks["task-c"] = task("review")
    reports["task-c"] = [_report("c1", "completed", "done")]
    await service.check_work()
    item_a = store.get_work("task-a")
    assert item_a is not None and item_a["asked"] and item_a["review_due"]
    assert store.get_work("task-b")["closed"]  # type: ignore[index]

    # task-a: redo, and the next report lands while the task still looks "review".
    await service.review("task-a", verdict="redo", cause="worker", note="skipped check 2")
    reports["task-a"].append(_report("a3", "ready_for_review", "fixed"))
    store.mark_inbox_read([event["id"] for event in store.list_inbox()])
    await service.check_work()
    assert store.get_work("task-a")["review_due"]  # type: ignore[index]
    assert [event["kind"] for event in store.list_inbox()] == ["task_ready"]

    # task-b: closed as failed, then restarted and returned.
    await service.review("task-b", verdict="redo", cause="env", note="no GPU")
    tasks["task-b"] = task("working")
    await service.check_work()
    assert not store.get_work("task-b")["closed"]  # type: ignore[index]
    tasks["task-b"] = task("review")
    reports["task-b"].append(_report("b2", "ready_for_review", "ran"))
    await service.check_work()
    assert store.get_work("task-b")["review_due"]  # type: ignore[index]

    # task-b again: re-run and failed again between two polls; only the end time moved.
    await service.review("task-b", verdict="redo", cause="env", note="no GPU")
    tasks["task-b"] = task("failed", failed_at=utc_now() - timedelta(minutes=5))
    await service.check_work()
    assert store.get_work("task-b")["ended_at"] and store.get_work("task-b")["closed"]  # type: ignore[index]
    store.mark_inbox_read([event["id"] for event in store.list_inbox()])
    await service.review("task-b", verdict="redo", cause="env", note="no GPU")
    await service.check_work()
    assert store.list_inbox() == [], "same run, no event"
    tasks["task-b"] = task("failed", failed_at=utc_now())
    await service.check_work()
    assert store.get_work("task-b")["review_due"]  # type: ignore[index]
    assert [event["kind"] for event in store.list_inbox()] == ["task_failed"]

    # task-a again: the worker's report and the Hub reviewer's start land in one poll.
    await service.review("task-a", verdict="redo", cause="worker", note="still skips check 2")
    reports["task-a"] += [
        _report("a4", "ready_for_review", "fixed check 2 too"),
        _report("a5", "review_started", "Reviewer is checking"),
    ]
    await service.check_work()
    item_a = store.get_work("task-a")
    assert item_a is not None and item_a["review_due"], "a new round, not the reviewer's"
    card = store.get_card(item_a["result_card"])
    assert card is not None and "fixed check 2 too" in card.body
    assert "Reviewer is checking" not in card.body

    # task-c comes back a second time before any verdict: the card shows the new report.
    reports["task-c"].append(_report("c2", "ready_for_review", "second try, all checks pass"))
    await service.check_work()
    card = store.get_card(store.get_work("task-c")["result_card"])  # type: ignore[index]
    assert card is not None and "second try" in card.body

    # task-c: the user changed their mind. Not a brief miss, not in the rate.
    with pytest.raises(ValueError, match="only for brief"):
        await service.review("task-c", verdict="redo", cause="change", lesson="x y z", note="n")
    await service.review("task-c", verdict="redo", cause="change", note="wants a table now")
    await service.review("task-a", verdict="ok")
    await service.review("task-b", verdict="ok")
    stats = service.review_stats()
    assert (stats["reviewed"], stats["first_pass"], stats["judged"]) == (2, 0, 3)
    assert stats["causes"] == {"worker": 1, "env": 1}


@pytest.mark.asyncio
async def test_watching_again_after_unwatch_tracks_the_task_fully(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    tasks: dict[str, Any] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _tid: [])

    def task(status: str) -> SimpleNamespace:
        return SimpleNamespace(
            id="task-x",
            workspace_id="ws",
            title="x",
            agent_type=SimpleNamespace(value="codex"),
            status=SimpleNamespace(value=status),
        )

    tasks["task-x"] = task("working")
    await service.watch("task-x")
    await service.unwatch("task-x")
    await service.watch("task-x")
    assert not store.get_work("task-x").get("stopped")  # type: ignore[union-attr]
    tasks["task-x"] = task("done")
    await service.check_work()
    tasks["task-x"] = task("working")
    await service.check_work()
    item = store.get_work("task-x")
    assert item is not None and item["status"] == "working" and not item["closed"]


@pytest.mark.asyncio
async def test_review_reuses_a_lesson_that_says_the_same(service: DotService) -> None:
    store = service.store
    old = store.add_lesson("When summarizing a paper, name the paper version", "task-0")
    for task_id in ("task-1", "task-2"):
        store.upsert_work(task_id, status="review", result_card="c", review_due=True)
    result = await service.review(
        "task-1",
        verdict="redo",
        cause="brief",
        lesson="When summarizing papers, name the paper version",
    )
    assert result["lesson_reused"] and result["lesson"]["id"] == old["id"]
    assert result["lesson"]["votes"] == 2 and len(store.list_lessons()) == 1
    with pytest.raises(service_module.LessonNotFound):
        await service.review("task-2", verdict="redo", cause="brief", lesson_id="l-none")


def test_reviews_survive_a_full_inbox(service: DotService) -> None:
    store = service.store
    for index in range(8):
        title = f"Paper {index} " + "p" * 100
        store.upsert_work(
            f"task-{index}", title=title, status="review", result_card="c", review_due=True
        )
    for index in range(7):
        store.add_focus(f"Goal {index}", why="y" * 150)
    for index in range(12):
        store.add_inbox("task_done", f"event {index} " + "x" * 390)
    built = build_turn_pack(store, None, "", first_turn=False, session_turn=2)
    assert all(f"- [task-{index}] Paper {index}" in built.text for index in range(8))
    assert "a brief miss needs `--lesson`" in built.text
    assert len(built.text) <= 5000
    assert 0 < len(built.delivered_event_ids) < 12, "the rest of the events wait for a later turn"


def test_old_lessons_leave_the_session_pack(service: DotService, monkeypatch: MonkeyPatch) -> None:
    lesson = service.store.add_lesson("Say where the result goes")
    assert "Say where the result goes" in build_session_pack(service.store)
    later = utc_now() + timedelta(days=LESSON_HALF_LIFE_DAYS * 1.5)
    monkeypatch.setattr("claude_hub.services.dot.context.utc_now", lambda: later)
    assert "Say where the result goes" not in build_session_pack(service.store)
    assert service.store.get_lesson(lesson["id"]) is not None, "faded lessons stay as history"


@pytest.mark.asyncio
async def test_watching_a_delegated_task_keeps_its_review(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    task = SimpleNamespace(
        id="task-d",
        workspace_id="ws",
        title="d",
        agent_type=SimpleNamespace(value="codex"),
        status=SimpleNamespace(value="review"),
    )
    monkeypatch.setattr(workspace_manager, "tasks", {"task-d": task})
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _tid: [])
    store.upsert_work("task-d", workspace_id="ws", title="d", status="review", result_card="c",
                      review_due=True, last_report_id="r1")  # fmt: skip
    item = await service.watch("task-d", stall_hours=4)
    assert not item.get("watched") and item["review_due"] and item["last_report_id"] == "r1"
    assert item["stall_hours"] == 4
    assert [entry["task_id"] for entry in store.pending_reviews()] == ["task-d"]
    await service.review("task-d", verdict="ok", note="fine")
    # A watched task is never judged, even if an old file marks it due.
    store.upsert_work("task-w", status="review", watched=True, review_due=True)
    assert store.pending_reviews() == []


def _hub_request(report_id: str, message: str, reason: Optional[str] = None) -> SimpleNamespace:
    report = _report(report_id, "ready_for_review", message)
    report.validation = None
    report.risks = None
    report.review_decision = SimpleNamespace(value="request")
    report.review_reason = reason or message
    return report


@pytest.mark.asyncio
async def test_hub_review_requests_are_not_results(
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

    for task_id in ("task-1", "task-2"):
        store.upsert_work(task_id, workspace_id="ws", title=task_id, status="working")
        tasks[task_id] = task("working")
    tasks["task-1"] = task("review")
    reports["task-1"] = [_report("w1", "ready_for_review", "1. ✓ table written")]
    await service.check_work()
    store.mark_inbox_read()

    # The user asks the Hub reviewer to look again: no new round, the card keeps the work.
    reports["task-1"].append(_hub_request("h1", "Human requested reviewer checks."))
    # task-2 is moved to review by hand; its result is what the worker said last.
    tasks["task-2"] = task("review")
    reports["task-2"] = [
        _report("w2", "working", "half the rows done"),
        _hub_request("h2", "Task manually moved to review status.",
                     "Manual status transition to REVIEW."),  # fmt: skip
    ]
    await service.check_work()
    events = store.list_inbox()
    assert [(event["kind"], event["task_id"]) for event in events] == [("task_ready", "task-2")]
    assert "half the rows done" in events[0]["text"]
    for task_id, said in (("task-1", "table written"), ("task-2", "half the rows done")):
        card = store.get_card(store.get_work(task_id)["result_card"])  # type: ignore[index]
        assert card is not None and said in card.body and "Human requested" not in card.body


@pytest.mark.asyncio
async def test_one_bad_task_does_not_stop_the_watcher(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    huge = _report("r1", "ready_for_review", "x" * 300_000)
    huge.validation = "v" * 300_000

    def reports(_ws: str, task_id: str) -> list[Any]:
        if task_id == "task-bad":
            raise RuntimeError("broken report file")
        return [huge] if task_id == "task-big" else []

    review = SimpleNamespace(status=SimpleNamespace(value="review"), title="t")
    monkeypatch.setattr(workspace_manager, "tasks", {"task-bad": review, "task-big": review})
    monkeypatch.setattr(workspace_manager, "reports_for_task", reports)
    for task_id in ("task-bad", "task-big"):
        store.upsert_work(task_id, workspace_id="ws", title=task_id, status="working")
    assert await service.check_work() == 1
    card = store.get_card(store.get_work("task-big")["result_card"])  # type: ignore[index]
    assert card is not None and len(card.body) < 50_000


@pytest.mark.asyncio
async def test_a_reviewer_question_is_news_but_not_a_worker_question(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    reports = [_report("q1", "review_needs_input", "Which baseline counts?")]
    task = SimpleNamespace(status=SimpleNamespace(value="review"), title="t")
    monkeypatch.setattr(workspace_manager, "tasks", {"task-q": task})
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _tid: reports)
    store.upsert_work("task-q", workspace_id="ws", title="q", status="review", result_card="c",
                      review_due=True, last_report_id="r0")  # fmt: skip
    await service.check_work()
    assert [event["kind"] for event in store.list_inbox()] == ["task_needs_input"]
    assert not store.get_work("task-q").get("asked")  # type: ignore[union-attr]


def test_a_tidy_turn_leaves_events_for_the_user(service: DotService) -> None:
    event = service.store.add_inbox("task_done", "Paper — done", task_id="task-1")
    plan = service.prepare_turn("[Dot tidy] Nightly memory tidy.", "conv-1")
    assert plan.delivered_event_ids == [] and "Paper — done" not in plan.turn_pack
    plan = service.prepare_turn("hi", None)
    assert plan.delivered_event_ids == [event["id"]]


def test_a_broken_hand_edit_is_kept_aside_not_erased(service: DotService) -> None:
    store = service.store
    (store.home / "work.json").write_text('[{"task_id": "task-1",}]', encoding="utf-8")
    assert store.list_work() == []
    store.upsert_work("task-2", status="working")
    kept = list(store.home.glob("work.json.corrupt-*"))
    assert len(kept) == 1 and '"task-1"' in kept[0].read_text(encoding="utf-8")
    assert [item["task_id"] for item in store.list_work()] == ["task-2"]

    # Stray values inside a list, odd numbers and a read event without an id do not crash.
    (store.home / "focus.json").write_text(
        '[1, {"id": "f-1", "title": "a", "priority": "high"}]', encoding="utf-8"
    )
    assert [item["id"] for item in store.list_focus()] == ["f-1"]
    store.add_focus("b")
    (store.home / "inbox.json").write_text(
        json.dumps([{"read": True} for _ in range(201)]), encoding="utf-8"
    )
    store.add_inbox("task_done", "x")
    assert lesson_score({"weight": 10**400, "votes": 2}, utc_now()) == 2
    cards = store.memory_dir / "knowledge"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "k.md").write_text(
        '---\nid: "k"\ntitle: "k"\nimportance: 1e999\n---\nbody\n', encoding="utf-8"
    )
    (cards / "latin1.md").write_bytes(b'---\nid: "x"\ntitle: "caf\xe9"\n---\n')
    assert store.get_card("k") is not None and store.get_card("latin1") is None
    assert [card.id for card in store.list_cards()] == ["k"]


@pytest.mark.asyncio
async def test_the_stuck_review_reaper_and_late_hub_requests(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    reports: dict[str, list[Any]] = {}
    review = SimpleNamespace(status=SimpleNamespace(value="review"), title="t")
    monkeypatch.setattr(workspace_manager, "tasks", {"task-1": review, "task-2": review})
    monkeypatch.setattr(
        workspace_manager, "reports_for_task", lambda _ws, tid: reports.get(tid, [])
    )
    for task_id in ("task-1", "task-2"):
        store.upsert_work(task_id, workspace_id="ws", title=task_id, status="working")
        reports[task_id] = [_report(f"{task_id}-w1", "ready_for_review", "1. ✓ first try")]
    await service.check_work()
    store.mark_inbox_read()

    # task-1: the reaper re-sends the stuck review, copying a question state.
    reaper = _hub_request(
        "h1",
        "Re-dispatching stuck review task (fallback reaper); prior reviewer dispatch did not "
        "complete.",
        "Stuck review recovered by background dispatcher.",
    )
    reaper.state = SimpleNamespace(value="needs_input")
    reports["task-1"].append(reaper)
    # task-2: a second worker return and then a Hub request land in the same poll.
    reports["task-2"] += [
        _report("task-2-w2", "ready_for_review", "1. ✓ second try"),
        _hub_request("h2", "Human requested reviewer checks."),
    ]
    await service.check_work()
    events = store.list_inbox()
    assert [(event["kind"], event["task_id"]) for event in events] == [("task_ready", "task-2")]
    assert "second try" in events[0]["text"]
    assert not store.get_work("task-1").get("asked")  # type: ignore[union-attr]
    card = store.get_card(store.get_work("task-1")["result_card"])  # type: ignore[index]
    assert card is not None and "first try" in card.body and "reaper" not in card.body


@pytest.mark.asyncio
async def test_a_failed_write_does_not_repeat_the_event(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    review = SimpleNamespace(status=SimpleNamespace(value="review"), title="t")
    monkeypatch.setattr(workspace_manager, "tasks", {"task-1": review})
    monkeypatch.setattr(
        workspace_manager,
        "reports_for_task",
        lambda _ws, _tid: [_report("r1", "ready_for_review", "ok")],
    )
    store.upsert_work("task-1", workspace_id="ws", title="t", status="working")
    upsert = store.upsert_work
    failures = [OSError("disk full")]

    def flaky(task_id: str, **fields: Any) -> dict[str, Any]:
        if failures and "result_card" in fields:
            raise failures.pop()
        return upsert(task_id, **fields)

    monkeypatch.setattr(store, "upsert_work", flaky)
    assert await service.check_work() == 0 and len(store.list_inbox()) == 1
    assert await service.check_work() == 1
    assert await service.check_work() == 0
    assert len(store.list_inbox()) == 1

    # The other way round: a failed event write leaves the change to be seen again.
    monkeypatch.setattr(
        workspace_manager,
        "reports_for_task",
        lambda _ws, _tid: [
            _report("r1", "ready_for_review", "ok"),
            _report("r2", "needs_input", "Which config?"),
        ],
    )
    working = SimpleNamespace(status=SimpleNamespace(value="working"), title="t")
    monkeypatch.setattr(workspace_manager, "tasks", {"task-1": working})
    store.upsert_work("task-1", status="working", closed=False)
    add = store.add_inbox
    lost = [OSError("disk full")]

    def flaky_inbox(kind: str, text: str, **extra: Any) -> dict[str, Any]:
        if lost:
            raise lost.pop()
        return add(kind, text, **extra)

    monkeypatch.setattr(store, "add_inbox", flaky_inbox)
    assert await service.check_work() == 0
    assert await service.check_work() == 1
    assert store.list_inbox()[-1]["kind"] == "task_needs_input"


def test_unreadable_files_are_empty_for_readers_and_errors_for_writers(
    service: DotService,
) -> None:
    store = service.store
    (store.home / "identity.json").mkdir()
    assert store.read_identity()["name"] == "Dot"
    (store.home / "work.json").unlink(missing_ok=True)
    (store.home / "work.json").mkdir()
    assert store.list_work() == []
    with pytest.raises(OSError):
        store.upsert_work("task-1", status="working")
    assert (store.home / "work.json").is_dir()


def test_cards_round_trip_odd_line_breaks_and_quick_edits(service: DotService) -> None:
    import os
    import time

    from claude_hub.services.dot.index import DotIndex
    from claude_hub.services.dot.store import Card

    store = service.store
    title = "Line\u2028separator \x85 and\u2029end"
    body = "page\x0cbreak and \x1c group\nnext line"
    card = store.save_card(Card(id="", kind="knowledge", title=title, body=body))
    again = store.get_card(card.id)
    assert again is not None and (again.title, again.body) == (title, body)

    # Two saves within the clock's resolution: the index must see the second one.
    index = DotIndex(store)
    path = store.find_card_path(card.id)
    assert path is not None
    stamp = time.time_ns()
    os.utime(path, ns=(stamp, stamp))
    assert [hit.id for hit in index.search("separator")] == [card.id]
    store.save_card(Card(id=card.id, kind="knowledge", title=title.replace("separator", "divider"),
                         body=body))  # fmt: skip
    os.utime(path, ns=(stamp, stamp))
    assert [hit.id for hit in index.search("divider")] == [card.id]


@pytest.mark.asyncio
async def test_hub_reviewer_steps_are_not_new_results(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    reports: list[Any] = []
    tasks: dict[str, Any] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _id: reports)

    def poll(status: str, *new: Any, **times: Any) -> Any:
        tasks["task-r"] = SimpleNamespace(status=SimpleNamespace(value=status), title="t", **times)
        reports.extend(new)
        return service.check_work()

    store.upsert_work("task-r", workspace_id="ws", title="Port", status="working")
    await poll("review", _report("r1", "ready_for_review", "ported"))
    # The Hub reviewer starts (task back to working), then asks the user something.
    await poll("working", _report("r2", "review_started", "Reviewer is checking"))
    await poll("review", _report("r3", "review_needs_input", "Which API version?"))
    events = store.list_inbox()
    assert [event["kind"] for event in events] == ["task_ready", "task_needs_input"]
    assert "Which API version?" in events[1]["text"]
    # The reviewer passes it and a person accepts it: the same result, no news.
    await poll("working", _report("r4", "review_started", "again"))
    await poll("done", _report("r5", "review_passed", "fine"), completed_at=utc_now())
    assert len(store.list_inbox()) == 2
    # Reopened and accepted again between two polls, with nothing new: still no news.
    await poll("done", completed_at=utc_now() + timedelta(minutes=5))
    assert len(store.list_inbox()) == 2


@pytest.mark.asyncio
async def test_progress_after_a_result_is_not_the_result(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    reports: list[Any] = []
    tasks: dict[str, Any] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _id: reports)

    def poll(status: str, *new: Any) -> Any:
        tasks["task-p"] = SimpleNamespace(status=SimpleNamespace(value=status), title="t")
        reports.extend(new)
        return service.check_work()

    def card_body() -> str:
        card = store.get_card(store.get_work("task-p")["result_card"])  # type: ignore[index]
        assert card is not None
        return card.body

    store.upsert_work("task-p", workspace_id="ws", title="Port", status="working")
    await poll("review", _report("r1", "ready_for_review", "ported"))
    await service.review("task-p", verdict="ok")
    # The worker posts progress while in review; the Hub reviewer then goes over it.
    await poll("review", _report("r2", "working", "still polishing"))
    await poll("working", _report("r3", "review_started", "checking"))
    await poll("review", _report("r4", "review_passed", "fine"))
    assert len(store.list_inbox()) == 1
    assert "ported" in card_body() and "still polishing" not in card_body()
    assert store.pending_reviews() == []
    # A new result followed by progress in one poll is a new round, shown by its result.
    await poll("review", _report("r5", "completed", "ported again"), _report("r6", "working", "x"))
    assert [event["kind"] for event in store.list_inbox()] == ["task_ready", "task_ready"]
    assert "ported again" in card_body()


@pytest.mark.asyncio
async def test_what_the_watcher_shows_after_send_backs_and_failures(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    reports: dict[str, list[Any]] = {}
    tasks: dict[str, Any] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(
        workspace_manager, "reports_for_task", lambda _ws, tid: reports.setdefault(tid, [])
    )

    def poll(task_id: str, status: str, *new: Any, **extra: Any) -> Any:
        tasks[task_id] = SimpleNamespace(status=SimpleNamespace(value=status), title="t", **extra)
        reports.setdefault(task_id, []).extend(new)
        return service.check_work()

    def kinds() -> list[str]:
        return [event["kind"] for event in store.list_inbox()]

    def card(task_id: str) -> str:
        found = store.get_card(store.get_work(task_id)["result_card"])  # type: ignore[index]
        assert found is not None
        return found.body

    # Sent back, some progress, then moved to review by hand: the card shows the
    # newest word, not the old round's result as if it were new.
    store.upsert_work("a", workspace_id="ws", title="A", status="working")
    await poll("a", "review", _report("a1", "completed", "round one done"))
    await service.review("a", verdict="ok")
    await poll("a", "working", _report("a2", "working", "Task continued from review"))
    await poll("a", "review", _report("a3", "working", "half done, API error loop"))
    assert kinds() == ["task_ready", "task_ready"]
    assert "half done" in card("a") and store.get_work("a")["review_due"]  # type: ignore[index]

    # A reviewer question, then late worker progress in the same poll: still asked.
    store.upsert_work("b", workspace_id="ws", title="B", status="working")
    await poll("b", "review", _report("b1", "completed", "done"))
    await poll("b", "working", _report("b2", "review_started", "checking"))
    await poll(
        "b",
        "review",
        _report("b3", "review_needs_input", "Which API version?"),
        _report("b4", "working", "polishing"),
    )
    assert kinds()[-2:] == ["task_ready", "task_needs_input"]

    # Sent back, a new result and accepted, all between two polls: the new result.
    store.upsert_work("c", workspace_id="ws", title="C", status="working")
    await poll("c", "review", _report("c1", "completed", "first try"))
    await poll("c", "done", _report("c2", "completed", "second try"), completed_at=utc_now())
    assert kinds()[-1] == "task_done" and "second try" in card("c")

    # The Hub failed it (session died) after a continue: the card says why.
    store.upsert_work("d", workspace_id="ws", title="D", status="working")
    await poll(
        "d",
        "failed",
        _report("d1", "working", "Task continued after failure"),
        failed_at=utc_now(),
        failure_reason="session died",
    )
    events = store.list_inbox()
    assert events[-1]["kind"] == "task_failed" and "session died" in events[-1]["text"]
    assert "## Failure\nsession died" in card("d")

    # Moved to failed by hand (no end time) after a result in review: still news.
    store.upsert_work("e", workspace_id="ws", title="E", status="working")
    await poll("e", "review", _report("e1", "completed", "done"))
    await poll("e", "failed")
    assert kinds()[-1] == "task_failed"


@pytest.mark.asyncio
async def test_watching_keeps_old_results_and_unseen_reports_as_they_were(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    store = service.store
    reports: list[Any] = []
    tasks: dict[str, Any] = {}
    monkeypatch.setattr(workspace_manager, "tasks", tasks)
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _id: reports)

    def put(status: str, *new: Any) -> None:
        tasks["task-w"] = SimpleNamespace(
            id="task-w",
            workspace_id="ws",
            status=SimpleNamespace(value=status),
            title="t",
            agent_type=SimpleNamespace(value="claude"),
        )
        reports.extend(new)

    # Watched while in review: the Hub reviewer going over the old result is not news.
    put("review", _report("r1", "ready_for_review", "old result"))
    await service.watch("task-w")
    put("working", _report("r2", "review_started", "checking"))
    await service.check_work()
    put("review", _report("r3", "review_passed", "fine"))
    await service.check_work()
    assert store.list_inbox() == []
    # Watching it again before the next poll keeps the reports it has not seen.
    put("working", _report("r4", "needs_input", "Which branch?"))
    await service.watch("task-w")
    await service.check_work()
    assert [event["kind"] for event in store.list_inbox()] == ["task_needs_input"]


def test_turn_fills_a_cold_index_outside_the_store_lock(
    service: DotService, monkeypatch: MonkeyPatch
) -> None:
    import threading

    free: list[bool] = []
    sync = service.index.sync

    def try_lock() -> None:
        got = service.store.lock.acquire(blocking=False)
        if got:
            service.store.lock.release()
        free.append(got)

    def spy() -> int:
        probe = threading.Thread(target=try_lock)
        probe.start()
        probe.join()
        return sync()

    monkeypatch.setattr(service.index, "sync", spy)
    service.prepare_turn("what about the paper notes", "conv-1")
    assert free and free[0], "the first (slow) sync runs without the store lock"

    # A locked index costs the recall, not the turn's pack (inbox, review, rotation).
    import sqlite3

    def locked(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    service.store.add_inbox("task_ready", "Port — ported", key="k1")
    monkeypatch.setattr(service.index, "sync", locked)
    monkeypatch.setattr(service.index, "search", locked)
    plan = service.prepare_turn("what about the paper notes", "conv-1")
    assert "Port — ported" in plan.turn_pack and plan.delivered_event_ids


def test_broken_journal_index_and_folders_heal(service: DotService) -> None:
    import shutil

    from claude_hub.services.dot.store import Card

    store = service.store
    store.append_journal("you", "remember the flash attention notes")
    # A write cut off in the middle of a character.
    with store.journal_path(store.journal_days()[-1]).open("ab") as stream:
        stream.write("## 23:59 · dot\n\nhalf é".encode()[:-1])
    assert "flash attention" in " ".join(store.recent_journal())

    # The index is rebuilt from the cards when its file is garbage.
    card = store.save_card(Card(id="", kind="knowledge", title="Flash attention notes"))
    assert [hit.id for hit in service.index.search("flash")] == [card.id]
    store.index_path.write_bytes(b"not a database at all" * 100)
    assert [hit.id for hit in service.index.search("flash")] == [card.id]
    # A good header with damaged pages inside opens fine and fails on the first read.
    for number in range(40):
        store.save_card(Card(id="", kind="knowledge", title=f"Filler {number}", body="x" * 3000))
    assert service.index.count() == 41
    data = bytearray(store.index_path.read_bytes())
    for start in range(8192, len(data) - 4096, 4096):
        data[start : start + 200] = b"\xff" * 200
    store.index_path.write_bytes(bytes(data))
    assert service.index.count() == 41
    assert [hit.id for hit in service.index.search("flash")] == [card.id]

    # Folders removed by hand while the server runs come back on the next write.
    shutil.rmtree(store.journal_dir)
    shutil.rmtree(store.files_dir, ignore_errors=True)
    store.append_journal("dot", "still here")
    assert store.recent_journal()[-1].endswith("still here")
    assert store.store_file(b"data", "a.txt").startswith("files/")

    # A card file an editor saved with a byte order mark is still a card.
    path = store.memory_dir / "knowledge" / f"{card.id}.md"
    path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert store.get_card(card.id) is not None

    # A state.json nested too deep is no Dot tab, not a crash in every Chat turn.
    (store.home / "state.json").write_text("[" * 200_000, encoding="utf-8")
    assert service.is_dot_tab("tab-1") is False


def test_common_words_and_wake_ups_recall_nothing(service: DotService) -> None:
    from claude_hub.services.dot.store import Card

    store = service.store
    store.save_card(Card(id="", kind="knowledge", title="Plans", body="what the team will do"))
    assert service.index.search("what is the") == []
    assert service.index.search("what is the team plan")
    plan = service.prepare_turn("what is the team plan", "conv-1")
    assert "Memory that may help" in plan.turn_pack
    wake = service.prepare_turn(service_module.WAKE_MESSAGE, "conv-1")
    assert "Memory that may help" not in wake.turn_pack
    # "hi" would match "this" as a substring; a short CJK word still recalls.
    store.save_card(Card(id="", kind="knowledge", title="this week", summary="論文 notes"))
    assert "Memory that may help" not in service.prepare_turn("hi", "conv-1").turn_pack
    assert "論文 notes" in service.prepare_turn("論文", "conv-1").turn_pack
