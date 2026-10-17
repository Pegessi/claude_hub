"""Public Codex/TraeX adapters with stable source-discovery patch targets.

Normalization and per-session attribution live under ``transcripts``. These
concrete classes preserve the established adapter identities and inheritance.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    ManagedSession,
    StreamCapabilities,
)
from ...models.agent_stream import normalize_provider_usage
from ..ttyd_manager import (
    _codex_candidates_for_cwd,
    _codex_scan_sessions,
    _pick_backfill_session,
)
from .base import (
    AgentStreamAdapter,
    NormalizeContext,
    discover_source_cached,
    resolve_cwd,
    resolve_process_hint,
)
from .fork_seed import strip_fork_seed_history
from .native import (
    _CODEX_QUESTION_METHODS,
    codex_normalize_questions,
    strip_hub_runtime_guidance,
)
from .transcripts.codex import CodexTranscript
from .transcripts.codex_values import _CMD_RE as _CMD_RE
from .transcripts.codex_values import _CODE_MODE_NESTED_PREFIX as _CODE_MODE_NESTED_PREFIX
from .transcripts.codex_values import _COLLAB_MESSAGE_TOOLS as _COLLAB_MESSAGE_TOOLS
from .transcripts.codex_values import _COLLAB_SPAWN_TOOLS as _COLLAB_SPAWN_TOOLS
from .transcripts.codex_values import _FLAT_OBJ_RE as _FLAT_OBJ_RE
from .transcripts.codex_values import _TRANSIENT_NOTICE_MESSAGE_ID as _TRANSIENT_NOTICE_MESSAGE_ID
from .transcripts.codex_values import _TRANSIENT_NOTICE_RE as _TRANSIENT_NOTICE_RE
from .transcripts.codex_values import _clean_thread_id as _clean_thread_id
from .transcripts.codex_values import _codex_extract_text as _codex_extract_text
from .transcripts.codex_values import _codex_parse_arguments as _codex_parse_arguments
from .transcripts.codex_values import _codex_tool_args as _codex_tool_args
from .transcripts.codex_values import _transient_provider_notice as _transient_provider_notice
from .transcripts.traex import TraexTranscript


class CodexJsonlAdapter(CodexTranscript):
    """Codex transcript adapter with legacy source lookup dependencies."""

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        cwd = resolve_cwd(session)
        candidates = _codex_candidates_for_cwd(cwd)
        if not candidates:
            return None

        _, agent_session_id = resolve_process_hint(session)
        if agent_session_id:
            return self._find_pinned(candidates, agent_session_id, cwd)

        created = self._session_created_epoch(session)
        if created is None:
            return None
        scored = [(abs(start - created), sid, cand_path) for start, sid, cand_path in candidates]
        picked = _pick_backfill_session(created, scored)
        if picked:
            for _start, sid, cand_path in candidates:
                if sid == picked:
                    p = Path(cand_path)
                    return p if p.is_file() else None
        return None

    @staticmethod
    def _find_pinned(candidates: List[tuple], agent_session_id: str, cwd: str) -> Optional[Path]:
        for _start, sid, path in candidates:
            if sid == agent_session_id:
                p = Path(path)
                return p if p.is_file() else None
        entry = _codex_scan_sessions().get(agent_session_id)
        if entry is None or not entry.cwd:
            return None
        try:
            same_cwd = Path(entry.cwd).resolve() == Path(cwd).resolve()
        except OSError:
            same_cwd = entry.cwd == cwd
        if same_cwd:
            p = Path(entry.path)
            return p if p.is_file() else None
        return None

    @staticmethod
    def _session_created_epoch(session: ManagedSession) -> Optional[float]:
        created = session.created_at
        if isinstance(created, datetime):
            return created.timestamp()
        if isinstance(created, (int, float)):
            return float(created)
        return None


class TraexJsonlAdapter(TraexTranscript, CodexJsonlAdapter):
    """Live TraeX adapter preserving the Codex adapter relationship."""
