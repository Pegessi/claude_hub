"""Attachment ownership failures shared by cache consumers."""


class AttachmentOwnerUnavailable(RuntimeError):
    """The cache owner was deleted or replaced before an atomic operation."""
