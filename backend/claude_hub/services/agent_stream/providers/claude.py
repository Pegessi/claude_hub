"""ClaudeSession implementation; compatibility dependencies are explicit."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from ....models import ChatMode, ManagedSession, StreamModeOption
from ..provider_network import ProviderEndpointUnknownError, ProviderNetworkError
from .process import ProcessSession


class ClaudeSession(ProcessSession):
    """Claude Code native transport via ``stream-json`` stdout.

    Each turn spawns ``claude --print --input-format stream-json
    --output-format stream-json --include-partial-messages`` and writes a
    single SDKUserMessage JSONL record on stdin, then closes stdin for the
    one-shot turn. The conversation id from the system init record is
    captured so the next turn resumes via ``--resume``.

    Images are sent as ``image`` content blocks inside the user message
    envelope (``source.type=base64``). The ``--attach`` flag does not exist
    in Claude Code 2.1.x and the ``[IMAGE:base64]`` inline token was never
    verified; the SDKUserMessage envelope is the documented mechanism.
    """

    adapter_id = "claude-native"
    schema_version = 1
    # Capability flags (tool timeline, SDKUserMessage base64 images, native
    # system instructions, exact goal usage) come from the ProviderSpec
    # registry via SessionCore property defaults.

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(
            session,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        # Staged image bytes for the next turn's user message content blocks.
        self._staged_images: List[bytes] = []
        self._network_settings_by_gen: Dict[int, Path] = {}
        self._validated_tab_settings_payload: Optional[Dict[str, Any]] = None
        self._validated_claude_launch_env: Optional[Dict[str, str]] = None
        self._tab_endpoint_is_authoritative = False
        self._input_message_uuid: Optional[str] = None

    def accepts_notification(self, record: Dict[str, Any]) -> bool:
        """Only the submitted input's result may release its turn guard.

        A resumed Claude process can emit background-task results before it
        answers stdin, including empty zero-turn results for batched notices.
        Admitting those would retire stdout and discard the real answer.
        Prefer explicit input correlation; older CLIs may only give provenance.
        Unattributed legacy results and legitimate zero-turn commands still end
        their turn. This runs before normalization can clear adapter state.
        """
        if record.get("type") != "result" or self._input_message_uuid is None:
            return True
        linked: set[str] = set()
        singular = record.get("user_message_uuid")
        if isinstance(singular, str) and singular:
            linked.add(singular)
        plural = record.get("user_message_uuids")
        if isinstance(plural, list):
            linked.update(value for value in plural if isinstance(value, str) and value)
        if linked:
            return self._input_message_uuid in linked
        origin = record.get("origin")
        kind = origin.get("kind") if isinstance(origin, dict) else None
        return kind not in ("task-notification", "peer")

    def _tab_settings_path(self) -> Optional[Path]:
        tab_id = getattr(self.session, "tab_id", None)
        if not tab_id:
            return None
        path = self._runtime.runtime_home() / "launch_env" / f"{tab_id}.settings.json"
        return path if path.exists() or path.is_symlink() else None

    def _provider_configuration_env(self) -> Dict[str, str]:
        env = self._build_env()
        settings_path = self._tab_settings_path()
        if settings_path is None:
            self._validated_tab_settings_payload = {}
            self._validated_claude_launch_env = dict(env)
            self._tab_endpoint_is_authoritative = False
            env.pop("ANTHROPIC_BASE_URL", None)
            return env
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderNetworkError(
                "Claude tab settings file is unreadable or malformed"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderNetworkError("Claude tab settings file must contain a JSON object")
        settings_env = payload.get("env", {})
        if not isinstance(settings_env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in settings_env.items()
        ):
            raise ProviderNetworkError("Claude tab settings env must contain only string values")
        self._validated_tab_settings_payload = payload
        env.update(settings_env)
        self._validated_claude_launch_env = dict(env)
        endpoint = settings_env.get("ANTHROPIC_BASE_URL")
        self._tab_endpoint_is_authoritative = isinstance(endpoint, str) and bool(endpoint.strip())
        if not self._tab_endpoint_is_authoritative:
            env.pop("ANTHROPIC_BASE_URL", None)
        return env

    async def _build_provider_env(self) -> Dict[str, str]:
        selected = await super()._build_provider_env()
        if not self._tab_endpoint_is_authoritative:
            if self._validated_claude_launch_env is None:
                raise ProviderNetworkError("Claude launch environment was not validated")
            return dict(self._validated_claude_launch_env)
        return selected

    def _write_selected_settings(self, provider_env: Dict[str, str]) -> Optional[Path]:
        mode = provider_env.get(self._runtime.network_mode_env(), "auto").strip().lower()
        if mode == "inherit":
            return None
        if not self._tab_endpoint_is_authoritative:
            if mode == "auto":
                return None
            raise ProviderEndpointUnknownError(
                "forced Claude network mode requires ANTHROPIC_BASE_URL in tab settings"
            )
        try:
            endpoint = self._runtime.resolve_endpoint("claude", provider_env)
        except ProviderEndpointUnknownError:
            if mode == "auto":
                return None
            raise
        if self._validated_tab_settings_payload is None:
            raise ProviderNetworkError("Claude tab settings were not validated before launch")
        payload = copy.deepcopy(self._validated_tab_settings_payload)
        settings_env = payload.setdefault("env", {})
        if not isinstance(settings_env, dict):
            raise ProviderNetworkError("Claude tab settings env must be an object")
        settings_env["ANTHROPIC_BASE_URL"] = endpoint
        # Claude settings env has higher precedence than the process env. Set
        # every proxy spelling so a user-level settings file cannot silently
        # restore a route that the preflight did not select.
        for lower, upper in zip(
            self._runtime.proxy_env_names()[::2], self._runtime.proxy_env_names()[1::2]
        ):
            # An explicitly empty lowercase setting disables the uppercase
            # proxy, matching the precedence used during route selection.
            value = provider_env[lower] if lower in provider_env else provider_env.get(upper, "")
            settings_env[lower] = value
            settings_env[upper] = value
        launch_dir = self._runtime.runtime_home() / "launch_env"
        launch_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix=f"{getattr(self.session, 'tab_id', 'chat')}.network-",
            suffix=".settings.json",
            dir=str(launch_dir),
        )
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            os.chmod(path, 0o600)
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return path

    def _build_command(self, settings_path: Optional[Path] = None) -> List[str]:
        cmd = [
            "claude",
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # Teach the agent to treat the non-interactive auto-decline of
            # AskUserQuestion as "still pending in the UI — wait" rather than
            # "the user declined". A system prompt (not a user message) so it
            # is not echoed into the transcript; persists on every --resume.
            "--append-system-prompt",
            (
                f"{self._runtime.question_guidance()}\n\n{self._dot_system_pack}"
                if self._dot_system_pack
                else self._runtime.question_guidance()
            ),
        ]
        if self.session.solo_mode and self._current_mode == ChatMode.DEFAULT.value:
            cmd.append("--dangerously-skip-permissions")
        if self._current_mode == ChatMode.PLAN.value:
            cmd.extend(["--permission-mode", "plan"])
        cmd.extend(self._resume_arg())
        # Chat turns are one-shot subprocesses whose env only arrives via the
        # process environment — the lowest-precedence source in Claude Code,
        # below user-level ~/.claude/settings.json. Re-assert the tab env
        # through the same per-tab settings file terminal launches pass
        # (written by ttyd_manager), so the tab's configured provider wins
        # over any user-level settings env (e.g. a global relay override).
        effective_settings_path = settings_path or self._tab_settings_path()
        if effective_settings_path is not None:
            cmd.extend(["--settings", str(effective_settings_path)])
        return cmd

    def available_modes(self) -> List[StreamModeOption]:
        if self._runtime.help_confirms_plan_mode("claude", "--permission-mode"):
            return [self._runtime.default_mode(), self._runtime.plan_mode()]
        return [self._runtime.default_mode()]

    async def rotate_conversation(self) -> str:
        return str(uuid4())

    def _resume_arg(self) -> List[str]:
        """Claude: ``--session-id`` creates the conversation; ``--resume``
        requires it to exist. Use ``--session-id`` for the constructive
        (unverified) first turn, then ``--resume`` once the id is confirmed."""
        sid = self._conversation_id
        if not sid:
            return []
        if self._conversation_id_verified:
            return ["--resume", sid]
        return ["--session-id", sid]

    def _build_stdin(self, text: str) -> str:
        """Build the SDKUserMessage JSONL record for stdin.

        Envelope shape (verified against Claude Code's SDK input format):

            {"type":"user","message":{"role":"user","content":[
              {"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}},
              {"type":"text","text":"<prompt>"}
            ]},"parent_tool_use_id":null,"session_id":null}

        For text-only turns the content may be a single text block or a plain
        string; we always emit a list of blocks for consistency.
        """
        content: List[Dict[str, Any]] = []
        for img in self._staged_images:
            media_type = self._runtime.detect_image_mime(img)
            if media_type is None:
                # Should have been rejected in _stage_images, but fail closed.
                raise ValueError("unsupported image format")
            b64 = base64.b64encode(img).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                }
            )
        content.append({"type": "text", "text": text})
        self._input_message_uuid = str(uuid4())
        envelope = {
            "type": "user",
            "uuid": self._input_message_uuid,
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": None,
        }
        return json.dumps(envelope)

    async def _send_text(self, text: str) -> None:
        provider_env = await self._build_provider_env()
        self.check_delivery_admission()
        settings_path = self._write_selected_settings(provider_env)
        cmd = self._build_command(settings_path)
        stdin_text = self._build_stdin(text)
        # Clear staged images after they've been incorporated into the
        # envelope for this turn.
        self._staged_images = []
        generation = self._stdout_generation + 1
        if settings_path is not None:
            self._network_settings_by_gen[generation] = settings_path
        try:
            self._prepared_provider_env = provider_env
            await self._spawn_oneshot(cmd, stdin_text)
        except (Exception, asyncio.CancelledError):
            path = self._network_settings_by_gen.pop(generation, None)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            self._prepared_provider_env = None

    async def _drain_oneshot_stdout(self, generation: int) -> None:
        try:
            await super()._drain_oneshot_stdout(generation)
        finally:
            path = self._network_settings_by_gen.pop(generation, None)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def stop(self) -> None:
        await super().stop()
        for path in self._network_settings_by_gen.values():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._network_settings_by_gen.clear()

    def _stage_images(self, images: List[bytes]) -> None:
        """Stage images for the next turn's user message content blocks.

        Images are validated by magic bytes and stored as raw bytes; they are
        base64-encoded into the SDKUserMessage envelope on the next
        ``_send_text`` call.
        """
        if not images:
            return
        for img in images:
            media_type = self._runtime.detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            self._staged_images.append(img)

    def _clear_staged_images(self) -> None:
        self._staged_images = []

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Extract the conversation id from Claude's system init record.

        Claude emits a top-level ``{type:"system", subtype:"init",
        session_id:"..."}`` record at the start of each ``--print`` run. That
        ``session_id`` is the value to pass to ``--resume`` on the next turn.
        ``message_start.message.id`` is a per-message id, not the conversation
        id, so it must NOT be used for resume.
        """
        if record.get("type") != "system":
            return
        if record.get("subtype") != "init":
            return
        sid = record.get("session_id")
        if isinstance(sid, str) and sid:
            self._persist_conversation_id(sid)
            self._handshake_complete = True
