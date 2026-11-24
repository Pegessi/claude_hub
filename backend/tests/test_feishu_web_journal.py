"""Web pushes are recoverable across a process restart before ACK."""

from pathlib import Path

from claude_hub.services.feishu_web_journal import WebPushJournal
from claude_hub.services.feishu_web_session.client import PushedMessage
from claude_hub.services.feishu_web_session.rich import WebMessage


def test_web_push_journal_is_durable_idempotent_and_ordered(tmp_path: Path) -> None:
    path = tmp_path / "pending.sqlite3"
    first = PushedMessage(
        WebMessage(id="7400000000000000001", chat_id="7300000000000000001", content=b"payload"),
        chat_type=2,
        at_me=True,
    )
    second = PushedMessage(
        WebMessage(id="7400000000000000002", chat_id="7300000000000000001"), chat_type=2
    )
    journal = WebPushJournal(path)
    journal.add(first)
    journal.add(first)
    journal.add(second)
    resumed = WebPushJournal(path)
    assert resumed.pending() == [first, second]
    resumed.remove(first.message.id)
    assert resumed.pending() == [second]
    assert path.stat().st_mode & 0o077 == 0


def test_journal_rejects_non_web_identity(tmp_path: Path) -> None:
    import pytest

    journal = WebPushJournal(tmp_path / "pending.sqlite3")
    with pytest.raises(ValueError, match="numeric"):
        journal.add(PushedMessage(WebMessage(id="om_invalid")))
