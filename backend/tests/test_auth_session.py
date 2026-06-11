"""Tests for the file-backed login session store (``claude_hub.auth.session``).

Sessions persist to a JSON file under the user's home directory. Each test
redirects ``SESSIONS_FILE`` to a ``tmp_path`` so the real store is never touched
and expiry/cleanup branches can be driven deterministically.
"""

from datetime import datetime, timedelta

import pytest
from pytest import MonkeyPatch

from claude_hub.auth import session as session_store
from claude_hub.models.schemas import User


@pytest.fixture(autouse=True)
def _isolated_sessions_file(monkeypatch: MonkeyPatch, tmp_path) -> None:
    """Point the session store at a throwaway file for every test."""
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")


def _user(open_id: str = "ou_1", email: str = "u@example.com") -> User:
    return User(open_id=open_id, name="User", email=email)


def test_create_and_get_session_round_trip() -> None:
    created = session_store.create_session(_user(), "access", "refresh")

    fetched = session_store.get_session(created.session_id)

    assert fetched is not None
    assert fetched.session_id == created.session_id
    assert fetched.user.open_id == "ou_1"
    assert fetched.feishu_access_token == "access"
    assert fetched.feishu_refresh_token == "refresh"


def test_get_unknown_session_returns_none() -> None:
    assert session_store.get_session("does-not-exist") is None


def test_get_session_deletes_and_returns_none_when_expired(monkeypatch: MonkeyPatch) -> None:
    created = session_store.create_session(_user(), "access", "refresh")

    # Force the stored session to look expired.
    sessions = session_store._load_sessions()
    sessions[created.session_id]["expires_at"] = (datetime.now() - timedelta(days=1)).isoformat()
    session_store._save_sessions(sessions)

    assert session_store.get_session(created.session_id) is None
    # Expired session is purged from the store on access.
    assert created.session_id not in session_store._load_sessions()


def test_delete_session_removes_entry() -> None:
    created = session_store.create_session(_user(), "access", "refresh")

    session_store.delete_session(created.session_id)

    assert session_store.get_session(created.session_id) is None


def test_delete_unknown_session_is_noop() -> None:
    # Should not raise even when the id was never stored.
    session_store.delete_session("missing")


def test_cleanup_expired_sessions_removes_only_expired() -> None:
    active = session_store.create_session(_user("ou_active"), "a", "r")
    expired = session_store.create_session(_user("ou_expired"), "a", "r")

    sessions = session_store._load_sessions()
    sessions[expired.session_id]["expires_at"] = (datetime.now() - timedelta(days=1)).isoformat()
    session_store._save_sessions(sessions)

    removed = session_store.cleanup_expired_sessions()

    assert removed == 1
    remaining = session_store._load_sessions()
    assert active.session_id in remaining
    assert expired.session_id not in remaining


def test_cleanup_drops_malformed_session_entries() -> None:
    good = session_store.create_session(_user("ou_good"), "a", "r")

    sessions = session_store._load_sessions()
    # Entry missing the expires_at field is treated as corrupt and dropped.
    sessions["corrupt"] = {"session_id": "corrupt"}
    session_store._save_sessions(sessions)

    removed = session_store.cleanup_expired_sessions()

    assert removed == 1
    remaining = session_store._load_sessions()
    assert good.session_id in remaining
    assert "corrupt" not in remaining


def test_cleanup_with_no_sessions_returns_zero() -> None:
    assert session_store.cleanup_expired_sessions() == 0


def test_load_sessions_tolerates_corrupt_file() -> None:
    session_store.SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    session_store.SESSIONS_FILE.write_text("{ not valid json")

    # A corrupt store degrades to empty rather than raising.
    assert session_store._load_sessions() == {}
