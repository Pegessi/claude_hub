"""Compatibility façade for scheduled-task orchestration."""

from ._constants import *  # noqa: F401,F403
from ._constants import SCHEDULED_TASKS_FILE
from .scheduling import (
    _ScheduledChatRunsMixin,
    _ScheduledHubTasksMixin,
    _ScheduleDispatcherMixin,
    _ScheduleRegistryMixin,
    _ScheduleStorageMixin,
)


class _SchedulingMixin(
    _ScheduleStorageMixin,
    _ScheduleRegistryMixin,
    _ScheduleDispatcherMixin,
    _ScheduledChatRunsMixin,
    _ScheduledHubTasksMixin,
):
    """Compose scheduling responsibilities in their historical method order."""
