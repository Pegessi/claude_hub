"""Structural contracts for decomposed feedback, Bot pool, and Dot services."""

from __future__ import annotations

import ast
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_hub.services import feedback_lessons, feishu_bot_pool
from claude_hub.services.bot_pool.store import FeishuBotPoolStore as BotPoolImplementation
from claude_hub.services.dot import service as dot_service
from claude_hub.services.dot import store as dot_store
from claude_hub.services.dot.runtime.service import DotService as DotRuntimeImplementation
from claude_hub.services.dot.storage.store import DotStore as DotStoreImplementation
from claude_hub.services.feedback.store import FeedbackLessonStore as FeedbackImplementation

_SERVICE_ROOT = Path(feedback_lessons.__file__).parent
_OWNED_MODULES = {
    "claude_hub.services.feedback_lessons": _SERVICE_ROOT / "feedback_lessons.py",
    "claude_hub.services.feedback.common": _SERVICE_ROOT / "feedback/common.py",
    "claude_hub.services.feedback.persistence": _SERVICE_ROOT / "feedback/persistence.py",
    "claude_hub.services.feedback.catalog": _SERVICE_ROOT / "feedback/catalog.py",
    "claude_hub.services.feedback.store": _SERVICE_ROOT / "feedback/store.py",
    "claude_hub.services.feishu_bot_pool": _SERVICE_ROOT / "feishu_bot_pool.py",
    "claude_hub.services.bot_pool.common": _SERVICE_ROOT / "bot_pool/common.py",
    "claude_hub.services.bot_pool.state": _SERVICE_ROOT / "bot_pool/state.py",
    "claude_hub.services.bot_pool.store": _SERVICE_ROOT / "bot_pool/store.py",
    "claude_hub.services.dot.service": _SERVICE_ROOT / "dot/service.py",
    "claude_hub.services.dot.runtime.common": _SERVICE_ROOT / "dot/runtime/common.py",
    "claude_hub.services.dot.runtime.session": _SERVICE_ROOT / "dot/runtime/session.py",
    "claude_hub.services.dot.runtime.work": _SERVICE_ROOT / "dot/runtime/work.py",
    "claude_hub.services.dot.runtime.service": _SERVICE_ROOT / "dot/runtime/service.py",
    "claude_hub.services.dot.store": _SERVICE_ROOT / "dot/store.py",
    "claude_hub.services.dot.storage.cards": _SERVICE_ROOT / "dot/storage/cards.py",
    "claude_hub.services.dot.storage.repository": _SERVICE_ROOT / "dot/storage/repository.py",
    "claude_hub.services.dot.storage.memory": _SERVICE_ROOT / "dot/storage/memory.py",
    "claude_hub.services.dot.storage.store": _SERVICE_ROOT / "dot/storage/store.py",
}


def _owned_edges(module_name: str, path: Path) -> set[str]:
    edges: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            edges.update(alias.name for alias in node.names if alias.name in _OWNED_MODULES)
        elif isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            if node.level:
                package = module_name.rpartition(".")[0]
                imported_module = importlib.util.resolve_name(
                    "." * node.level + imported_module,
                    package,
                )
            if imported_module in _OWNED_MODULES:
                edges.add(imported_module)
    return edges - {module_name}


def test_legacy_entries_are_small_and_export_the_feature_implementations() -> None:
    for relative in ("feedback_lessons.py", "feishu_bot_pool.py", "dot/service.py", "dot/store.py"):
        assert len((_SERVICE_ROOT / relative).read_text(encoding="utf-8").splitlines()) <= 800

    assert issubclass(feedback_lessons.FeedbackLessonStore, FeedbackImplementation)
    assert feishu_bot_pool.FeishuBotPoolStore is BotPoolImplementation
    assert issubclass(dot_store.DotStore, DotStoreImplementation)
    assert issubclass(dot_service.DotService, DotRuntimeImplementation)


def test_owned_service_dependency_graph_is_acyclic() -> None:
    graph = {name: _owned_edges(name, path) for name, path in _OWNED_MODULES.items()}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_name: str) -> None:
        if module_name in visiting:
            pytest.fail(f"owned service import cycle reaches {module_name}")
        if module_name in visited:
            return
        visiting.add(module_name)
        for dependency in graph[module_name]:
            visit(dependency)
        visiting.remove(module_name)
        visited.add(module_name)

    for module_name in graph:
        visit(module_name)


def test_dot_legacy_dependency_hooks_follow_module_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    service = dot_service.DotService()
    marker = dot_service.utc_now()
    monkeypatch.setattr(dot_service, "utc_now", lambda: marker)
    monkeypatch.setattr(dot_service, "WATCH_INTERVAL_SECONDS", 0.25)

    assert service._utc_now() is marker
    assert service._watch_interval_seconds() == 0.25


def test_feedback_facade_settings_are_late_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = feedback_lessons.FeedbackLessonStore(tmp_path)
    monkeypatch.setattr(feedback_lessons, "_DIGEST_MAX_TITLE", 5)
    monkeypatch.setattr(feedback_lessons, "FEEDBACK_INDEX_SCHEMA_VERSION", 9)
    monkeypatch.setattr(feedback_lessons, "FEEDBACK_SUMMARY_PROMPT_VERSION", 11)
    monkeypatch.setattr(feedback_lessons, "SINGLE_EVIDENCE_CONFIDENCE_CAP", 0.2)
    monkeypatch.setattr(feedback_lessons, "MULTI_EVIDENCE_CONFIDENCE_CAP", 0.3)

    digest = feedback_lessons.FeedbackTaskDigest(task_id="task", title="abcdefghij")
    assert store._compact_digest_for_prompt(digest)["title"] == "ab..."
    assert store._read_feedback_index("workspace") == {
        "schema_version": 9,
        "workspace_id": "workspace",
        "prompt_version": 11,
        "last_full_scan_at": None,
        "processed_task_records": [],
        "lesson_fingerprints": {},
    }

    def capped_confidence(evidence_task_ids: list[str]) -> float | None:
        return store._validate_lesson_payload(
            workspace_id="workspace",
            applies_when=["python"],
            do="verify",
            avoid="guessing",
            evidence_task_ids=evidence_task_ids,
            source_record_ids=[],
            confidence=0.9,
            enforce_iteration_signal=False,
        )

    assert capped_confidence(["one"]) == 0.2
    assert capped_confidence(["one", "two"]) == 0.3


def test_dot_store_facade_clock_is_late_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_DOT_HOME", str(tmp_path / "dot"))
    service = dot_service.DotService()
    store = service.store
    assert isinstance(store, dot_store.DotStore)
    explicit_store = dot_store.DotStore(tmp_path / "explicit-dot")
    assert dot_service.DotService(explicit_store).store is explicit_store

    marker = datetime(2001, 2, 3, 12, 34, tzinfo=timezone.utc)
    monkeypatch.setattr(dot_store, "utc_now", lambda: marker)

    store.append_journal("user", "remember this")
    focus = store.add_focus("Ship the split")

    journal = store.journal_path("2001-02-03").read_text(encoding="utf-8")
    assert "· user" in journal
    assert focus["created"] == "2001-02-03T12:34:00+00:00"
