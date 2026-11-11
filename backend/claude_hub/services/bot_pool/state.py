"""Atomic Bot pool state codec and synchronized state access."""

from claude_hub.services.bot_pool.common import (
    _LEGACY_STATE_VERSION,
    _MAX_ID_CHARS,
    _MAX_NAME_CHARS,
    _MAX_SECRET_CHARS,
    _PREVIOUS_STATE_VERSION,
    _RATE_WINDOW_SECONDS,
    _STATE_VERSION,
    _TOMBSTONE_RETENTION_SECONDS,
    _V2_STATE_VERSION,
    BOT_MODE_SINGLE,
    ENV_BOT_ID,
    LEGACY_BOT_ID,
    MAX_CHANNELS_PER_BOT,
    OFFICIAL_FEISHU_API,
    Any,
    BotBinding,
    BotEntry,
    EffectiveBot,
    FeishuBotAppIdConflict,
    FeishuBotConfig,
    FeishuBotNotFound,
    FeishuBotOccupied,
    FeishuBotPoolError,
    FeishuBotPoolStateError,
    FeishuBotRevisionConflict,
    FeishuBotRevoked,
    FeishuBotUnavailable,
    FeishuChatOccupied,
    FeishuPoolFull,
    Mapping,
    PairingClaim,
    Path,
    PoolSnapshot,
    _as_bool,
    _as_float,
    _as_int,
    _as_mapping,
    _as_optional_text,
    _as_text,
    _owner_from_dict,
    _safe_timestamp,
    _state_error,
    _validate_pool_numbers,
    binding_chat_type,
    bot_mode,
    bot_transport_revision,
    deprecated_env_present,
    environment_config,
    environment_present,
    os,
    resolve_runtime_home,
    threading,
    time,
)
from claude_hub.services.local_storage import read_document, write_document


