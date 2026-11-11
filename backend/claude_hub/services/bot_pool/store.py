"""Bot credential lifecycle, pairing, and active binding operations."""

from claude_hub.services.bot_pool.common import (
    _CLAIM_TTL_SECONDS,
    _CODE_ALPHABET,
    _CODE_LENGTH,
    _CODE_PREFIX,
    _CODE_TTL_SECONDS,
    _CONFIRM_ALPHABET,
    _CONFIRM_LENGTH,
    _CONFIRM_MAX_ATTEMPTS,
    _MAX_CLAIMS_PER_BOT,
    _MAX_ID_CHARS,
    _MAX_PENDING_PER_BOT,
    _RATE_MAX,
    _RESERVED_BOT_IDS,
    BINDING_CHAT_TYPE_GROUP,
    BINDING_CHAT_TYPE_P2P,
    BOT_MODE_SINGLE,
    BOT_MODES,
    ENV_BOT_ID,
    MAX_BOTS,
    Any,
    BotBinding,
    FeishuBindingCodeError,
    FeishuBotConfig,
    FeishuBotInvalidValue,
    FeishuBotModeSwitchBlocked,
    FeishuBotOccupied,
    FeishuBotPoolStateError,
    FeishuBotRateLimited,
    FeishuBotReadOnly,
    FeishuBotRevoked,
    FeishuChatOccupied,
    FeishuPairingMismatch,
    FeishuPairingNotFound,
    FeishuPairingNotOwned,
    FeishuPoolFull,
    Mapping,
    OwnerIdentity,
    PairingClaim,
    _as_float,
    _as_int,
    _as_optional_text,
    _as_text,
    _code_digest,
    _confirm_digest,
    _owner_from_dict,
    _owner_to_dict,
    _safe_timestamp,
    binding_chat_type,
    bot_mode,
    environment_config,
    environment_present,
    hmac,
    secrets,
)
from claude_hub.services.bot_pool.state import FeishuBotPoolStateStore


def _write_text(value: Any, name: str, *, limit: int = _MAX_ID_CHARS) -> str:
    try:
        return _as_text(value, name, limit=limit)
    except FeishuBotPoolStateError as exc:
        raise FeishuBotInvalidValue(str(exc)) from exc


def _write_optional_text(value: Any, name: str, *, limit: int = _MAX_ID_CHARS) -> str | None:
    try:
        return _as_optional_text(value, name, limit=limit)
    except FeishuBotPoolStateError as exc:
        raise FeishuBotInvalidValue(str(exc)) from exc


def _write_float(value: Any, name: str) -> float:
    try:
        return _as_float(value, name)
    except FeishuBotPoolStateError as exc:
        raise FeishuBotInvalidValue(str(exc)) from exc


def _write_owner(owner: Any, name: str) -> dict[str, Any]:
    if not isinstance(owner, OwnerIdentity):
        raise FeishuBotInvalidValue(f"Invalid Feishu Bot pool field {name!r}")
    try:
        validated = _owner_from_dict(_owner_to_dict(owner), name)
    except FeishuBotPoolStateError as exc:
        raise FeishuBotInvalidValue(str(exc)) from exc
    return _owner_to_dict(validated)


