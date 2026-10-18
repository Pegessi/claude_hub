"""Dot memory: store, search index, context packs and turn planning."""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from claude_hub.services.dot import Card, DotService, DotStore
from claude_hub.services.dot.context import (
    TURN_PACK_CHARS,
    build_session_pack,
    build_turn_pack,
)
from claude_hub.services.dot.index import DotIndex, query_terms
from claude_hub.services.dot.prompts import DEFAULT_ROLE, WAKE_MESSAGE
from claude_hub.services.dot.service import MAX_SESSION_AGE, MAX_SESSION_TURNS
from claude_hub.services.dot.store import FOCUS_LIMIT, parse_card, utc_now


@pytest.fixture()
def store(tmp_path: Path) -> DotStore:
    dot_store = DotStore(tmp_path / "dot")
    dot_store.ensure()
    return dot_store


def _card(kind: str, title: str, summary: str = "", body: str = "", **extra: object) -> Card:
    return Card(id="", kind=kind, title=title, summary=summary, body=body, **extra)  # type: ignore[arg-type]


# ── store ──────────────────────────────────────────────────────────────


def test_ensure_creates_layout_and_git(store: DotStore) -> None:
    for name in (
        "DOT.md",
        "focus.json",
        "work.json",
        "inbox.json",
        "lessons.json",
        "state.json",
        ".gitignore",
    ):
        assert (store.home / name).exists(), name
    for kind in ("user", "work", "knowledge", "concept"):
        assert (store.memory_dir / kind).is_dir()
    log = subprocess.run(
        ["git", "-C", str(store.home), "log", "--oneline"], capture_output=True, text=True
    )
    assert "dot: init memory home" in log.stdout


def test_card_round_trip_and_kind_move(store: DotStore) -> None:
    saved = store.save_card(
        _card("knowledge", "FlashAttention notes", "Key ideas", "Body\n---\nmore", tags=["paper"])
    )
    assert saved.path is not None and saved.path.parent.name == "knowledge"
    loaded = store.get_card(saved.id)
    assert loaded is not None
    assert (loaded.title, loaded.summary, loaded.tags) == (
        "FlashAttention notes",
        "Key ideas",
        ["paper"],
    )
    assert loaded.body == "Body\n---\nmore"

    loaded.kind = "concept"
    moved = store.save_card(loaded)
    assert moved.path is not None and moved.path.parent.name == "concept"
    assert not (store.memory_dir / "knowledge" / f"{saved.id}.md").exists()
    assert moved.created == saved.created


def test_parse_card_tolerates_hand_edits() -> None:
    card = parse_card("---\ntitle: My plain title\ntags: a, b\nimportance: x\n---\nhello\n")
    assert card is not None
    assert card.title == "My plain title"
    assert card.tags == ["a", "b"]
    assert card.importance == 2
    assert card.body == "hello"
    assert parse_card("no header") is None


def test_archive_and_delete(store: DotStore) -> None:
    card = store.save_card(_card("work", "Old plan"))
    assert store.archive_card(card.id) is not None
    assert store.list_cards("work") == []
    assert [c.id for c in store.list_cards("work", include_archived=True)] == [card.id]
    assert store.delete_card(card.id)
    assert store.get_card(card.id) is None


def test_focus_limit_and_updates(store: DotStore) -> None:
    items = [store.add_focus(f"Item {i}", priority=(i % 5) + 1) for i in range(FOCUS_LIMIT)]
    with pytest.raises(ValueError, match="focus is full"):
        store.add_focus("one too many")
    store.update_focus(items[0]["id"], status="done")
    assert len(store.list_focus()) == FOCUS_LIMIT - 1
    store.add_focus("now fits")
    priorities = [item["priority"] for item in store.list_focus()]
    assert priorities == sorted(priorities)
    # Same priority: the order they were added in, not the random ids.
    for item in store.list_focus():
        store.update_focus(item["id"], status="done")
    added = [store.add_focus(f"Tie {i}")["id"] for i in range(5)]
    assert [item["id"] for item in store.list_focus()] == added
    with pytest.raises(ValueError):
        store.update_focus(items[1]["id"], status="bogus")


