"""Original provider image and optional bounded display preview."""

from pydantic import BaseModel


class ChatImageAttachment(BaseModel):
    filename: str
    mime_type: str
    data_url: str
    preview_data_url: str | None = None
    preview_is_original: bool = False
