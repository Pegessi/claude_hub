from __future__ import annotations

import importlib
import sys
import types
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "RemoteProfileManager": (".remote_profiles", "RemoteProfileManager"),
    "remote_profile_manager": (".remote_profiles", "remote_profile_manager"),
    "ConnectionManager": (".session_manager", "ConnectionManager"),
    "connection_manager": (".session_manager", "connection_manager"),
    "TTYDManager": (".ttyd_manager", "TTYDManager"),
    "TTYDProcess": (".ttyd_manager", "TTYDProcess"),
    "ttyd_manager": (".ttyd_manager", "ttyd_manager"),
    "WorkspaceManager": (".workspace_manager", "WorkspaceManager"),
    "workspace_manager": (".workspace_manager", "workspace_manager"),
}


class _ServicesPackage(types.ModuleType):
    """Preserve singleton package attributes when same-named submodules load."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _LAZY_EXPORTS and isinstance(value, types.ModuleType):
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ServicesPackage


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
