"""Manager status responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Tuple

from ....models import AgentRuntimeStatus, AgentType, SessionKind, TerminalAgentStatus
from ...agent_status_markers import codex_output_is_working
from ..defaults import (
    _ANSI_ESCAPE_RE,
    _ATTENTION_TAIL_PATTERNS,
    _BARE_SHELL_PROMPT_RE,
    _CLAUDE_WORKING_STATUS_RE,
    _CURSOR_WORKING_STATUS_RE,
    _IDLE_TAIL_HINTS,
    _STATUS_CACHE_TTL_SECONDS,
    _WORKING_FRAME_STALE_SECONDS,
    _WORKING_TAIL_PATTERNS,
)
from ..records import _ReplyExcerptCacheEntry
from .host import ManagerHost

if TYPE_CHECKING:
    from ...agent_stream.store import ReplyExcerpt
    from ...ttyd_manager import TTYDProcess


logger = logging.getLogger("claude_hub.services.ttyd_manager")


def tab_stream_identity(
    tab_id: str,
    workspace_id: Optional[str],
    managed_session_id: Optional[str],
) -> tuple[str, str]:
    """Where this tab's structured stream actually lives.

    A Workspace Chat persists its stream under its managed session
    (``AgentStreamStore(workspace_id, managed_session_id)``, see
    ``services/managed_chat``), while a direct Chat uses the terminal-tab
    namespace. Reading the wrong namespace yields no events at all: no reply
    excerpt, no unread flag, no activity time — which is exactly what the
    Agents scope of the inbox would have shown.
    """
    managed = (managed_session_id or "").strip()
    workspace = (workspace_id or "").strip()
    if managed and workspace:
        return workspace, managed
    return "terminal-tabs", f"terminal-tab-{tab_id}"


def _reply_excerpt_entry(
    path: Path, cached: Optional[_ReplyExcerptCacheEntry]
) -> Optional[_ReplyExcerptCacheEntry]:
    """Stat the stream and rescan it only when it actually grew.

    Runs in a worker thread: both the stat and the bounded tail scan are
    blocking file I/O, and the status poll covers ~40 tabs every 5 seconds.
    """
    # Deferred import: the agent_stream package imports this module's graph.
    from ...agent_stream.store import (
        _REPLY_EXCERPT_MAX_CHARS,
        _REPLY_EXCERPT_SCAN_BYTES,
        read_reply_excerpt,
    )

    try:
        stat_result = path.stat()
    except OSError:
        return None
    if (
        cached is not None
        and cached.mtime_ns == stat_result.st_mtime_ns
        and cached.size == stat_result.st_size
    ):
        return cached
    return _ReplyExcerptCacheEntry(
        mtime_ns=stat_result.st_mtime_ns,
        size=stat_result.st_size,
        excerpt=read_reply_excerpt(path, _REPLY_EXCERPT_MAX_CHARS, _REPLY_EXCERPT_SCAN_BYTES),
    )


class ManagerStatusMixin(ManagerHost):
    def _classify_agent_status(
        self,
        process: TTYDProcess,
        output: str,
        output_hash: str,
        foreground_command: Optional[str],
    ) -> tuple[AgentRuntimeStatus, str, Optional[str], Optional[datetime]]:
        now = self._runtime.datetime.now()
        snapshot = self._status_snapshots.get(process.tab_id)
        last_hash = snapshot.get("hash") if snapshot else None
        last_changed_at = snapshot.get("last_changed_at") if snapshot else None
        frame_first_seen_at = snapshot.get("frame_first_seen_at") if snapshot else None

        if snapshot is None:
            frame_first_seen_at = now
            self._status_snapshots[process.tab_id] = {
                "hash": output_hash,
                "last_changed_at": None,
                "frame_first_seen_at": frame_first_seen_at,
            }
        elif last_hash != output_hash:
            last_changed_at = now
            frame_first_seen_at = now
            self._status_snapshots[process.tab_id] = {
                "hash": output_hash,
                "last_changed_at": last_changed_at,
                "frame_first_seen_at": frame_first_seen_at,
            }
        # else: identical frame — keep the existing frame_first_seen_at so we can
        # measure how long the screen has been frozen.

        # Aliveness is verified by the caller (``get_tab_agent_status``) before
        # classification, so we avoid a redundant per-tab ``tmux has-session``
        # subprocess here — that check previously blocked the event loop on
        # every board refresh.

        def working_or_stale() -> tuple[AgentRuntimeStatus, str, Optional[str], Optional[datetime]]:
            # A live agent repaints its spinner/elapsed counter every second, so
            # the captured frame keeps changing. If working markers are showing
            # but the frame has been frozen past the staleness window, the agent
            # has stopped behind a lingering "working" frame — flag it for
            # attention instead of reporting it as working forever.
            if (
                frame_first_seen_at is not None
                and (now - frame_first_seen_at).total_seconds() >= _WORKING_FRAME_STALE_SECONDS
            ):
                return (
                    AgentRuntimeStatus.ATTENTION,
                    "Agent may be stuck",
                    "working indicator has not changed; agent appears stopped",
                    last_changed_at,
                )
            return (
                AgentRuntimeStatus.WORKING,
                "Working",
                "agent is processing",
                last_changed_at,
            )

        # Only inspect the bottom of the visible screen — the current prompt
        # area. Historical scrollback is ignored so that words like
        # "Reading"/"editing" from past activity can't drive classification.
        non_empty = [line.strip() for line in output.splitlines() if line.strip()]
        tail_lines = non_empty[-5:]
        status_tail_lines = non_empty[-10:]
        tail = "\n".join(tail_lines).lower()
        status_tail = "\n".join(status_tail_lines).lower()
        last_line = tail_lines[-1] if tail_lines else ""

        for pattern in _ATTENTION_TAIL_PATTERNS:
            if pattern in tail:
                return (
                    AgentRuntimeStatus.ATTENTION,
                    "Agent waiting for input",
                    "needs your response",
                    last_changed_at,
                )

        # A bare shell prompt on the last line means the agent has returned to
        # an idle prompt. This takes priority over working markers higher in
        # the scrollback (e.g. a lingering "esc to interrupt" from the
        # just-finished turn), so an agent that has finished is promptly
        # classified idle instead of lingering in "working".
        if _BARE_SHELL_PROMPT_RE.search(last_line):
            return (
                AgentRuntimeStatus.IDLE,
                "Idle",
                "shell prompt visible",
                last_changed_at,
            )

        # Current Codex keeps "? for shortcuts" and its composer visible even
        # during a turn. Its explicit live indicator must win over that chrome;
        # the prompt alone is not evidence of idle. Preserve attention/shell
        # precedence and the frozen-frame guard.
        if process.agent_type == AgentType.CODEX and codex_output_is_working(
            output, allow_resized=True
        ):
            return working_or_stale()

        # Claude Code / Codex idle hints ("? for shortcuts", "/ for commands")
        # on the bottom lines also mean the agent is idle and waiting for
        # input. Like the shell prompt, these take priority over working
        # markers in the scrollback above.
        for hint in _IDLE_TAIL_HINTS:
            if hint in tail:
                return (
                    AgentRuntimeStatus.IDLE,
                    "Idle",
                    "agent prompt visible",
                    last_changed_at,
                )

        # Preserve TraeX's existing idle-hint precedence.
        if process.agent_type == AgentType.TRAEX and codex_output_is_working(output):
            return working_or_stale()

        for pattern in _WORKING_TAIL_PATTERNS:
            if pattern in status_tail:
                return working_or_stale()

        if _CLAUDE_WORKING_STATUS_RE.search("\n".join(status_tail_lines)):
            return working_or_stale()

        if _CURSOR_WORKING_STATUS_RE.search("\n".join(status_tail_lines)):
            return working_or_stale()

        if foreground_command and foreground_command in {
            "claude",
            "codex",
            "traex",
            "agent",
            # agyx execs the real agy binary, so the foreground process is agy.
            "agyx",
            "agy",
        }:
            return (
                AgentRuntimeStatus.IDLE,
                "Idle",
                f"{foreground_command} is waiting",
                last_changed_at,
            )

        return (
            AgentRuntimeStatus.IDLE,
            "Idle",
            "no activity indicators",
            last_changed_at,
        )

    def mark_tab_viewed(self, tab_id: str) -> bool:
        """Record that the user opened this tab (clears its unread flag).

        Returns True if the tab exists, False otherwise.
        """
        process = self.processes.get(tab_id)
        if process is None:
            return False
        # Use aware UTC so it compares cleanly with the event stream's
        # created_at (which is datetime.now(timezone.utc)).
        process.last_viewed_at = self._runtime.datetime.now(timezone.utc)
        self._save_state()
        return True

    async def _reply_excerpt(self, process: TTYDProcess) -> "Optional[ReplyExcerpt]":
        """Newest assistant text for this tab, reused until the stream grows.

        The status endpoint is polled for ~40 tabs every 5 seconds, so this
        stays cheap and off the event loop: the stat and any tail scan run in a
        worker thread, and a streaming turn does not reach here at all (see the
        caller, which skips the read while the turn is working).
        """
        from ...agent_stream.store import AgentStreamStore

        # Owners built without ``ManagerStateMixin.__init__`` (constructed
        # directly in tests) have no cache yet; create it on first use instead
        # of failing a best-effort status read.
        cache: Optional[Dict[Tuple[str, str], _ReplyExcerptCacheEntry]] = getattr(
            self, "_reply_excerpt_cache", None
        )
        if cache is None:
            cache = {}
            self._reply_excerpt_cache = cache

        store = AgentStreamStore(
            *tab_stream_identity(process.tab_id, process.workspace_id, process.managed_session_id)
        )
        cache_key = (process.tab_id, str(store.path))
        cached = cache.get(cache_key)
        try:
            entry = await asyncio.to_thread(_reply_excerpt_entry, store.path, cached)
        except Exception as e:  # pragma: no cover - defensive, best effort only
            logger.debug(f"Unable to read reply excerpt for tab {process.tab_id}: {e}")
            entry = None
        if entry is None:
            cache.pop(cache_key, None)
            return None
        cache[cache_key] = entry
        return entry.excerpt

    async def compute_tab_unread(self, process: TTYDProcess) -> bool:
        """Return True when the tab's latest completed turn is unread.

        A turn that completed after the user's ``last_viewed_at`` is unread;
        a tab that has never been viewed is unread once it has a completed turn.
        """
        # Deferred import to avoid a circular import (agent_stream is a
        # sibling service; the fork code at the top of this file does the same).
        from ...agent_stream.store import AgentStreamStore

        store = AgentStreamStore(
            *tab_stream_identity(process.tab_id, process.workspace_id, process.managed_session_id)
        )
        completed_at_str = await store.latest_turn_completed_at()
        if completed_at_str is None:
            return False
        try:
            completed_at = self._runtime.datetime.fromisoformat(completed_at_str)
        except ValueError:
            return False
        if process.last_viewed_at is None:
            return True
        return completed_at > process.last_viewed_at

    async def get_tab_agent_status(
        self,
        tab_id: str,
        use_cache: bool = True,
        live_sessions: Optional[set[str]] = None,
    ) -> Optional[TerminalAgentStatus]:
        """Get a best-effort terminal agent status for one tab.

        When ``live_sessions`` is provided (a snapshot of live tmux session
        names from a single ``tmux list-sessions`` call), tmux existence is
        checked against that set instead of spawning a per-tab subprocess.
        """
        process = self.processes.get(tab_id)
        if not process:
            return None

        sampled_at = self._runtime.datetime.now()
        if (
            process.session_kind == SessionKind.CHAT
            and process.cursor_transport != "terminal_transcript"
        ):
            # Deferred import: the agent_stream package imports this manager's
            # module graph, so a module-level import would be circular. The
            # inbox wording lives next to the tool labels that use it.
            from ...agent_stream.activity_labels import IDLE_DETAIL

            native = self._runtime.get_tab_native_runtime_snapshot(tab_id)
            # Producers older than the inbox contract pass a bare snapshot, so
            # read every new field defensively instead of assuming it exists.
            pending_question = bool(getattr(native, "pending_question", False)) if native else False
            activity = getattr(native, "activity", None) if native else None
            activity_since = getattr(native, "activity_since", None) if native else None
            if native is None:
                if self._runtime.native_provider_binary_available(process.agent_type):
                    native_runtime_status = AgentRuntimeStatus.IDLE
                    native_detail = IDLE_DETAIL
                else:
                    native_runtime_status = AgentRuntimeStatus.OFFLINE
                    native_detail = "native provider binary is unavailable"
            else:
                native_runtime_status = native.status
                native_detail = native.detail
            native_status_text = {
                AgentRuntimeStatus.IDLE: "Idle",
                AgentRuntimeStatus.WORKING: "Working",
                AgentRuntimeStatus.ATTENTION: "Needs attention",
                AgentRuntimeStatus.OFFLINE: "Offline",
            }[native_runtime_status]
            # A turn that is merely streaming shows the previous answer and
            # would rescan a growing file on every poll, so its excerpt read is
            # skipped and the activity line speaks for it. A turn that is ALSO
            # blocked on a question is the top "Waiting for your answer" row:
            # that row keeps its preview.
            excerpt = (
                None
                if (native_runtime_status == AgentRuntimeStatus.WORKING and not pending_question)
                else await self._reply_excerpt(process)
            )
            return TerminalAgentStatus(
                tab_id=process.tab_id,
                tab_name=process.name,
                agent_type=process.agent_type,
                status=native_runtime_status,
                status_text=native_status_text,
                detail=native_detail,
                tmux_session=process.tmux_session,
                last_changed_at=None,
                sampled_at=sampled_at,
                pending_question=pending_question,
                activity=activity if native is not None else None,
                activity_since=activity_since,
                last_reply_excerpt=excerpt.text if excerpt else None,
                last_reply_at=excerpt.written_at if excerpt else None,
            )

        cached = self._status_cache.get(tab_id)
        if (
            use_cache
            and cached
            and (sampled_at - cached.sampled_at).total_seconds() < _STATUS_CACHE_TTL_SECONDS
        ):
            return cached

        if live_sessions is not None:
            session_alive = process.tmux_session in live_sessions
        else:
            session_alive = await self._runtime._tmux_session_exists_async(process.tmux_session)
        if not session_alive:
            status = TerminalAgentStatus(
                tab_id=process.tab_id,
                tab_name=process.name,
                agent_type=process.agent_type,
                status=AgentRuntimeStatus.OFFLINE,
                status_text="Offline",
                detail="tmux session is not available",
                tmux_session=process.tmux_session,
                last_changed_at=None,
                sampled_at=sampled_at,
            )
            self._status_cache[tab_id] = status
            return status

        try:
            raw_output = await process.capture_history(lines=120)
        except Exception as e:
            logger.debug(f"Unable to capture status output for tab {tab_id}: {e}")
            raw_output = ""

        foreground_command = await process.capture_foreground_command()
        output = _ANSI_ESCAPE_RE.sub("", raw_output)
        output_hash = hashlib.sha256(output.encode("utf-8", errors="ignore")).hexdigest()
        runtime_status, status_text, detail, last_changed_at = self._classify_agent_status(
            process,
            output,
            output_hash,
            foreground_command,
        )

        status = TerminalAgentStatus(
            tab_id=process.tab_id,
            tab_name=process.name,
            agent_type=process.agent_type,
            status=runtime_status,
            status_text=status_text,
            detail=detail,
            tmux_session=process.tmux_session,
            last_changed_at=last_changed_at,
            sampled_at=sampled_at,
        )
        self._status_cache[tab_id] = status
        return status

    async def list_tab_agent_statuses(
        self,
        tab_ids: Optional[Iterable[str]] = None,
    ) -> list[TerminalAgentStatus]:
        """List best-effort terminal agent statuses in tab order.

        With no explicit ``tab_ids`` the snapshot matches ``list_tabs``:
        archived tabs are skipped. They have no runtime and exposing them here
        makes the sidebar's status poll see "unknown new tabs" every cycle,
        forcing a full tab-list refresh. Callers that pass ``tab_ids``
        explicitly (e.g. the workspace monitor) control their own sampling.
        """
        if tab_ids is None:
            ordered_ids = [
                tab_id
                for tab_id in self._tab_order
                if tab_id in self.processes and not self.processes[tab_id].archived
            ]
            ordered_ids.extend(
                tab_id
                for tab_id, process in self.processes.items()
                if tab_id not in ordered_ids and not process.archived
            )
        else:
            seen: set[str] = set()
            ordered_ids = []
            for tab_id in tab_ids:
                if tab_id in self.processes and tab_id not in seen:
                    ordered_ids.append(tab_id)
                    seen.add(tab_id)

        live_sessions = await self._runtime._tmux_list_sessions()
        results = await asyncio.gather(
            *(
                self.get_tab_agent_status(tab_id, live_sessions=live_sessions)
                for tab_id in ordered_ids
            )
        )
        return [status for status in results if status]
