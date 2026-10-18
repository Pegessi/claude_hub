"""HTTP contracts for bounded, Chat-scoped local Markdown previews."""

from __future__ import annotations

import asyncio
import html
import importlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from httpx import AsyncClient, Response

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    TerminalTab,
    User,
    WorkspaceArtifactPreview,
    WorkspaceSessionRole,
)

TAB_ID = "markdown-tab"
MANAGED_ID = "markdown-session"
CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
CONTENT = "# Investigation\n\nA **Markdown** document.\n"


@dataclass
class MarkdownScope:
    cwd: Path
    temporary: Path
    tabs: dict[str, TerminalTab]
    sessions: dict[str, ManagedSession]


def _make_tab(tab_id: str, cwd: Path) -> TerminalTab:
    return TerminalTab(
        id=tab_id,
        name="Markdown Chat",
        cwd=str(cwd),
        port=0,
        created_at=CREATED_AT,
        is_active=True,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        target=ExecutionTarget.LOCAL,
    )


def _make_session(cwd: Path) -> ManagedSession:
    return ManagedSession(
        id=MANAGED_ID,
        workspace_id="markdown-workspace",
        tab_id="managed-markdown-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Managed Markdown",
        workspace_path=str(cwd),
        tmux_session="",
        target=ExecutionTarget.LOCAL,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


@pytest.fixture
def markdown_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MarkdownScope:
    from claude_hub.api import agent_stream as api
    from claude_hub.auth import dependencies
    from claude_hub.services.agent_stream import local_images, local_markdown

    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path / "state")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "report.md").write_text(CONTENT, encoding="utf-8")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    # A private temporary root keeps the grant tests independent of the runner's TMPDIR.
    monkeypatch.setattr(local_images, "_TEMP_IMAGE_ROOTS", (temporary,))
    monkeypatch.setattr(local_markdown.tempfile, "gettempdir", lambda: str(temporary))
    tabs = {TAB_ID: _make_tab(TAB_ID, cwd)}
    sessions = {MANAGED_ID: _make_session(cwd)}
    monkeypatch.setattr(api.ttyd_manager, "get_tab", tabs.get)
    monkeypatch.setattr(api, "workspace_manager", SimpleNamespace(sessions=sessions))
    monkeypatch.setattr(dependencies, "is_local_network_request", lambda _request: True)
    return MarkdownScope(cwd, temporary, tabs, sessions)


async def _get_markdown(
    client: AsyncClient,
    path: str,
    *,
    identity: str = TAB_ID,
    managed: bool = False,
) -> Response:
    owner = "sessions" if managed else "tabs"
    return await client.get(
        f"/api/workspaces/{owner}/{identity}/stream/markdown", params={"path": path}
    )


