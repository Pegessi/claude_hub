"""Compatibility and value contracts for extracted terminal parsing helpers."""

from __future__ import annotations

import importlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from claude_hub import services
from claude_hub.services.terminal_runtime import catalog, records
from claude_hub.services.ttyd_manager import ScanEntry, TabStateError, TTYDManager, TTYDProcess

TM = importlib.import_module("claude_hub.services.ttyd_manager")


def _user_line(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        }
    )


def _entry(
    sid: str,
    *,
    cwd: str = "/work",
    epoch: float | None = 100.0,
    title: str = "rollout title",
    source: str = "user",
) -> ScanEntry:
    return ScanEntry(f"/{sid}.jsonl", 100, 10, cwd, epoch, False, title, source)


def _live_tab(**changes: Any) -> dict[str, Any]:
    return {
        "id": "live-tab",
        "name": "Live",
        "port": 7800,
        "created_at": "2026-10-17T12:00:00+00:00",
        **changes,
    }


def _pending(**changes: str) -> dict[str, str]:
    return {
        "tab_id": "retired-tab",
        "created_at": "2026-10-17T11:00:00+00:00",
        "token": "a" * 32,
        "authorization": TM._TAB_CLEANUP_AUTHORIZATION,
        **changes,
    }


def _snapshot(
    *, tabs: list[dict[str, Any]] | None = None, pending: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "version": TM._TABS_STATE_VERSION,
        "tabs": tabs if tabs is not None else [_live_tab()],
        "pending_stream_cleanup": pending if pending is not None else [_pending()],
    }


def test_terminal_owner_classes_and_singleton_keep_historical_identity() -> None:
    assert services.TTYDManager is TM.TTYDManager is TTYDManager
    assert services.TTYDProcess is TM.TTYDProcess is TTYDProcess
    assert services.ttyd_manager is TM.ttyd_manager
    assert type(TM.ttyd_manager) is TTYDManager
    for cls in (TTYDManager, TTYDProcess):
        assert cls.__module__ == TM.__name__
    assert ScanEntry is records.ScanEntry
    assert TabStateError is records.TabStateError
    assert TM._PendingTabCleanup is records._PendingTabCleanup
    assert TM._codex_id_in_index is TM._codex_id_exists
    assert type(_entry("one")) is ScanEntry


def test_codex_title_callbacks_resolve_from_historical_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classifier = Mock(return_value=("title", "patched title"))
    monkeypatch.setattr(TM, "_codex_classify_and_extract", classifier)
    assert TM._codex_title_from_lines([_user_line("original")]) == "patched title"
    classifier.assert_called_once_with("original")


def test_codex_classification_keeps_title_and_role_patch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = Mock(return_value="Patched task")
    role = Mock(return_value="Patched role")
    monkeypatch.setattr(TM, "_extract_task_title_from_text", title)
    monkeypatch.setattr(TM, "_bootstrap_role_label", role)
    assert TM._codex_classify_and_extract("New workspace task assigned.") == (
        "title",
        "Patched task",
    )
    assert TM._codex_classify_and_extract("You are a resident agent") == (
        "fallback",
        "Patched role",
    )
    title.assert_called_once_with("New workspace task assigned.")
    role.assert_called_once_with("You are a resident agent")


def test_codex_title_policy_constants_keep_patch_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TM, "_CODEX_SKIP_TITLE_PREFIXES", ("skip this",))
    monkeypatch.setattr(TM, "_CODEX_TITLE_MAX_LEN", 5)
    assert TM._codex_title_from_lines([_user_line("abcdef"), _user_line("skip this")]) == "abcd…"
    monkeypatch.setattr(TM, "_CODEX_WORKSPACE_TASK_PREFIXES", ("custom task",))
    monkeypatch.setattr(TM, "_CODEX_WORKSPACE_RECOVERY_PREFIXES", ("custom recovery",))
    monkeypatch.setattr(TM, "_CODEX_TASK_TITLE_RE", re.compile(r"Label: (.+)"))
    assert TM._codex_classify_and_extract("custom task\nLabel: accepted") == ("title", "accepted")
    assert TM._codex_classify_and_extract("custom recovery\nLabel: resumed") == ("title", "resumed")
    monkeypatch.setattr(TM, "_CODEX_WORKSPACE_BOOTSTRAP_PREFIXES", ("custom idle",))
    monkeypatch.setattr(TM, "_bootstrap_role_label", lambda text: "Custom role")
    assert TM._codex_classify_and_extract("custom idle") == ("fallback", "Custom role")


