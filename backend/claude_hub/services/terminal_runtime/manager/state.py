"""Manager state responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from functools import cached_property
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ....config import settings
from ....models import (
    AgentType,
    ChatMode,
    ExecutionTarget,
    SessionKind,
    TerminalAgentStatus,
    WorkspaceSessionRole,
)
from ..defaults import _TABS_STATE_VERSION
from ..records import TabStateError, _AgentStatusSnapshot, _PendingTabCleanup
from .host import ManagerHost

if TYPE_CHECKING:
    from ...ttyd_manager import TTYDProcess


logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ManagerStateMixin(ManagerHost):
    _state_error: Optional[str] = None

    _state_confirmed = True

    _cleanup_retry_offset = 0

    _cleanup_scheduling_stopped = False

    def __init__(self) -> None:
        self.processes: Dict[str, TTYDProcess] = {}
        self._next_port = settings.ttyd_base_port
        self._tab_order: List[str] = []
        self._status_snapshots: Dict[str, _AgentStatusSnapshot] = {}
        self._status_cache: Dict[str, TerminalAgentStatus] = {}
        self._start_locks: Dict[str, asyncio.Lock] = {}
        logger.info("=" * 60)
        logger.info("Initializing TTYDManager - tmux session persistence enabled")
        logger.info("=" * 60)
        # Do not touch tmux during module import. Backend ownership is acquired
        # in FastAPI lifespan before start_all_tabs performs the tmux probe;
        # probing here lets a duplicate backend hang or mutate shared runtime
        # state before the single-instance guard can reject it.
        self._load_state()
        self._load_order()
        # Do not rewrite tab snapshot/order files on load. Legacy process
        # construction may still materialize provider launch settings.
        if self._state_error is None:
            self._sync_order_with_processes(persist=False)

    @cached_property
    def _cleanup_tasks(self) -> Dict[Tuple[str, str], asyncio.Task[bool]]:
        return {}

    @cached_property
    def _pending_stream_cleanup(self) -> Dict[str, _PendingTabCleanup]:
        return {}

    @cached_property
    def _cleanup_locks(self) -> Dict[str, asyncio.Lock]:
        return {}

    def _load_state(self) -> None:
        if self._runtime.STATE_FILE.exists():
            try:
                with open(self._runtime.STATE_FILE, "r") as f:
                    data, pending = self._runtime._parse_tabs_snapshot(json.load(f))
                    loaded: Dict[str, TTYDProcess] = {}
                    max_port = self._next_port
                    for tab_data in data:
                        agent_type_str = tab_data.get("agent_type", "claude")
                        agent_type = (
                            AgentType(agent_type_str)
                            if agent_type_str in [e.value for e in AgentType]
                            else AgentType.CLAUDE
                        )
                        session_kind_str = tab_data.get("session_kind")
                        if session_kind_str == "agent":
                            session_kind = SessionKind.CHAT
                        elif session_kind_str in [e.value for e in SessionKind]:
                            session_kind = SessionKind(session_kind_str)
                        else:
                            # Legacy rows (no session_kind) are raw TUI task
                            # runners (dispatcher / reviewer / worker) on the
                            # managed control plane. They must keep TERMINAL
                            # semantics — only an explicit persisted
                            # session_kind=chat marks a direct-user native
                            # Chat surface.
                            session_kind = SessionKind.TERMINAL
                        chat_mode_str = tab_data.get("chat_mode", ChatMode.DEFAULT.value)
                        chat_mode = (
                            ChatMode(chat_mode_str)
                            if chat_mode_str in [e.value for e in ChatMode]
                            else ChatMode.DEFAULT
                        )
                        target_str = tab_data.get("target", "local")
                        target = (
                            ExecutionTarget(target_str)
                            if target_str in [e.value for e in ExecutionTarget]
                            else ExecutionTarget.LOCAL
                        )
                        workspace_role_str = tab_data.get("workspace_role")
                        workspace_role = (
                            WorkspaceSessionRole(workspace_role_str)
                            if workspace_role_str in [e.value for e in WorkspaceSessionRole]
                            else None
                        )
                        process = self._runtime.TTYDProcess(
                            tab_id=tab_data["id"],
                            port=tab_data["port"],
                            name=tab_data["name"],
                            shell=tab_data.get("shell"),
                            cwd=tab_data.get("cwd"),
                            created_at=self._runtime.datetime.fromisoformat(tab_data["created_at"]),
                            solo_mode=tab_data.get("solo_mode", False),
                            agent_type=agent_type,
                            session_kind=session_kind,
                            chat_mode=chat_mode,
                            target=target,
                            remote_profile_id=tab_data.get("remote_profile_id"),
                            remote_cwd=tab_data.get("remote_cwd"),
                            remote_reconnect=tab_data.get("remote_reconnect", True),
                            remote_forward_port=tab_data.get("remote_forward_port"),
                            env=tab_data.get("env", {}),
                            workspace_id=tab_data.get("workspace_id"),
                            workspace_name=tab_data.get("workspace_name"),
                            workspace_role=workspace_role,
                            managed_session_id=tab_data.get("managed_session_id"),
                            agent_session_id=tab_data.get("agent_session_id"),
                            agent_session_id_verified=tab_data.get(
                                "agent_session_id_verified", False
                            ),
                            from_persisted_state=True,
                            resume_quarantined=tab_data.get("resume_quarantined", False),
                            shell_explicitly_provided=tab_data.get("shell_explicitly_provided"),
                            cursor_transport=self._runtime._promote_cursor_chat_transport(
                                session_kind,
                                agent_type,
                                target,
                                tab_data.get("cursor_transport", "terminal"),
                            ),
                            cursor_data_dir=tab_data.get("cursor_data_dir"),
                            cursor_cli_version=tab_data.get("cursor_cli_version"),
                            cursor_transcript_path=tab_data.get("cursor_transcript_path"),
                            cursor_transcript_schema=tab_data.get("cursor_transcript_schema"),
                            forked_from_tab_id=tab_data.get("forked_from_tab_id"),
                            forked_from_ordinal=tab_data.get("forked_from_ordinal"),
                            parent_tab_id=tab_data.get("parent_tab_id"),
                            subagent_id=tab_data.get("subagent_id"),
                            archived=tab_data.get("archived", False),
                            archived_at=self._runtime._parse_optional_iso(
                                tab_data.get("archived_at")
                            ),
                            last_viewed_at=self._runtime._parse_optional_iso(
                                tab_data.get("last_viewed_at")
                            ),
                        )
                        loaded[process.tab_id] = process
                        if process.port > max_port:
                            max_port = process.port
                    self.processes = loaded
                    self._pending_stream_cleanup = pending
                    # A prior process may have replaced this snapshot without
                    # syncing its directory. Reading it is not durability proof.
                    self._state_confirmed = not pending
                    self._next_port = max_port + 1
                logger.info(f"Loaded {len(self.processes)} tabs from {self._runtime.STATE_FILE}")
                for tab_id, process in self.processes.items():
                    logger.info(
                        f"  - Tab: {process.name} (tmux: {process.tmux_session}, agent: {process.agent_type.value})"
                    )
            except Exception as e:
                self._state_error = str(e)
                logger.error(f"Failed to load state: {e}")
        else:
            logger.info(f"No state file at {self._runtime.STATE_FILE}, starting fresh")

    def _require_valid_state(self) -> None:
        if self._state_error is not None:
            raise TabStateError("Tab state is invalid; refusing to overwrite cleanup evidence")

    def _persist_state(self) -> None:
        """All writers preserve the same live owners and cleanup obligations."""
        self._require_valid_state()
        payload = {
            "version": _TABS_STATE_VERSION,
            "tabs": [process.to_dict() for process in self.processes.values()],
            "pending_stream_cleanup": [
                asdict(record) for record in self._pending_stream_cleanup.values()
            ],
        }
        self._runtime._parse_tabs_snapshot(payload)
        try:
            self._runtime._write_tabs_snapshot(payload)
        except OSError:
            self._state_confirmed = False
            raise
        self._state_confirmed = True

    def _save_state(self) -> None:
        try:
            self._persist_state()
            logger.debug(f"Saved {len(self.processes)} tabs to {self._runtime.STATE_FILE}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_order(self) -> None:
        """Load tab order from file."""
        if self._runtime.ORDER_FILE.exists():
            try:
                with open(self._runtime.ORDER_FILE, "r") as f:
                    self._tab_order = json.load(f)
                logger.info(f"Loaded tab order: {self._tab_order}")
            except Exception as e:
                logger.error(f"Failed to load tab order: {e}")
                self._tab_order = []
        else:
            logger.info(
                f"Order file {self._runtime.ORDER_FILE} does not exist, starting with empty order"
            )

    def _save_order(self) -> None:
        """Save tab order to file."""
        try:
            self._runtime.ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self._runtime.ORDER_FILE, "w") as f:
                json.dump(self._tab_order, f)
            logger.info(f"Saved tab order to {self._runtime.ORDER_FILE}: {self._tab_order}")
        except Exception as e:
            logger.error(f"Failed to save tab order: {e}")

    def set_tab_order(self, tab_ids: List[str]) -> None:
        """Set the order of tabs."""
        logger.info(f"set_tab_order called with: {tab_ids}")
        logger.info(f"Current processes keys: {list(self.processes.keys())}")
        valid_ids = list(dict.fromkeys(tid for tid in tab_ids if tid in self.processes))
        submitted_ids = set(valid_ids)
        submitted_iter = iter(valid_ids)
        merged_ids: List[str] = []
        current_ids = set()

        # Preserve the slots of tabs omitted by a stale payload while reordering
        # the submitted tabs around them.
        for tid in self._tab_order:
            if tid not in self.processes or tid in current_ids:
                continue
            current_ids.add(tid)
            if tid in submitted_ids:
                merged_ids.append(next(submitted_iter))
            else:
                merged_ids.append(tid)

        merged_ids.extend(submitted_iter)
        ordered_ids = set(merged_ids)
        # Add processes unknown to both the persisted and submitted orders.
        for tid in self.processes:
            if tid not in ordered_ids:
                merged_ids.append(tid)
                ordered_ids.add(tid)
        self._tab_order = merged_ids
        logger.info(f"Final tab order set to: {self._tab_order}")
        self._save_order()

    def _ensure_tab_in_order(self, tab_id: str) -> None:
        """Ensure a newly created tab appears before existing tabs."""
        if tab_id not in self._tab_order:
            self._tab_order.insert(0, tab_id)
            self._save_order()

    def _sync_order_with_processes(self, *, persist: bool = True) -> None:
        """Sync order list with current processes - add any missing tabs."""
        # Add any tabs that are in processes but not in order
        order_changed = False
        for tab_id in self.processes:
            if tab_id not in self._tab_order:
                self._tab_order.append(tab_id)
                order_changed = True
        # Remove any tab IDs that are in order but not in processes
        original_len = len(self._tab_order)
        self._tab_order = [tid for tid in self._tab_order if tid in self.processes]
        if len(self._tab_order) != original_len:
            order_changed = True
        # If order is still empty, initialize with all process IDs
        if not self._tab_order and self.processes:
            self._tab_order = list(self.processes.keys())
            order_changed = True
        if order_changed:
            logger.info(f"Synced tab order: {self._tab_order}")
            if persist:
                self._save_order()
