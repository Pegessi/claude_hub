"""Bounded contract tests for Task Graph docs and Resident prompt."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from claude_hub.services.task_graph import (
    LEGACY_RESIDENT_CONSUMER_TEMPLATE,
    legacy_resident_consumer_key,
)
from claude_hub.services.workspace_manager._workspaces import (
    _build_resident_master_prompt,
    _build_task_graph_block,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "TASK_GRAPH.md"
ROOT_README = REPO_ROOT / "README.md"
BACKEND_README = REPO_ROOT / "backend" / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_FORBIDDEN_README_STALE_AGENT_TREE_PRIMARY = (
    "Use the backend Agent Tree",
    "Resident or worker can spawn",
    "Resident or worker",
    "can spawn managed Claude",
    "claude-hub agent-tree` or",
    "agent-facing Agent Tree / durable",
    "`claude-hub agent-tree` CLI (backend-only",
    "Agents that need to spawn children, wait on directed events, ACK",
    "Agents that need to spawn children",
    "Public spawn is `managed_task`",
    "There is no Agent Tree UI yet",
)

_FORBIDDEN_REMOVED_LEGACY_SURFACE = (
    "/api/agent-tree",
    "claude-hub agent-tree",
    "compat projection",
    "AgentRun",
    "agent_run_id",
    "resident_root",
    "Deprecated AgentRun",
    "## Agent Tree REST",
)

_TASK_CLI_SUBCOMMANDS = ("tree", "events", "wait", "ack", "followup", "start")


def _assert_readme_covers_task_cli_subcommands(text: str) -> None:
    """Primary task CLI subcommands: slash list or each subcommand named."""
    if "tree/events/wait/ack/followup/start" in text:
        return
    lowered = text.lower()
    missing = [sub for sub in _TASK_CLI_SUBCOMMANDS if sub not in lowered]
    assert not missing, f"missing task subcommands: {missing}"


def _assert_no_stale_agent_tree_primary_wording(text: str) -> None:
    lowered = text.lower()
    for forbidden in _FORBIDDEN_README_STALE_AGENT_TREE_PRIMARY:
        assert forbidden.lower() not in lowered, forbidden


def _assert_no_removed_legacy_surface(text: str, *, allow_removed_history: bool = False) -> None:
    """Canonical docs must not advertise deleted Agent Tree / AgentRun APIs."""
    scan = text
    if allow_removed_history and "## Migration / removed history" in text:
        scan = text.split("## Migration / removed history", 1)[0]
    lowered = scan.lower()
    for forbidden in _FORBIDDEN_REMOVED_LEGACY_SURFACE:
        assert forbidden.lower() not in lowered, forbidden
    assert "agent-tree" not in lowered
    assert "agent_tree" not in lowered


def _assert_shared_task_first_floor(text: str) -> None:
    assert "Task Graph" in text
    assert "TaskMailbox" in text
    assert "claude-hub task" in text


def _assert_root_readme_task_first(text: str) -> None:
    assert "docs/TASK_GRAPH.md" in text
    assert "task:<task_id>" in text
    _assert_readme_covers_task_cli_subcommands(text)
    assert "Task session assignments" in text or "Task session assignment" in text
    assert "independent long-running agent" in text.lower()
    assert "not a mailbox consumer" in text.lower()
    assert text.count("claude-hub agent-tree spawn") == 0
    assert "uv run claude-hub --json task tree" in text
    assert "uv run claude-hub --json task events" in text
    assert "uv run claude-hub --json task wait" in text
    assert "uv run claude-hub --json task ack" in text
    assert "uv run claude-hub --json task followup" in text
    assert "claude-hub task` primary" in text
    _assert_no_removed_legacy_surface(text)


def _assert_backend_readme_task_first(text: str) -> None:
    assert "../docs/TASK_GRAPH.md" in text
    assert "## Task Graph / TaskMailbox (primary)" in text
    _assert_readme_covers_task_cli_subcommands(text)
    assert "/api/workspaces/{id}/tasks/*" in text
    assert "Task session assignments" in text
    assert "independent long-running agent" in text.lower()
    assert "not a mailbox consumer" in text.lower()
    assert "New work must use Task Graph APIs" in text
    assert "explicit Task assignment" in text
    _assert_no_removed_legacy_surface(text)


_FORBIDDEN_DUAL_CONTROL_PHRASES = (
    "do not treat Task status as a substitute for run events",
    "do not treat Task board status",
    "Agent Tree itself is the run tree",
    "without creating a new Hub Task by hand",
    "Tasks and sessions are how",
    "managed_task executes. Agent Tree",
    "independent tree",
    "dual control",
    "durable mailbox (agent use)",
    "resident:<workspace",
    "resident:<workspace_id>",
)

_FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE = (
    "/resident/events",
    "/resident/wait",
    "/resident/ack",
    "Resident consumer",
    "resident_root",
    "RESIDENT_ROOT",
    "Created by Hub for the Resident",
)


def _assert_master_prompt_uses_task_rest_only(prompt: str) -> None:
    """Resident master prompt orchestrates via Task REST only."""
    for forbidden in (
        "/api/agent-tree",
        "resident_root",
        "AgentRun",
        "root run",
        "run_id",
        "omit executor_config",
        "session_id is mandatory",
    ):
        assert forbidden not in prompt, forbidden
    for required in (
        "target_session_id",
        "/tasks/",
        "/start",
        "/events",
        "/wait",
        "/ack",
        "/followup",
        "/continue",
        '"status":"done"',
    ):
        assert required in prompt, required


def test_working_log_banner_marks_agent_tree_historical_only() -> None:
    path = REPO_ROOT / "docs" / "working-logs" / "2026-08-16-agent-tree-durable-mailbox.md"
    text = path.read_text(encoding="utf-8")
    banner = text.split("\n", 10)[0:8]
    banner_text = "\n".join(banner)
    assert "REMOVED" in banner_text
    assert "HISTORICAL ONLY" in banner_text
    assert "../TASK_GRAPH.md" in banner_text
    assert "[`docs/AGENT_TREE.md`]" not in banner_text
    assert "Current public boundary" not in banner_text


def test_changelog_documents_legacy_consumer_migration_only() -> None:
    """Deprecated resident consumer key is changelog/migration-only, not operational."""
    guide = GUIDE.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    template = LEGACY_RESIDENT_CONSUMER_TEMPLATE
    assert template == "workspace:{workspace_id}:resident"
    assert template in changelog
    assert "migration-only" in changelog.lower() or "deprecated" in changelog.lower()
    operational = guide.split("## Migration / removed history", 1)[0]
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in operational, forbidden
    for forbidden in ("resident:<workspace>", "resident:<workspace_id>"):
        assert forbidden not in operational, forbidden
        assert forbidden not in changelog, forbidden


def test_guide_single_task_graph_model_no_resident_mailbox() -> None:
    """Canonical guide: Task Graph + ordinary sessions; no Resident mailbox orchestration."""
    text = GUIDE.read_text(encoding="utf-8")
    operational = text.split("## Migration / removed history", 1)[0]
    for needle in (
        "Task Graph (canonical)",
        "Session assignment",
        "Optional Resident agent",
        "task:<task_id>",
        "target_session_id",
        "consumer_ack_sequence",
        "parent_task_id",
    ):
        assert needle in operational, needle
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in operational, forbidden
    _assert_no_removed_legacy_surface(text, allow_removed_history=True)
    assert "resident_ack_sequence" not in text
    assert "follow-up `487c630c`" not in text
    assert legacy_resident_consumer_key("ws-1") not in operational
    assert "487c630c" in text  # cancelled legacy plan note lives in Migration section


def test_guide_migration_section_labels_removed_history() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    migration = text.split("## Migration / removed history", 1)[1]
    assert "removed from runtime" in migration.lower()
    assert "2026-08-16-agent-tree-durable-mailbox.md" in migration
    assert "not current API" in migration.lower() or "not** current API" in migration


def test_task_graph_guide_covers_required_agent_contract() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    operational = text.split("## Migration / removed history", 1)[0]
    assert 180 <= operational.count("\n") + 1 <= 280
    for needle in (
        "## Mental model",
        "Task Graph (canonical)",
        "Session assignment",
        "claude-hub task",
        "/api/workspaces/WS_ID/tasks/tree",
        "/tasks/TASK_ID/abort",
        "TaskMailbox `ABORT`",
        "manual control",
        "parent_task_id",
        "TaskMailbox",
        "consumer_ack_sequence",
        "/events?since_sequence=0",
        "target_session_id",
        "retry-uncertain",
    ):
        assert needle in operational, needle
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden not in operational, forbidden
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in operational, forbidden
    _assert_no_removed_legacy_surface(text, allow_removed_history=True)


def test_task_graph_guide_forbids_dual_control_plane_wording() -> None:
    """Lock Task Graph as sole orchestration model in entry docs."""
    text = GUIDE.read_text(encoding="utf-8")
    operational = text.split("## Migration / removed history", 1)[0]
    lowered = operational.lower()
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden.lower() not in lowered, forbidden
    assert "claude-hub task" in lowered
    assert "task graph (canonical)" in lowered or "task graph (primary)" in lowered
    assert "task graph / taskmailbox" in agents.lower()
    assert "primary: `claude-hub task`" in agents
    assert "docs/TASK_GRAPH.md" in agents
    assert "claude-hub agent-tree" not in agents.lower()
    assert agents == claude


def test_task_graph_primary_contract_matches_source() -> None:
    """Task Graph primary semantics: keys, assignment, abort."""
    text = GUIDE.read_text(encoding="utf-8")
    operational = text.split("## Migration / removed history", 1)[0]
    for needle in (
        "## Task Graph REST (primary)",
        "parent_task_id",
        "Session assignment",
        "claude-hub --json task abort",
        "TaskMailbox `ABORT`",
        "related_task_id",
        "target_session_id",
    ):
        assert needle in operational, needle
    assert "follow-up `487c630c`" not in text
    assert "follow-up: task `487c630c" not in text
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden not in operational, forbidden
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in operational, forbidden
    _assert_no_removed_legacy_surface(text, allow_removed_history=True)


def test_readmes_task_graph_primary_not_stale_agent_tree() -> None:
    """README navigation: Task Graph primary; no resurrected agent-tree surface."""
    root_readme = ROOT_README.read_text(encoding="utf-8")
    backend_readme = BACKEND_README.read_text(encoding="utf-8")
    for readme in (root_readme, backend_readme):
        _assert_no_stale_agent_tree_primary_wording(readme)
        _assert_shared_task_first_floor(readme)
    _assert_root_readme_task_first(root_readme)
    _assert_backend_readme_task_first(backend_readme)


def test_readmes_and_agent_entry_link_the_guide() -> None:
    root_readme = ROOT_README.read_text(encoding="utf-8")
    backend_readme = BACKEND_README.read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "docs/TASK_GRAPH.md" in root_readme
    assert "../docs/TASK_GRAPH.md" in backend_readme
    assert "Task Graph / TaskMailbox" in root_readme
    assert "claude-hub task` primary" in root_readme
    assert "docs/TASK_GRAPH.md" in agents
    assert "Task Graph / TaskMailbox" in agents
    assert "primary: `claude-hub task`" in agents
    assert agents == claude


def test_resident_task_graph_block_teaches_current_boundaries() -> None:
    block = _build_task_graph_block("http://localhost:8173", "ws-1")
    for needle in (
        "## Task Graph",
        "Orchestrate via workspace Tasks only",
        "explicit TASK_ID",
        "/api/workspaces/ws-1/tasks/tree",
        "/api/workspaces/ws-1/tasks",
        "target_session_id",
        "since_sequence",
        '"origin":"resident"',
    ):
        assert needle in block, needle
    assert "root-run" not in block
    assert "/api/agent-tree/spawn" not in block


def test_pinned_session_and_status_codes_are_source_accurate() -> None:
    """Master prompt uses Task start/target_session_id; guide documents reports."""
    text = GUIDE.read_text(encoding="utf-8")
    operational = text.split("## Migration / removed history", 1)[0]
    for needle in (
        "HTTP 409",
        "call_id",
        "retry-uncertain",
    ):
        assert needle in operational, needle

    block = _build_task_graph_block("http://localhost:8173", "ws-1")
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        task_graph_block=block,
    )
    for blob in (block, master):
        assert "Task Graph" in blob
        assert '"origin":"resident"' in blob
    _assert_master_prompt_uses_task_rest_only(master)


def test_negative_422_is_unavailable_executor_not_adapter_rejection() -> None:
    block = _build_task_graph_block("http://localhost:8173", "ws-1")
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        task_graph_block=block,
    )
    assert "/api/agent-tree/spawn" not in master
    _assert_master_prompt_uses_task_rest_only(master)


_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.S)


def test_guide_bash_examples_use_task_graph_rest_only() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    operational = text.split("## Migration / removed history", 1)[0]
    for block in _BASH_BLOCK.findall(operational):
        assert "/api/agent-tree" not in block
        assert "agent-tree" not in block.lower()
