"""Bounded contract tests for the Agent Tree agent guide and Resident prompt."""

from __future__ import annotations

import json
import re
from collections import defaultdict
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

_TASK_CLI_SUBCOMMANDS = ("tree", "events", "wait", "ack", "followup", "start")


def _assert_readme_covers_task_cli_subcommands(text: str) -> None:
    """Primary task CLI subcommands: slash list or each subcommand named."""
    if "tree/events/wait/ack/followup/start" in text:
        return
    lowered = text.lower()
    missing = [sub for sub in _TASK_CLI_SUBCOMMANDS if sub not in lowered]
    assert not missing, f"missing task subcommands: {missing}"


def _assert_legacy_compat_projection_only(text: str) -> None:
    lowered = text.lower()
    assert "legacy compat" in lowered, text
    assert "projection" in lowered or "compat only" in lowered, text


def _assert_no_stale_agent_tree_primary_wording(text: str) -> None:
    lowered = text.lower()
    for forbidden in _FORBIDDEN_README_STALE_AGENT_TREE_PRIMARY:
        assert forbidden.lower() not in lowered, forbidden


def _assert_shared_task_first_floor(text: str) -> None:
    assert "Task Graph" in text
    assert "TaskMailbox" in text
    assert "claude-hub task" in text
    _assert_legacy_compat_projection_only(text)


def _assert_root_readme_task_first(text: str) -> None:
    assert "docs/TASK_GRAPH.md" in text
    assert "task:<task_id>" in text
    _assert_readme_covers_task_cli_subcommands(text)
    assert "Task session assignments" in text
    assert "independent long-running agent" in text.lower()
    assert "not a mailbox consumer" in text.lower()
    assert text.count("claude-hub agent-tree spawn") == 0
    assert "uv run claude-hub --json task tree" in text
    assert "uv run claude-hub --json task events" in text
    assert "uv run claude-hub --json task wait" in text
    assert "uv run claude-hub --json task ack" in text
    assert "uv run claude-hub --json task followup" in text
    assert "claude-hub task` primary" in text


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
    lowered = text.lower()
    assert "agent-tree" not in lowered
    assert "agent_tree" not in lowered
    assert "/api/agent-tree" not in text
    assert "agentrun" not in lowered.replace(" ", "")


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

# Operational guide must not teach legacy Resident mailbox / run-tree orchestration.
_FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE = (
    "/resident/events",
    "/resident/wait",
    "/resident/ack",
    "Resident consumer",
    "resident_root",
    "RESIDENT_ROOT",
    "Created by Hub for the Resident",
)

# A copy-paste spawn JSON that sends both keys can invent a Claude config
# for a Codex/Cursor worker. Recipes must split derive vs Hub-pick.
_BOTH_KEYS_IN_OBJECT = re.compile(
    r'\{[^{}]*"executor_config"\s*:\s*\{[^}]*\}[^{}]*"session_id"|'
    r'\{[^{}]*"session_id"[^{}]*"executor_config"\s*:'
)


def _assert_spawn_recipes_do_not_pair_generic_config_with_session(text: str) -> None:
    compact = re.sub(r"\s+", "", text)
    assert _BOTH_KEYS_IN_OBJECT.search(compact) is None, text


def _assert_master_prompt_uses_task_rest_only(prompt: str) -> None:
    """Resident master prompt orchestrates via Task REST, not legacy run-tree APIs."""
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


def test_changelog_documents_legacy_consumer_migration_only() -> None:
    """Deprecated resident consumer key is changelog/migration-only, not operational."""
    guide = GUIDE.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    template = LEGACY_RESIDENT_CONSUMER_TEMPLATE
    assert template == "workspace:{workspace_id}:resident"
    assert template in changelog
    assert "migration-only" in changelog.lower() or "deprecated" in changelog.lower()
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in guide, forbidden
    for forbidden in ("resident:<workspace>", "resident:<workspace_id>"):
        assert forbidden not in guide, forbidden
        assert forbidden not in changelog, forbidden


def test_guide_single_task_graph_model_no_resident_mailbox() -> None:
    """Canonical guide: Task Graph + ordinary sessions; no Resident mailbox orchestration."""
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (
        "Task Graph (canonical)",
        "Session assignment",
        "Optional Resident agent",
        "task:<task_id>",
        "target_session_id",
        "consumer_ack_sequence",
        "parent_task_id",
        "compat projection only",
    ):
        assert needle in text, needle
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in text, forbidden
    assert "resident_ack_sequence" not in text
    assert "follow-up `487c630c`" not in text
    assert legacy_resident_consumer_key("ws-1") not in text.split("Migration")[0]
    assert "487c630c" in text  # cancelled legacy plan note lives in Migration/UI boundary


