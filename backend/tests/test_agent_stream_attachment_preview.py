"""Security and source-only contracts for Chat-scoped text attachments."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from httpx import AsyncClient, Response

from claude_hub.models import ExecutionTarget, ManagedSession, StreamAttachmentPreview, User
from tests.test_agent_stream_markdown import markdown_scope  # noqa: F401
from tests.test_agent_stream_markdown import (
    CREATED_AT,
    MANAGED_ID,
    TAB_ID,
    MarkdownScope,
    _assert_unavailable,
    _get_markdown,
    _write_reference,
)


async def _get_attachment(
    client: AsyncClient,
    path: str,
    *,
    managed: bool = False,
    identity: str | None = None,
) -> Response:
    owner = "sessions" if managed else "tabs"
    identity = identity or (MANAGED_ID if managed else TAB_ID)
    return await client.get(
        f"/api/workspaces/{owner}/{identity}/stream/attachment", params={"path": path}
    )


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize(
    "filename,kind,language",
    [
        ("code.py", "code", "python"),
        ("index.TSX", "code", "typescript"),
        ("kernel.cu", "code", "cpp"),
        ("report.JSON", "data", "json"),
        ("records.jsonl", "data", "json"),
        ("notebook.ipynb", "data", "json"),
        ("settings.yaml", "data", "yaml"),
        ("pyproject.toml", "data", "ini"),
        ("table.csv", "data", "plaintext"),
        ("table.tsv", "data", "plaintext"),
        ("page.html", "code", "xml"),
        ("component.vue", "code", "xml"),
        ("source.xml", "data", "xml"),
        ("service.conf", "text", "plaintext"),
        ("output.log", "text", "plaintext"),
        ("changes.patch", "text", "diff"),
        ("notes.rst", "text", "plaintext"),
        ("Dockerfile", "code", "dockerfile"),
        ("Makefile", "code", "makefile"),
        ("CMakeLists.txt", "code", "cmake"),
        ("README", "text", "plaintext"),
        ("LICENSE", "text", "plaintext"),
    ],
)
async def test_attachment_returns_source_text_and_format_hints(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    managed: bool,
    filename: str,
    kind: str,
    language: str,
) -> None:
    # Even active-looking HTML, scripts and notebook fields remain literal text.
    content = '<script>throw "not executed"</script>\n{"large_id": 9007199254740993}\n'
    path = markdown_scope.cwd / filename
    path.write_text(content, encoding="utf-8")
    response = await _get_attachment(client, filename, managed=managed)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "path": str(path),
        "filename": filename,
        "content": content,
        "size_bytes": len(content.encode("utf-8")),
        "truncated": False,
        "kind": kind,
        "language": language,
    }


@pytest.mark.parametrize("managed", [False, True])
async def test_attachment_markdown_keeps_legacy_response_contract(
    client: AsyncClient, markdown_scope: MarkdownScope, managed: bool
) -> None:
    identity = MANAGED_ID if managed else TAB_ID
    legacy = await _get_markdown(client, "report.md", identity=identity, managed=managed)
    current = await _get_attachment(client, "report.md", managed=managed)
    assert legacy.status_code == current.status_code == 200
    assert current.json() == {**legacy.json(), "kind": "markdown", "language": "markdown"}
    assert "kind" not in legacy.json()
    (markdown_scope.cwd / "code.py").write_text("print('ok')", encoding="utf-8")
    _assert_unavailable(await _get_markdown(client, "code.py", identity=identity, managed=managed))


async def test_empty_and_utf8_bom_attachment_paths_round_trip(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    filename = "记录 &?#% .json"
    path = markdown_scope.cwd / filename
    path.write_bytes(b"\xef\xbb\xbf" + '{"内容": "正常"}'.encode())
    response = await _get_attachment(client, str(path))
    assert response.status_code == 200
    assert response.json()["content"] == '{"内容": "正常"}'
    assert response.json()["path"] == str(path)
    path.write_bytes(b"")
    empty = await _get_attachment(client, str(path))
    assert empty.status_code == 200
    assert empty.json()["content"] == ""
    assert empty.json()["size_bytes"] == 0
    assert not empty.json()["truncated"]


@pytest.mark.parametrize(
    "filename",
    [
        ".env",
        "private.pem",
        "id_rsa",
        "archive.zip",
        "data.pkl",
        "image.svg",
        "report.pdf",
        "unknown",
    ],
)
async def test_unsupported_formats_do_not_open_files(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    from claude_hub.services.agent_stream import local_markdown

    (markdown_scope.cwd / filename).write_text("must not be read", encoding="utf-8")

    def forbidden_read(*_args: Any) -> tuple[bytes, int]:
        pytest.fail("unsupported attachment reached file I/O")

    monkeypatch.setattr(local_markdown, "_read_beneath", forbidden_read)
    _assert_unavailable(await _get_attachment(client, filename))


@pytest.mark.parametrize("path", ["", "a" * 4097], ids=["empty", "over-limit"])
async def test_attachment_path_query_is_bounded(
    client: AsyncClient, markdown_scope: MarkdownScope, path: str
) -> None:
    assert (await _get_attachment(client, path)).status_code == 422


@pytest.mark.parametrize("path", ["absent.py", "bad\x00.json", "bad\x7f.py", "code.py:12"])
async def test_invalid_attachment_path_is_opaque(
    client: AsyncClient, markdown_scope: MarkdownScope, path: str
) -> None:
    _assert_unavailable(await _get_attachment(client, path))


@pytest.mark.parametrize("data", [b"\xffinvalid", b"utf-8\x00binary", b"\xef\xbb"])
async def test_binary_attachment_is_rejected_as_text(
    client: AsyncClient, markdown_scope: MarkdownScope, data: bytes
) -> None:
    (markdown_scope.cwd / "data.json").write_bytes(data)
    response = await _get_attachment(client, "data.json")
    assert response.status_code == 415
    assert response.json() == {"detail": "document is not UTF-8 text"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("over_limit", [False, True])
async def test_attachment_budget_preserves_complete_utf8_characters(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    monkeypatch: pytest.MonkeyPatch,
    over_limit: bool,
) -> None:
    from claude_hub.services.agent_stream import local_markdown

    limit = 512 * 1024
    assert local_markdown.MARKDOWN_PREVIEW_MAX_BYTES == limit
    data = b"x" * limit if not over_limit else b"x" * (limit - 1) + "中".encode() + b"tail"
    (markdown_scope.cwd / "data.json").write_bytes(data)
    original_fdopen = os.fdopen
    reads: list[int] = []

    class BoundedReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            assert size == limit + 1
            reads.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(
        local_markdown.os,
        "fdopen",
        lambda *args, **kwargs: BoundedReader(original_fdopen(*args, **kwargs)),
    )
    response = await _get_attachment(client, "data.json")
    assert response.status_code == 200
    assert response.json()["content"] == "x" * (limit - (1 if over_limit else 0))
    assert response.json()["size_bytes"] == len(data)
    assert response.json()["truncated"] is over_limit
    assert reads == [limit + 1]


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("absolute", [False, True])
async def test_attachment_outside_cwd_is_denied_without_reference(
    client: AsyncClient, markdown_scope: MarkdownScope, managed: bool, absolute: bool
) -> None:
    private = markdown_scope.cwd.parent / "private.json"
    private.write_text("private source", encoding="utf-8")
    _assert_unavailable(
        await _get_attachment(
            client, str(private) if absolute else "../private.json", managed=managed
        )
    )


@pytest.mark.parametrize("directory", [False, True])
async def test_attachment_symlinks_cannot_escape_cwd(
    client: AsyncClient, markdown_scope: MarkdownScope, directory: bool
) -> None:
    outside = markdown_scope.cwd.parent / "private"
    outside.mkdir()
    (outside / "source.py").write_text("private source", encoding="utf-8")
    link = markdown_scope.cwd / ("linked" if directory else "linked.py")
    link.symlink_to(outside if directory else outside / "source.py", target_is_directory=directory)
    _assert_unavailable(
        await _get_attachment(client, "linked/source.py" if directory else link.name)
    )


async def test_attachment_fifo_and_directory_do_not_block(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    directory = markdown_scope.cwd / "directory.json"
    directory.mkdir()
    _assert_unavailable(await _get_attachment(client, directory.name))
    fifo = markdown_scope.cwd / "pipe.json"
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        response = await asyncio.wait_for(_get_attachment(client, fifo.name), timeout=2)
        _assert_unavailable(response)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("location", ["", ":12", ":12:4", "#L12"])
async def test_exact_assistant_temporary_code_reference_is_allowed(
    client: AsyncClient, markdown_scope: MarkdownScope, location: str
) -> None:
    quoted = markdown_scope.temporary / "记录 # source.py"
    sibling = markdown_scope.temporary / "other.py"
    quoted.write_text("print('quoted')\n", encoding="utf-8")
    sibling.write_text("private source", encoding="utf-8")
    _write_reference(f"[Source](file://{quote(str(quoted))}{location})", split=True)
    response = await _get_attachment(client, str(quoted))
    assert response.status_code == 200
    assert response.json()["kind"] == "code"
    assert response.json()["content"] == "print('quoted')\n"
    _assert_unavailable(await _get_attachment(client, str(sibling)))
    quoted.unlink()
    _assert_unavailable(await _get_attachment(client, str(quoted)))


@pytest.mark.parametrize("source", ["user", "other-session", "tool", "code", "image", "template"])
async def test_non_assistant_links_cannot_grant_attachment_access(
    client: AsyncClient, markdown_scope: MarkdownScope, source: str
) -> None:
    private = markdown_scope.temporary / "private.json"
    private.write_text("private source", encoding="utf-8")
    options: dict[str, Any] = {}
    text = f"[Source]({private})"
    if source == "user":
        options["role"] = "user"
    elif source == "other-session":
        options["event_session"] = "terminal-tab-other"
    elif source == "tool":
        options["kind"] = "tool_call_completed"
    elif source == "code":
        text = f"```text\n{text}\n```"
    elif source == "image":
        text = "!" + text
    elif source == "template":
        text = f'<template><a href="{private}">Source</a></template>'
    _write_reference(text, **options)
    _assert_unavailable(await _get_attachment(client, str(private)))


async def test_attachment_reference_cannot_grant_outside_temp_or_symlink(
    client: AsyncClient, markdown_scope: MarkdownScope
) -> None:
    private = markdown_scope.cwd.parent / "private.py"
    private.write_text("private source", encoding="utf-8")
    link = markdown_scope.temporary / "linked.py"
    link.symlink_to(private)
    _write_reference(f"[Private]({private})\n\n[Link]({link})")
    _assert_unavailable(await _get_attachment(client, str(private)))
    _assert_unavailable(await _get_attachment(client, str(link)))


@pytest.mark.parametrize("managed", [False, True])
async def test_remote_attachment_owner_cannot_read_local_file(
    client: AsyncClient, markdown_scope: MarkdownScope, managed: bool
) -> None:
    (markdown_scope.cwd / "code.py").write_text("private source", encoding="utf-8")
    if managed:
        markdown_scope.sessions[MANAGED_ID].target = ExecutionTarget.REMOTE
    else:
        markdown_scope.tabs[TAB_ID].target = ExecutionTarget.REMOTE
    _assert_unavailable(await _get_attachment(client, "code.py", managed=managed))


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("replace", [False, True])
async def test_attachment_read_rechecks_owner_incarnation(
    client: AsyncClient,
    markdown_scope: MarkdownScope,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    replace: bool,
) -> None:
    from claude_hub.api.routes.agent_stream import media

    (markdown_scope.cwd / "code.py").write_text("private source", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_read = media.read_attachment_preview

    def delayed_read(session: ManagedSession, path: str) -> StreamAttachmentPreview:
        try:
            result = original_read(session, path)
            entered.set()
            assert release.wait(5), "attachment read was not released"
            return result
        finally:
            finished.set()

    monkeypatch.setattr(media, "read_attachment_preview", delayed_read)
    pending = asyncio.create_task(_get_attachment(client, "code.py", managed=managed))
    try:
        assert await asyncio.to_thread(entered.wait, 5), "attachment read did not start"
        replacement = {"created_at": CREATED_AT + timedelta(seconds=1)}
        if managed:
            old_session = markdown_scope.sessions.pop(MANAGED_ID)
            if replace:
                markdown_scope.sessions[MANAGED_ID] = old_session.model_copy(update=replacement)
        else:
            old_tab = markdown_scope.tabs.pop(TAB_ID)
            if replace:
                markdown_scope.tabs[TAB_ID] = old_tab.model_copy(update=replacement)
        release.set()
        response = await pending
        _assert_unavailable(response)
        assert "private source" not in response.text
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5), "attachment worker did not finish"
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("managed", [False, True])
async def test_attachment_authentication_precedes_file_read(
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
    user = User(open_id="attachment-user", name="Attachment User", email="attachment@example.test")
    monkeypatch.setattr(
        dependencies,
        "get_session",
        lambda token: SimpleNamespace(user=user) if token == "valid" else None,
    )
    reads: list[str] = []
    original_read = media.read_attachment_preview
    (markdown_scope.cwd / "data.json").write_text("{}", encoding="utf-8")

    def observed_read(session: ManagedSession, path: str) -> StreamAttachmentPreview:
        reads.append(path)
        return original_read(session, path)

    monkeypatch.setattr(media, "read_attachment_preview", observed_read)
    response = await _get_attachment(client, "data.json", managed=managed)
    assert response.status_code == 401
    assert not reads
    client.cookies.set(cookie_name, "invalid")
    response = await _get_attachment(client, "data.json", managed=managed)
    assert response.status_code == 401
    assert not reads
    client.cookies.set(cookie_name, "valid")
    response = await _get_attachment(client, "data.json", managed=managed)
    assert response.status_code == 200
    assert response.json()["content"] == "{}"
    assert reads == ["data.json"]
