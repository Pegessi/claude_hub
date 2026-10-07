"""Migration regressions for the per-Bot message deduplication store.

The pre-pool build keyed claims by bare ``message_id``; the pool build keys
them by ``f"{bot_id}:{message_id}"``. Feishu re-delivers an event for up to 300
seconds after ``create_time``, and a user can finish re-pairing well inside
that window, so the two namespaces have to overlap for as long as the replay
window can reach. These tests pin the shape of that overlap: bot-agnostic for
migrated ids, strictly per-Bot for everything new.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_hub.services.feishu_bot import (
    _LEGACY_CLAIM_RETENTION_SECONDS,
    FeishuBotError,
    FeishuMessageDedupStore,
)

_T0 = 1_700_000_000.0


class _ClockedDedupStore(FeishuMessageDedupStore):
    """Store with a movable clock, so retention can be tested without sleeping."""

    def __init__(self, path: Path, now: float) -> None:
        self.clock = now
        super().__init__(path, now=lambda: self.clock)


def _write_v1(path: Path, events: dict[str, Any], **extra: Any) -> None:
    payload: dict[str, Any] = {"version": 1, "events": events}
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_migrated_message_id_is_still_claimed(tmp_path: Path) -> None:
    path = tmp_path / "feishu_bot_events.json"
    _write_v1(
        path,
        {"om-1": {"claimed_at": _T0, "status": "completed", "finished_at": _T0 + 1}},
    )
    store = _ClockedDedupStore(path, _T0 + 120)

    assert store.claim("bot-a:om-1") is False


def test_migrated_unfinished_claim_is_still_claimed(tmp_path: Path) -> None:
    """A v1 claim left mid-flight must not become re-deliverable.

    The old build treated the presence of the key as the claim, whatever its
    status: a crash between claim and finish meant the message was dropped, not
    retried. Migrating only completed claims would quietly turn at-most-once
    into at-least-once for exactly the messages that already went wrong once.
    """

    path = tmp_path / "feishu_bot_events.json"
    _write_v1(path, {"om-2": {"claimed_at": _T0, "status": "processing"}})
    store = _ClockedDedupStore(path, _T0 + 120)

    assert store.claim("bot-a:om-2") is False


def test_migrated_claim_blocks_a_different_bot(tmp_path: Path) -> None:
    """Deliberate: re-pairing may land on a Bot that did not exist under v1."""

    path = tmp_path / "feishu_bot_events.json"
    _write_v1(path, {"om-3": {"claimed_at": _T0, "status": "completed"}})
    store = _ClockedDedupStore(path, _T0 + 120)

    assert store.claim("bot-a:om-3") is False
    assert store.claim("bot-b:om-3") is False
    assert set(_read(path)["legacy_events"]) == {"om-3"}


def test_legacy_set_survives_the_rewrite_to_v2(tmp_path: Path) -> None:
    """The first claim rewrites the file as v2; protection must not be lost.

    Without this, any unrelated message would persist a v2 file and silently
    discard the migrated ids, reopening the window after a single callback.
    """

    path = tmp_path / "feishu_bot_events.json"
    _write_v1(path, {"om-4": {"claimed_at": _T0, "status": "completed"}})
    first = _ClockedDedupStore(path, _T0 + 60)
    assert first.claim("bot-a:om-5") is True
    assert _read(path)["version"] == 2

    second = _ClockedDedupStore(path, _T0 + 120)
    assert second.claim("bot-a:om-4") is False


def test_migrated_claims_expire_and_release_the_id(tmp_path: Path) -> None:
    path = tmp_path / "feishu_bot_events.json"
    _write_v1(path, {"om-6": {"claimed_at": _T0, "status": "completed"}})
    store = _ClockedDedupStore(path, _T0 + _LEGACY_CLAIM_RETENTION_SECONDS + 1)

    assert store.claim("bot-a:om-6") is True
    saved = _read(path)
    assert saved["legacy_events"] == {}
    assert "bot-a:om-6" in saved["events"]


def test_a_stale_upgrade_migrates_nothing(tmp_path: Path) -> None:
    """Claims older than the window are dropped on the first load.

    Retention runs on the timestamp the old build wrote, so a long outage
    empties the set. Feishu's own replay window has expired by then, which
    leaves nothing to protect and no reason to keep blocking those ids.
    """

    path = tmp_path / "feishu_bot_events.json"
    _write_v1(
        path,
        {
            "om-7": {
                "claimed_at": _T0 - _LEGACY_CLAIM_RETENTION_SECONDS - 1,
                "status": "completed",
            }
        },
    )
    store = _ClockedDedupStore(path, _T0)

    assert store.claim("bot-a:om-7") is True
    assert _read(path)["legacy_events"] == {}


def test_new_claims_never_enter_the_legacy_set(tmp_path: Path) -> None:
    """New traffic stays per-Bot even while the compatibility set is active."""

    path = tmp_path / "feishu_bot_events.json"
    _write_v1(path, {"om-8": {"claimed_at": _T0, "status": "completed"}})
    store = _ClockedDedupStore(path, _T0 + 120)

    assert store.claim("bot-a:om-9") is True
    saved = _read(path)
    assert set(saved["legacy_events"]) == {"om-8"}
    assert set(saved["events"]) == {"bot-a:om-9"}

    assert store.claim("bot-b:om-9") is True
    assert set(_read(path)["legacy_events"]) == {"om-8"}


def test_per_bot_namespace_is_unchanged_without_a_migration(tmp_path: Path) -> None:
    path = tmp_path / "feishu_bot_events.json"
    store = _ClockedDedupStore(path, _T0)

    assert store.claim("bot-a:om-10") is True
    assert store.claim("bot-a:om-10") is False
    assert store.claim("bot-b:om-10") is True


def test_migration_drops_v1_pairing_state(tmp_path: Path) -> None:
    """Pairing state moved into the pool record and must not linger here."""

    path = tmp_path / "feishu_bot_events.json"
    _write_v1(
        path,
        {"om-11": {"claimed_at": _T0, "status": "completed"}},
        pending={"code": "ABC123", "expires_at": _T0 + 300},
        bindings={"tab-1": {"chat_id": "oc-1", "open_id": "ou-1"}},
        rate_limits={"oc-1": [_T0]},
    )
    store = _ClockedDedupStore(path, _T0 + 120)

    assert store.claim("bot-a:om-12") is True
    saved = _read(path)
    assert saved["version"] == 2
    assert set(saved) == {"version", "events", "legacy_events"}


def test_unreadable_legacy_set_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "feishu_bot_events.json"
    path.write_text(json.dumps({"version": 2, "events": {}, "legacy_events": []}), encoding="utf-8")
    store = _ClockedDedupStore(path, _T0)

    with pytest.raises(FeishuBotError):
        store.claim("bot-a:om-13")


@pytest.mark.parametrize("operation", ["claim", "finish"])
def test_malformed_legacy_entries_fail_closed_without_rewrite(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / "feishu_bot_events.json"
    _write_v1(path, {"om-14": "not-a-record"})
    before = path.read_bytes()
    store = _ClockedDedupStore(path, _T0)

    with pytest.raises(FeishuBotError):
        if operation == "claim":
            store.claim("bot-a:om-14")
        else:
            store.finish("bot-a:om-14", "completed")
    assert path.read_bytes() == before


_BAD_RECORDS = [
    pytest.param(None, id="null-record"),
    pytest.param([], id="list-record"),
    pytest.param({}, id="missing-fields"),
    pytest.param({"status": "processing"}, id="missing-time"),
    pytest.param({"claimed_at": _T0}, id="missing-status"),
    pytest.param({"claimed_at": True, "status": "processing"}, id="bool-time"),
    pytest.param({"claimed_at": "1700000000", "status": "processing"}, id="string-time"),
    pytest.param({"claimed_at": float("nan"), "status": "processing"}, id="nan-time"),
    pytest.param({"claimed_at": float("inf"), "status": "processing"}, id="infinite-time"),
    pytest.param({"claimed_at": 10**400, "status": "processing"}, id="overflow-time"),
    pytest.param({"claimed_at": 1e100, "status": "processing"}, id="out-of-range-time"),
    pytest.param({"claimed_at": -1, "status": "processing"}, id="negative-time"),
    pytest.param({"claimed_at": _T0, "status": None}, id="invalid-status"),
    pytest.param({"claimed_at": _T0, "status": " "}, id="blank-status"),
    pytest.param(
        {"claimed_at": _T0, "status": "completed", "finished_at": float("inf")},
        id="bad-finish-time",
    ),
    # A plausibly expired timestamp is not permission to erase a malformed row.
    pytest.param({"claimed_at": 0, "status": []}, id="expired-but-malformed"),
]


@pytest.mark.parametrize("record", _BAD_RECORDS)
@pytest.mark.parametrize("location", ["v1", "v2", "legacy-v2"])
@pytest.mark.parametrize("operation", ["claim", "finish"])
def test_bad_authority_record_is_never_pruned_into_a_fresh_claim(
    tmp_path: Path,
    record: Any,
    location: str,
    operation: str,
) -> None:
    path = tmp_path / "events.json"
    if location == "v1":
        state = {"version": 1, "events": {"om-old": record}}
    elif location == "v2":
        state = {"version": 2, "events": {"bot-a:om-old": record}, "legacy_events": {}}
    else:
        state = {"version": 2, "events": {}, "legacy_events": {"om-old": record}}
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()
    store = _ClockedDedupStore(path, _T0 + _LEGACY_CLAIM_RETENTION_SECONDS + 1)
    with pytest.raises(FeishuBotError):
        if operation == "claim":
            store.claim("bot-a:om-old")
        else:
            store.finish("bot-a:om-old", "completed")
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [True, False, 1.0, 2.0, "1", None])
def test_invalid_dedup_version_is_preserved(tmp_path: Path, version: Any) -> None:
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"version": version, "events": {}}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(FeishuBotError):
        _ClockedDedupStore(path, _T0).claim("bot-a:om-new")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"\xff", id="bad-utf8"),
        pytest.param(b"{", id="bad-json"),
        pytest.param(
            b'{"version":1,"events":{"om-old":{"status":"processing","claimed_at":'
            + b"9" * 5000
            + b"}}}",
            id="excessive-json-integer",
        ),
    ],
)
def test_unreadable_dedup_state_is_controlled(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "events.json"
    path.write_bytes(raw)
    with pytest.raises(FeishuBotError):
        _ClockedDedupStore(path, _T0).claim("bot-a:om-new")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("key", ["om-without-bot", ":om", "bot:", ""])
def test_v2_authority_key_requires_a_namespace(tmp_path: Path, key: str) -> None:
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "events": {key: {"claimed_at": _T0, "status": "processing"}},
                "legacy_events": {},
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    with pytest.raises(FeishuBotError):
        _ClockedDedupStore(path, _T0).claim("bot-a:om-new")
    assert path.read_bytes() == before


def test_finish_preserves_original_claim_time(tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    store = _ClockedDedupStore(path, _T0)
    assert store.claim("bot-a:om-new")
    store.clock = _T0 + 20
    store.finish("bot-a:om-new", "completed")
    assert _read(path)["events"]["bot-a:om-new"] == {
        "claimed_at": _T0,
        "status": "completed",
        "finished_at": _T0 + 20,
    }
    assert not _ClockedDedupStore(path, _T0 + 30).claim("bot-a:om-new")