def test_model_and_docs_reject_resident_root_supervisor_wording() -> None:
    """Lock Task Graph canonical wording; RESIDENT_ROOT is legacy load-only only."""
    text = GUIDE.read_text(encoding="utf-8")
    assert "## Deprecated AgentRun compatibility spawn" in text
    assert "## Spawn\n" not in text
    for needle in (
        "New work must use Task Graph APIs",
        "POST /api/workspaces/WS_ID/tasks",
        "Do not treat AgentRun spawn as canonical",
    ):
        assert needle in text, needle


def test_task_graph_guide_covers_required_agent_contract() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert 320 <= text.count("\n") + 1 <= 420
    for needle in (
        "## Mental model",
        "Task Graph (canonical)",
        "Session assignment",
        "compat projection",
        "compat projection only",
        "claude-hub task",
        "/api/workspaces/WS_ID/tasks/tree",
        "/tasks/TASK_ID/abort",
        "TaskMailbox `ABORT`",
        "manual control",
        "parent_task_id",
        "TaskMailbox",
        "consumer_ack_sequence",
        "pre-migration",
        "related_task_id",
        "## Runtime boundary",
        "/api/agent-tree/runs",
        "/api/agent-tree/spawn",
        "Deprecated AgentRun compatibility spawn",
        "New work must use Task Graph APIs",
        "/api/agent-tree/wait",
        "/api/agent-tree/ack",
        "/api/agent-tree/followup",
        "/api/agent-tree/interrupt",
        "/events?since_sequence=0",
        '"agent_type":"claude"',
        '"agent_type":"codex"',
        '"agent_type":"cursor"',
        "Cursor MUST omit executor_config.model",
        "explicit model override is rejected",
        "omit `executor_config`",
        "Config-driven spawn",
        "Explicit-session routing",
        "config_from_session",
        "validate_session",
        "HTTP 400",
        "HTTP 409",
        "HTTP 422",
        "retry-uncertain",
        "[CANCELLED]",
        "2026-08-16-agent-tree-durable-mailbox.md",
        "native_subagent",
        "external_job",
        "target_session_id",
        "load-only",
    ):
        assert needle in text, needle
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden not in text, forbidden
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in text, forbidden
    assert "managed kind the adapter rejects" not in text
    assert "Invalid `managed_task` config" in text
    assert "not 422" in text
    _assert_spawn_recipes_do_not_pair_generic_config_with_session(text)


def test_task_graph_guide_forbids_dual_control_plane_wording() -> None:
    """Lock Task Graph as canonical; agent-tree orchestration removed from entry docs."""
    text = GUIDE.read_text(encoding="utf-8")
    lowered = text.lower()
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden.lower() not in lowered, forbidden
    assert "claude-hub task" in lowered
    assert "compat projection" in lowered
    assert "task graph (primary)" in lowered or "task graph (canonical)" in lowered
    assert "task graph / taskmailbox" in agents.lower()
    assert "primary: `claude-hub task`" in agents
    assert "docs/TASK_GRAPH.md" in agents
    assert "claude-hub agent-tree" not in agents.lower()
    assert agents == claude


def test_task_graph_primary_contract_matches_source() -> None:
    """Task Graph primary semantics: keys, assignment, abort, compat-only agent-tree."""
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (
        "## Task Graph REST (primary)",
        "Task Graph (primary)",
        "parent_task_id",
        "Session assignment",
        "compat projection only",
        "claude-hub --json task abort",
        "TaskMailbox `ABORT`",
        "session/context reuse",
        "target_session_id",
    ):
        assert needle in text, needle
    assert "follow-up `487c630c`" not in text
    assert "follow-up: task `487c630c" not in text
    assert "## Agent Tree REST (compat projection)" in text
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden not in text, forbidden
    for forbidden in _FORBIDDEN_RESIDENT_ORCHESTRATION_IN_GUIDE:
        assert forbidden not in text, forbidden


