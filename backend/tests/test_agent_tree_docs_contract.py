"""Bounded contract tests for the Agent Tree agent guide and Resident prompt."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from claude_hub.services.task_graph import (
    RESIDENT_CONSUMER_TEMPLATE,
    make_resident_consumer_key,
)
from claude_hub.services.workspace_manager._workspaces import (
    _build_agent_tree_block,
    _build_resident_master_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "AGENT_TREE.md"

CHANGELOG = REPO_ROOT / "CHANGELOG.md"


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

# A copy-paste spawn JSON that sends both keys can invent a Claude config
# for a Codex/Cursor worker. Recipes must split derive vs Hub-pick.
_BOTH_KEYS_IN_OBJECT = re.compile(
    r'\{[^{}]*"executor_config"\s*:\s*\{[^}]*\}[^{}]*"session_id"|'
    r'\{[^{}]*"session_id"[^{}]*"executor_config"\s*:'
)


def _assert_spawn_recipes_do_not_pair_generic_config_with_session(text: str) -> None:
    compact = re.sub(r"\s+", "", text)
    assert _BOTH_KEYS_IN_OBJECT.search(compact) is None, text


def test_changelog_and_guide_use_exact_resident_consumer_key() -> None:
    """Resident consumer key must match task_graph.make_resident_consumer_key."""
    guide = GUIDE.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    template = RESIDENT_CONSUMER_TEMPLATE
    assert template == "workspace:{workspace_id}:resident"
    assert template in guide
    assert template in changelog
    for forbidden in ("resident:<workspace>", "resident:<workspace_id>"):
        assert forbidden not in guide, forbidden
        assert forbidden not in changelog, forbidden


def test_agent_tree_guide_covers_required_agent_contract() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert 320 <= text.count("\n") + 1 <= 420
    for needle in (
        "## Mental model",
        "Task Graph (canonical)",
        "Session assignment",
        "compat projection",
        "compat projection only",
        "claude-hub task",
        "workspace:{workspace_id}:resident",
        "/api/workspaces/WS_ID/tasks/tree",
        "/tasks/TASK_ID/abort",
        "TaskMailbox `ABORT`",
        "manual control",
        "parent_task_id",
        "TaskMailbox",
        "consumer_ack_sequence",
        "resident_ack_sequence",
        "pre-migration",
        "related_task_id",
        "## Runtime boundary",
        "/api/agent-tree/runs",
        "/api/agent-tree/spawn",
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
        "487c630c",
        "2026-08-16-agent-tree-durable-mailbox.md",
        "native_subagent",
        "external_job",
    ):
        assert needle in text, needle
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden not in text, forbidden
    assert "managed kind the adapter rejects" not in text
    assert "Invalid `managed_task` config" in text
    assert "not 422" in text
    _assert_spawn_recipes_do_not_pair_generic_config_with_session(text)


def test_task_graph_guide_forbids_dual_control_plane_wording() -> None:
    """Lock Task Graph as canonical; agent-tree is compat projection only."""
    text = GUIDE.read_text(encoding="utf-8")
    lowered = text.lower()
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden.lower() not in lowered, forbidden
    assert RESIDENT_CONSUMER_TEMPLATE in text
    assert make_resident_consumer_key("ws-example") == "workspace:ws-example:resident"
    assert "claude-hub task" in lowered
    assert "compat projection" in lowered
    assert "task graph (primary)" in lowered or "task graph (canonical)" in lowered
    assert "task graph / taskmailbox" in agents.lower()
    assert "primary: `claude-hub task`" in agents
    assert "agent-tree` compat only" in agents
    assert agents == claude


def test_task_graph_primary_contract_matches_source() -> None:
    """Task Graph primary semantics: keys, assignment, abort, compat-only agent-tree."""
    text = GUIDE.read_text(encoding="utf-8")
    assert RESIDENT_CONSUMER_TEMPLATE in text
    assert make_resident_consumer_key("ws-1") == "workspace:ws-1:resident"
    for needle in (
        "## Task Graph REST (primary)",
        "Task Graph (primary)",
        "parent_task_id",
        "Session assignment",
        "compat projection only",
        "claude-hub --json task abort",
        "TaskMailbox `ABORT`",
        "session/context reuse",
        "487c630c",
    ):
        assert needle in text, needle
    assert "## Agent Tree REST (compat projection)" in text
    for forbidden in _FORBIDDEN_DUAL_CONTROL_PHRASES:
        assert forbidden not in text, forbidden


def test_readmes_and_agent_entry_link_the_guide() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    backend_readme = (REPO_ROOT / "backend" / "README.md").read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "docs/AGENT_TREE.md" in root_readme
    assert "../docs/AGENT_TREE.md" in backend_readme
    assert "docs/AGENT_TREE.md" in agents
    assert "Task Graph / TaskMailbox" in agents
    assert "primary: `claude-hub task`" in agents
    assert agents == claude


def test_resident_agent_tree_block_teaches_current_boundaries() -> None:
    missing = _build_agent_tree_block("http://localhost:8173", "ws-1", None)
    assert "not yet available" in missing
    assert "spawn" not in missing.lower()

    block = _build_agent_tree_block("http://localhost:8173", "ws-1", "root-run-1", ack_sequence=4)
    for needle in (
        "root-run-1",
        'since_sequence":4',
        "executor_config",
        "omit executor_config",
        "config_from_session",
        "validate_session",
        "Claude/Codex only",
        "Cursor MUST omit executor_config.model",
        "explicit model override is rejected",
        "HTTP 400, not 422",
        "docs/AGENT_TREE.md",
        "HTTP 400",
        "HTTP 409",
        "HTTP 422",
        "native_subagent",
        "external_job",
        "retry-uncertain",
        "487c630c",
        "/api/agent-tree/runs?workspace_id=ws-1",
        "/api/agent-tree/spawn",
        "/api/agent-tree/wait",
        "/api/agent-tree/ack",
        "/api/agent-tree/followup",
        "/api/agent-tree/interrupt",
        "/events?since_sequence=0",
    ):
        assert needle in block, needle
    assert "same call_id" in block
    assert "new call_id" in block
    assert '"executor_config":{"agent_type":"claude"' not in block
    _assert_spawn_recipes_do_not_pair_generic_config_with_session(block)


def test_pinned_session_and_status_codes_are_source_accurate() -> None:
    """Lock the two review blockers: derive/match pinned sessions; 400 vs 422."""
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

    block = _build_agent_tree_block("http://localhost:8173", "ws-1", "root-run-1")
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        agent_tree_block=block,
    )
    for blob in (block, master):
        assert "omit executor_config" in blob
        assert "Do not hard-code Claude" in blob
        assert "HTTP 400" in blob
        assert '"executor_config":{"agent_type":"claude"' not in blob
        _assert_spawn_recipes_do_not_pair_generic_config_with_session(blob)
    assert "native_subagent/external_job" in block
    assert "HTTP 400, not 422" in block


def test_negative_cursor_model_is_omitted_from_examples() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", text)
    assert '"agent_type":"cursor","model"' not in compact
    cursor_example = re.search(r"```json\n(\{\"agent_type\":\"cursor\".*?\})\n```", text)
    assert cursor_example is not None, text
    assert "model" not in cursor_example.group(1)
    block = _build_agent_tree_block("http://localhost:8173", "ws-1", "root-run-1")
    assert "Cursor MUST omit executor_config.model" in block
    assert "explicit model override is rejected" in block


def test_negative_explicit_session_does_not_hardcode_claude_config() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "OPTIONAL_WORKER_SESSION_ID" not in text
    assert "Do not combine a hardcoded Claude" in text
    assert "Config-driven spawn" in text
    assert "Explicit-session routing" in text
    _assert_spawn_recipes_do_not_pair_generic_config_with_session(text)

    block = _build_agent_tree_block("http://localhost:8173", "ws-1", "root-run-1")
    master = _build_resident_master_prompt(
        SimpleNamespace(id="ws-1"),
        "http://localhost:8173",
        "sid",
        "",
        agent_tree_block=block,
    )
    for blob in (block, master):
        assert "explicit-session routing" in blob.lower()
        assert "session_id is mandatory" in blob
        assert "Do not combine hardcoded Claude" in blob
        assert '"executor_config":{"agent_type":"claude"' not in blob
        _assert_spawn_recipes_do_not_pair_generic_config_with_session(blob)


def test_negative_422_is_unavailable_executor_not_adapter_rejection() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "managed kind the adapter rejects" not in text
    assert "or a managed kind" not in text
    assert "Unavailable executor only" in text
    assert "Invalid `managed_task` config" in text
    assert "not 422" in text
    block = _build_agent_tree_block("http://localhost:8173", "ws-1", "root-run-1")
    assert "HTTP 400, not 422" in block
    assert "native_subagent/external_job" in block
    assert "managed kind the adapter rejects" not in block


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
