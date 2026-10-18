"""Compatibility façade for task management mixins."""

from ._constants import *  # noqa: F401,F403
from .tasking import (
    _TaskAbortMixin,
    _TaskCoreMixin,
    _TaskFeedbackSummaryMixin,
    _TaskFollowupMixin,
)


class _TasksMixin(_TaskAbortMixin):
    """Compose task responsibilities in their historical method order."""