def _assert_unavailable(response: Response) -> None:
    assert response.status_code == 404
    assert response.json() == {"detail": "document not available"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def _write_reference(
    text: str,
    *,
    kind: str = "text_delta",
    role: str = "assistant",
    split: bool = False,
    store_session: str = f"terminal-tab-{TAB_ID}",
    event_session: str | None = None,
    message_role: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    from claude_hub.services.agent_stream.store import AgentStreamStore

    store = AgentStreamStore("terminal-tabs", store_session)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    chunks = [text[: len(text) // 2], text[len(text) // 2 :]] if split else [text]
    events = [
        {
            "session_id": event_session or store_session,
            "tab_id": TAB_ID,
            "type": kind,
            "turn_id": "markdown-turn",
            "message_id": f"markdown-turn:{message_role or role}",
            "run_epoch": 1,
            "payload": {
                "role": role,
                "assistant_text" if kind == "turn_completed" else "text": chunk,
                **(extra_payload or {}),
            },
        }
        for chunk in chunks
    ]
    store.path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


@pytest.mark.parametrize("absolute", [False, True])
async def test_reads_cwd_relative_and_absolute_markdown(
    client: AsyncClient, markdown_scope: MarkdownScope, absolute: bool
) -> None:
    path = markdown_scope.cwd / "report.md"
    response = await _get_markdown(client, str(path) if absolute else path.name)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "path": str(path),
        "filename": path.name,
        "content": CONTENT,
        "size_bytes": len(CONTENT.encode("utf-8")),
        "truncated": False,
    }


@pytest.mark.parametrize("suffix", [".md", ".MD", ".markdown", ".mdown", ".mkd"])
@pytest.mark.parametrize("bom", [False, True])
async def test_utf8_bom_and_url_encoded_filename_round_trip(
    client: AsyncClient, markdown_scope: MarkdownScope, suffix: str, bom: bool
) -> None:
    filename = "排查 &?#% 记录" + suffix
    content = "# 排查记录\n\n状态正常。\n"
    data = (b"\xef\xbb\xbf" if bom else b"") + content.encode("utf-8")
    path = markdown_scope.cwd / filename
    path.write_bytes(data)
    response = await client.get(
        f"/api/workspaces/tabs/{TAB_ID}/stream/markdown?path={quote(filename, safe='')}"
    )
    assert response.status_code == 200
    assert response.json()["content"] == content
    assert response.json()["path"] == str(path)
    assert response.json()["size_bytes"] == len(data)


async def test_relative_parent_segment_stays_inside_cwd(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    (markdown_scope.cwd / "docs").mkdir()
    response = await _get_markdown(client, "docs/../report.md")
    assert response.status_code == 200
    assert response.json()["path"] == str(markdown_scope.cwd / "report.md")


async def test_preview_preserves_raw_source_without_serving_html(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    raw = "<script>alert(1)</script>\n\n[Link](details.md)\n"
    (markdown_scope.cwd / "report.md").write_text(raw, encoding="utf-8")
    response = await _get_markdown(client, "report.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["content"] == raw


async def test_empty_markdown_is_a_valid_preview(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    (markdown_scope.cwd / "empty.md").touch()
    response = await _get_markdown(client, "empty.md")
    assert response.status_code == 200
    assert response.json()["content"] == ""
    assert response.json()["size_bytes"] == 0
    assert response.json()["truncated"] is False


async def test_read_budget_and_multibyte_truncation(
    client: AsyncClient, markdown_scope: MarkdownScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_markdown

    limit = 8
    monkeypatch.setattr(local_markdown, "MARKDOWN_PREVIEW_MAX_BYTES", limit)
    data = b"# abcd" + "中".encode("utf-8") + b"more data"
    (markdown_scope.cwd / "report.md").write_bytes(data)
    original_fdopen = os.fdopen
    reads: list[int] = []

    class BudgetedReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            reads.append(size)
            assert 0 <= size <= limit + 1, "preview attempted an unbounded file read"
            return self.handle.read(size)

    monkeypatch.setattr(
        local_markdown.os,
        "fdopen",
        lambda *args, **kwargs: BudgetedReader(original_fdopen(*args, **kwargs)),
    )
    response = await _get_markdown(client, "report.md")
    assert response.status_code == 200
    assert response.json()["content"] == "# abcd"
    assert response.json()["size_bytes"] == len(data)
    assert response.json()["truncated"] is True
    assert reads == [limit + 1]


async def test_exact_limit_is_not_truncated(
    client: AsyncClient, markdown_scope: MarkdownScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_markdown

    monkeypatch.setattr(local_markdown, "MARKDOWN_PREVIEW_MAX_BYTES", 8)
    (markdown_scope.cwd / "report.md").write_bytes(b"12345678")
    response = await _get_markdown(client, "report.md")
    assert response.status_code == 200
    assert response.json()["content"] == "12345678"
    assert response.json()["truncated"] is False


@pytest.mark.parametrize(
    "path", ["absent.md", "notes.txt", "folder.md", "   ", "bad\x00.md", "bad\x7f.md"]
)
async def test_missing_wrong_extension_directory_and_bad_paths_are_opaque(
    client: AsyncClient, markdown_scope: MarkdownScope, path: str
) -> None:
    (markdown_scope.cwd / "notes.txt").write_text("secret text", encoding="utf-8")
    (markdown_scope.cwd / "folder.md").mkdir()
    _assert_unavailable(await _get_markdown(client, path))


@pytest.mark.parametrize("path", ["", "a" * 4097], ids=["empty", "over-limit"])
async def test_query_length_is_validated(
    client: AsyncClient, markdown_scope: MarkdownScope, path: str
) -> None:
    assert (await _get_markdown(client, path)).status_code == 422


@pytest.mark.parametrize("data", [b"\xff\xfeinvalid", b"# text\x00binary", b"\xef\xbb"])
async def test_binary_and_invalid_utf8_are_rejected(
    client: AsyncClient, markdown_scope: MarkdownScope, data: bytes
) -> None:
    (markdown_scope.cwd / "report.md").write_bytes(data)
    response = await _get_markdown(client, "report.md")
    assert response.status_code == 415
    assert response.json() == {"detail": "document is not UTF-8 text"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("absolute", [False, True])
async def test_unreferenced_outside_cwd_and_parent_traversal_are_denied(
    client: AsyncClient, markdown_scope: MarkdownScope, absolute: bool
) -> None:
    outside = markdown_scope.cwd.parent / "private.md"
    outside.write_text("secret text", encoding="utf-8")
    _assert_unavailable(await _get_markdown(client, str(outside) if absolute else "../private.md"))


async def test_other_chat_cwd_is_not_inherited(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    other = markdown_scope.cwd.parent / "other-workspace"
    other.mkdir()
    (other / "report.md").write_text("Other Chat", encoding="utf-8")
    markdown_scope.tabs["other-tab"] = _make_tab("other-tab", other)
    _assert_unavailable(await _get_markdown(client, str(other / "report.md")))
    own = await _get_markdown(client, "report.md", identity="other-tab")
    assert own.status_code == 200
    assert own.json()["content"] == "Other Chat"


@pytest.mark.parametrize("directory", [False, True])
async def test_symlinks_cannot_escape_cwd(
    client: AsyncClient, markdown_scope: MarkdownScope, directory: bool
) -> None:
    target = markdown_scope.cwd.parent / "outside"
    target.mkdir()
    (target / "private.md").write_text("secret text", encoding="utf-8")
    link = markdown_scope.cwd / ("linked" if directory else "linked.md")
    link.symlink_to(target if directory else target / "private.md", target_is_directory=directory)
    _assert_unavailable(
        await _get_markdown(client, "linked/private.md" if directory else link.name)
    )


async def test_fifo_is_denied_without_blocking(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    fifo = markdown_scope.cwd / "pipe.md"
    os.mkfifo(fifo)
    # Keep a writer available so even a regressed blocking reader can be released
    # in finally rather than leaving an executor thread behind after the timeout.
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        response = await asyncio.wait_for(_get_markdown(client, fifo.name), timeout=2)
        _assert_unavailable(response)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    "kind,split", [("text_delta", False), ("text_delta", True), ("turn_completed", False)]
)
async def test_exact_assistant_temporary_reference_is_allowed(
    client: AsyncClient, markdown_scope: MarkdownScope, kind: str, split: bool
) -> None:
    outside = markdown_scope.temporary / "排查 & record.md"
    outside.write_text(CONTENT, encoding="utf-8")
    _write_reference(f"[Record](<{outside}>)", kind=kind, split=split)
    response = await _get_markdown(client, str(outside))
    assert response.status_code == 200
    assert response.json()["content"] == CONTENT
    assert response.json()["path"] == str(outside)


async def test_file_url_with_encoded_name_and_fragment_grants_exact_document(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    outside = markdown_scope.temporary / "排查 # record.md"
    outside.write_text(CONTENT, encoding="utf-8")
    _write_reference(f"[Record](file://{quote(str(outside))}#results)")
    assert (await _get_markdown(client, str(outside))).status_code == 200


@pytest.mark.parametrize(
    "source",
    [
        "user-role",
        "user-message",
        "tool-result",
        "tool-call",
        "other-store",
        "other-session",
        "bare-path",
    ],
)
async def test_other_sources_cannot_grant_temporary_access(
    client: AsyncClient, markdown_scope: MarkdownScope, source: str
) -> None:
    outside = markdown_scope.temporary / "private.md"
    outside.write_text("secret text", encoding="utf-8")
    options: dict[str, Any] = {}
    text = f"[Record]({outside})"
    if source == "user-role":
        options["role"] = "user"
    elif source == "user-message":
        options["message_role"] = "user"
    elif source == "tool-result":
        options["kind"] = "tool_call_completed"
    elif source == "tool-call":
        options["kind"] = "tool_call_started"
        options["extra_payload"] = {"name": "Read", "args": {"file_path": str(outside)}}
    elif source == "other-store":
        options["store_session"] = "terminal-tab-other"
    elif source == "other-session":
        options["event_session"] = "terminal-tab-other"
    elif source == "bare-path":
        text = f"File at {outside}."
    _write_reference(text, **options)
    _assert_unavailable(await _get_markdown(client, str(outside)))


async def test_temporary_reference_does_not_grant_siblings_or_survive_deletion(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    quoted = markdown_scope.temporary / "record.md"
    sibling = markdown_scope.temporary / "private.md"
    quoted.write_text(CONTENT, encoding="utf-8")
    sibling.write_text("secret text", encoding="utf-8")
    _write_reference(f"[Record]({quoted})")
    assert (await _get_markdown(client, str(quoted))).status_code == 200
    _assert_unavailable(await _get_markdown(client, str(sibling)))
    quoted.unlink()
    _assert_unavailable(await _get_markdown(client, str(quoted)))


async def test_reference_cannot_grant_non_temporary_file_or_symlink(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    private = markdown_scope.cwd.parent / "private.md"
    private.write_text("secret text", encoding="utf-8")
    linked = markdown_scope.temporary / "linked.md"
    linked.symlink_to(private)
    _write_reference(f"[Private]({private})\n\n[Linked]({linked})")
    _assert_unavailable(await _get_markdown(client, str(private)))
    _assert_unavailable(await _get_markdown(client, str(linked)))


@pytest.mark.parametrize("form", ["inline", "reference", "raw-anchor", "raw-anchor-whitespace"])
async def test_rendered_links_grant_the_exact_temporary_file(
    client: AsyncClient, markdown_scope: MarkdownScope, form: str
) -> None:
    outside = markdown_scope.temporary / "result & notes.md"
    outside.write_text(CONTENT, encoding="utf-8")
    if form == "inline":
        text = f"[Record](<{outside}>)"
    elif form == "reference":
        text = f"[Record][result]\n\n[result]: <{outside}>"
    elif form == "raw-anchor-whitespace":
        text = f'<a href="  {html.escape(str(outside), quote=True)}  ">Record</a>'
    else:
        text = f'<a href="{html.escape(str(outside), quote=True)}">Record</a>'
    _write_reference(text)
    response = await _get_markdown(client, str(outside))
    assert response.status_code == 200
    assert response.json()["content"] == CONTENT


@pytest.mark.parametrize(
    "form",
    [
        "backtick-fence",
        "tilde-fence",
        "inline-code",
        "indented-code",
        "image",
        "html-image",
        "escaped-link",
        "nested-template",
        "crossed-template-noscript",
        "self-closing-template",
        "duplicate-href-external-first",
        "duplicate-href-empty-first",
        "template-markdown",
        "raw-file-url",
        "raw-file-url-whitespace",
        "raw-textarea",
        "raw-script",
    ],
)
async def test_non_link_markdown_syntax_cannot_grant_temporary_file(
    client: AsyncClient, markdown_scope: MarkdownScope, form: str
) -> None:
    outside = markdown_scope.temporary / "private.md"
    outside.write_text("secret text", encoding="utf-8")
    link = f"[Record]({outside})"
    examples = {
        "backtick-fence": f"```markdown\n{link}\n```",
        "tilde-fence": f"~~~markdown\n{link}\n~~~",
        "inline-code": f"Example `{link}` only.",
        "indented-code": f"    {link}\n",
        "image": f"!{link}",
        "html-image": f'<img src="{outside}" alt="Record">',
        "escaped-link": "\\" + link,
        "nested-template": (
            f'<template><template></template><a href="{outside}">Record</a></template>'
        ),
        "crossed-template-noscript": f'<template><noscript></template><a href="{outside}">Record</a>',
        "self-closing-template": f'<template/><a href="{outside}">Record</a>',
        "duplicate-href-external-first": f'<a href="https://example.test" href="{outside}">Record</a>',
        "duplicate-href-empty-first": f'<a href href="{outside}">Record</a>',
        "template-markdown": f"<template>\n\n{link}\n\n</template>",
        "raw-file-url": f'<a href="file://{outside}">Record</a>',
        "raw-file-url-whitespace": f'<a href="  file://{outside}  ">Record</a>',
        "raw-textarea": f'<textarea><a href="{outside}">Record</a></textarea>',
        "raw-script": f'<script><a href="{outside}">Record</a></script>',
    }
    _write_reference(examples[form])
    _assert_unavailable(await _get_markdown(client, str(outside)))


@pytest.mark.parametrize("over_limit", [False, True], ids=["at-limit", "over-limit"])
async def test_completed_message_reference_respects_scan_limit(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    monkeypatch: pytest.MonkeyPatch,
    over_limit: bool,
) -> None:
    from claude_hub.services.agent_stream import markdown_references

    outside = markdown_scope.temporary / "capped-report.md"
    outside.write_text(CONTENT, encoding="utf-8")
    text = f"Record\n\n[Open]({outside})"
    monkeypatch.setattr(markdown_references, "_MAX_MESSAGE_CHARS", len(text))
    _write_reference(text + (" " if over_limit else ""), kind="turn_completed")
    response = await _get_markdown(client, str(outside))
    if over_limit:
        _assert_unavailable(response)
    else:
        assert response.status_code == 200
        assert response.json()["content"] == CONTENT


async def test_temporary_grant_reads_fresh_content_after_same_path_recreation(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    outside = markdown_scope.temporary / "live-report.md"
    outside.write_text("# Original document", encoding="utf-8")
    _write_reference(f"[Record]({outside})")
    original = await _get_markdown(client, str(outside))
    assert original.status_code == 200
    assert original.json()["content"] == "# Original document"
    outside.unlink()
    _assert_unavailable(await _get_markdown(client, str(outside)))
    # The assistant authorizes a live pathname, not an immutable content snapshot.
    outside.write_text("# Replacement document", encoding="utf-8")
    replacement = await _get_markdown(client, str(outside))
    assert replacement.status_code == 200
    assert replacement.json()["content"] == "# Replacement document"
    assert replacement.json()["path"] == str(outside)
    assert replacement.headers["cache-control"] == "no-store"


async def test_renamed_cwd_ancestor_cannot_be_replaced_by_a_symlink_before_open(
    client: AsyncClient, markdown_scope: MarkdownScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_markdown

    ancestor = markdown_scope.cwd / "active-parent"
    root = ancestor / "nested-workspace"
    root.mkdir(parents=True)
    (root / "report.md").write_text(CONTENT, encoding="utf-8")
    markdown_scope.tabs[TAB_ID].cwd = str(root)
    hostile = markdown_scope.cwd / "other-parent"
    hostile_root = hostile / "nested-workspace"
    hostile_root.mkdir(parents=True)
    (hostile_root / "report.md").write_text("secret replacement document", encoding="utf-8")
    preserved = markdown_scope.cwd / "original-parent"
    resolved = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_read = local_markdown._read_beneath

    def delayed_open(resolved_root: Path, relative: Path) -> tuple[bytes, int]:
        try:
            assert resolved_root == root
            resolved.set()
            assert release.wait(5), "resolved Markdown root was not released"
            return original_read(resolved_root, relative)
        finally:
            finished.set()

    monkeypatch.setattr(local_markdown, "_read_beneath", delayed_open)
    pending = asyncio.create_task(_get_markdown(client, "report.md"))
    try:
        assert await asyncio.to_thread(resolved.wait, 5), "Markdown root was not resolved"
        ancestor.rename(preserved)
        ancestor.symlink_to(hostile, target_is_directory=True)
        release.set()
        response = await pending
        _assert_unavailable(response)
        assert "secret" not in response.text
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5), "Markdown worker did not finish"
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if ancestor.is_symlink():
            ancestor.unlink()
        if preserved.exists():
            preserved.rename(ancestor)


@pytest.mark.parametrize("managed", [False, True])
async def test_remote_owner_cannot_read_local_hub_files(
    client: AsyncClient, markdown_scope: MarkdownScope, managed: bool
) -> None:
    if managed:
        markdown_scope.sessions[MANAGED_ID].target = ExecutionTarget.REMOTE
    else:
        markdown_scope.tabs[TAB_ID].target = ExecutionTarget.REMOTE
    response = await _get_markdown(
        client, "report.md", managed=managed, identity=MANAGED_ID if managed else TAB_ID
    )
    _assert_unavailable(response)


async def test_managed_session_identity_is_distinct_from_tab_and_provider_identity(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    session = markdown_scope.sessions[MANAGED_ID]
    session.agent_session_id = "provider-conversation"
    managed_tab = _make_tab(session.tab_id, markdown_scope.cwd)
    managed_tab.managed_session_id = session.id
    managed_tab.workspace_role = WorkspaceSessionRole.WORKER
    markdown_scope.tabs[managed_tab.id] = managed_tab
    response = await _get_markdown(client, "report.md", identity=session.id, managed=True)
    assert response.status_code == 200
    assert response.json()["content"] == CONTENT
    assert response.headers["cache-control"] == "no-store"
    assert (await _get_markdown(client, "report.md", identity=managed_tab.id)).status_code == 409
    for wrong_identity in (session.tab_id, "provider-conversation", "unknown"):
        response = await _get_markdown(client, "report.md", identity=wrong_identity, managed=True)
        assert response.status_code == 404
    assert (await _get_markdown(client, "report.md", identity="unknown")).status_code == 404


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("replace", [False, True])
async def test_owner_deleted_or_replaced_during_threaded_read_cannot_receive_content(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    replace: bool,
) -> None:
    from claude_hub.api.routes.agent_stream import media

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_read = media.read_markdown_preview

    def delayed_read(session: ManagedSession, path: str) -> WorkspaceArtifactPreview:
        try:
            result = original_read(session, path)
            entered.set()
            assert release.wait(5), "Markdown read was not released"
            return result
        finally:
            finished.set()

    monkeypatch.setattr(media, "read_markdown_preview", delayed_read)
    identity = MANAGED_ID if managed else TAB_ID
    pending = asyncio.create_task(
        _get_markdown(client, "report.md", identity=identity, managed=managed)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5), "Markdown read did not start"
        replacement = {"created_at": CREATED_AT + timedelta(seconds=1)}
        if managed:
            old_session = markdown_scope.sessions.pop(identity)
            if replace:
                markdown_scope.sessions[identity] = old_session.model_copy(update=replacement)
        else:
            old_tab = markdown_scope.tabs.pop(identity)
            if replace:
                markdown_scope.tabs[identity] = old_tab.model_copy(update=replacement)
        release.set()
        response = await pending
        _assert_unavailable(response)
        assert CONTENT not in response.text
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5), "Markdown worker did not finish"
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("managed", [False, True])
async def test_authentication_is_required_before_reading_document(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
) -> None:
    from claude_hub.api.routes.agent_stream import media
    from claude_hub.auth import dependencies

    cookie_name = dependencies.settings.session_cookie_name
    monkeypatch.setattr(dependencies, "is_local_network_request", lambda _request: False)
    monkeypatch.setattr(
        dependencies,
        "settings",
        SimpleNamespace(auth_enabled=True, allowed_open_ids_list=[], allowed_emails_list=[]),
    )
    user = User(open_id="markdown-user", name="Markdown User", email="markdown@example.test")
    monkeypatch.setattr(
        dependencies,
        "get_session",
        lambda token: SimpleNamespace(user=user) if token == "valid" else None,
    )
    reads: list[str] = []
    original_read = media.read_markdown_preview

    def observed_read(session: ManagedSession, path: str) -> WorkspaceArtifactPreview:
        reads.append(path)
        return original_read(session, path)

    monkeypatch.setattr(media, "read_markdown_preview", observed_read)
    identity = MANAGED_ID if managed else TAB_ID
    response = await _get_markdown(client, "report.md", identity=identity, managed=managed)
    assert response.status_code == 401
    assert not reads
    client.cookies.set(cookie_name, "invalid")
    response = await _get_markdown(client, "report.md", identity=identity, managed=managed)
    assert response.status_code == 401
    assert not reads
    client.cookies.set(cookie_name, "valid")
    response = await _get_markdown(client, "report.md", identity=identity, managed=managed)
    assert response.status_code == 200
    assert response.json()["content"] == CONTENT
    assert reads == ["report.md"]


@pytest.mark.parametrize(
    "template",
    [
        '<select><a href="{path}">Record</a></select>',
        '<select><option><a href="{path}">Record</a></option></select>',
        '<select/><a href="{path}">Record</a>',
        "<select>\n\n[Record]({path})\n\n</select>",
    ],
    ids=["select", "select-option", "self-closing-select", "select-markdown"],
)
async def test_select_content_cannot_grant_temporary_markdown(
    client: AsyncClient, markdown_scope: MarkdownScope, template: str
) -> None:
    private = markdown_scope.temporary / "private.md"
    private.write_text(CONTENT, encoding="utf-8")
    _write_reference(template.format(path=private))
    _assert_unavailable(await _get_markdown(client, str(private)))
