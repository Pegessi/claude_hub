"""Process state responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, Optional

from ....models import (
    AgentType,
    ChatMode,
    ExecutionTarget,
    SessionKind,
    TerminalTab,
    WorkspaceSessionRole,
)
from ..records import ScanEntry
from .host import ProcessHost

logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ProcessStateMixin(ProcessHost):
    def __init__(
        self,
        tab_id: str,
        port: int,
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        created_at: Optional[datetime] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: Optional[WorkspaceSessionRole] = None,
        managed_session_id: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        agent_session_id: Optional[str] = None,
        agent_session_id_verified: bool = False,
        from_persisted_state: bool = False,
        resume_quarantined: bool = False,
        shell_explicitly_provided: Optional[bool] = None,
        cursor_transport: str = "terminal",
        cursor_data_dir: Optional[str] = None,
        cursor_cli_version: Optional[str] = None,
        cursor_transcript_path: Optional[str] = None,
        cursor_transcript_schema: Optional[str] = None,
        session_kind: SessionKind = SessionKind.TERMINAL,
        chat_mode: ChatMode = ChatMode.DEFAULT,
        forked_from_tab_id: Optional[str] = None,
        forked_from_ordinal: Optional[int] = None,
        archived: bool = False,
        archived_at: Optional[datetime] = None,
        last_viewed_at: Optional[datetime] = None,
    ):
        self.tab_id = tab_id
        self.port = port
        self.name = name
        self.cwd = cwd
        self.solo_mode = solo_mode
        self.agent_type = agent_type
        self.session_kind = session_kind
        self.chat_mode = chat_mode
        self.forked_from_tab_id = forked_from_tab_id
        self.forked_from_ordinal = forked_from_ordinal
        self.target = target
        self.remote_profile_id = remote_profile_id
        self.remote_cwd = remote_cwd
        self.remote_reconnect = remote_reconnect
        self.remote_forward_port = remote_forward_port
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.workspace_role = workspace_role
        self.managed_session_id = managed_session_id
        self.cursor_transport = (
            cursor_transport
            if cursor_transport in {"acp", "terminal_transcript", "native"}
            else "terminal"
        )
        self.cursor_data_dir = cursor_data_dir
        self.cursor_cli_version = cursor_cli_version
        self.cursor_transcript_path = cursor_transcript_path
        self.cursor_transcript_schema = cursor_transcript_schema
        launch_env = env if env else self._default_env_for_agent(agent_type)
        self.env = self._clean_env(launch_env)
        if self.cursor_transport == "terminal_transcript" and self.cursor_data_dir:
            # Pin the exact source directory into the child environment. This
            # keeps the process that owns the raw pane and the provenance that
            # gates Structured on the same Cursor data root, including custom
            # HOME/CURSOR_DATA_DIR launch environments.
            self.env["CURSOR_DATA_DIR"] = self.cursor_data_dir
        self._prepare_agent_env()
        if agent_type != AgentType.CLAUDE:
            self._setup_tunnel_env()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.created_at = created_at or self._runtime.datetime.now()
        self.is_active = False
        # Soft-delete state. Archived tabs keep their JSONL history but have
        # no live runtime; ``archived_at`` is set once at archive time and
        # drives the archived-browser sort order.
        self.archived: bool = bool(archived)
        self.archived_at: Optional[datetime] = archived_at
        # When the user last opened this tab. Drives the unread flag: a turn
        # that completed after this is "unread".
        self.last_viewed_at: Optional[datetime] = last_viewed_at
        self.tmux_session = self._runtime._tmux_session_name(tab_id)
        # Stable per-tab agent conversation id. Pinned at first launch via the
        # agent CLI's --session-id flag (for agents that support it) or
        # discovered from on-disk rollout shortly after launch (codex), so that
        # after a machine reboot (tmux sessions gone) we can resume EXACTLY this
        # tab's conversation instead of a fresh one. Many tabs share one cwd, so
        # a cwd-scoped --continue / --last would collide across tabs; an
        # explicit id keeps each tab distinct.
        #
        # - claude: supports --session-id at launch → pinned immediately.
        # - codex: no --session-id flag at launch; we generate an id at
        #   construction time and discover the REAL codex-assigned uuid from the
        #   rollout file shortly after the tab starts (see
        #   _discover_codex_session_id / _backfill_codex_session_ids), then
        #   overwrite self.agent_session_id with it. The generated value is
        #   only used temporarily and will be replaced before the next persist.
        #   For tabs that never successfully start (e.g. codex auth missing),
        #   the generated id stays — on recovery, `codex resume <fake-uuid>`
        #   fails and starts fresh, re-pinning via the same discovery path.
        # - cursor: constructive pin — `agent --resume <uuid>` creates a fresh
        #   chat store if none exists, so we ALWAYS pin a uuid4 at construction
        #   (defensive: start_all_tabs phase 0 backfills any legacy-None values).
        self.agent_session_id: Optional[str]
        if agent_session_id:
            self.agent_session_id = agent_session_id
        elif agent_type in (AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR):
            self.agent_session_id = str(uuid.uuid4())
        else:
            self.agent_session_id = None
        # Whether ``agent_session_id`` has been verified by the provider (the
        # provider emitted it in a system/init record). Only a verified id may
        # be passed to ``--resume``; an unverified (constructive) id must use
        # ``--session-id``. Persisted alongside ``agent_session_id`` so a cold
        # restart does not infer verification merely from the presence of a
        # UUID.
        self.agent_session_id_verified: bool = agent_session_id_verified
        # True only when an explicit session id was supplied at construction
        # time (e.g. the user selected a specific Codex session to resume in
        # the create-tab UI). A generated uuid4 placeholder (above) is used
        # solely for conversation pinning and must NOT be treated as a
        # "resume this session" signal — see _should_recover.
        self._has_explicit_session_id: bool = bool(agent_session_id)
        if self.cursor_transport == "terminal_transcript" and not (
            self.agent_type == AgentType.CURSOR
            and self.target == ExecutionTarget.LOCAL
            and self._runtime.cursor_terminal_transcript_provenance_valid(
                cwd=self.cwd,
                session_id=self.agent_session_id,
                cli_version=self.cursor_cli_version,
                transcript_path=self.cursor_transcript_path,
                transcript_schema=self.cursor_transcript_schema,
                data_dir=self.cursor_data_dir,
                env=self.env,
            )
        ):
            logger.warning(
                "Cursor terminal transcript provenance mismatch for tab %s; "
                "failing closed to terminal transport",
                self.tab_id,
            )
            self.cursor_transport = "terminal"
            self.cursor_data_dir = None
            self.cursor_cli_version = None
            self.cursor_transcript_path = None
            self.cursor_transcript_schema = None
        # True when this process was reconstructed from persisted tabs.json on
        # startup (vs. freshly created by a user/API call). Combined with an
        # absent tmux session, this is the signal that we are recovering after a
        # reboot and should resume the prior conversation.
        self.from_persisted_state = from_persisted_state
        # Codex-only: True when a prior cold-start attribution failed and we
        # quarantined this tab to avoid cross-wiring. On the next launch we
        # MUST start fresh (no `codex resume`) to prevent replaying a
        # potentially mis-attributed session. Cleared by a successful FRESH_PIN
        # or the pre-sibling salvage check in Phase 1S; preserved across backend
        # restarts via tabs.json. Defaults to False for back-compat
        # with persisted states created before this field existed.
        self.resume_quarantined: bool = bool(resume_quarantined)
        # --- Runtime-only fields (NOT persisted in tabs.json) ---
        # Wall-clock and monotonic timestamps stamped immediately BEFORE
        # tmux new-session -d spawns the agent shell in ensure_tmux_session.
        # These anchor the new-pin timestamp window used for Phase 1C codex
        # attribution. Both must be stamped there (not in start()), because
        # ttyd is lazy: the agent actually runs inside tmux and the
        # 1-second await asyncio.sleep(1) in start() happens AFTER the agent
        # has already been producing output for ~1.1 s.
        self._launch_wall: Optional[float] = None
        self._launch_mono: Optional[float] = None
        # True iff this cold launch produced a new rollout file (vs appending
        # to an existing one on a successful resume). Set by Phase 1C/salvage.
        self._is_new_pin: Optional[bool] = None
        # Full dict[sidad, ScanEntry] snapshot taken immediately before
        # ensure_tmux_session. Retained so Phase-R reconciliation can verify
        # append-resume entries grew (size + mtime) relative to this
        # pre-launch baseline — a frozenset of sids cannot detect growth.
        self._pre_scan: Optional[Dict[str, ScanEntry]] = None
        # Set by Phase 1C while signal/exception failure is being salvaged before
        # a sibling launch. None means "launched cleanly".
        self._pending_quarantine_reason: Optional[str] = None
        # Track whether the caller explicitly supplied a shell. For agent tabs
        # (claude/codex/cursor) the default launch command is the agent CLI;
        # an explicit shell overrides that so the tab runs a plain shell
        # instead (used by tests that need deterministic scrollback without a
        # real agent login).
        #
        # NOTE: self.shell is always non-None (it falls back to the agent
        # command or the user's shell), so we cannot derive this flag from
        # self.shell after construction. It must be passed in explicitly when
        # reconstructing from persisted state.
        if shell_explicitly_provided is not None:
            self._shell_explicitly_provided = shell_explicitly_provided
        else:
            # Legacy state (pre-shell_explicitly_provided): the flag was not
            # persisted. self.shell is always non-None (it falls back to the
            # agent command or the user's shell), so bool(shell) would
            # incorrectly mark every agent tab as explicit-shell and bypass
            # the agent CLI/resume path. Instead, derive the flag by comparing
            # the persisted shell to the default for this agent type:
            #   - agent tabs: shell == get_agent_command(agent_type) means the
            #     default agent CLI was used (not an explicit override).
            #   - terminal tabs: the shell is always run directly, so the flag
            #     has no behavioral effect; default to False.
            if agent_type == AgentType.TERMINAL:
                self._shell_explicitly_provided = False
            else:
                default_cmd = self._runtime.get_agent_command(agent_type)
                self._shell_explicitly_provided = bool(shell) and shell != default_cmd
        if session_kind == SessionKind.CHAT:
            # Chat tabs always host an inert lifecycle shell. Ignore even an
            # explicitly supplied provider-shaped ``shell`` value: the native
            # ProviderSession is the only process allowed to own the provider.
            self.shell = os.environ.get("SHELL", "/bin/bash")
        elif shell:
            self.shell = shell
        elif agent_type == AgentType.TERMINAL:
            self.shell = os.environ.get("SHELL", "/bin/bash")
        else:
            self.shell = self._runtime.get_agent_command(agent_type)

    @property
    def is_managed_chat(self) -> bool:
        """Whether this tab is a headless Workspace-managed Chat record."""

        return self.workspace_role is not None and self.session_kind == SessionKind.CHAT

    def to_dict(self) -> dict:
        return {
            "id": self.tab_id,
            "name": self.name,
            "shell": self.shell,
            "cwd": self.cwd,
            "solo_mode": self.solo_mode,
            "agent_type": (
                self.agent_type.value if isinstance(self.agent_type, AgentType) else self.agent_type
            ),
            "session_kind": (
                self.session_kind.value
                if isinstance(self.session_kind, SessionKind)
                else self.session_kind
            ),
            "chat_mode": (
                self.chat_mode.value if isinstance(self.chat_mode, ChatMode) else self.chat_mode
            ),
            "target": (
                self.target.value if isinstance(self.target, ExecutionTarget) else self.target
            ),
            "remote_profile_id": self.remote_profile_id,
            "remote_cwd": self.remote_cwd,
            "remote_reconnect": self.remote_reconnect,
            "env": self.env,
            "remote_forward_port": self.remote_forward_port,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "workspace_role": (
                self.workspace_role.value
                if isinstance(self.workspace_role, WorkspaceSessionRole)
                else self.workspace_role
            ),
            "managed_session_id": self.managed_session_id,
            "port": self.port,
            "created_at": self.created_at.isoformat(),
            "agent_session_id": self.agent_session_id,
            "agent_session_id_verified": self.agent_session_id_verified,
            "resume_quarantined": bool(self.resume_quarantined),
            "shell_explicitly_provided": self._shell_explicitly_provided,
            "cursor_transport": self.cursor_transport,
            "cursor_data_dir": self.cursor_data_dir,
            "cursor_cli_version": self.cursor_cli_version,
            "cursor_transcript_path": self.cursor_transcript_path,
            "cursor_transcript_schema": self.cursor_transcript_schema,
            "forked_from_tab_id": self.forked_from_tab_id,
            "forked_from_ordinal": self.forked_from_ordinal,
            "archived": bool(self.archived),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "last_viewed_at": self.last_viewed_at.isoformat() if self.last_viewed_at else None,
        }

    def to_schema(self) -> TerminalTab:
        return TerminalTab(
            id=self.tab_id,
            name=self.name,
            shell=self.shell,
            cwd=self.cwd,
            solo_mode=self.solo_mode,
            agent_type=self.agent_type,
            session_kind=self.session_kind,
            chat_mode=self.chat_mode,
            target=self.target,
            remote_profile_id=self.remote_profile_id,
            remote_cwd=self.remote_cwd,
            remote_reconnect=self.remote_reconnect,
            env=self.env,
            port=self.port,
            created_at=self.created_at,
            is_active=self.is_active,
            workspace_id=self.workspace_id,
            workspace_name=self.workspace_name,
            workspace_role=self.workspace_role,
            managed_session_id=self.managed_session_id,
            agent_session_id=self.agent_session_id,
            agent_session_id_verified=self.agent_session_id_verified,
            cursor_transport=self.cursor_transport,
            cursor_data_dir=self.cursor_data_dir,
            cursor_cli_version=self.cursor_cli_version,
            cursor_transcript_path=self.cursor_transcript_path,
            cursor_transcript_schema=self.cursor_transcript_schema,
            forked_from_tab_id=self.forked_from_tab_id,
            forked_from_ordinal=self.forked_from_ordinal,
            archived=self.archived,
            archived_at=self.archived_at,
            last_viewed_at=self.last_viewed_at,
            is_unread=None,
        )
