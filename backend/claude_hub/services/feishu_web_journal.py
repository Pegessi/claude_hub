"""Durable local inbox for WebSocket frames before protocol acknowledgement."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from claude_hub.services.feishu_web_session.client import PushedMessage, is_web_entity_id
from claude_hub.services.feishu_web_session.rich import WebMessage


class WebPushJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=15)
        db.execute(
            "CREATE TABLE IF NOT EXISTS pending "
            "(seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)"
        )
        return db

    def add(self, pushed: PushedMessage) -> None:
        message = pushed.message
        if not is_web_entity_id(message.id):
            raise ValueError("Web push has no numeric message identity")
        value = asdict(message)
        value["content"] = base64.b64encode(message.content).decode("ascii")
        payload = json.dumps(
            {
                "message": value,
                "chat_type": pushed.chat_type,
                "at_me": pushed.at_me,
                "removed": pushed.removed,
            },
            ensure_ascii=False,
        )
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO pending(message_id, payload) VALUES (?, ?)",
                (message.id, payload),
            )

    def pending(self) -> list[PushedMessage]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload FROM pending ORDER BY seq LIMIT 256").fetchall()
        result = []
        for (payload,) in rows:
            value = json.loads(payload)
            message = value["message"]
            message["content"] = base64.b64decode(message["content"], validate=True)
            message["at_user_ids"] = tuple(message.get("at_user_ids", ()))
            result.append(
                PushedMessage(
                    message=WebMessage(**message),
                    chat_type=value["chat_type"],
                    at_me=value["at_me"],
                    removed=value["removed"],
                )
            )
        return result

    def remove(self, message_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM pending WHERE message_id = ?", (message_id,))
