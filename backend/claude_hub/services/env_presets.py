"""Persistence service for user-defined environment-variable presets.

Stores custom presets and hidden built-in preset IDs in a JSON file under
``~/.claude_hub/env_presets.json`` so that presets survive across origins
(localhost vs LAN IP) and across browser machines. Built-in presets themselves
are hardcoded in the frontend; this service only stores the delta (custom
presets + which built-ins the user has chosen to hide).

Security note: env preset text may contain API tokens / secrets. This module
never logs the ``text`` field of any preset — only names and IDs appear in log
messages.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from ..models import EnvPreset, EnvPresetBulkImport

logger = logging.getLogger(__name__)

ENV_PRESETS_FILE = Path.home() / ".claude_hub" / "env_presets.json"

# Built-in preset IDs that can be hidden. Kept in sync with frontend
# BUILT_IN_PRESET_IDS (useLaunchEnvPresets.ts).
BUILT_IN_PRESET_IDS = frozenset(
    {"none", "local-proxy-7890", "socks-proxy-1080", "volcengine-coding-plan"}
)


class _EnvPresetState:
    """In-memory representation of the persisted state."""

    __slots__ = ("custom_presets", "hidden_builtin_ids")

    def __init__(self) -> None:
        self.custom_presets: dict[str, EnvPreset] = {}
        self.hidden_builtin_ids: set[str] = set()

    def to_response(self) -> dict:
        return {
            "custom_presets": [preset.model_dump() for preset in self.custom_presets.values()],
            "hidden_builtin_ids": sorted(self.hidden_builtin_ids),
        }


class EnvPresetManager:
    """Thread-safe JSON-file backed manager for env presets."""

    def __init__(self, path: Path = ENV_PRESETS_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._state = _EnvPresetState()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_presets(self) -> dict:
        """Return the full state dict (custom_presets list + hidden ids)."""
        with self._lock:
            return self._state.to_response()

    def get_preset(self, preset_id: str) -> Optional[EnvPreset]:
        with self._lock:
            return self._state.custom_presets.get(preset_id)

    def create_preset(self, name: str, text: str, preset_id: Optional[str] = None) -> EnvPreset:
        """Create a new custom preset. If ``preset_id`` is provided it is used
        (caller-responsible for uniqueness); otherwise an id is generated."""
        if preset_id is None:
            import time

            preset_id = f"custom-{int(time.time() * 1000):x}"
        preset = EnvPreset(id=preset_id, name=name.strip(), text=text)
        with self._lock:
            if preset_id in self._state.custom_presets:
                raise ValueError(f"Preset with id '{preset_id}' already exists")
            self._state.custom_presets[preset_id] = preset
            self._save()
        logger.info(f"Created env preset '{name}' (id={preset_id})")
        return preset

    def update_preset(
        self,
        preset_id: str,
        name: Optional[str] = None,
        text: Optional[str] = None,
    ) -> Optional[EnvPreset]:
        """Update an existing custom preset. Returns None if not found."""
        with self._lock:
            preset = self._state.custom_presets.get(preset_id)
            if preset is None:
                return None
            if name is not None:
                preset.name = name.strip()
            if text is not None:
                preset.text = text
            self._state.custom_presets[preset_id] = preset
            self._save()
        logger.info(f"Updated env preset '{preset.name}' (id={preset_id})")
        return preset

    def upsert_preset(self, preset_id: str, name: str, text: str) -> EnvPreset:
        """Create or update (by id) — used for sync/merge."""
        with self._lock:
            existing = self._state.custom_presets.get(preset_id)
            if existing is not None:
                existing.name = name.strip()
                existing.text = text
                self._save()
                logger.info(f"Upserted (updated) env preset '{existing.name}' (id={preset_id})")
                return existing
            preset = EnvPreset(id=preset_id, name=name.strip(), text=text)
            self._state.custom_presets[preset_id] = preset
            self._save()
        logger.info(f"Upserted (created) env preset '{preset.name}' (id={preset_id})")
        return preset

    def delete_preset(self, preset_id: str) -> bool:
        """Delete a custom preset. Returns False if not found or is a built-in."""
        if preset_id in BUILT_IN_PRESET_IDS:
            # Built-in presets are hidden, not deleted.
            return False
        with self._lock:
            if preset_id not in self._state.custom_presets:
                return False
            removed = self._state.custom_presets.pop(preset_id)
            self._save()
        logger.info(f"Deleted env preset '{removed.name}' (id={preset_id})")
        return True

    def set_hidden(self, preset_id: str, hidden: bool) -> bool:
        """Hide or unhide a built-in preset. Returns False if id is not a built-in."""
        if preset_id not in BUILT_IN_PRESET_IDS:
            return False
        with self._lock:
            if hidden:
                self._state.hidden_builtin_ids.add(preset_id)
            else:
                self._state.hidden_builtin_ids.discard(preset_id)
            self._save()
        action = "hidden" if hidden else "unhidden"
        logger.info(f"{action.capitalize()} built-in env preset (id={preset_id})")
        return True

    def bulk_import(self, payload: EnvPresetBulkImport) -> dict:
        """Merge-import presets and hidden ids from a client (localStorage migration).

        - Custom presets are merged by id (existing presets are NOT overwritten
          by imported ones — backend wins on conflict to prevent older/stale
          clients from clobbering newer server state).
        - Hidden built-in ids are unioned (no unhide on import).
        Returns the post-import state.
        """
        imported_count = 0
        with self._lock:
            for item in payload.custom_presets:
                if item.id in self._state.custom_presets:
                    continue  # backend wins on conflict
                if not item.name.strip() or not item.text.strip():
                    continue
                self._state.custom_presets[item.id] = EnvPreset(
                    id=item.id, name=item.name.strip(), text=item.text
                )
                imported_count += 1
            for hid in payload.hidden_builtin_ids:
                if hid in BUILT_IN_PRESET_IDS:
                    self._state.hidden_builtin_ids.add(hid)
            self._save()
        if imported_count:
            logger.info(f"Bulk-imported {imported_count} env preset(s) from client migration")
        return self._state.to_response()

    # ------------------------------------------------------------------
    # Persistence (internal)
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            logger.error(f"Failed to load env presets from {self.path}: {exc}")
            return

        custom_raw = raw.get("custom_presets", [])
        hidden_raw = raw.get("hidden_builtin_ids", [])

        if not isinstance(custom_raw, list):
            logger.warning(f"Invalid env presets file: custom_presets is not a list in {self.path}")
            custom_raw = []
        if not isinstance(hidden_raw, list):
            logger.warning(
                f"Invalid env presets file: hidden_builtin_ids is not a list in {self.path}"
            )
            hidden_raw = []

        for item in custom_raw:
            try:
                preset = EnvPreset(**item)
                if not preset.name.strip() or not preset.text.strip():
                    continue
                self._state.custom_presets[preset.id] = preset
            except Exception as exc:
                logger.warning(f"Skipping invalid env preset entry: {exc}")

        for hid in hidden_raw:
            if isinstance(hid, str) and hid in BUILT_IN_PRESET_IDS:
                self._state.hidden_builtin_ids.add(hid)

        loaded = len(self._state.custom_presets)
        logger.info(f"Loaded {loaded} env preset(s) from {self.path}")

    def _save(self) -> None:
        """Must be called while holding ``self._lock``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._state.to_response()
        tmp_path = self.path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(self.path)


# Module-level singleton (same pattern as remote_profile_manager).
env_preset_manager = EnvPresetManager()
