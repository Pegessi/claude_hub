"""CodexConfiguredSession implementation; compatibility dependencies are explicit."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ....models import (
    AgentType,
    ChatMode,
    ManagedSession,
    StreamCapabilities,
    StreamModelOption,
    StreamModeOption,
    StreamReasoningEffortOption,
)
from ..provider_network import ProviderNetworkError
from .contracts import ProviderGoal
from .process import ProcessSession


class CodexConfiguredSession(ProcessSession):
    """Codex native transport via the app-server JSON-RPC stdio protocol.

    ``start`` launches ``codex app-server --stdio`` and runs the
    ``initialize`` handshake (with ``clientInfo``) followed by the
    ``initialized`` notification, then creates or resumes a thread.
    ``send_message`` issues a ``turn/start`` request with ``input`` items.

    Responses (records with an ``id``) are dispatched to per-request
    Futures; notifications (no ``id``) are forwarded to the notification
    queue consumed by :meth:`read_line`. This avoids the spin-loop hazard
    of re-queueing notifications ahead of a pending response.
    """

    adapter_id = "codex-native"
    schema_version = 1
    supports_tool_timeline = True
    supports_approval_ui = True
    supports_images = True
    supports_native_system_instructions = True
    goal_usage_quality = "exact"

    @property
    def eof_is_fatal(self) -> bool:
        # Codex runs a persistent app-server; EOF means it died.
        return True

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
        self._jsonrpc_id = 0
        self._thread_id: Optional[str] = None
        self._provider_turn_id: Optional[str] = None
        # Stop retires one provider turn while the persistent app-server keeps
        # running. Filter its late notifications until turn/completed confirms
        # the interrupt, so they cannot be attributed to the next Hub turn.
        self._discard_turn_id: Optional[str] = None
        self._interrupted = asyncio.Event()
        # Per-request response futures keyed by JSON-RPC id.
        self._pending_requests: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        # Server→client question requests awaiting an answer, keyed by the
        # server's JSON-RPC id. The value is the request params (with
        # ``questions``); consumed by :meth:`answer_pending_question`.
        self._pending_questions: Dict[Any, Dict[str, Any]] = {}
        # Notifications (no ``id``) are placed here for the tailer to consume.
        self._notification_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        # Staged image temp files for the next turn/start.
        self._staged_images: List[Path] = []
        # Image temp files owned by the in-flight turn; cleaned up on
        # turn/completed or stop.
        self._inflight_images: List[Path] = []
        # ``SessionTailer._run_native_push`` and the first composer send can
        # both discover an unstarted transport at the same time.  Codex owns
        # one persistent app-server, so initialization must be single-flight:
        # two readers attached to the same overwritten ``self._process``
        # would otherwise concurrently read one stdout stream and tear down
        # structured delivery with asyncio's "another coroutine is already
        # waiting" error.
        self._start_lock = asyncio.Lock()
        self._thread_model: Optional[str] = None
        self._mode_presets: Dict[str, Dict[str, Any]] = {}
        self._mode_discovery_attempted = False
        self._provider_goal_api_available: Optional[bool] = None
        self._model_discovery_attempted = False
        self._provider_model_options: Optional[List[StreamModelOption]] = None
        self._network_config_fingerprint: Optional[str] = None
        self._defer_initial_thread_adoption = False
        self._pending_rotation: Optional[Tuple[str, Dict[str, Any]]] = None

    def _build_command(self) -> List[str]:
        return ["codex", "app-server", "--stdio"]

    async def _prepare_for_send(self) -> None:
        if self._turn_in_flight:
            return
        if not self._started or self._process is None:
            return
        current = self._provider_network_fingerprint()
        if self._network_config_fingerprint is None:
            self._network_config_fingerprint = current
            return
        if current == self._network_config_fingerprint:
            return
        # Validate the replacement route before stopping a healthy app-server.
        # This runs before _begin_turn, so no prompt has been accepted and no
        # retry or duplicate submission is possible.
        await self._build_provider_env()
        await self.restart_for_recovery()

    async def stop(self) -> None:
        """Terminate the app-server and clean up any staged image temp files."""
        async with self._start_lock:
            staged = self._staged_images
            inflight = self._inflight_images
            self._staged_images = []
            self._inflight_images = []
            self._pending_rotation = None
            # The app-server is being terminated; any unanswered questions die
            # with it.
            self._pending_questions = {}
            await super().stop()
            self._cleanup_images(staged)
            self._cleanup_images(inflight)

    async def start(self) -> None:
        async with self._start_lock:
            # A second caller that waited for the in-progress handshake can
            # reuse the one healthy app-server.  Re-enter initialization only
            # when the reader already terminated.
            if (
                self._started
                and self._process is not None
                and self._reader_task is not None
                and not self._reader_task.done()
            ):
                return

            # Clear a half-started/dead process before retrying.  This is also
            # important after an initialize or thread/resume failure: leaving
            # ``_started`` true would make every later send target a broken
            # pipe instead of getting one clean retry.
            if self._process is not None or self._reader_task is not None:
                await self._terminate_process()
            self._started = False

            cmd = self._build_command()
            for attempt in range(2):
                # Every retry owns a fresh queue; EOF from a superseded process
                # must never terminate the replacement generation.
                self._notification_queue = asyncio.Queue()
                network_config_fingerprint = self._provider_network_fingerprint()
                provider_env = await self._build_provider_env()
                if self._provider_network_fingerprint() != network_config_fingerprint:
                    if attempt == 0:
                        continue
                    self._last_error = (
                        "provider network configuration changed repeatedly during preflight"
                    )
                    raise ProviderNetworkError(self._last_error)
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=self._cwd or None,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=provider_env,
                    )
                except FileNotFoundError:
                    self._last_error = f"provider binary not found: {cmd[0]}"
                    raise
                except OSError as exc:
                    self._last_error = f"failed to launch provider: {exc}"
                    raise
                self._started = True
                self._reader_task = asyncio.create_task(self._drain_stdout())
                self._stderr_task = asyncio.create_task(self._drain_stderr())
                try:
                    await self._initialize_protocol()
                except Exception as exc:
                    self._last_error = f"codex app-server initialization failed: {exc}"
                    self._started = False
                    await self._terminate_process()
                    raise RuntimeError(self._last_error) from exc
                if self._provider_network_fingerprint() != network_config_fingerprint:
                    self._started = False
                    await self._terminate_process()
                    if attempt == 0:
                        continue
                    self._last_error = (
                        "provider network configuration changed repeatedly during startup"
                    )
                    raise ProviderNetworkError(self._last_error)
                try:
                    await self._initialize_thread()
                except Exception as exc:
                    self._last_error = f"codex app-server initialization failed: {exc}"
                    self._started = False
                    await self._terminate_process()
                    raise RuntimeError(self._last_error) from exc
                self._last_error = None
                self._network_config_fingerprint = network_config_fingerprint
                return

    async def _initialize(self) -> None:
        """Run the JSON-RPC initialization handshake and create or resume a thread."""
        await self._initialize_protocol()
        await self._initialize_thread()

    async def _initialize_protocol(self) -> None:
        """Complete the local app-server protocol handshake."""
        init_params = {
            "clientInfo": {"name": "claude-hub", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        }
        await self._send_request("initialize", init_params)
        await self._send_notification("initialized", {})

    async def _initialize_thread(self) -> None:
        """Create or resume the provider thread after network config is stable."""
        # Resume an existing thread when the session pins one; otherwise start
        # a fresh thread.
        if self._conversation_id:
            try:
                resume_resp = await self._send_request(
                    "thread/resume", {"threadId": self._conversation_id, **self._thread_config()}
                )
                if isinstance(resume_resp, dict):
                    self._capture_thread_model(resume_resp)
                    thread = resume_resp.get("thread")
                    if isinstance(thread, dict) and thread.get("id"):
                        self._thread_id = thread["id"]
                        return
            except RuntimeError:
                if self._required_conversation_id is not None:
                    raise
                # Resume failed (e.g. thread not found); fall through to start.
                self._runtime.logger().warning("codex thread/resume failed; starting a new thread")
        if self._required_conversation_id is not None:
            raise RuntimeError("the original provider thread could not be resumed")
        thread_resp = await self._send_request("thread/start", self._thread_config())
        if isinstance(thread_resp, dict):
            thread = thread_resp.get("thread")
            conversation_id = thread.get("id") if isinstance(thread, dict) else None
            if isinstance(conversation_id, str) and conversation_id:
                if self._defer_initial_thread_adoption:
                    self._pending_rotation = (conversation_id, thread_resp)
                    return
                self._capture_thread_model(thread_resp)
                self._thread_id = conversation_id
                self._persist_conversation_id(conversation_id)

    async def rotate_conversation(self) -> str:
        if not self._started or self._process is None:
            defer_initial = not self._conversation_id
            self._defer_initial_thread_adoption = defer_initial
            try:
                await self.start()
            finally:
                self._defer_initial_thread_adoption = False
            if defer_initial:
                if self._pending_rotation is None:
                    raise RuntimeError(
                        f"{self.adapter_id} initial thread/start returned no thread id"
                    )
                return self._pending_rotation[0]
        response = await self._send_request("thread/start", self._thread_config())
        if not isinstance(response, dict):
            raise RuntimeError(f"{self.adapter_id} thread/start returned no result")
        thread = response.get("thread")
        conversation_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(conversation_id, str) or not conversation_id:
            raise RuntimeError(f"{self.adapter_id} thread/start returned no thread id")
        self._pending_rotation = (conversation_id, response)
        return conversation_id

    def adopt_rotated_conversation(self, conversation_id: str) -> None:
        pending = self._pending_rotation
        if pending is None or pending[0] != conversation_id:
            raise RuntimeError(f"{self.adapter_id} has no matching prepared rotation")
        self._adopt_rotated_conversation_id(conversation_id)
        self._capture_thread_model(pending[1])
        self._thread_id = conversation_id
        self._provider_turn_id = None
        self._pending_rotation = None

    async def discard_rotated_conversation(self, conversation_id: str) -> None:
        if self._pending_rotation is not None and self._pending_rotation[0] == conversation_id:
            self._pending_rotation = None

    async def goal_get(self) -> Optional[ProviderGoal]:
        """Read the provider goal when the optional RPC is implemented."""
        result = await self._goal_request("thread/goal/get", {"threadId": self._thread_id})
        if result is None:
            return None
        goal = result.get("goal") if isinstance(result, dict) else None
        return dict(goal) if isinstance(goal, dict) else None  # type: ignore[return-value]

    async def goal_set(self, goal: ProviderGoal) -> bool:
        """Set a provider goal, returning False when the RPC is unavailable."""
        params: Dict[str, Any] = {"threadId": self._thread_id}
        for key in ("objective", "status", "tokenBudget"):
            if key in goal:
                params[key] = goal[key]
        result = await self._goal_request("thread/goal/set", params)
        return result is not None

    async def goal_clear(self) -> bool:
        """Clear a provider goal, returning False when the RPC is unavailable."""
        result = await self._goal_request("thread/goal/clear", {"threadId": self._thread_id})
        return result is not None

    async def _goal_request(self, method: str, params: Dict[str, Any]) -> Any:
        if self._provider_goal_api_available is False:
            return None
        try:
            result = await self._send_request(method, params)
        except RuntimeError as exc:
            message = str(exc).lower()
            if "-32601" in message or "method not found" in message:
                self._provider_goal_api_available = False
                return None
            raise
        self._provider_goal_api_available = True
        return result

    def capabilities(self) -> StreamCapabilities:
        caps = super().capabilities()
        # Hub remains the only scheduler until provider Goal state is fully
        # reconciled into GoalRun. RPC availability alone must never switch
        # ownership and accidentally enable two continuation schedulers.
        return caps.model_copy(update={"goal_execution_owner": "hub_managed"})

    def _sandbox_plan_solo(self) -> Tuple[bool, bool]:
        """Return ``(plan, solo)`` honouring the existing Chat mode semantics.

        Plan forces read-only regardless of the solo toggle (mirrors Claude's
        ``--permission-mode plan`` and TraeX); solo therefore only applies in
        the default mode.
        """
        plan = self._current_mode == ChatMode.PLAN.value
        solo = bool(self.session.solo_mode) and not plan
        return plan, solo

    @staticmethod
    def _sandbox_type(plan: bool, solo: bool) -> str:
        if plan:
            return "readOnly"
        if solo:
            return "dangerFullAccess"
        return "workspaceWrite"

    def _network_access_enabled(self, plan: bool, solo: bool) -> bool:
        """Whether to attach ``networkAccess: true`` to the sandbox policy.

        - plan/readOnly: never — the model issues no shell commands at all.
        - solo/dangerFullAccess: not needed — that tier already permits full
          egress (verified against codex 0.156.1 seatbelt).
        - default/workspaceWrite: only on an explicit
          ``HUB_CHAT_ALLOW_NETWORK`` opt-in, since the engine's only lift is
          full outbound (no loopback-only tier).
        """
        if plan or solo:
            return False
        return self._runtime.env_flag(self.session.env, self._runtime.chat_network_env())

    def _sandbox_policy(self, plan: bool, solo: bool) -> Dict[str, Any]:
        policy: Dict[str, Any] = {"type": self._sandbox_type(plan, solo)}
        if self._network_access_enabled(plan, solo):
            policy["networkAccess"] = True
        return policy

    def _permission_config(self) -> Dict[str, Any]:
        plan, solo = self._sandbox_plan_solo()
        return {
            "approvalPolicy": "never" if solo else "on-request",
            "sandboxPolicy": self._sandbox_policy(plan, solo),
        }

    def _thread_config(self) -> Dict[str, Any]:
        return {"cwd": self._cwd, **self._permission_config()}

    def _turn_config(self) -> Dict[str, Any]:
        return self._permission_config()

    def _capture_thread_model(self, response: Dict[str, Any]) -> None:
        model = response.get("model")
        if isinstance(model, str) and model:
            self._thread_model = model

    async def _discover_modes(self) -> None:
        """Load app-server advertised presets; failure leaves default only."""

        self._mode_presets = {}
        self._mode_discovery_attempted = True
        try:
            response = await self._send_request("collaborationMode/list", {})
        except RuntimeError:
            self._runtime.logger().warning(
                "codex collaborationMode/list unavailable; plan mode disabled"
            )
            return
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            provider_mode = item.get("mode")
            name = str(item.get("name", "")).strip().lower()
            if provider_mode in {ChatMode.DEFAULT.value, ChatMode.PLAN.value}:
                ui_mode = provider_mode
            elif name in {ChatMode.DEFAULT.value, ChatMode.PLAN.value}:
                ui_mode = name
            else:
                continue
            self._mode_presets[ui_mode] = dict(item)

    async def prepare_capabilities(self) -> None:
        # Populate ``_available_models`` (base implementation); without this
        # super call the codex override would skip model discovery.
        await super().prepare_capabilities()
        await self.start()
        if not self._mode_discovery_attempted:
            await self._discover_modes()
        if self._provider_model_options is not None:
            self._available_models = self._provider_model_options
        else:
            await self._discover_models()
        self._metadata_cacheable = self._provider_model_options is not None

    async def _discover_models(self) -> bool:
        """Load the app-server's model-specific reasoning capabilities."""

        pages: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor is not None else {}
            try:
                response = await self._send_request("model/list", params)
            except RuntimeError:
                self._runtime.logger().warning(
                    "%s model/list unavailable; using static fallback", self.adapter_id
                )
                return False
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                return False
            pages.append(response)
            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        options: List[StreamModelOption] = []
        seen: set[str] = set()
        data = [item for page in pages for item in page["data"]]
        for item in data:
            if not isinstance(item, dict) or item.get("hidden") is True:
                continue
            model = item.get("model")
            if not isinstance(model, str) or not model or model in seen:
                continue
            seen.add(model)
            efforts: List[StreamReasoningEffortOption] = []
            raw_efforts = item.get("supportedReasoningEfforts")
            if isinstance(raw_efforts, list):
                for raw_effort in raw_efforts:
                    if not isinstance(raw_effort, dict):
                        continue
                    effort = raw_effort.get("reasoningEffort")
                    if (
                        not isinstance(effort, str)
                        or effort not in self._runtime.reasoning_efforts()
                    ):
                        continue
                    description = raw_effort.get("description")
                    efforts.append(
                        StreamReasoningEffortOption(
                            id=effort,
                            label=self._runtime.reasoning_labels()[effort],
                            description=description if isinstance(description, str) else "",
                        )
                    )
            default_effort = item.get("defaultReasoningEffort")
            if (
                not isinstance(default_effort, str)
                or default_effort not in self._runtime.reasoning_efforts()
            ):
                default_effort = None
            label = item.get("displayName")
            description = item.get("description")
            options.append(
                StreamModelOption(
                    id=model,
                    label=label if isinstance(label, str) and label else model,
                    description=description if isinstance(description, str) else "",
                    provider_model_id=model,
                    default_reasoning_effort=default_effort,
                    supported_reasoning_efforts=efforts,
                )
            )
        if options:
            self._model_discovery_attempted = True
            self._provider_model_options = options
            self._available_models = options
            return True
        return False

    def available_modes(self) -> List[StreamModeOption]:
        options = [self._runtime.default_mode()]
        if ChatMode.PLAN.value in self._mode_presets and self._thread_model:
            options.append(self._runtime.plan_mode())
        return options

    def _selected_model_override(self) -> Optional[str]:
        """Return the user-pinned ``CODEX_MODEL``, if one was selected.

        The composer's model picker writes ``CODEX_MODEL`` into the session
        env. Unlike Claude/Cursor (one-shot per turn, so the env is read live
        on each command rebuild), the codex app-server is persistent — its
        model is fixed at thread start and the CLI ignores ``CODEX_MODEL``
        in the environment. The per-turn ``collaborationMode.settings.model``
        channel is what actually changes the model mid-conversation, so the
        selected model is injected there rather than into the spawn env.
        """
        model = self.session.env.get("CODEX_MODEL")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return None

    def _selected_reasoning_effort(self) -> Optional[str]:
        effort = self.session.env.get("CODEX_REASONING_EFFORT")
        if self.session.agent_type == AgentType.TRAEX:
            effort = effort or self.session.env.get("TRAEX_REASONING_EFFORT")
        if isinstance(effort, str) and effort.strip() in self._runtime.reasoning_efforts():
            selected = effort.strip()
            model_id = self.current_model()
            catalog = self._provider_model_options or []
            model = next((item for item in catalog if item.id == model_id), None)
            if model is not None and selected not in {
                item.id for item in model.supported_reasoning_efforts
            }:
                return None
            return selected
        return None

    def current_model(self) -> Optional[str]:
        return self._selected_model_override() or self._thread_model

    def current_reasoning_effort(self) -> Optional[str]:
        """Return the effort used when no explicit picker override exists."""

        preset = self._mode_presets.get(self._current_mode, {})
        preset_effort = preset.get("reasoning_effort")
        if isinstance(preset_effort, str) and preset_effort in self._runtime.reasoning_efforts():
            return preset_effort
        catalog = self._provider_model_options or self._available_models
        model_id = self.current_model()
        if model_id is None and len(catalog) == 1:
            model_id = catalog[0].id
        model = next((item for item in catalog if item.id == model_id), None)
        return model.default_reasoning_effort if model is not None else None

    def _collaboration_mode_payload(self) -> Optional[Dict[str, Any]]:
        model_override = self._selected_model_override()
        preset = self._mode_presets.get(self._current_mode)
        if preset is None:
            if self._current_mode != ChatMode.DEFAULT.value:
                raise RuntimeError(f"Codex Chat mode is unavailable: {self._current_mode}")
            # No advertised default preset (e.g. collaborationMode/list was
            # never called or failed). Normally no payload is needed — the
            # app-server uses its configured default. But when the user pinned
            # a model we must still send it, otherwise the selection is
            # silently dropped. Verified against codex app-server 0.151.0:
            # a default-mode collaborationMode with settings.model is accepted
            # and surfaces in thread/settings/updated.
            effort_override = self._selected_reasoning_effort()
            developer_instructions = self._dot_system_pack or None
            if (
                model_override is None
                and effort_override is None
                and developer_instructions is None
            ):
                return None
            model = model_override or self._thread_model
            if not isinstance(model, str) or not model:
                raise RuntimeError("Codex collaboration mode requires the active thread model")
            default_settings: Dict[str, Any] = {
                "model": model,
                "developer_instructions": developer_instructions,
            }
            if effort_override is not None:
                default_settings["reasoning_effort"] = effort_override
            return {
                "mode": ChatMode.DEFAULT.value,
                "settings": default_settings,
            }
        model = model_override or preset.get("model") or self._thread_model
        if not isinstance(model, str) or not model:
            raise RuntimeError("Codex collaboration mode requires the active thread model")
        settings: Dict[str, Any] = {
            "model": model,
            "developer_instructions": self._dot_system_pack or None,
        }
        effort = self._selected_reasoning_effort() or preset.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            settings["reasoning_effort"] = effort
        provider_mode = preset.get("mode")
        if not isinstance(provider_mode, str) or not provider_mode:
            raise RuntimeError("Codex collaboration mode preset has no provider mode")
        return {"mode": provider_mode, "settings": settings}

    @abstractmethod
    async def _send_request(self, method: str, params: Dict[str, Any]) -> Any: ...

    @abstractmethod
    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None: ...

    @abstractmethod
    async def _drain_stdout(self) -> None: ...

    @staticmethod
    @abstractmethod
    def _cleanup_images(paths: List[Path]) -> None: ...
