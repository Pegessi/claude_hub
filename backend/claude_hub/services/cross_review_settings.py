"""Shared persistent defaults for Hub subagent delegation and Goal cross-review."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.goal_run import SubagentModelPreference
from .runtime_isolation import resolve_runtime_home

_LEGACY_DROPPED_SETTINGS_KEYS = frozenset(
    {
        "review_mode",
        "include_review_briefing",
        "generate_mr_description",
    }
)


class SubagentPrioritySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_candidates: list[SubagentModelPreference] = Field(default_factory=list, max_length=8)
    push_remote_branch: bool = False
    default_preset: Literal["cross_review_slim", "readonly_brief"] = "cross_review_slim"

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        raw_preset = cleaned.get("default_preset")
        if raw_preset == "readonly_brief":
            cleaned["default_preset"] = "readonly_brief"
            cleaned["push_remote_branch"] = False
        else:
            cleaned["default_preset"] = "cross_review_slim"
        for key in _LEGACY_DROPPED_SETTINGS_KEYS:
            cleaned.pop(key, None)
        return cleaned

    @model_validator(mode="after")
    def enforce_readonly_no_push(self) -> "SubagentPrioritySettings":
        if self.default_preset == "readonly_brief" and self.push_remote_branch:
            self.push_remote_branch = False
        return self


class SubagentPrioritySettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_candidates: list[SubagentModelPreference] | None = Field(default=None, max_length=8)
    push_remote_branch: bool | None = None
    default_preset: Literal["cross_review_slim", "readonly_brief"] | None = None


CrossReviewSettings = SubagentPrioritySettings
CrossReviewSettingsPatch = SubagentPrioritySettingsPatch

logger = logging.getLogger(__name__)
_lock = Lock()


def _settings_paths() -> tuple[Path, Path]:
    home = resolve_runtime_home()
    return home / "subagent_settings.json", home / "cross_review_settings.json"


def _read_subagent_settings_unlocked() -> SubagentPrioritySettings:
    primary, legacy = _settings_paths()
    for path in (primary, legacy):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            logger.warning("Could not read subagent settings %s; using defaults: %s", path, exc)
            return SubagentPrioritySettings()
        try:
            return SubagentPrioritySettings.model_validate(raw)
        except ValueError as exc:
            logger.warning("Invalid subagent settings %s; using defaults: %s", path, exc)
            return SubagentPrioritySettings()
    return SubagentPrioritySettings()


def read_subagent_settings() -> SubagentPrioritySettings:
    with _lock:
        return _read_subagent_settings_unlocked()


def _write_subagent_settings_unlocked(settings: SubagentPrioritySettings) -> None:
    primary, legacy = _settings_paths()
    payload = settings.model_dump(mode="json")
    primary.parent.mkdir(parents=True, exist_ok=True)
    for path in (primary, legacy):
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_subagent_settings(settings: SubagentPrioritySettings) -> None:
    with _lock:
        _write_subagent_settings_unlocked(settings)


def patch_subagent_settings(patch: SubagentPrioritySettingsPatch) -> SubagentPrioritySettings:
    """Atomically apply a narrow partial update without overwriting unrelated fields."""
    with _lock:
        current = _read_subagent_settings_unlocked()
        merged = current.model_dump(mode="python")
        if patch.preferred_candidates is not None:
            merged["preferred_candidates"] = [
                item.model_dump(mode="python") for item in patch.preferred_candidates
            ]
        if patch.default_preset is not None:
            merged["default_preset"] = patch.default_preset
            if patch.default_preset == "readonly_brief" and patch.push_remote_branch is None:
                merged["push_remote_branch"] = False
        if patch.push_remote_branch is not None:
            merged["push_remote_branch"] = patch.push_remote_branch
        updated = SubagentPrioritySettings.model_validate(merged)
        _write_subagent_settings_unlocked(updated)
        return updated


read_cross_review_settings = read_subagent_settings
write_cross_review_settings = write_subagent_settings
patch_cross_review_settings = patch_subagent_settings