class FeishuBotPoolStateStore(object):
    """Single-writer SQLite pool guarded by one process lock."""

    def __init__(
        self,
        path: Path | None = None,
        legacy_path: Path | None = None,
        now: Any = time.time,
    ) -> None:
        home = resolve_runtime_home()
        self.path = path or (home / "secrets" / "feishu_bot_pool.json")
        self.legacy_path = legacy_path or (home / "secrets" / "feishu_bot.json")
        self._now = now
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- storage

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "pool_revision": 0,
            "bots": {},
            "rate_limits": {},
        }

    def _read_file(self, path: Path) -> dict[str, Any] | None:
        try:
            value = read_document(path)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise FeishuBotPoolStateError("Cannot read Feishu Bot pool state") from exc
        except ValueError as exc:
            raise FeishuBotPoolStateError("Invalid Feishu Bot pool state") from exc
        if not isinstance(value, dict):
            raise FeishuBotPoolStateError("Invalid Feishu Bot pool state")
        return value

    @staticmethod
    def _migrate_entry_bindings(raw: dict[str, Any]) -> None:
        """Migrate v2/v3 single ``binding`` to ``bindings`` and normalize ``mode``."""

        if raw.get("revoked_at") is not None and "binding" not in raw and "bindings" not in raw:
            if "transport_revision" in raw and raw["transport_revision"] is not None:
                bot_transport_revision(raw)
            return
        existing = raw.get("bindings")
        if existing is not None and not isinstance(existing, list):
            raise _state_error("bot.bindings")
        if "binding" in raw:
            legacy_binding = raw.pop("binding")
            if legacy_binding is not None:
                mapped_legacy = _as_mapping(legacy_binding, "binding")
                if not existing:
                    raw["bindings"] = [mapped_legacy]
                else:
                    existing_ids = {
                        item.get("pairing_id")
                        for item in existing
                        if isinstance(item, dict) and item.get("pairing_id") is not None
                    }
                    if mapped_legacy.get("pairing_id") not in existing_ids:
                        raw["bindings"] = [mapped_legacy, *existing]
            elif existing is None:
                raw["bindings"] = []
        elif existing is None:
            raw["bindings"] = []
        if "mode" not in raw or raw["mode"] is None:
            raw["mode"] = BOT_MODE_SINGLE
        else:
            bot_mode(raw)
        raw["transport_revision"] = bot_transport_revision(raw)

    def _load(self) -> dict[str, Any]:
        value = self._read_file(self.path)
        if value is None:
            # Once the pool file exists it is the only authority. The legacy file
            # is never consulted again, so a rolled-back write cannot resurrect a
            # revoked authorization.
            return self._migrate_legacy()
        version = _as_int(value.get("version"), "version")
        if version not in {_V2_STATE_VERSION, _PREVIOUS_STATE_VERSION, _STATE_VERSION}:
            raise FeishuBotPoolStateError("Unsupported Feishu Bot pool state version")
        bots = _as_mapping(value.get("bots"), "bots")
        rate_limits = _as_mapping(value.get("rate_limits"), "rate_limits")
        for bot_id, raw in bots.items():
            _as_text(bot_id, "bots key", limit=_MAX_ID_CHARS)
            raw_entry = _as_mapping(raw, f"bots[{bot_id}]")
            credentials = raw_entry.get("credentials")
            if isinstance(credentials, dict):
                raw_entry["credentials"] = {"app_secret": credentials.get("app_secret")}
            self._migrate_entry_bindings(raw_entry)
        _validate_pool_numbers(bots, rate_limits)
        return {
            "version": _STATE_VERSION,
            "pool_revision": _as_int(value.get("pool_revision"), "pool_revision"),
            "bots": bots,
            "rate_limits": rate_limits,
        }

    def _migrate_legacy(self) -> dict[str, Any]:
        """Convert the v1 single-Bot credential file into a one-entry pool.

        Fails closed: a legacy file that exists but cannot be trusted must not
        be presented as an empty pool, or an operator would re-create a Bot
        while the old credentials are still on disk.

        Only credentials migrate. Old bindings are intentionally dropped: their
        authentication premise was that the Bot app and the Web OAuth app were
        the same, so ``owner_open_id`` could be compared with the Feishu sender.
        The pool abolishes that premise, and reinstating those bindings would
        mean trusting a historical self-identification that cannot be
        re-verified. Affected users pair once more.
        """

        value = self._read_file(self.legacy_path)
        if value is None:
            return self._empty()
        required = {"version", "revision", "binding_generation", "updated_at", "config"}
        if (
            not required.issubset(value)
            or _as_int(value.get("version"), "legacy.version") != _LEGACY_STATE_VERSION
        ):
            raise FeishuBotPoolStateError("Unsupported legacy Feishu Bot configuration")
        revision = _as_int(value.get("revision"), "legacy.revision")
        generation = _as_int(value.get("binding_generation"), "legacy.binding_generation")
        raw_updated = value.get("updated_at")
        updated_at = None if raw_updated is None else _as_float(raw_updated, "legacy.updated_at")
        raw_config = value.get("config")
        state = self._empty()
        if raw_config is None:
            # The old store wrote config=None for an explicit disable. Keep that
            # as a tombstone so events for the retired Bot stay silent.
            state["bots"][LEGACY_BOT_ID] = {
                "bot_id": LEGACY_BOT_ID,
                "revision": revision,
                "generation": generation,
                "revoked_at": updated_at if updated_at is not None else self._now(),
            }
            return state
        if not isinstance(raw_config, dict):
            raise FeishuBotPoolStateError("Invalid legacy Feishu Bot configuration")
        keys = ("app_id", "app_secret")
        if not all(isinstance(raw_config.get(key), str) and raw_config[key] for key in keys):
            raise FeishuBotPoolStateError("Incomplete legacy Feishu Bot configuration")
        state["bots"][LEGACY_BOT_ID] = {
            "bot_id": LEGACY_BOT_ID,
            "name": "Feishu Bot",
            "app_id": raw_config["app_id"],
            "mode": BOT_MODE_SINGLE,
            "credentials": {
                "app_secret": raw_config["app_secret"],
            },
            "revision": revision,
            "generation": generation,
            "transport_revision": revision,
            "enabled": True,
            "created_at": updated_at,
            "updated_at": updated_at,
            "pending": {},
            "claims": {},
            "bindings": [],
            "revoked_at": None,
        }
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["pool_revision"] = _as_int(state.get("pool_revision", 0), "pool_revision") + 1
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        write_document(self.path, state)

    # ------------------------------------------------------------- read model

    @staticmethod
    def _claim_from_dict(bot_id: str, pairing_id: str, value: dict[str, Any]) -> PairingClaim:
        name = f"claims[{pairing_id}]"
        return PairingClaim(
            pairing_id=pairing_id,
            bot_id=bot_id,
            owner=_owner_from_dict(value, name),
            tab_id=_as_text(value.get("tab_id"), f"{name}.tab_id", limit=_MAX_ID_CHARS),
            workspace_id=_as_optional_text(value.get("workspace_id"), f"{name}.workspace_id"),
            sender_open_id=_as_text(
                value.get("sender_open_id"),
                f"{name}.sender_open_id",
                limit=_MAX_ID_CHARS,
            ),
            chat_id=_as_text(value.get("chat_id"), f"{name}.chat_id", limit=_MAX_ID_CHARS),
            created_at=_as_float(value.get("created_at"), f"{name}.created_at"),
            expires_at=_as_float(value.get("expires_at"), f"{name}.expires_at"),
            generation=_as_int(value.get("generation"), f"{name}.generation"),
        )

    @staticmethod
    def _binding_from_dict(bot_id: str, value: dict[str, Any]) -> BotBinding:
        return BotBinding(
            pairing_id=_as_text(value.get("pairing_id"), "binding.pairing_id", limit=_MAX_ID_CHARS),
            bot_id=bot_id,
            owner=_owner_from_dict(value, "binding"),
            tab_id=_as_text(value.get("tab_id"), "binding.tab_id", limit=_MAX_ID_CHARS),
            workspace_id=_as_optional_text(value.get("workspace_id"), "binding.workspace_id"),
            sender_open_id=_as_text(
                value.get("sender_open_id"),
                "binding.sender_open_id",
                limit=_MAX_ID_CHARS,
            ),
            chat_id=_as_text(value.get("chat_id"), "binding.chat_id", limit=_MAX_ID_CHARS),
            created_at=_as_float(value.get("created_at"), "binding.created_at"),
            generation=_as_int(value.get("generation"), "binding.generation"),
            # Legacy bindings predate groups and are all private pairings.
            chat_type=binding_chat_type(value),
            chat_name=_as_optional_text(value.get("chat_name"), "binding.chat_name"),
        )

    @staticmethod
    def _new_entry(bot_id: str, name: str, app_id: str, now: float) -> dict[str, Any]:
        return {
            "bot_id": bot_id,
            "name": name,
            "app_id": app_id,
            "mode": BOT_MODE_SINGLE,
            "credentials": None,
            "revision": 0,
            "generation": 0,
            "transport_revision": 0,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "pending": {},
            "claims": {},
            "bindings": [],
            "revoked_at": None,
        }

    @staticmethod
    def _entry_config(
        raw: dict[str, Any], env_config: FeishuBotConfig | None
    ) -> FeishuBotConfig | None:
        """Credentials come from the file, or from the environment for ``env``."""

        if raw["bot_id"] == ENV_BOT_ID:
            if env_config is None or raw.get("app_id") != env_config.app_id:
                return None
            return env_config
        credentials = raw.get("credentials")
        if not isinstance(credentials, dict):
            return None
        return FeishuBotConfig(
            app_id=_as_text(raw.get("app_id"), "app_id", limit=_MAX_ID_CHARS),
            app_secret=_as_text(credentials.get("app_secret"), "app_secret"),
            api_base_url=OFFICIAL_FEISHU_API,
        )

    def _entry_view(
        self,
        bot_id: str,
        raw: dict[str, Any],
        env_config: FeishuBotConfig | None,
        now: float,
        *,
        env_seen: bool = False,
    ) -> BotEntry:
        is_env = bot_id == ENV_BOT_ID
        config = self._entry_config(raw, env_config)
        generation = _as_int(raw.get("generation"), "generation")
        claims: list[PairingClaim] = []
        for pairing_id, value in _as_mapping(raw.get("claims", {}), "claims").items():
            if not isinstance(value, dict):
                continue
            try:
                claim = self._claim_from_dict(bot_id, pairing_id, value)
            except FeishuBotPoolStateError:
                continue
            if claim.expires_at > now and claim.generation == generation:
                claims.append(claim)
        mode = bot_mode(raw)
        raw_bindings = raw.get("bindings")
        if raw_bindings is None or raw_bindings == []:
            raw_binding = raw.get("binding")
            if raw_binding is not None:
                raw_bindings = [raw_binding]
            elif raw_bindings is None:
                raw_bindings = []
        if not isinstance(raw_bindings, list):
            raise _state_error("bot.bindings")
        indexed_bindings: list[tuple[float, int, BotBinding]] = []
        for idx, item in enumerate(raw_bindings):
            candidate = self._binding_from_dict(bot_id, _as_mapping(item, "binding"))
            if candidate.generation == generation:
                indexed_bindings.append((candidate.created_at, idx, candidate))
        indexed_bindings.sort(key=lambda pair: (pair[0], pair[1]))
        bindings = tuple(pair[2] for pair in indexed_bindings)
        source = "stored"
        if is_env:
            source = "invalid_environment" if env_seen and config is None else "environment"
        callbacks = _as_mapping(raw.get("callbacks", {}), "callbacks")
        callback_enabled = _as_bool(callbacks.get("callback_enabled", False), "callback_enabled")
        verification_token = _as_optional_text(
            callbacks.get("verification_token"), "verification_token", limit=_MAX_SECRET_CHARS
        )
        encrypt_key = _as_optional_text(
            callbacks.get("encrypt_key"), "encrypt_key", limit=_MAX_SECRET_CHARS
        )
        expected_tenant_key = _as_optional_text(
            callbacks.get("expected_tenant_key"), "expected_tenant_key", limit=_MAX_ID_CHARS
        )
        if callback_enabled and not (verification_token and encrypt_key and expected_tenant_key):
            raise FeishuBotPoolStateError(
                "Enabled callbacks require protected credentials and tenant"
            )
        return BotEntry(
            bot_id=bot_id,
            name=_as_text(raw.get("name"), "name", limit=_MAX_NAME_CHARS),
            feishu_name=_as_optional_text(
                raw.get("feishu_name"), "feishu_name", limit=_MAX_NAME_CHARS
            ),
            feishu_avatar_url=_as_optional_text(
                raw.get("feishu_avatar_url"),
                "feishu_avatar_url",
                limit=_MAX_SECRET_CHARS,
            ),
            feishu_open_id=_as_optional_text(
                raw.get("feishu_open_id"), "feishu_open_id", limit=_MAX_ID_CHARS
            ),
            app_id=(config.app_id if config is not None else str(raw.get("app_id") or "")),
            source=source,
            enabled=_as_bool(raw.get("enabled"), "enabled"),
            credentials_editable=not is_env,
            deletable=not is_env,
            revision=_as_int(raw.get("revision"), "revision"),
            generation=generation,
            configured=config is not None,
            created_at=(
                None
                if raw.get("created_at") is None
                else _as_float(raw["created_at"], "created_at")
            ),
            updated_at=(
                None
                if raw.get("updated_at") is None
                else _as_float(raw["updated_at"], "updated_at")
            ),
            config=config,
            claims=tuple(claims),
            bindings=bindings,
            mode=mode,
            callback_enabled=callback_enabled,
            verification_token=verification_token,
            encrypt_key=encrypt_key,
            expected_tenant_key=expected_tenant_key,
            transport_revision=bot_transport_revision(raw),
        )

    def snapshot(self, environ: Mapping[str, str] | None = None) -> PoolSnapshot:
        env_config = environment_config(environ)
        now = self._now()
        with self._lock:
            state = self._load_synced(environ, now)
        raw_bots = dict(state["bots"])
        entries = tuple(
            self._entry_view(bot_id, raw, env_config, now, env_seen=environment_present(environ))
            for bot_id, raw in sorted(raw_bots.items())
            if isinstance(raw, dict) and not raw.get("revoked_at")
        )
        return PoolSnapshot(
            pool_revision=state["pool_revision"],
            bots=entries,
            deprecated_env=deprecated_env_present(environ),
        )

    def effective(self, bot_id: str, environ: Mapping[str, str] | None = None) -> EffectiveBot:
        """Resolve usable credentials, distinguishing revoked from unknown."""

        env_config = environment_config(environ)
        now = self._now()
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict):
            raise FeishuBotNotFound(bot_id)
        if raw.get("revoked_at"):
            raise FeishuBotRevoked(bot_id)
        entry = self._entry_view(
            bot_id, raw, env_config, now, env_seen=environment_present(environ)
        )
        if not entry.enabled:
            raise FeishuBotRevoked(bot_id)
        if entry.config is None:
            raise FeishuBotUnavailable(bot_id)
        return EffectiveBot(
            bot_id=bot_id,
            app_id=entry.config.app_id,
            config=entry.config,
            revision=entry.revision,
            generation=entry.generation,
            feishu_name=entry.feishu_name,
            feishu_avatar_url=entry.feishu_avatar_url,
            feishu_open_id=entry.feishu_open_id,
            callback_enabled=entry.callback_enabled,
            verification_token=entry.verification_token,
            encrypt_key=entry.encrypt_key,
            expected_tenant_key=entry.expected_tenant_key,
            transport_revision=entry.transport_revision,
        )

    def is_current(self, effective: EffectiveBot, environ: Mapping[str, str] | None = None) -> bool:
        """Re-check a pre-await snapshot before acting on it."""

        try:
            current = self.effective(effective.bot_id, environ)
        except FeishuBotPoolError:
            return False
        return (
            current.app_id == effective.app_id
            and current.revision == effective.revision
            and current.generation == effective.generation
            and current.config == effective.config
            and current.callback_enabled == effective.callback_enabled
            and current.verification_token == effective.verification_token
            and current.encrypt_key == effective.encrypt_key
            and current.expected_tenant_key == effective.expected_tenant_key
        )

    def is_current_for_delivery(
        self, effective: EffectiveBot, environ: Mapping[str, str] | None = None
    ) -> bool:
        """Check whether a Bot's credentials, generation, and callback state still match.

        Unlike ``is_current``, this delivery fence ignores ``revision`` so that
        sibling channel mutations and Bot renames do not invalidate another
        channel's in-flight turn, outbound reply, or question card while HTTP
        mutations still advance ``revision`` for CAS. Channel-level authority is
        fenced separately by ``pairing_id`` via ``_binding_still_routes``.
        """

        try:
            current = self.effective(effective.bot_id, environ)
        except FeishuBotPoolError:
            return False
        return (
            current.app_id == effective.app_id
            and current.generation == effective.generation
            and current.transport_revision == effective.transport_revision
            and current.config == effective.config
            and current.callback_enabled == effective.callback_enabled
            and current.verification_token == effective.verification_token
            and current.encrypt_key == effective.encrypt_key
            and current.expected_tenant_key == effective.expected_tenant_key
        )

    def transport_revision(self, bot_id: str, environ: Mapping[str, str] | None = None) -> int:
        """Return the revision at which credentials, callbacks, or generation last changed."""

        now = self._now()
        with self._lock:
            state = self._load_synced(environ, now)
            raw = state["bots"].get(bot_id)
            if not isinstance(raw, dict) or raw.get("revoked_at"):
                raise FeishuBotNotFound(bot_id)
            return bot_transport_revision(raw)

    # ------------------------------------------------------------ invariants

    def _prune(self, state: dict[str, Any], now: float) -> None:
        cutoff = now - _RATE_WINDOW_SECONDS
        state["rate_limits"] = {
            owner: kept
            for owner, stamps in state["rate_limits"].items()
            if isinstance(stamps, list)
            for kept in (
                [float(s) for s in stamps if isinstance(s, (int, float)) and float(s) >= cutoff],
            )
            if kept
        }
        for bot_id, raw in list(state["bots"].items()):
            if not isinstance(raw, dict):
                del state["bots"][bot_id]
                continue
            revoked_at = raw.get("revoked_at")
            if isinstance(revoked_at, (int, float)):
                if now - float(revoked_at) > _TOMBSTONE_RETENTION_SECONDS:
                    del state["bots"][bot_id]
                continue
            generation = raw.get("generation")
            raw["pending"] = {
                digest: value
                for digest, value in _as_mapping(raw.get("pending", {}), "pending").items()
                if isinstance(value, dict)
                and _safe_timestamp(value.get("expires_at")) > now
                and value.get("generation") == generation
            }
            raw["claims"] = {
                pairing_id: value
                for pairing_id, value in _as_mapping(raw.get("claims", {}), "claims").items()
                if isinstance(value, dict)
                and _safe_timestamp(value.get("expires_at")) > now
                and value.get("generation") == generation
            }

    def _live_entry(self, state: dict[str, Any], bot_id: str) -> dict[str, Any]:
        raw = state["bots"].get(bot_id)
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            raise FeishuBotNotFound(bot_id)
        return raw

    def _sync_env_entry(
        self, state: dict[str, Any], environ: Mapping[str, str] | None, now: float
    ) -> bool:
        """Revoke changed or conflicting environment identity once, durably."""
        config = environment_config(environ)
        app_id = config.app_id if config is not None else ""
        if app_id and any(
            other_id != ENV_BOT_ID
            and isinstance(other, dict)
            and not other.get("revoked_at")
            and other.get("app_id") == app_id
            for other_id, other in state["bots"].items()
        ):
            app_id = ""
        raw = state["bots"].get(ENV_BOT_ID)
        if raw is None:
            if not environment_present(environ):
                return False
            state["bots"][ENV_BOT_ID] = self._new_entry(ENV_BOT_ID, "Environment Bot", app_id, now)
            return True
        if not isinstance(raw, dict) or raw.get("revoked_at"):
            return False
        if raw.get("app_id") == app_id:
            return False
        # Compare the accepted identity, not the rejected environment value, so
        # reads in a persistent conflict cannot advance the counters forever.
        raw["app_id"] = app_id
        raw["feishu_name"] = None
        raw["feishu_avatar_url"] = None
        raw["feishu_open_id"] = None
        self._advance_generation(raw)
        self._touch(raw, now)
        self._touch_transport(raw)
        return True

    def _load_synced(self, environ: Mapping[str, str] | None, now: float) -> dict[str, Any]:
        state = self._load()
        if self._sync_env_entry(state, environ, now):
            self._save(state)
        return state

    @staticmethod
    def _check_revision(raw: dict[str, Any], expected_revision: int) -> None:
        if _as_int(raw.get("revision"), "revision") != expected_revision:
            raise FeishuBotRevisionConflict(str(raw.get("bot_id")))

    @staticmethod
    def _touch(raw: dict[str, Any], now: float) -> None:
        raw["revision"] = _as_int(raw.get("revision"), "revision") + 1
        raw["updated_at"] = now

    @staticmethod
    def _touch_transport(raw: dict[str, Any]) -> None:
        raw["transport_revision"] = _as_int(raw.get("revision"), "revision")

    @staticmethod
    def _advance_generation(raw: dict[str, Any]) -> None:
        """Invalidate every outstanding code, claim, and binding for this Bot."""

        raw["generation"] = _as_int(raw.get("generation"), "generation") + 1
        raw["pending"] = {}
        raw["claims"] = {}
        raw.pop("binding", None)
        raw["bindings"] = []

    @staticmethod
    def _active_raw_bindings(raw: dict[str, Any]) -> list[dict[str, Any]]:
        generation = raw.get("generation")
        items = raw.get("bindings")
        if not isinstance(items, list) or not items:
            single = raw.get("binding")
            if isinstance(single, dict):
                items = [single]
            elif not isinstance(items, list):
                items = []
        return [
            item
            for item in items
            if isinstance(item, dict) and item.get("generation") == generation
        ]

    @classmethod
    def _require_bot_binding_slot(cls, raw: dict[str, Any], bot_id: str) -> None:
        active = cls._active_raw_bindings(raw)
        if bot_mode(raw) == BOT_MODE_SINGLE:
            if active:
                raise FeishuBotOccupied(bot_id)
        elif len(active) >= MAX_CHANNELS_PER_BOT:
            raise FeishuPoolFull("channels")

    @classmethod
    def _require_tab_free(cls, state: dict[str, Any], tab_id: str) -> None:
        for _other_id, raw in state["bots"].items():
            if not isinstance(raw, dict) or raw.get("revoked_at"):
                continue
            for binding in cls._active_raw_bindings(raw):
                if binding.get("tab_id") == tab_id:
                    raise FeishuChatOccupied(tab_id)

    @classmethod
    def _require_chat_free(cls, state: dict[str, Any], chat_id: str) -> None:
        for _other_id, raw in state["bots"].items():
            if not isinstance(raw, dict) or raw.get("revoked_at"):
                continue
            for binding in cls._active_raw_bindings(raw):
                if binding.get("chat_id") == chat_id:
                    raise FeishuChatOccupied(chat_id)

    @staticmethod
    def _require_app_id_free(state: dict[str, Any], app_id: str, *, exclude: str | None) -> None:
        for other_id, raw in state["bots"].items():
            if other_id == exclude or not isinstance(raw, dict) or raw.get("revoked_at"):
                continue
            if raw.get("app_id") == app_id:
                raise FeishuBotAppIdConflict(app_id)
