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


def test_journal_pages_past_retained_entries(tmp_path: Path) -> None:
    journal = WebPushJournal(tmp_path / "pending.sqlite3")
    messages = [PushedMessage(WebMessage(id=str(7400000000000010000 + i))) for i in range(257)]
    for message in messages:
        journal.add(message)

    first = journal.pending_page()
    assert [message for _, message in first] == messages[:256]
    journal.remove(messages[1].message.id)
    tail = journal.pending_page(first[-1][0])
    assert [message for _, message in tail] == messages[256:]
    assert journal.pending_page(tail[-1][0]) == []
    assert journal.pending()[0] == messages[0]
