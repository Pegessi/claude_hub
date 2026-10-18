"""Compatibility façade for workspace report processing."""

from ._constants import *  # noqa: F401,F403
from .reports import (
    ReportCallIdConflict,
    _AutonomousReportMixin,
    _InternalReportHandlingMixin,
    _ReportAcknowledgementsMixin,
    _ReportEventsMixin,
    _ReportIdentityMixin,
    _ReportIntakeMixin,
    _ReportReviewRequestMixin,
)


class _ReportsMixin(_ReportIntakeMixin):
    """Compose report responsibilities in their historical method order."""


__all__ = ["ReportCallIdConflict", "_ReportsMixin"]
