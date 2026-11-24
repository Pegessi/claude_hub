"""Normalized user-identity messages; resource keys remain bound to their source message."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeishuUserMessage:
    """Normalized message record returned by user-identity IM read APIs."""

    message_id: str
    chat_id: str
    sender_open_id: str
    sender_type: str
    msg_type: str
    text: str
    create_time_ms: int
    thread_id: str | None = None
    parent_id: str | None = None
    root_id: str | None = None
    has_images: bool = False
    image_refs: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "sender_open_id": self.sender_open_id,
            "sender_type": self.sender_type,
            "msg_type": self.msg_type,
            "text": self.text,
            "create_time_ms": self.create_time_ms,
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "root_id": self.root_id,
            "has_images": self.has_images,
            "image_refs": [list(ref) for ref in self.image_refs],
        }
