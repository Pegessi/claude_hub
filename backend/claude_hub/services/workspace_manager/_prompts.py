"""Compatibility façade for workspace prompt builders."""

from ._constants import *  # noqa: F401,F403
from .prompts import (
    _AssignmentPromptsMixin,
    _BootstrapPromptsMixin,
    _RecoveryPromptsMixin,
    _ReviewPromptsMixin,
)


class _PromptsMixin(_RecoveryPromptsMixin):
    """Compose prompt responsibilities in their historical method order."""