class FeishuBotPoolStore(FeishuBotPoolStateStore):

    def _max_bots(self) -> int:
        return MAX_BOTS

    # ------------------------------------------------------- bot lifecycle

    def create_bot(
        self,
        *,
        name: str,
        config: FeishuBotConfig,
        feishu_name: str | None = None,
        feishu_avatar_url: str | None = None,
        feishu_open_id: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> str:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            if (
                len([v for v in state["bots"].values() if not v.get("revoked_at")])
                >= self._max_bots()
            ):
                raise FeishuPoolFull("pool")
            self._require_app_id_free(state, config.app_id, exclude=None)
            bot_id = secrets.token_hex(8)
            while bot_id in state["bots"] or bot_id in _RESERVED_BOT_IDS:
                bot_id = secrets.token_hex(8)
            raw = self._new_entry(bot_id, name, config.app_id, now)
            raw["credentials"] = {"app_secret": config.app_secret}
            raw["feishu_name"] = feishu_name
            raw["feishu_avatar_url"] = feishu_avatar_url
            raw["feishu_open_id"] = feishu_open_id
            state["bots"][bot_id] = raw
            self._save(state)
            return bot_id

    def rotate_secrets(
        self,
        bot_id: str,
        *,
        app_secret: str,
        feishu_name: str | None = None,
        feishu_avatar_url: str | None = None,
        feishu_open_id: str | None = None,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Replace the App Secret under the same app_id.

        The active binding is preserved: it identifies a conversation under an
        app identity that has not changed. Bumping ``revision`` is enough to
        retire snapshots taken before the rotation.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if bot_id == ENV_BOT_ID:
                raise FeishuBotReadOnly(bot_id)
            self._check_revision(raw, expected_revision)
            raw["credentials"] = {"app_secret": app_secret}
            raw["feishu_name"] = feishu_name
            raw["feishu_avatar_url"] = feishu_avatar_url
            raw["feishu_open_id"] = feishu_open_id
            self._touch(raw, now)
            raw["transport_revision"] = raw["revision"]
            self._save(state)

    def configure_callbacks(
        self,
        bot_id: str,
        *,
        callback_enabled: bool,
        expected_revision: int,
        verification_token: str | None = None,
        encrypt_key: str | None = None,
        expected_tenant_key: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Provision protected card-callback settings; every update revokes old snapshots.

        A revision bump invalidates action bindings while retaining the active p2p
        pairing. Secret values are never returned by the public pool API.
        """
        if not isinstance(callback_enabled, bool):
            raise ValueError("callback_enabled must be a boolean")
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            callbacks = dict(raw.get("callbacks", {}))
            for key, value in (
                ("verification_token", verification_token),
                ("encrypt_key", encrypt_key),
                ("expected_tenant_key", expected_tenant_key),
            ):
                if value is not None:
                    callbacks[key] = _as_text(
                        value, key, limit=_MAX_ID_CHARS if key == "expected_tenant_key" else 4096
                    )
            if callback_enabled and not all(
                callbacks.get(key)
                for key in ("verification_token", "encrypt_key", "expected_tenant_key")
            ):
                raise ValueError("Enabled callbacks require protected credentials and tenant")
            callbacks["callback_enabled"] = callback_enabled
            raw["callbacks"] = callbacks
            self._touch(raw, now)
            raw["transport_revision"] = raw["revision"]
            self._save(state)

    def update_profile(
        self,
        bot_id: str,
        *,
        feishu_name: str,
        feishu_avatar_url: str | None,
        feishu_open_id: str | None = None,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Persist Feishu-managed presentation metadata without rotating transport."""

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if (
                raw.get("feishu_name") == feishu_name
                and raw.get("feishu_avatar_url") == feishu_avatar_url
                and raw.get("feishu_open_id") == feishu_open_id
            ):
                return
            raw["feishu_name"] = feishu_name
            raw["feishu_avatar_url"] = feishu_avatar_url
            raw["feishu_open_id"] = feishu_open_id
            raw["updated_at"] = now
            self._save(state)

    def update_bot(
        self,
        bot_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        enabled: bool | None = None,
        mode: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if mode is not None and mode not in BOT_MODES:
            raise ValueError(f"invalid bot mode: {mode!r}")
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            current_mode = bot_mode(raw)
            mode_changed = mode is not None and mode != current_mode
            if mode_changed:
                if self._active_raw_bindings(raw):
                    raise FeishuBotModeSwitchBlocked(bot_id)
                raw["mode"] = mode
                self._advance_generation(raw)
            elif mode is not None:
                raw["mode"] = mode
            if name is not None:
                raw["name"] = name
            enabled_changed = enabled is not None and enabled != raw.get("enabled")
            if enabled_changed:
                raw["enabled"] = enabled
                if not enabled:
                    # Disabling revokes outstanding pairing state for this Bot.
                    self._advance_generation(raw)
            self._touch(raw, now)
            if mode_changed or enabled_changed:
                raw["transport_revision"] = raw["revision"]
            self._save(state)

    def delete_bot(
        self,
        bot_id: str,
        *,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Replace the entry with a secret-free tombstone.

        The tombstone lets any event already accepted for a revoked Bot be
        dropped silently.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if bot_id == ENV_BOT_ID:
                raise FeishuBotReadOnly(bot_id)
            self._check_revision(raw, expected_revision)
            state["bots"][bot_id] = {
                "bot_id": bot_id,
                "revision": _as_int(raw.get("revision"), "revision") + 1,
                "generation": _as_int(raw.get("generation"), "generation") + 1,
                "revoked_at": now,
            }
            self._save(state)

    # ------------------------------------------------------------- pairing

    def issue_code(
        self,
        bot_id: str,
        *,
        owner: OwnerIdentity,
        tab_id: str,
        workspace_id: str | None,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[str, float, int]:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            self._require_bot_binding_slot(raw, bot_id)
            validated_owner = _write_owner(owner, "pending")
            validated_tab = _write_text(tab_id, "pending.tab_id", limit=_MAX_ID_CHARS)
            validated_workspace = _write_optional_text(workspace_id, "pending.workspace_id")
            self._require_tab_free(state, validated_tab)
            # Rate limiting is per Hub identity, not per Bot: otherwise picking a
            # different Bot would multiply the allowance.
            stamps = state["rate_limits"].setdefault(owner.open_id, [])
            if len(stamps) >= _RATE_MAX:
                self._save(state)
                raise FeishuBotRateLimited(owner.open_id)
            if len(raw["pending"]) >= _MAX_PENDING_PER_BOT:
                raise FeishuPoolFull("pending")
            stamps.append(now)
            code = _CODE_PREFIX + "".join(
                secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH)
            )
            expires_at = now + _CODE_TTL_SECONDS
            raw["pending"][_code_digest(bot_id, code)] = {
                **validated_owner,
                "tab_id": validated_tab,
                "workspace_id": validated_workspace,
                "issued_at": now,
                "expires_at": expires_at,
                "generation": raw["generation"],
            }
            self._touch(raw, now)
            self._save(state)
            return code, expires_at, _as_int(raw["revision"], "revision")

    def claim_code(
        self,
        bot_id: str,
        *,
        code: str,
        sender_open_id: str,
        chat_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[PairingClaim, str]:
        """Consume a code presented through the Bot's verified channel.

        This creates a claim, never a binding. Nothing is routed until the Hub
        owner types back the confirmation word that only this reply carries.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            validated_sender = _write_text(
                sender_open_id, "claims.sender_open_id", limit=_MAX_ID_CHARS
            )
            validated_chat = _write_text(chat_id, "claims.chat_id", limit=_MAX_ID_CHARS)
            digest = _code_digest(bot_id, code)
            value = raw["pending"].pop(digest, None)
            if not isinstance(value, dict):
                raise FeishuBindingCodeError("unknown")
            if _safe_timestamp(value.get("expires_at")) <= now:
                self._save(state)
                raise FeishuBindingCodeError("expired")
            if value.get("generation") != raw["generation"]:
                self._save(state)
                raise FeishuBindingCodeError("superseded")
            if len(raw["claims"]) >= _MAX_CLAIMS_PER_BOT:
                self._save(state)
                raise FeishuPoolFull("claims")
            pairing_id = secrets.token_hex(16)
            word = "".join(secrets.choice(_CONFIRM_ALPHABET) for _ in range(_CONFIRM_LENGTH))
            claim_record = {
                **_owner_to_dict(_owner_from_dict(value, "pending")),
                "tab_id": _as_text(value.get("tab_id"), "pending.tab_id", limit=_MAX_ID_CHARS),
                "workspace_id": _as_optional_text(
                    value.get("workspace_id"), "pending.workspace_id"
                ),
                "sender_open_id": validated_sender,
                "chat_id": validated_chat,
                "created_at": now,
                "expires_at": now + _CLAIM_TTL_SECONDS,
                "generation": raw["generation"],
                # The confirmation word protects online pairing only. File
                # confidentiality relies on the private directory and file mode.
                "confirm_digest": _confirm_digest(bot_id, pairing_id, word),
                "confirm_attempts": 0,
            }
            claim = self._claim_from_dict(bot_id, pairing_id, claim_record)
            raw["claims"][pairing_id] = claim_record
            self._touch(raw, now)
            self._save(state)
            return claim, word

    @classmethod
    def _require_sender_p2p_free(cls, raw: dict[str, object], sender_open_id: str) -> None:
        for binding in cls._active_raw_bindings(raw):
            if (
                binding_chat_type(binding) == BINDING_CHAT_TYPE_P2P
                and binding.get("sender_open_id") == sender_open_id
            ):
                raise FeishuChatOccupied(sender_open_id)

    def activate_claim(
        self,
        bot_id: str,
        *,
        pairing_id: str,
        confirm_word: str,
        actor: OwnerIdentity,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            value = raw["claims"].get(pairing_id)
            if not isinstance(value, dict):
                raise FeishuPairingNotFound(pairing_id)
            owner = _owner_from_dict(value, "claim")
            if owner.kind != actor.kind or not hmac.compare_digest(owner.open_id, actor.open_id):
                raise FeishuPairingNotOwned(pairing_id)
            expected = str(value.get("confirm_digest", ""))
            if not hmac.compare_digest(expected, _confirm_digest(bot_id, pairing_id, confirm_word)):
                attempts = _as_int(value.get("confirm_attempts", 0), "confirm_attempts") + 1
                if attempts >= _CONFIRM_MAX_ATTEMPTS:
                    del raw["claims"][pairing_id]
                else:
                    value["confirm_attempts"] = attempts
                self._touch(raw, now)
                self._save(state)
                raise FeishuPairingMismatch(pairing_id)
            self._require_bot_binding_slot(raw, bot_id)
            tab_id = _as_text(value.get("tab_id"), "claim.tab_id", limit=_MAX_ID_CHARS)
            workspace_id = _as_optional_text(value.get("workspace_id"), "claim.workspace_id")
            chat_id = _as_text(value.get("chat_id"), "claim.chat_id", limit=_MAX_ID_CHARS)
            sender_open_id = _as_text(
                value.get("sender_open_id"), "claim.sender_open_id", limit=_MAX_ID_CHARS
            )
            self._require_tab_free(state, tab_id)
            self._require_chat_free(state, chat_id)
            self._require_sender_p2p_free(raw, sender_open_id)
            binding_id = secrets.token_hex(16)
            new_binding = {
                **_owner_to_dict(owner),
                "pairing_id": binding_id,
                "tab_id": tab_id,
                "workspace_id": workspace_id,
                "sender_open_id": sender_open_id,
                "chat_id": chat_id,
                "chat_type": BINDING_CHAT_TYPE_P2P,
                "created_at": now,
                "generation": raw["generation"],
            }
            binding = self._binding_from_dict(bot_id, new_binding)
            raw["bindings"] = [*self._active_raw_bindings(raw), new_binding]
            if bot_mode(raw) == BOT_MODE_SINGLE:
                raw["claims"] = {}
                raw["pending"] = {}
            else:
                raw["claims"].pop(pairing_id, None)
            self._touch(raw, now)
            self._save(state)
            return binding

    def release_binding(
        self,
        bot_id: str,
        *,
        expected_revision: int,
        pairing_id: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Unbind explicitly.

        When ``pairing_id`` is omitted, every binding on this Bot is released
        and ``generation`` advances so a message already in flight cannot land
        on a later binding. When ``pairing_id`` is given, only that one binding
        is removed.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            active = self._active_raw_bindings(raw)
            if pairing_id is None:
                released = self._binding_from_dict(bot_id, active[0]) if active else None
                self._advance_generation(raw)
                self._touch(raw, now)
                self._save(state)
                return released
            matched = next(
                (item for item in active if item.get("pairing_id") == pairing_id),
                None,
            )
            if matched is None:
                raise FeishuPairingNotFound(pairing_id)
            released = self._binding_from_dict(bot_id, matched)
            remaining = [item for item in active if item.get("pairing_id") != pairing_id]
            if remaining:
                raw["bindings"] = remaining
            else:
                self._advance_generation(raw)
            self._touch(raw, now)
            self._save(state)
            return released

    def routed_binding(
        self,
        bot_id: str,
        *,
        sender_open_id: str,
        chat_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Resolve the active binding for one verified inbound conversation.

        No Web identity is compared here: the Bot-side ``open_id`` belongs to a
        different application and is not comparable with the Hub owner's.
        """

        now = self._now()
        env_config = environment_config(environ)
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            return None
        entry = self._entry_view(
            bot_id, raw, env_config, now, env_seen=environment_present(environ)
        )
        if not entry.enabled:
            return None
        for binding in entry.bindings:
            if binding.is_group:
                continue
            if hmac.compare_digest(binding.sender_open_id, sender_open_id) and hmac.compare_digest(
                binding.chat_id, chat_id
            ):
                return binding
        return None

    def routed_binding_for_sender(
        self,
        bot_id: str,
        *,
        sender_open_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Resolve a group mention through the sender's existing p2p binding.

        The group ``chat_id`` is deliberately not persisted or trusted as
        pairing authority.
        """

        now = self._now()
        env_config = environment_config(environ)
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            return None
        entry = self._entry_view(
            bot_id, raw, env_config, now, env_seen=environment_present(environ)
        )
        if not entry.enabled:
            return None
        for binding in entry.bindings:
            if binding.is_group:
                continue
            if hmac.compare_digest(binding.sender_open_id, sender_open_id):
                return binding
        return None

    def routed_binding_for_chat(
        self,
        bot_id: str,
        *,
        chat_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Resolve a group binding for one verified inbound group chat.

        A group binding carries no single sender: every member of the bound
        group may address the Bot, and Feishu itself has already proven
        membership by delivering the event. Private bindings never match here,
        so their exact-sender rule stays intact.
        """

        now = self._now()
        env_config = environment_config(environ)
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            return None
        entry = self._entry_view(
            bot_id, raw, env_config, now, env_seen=environment_present(environ)
        )
        if not entry.enabled:
            return None
        for binding in entry.bindings:
            if not binding.is_group:
                continue
            if hmac.compare_digest(binding.chat_id, chat_id):
                return binding
        return None

    def bind_p2p(
        self,
        bot_id: str,
        *,
        owner: OwnerIdentity,
        tab_id: str,
        workspace_id: str | None,
        sender_open_id: str,
        chat_id: str,
        chat_name: str | None = None,
        created_at: float | None = None,
        expected_revision: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding:
        """Bind one private (p2p) conversation directly to a Chat tab."""

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            if expected_revision is not None:
                self._check_revision(raw, expected_revision)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            self._require_bot_binding_slot(raw, bot_id)
            validated_owner = _write_owner(owner, "binding")
            validated_tab = _write_text(tab_id, "binding.tab_id", limit=_MAX_ID_CHARS)
            validated_workspace = _write_optional_text(workspace_id, "binding.workspace_id")
            validated_chat = _write_text(chat_id, "binding.chat_id", limit=_MAX_ID_CHARS)
            validated_sender = _write_text(
                sender_open_id, "binding.sender_open_id", limit=_MAX_ID_CHARS
            )
            validated_chat_name = (
                _write_text(chat_name, "binding.chat_name", limit=_MAX_ID_CHARS)
                if chat_name is not None
                else None
            )
            self._require_tab_free(state, validated_tab)
            self._require_chat_free(state, validated_chat)
            self._require_sender_p2p_free(raw, validated_sender)
            binding_id = secrets.token_hex(16)
            stamp = now if created_at is None else _write_float(created_at, "binding.created_at")
            new_binding = {
                **validated_owner,
                "pairing_id": binding_id,
                "tab_id": validated_tab,
                "workspace_id": validated_workspace,
                "sender_open_id": validated_sender,
                "chat_id": validated_chat,
                "chat_name": validated_chat_name,
                "chat_type": BINDING_CHAT_TYPE_P2P,
                "created_at": stamp,
                "generation": raw["generation"],
            }
            binding = self._binding_from_dict(bot_id, new_binding)
            raw["bindings"] = [*self._active_raw_bindings(raw), new_binding]
            if bot_mode(raw) == BOT_MODE_SINGLE:
                raw["claims"] = {}
                raw["pending"] = {}
            self._touch(raw, now)
            self._save(state)
            return binding

    def bind_group(
        self,
        bot_id: str,
        *,
        owner: OwnerIdentity,
        tab_id: str,
        workspace_id: str | None,
        chat_id: str,
        chat_name: str | None,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding:
        """Bind one group chat without a pairing code.

        The Hub owner acts from an authenticated session and the caller has
        already proven the Bot is a member of ``chat_id``, so no in-chat
        confirmation is required. Occupancy, tab and revision rules are the
        same as the pairing flow.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if not raw.get("enabled"):
                raise FeishuBotRevoked(bot_id)
            self._require_bot_binding_slot(raw, bot_id)
            validated_owner = _write_owner(owner, "binding")
            validated_tab = _write_text(tab_id, "binding.tab_id", limit=_MAX_ID_CHARS)
            validated_workspace = _write_optional_text(workspace_id, "binding.workspace_id")
            validated_chat = _write_text(chat_id, "binding.chat_id", limit=_MAX_ID_CHARS)
            validated_sender = _write_text(
                owner.open_id, "binding.sender_open_id", limit=_MAX_ID_CHARS
            )
            validated_chat_name = (
                _write_text(chat_name, "binding.chat_name", limit=_MAX_ID_CHARS)
                if chat_name is not None
                else None
            )
            self._require_tab_free(state, validated_tab)
            self._require_chat_free(state, validated_chat)
            binding_id = secrets.token_hex(16)
            # Validate before writing: a rejected value must never reach the
            # file, or every later read of the pool would fail.
            new_binding = {
                **validated_owner,
                "pairing_id": binding_id,
                "tab_id": validated_tab,
                "workspace_id": validated_workspace,
                # Group bindings carry no single sender authority; the column
                # stays schema-valid and records who bound it.
                "sender_open_id": validated_sender,
                "chat_id": validated_chat,
                "chat_name": validated_chat_name,
                "chat_type": BINDING_CHAT_TYPE_GROUP,
                "created_at": now,
                "generation": raw["generation"],
            }
            binding = self._binding_from_dict(bot_id, new_binding)
            raw["bindings"] = [*self._active_raw_bindings(raw), new_binding]
            if bot_mode(raw) == BOT_MODE_SINGLE:
                raw["claims"] = {}
                raw["pending"] = {}
            self._touch(raw, now)
            self._save(state)
            return binding

    def drop_binding(self, bot_id: str, pairing_id: str) -> bool:
        """Remove one specific binding without CAS.

        Matching on ``pairing_id`` is what makes this safe from a background
        cleanup path: a binding created after the caller's snapshot has a
        different id and is never touched.
        """

        now = self._now()
        with self._lock:
            state = self._load()
            raw = state["bots"].get(bot_id)
            if not isinstance(raw, dict) or raw.get("revoked_at"):
                return False
            active = self._active_raw_bindings(raw)
            if not any(item.get("pairing_id") == pairing_id for item in active):
                return False
            remaining = [item for item in active if item.get("pairing_id") != pairing_id]
            if remaining:
                raw["bindings"] = remaining
            else:
                self._advance_generation(raw)
            self._touch(raw, now)
            self._save(state)
            return True
