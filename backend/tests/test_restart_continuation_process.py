"""Real launcher + real app shutdown/startup; fake providers and no tmux/network."""

import json
import os
import socket
import sys
import time
from pathlib import Path
from textwrap import dedent
from urllib.request import ProxyHandler, Request, build_opener

from claude_hub.service_launcher import ServiceLauncher
from claude_hub.services.service_restart import RestartStore


def test_planned_restart_continues_once_across_backend_processes(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fixture = tmp_path / "server.py"
    fixture.write_text(dedent("""
        import json, os
        from datetime import datetime
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import uvicorn
        from claude_hub import main
        from claude_hub.api import agent_stream as api, feishu_bot
        from claude_hub.auth.dependencies import get_current_user
        from claude_hub.models import AgentType, ChatMode, ExecutionTarget, SessionKind
        from claude_hub.services import feishu_bot_websocket, ttyd_manager, workspace_manager
        from claude_hub.services.agent_stream import tailer as tailers
        from claude_hub.services.agent_stream.native import ProviderSession

        tabs = {}
        for tab_id in ('working', 'waiting', 'idle'):
            tabs[tab_id] = SimpleNamespace(
                id=tab_id, name=tab_id, cwd=os.environ['EVIDENCE'], env={},
                agent_type=AgentType.CLAUDE, session_kind=SessionKind.CHAT,
                chat_mode=ChatMode.DEFAULT, target=ExecutionTarget.LOCAL,
                remote_cwd=None, remote_profile_id=None, remote_reconnect=True,
                solo_mode=True, cursor_transport='native', cursor_data_dir=None,
                cursor_cli_version=None, cursor_transcript_path=None, cursor_transcript_schema=None,
                agent_session_id='provider-' + tab_id, agent_session_id_verified=True,
                archived=False, workspace_id=None, workspace_role=None,
                managed_session_id=None, created_at=datetime.now(),
            )
        ttyd_manager.get_tab = tabs.get
        ttyd_manager.start_all_tabs = AsyncMock()
        ttyd_manager.cleanup = AsyncMock()
        workspace_manager.start_background_monitor = lambda: None
        workspace_manager.stop_background_monitor = AsyncMock(
            side_effect=RuntimeError('fixture monitor stop failure')
        )
        workspace_manager.sessions = {}
        workspace_manager.scheduled_task_runs = {}
        feishu_bot_websocket.prewarm_lark_sdk = lambda: None
        feishu_bot.start_feishu_websockets = AsyncMock()
        feishu_bot.quiesce_feishu_websockets = AsyncMock()

        async def stop_feishu_websockets(*, drain_timeout_seconds=None):
            manager = api._tab_tailer_manager
            active_tailers = manager.tailers() if manager is not None else []
            turn_in_flight = any(
                tailer.native_transport is not None and tailer.native_transport.turn_in_flight
                for tailer in active_tailers
            )
            row = {
                'drain_timeout_seconds': drain_timeout_seconds,
                'turn_in_flight': turn_in_flight,
                'ttyd_cleaned': ttyd_manager.cleanup.await_count > 0,
            }
            with (Path(os.environ['EVIDENCE']) / 'shutdowns.jsonl').open('a') as log:
                log.write(json.dumps(row) + '\\n')

        feishu_bot.stop_feishu_websockets = stop_feishu_websockets

        class MemoryProvider(ProviderSession):
            def _build_command(self): return []
            async def _send_text(self, text): pass
            async def send_message(self, text, images):
                await super().send_message(text, images)
                row = {'instance': os.environ['CLAUDE_HUB_INSTANCE_ID'],
                       'tab': self.session.tab_id, 'conversation': self._conversation_id,
                       'text': text}
                with (Path(os.environ['EVIDENCE']) / 'inputs.jsonl').open('a') as log:
                    log.write(json.dumps(row) + '\\n')
                if self.session.tab_id == 'waiting':
                    api._get_tab_tailer_manager().get_tailer(self.session.id)._blocking_approval_call_ids.add('question')

        tailers.create_native_session = lambda *args, **kwargs: MemoryProvider(*args, **kwargs)
        main.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='fixture')
        uvicorn.run(main.app, host='127.0.0.1', port=int(os.environ['PORT']), log_level='warning')
    """))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    store = RestartStore(runtime)
    launcher = ServiceLauncher(
        store,
        [sys.executable, str(fixture)],
        f"http://127.0.0.1:{port}/health",
        env={
            **os.environ,
            "CLAUDE_HUB_HOME": str(runtime),
            "CLAUDE_HUB_TMUX_SOCKET": "restart-test-private",
            "PORT": str(port),
            "EVIDENCE": str(tmp_path),
        },
        startup_timeout=15,
    )
    store.initialize(launcher.launcher_id)
    http = build_opener(ProxyHandler({}))

    def post(path: str, payload: dict, *, restart: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if restart:
            headers["X-Claude-Hub-Restart"] = "1"
        request = Request(
            f"http://127.0.0.1:{port}" + path,
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with http.open(request, timeout=5) as response:
            result = json.load(response)
            assert isinstance(result, dict)
            return result

    def inputs() -> list[dict]:
        path = tmp_path / "inputs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def shutdowns() -> list[dict]:
        path = tmp_path / "shutdowns.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    try:
        launcher.start_backend()
        launcher.wait_healthy()
        previous = launcher.instance_id
        for tab_id in ("working", "waiting"):
            post(
                f"/api/workspaces/tabs/{tab_id}/stream/send",
                {"text": "继续原任务", "client_turn_id": tab_id + "-turn"},
            )
        request = {
            "instance_id": previous,
            "request_id": "b0000000-0000-4000-8000-000000000001",
            "resume_running_chats": True,
        }
        post("/api/system/restart", request, restart=True)
        launcher.restart()
        assert shutdowns() == [
            {
                "drain_timeout_seconds": 0,
                "turn_in_flight": False,
                "ttyd_cleaned": True,
            }
        ]
        deadline = time.monotonic() + 10
        while len(inputs()) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        rows = inputs()
        assert [row["text"] for row in rows if row["tab"] == "working"] == [
            "继续原任务",
            "Please continue.",
        ]
        assert len([row for row in rows if row["tab"] == "waiting"]) == 1
        assert not any(row["tab"] == "idle" for row in rows)
        resumed = rows[-1]
        assert resumed["instance"] == launcher.instance_id != previous
        assert resumed["conversation"] == "provider-working"
        assert (
            post("/api/system/restart", request, restart=True)["operation"]["status"] == "succeeded"
        )
        # Another process with the same persisted operation must not replay a
        # started/uncertain attempt, even though it has lost all Python memory.
        launcher.stop_backend()
        launcher.start_backend()
        launcher.wait_healthy()
        time.sleep(0.3)
        assert len(inputs()) == 3
    finally:
        launcher.stop_backend()