def test_codex_title_regex_constants_keep_patch_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TM, "_CODEX_TASK_INLINE_RE", re.compile(r"Subject: (.+)"))
    monkeypatch.setattr(TM, "_CODEX_WORKSPACE_LINE_RE", re.compile(r"Project: (.+)"))
    assert TM._extract_task_title_from_text("Subject: Inline task") == "Inline task"
    assert TM._bootstrap_role_label("You are a resident agent\nProject: work") == "Agent (work)"


def test_codex_scanner_keeps_parser_title_and_limit_patch_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rollout = tmp_path / "rollout-test.jsonl"
    rollout.write_text("metadata\ntitle\nnot read\n", encoding="utf-8")
    parser = Mock(return_value=("session", 100.0, "/work", "user"))
    title = Mock(return_value="Patched rollout")
    monkeypatch.setattr(TM, "_codex_roots", lambda: [(tmp_path, False)])
    monkeypatch.setattr(TM, "_parse_session_meta", parser)
    monkeypatch.setattr(TM, "_codex_title_from_lines", title)
    monkeypatch.setattr(TM, "_CODEX_TITLE_SCAN_MAX_LINES", 2)
    scan = TM._codex_scan_sessions(with_title=True)
    assert type(scan["session"]) is ScanEntry
    assert scan["session"].title == "Patched rollout"
    parser.assert_called_once_with("metadata\n")
    title.assert_called_once_with(["metadata\n", "title\n"])
    title.reset_mock()
    assert (
        TM._codex_scan_sessions(with_title=True, skip_title_sids={"session"})["session"].title == ""
    )
    title.assert_not_called()


def test_codex_title_file_reader_keeps_patch_and_scan_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("first\nsecond\nthird\n", encoding="utf-8")
    lines_seen: list[str] = []

    def title(lines: Any) -> str:
        lines_seen.extend(lines)
        return "Patched file title"

    monkeypatch.setattr(TM, "_CODEX_TITLE_SCAN_MAX_LINES", 1)
    monkeypatch.setattr(TM, "_codex_title_from_lines", title)
    assert TM._codex_session_title(str(rollout)) == "Patched file title"
    assert lines_seen == ["first\n"]


def test_codex_index_reader_keeps_home_patch_and_latest_valid_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(TM, "_codex_home_dir", lambda: tmp_path)
    assert TM._codex_load_session_index() == {}
    (tmp_path / "session_index.jsonl").write_text(
        '\ninvalid JSON\n{"id":"sid","thread_name":"first"}\n'
        '{"id":"sid","thread_name":"last"}\n{"id":"sid","thread_name":""}\n',
        encoding="utf-8",
    )
    assert TM._codex_load_session_index() == {"sid": "last"}


def test_codex_catalog_keeps_single_cache_and_scan_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    scan = Mock(return_value={"sid": _entry("sid")})
    index = Mock(return_value={"sid": "Index title"})
    key = Mock(return_value=(1, 2))
    monkeypatch.setattr(TM, "_CODEX_SESSIONS_CACHE", None)
    monkeypatch.setattr(TM, "_codex_sessions_cache_key", key)
    monkeypatch.setattr(TM, "_codex_load_session_index", index)
    monkeypatch.setattr(TM, "_codex_scan_sessions", scan)
    first = TM.list_codex_sessions()
    assert first[0]["sessions"][0]["title"] == "Index title"
    assert TM.list_codex_sessions() is first
    assert TM._CODEX_SESSIONS_CACHE[1] is first
    scan.assert_called_once_with(with_title=True, skip_title_sids={"sid"})
    index.assert_called_once_with()
    key.return_value = (1, 3)
    assert TM.list_codex_sessions() == first
    assert scan.call_count == 2


def test_catalog_grouping_is_pure_and_preserves_existing_order_rules() -> None:
    scan = {
        "a-old": _entry("a-old", cwd="/a", epoch=10.0),
        "b-new": _entry("b-new", cwd="/b", epoch=40.0),
        "a-new": _entry("a-new", cwd="/a", epoch=30.0),
        "legacy": _entry("legacy", cwd="", epoch=None, source=""),
        "subagent": _entry("subagent", epoch=90.0, source="subagent"),
        "automation": _entry("automation", epoch=99.0, source="automation"),
    }
    names = {"a-new": "Index title"}
    before_scan, before_names = deepcopy(scan), names.copy()
    groups = catalog.group_sessions(scan, names)
    assert [group["cwd"] for group in groups] == ["/b", "/a", ""]
    assert [session["session_id"] for session in groups[1]["sessions"]] == ["a-new", "a-old"]
    assert groups[1]["sessions"][0]["title"] == "Index title"
    assert groups[0]["sessions"][0]["start_time"] == datetime.fromtimestamp(40.0).isoformat()
    assert groups[2]["sessions"][0]["start_time"] == ""
    assert all("start_epoch" not in session for group in groups for session in group["sessions"])
    assert scan == before_scan
    assert names == before_names


