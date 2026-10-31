"""AntigravitySession: native transport for the Antigravity CLI (``agy``).

Each Chat turn spawns ``agyx --input-format stream-json
--output-format stream-json`` and writes one NDJSON user record on stdin::

    {"event": "user", "message": {"content": "<prompt>"}}

stdout is agy's NDJSON event stream: ``init`` (carrying the conversation id),
``step_update`` (tools and the assistant text) and a terminal ``result``.
The conversation id from ``init`` is persisted so the next turn resumes with
``--conversation <id>``.

``agyx`` is a thin local wrapper (``~/.local/bin/agyx``) that provides the
proxy/TLS environment the consumer sign-in needs; the binary name Hub spawns
is therefore the wrapper, never ``agy`` directly.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ....models import ChatMode, ManagedSession, StreamModeOption
from .process import ProcessSession


class AntigravitySession(ProcessSession):
    """Antigravity CLI one-shot transport over stream-json stdout."""

    adapter_id = "antigravity-native"
    schema_version = 1

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> None:
        super().__init__(
            session,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )

    def accepts_notification(self, record: Dict[str, Any]) -> bool:
        # One-shot process: every stdout record belongs to the single turn in
        # flight. agy has no background-task results like resumed Claude.
        return True

    def available_modes(self) -> List[StreamModeOption]:
        # The probe takes the binary name (agyx), not the provider id.
        if self._runtime.help_confirms_plan_mode("agyx", "--mode"):
            return [self._runtime.default_mode(), self._runtime.plan_mode()]
        return [self._runtime.default_mode()]

    def _resume_arg(self) -> List[str]:
        """agy's ``--conversation <id>`` only accepts an id the CLI itself
        issued (captured from ``init``); never pass an unverified id."""
        sid = self._conversation_id
        if sid and self._conversation_id_verified:
            return ["--conversation", sid]
        return []

    def _build_command(self, provider_env: Optional[Dict[str, str]] = None) -> List[str]:
        cmd = [
            "agyx",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            # Hub drives approvals itself; agy's slash-command expansion only
            # matters in its interactive TUI.
            "--disable-slash-commands",
        ]
        if self.session.solo_mode and self._current_mode == ChatMode.DEFAULT.value:
            cmd.append("--dangerously-skip-permissions")
        if self._current_mode == ChatMode.PLAN.value:
            cmd.extend(["--mode", "plan"])
        if provider_env:
            model = provider_env.get("ANTIGRAVITY_MODEL", "").strip()
            if model:
                cmd.extend(["--model", model])
        cmd.extend(self._resume_arg())
        return cmd

    def _build_stdin(self, text: str) -> str:
        return json.dumps({"event": "user", "message": {"content": text}})

    async def _build_provider_env(self) -> Dict[str, str]:
        """Skip Hub's direct/proxy route selection.

        agyx owns the network path itself (it starts the local eligibility
        bypass proxy that chains to the deployment egress and pins
        SSL_CERT_FILE), so the generic endpoint resolver — which only knows
        claude/codex/cursor/maso — must not reject the provider. Session env
        overrides (e.g. ANTIGRAVITY_MODEL) still apply via ``_build_env``.
        """
        return self._build_env()

    async def _send_text(self, text: str) -> None:
        provider_env = await self._build_provider_env()
        self.check_delivery_admission()
        cmd = self._build_command(provider_env)
        stdin_text = self._build_stdin(text)
        await self._spawn_oneshot(cmd, stdin_text)

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Capture the conversation id from agy's ``init`` record."""
        if record.get("event") != "init":
            return
        sid = record.get("conversation_id")
        if isinstance(sid, str) and sid:
            self._persist_conversation_id(sid)
            self._handshake_complete = True
