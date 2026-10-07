"""Task CLI boundaries, using only mocked HTTP transports."""

import json
import os
import re
import stat

import httpx
import pytest
from click.testing import CliRunner

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli

CAPABILITIES = {
    "supported_execution_controls": ["workspace", "initiator"],
    "progress_states": [
        "started",
        "working",
        "blocked",
        "needs_input",
        "completed",
        "failed",
        "released",
    ],
    "record_only_requires_reporter_key": True,
    "handoff_requires_release": True,
    "legacy_chat_work_create": False,
}
TASK = {
    "id": "task-1",
    "workspace_id": "ws1",
    "title": "Independent goal",
    "status": "working",
    "task_mode": "reviewed",
    "execution_control": "initiator",
    "execution_epoch": 1,
    "progress_revision": 0,
    "execution_released": False,
}


@pytest.fixture(autouse=True)
def clear_ambient_tab_id(monkeypatch):
    monkeypatch.delenv("CLAUDE_HUB_TAB_ID", raising=False)


def patch_client(monkeypatch, handler):
    captured = []

    def record(request):
        captured.append(request)
        return handler(request)

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(base_url="http://testserver", transport=httpx.MockTransport(record)),
    )
    return captured


def body(request):
    return json.loads(request.content)


def key_file(tmp_path, text="k" * 43):
    path = tmp_path / "key"
    path.write_text(text)
    path.chmod(0o600)
    return path


def register_args(path):
    return [
        "--json",
        "task",
        "register",
        "ws1",
        "--title",
        "Independent goal",
        "--prompt",
        "Do the bounded work",
        "--reporter-key-file",
        str(path),
    ]


def progress_args(path):
    return [
        "--json",
        "task",
        "progress",
        "ws1",
        "task-1",
        "--reporter-key-file",
        str(path),
        "--state",
        "working",
        "--summary",
        "In progress",
        "--expected-execution-epoch",
        "1",
        "--expected-progress-revision",
        "0",
        "--call-id",
        "call-1",
    ]


def handoff_args(target="initiator"):
    return [
        "--json",
        "task",
        "handoff",
        "ws1",
        "task-1",
        "--execution-control",
        target,
        "--expected-execution-epoch",
        "1",
        "--expected-progress-revision",
        "0",
    ]


def envelope():
    return {
        "task": {**TASK, "progress_revision": 1},
        "event": {
            "sequence": 1,
            "type": "progress",
            "call_id": "call-1",
            "task_id": "task-1",
            "consumer_key": "task:task-1",
            "payload": {"state": "working", "summary": "In progress"},
        },
        "replayed": False,
    }


def test_help_exposes_explicit_commands():
    result = CliRunner().invoke(cli, ["task", "--help"])
    assert result.exit_code == 0
    for name in ("register", "progress", "handoff", "dispatch", "context"):
        assert name in result.output
    for name in ("register", "progress"):
        help_text = CliRunner().invoke(cli, ["task", name, "--help"]).output
        assert "--tab-id" not in help_text
        assert "explicitly" in help_text
    context = CliRunner().invoke(cli, ["task", "context", "--help"]).output
    assert "main turn" in context and "CLI" in context and "child" in context


