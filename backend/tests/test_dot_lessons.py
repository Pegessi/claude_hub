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
