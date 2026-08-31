"""Task attachment persistence and prompt blocks."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _AttachmentsMixin:
    def _persist_attachments(
        self,
        workspace_id: str,
        owner_id: str,
        attachments: list[WorkspaceAttachmentCreate],
    ) -> list[WorkspaceAttachment]:
        persisted: list[WorkspaceAttachment] = []
        if not attachments:
            return persisted

        owner_dir = self._workspace_attachments_dir(workspace_id) / owner_id
        owner_dir.mkdir(parents=True, exist_ok=True)
        for item in attachments:
            mime_type = item.mime_type.strip().lower()
            suffix = IMAGE_ATTACHMENT_TYPES.get(mime_type)
            if not suffix:
                raise ValueError(f"Unsupported attachment type: {item.mime_type}")
            header = f"data:{mime_type};base64,"
            if not item.data_url.startswith(header):
                raise ValueError("Attachment data must be a matching base64 data URL")
            try:
                content = base64.b64decode(item.data_url[len(header) :], validate=True)
            except binascii.Error as exc:
                raise ValueError("Invalid attachment data") from exc
            if not content:
                raise ValueError("Attachment data is empty")
            if len(content) > ATTACHMENT_MAX_BYTES:
                raise ValueError("Attachment exceeds the 8 MB limit")

            attachment_id = uuid.uuid4().hex
            filename = _safe_attachment_filename(item.filename, suffix)
            path = owner_dir / f"{attachment_id}-{filename}"
            path.write_bytes(content)
            persisted.append(
                WorkspaceAttachment(
                    id=attachment_id,
                    filename=filename,
                    mime_type=mime_type,
                    path=str(path),
                    size_bytes=len(content),
                )
            )
        return persisted

    def _attachment_prompt_block(self, attachments: list[WorkspaceAttachment]) -> str:
        if not attachments:
            return ""
        lines = [
            "Attachments:",
            (
                "Image handling: these images are part of the user's message, not "
                "mere file references. Before answering a request that depends on an "
                "image, inspect it from the local path with your native image-viewing "
                "capability."
            ),
        ]
        for attachment in attachments:
            lines.append(
                f"- {attachment.filename} ({attachment.mime_type}, {attachment.size_bytes} bytes): "
                f"{attachment.path}"
            )
        return "\n".join(lines)

    def _append_attachment_block(self, message: str, attachments: list[WorkspaceAttachment]) -> str:
        block = self._attachment_prompt_block(attachments)
        if not block:
            return message
        if not message.strip():
            return block
        return f"{message.rstrip()}\n\n{block}"

    def get_attachment(self, attachment_id: str) -> WorkspaceAttachment:
        for task in self.tasks.values():
            for attachment in task.attachments:
                if attachment.id == attachment_id:
                    return attachment
        raise KeyError(attachment_id)