def test_register_preflights_and_persists_key_before_request(tmp_path, monkeypatch):
    path = tmp_path / "key"

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/api/workspaces/ws1/task-capabilities"
            assert not path.exists()
            return httpx.Response(200, json=CAPABILITIES)
        assert path.read_text() == body(request)["reporter_key"]
        return httpx.Response(201, json=TASK)

    captured = patch_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        register_args(path)
        + [
            "--source-kind",
            "agent",
            "--source-agent-id",
            "child-1",
            "--execution-provider",
            "codex",
            "--execution-session-id",
            "session-1",
            "--execution-thread-id",
            "thread-1",
            "--execution-turn-id",
            "turn-1",
            "--execution-run-epoch",
            "7",
            "--parent-task-id",
            "parent-1",
            "--depends-on",
            "dep-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    value = body(captured[1])
    assert value["execution_control"] == "initiator"
    assert value["source"] == {"kind": "agent", "agent_id": "child-1"}
    assert value["execution_ref"] == {
        "provider": "codex",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "run_epoch": 7,
    }
    assert value["parent_task_id"] == "parent-1" and value["depends_on_task_ids"] == ["dep-1"]
    assert re.fullmatch(r"[0-9a-f]{64}", value["request_key"])
    assert path.read_text() not in result.output and value["request_key"] not in result.output
    assert json.loads(result.stdout)["replayed"] is False
    assert [r.url.path for r in captured] == [
        "/api/workspaces/ws1/task-capabilities",
        "/api/workspaces/ws1/tasks",
    ]


def test_registration_retry_preserves_request_and_key(tmp_path, monkeypatch):
    path = tmp_path / "key"
    captured = patch_client(
        monkeypatch,
        lambda r: (
            httpx.Response(200, json=CAPABILITIES)
            if r.method == "GET"
            else httpx.Response(200, json=TASK, headers={"X-Task-Replayed": "true"})
        ),
    )
    first = CliRunner().invoke(cli, register_args(path))
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(cli, register_args(path) + ["--reuse-reporter-key-file"])
    assert second.exit_code == 0, second.output
    assert [r.method for r in captured] == ["GET", "POST", "GET", "POST"]
    assert body(captured[1]) == body(captured[3])
    assert json.loads(second.stdout)["replayed"] is True
    blind = CliRunner().invoke(cli, register_args(path))
    assert blind.exit_code != 0 and "already exists" in blind.output
    assert len(captured) == 5 and captured[-1].method == "GET"


@pytest.mark.parametrize("status", [200, 404])
def test_capability_failure_creates_no_key_or_mutation(tmp_path, monkeypatch, status):
    path = tmp_path / "key"
    response = (
        {"supported_execution_controls": ["workspace"]}
        if status == 200
        else {"detail": "private-backend-detail"}
    )
    captured = patch_client(monkeypatch, lambda r: httpx.Response(status, json=response))
    result = CliRunner().invoke(cli, register_args(path))
    assert result.exit_code != 0 and "initiator_task_registration_unavailable" in result.output
    assert "private-backend-detail" not in result.output and not path.exists()
    assert [r.method for r in captured] == ["GET"]


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("a" * 32, True),
        ("b" * 256, True),
        ("c" * 31, False),
        ("d" * 257, False),
        ("e" * 31 + "\n", False),
        ("f" * 31 + " ", False),
        ("é" * 32, False),
    ],
)
def test_key_boundaries(tmp_path, monkeypatch, text, valid):
    path = key_file(tmp_path, text)
    captured = patch_client(monkeypatch, lambda r: httpx.Response(200, json=envelope()))
    result = CliRunner().invoke(cli, progress_args(path))
    assert (result.exit_code == 0) is valid, result.output
    assert len(captured) == (1 if valid else 0)
    assert text not in result.output


def test_key_file_types_and_permissions_fail_without_http(tmp_path, monkeypatch):
    path = key_file(tmp_path)
    link, folder, fifo = [tmp_path / name for name in ("link", "folder", "fifo")]
    link.symlink_to(path)
    folder.mkdir()
    os.mkfifo(fifo, 0o600)
    path.chmod(0o640)
    captured = patch_client(monkeypatch, lambda r: httpx.Response(500, json={}))
    for candidate in (path, link, folder, fifo, tmp_path / "missing"):
        result = CliRunner().invoke(cli, progress_args(candidate))
        assert result.exit_code != 0, (candidate, result.output)
    assert captured == []


@pytest.mark.parametrize("operation", ["register", "progress"])
def test_mutations_never_infer_ref_from_ambient_tab(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "main-tab")
    path = key_file(tmp_path) if operation == "progress" else tmp_path / "key"

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/api/workspaces/ws1/task-capabilities"
            return httpx.Response(200, json=CAPABILITIES)
        assert "execution_ref" not in body(request)
        return httpx.Response(200, json=TASK if operation == "register" else envelope())

    captured = patch_client(monkeypatch, handler)
    args = progress_args(path) if operation == "progress" else register_args(path)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert all("/tabs/" not in r.url.path for r in captured)
    assert len(captured) == (1 if operation == "progress" else 2)