def test_codex_title_preserves_last_fallback_and_first_explicit_task_behavior() -> None:
    assert TM._codex_title_from_lines([_user_line("first"), _user_line("last")]) == "last"
    assert (
        TM._codex_title_from_lines(
            [
                _user_line("fallback"),
                _user_line("New workspace task assigned.\nTask title: Explicit title"),
                _user_line("later fallback"),
            ]
        )
        == "Explicit title"
    )


def test_snapshot_parser_preserves_record_identity_input_and_legacy_list() -> None:
    value = _snapshot()
    before = deepcopy(value)
    tabs, pending = TM._parse_tabs_snapshot(value)
    assert tabs is value["tabs"]
    assert type(pending["retired-tab"]) is TM._PendingTabCleanup
    assert asdict(pending["retired-tab"]) == value["pending_stream_cleanup"][0]
    assert value == before
    assert TM._parse_tabs_snapshot(tabs) == (tabs, {})


@pytest.mark.parametrize(
    "value, message",
    [
        ({"version": True, "tabs": [], "pending_stream_cleanup": []}, "unsupported or invalid"),
        (
            {"version": 2, "tabs": [], "pending_stream_cleanup": [], "extra": 1},
            "unsupported or invalid",
        ),
        ({"version": 2, "tabs": {}, "pending_stream_cleanup": []}, "invalid tab or cleanup list"),
        (_snapshot(tabs=[_live_tab(port=True)]), "invalid or duplicate tab record"),
        (_snapshot(tabs=[_live_tab(), _live_tab()]), "invalid or duplicate tab record"),
        (_snapshot(pending=[_pending(tab_id="live-tab")]), "live-colliding cleanup obligation"),
        (_snapshot(pending=[_pending(), _pending(tab_id="another-tab")]), "cleanup obligation"),
        (_snapshot(pending=[_pending(token="not-a-token")]), "cleanup obligation"),
        (_snapshot(pending=[_pending(authorization="untrusted")]), "cleanup obligation"),
    ],
)
def test_snapshot_validation_keeps_original_error_type(value: Any, message: str) -> None:
    with pytest.raises(TabStateError, match=message) as raised:
        TM._parse_tabs_snapshot(value)
    assert type(raised.value) is TabStateError


def test_snapshot_invalid_timestamp_still_raises_value_error() -> None:
    with pytest.raises(ValueError):
        TM._parse_tabs_snapshot(_snapshot(tabs=[_live_tab(created_at="not-a-date")]))


def test_snapshot_policy_and_record_constructor_keep_patch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _snapshot(tabs=[], pending=[_pending(tab_id="custom", authorization="custom-auth")])
    value["version"] = 17
    factory = Mock(wraps=TM._PendingTabCleanup)
    monkeypatch.setattr(TM, "_TABS_STATE_VERSION", 17)
    monkeypatch.setattr(TM, "_TAB_CLEANUP_AUTHORIZATION", "custom-auth")
    monkeypatch.setattr(TM, "_TAB_ID_RE", re.compile(r"custom\Z"))
    monkeypatch.setattr(TM, "_PendingTabCleanup", factory)
    _, pending = TM._parse_tabs_snapshot(value)
    assert pending["custom"].authorization == "custom-auth"
    factory.assert_called_once_with(**value["pending_stream_cleanup"][0])


def test_terminal_datetime_patch_reaches_all_extracted_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Mock(wraps=datetime)
    monkeypatch.setattr(TM, "datetime", clock)
    assert (
        TM._parse_session_meta(json.dumps({"id": "sid", "timestamp": "2026-10-17T12:00:00Z"}))[0]
        == "sid"
    )
    clock.fromisoformat.assert_called_once_with("2026-10-17T12:00:00+00:00")
    clock.reset_mock()
    TM._parse_tabs_snapshot(_snapshot())
    assert clock.fromisoformat.call_count == 2
    monkeypatch.setattr(TM, "_CODEX_SESSIONS_CACHE", None)
    monkeypatch.setattr(TM, "_codex_sessions_cache_key", lambda: (0, 0))
    monkeypatch.setattr(TM, "_codex_load_session_index", lambda: {})
    monkeypatch.setattr(TM, "_codex_scan_sessions", lambda **kwargs: {"sid": _entry("sid")})
    TM.list_codex_sessions()
    clock.fromtimestamp.assert_called_once_with(100.0)


def test_snapshot_error_constructor_keeps_patch_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class CustomStateError(RuntimeError):
        pass

    monkeypatch.setattr(TM, "TabStateError", CustomStateError)
    with pytest.raises(CustomStateError, match="unsupported or invalid"):
        TM._parse_tabs_snapshot({})
