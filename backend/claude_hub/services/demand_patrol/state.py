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
        state = PatrolState.model_validate(payload)
        state.bound_history()
        return state

    def save(self, state: PatrolState) -> None:
        state.bound_history()
        write_document(self.path, state.model_dump(mode="json"))
