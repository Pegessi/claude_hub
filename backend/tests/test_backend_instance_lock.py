import logging
from logging.handlers import RotatingFileHandler
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


@pytest.mark.asyncio
async def test_rejected_backend_owner_does_not_rotate_active_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from claude_hub import main

    lock_path = tmp_path / "backend.lock"
    log_file = tmp_path / "backend.log"
    active_output = "active owner output\n"
    log_file.write_text(active_output, encoding="utf-8")
    monkeypatch.setattr(main, "backend_lock_file", lock_path)
    monkeypatch.setattr(main, "log_file", log_file)
    monkeypatch.setattr(main.settings, "backend_log_max_bytes", 8)
    monkeypatch.setattr(main.settings, "backend_log_backup_count", 2)

    assert not [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]

    with BackendInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="already owns Claude Hub state"):
            async with main.lifespan(main.app):
                pass

    assert log_file.read_text(encoding="utf-8") == active_output
    assert not (tmp_path / "backend.log.1").exists()