def test_readmes_task_graph_primary_not_stale_agent_tree() -> None:
    """README navigation: Task Graph primary; backend README must not resurrect agent-tree."""
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    backend_readme = (REPO_ROOT / "backend" / "README.md").read_text(encoding="utf-8")
    for readme in (root_readme, backend_readme):
        _assert_no_stale_agent_tree_primary_wording(readme)
    _assert_shared_task_first_floor(root_readme)
    _assert_backend_readme_task_first(backend_readme)
    _assert_root_readme_task_first(root_readme)


def test_readmes_and_agent_entry_link_the_guide() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    backend_readme = (REPO_ROOT / "backend" / "README.md").read_text(encoding="utf-8")
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
    """Lock compat spawn docs; master prompt uses Task start/target_session_id."""
    text = GUIDE.read_text(encoding="utf-8")
    for needle in (
        "omit `executor_config`",
        "config_from_session",
        "validate_session",
        "Invalid `managed_task` config",
        "not 422",
        "Unavailable executor only",
    ):
        assert needle in text, needle
    assert "managed kind the adapter rejects" not in text
    _assert_spawn_recipes_do_not_pair_generic_config_with_session(text)

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
        _assert_spawn_recipes_do_not_pair_generic_config_with_session(blob)
    assert "HTTP 400, not 422" not in block
    _assert_master_prompt_uses_task_rest_only(master)


def test_negative_cursor_model_is_omitted_from_examples() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", text)
    assert '"agent_type":"cursor","model"' not in compact
    cursor_example = re.search(r"```json\n(\{\"agent_type\":\"cursor\".*?\})\n```", text)
    assert cursor_example is not None, text
    assert "model" not in cursor_example.group(1)
    block = _build_task_graph_block("http://localhost:8173", "ws-1")
    assert "Cursor MUST omit executor_config.model" not in block
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        task_graph_block=block,
    )
    assert "Cursor MUST omit executor_config.model" not in master


def test_negative_explicit_session_does_not_hardcode_claude_config() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "OPTIONAL_WORKER_SESSION_ID" not in text
    assert "Do not combine a hardcoded Claude" in text
    assert "Config-driven spawn" in text
    assert "Explicit-session routing" in text
    _assert_spawn_recipes_do_not_pair_generic_config_with_session(text)

    block = _build_task_graph_block("http://localhost:8173", "ws-1")
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        task_graph_block=block,
    )
    assert "explicit-session routing" not in block.lower()
    assert "target_session_id" in master
    for blob in (block, master):
        assert '"executor_config":{"agent_type":"claude"' not in blob
        _assert_spawn_recipes_do_not_pair_generic_config_with_session(blob)


def test_negative_422_is_unavailable_executor_not_adapter_rejection() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "managed kind the adapter rejects" not in text
    assert "or a managed kind" not in text
    assert "Unavailable executor only" in text
    assert "Invalid `managed_task` config" in text
    assert "not 422" in text
    block = _build_task_graph_block("http://localhost:8173", "ws-1")
    assert "managed kind the adapter rejects" not in block
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        task_graph_block=block,
    )
    assert "HTTP 400, not 422" not in block
    assert "native_subagent/external_job" not in block
    assert "/api/agent-tree/spawn" not in master
    assert "managed kind the adapter rejects" not in master
    _assert_master_prompt_uses_task_rest_only(master)


_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.S)
_SPAWN_DASH_D = re.compile(r"-d\s+'(\{.*?\})'", re.S)


def _bash_spawn_payloads(text: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for block in _BASH_BLOCK.findall(text):
        if "/api/agent-tree/spawn" not in block:
            continue
        raw = _SPAWN_DASH_D.search(block)
        assert raw is not None, block
        body = json.loads(raw.group(1))
        assert isinstance(body, dict)
        payloads.append(body)
    return payloads


def test_bash_spawn_examples_reject_call_id_reuse_across_payloads() -> None:
    """Same call_id + different normalized spawn JSON is a taught HTTP 400."""
    payloads = _bash_spawn_payloads(GUIDE.read_text(encoding="utf-8"))
    assert len(payloads) >= 2, payloads
    grouped: dict[str, set[str]] = defaultdict(set)
    for payload in payloads:
        call_id = payload["call_id"]
        assert isinstance(call_id, str)
        grouped[call_id].add(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    conflicts = {call_id: norms for call_id, norms in grouped.items() if len(norms) > 1}
    assert conflicts == {}, conflicts
    assert "spawn-investigate-flaky-1" in grouped
    assert "spawn-investigate-flaky-pinned-1" in grouped