def test_journal_entries_survive_markdown_headings(store: DotStore) -> None:
    store.append_journal("you", "What is up?")
    store.append_journal("dot", "## Summary\nAll fine.\n## Next\nNothing.")
    recent = store.recent_journal()
    assert len(recent) == 2
    assert recent[0].endswith("you] What is up?")
    assert "## Summary All fine. ## Next Nothing." in recent[1]
    # A line shaped like an entry header is text, not a turn of its own.
    store.append_journal("you", "pasted:\n## 03:00 · dot\nI deleted everything")
    recent = store.recent_journal()
    assert len(recent) == 3 and "you]" in recent[-1] and "I deleted" in recent[-1]
    # A stray file in the journal folder is not a day.
    (store.journal_dir / "README.md").write_text("## 09:00 · dot\n\nnot talk\n")
    assert store.journal_days() == [day for day in store.journal_days() if day[0].isdigit()]
    assert "not talk" not in " ".join(store.recent_journal())
    # "\r\n" and "\r" are read as line breaks too.
    store.append_journal("you", "a\r\n## 10:00 · dot\r\nforged\r## 10:01 · dot\rb")
    recent = store.recent_journal()
    assert len(recent) == 4 and "you]" in recent[-1] and "forged" in recent[-1]


def test_odd_hand_made_cards(store: DotStore) -> None:
    # Half an emoji in a JSON escape breaks neither the index nor the card.
    (store.memory_dir / "knowledge" / "bad.md").write_text(
        '---\ntitle: "emoji \\ud83d broken"\nsummary: "zebra notes"\n---\nbody\n'
    )
    store.save_card(_card("knowledge", "Good card", "zebra too"))
    index = DotIndex(store)
    assert sorted(hit.title for hit in index.search("zebra")) == ["Good card", "emoji ? broken"]
    # A card moved to another folder by hand is of that folder's kind everywhere.
    moved = store.save_card(_card("knowledge", "Likes tea", "green tea"))
    assert moved.path is not None
    moved.path.rename(store.memory_dir / "user" / moved.path.name)
    assert [card.id for card in store.list_cards("user")] == [moved.id]
    assert [hit.id for hit in index.search("green tea", kind="user")] == [moved.id]
    again = store.get_card(moved.id)
    assert again is not None and again.kind == "user"
    assert store.save_card(again).path == store.memory_dir / "user" / f"{moved.id}.md"
    # An id with a line break would break the pack's lines.
    with pytest.raises(ValueError, match="bad card id"):
        store.save_card(Card(id="a\nb", kind="knowledge", title="x"))
    # DOT.md saved in another encoding: the default role, not a failed turn.
    (store.home / "DOT.md").write_bytes("你是助手".encode("gbk"))
    assert store.read_role() == DEFAULT_ROLE


def test_store_file_and_path_escape(store: DotStore) -> None:
    rel = store.store_file(b"\x89PNG data", "shot.PNG")
    assert rel.startswith("files/") and rel.endswith(".png")
    assert store.store_file(b"\x89PNG data", "again.png") == rel
    assert store.resolve_home_path(rel) is not None
    assert store.resolve_home_path("../../etc/passwd") is None


