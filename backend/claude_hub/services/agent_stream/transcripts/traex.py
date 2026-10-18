"""TraexTranscript with per-adapter state inherited from its typed owner."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ....models import AgentStreamEvent, AgentStreamEventType, ManagedSession
from ..base import NormalizeContext
from .codex import CodexTranscript


class TraexTranscript(CodexTranscript):
    """Normalize live TraeX notifications without discovering Codex rollouts.

    TraeX terminal transcripts and edit-resend are not supported yet.
    """

    adapter_id = "traex-jsonl"
    # Terminal transcript structured surface is not wired (see docstring); only
    # the native app-server powers the structured Chat view.
    supports_transcript_discovery = False

    def _normalize_notification(
        self, method: str, params: Any, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        if isinstance(params, dict):
            text = self._traex_status_text(method, params)
            if text:
                return [
                    ctx.event(
                        AgentStreamEventType.STATUS,
                        {
                            "text": text,
                            "provider_status": method,
                            "snapshot": True,
                        },
                        message_id=f"traex-status:{method}",
                    )
                ]
        return super()._normalize_notification(method, params, ctx)

    @staticmethod
    def _traex_status_text(method: str, params: Dict[str, Any]) -> Optional[str]:
        """Translate TraeX-only lifecycle notices into visible timeline text."""
        provider_message = params.get("message")
        message = provider_message.strip() if isinstance(provider_message, str) else ""
        if method == "queue/status":
            state = params.get("state")
            if state == "queued":
                position = params.get("position")
                fallback = (
                    f"Queued for model capacity (position {position})."
                    if isinstance(position, int) and not isinstance(position, bool)
                    else "Queued for model capacity."
                )
                return message or fallback
            if state == "waiting":
                return message or "Waiting for model capacity."
            if state == "ready":
                return message or "Model is ready; starting the response."
            return message or "Model queue status updated."
        if method == "model/loopDetectedRecovering":
            attempt = params.get("attempt")
            maximum = params.get("maxAttempts")
            suffix = (
                f" (attempt {attempt}/{maximum})"
                if isinstance(attempt, int) and isinstance(maximum, int)
                else ""
            )
            reason = params.get("reason")
            detail = reason.strip() if isinstance(reason, str) else ""
            punctuation = f": {detail}" if detail else "."
            return message or f"Response loop detected; recovering{suffix}{punctuation}"
        if method in {"model/rerouted", "model/fallback"}:
            return message or (
                "The request was rerouted to another model."
                if method == "model/rerouted"
                else "The requested model was unavailable; using a fallback."
            )
        if method == "warning":
            return message or "TraeX reported a warning."
        return None

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        return None