def test_progress_header_payload_and_safe_summary(tmp_path, monkeypatch):
    path = key_file(tmp_path)

    def handler(request):
        assert request.headers["X-Task-Reporter-Key"] == path.read_text()
        assert request.url.path == "/api/workspaces/ws1/tasks/task-1/progress"
        return httpx.Response(200, json=envelope())

    captured = patch_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        progress_args(path)
        + [
            "--validation",
            "tests passed",
            "--risks",
            "none",
            "--artifact-ref",
            "artifact://one",
            "--execution-provider",
            "codex",
            "--execution-thread-id",
            "thread-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert body(captured[0]) == {
        "call_id": "call-1",
        "expected_execution_epoch": 1,
        "expected_progress_revision": 0,
        "state": "working",
        "summary": "In progress",
        "artifact_refs": ["artifact://one"],
        "validation": "tests passed",
        "risks": "none",
        "execution_ref": {"provider": "codex", "thread_id": "thread-1"},
    }
    assert json.loads(result.stdout) == {
        "workspace_id": "ws1",
        "task_id": "task-1",
        "status": "working",
        "execution_control": "initiator",
        "execution_epoch": 1,
        "progress_revision": 1,
        "progress_state": "working",
        "replayed": False,
    }
    assert "call_id=call-1" in result.stderr and path.read_text() not in result.output
    assert len(captured) == 1


def test_generated_call_id_is_visible_for_retry(tmp_path, monkeypatch):
    path = key_file(tmp_path)
    captured = patch_client(monkeypatch, lambda r: httpx.Response(200, json=envelope()))
    args = progress_args(path)
    index = args.index("--call-id")
    del args[index : index + 2]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    value = body(captured[0])["call_id"]
    assert re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
    assert f"call_id={value}" in result.stderr


