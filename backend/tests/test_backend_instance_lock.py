from pathlib import Path

import pytest

from claude_hub.services.backend_instance_lock import BackendInstanceLock


def test_backend_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "backend.lock"

    with BackendInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="already owns Claude Hub state"):
            with BackendInstanceLock(lock_path):
                pass


def test_backend_instance_lock_is_released_for_next_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "backend.lock"

    with BackendInstanceLock(lock_path):
        pass

    with BackendInstanceLock(lock_path):
        pass
