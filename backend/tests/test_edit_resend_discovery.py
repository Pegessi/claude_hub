"""Regression tests for the edit-resend discovery fix.

Root cause being guarded: a direct Chat tab created with a cwd that goes
through a symlink (macOS ``/tmp`` -> ``/private/tmp``) stored the *unresolved*
cwd.  Edit-resend locates the provider transcript via ``discover_source`` ->
``resolve_cwd`` -> ``_claude_project_dir_for_cwd``; the unresolved path encoded
to a non-existent Claude project dir, so edit-resend always failed with
"could not locate provider transcript file" (409).

The fix symlink-resolves the cwd in ``resolve_cwd``.  These tests pin that
behavior so it cannot regress.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_hub.services.agent_stream.base import resolve_cwd


def _make_session(*, cwd: str, tab_id: str = "tab-1") -> SimpleNamespace:
    """A minimal session shape: resolve_process_hint reads tab_id + workspace_path.

    No live ttyd process is registered in the test, so resolve_process_hint
    falls back to ``workspace_path`` (the value under test).
    """
    return SimpleNamespace(tab_id=tab_id, workspace_path=cwd, agent_session_id=None)


def test_resolve_cwd_resolves_symlinks(tmp_path: Path) -> None:
    """resolve_cwd must return the real path, not the symlink path.

    A symlinked cwd (``/tmp`` -> ``/private/tmp``) otherwise encodes to the
    wrong Claude project dir and breaks edit-resend discovery.
    """
    real_dir = tmp_path / "real-workdir"
    real_dir.mkdir()
    link_dir = tmp_path / "link-workdir"
    link_dir.symlink_to(real_dir)

    session = _make_session(cwd=str(link_dir))
    resolved = resolve_cwd(session)

    assert resolved == str(real_dir)
    assert resolved != str(link_dir)


def test_resolve_cwd_passes_through_plain_path(tmp_path: Path) -> None:
    """A cwd with no symlink component is returned unchanged (resolved)."""
    plain = tmp_path / "plain-workdir"
    plain.mkdir()

    session = _make_session(cwd=str(plain))
    assert resolve_cwd(session) == str(plain)


def test_resolve_cwd_empty_returns_empty() -> None:
    """An absent cwd stays empty — must not resolve to the process cwd."""
    session = _make_session(cwd="")
    assert resolve_cwd(session) == ""


def test_discover_source_finds_transcript_through_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a symlinked cwd still maps to the real Claude project dir.

    Sets up a fake ``~/.claude/projects/<encoded-real-cwd>/<sid>.jsonl`` and a
    session whose cwd is the symlink; discover_source must locate the transcript
    via the resolved path.
    """
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    real_dir = tmp_path / "real-workdir"
    real_dir.mkdir()
    link_dir = tmp_path / "link-workdir"
    link_dir.symlink_to(real_dir)

    # Fake HOME so the Claude project dir lands under tmp_path, not the real ~.
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Build the project dir from the RESOLVED cwd (as Claude itself would).
    from claude_hub.services.ttyd_manager import _claude_project_dir_for_cwd

    project_dir = _claude_project_dir_for_cwd(str(real_dir))
    project_dir.mkdir(parents=True, exist_ok=True)

    agent_session_id = "11111111-2222-3333-4444-555555555555"
    transcript = project_dir / f"{agent_session_id}.jsonl"
    transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-09-15T00:00:00Z"}) + "\n")

    session = SimpleNamespace(
        tab_id="tab-1",
        workspace_path=str(link_dir),  # the UNRESOLVED (symlink) cwd
        agent_session_id=agent_session_id,
        created_at=None,
    )

    adapter = ClaudeJsonlAdapter()
    found = adapter.discover_source(session)

    assert found == transcript
