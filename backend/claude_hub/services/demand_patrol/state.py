"""Patrol document storage; the backend instance owns serialization."""

from pathlib import Path

from ..local_storage import read_document, write_document
from ..runtime_isolation import resolve_runtime_home
from .models import PatrolState


class PatrolStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or resolve_runtime_home() / "demand_patrol_state.json"

    def load(self) -> PatrolState:
        try:
            payload = read_document(self.path)
        except FileNotFoundError:
            return PatrolState()
        # Corrupt state fails closed, rather than losing deduplication evidence.
        # Old scheduled Feishu polling settings are accepted only on disk. API
        # writes still reject them so removed controls cannot silently succeed.
        payload = dict(payload)
        config = dict(payload.get("config", {}))
        for field in (
            "feishu_enabled",
            "feishu_interval_minutes",
            "feishu_window_start",
            "feishu_window_end",
            "max_triage_per_run",
        ):
            config.pop(field, None)
        payload["config"] = config
        payload["recent_runs"] = [
            record for record in payload.get("recent_runs", []) if record.get("channel") != "feishu"
        ]
        state = PatrolState.model_validate(payload)
        state.bound_history()
        return state

    def save(self, state: PatrolState) -> None:
        state.bound_history()
        write_document(self.path, state.model_dump(mode="json"))
