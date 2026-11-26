"""Persistent defaults for the existing Goal cross-review gate."""

import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.goal_run import CrossReviewModelPreference, StrictReviewMode
from .runtime_isolation import resolve_runtime_home


class CrossReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_preset: Literal["mr_slim", "bug_only", "readonly_brief"] = "mr_slim"
    review_mode: StrictReviewMode = "single"
    preferred_candidates: list[CrossReviewModelPreference] = Field(
        default_factory=list, max_length=8
    )
    include_review_briefing: bool = True
    generate_mr_description: bool = False
    push_remote_branch: bool = False

    @model_validator(mode="after")
    def validate_readonly(self) -> "CrossReviewSettings":
        if self.default_preset == "readonly_brief" and self.push_remote_branch:
            raise ValueError("Read-only audit preset cannot push branches")
        return self


logger = logging.getLogger(__name__)
_lock = Lock()


def read_cross_review_settings() -> CrossReviewSettings:
    path = resolve_runtime_home() / "cross_review_settings.json"
    try:
        return CrossReviewSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CrossReviewSettings()
    except (OSError, ValueError) as exc:
        logger.warning("Could not read cross-review settings; using defaults: %s", exc)
        return CrossReviewSettings()


def write_cross_review_settings(settings: CrossReviewSettings) -> None:
    path = resolve_runtime_home() / "cross_review_settings.json"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(settings.model_dump(mode="json"), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
