"""Runtime completeness checks for the WorkspaceManager mixin contract."""

from claude_hub.services.workspace_manager import WorkspaceManager


def test_workspace_manager_implements_every_abstract_host_member() -> None:
    assert WorkspaceManager.__abstractmethods__ == frozenset()
    WorkspaceManager()
