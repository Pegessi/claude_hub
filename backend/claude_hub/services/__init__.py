import sys
from pathlib import Path

if (
    "claude_hub.services.storage_maintenance" not in getattr(sys, "orig_argv", ())
    and Path(sys.argv[0] if sys.argv else "").name != "storage_maintenance.py"
):
    from .remote_profiles import RemoteProfileManager, remote_profile_manager
    from .session_manager import ConnectionManager, connection_manager
    from .ttyd_manager import TTYDManager, TTYDProcess, ttyd_manager
    from .workspace_manager import WorkspaceManager, workspace_manager

    __all__ = [
        "TTYDManager",
        "TTYDProcess",
        "ttyd_manager",
        "ConnectionManager",
        "connection_manager",
        "RemoteProfileManager",
        "remote_profile_manager",
        "WorkspaceManager",
        "workspace_manager",
    ]