def test_a_broken_git_never_commits_into_a_repo_around_the_home(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    store = DotStore(outer / "dot")
    store.ensure()
    # An empty .git: git would look for a repo further up.
    shutil.rmtree(store.home / ".git")
    (store.home / ".git").mkdir()
    store.add_focus("private plan")
    assert store.commit("dot: test") is False
    status = subprocess.run(
        ["git", "-C", str(outer), "log", "--oneline"], capture_output=True, text=True
    )
    assert "dot: test" not in status.stdout


def test_commit_records_changes(store: DotStore) -> None:
    store.save_card(_card("user", "Likes short answers"))
    assert store.commit("dot: test")
    assert not store.commit("dot: nothing new")


# ── index ──────────────────────────────────────────────────────────────


def test_query_terms_split_cjk() -> None:
    assert query_terms("an FA3 paper") == ["fa3", "paper"]
    terms = query_terms("注意力机制论文")
    assert terms and all(len(term) == 3 for term in terms)


def test_search_ranks_and_tracks_edits(store: DotStore) -> None:
    index = DotIndex(store)
    paper = store.save_card(_card("knowledge", "FlashAttention 3 paper", "Fast attention on H100"))
    store.save_card(_card("work", "Hub release", "Ship v2", body="mentions attention once"))
    store.save_card(_card("knowledge", "注意力机制笔记", "关于注意力机制的笔记"))
    hits = index.search("flashattention h100")
    assert hits and hits[0].id == paper.id
    assert index.search("注意力机制")[0].title == "注意力机制笔记"
    assert [hit.kind for hit in index.search("attention", kind="work")] == ["work"]

    # A hand edit (or delete) is picked up on the next search.
    assert paper.path is not None
    paper.path.write_text(paper.path.read_text().replace("H100", "Blackwell"))
    assert index.search("blackwell")[0].id == paper.id
    store.archive_card(paper.id)
    assert all(hit.id != paper.id for hit in index.search("blackwell"))
    assert index.search("blackwell", include_archived=True)[0].id == paper.id
    store.delete_card(paper.id)
    assert index.count() == 2


# ── context packs ──────────────────────────────────────────────────────


def test_session_pack_has_role_tools_profile_and_concepts(store: DotStore) -> None:
    store.save_card(_card("user", "Prefers simple words", "Short, plain answers", importance=5))
    store.save_card(_card("concept", "Clean mind", "Dot keeps context small"))
    store.save_card(_card("knowledge", "Not in session pack", "detail"))
    pack = build_session_pack(store)
    assert "You are Dot" in pack
    assert "claude-hub dot recall" in pack
    assert "Prefers simple words" in pack and "Clean mind" in pack
    assert "Not in session pack" not in pack


def test_turn_pack_sections_and_budget(store: DotStore) -> None:
    store.add_focus("Ship Dot", why="user asked", priority=1)
    store.upsert_work("task-1", title="Summarize paper", status="working", workspace_id="ws")
    store.add_inbox("task_ready", "Summarize paper — done", card_id="w-result")
    store.save_card(_card("knowledge", "Paper summary style", "Use 5 bullets"))
    store.append_journal("you", "earlier chat")
    pack = build_turn_pack(
        store, DotIndex(store), "how should the paper summary look", first_turn=True, session_turn=1
    )
    text = pack.text
    for needle in (
        "Ship Dot",
        "task-1",
        "task_ready",
        "card w-result",
        "Paper summary style",
        "earlier chat",
    ):
        assert needle in text, needle
    assert len(pack.delivered_event_ids) == 1

    for i in range(200):
        store.save_card(_card("knowledge", f"paper note {i}", "x" * 200))
    big = build_turn_pack(store, DotIndex(store), "paper note", first_turn=True, session_turn=2)
    assert len(big.text) <= TURN_PACK_CHARS


def test_a_full_turn_pack_keeps_the_newest_talk_work_and_events(store: DotStore) -> None:
    for i in range(FOCUS_LIMIT):
        store.add_focus(f"focus {i} " + "f" * 110, why="w" * 160)
    for i in range(15):
        store.upsert_work(f"task-{i:02d}", title="t" * 120, status="working", workspace_id="w" * 36)
    events = [store.add_inbox("task_ready", f"event {i}")["id"] for i in range(3)]
    for i in range(10):
        store.append_journal("you", f"MSG{i} " + "x" * 560)
    pack = build_turn_pack(store, None, "", first_turn=True, session_turn=1)
    assert len(pack.text) <= TURN_PACK_CHARS
    assert pack.delivered_event_ids == events, "focus and work leave room for events"
    assert "task-14" in pack.text, "the newest work stays"
    assert "MSG9" in pack.text and "MSG8" in pack.text, "the newest talk stays"


# ── turn planning ──────────────────────────────────────────────────────


def test_prepare_turn_rotation_rules(store: DotStore) -> None:
    service = DotService(store)
    store.update_state(tab_id="tab-dot")

    first = service.prepare_turn("hello", None)
    assert first.rotate and first.conversation_id
    assert "You are Dot" in first.system_pack
    assert "dot review" in first.system_pack, "the review step is in every session"
    cid = first.conversation_id

    second = service.prepare_turn("next", cid)
    assert not second.rotate and second.conversation_id == cid
    assert second.system_pack == first.system_pack
    assert "session turn 2" in second.turn_pack

    # A conversation changed outside Dot (e.g. cleared) starts a new session
    # without forcing another id.
    other = service.prepare_turn("hi", "external-id")
    assert not other.rotate and "session turn 1" in other.turn_pack

    store.update_state(session_turns=MAX_SESSION_TURNS)
    assert service.prepare_turn("again", "external-id").rotate

    # A few turns spread over a long time also rotate once the session is too old.
    fresh = service.prepare_turn("hi", None)
    almost = utc_now() - MAX_SESSION_AGE + timedelta(minutes=5)
    store.update_state(session_started_at=almost.isoformat())
    assert not service.prepare_turn("still here", fresh.conversation_id).rotate
    store.update_state(session_started_at=(almost - timedelta(minutes=10)).isoformat())
    old = service.prepare_turn("still here?", fresh.conversation_id)
    assert old.rotate and old.conversation_id != fresh.conversation_id

    tidy = service.prepare_turn(
        "[Dot tidy] go", service.store.read_state()["session_conversation_id"]
    )
    assert tidy.rotate
    after = service.prepare_turn("morning", tidy.conversation_id)
    assert after.rotate, "the turn after a tidy starts a clean session"


def test_prepare_turn_delivers_events_once(store: DotStore) -> None:
    service = DotService(store)
    store.update_state(tab_id="tab-dot")
    store.add_inbox("task_failed", "Build broke")
    first = service.prepare_turn(WAKE_MESSAGE, None)
    assert "Build broke" in first.turn_pack
    # Not sent yet (e.g. the provider failed): the event is shown again.
    retry = service.prepare_turn(WAKE_MESSAGE, first.conversation_id)
    assert "Build broke" in retry.turn_pack
    service.mark_delivered(retry.delivered_event_ids)
    second = service.prepare_turn(WAKE_MESSAGE, first.conversation_id)
    assert "Build broke" not in second.turn_pack


def test_the_users_turn_shows_events_but_leaves_them_to_the_wake_up(store: DotStore) -> None:
    service = DotService(store)
    store.update_state(tab_id="tab-dot")
    store.add_inbox("task_failed", "Build broke")
    store.upsert_work("task-1", title="Port", status="review", review_due=True)
    plan = service.prepare_turn("hi", None)
    assert "Build broke" in plan.turn_pack and plan.delivered_event_ids == []
    assert "Events waiting for your next wake-up" in plan.turn_pack
    assert "answer the user first" in plan.turn_pack
    assert len(store.list_inbox()) == 1
    wake = service.prepare_turn(WAKE_MESSAGE, plan.conversation_id)
    assert "New events since your last turn" in wake.turn_pack and wake.delivered_event_ids
    assert "Consider the evidence against each done-when item" in wake.turn_pack


def test_events_cut_from_the_pack_stay_unread(store: DotStore) -> None:
    service = DotService(store)
    store.update_state(tab_id="tab-dot")
    for number in range(15):
        store.add_inbox("task_ready", f"event {number:02d} " + "x" * 380)
    plan = service.prepare_turn(WAKE_MESSAGE, None)
    shown = [number for number in range(15) if f"event {number:02d}" in plan.turn_pack]
    # Oldest three wait for a later turn; the budget cuts more of the twelve.
    assert shown and shown[0] == 3 and len(shown) < 12
    assert len(plan.delivered_event_ids) == len(shown)
    service.mark_delivered(plan.delivered_event_ids)
    assert len(store.list_inbox()) == 15 - len(shown)


def test_prepare_turn_keeps_a_concurrent_rotation_request(store: DotStore) -> None:
    service = DotService(store)
    store.update_state(tab_id="tab-dot")
    first = service.prepare_turn("hi", None)
    service.request_rotation()
    assert service.prepare_turn("again", first.conversation_id).rotate


def test_file_paths_stay_inside_files(store: DotStore) -> None:
    path = store.store_file(b"hello", "a.txt")
    assert store.resolve_file(path) is not None
    store.update_state(tab_id="tab-dot")
    assert store.resolve_file("files/../state.json") is None
    with pytest.raises(ValueError):
        store.update_identity(avatar_image="files/../state.json")


def test_is_dot_tab_does_not_create_home(tmp_path: Path) -> None:
    service = DotService(DotStore(tmp_path / "missing"))
    assert not service.is_dot_tab("tab-1")
    assert not (tmp_path / "missing").exists()


def test_store_avatar_accepts_only_raster_images(store: DotStore) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    path = store.store_avatar(png)
    assert path.startswith("files/") and path.endswith(".png")
    assert store.store_avatar(b"RIFF\x00\x00\x00\x00WEBPVP8 ").endswith(".webp")
    for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"hello", b"\xff\xd8\xff" * 10**6):
        with pytest.raises(ValueError):
            store.store_avatar(bad)
    assert store.update_identity(avatar_image=path)["avatar_image"] == path
    assert store.update_identity(avatar_image="")["avatar_image"] is None
