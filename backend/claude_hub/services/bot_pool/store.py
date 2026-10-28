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
    ENV_BOT_ID,
    MAX_BOTS,
    BotBinding,
    FeishuBindingCodeError,
    FeishuBotConfig,
    FeishuBotOccupied,
    FeishuBotRateLimited,
    FeishuBotReadOnly,
    FeishuBotRevoked,
    FeishuPairingMismatch,
    FeishuPairingNotFound,
    FeishuPairingNotOwned,
    FeishuPoolFull,
    Mapping,
    OwnerIdentity,
    PairingClaim,
    _as_int,
    _as_text,
    _code_digest,
    _confirm_digest,
    _owner_from_dict,
    _owner_to_dict,
    _safe_timestamp,
    environment_config,
    environment_present,
    hmac,
    secrets,
)
from claude_hub.services.bot_pool.state import FeishuBotPoolStateStore


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
        environ: Mapping[str, str] | None = None,
    ) -> None:
        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            if name is not None:
                raw["name"] = name
            if enabled is not None and enabled != raw.get("enabled"):
                raw["enabled"] = enabled
                if not enabled:
                    # Disabling revokes outstanding pairing state for this Bot.
                    self._advance_generation(raw)
            self._touch(raw, now)
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
            if raw.get("binding") is not None:
                raise FeishuBotOccupied(bot_id)
            self._require_tab_free(state, tab_id, exclude=bot_id)
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
                **_owner_to_dict(owner),
                "tab_id": tab_id,
                "workspace_id": workspace_id,
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
            raw["claims"][pairing_id] = {
                **_owner_to_dict(_owner_from_dict(value, "pending")),
                "tab_id": value["tab_id"],
                "workspace_id": value.get("workspace_id"),
                "sender_open_id": sender_open_id,
                "chat_id": chat_id,
                "created_at": now,
                "expires_at": now + _CLAIM_TTL_SECONDS,
                "generation": raw["generation"],
                # The confirmation word protects online pairing only. File
                # confidentiality relies on the private directory and file mode.
                "confirm_digest": _confirm_digest(bot_id, pairing_id, word),
                "confirm_attempts": 0,
            }
            self._touch(raw, now)
            self._save(state)
            claim = self._claim_from_dict(bot_id, pairing_id, raw["claims"][pairing_id])
            return claim, word

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
            if raw.get("binding") is not None:
                raise FeishuBotOccupied(bot_id)
            tab_id = _as_text(value.get("tab_id"), "claim.tab_id", limit=_MAX_ID_CHARS)
            self._require_tab_free(state, tab_id, exclude=bot_id)
            binding_id = secrets.token_hex(16)
            raw["binding"] = {
                **_owner_to_dict(owner),
                "pairing_id": binding_id,
                "tab_id": tab_id,
                "workspace_id": value.get("workspace_id"),
                "sender_open_id": value["sender_open_id"],
                "chat_id": value["chat_id"],
                "created_at": now,
                "generation": raw["generation"],
            }
            raw["claims"] = {}
            raw["pending"] = {}
            self._touch(raw, now)
            self._save(state)
            return self._binding_from_dict(bot_id, raw["binding"])

    def release_binding(
        self,
        bot_id: str,
        *,
        expected_revision: int,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Unbind explicitly. Advancing the generation is what guarantees that
        a message already in flight cannot land on a later binding."""

        now = self._now()
        with self._lock:
            state = self._load()
            self._prune(state, now)
            self._sync_env_entry(state, environ, now)
            raw = self._live_entry(state, bot_id)
            self._check_revision(raw, expected_revision)
            previous = raw.get("binding")
            released = (
                self._binding_from_dict(bot_id, previous) if isinstance(previous, dict) else None
            )
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
        binding = entry.binding
        if binding is None or not entry.enabled:
            return None
        if not hmac.compare_digest(binding.sender_open_id, sender_open_id):
            return None
        if not hmac.compare_digest(binding.chat_id, chat_id):
            return None
        return binding

    def routed_binding_for_sender(
        self,
        bot_id: str,
        *,
        sender_open_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> BotBinding | None:
        """Resolve a group mention through the sender's existing p2p binding.

        A Bot has at most one active binding, so an exact sender match is
        unambiguous. The group ``chat_id`` is deliberately not persisted or
        trusted as pairing authority.
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
        binding = entry.binding
        if binding is None or not entry.enabled:
            return None
        if not hmac.compare_digest(binding.sender_open_id, sender_open_id):
            return None
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
            binding = raw.get("binding")
            if not isinstance(binding, dict) or binding.get("pairing_id") != pairing_id:
                return False
            self._advance_generation(raw)
            self._touch(raw, now)
            self._save(state)
            return True