@pytest.mark.parametrize("operation", ["register", "progress", "handoff"])
@pytest.mark.parametrize("status", [403, 409, 422])
def test_server_detail_cannot_reflect_key(tmp_path, monkeypatch, operation, status):
    path = key_file(tmp_path) if operation == "progress" else tmp_path / "key"

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=CAPABILITIES)
        return httpx.Response(
            status,
            json={"detail": {"message": f"reflected {path.read_text()}", "key": path.read_text()}},
        )

    captured = patch_client(monkeypatch, handler)
    args = register_args(path) if operation == "register" else progress_args(path)
    if operation == "handoff":
        args = handoff_args() + ["--new-reporter-key-file", str(path), "--call-id", "handoff-1"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    expected = (
        f"invalid_task_{operation}"
        if status == 422
        else f"task_{operation}_{'forbidden' if status == 403 else 'conflict'}"
    )
    assert expected in result.output
    assert path.read_text() not in result.output
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [r.method for r in captured] == (
        ["GET", "POST"] if operation == "register" else ["POST"]
    )


def test_handoff_rotates_key_without_starting(tmp_path, monkeypatch):
    old, new = key_file(tmp_path), tmp_path / "new"

    def handler(request):
        assert request.url.path == "/api/workspaces/ws1/tasks/task-1/handoff"
        assert body(request) == {
            "call_id": "handoff-1",
            "expected_execution_epoch": 1,
            "expected_progress_revision": 0,
            "execution_control": "initiator",
            "new_reporter_key": new.read_text(),
            "execution_ref": {"provider": "claude", "thread_id": "child-2"},
        }
        data = envelope()
        data["task"]["execution_epoch"] = 2
        data["event"]["payload"]["state"] = "released"
        return httpx.Response(200, json=data)

    captured = patch_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        handoff_args()
        + [
            "--call-id",
            "handoff-1",
            "--new-reporter-key-file",
            str(new),
            "--execution-provider",
            "claude",
            "--execution-thread-id",
            "child-2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert new.read_text() != old.read_text() and new.read_text() not in result.output
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    data = json.loads(result.stdout)
    assert data["task_id"] == "task-1" and data["execution_epoch"] == 2
    assert data["progress_state"] == "released" and data["reporter_key_file"] == str(new)
    assert len(captured) == 1


def test_handoff_reuse_requires_call_id_and_workspace_rejects_keys(tmp_path, monkeypatch):
    path = key_file(tmp_path)
    captured = patch_client(monkeypatch, lambda r: httpx.Response(500, json={}))
    for args, expected in [
        (
            handoff_args()
            + ["--reuse-new-reporter-key-file", "--new-reporter-key-file", str(path)],
            "requires an explicit --call-id",
        ),
        (
            handoff_args("workspace") + ["--new-reporter-key-file", str(path)],
            "only valid for initiator",
        ),
    ]:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code != 0 and expected in result.output
    assert captured == []


def test_dispatch_has_explicit_start_request(monkeypatch):
    captured = patch_client(
        monkeypatch, lambda r: httpx.Response(200, json={**TASK, "execution_control": "workspace"})
    )
    result = CliRunner().invoke(
        cli,
        [
            "task",
            "dispatch",
            "task-1",
            "--agent-type",
            "codex",
            "--target-session-id",
            "session-1",
            "--clear-context",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [r.url.path for r in captured] == ["/api/workspaces/tasks/task-1/start"]
    assert body(captured[0]) == {
        "agent_type": "codex",
        "target_session_id": "session-1",
        "clear_context": True,
    }


def test_create_only_records_while_run_dispatches(monkeypatch):
    def handler(request):
        return (
            httpx.Response(200, json={"id": "session-1"})
            if request.url.path.endswith("/agent")
            else httpx.Response(201, json=TASK)
        )

    captured = patch_client(monkeypatch, handler)
    for operation, expected in [
        ("create", ["/api/workspaces/ws1/tasks"]),
        (
            "run",
            [
                "/api/workspaces/ws1/agent",
                "/api/workspaces/ws1/tasks",
                "/api/workspaces/tasks/task-1/start",
            ],
        ),
    ]:
        captured.clear()
        result = CliRunner().invoke(
            cli, ["task", operation, "ws1", "--title", "T", "--prompt", "P"]
        )
        assert result.exit_code == 0, result.output
        assert [r.url.path for r in captured] == expected
        assert (
            body(next(r for r in captured if r.url.path.endswith("/tasks")))["execution_control"]
            == "workspace"
        )


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("create", {"reporter_key": "secret"}),
        ("run", {"new_reporter_key": "secret"}),
        ("run", {"execution_control": "initiator"}),
        ("create", {"execution_control": "initiator"}),
        ("update", {"execution_control": "initiator"}),
    ],
)
def test_old_commands_cannot_smuggle_control_or_keys(monkeypatch, operation, payload):
    captured = patch_client(monkeypatch, lambda r: httpx.Response(500, json={}))
    args = ["task", operation, "task-1" if operation == "update" else "ws1"]
    if operation != "update":
        args += ["--title", "T", "--prompt", "P"]
    result = CliRunner().invoke(cli, args + ["--payload-json", json.dumps(payload)])
    assert result.exit_code != 0 and captured == []


def test_custom_headers_do_not_override_hub_identity():
    with HubClient(
        base_url="http://testserver", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        for header in (
            {"Authorization": "Bearer attacker"},
            {"COOKIE": "claude_hub_session=attacker"},
        ):
            with pytest.raises(ValueError, match="cannot override Hub authentication"):
                client.request_response("GET", "/api/auth/check", headers=header)


def test_context_explicit_identity_is_separate_from_tab_observation(monkeypatch):
    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "main-tab")
    observed = {
        "tab_id": "main-tab",
        "reason": "active",
        "tasks": [{"id": "different-task", "workspace_id": "other-ws"}],
        "workspace_ids": ["other-ws"],
        "execution_ref": {
            "provider": "claude",
            "session_id": "main-session",
            "thread_id": "main-thread",
            "turn_id": "main-turn",
            "run_epoch": 2,
        },
    }

    def handler(request):
        if request.url.path.endswith("/task-context"):
            return httpx.Response(200, json=observed)
        if request.url.path.endswith("/board"):
            return httpx.Response(200, json={"tasks": [TASK]})
        return httpx.Response(200, json=CAPABILITIES)

    captured = patch_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli, ["--json", "task", "context", "--workspace-id", "ws1", "--task-id", "task-1"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["workspace_id"] == "ws1" and data["task_id"] == "task-1"
    assert data["observed_task_ids"] == ["different-task"]
    assert data["execution_ref"]["thread_id"] == "main-thread"
    assert "title" not in data and "latest_progress" not in data
    assert [r.url.path for r in captured] == [
        "/api/workspaces/tabs/main-tab/task-context",
        "/api/workspaces/ws1/board",
        "/api/workspaces/ws1/task-capabilities",
    ]
    assert all(r.method == "GET" for r in captured)


def test_context_one_observed_task_is_only_a_lookup_clue(monkeypatch):
    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "main-tab")
    observed = {
        "tab_id": "main-tab",
        "reason": "active",
        "tasks": [{"id": "task-1", "workspace_id": "ws1"}],
        "workspace_ids": ["ws1"],
        "execution_ref": {"provider": "codex", "thread_id": "main-thread"},
    }

    def handler(request):
        if request.url.path.endswith("/task-context"):
            return httpx.Response(200, json=observed)
        if request.url.path == "/api/workspaces":
            return httpx.Response(200, json=[{"id": "ws1"}])
        if request.url.path.endswith("/board"):
            return httpx.Response(200, json={"tasks": [TASK]})
        return httpx.Response(200, json=CAPABILITIES)

    captured = patch_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "task", "context"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["task_id"] == "task-1" and data["observation_reason"] == "active"
    assert [r.url.path for r in captured] == [
        "/api/workspaces/tabs/main-tab/task-context",
        "/api/workspaces",
        "/api/workspaces/ws1/board",
        "/api/workspaces/ws1/task-capabilities",
    ]


@pytest.mark.parametrize("reason", ["inactive", "not_observed", "ambiguous"])
def test_context_nonactive_observation_does_not_claim_ref(monkeypatch, reason):
    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "main-tab")

    def handler(request):
        if request.url.path.endswith("/task-context"):
            return httpx.Response(
                200,
                json={
                    "tab_id": "main-tab",
                    "reason": reason,
                    "tasks": [],
                    "workspace_ids": ["ws1"],
                    "execution_ref": {"thread_id": "stale-thread"},
                },
            )
        if request.url.path.endswith("/board"):
            return httpx.Response(200, json={"tasks": [TASK]})
        return httpx.Response(200, json=CAPABILITIES)

    captured = patch_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["--json", "task", "context", "--workspace-id", "ws1"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["task_id"] is None and data["execution_ref"] is None
    assert data["observation_reason"] == reason
    assert data["record_only_requires_reporter_key"] is True
    assert data["handoff_requires_release"] is True
    assert data["legacy_chat_work_create"] is False
    assert all(r.method == "GET" for r in captured)


@pytest.mark.parametrize("tasks", [[], [{"id": "task-1"}, {"id": "task-2"}]])
def test_context_rejects_missing_or_ambiguous_identity_without_board_scan(monkeypatch, tasks):
    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "main-tab")
    captured = patch_client(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "tab_id": "main-tab",
                "reason": "active",
                "tasks": tasks,
                "workspace_ids": ["ws1"],
            },
        ),
    )
    result = CliRunner().invoke(cli, ["task", "context"])
    assert result.exit_code != 0 and "not unique" in result.output
    assert [r.url.path for r in captured] == ["/api/workspaces/tabs/main-tab/task-context"]


def test_context_without_identity_fails_before_http(monkeypatch):
    captured = patch_client(monkeypatch, lambda r: httpx.Response(500, json={}))
    result = CliRunner().invoke(cli, ["task", "context"])
    assert result.exit_code != 0 and "Pass --workspace-id/--task-id" in result.output
    assert captured == []
