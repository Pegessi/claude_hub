"""Cross-model strict review gate for Hub-managed Chat Goals."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from ...models import AgentType
from ...models.goal_run import (
    GoalRun,
    GoalStrictReviewFinding,
    GoalStrictReviewRound,
)

logger = logging.getLogger(__name__)

_GOAL_REVIEW_RE = re.compile(
    r"<goal-review>\s*(\{.*?\})\s*</goal-review>",
    re.IGNORECASE | re.DOTALL,
)
_MAX_REVIEW_JSON_CHARS = 32 * 1024
_DEFAULT_REVIEW_TIMEOUT_SECONDS = 900.0
_MAX_ASSISTANT_EXCERPT_CHARS = 12_000


@dataclass(frozen=True)
class StrictReviewerTarget:
    provider: str
    model: str | None = None
    effort: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class StrictReviewOutcome:
    verdict: str  # "PASS" | "BLOCK"
    summary: str
    findings: list[GoalStrictReviewFinding]
    reviewer_provider: str
    reviewer_model: str | None = None
    reviewer_subagent_id: str | None = None


StrictReviewerCallback = Callable[[GoalRun, str, int], Awaitable[StrictReviewOutcome]]


def build_strict_review_prompt(
    goal: GoalRun,
    worker_turn_id: str,
    assistant_text: str,
    round_number: int,
) -> str:
    """Build the read-only adversarial cross-review prompt for the reviewer subagent."""
    excerpt = assistant_text.strip()
    if len(excerpt) > _MAX_ASSISTANT_EXCERPT_CHARS:
        excerpt = excerpt[-_MAX_ASSISTANT_EXCERPT_CHARS:]
    checkpoint_json = (
        goal.checkpoint.model_dump_json(exclude={"turn_id", "created_at"}, indent=2)
        if goal.checkpoint is not None
        else "null"
    )
    prior_rounds = ""
    if goal.strict_review_history:
        lines = [
            f"- Round {item.round_number} ({item.reviewer_provider}"
            f"{'/' + item.reviewer_model if item.reviewer_model else ''}): "
            f"{item.verdict} — {item.summary}"
            for item in goal.strict_review_history
        ]
        prior_rounds = "\nPrevious strict review rounds:\n" + "\n".join(lines) + "\n"

    return f"""You are an independent adversarial reviewer verifying whether this Goal is genuinely complete.
This is a READ-ONLY review (round {round_number} of {goal.strict_review_max_rounds}).
Do NOT edit, write, commit, delete, or mutate repository files or services.

Authoritative Goal Objective:
{goal.objective}
{prior_rounds}
Worker's latest checkpoint:
{checkpoint_json}

Worker's completion claim (turn {worker_turn_id}):
{excerpt}

Instructions:
1. Inspect the actual working directory, git status, git diff, call paths, and test evidence directly.
   Never trust the worker's prose claim without checking the code and verification results.
2. Check for correctness bugs, unhandled edge cases, contract mismatches, missing required tests,
   unrequested scope creep, or needless defensive code/duplication.
3. Classify real defects as P0 (critical breakage/data loss), P1 (functional bug/missing requirement/broken test),
   P2 (minor non-blocking improvement), or P3 (nit).
4. Verdict MUST be "PASS" ONLY if there are ZERO P0 and ZERO P1 defects and the objective is verified.
   Otherwise verdict MUST be "BLOCK" with actionable file/line evidence and minimal fix guidance.

End your response with exactly one machine-readable JSON block:
<goal-review>
{{
  "verdict": "PASS" | "BLOCK",
  "summary": "one concise paragraph summarizing what was verified and why it passed or blocked",
  "findings": [
    {{
      "severity": "P0" | "P1" | "P2" | "P3",
      "file": "path/to/file:line or null",
      "summary": "concrete causal defect description",
      "fix": "smallest necessary fix"
    }}
  ]
}}
</goal-review>
"""


def parse_strict_review_output(
    raw_text: str,
    *,
    reviewer_provider: str,
    reviewer_model: str | None = None,
    reviewer_subagent_id: str | None = None,
) -> StrictReviewOutcome:
    """Parse ``<goal-review>`` JSON, enforcing fail-closed P0/P1 blocking rules."""
    matches = list(_GOAL_REVIEW_RE.finditer(raw_text))
    if not matches:
        raise ValueError("reviewer response omitted a <goal-review> JSON block")
    raw_json = matches[-1].group(1)
    if len(raw_json.encode("utf-8")) > _MAX_REVIEW_JSON_CHARS:
        raise ValueError("reviewer <goal-review> block exceeds 32 KiB")
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("reviewer <goal-review> block must be a JSON object")
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "BLOCK"}:
        raise ValueError("reviewer verdict must be PASS or BLOCK")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("reviewer summary must not be blank")
    raw_findings = payload.get("findings") or []
    if not isinstance(raw_findings, list):
        raise ValueError("reviewer findings must be a JSON list")
    findings = [GoalStrictReviewFinding.model_validate(item) for item in raw_findings[:30]]
    # Fail closed: any P0/P1 finding forces BLOCK even if the model wrote PASS.
    if any(item.severity in {"P0", "P1"} for item in findings):
        verdict = "BLOCK"
    return StrictReviewOutcome(
        verdict=verdict,
        summary=summary[:2000],
        findings=findings,
        reviewer_provider=reviewer_provider,
        reviewer_model=reviewer_model,
        reviewer_subagent_id=reviewer_subagent_id,
    )


def format_strict_review_feedback(round_record: GoalStrictReviewRound) -> str:
    """Format blocking review findings for the next Hub-managed Goal continuation."""
    model_suffix = f" ({round_record.reviewer_model})" if round_record.reviewer_model else ""
    lines = [
        f"Strict cross-model review round {round_record.round_number} "
        f"[{round_record.reviewer_provider}{model_suffix}] returned BLOCK:",
        f"Summary: {round_record.summary}",
    ]
    blocking = [f for f in round_record.findings if f.severity in {"P0", "P1"}]
    advisory = [f for f in round_record.findings if f.severity not in {"P0", "P1"}]
    if blocking:
        lines.append(
            "\nBlocking findings (P0/P1 — address every valid item or refute with concrete code/test evidence):"
        )
        for idx, item in enumerate(blocking, 1):
            loc = f" [{item.file}]" if item.file else ""
            fix = f" -> Fix: {item.fix}" if item.fix else ""
            lines.append(f"{idx}. ({item.severity}){loc} {item.summary}{fix}")
    if advisory:
        lines.append("\nNon-blocking observations (P2/P3 — optional):")
        for idx, item in enumerate(advisory, 1):
            loc = f" [{item.file}]" if item.file else ""
            lines.append(f"{idx}. ({item.severity}){loc} {item.summary}")
    lines.append(
        "\nFix the blocking defects, re-run verification, update <goal-checkpoint>, "
        'and emit <goal-status state="complete"> only when ready for re-review.'
    )
    return "\n".join(lines)


def select_cross_reviewer_candidates(
    parent_agent_type: AgentType,
    parent_env: dict[str, str],
    *,
    maso_base_url: str | None = None,
    maso_models: list[str] | None = None,
) -> list[StrictReviewerTarget]:
    """Choose heterogeneous reviewer candidates for ``parent_agent_type``."""
    candidates: list[StrictReviewerTarget] = []
    parent_maso_model = (parent_env.get("MASO_MODEL") or "").lower()
    available_maso = set(maso_models or [])

    def add_maso(preferred_base: str, preferred_variant: str) -> None:
        if not maso_base_url:
            return
        if available_maso and preferred_base not in available_maso:
            return
        candidates.append(
            StrictReviewerTarget(
                provider=AgentType.MASO.value,
                model=preferred_variant,
                env={"MASO_BASE_URL": maso_base_url},
            )
        )

    if parent_agent_type == AgentType.MASO:
        if "argon" in parent_maso_model or "gemini" in parent_maso_model:
            add_maso("gpt-6-astra", "gpt-6-astra_reasoning_high")
            candidates.append(StrictReviewerTarget(provider=AgentType.CODEX.value, effort="high"))
        else:
            add_maso("gemini-4-argon", "gemini-4-argon_reasoning_high")
            candidates.append(StrictReviewerTarget(provider=AgentType.CODEX.value, effort="high"))
    elif parent_agent_type in {AgentType.CODEX, AgentType.TRAEX}:
        add_maso("gemini-4-argon", "gemini-4-argon_reasoning_high")
        add_maso("gpt-6-astra", "gpt-6-astra_reasoning_high")
        candidates.append(StrictReviewerTarget(provider=AgentType.CLAUDE.value))
    elif parent_agent_type == AgentType.CLAUDE:
        add_maso("gemini-4-argon", "gemini-4-argon_reasoning_high")
        candidates.append(StrictReviewerTarget(provider=AgentType.CODEX.value, effort="high"))
    else:
        add_maso("gemini-4-argon", "gemini-4-argon_reasoning_high")
        candidates.append(StrictReviewerTarget(provider=AgentType.CODEX.value, effort="high"))
        candidates.append(StrictReviewerTarget(provider=AgentType.CLAUDE.value))

    # Last-resort same-provider clean-context fallback if cross-providers are unavailable.
    if parent_agent_type != AgentType.TERMINAL and not any(
        c.provider == parent_agent_type.value and c.model is None for c in candidates
    ):
        candidates.append(StrictReviewerTarget(provider=parent_agent_type.value))
    return candidates


async def run_subagent_strict_review(
    goal: GoalRun,
    assistant_text: str,
    round_number: int,
) -> StrictReviewOutcome:
    """Default production reviewer backed by ``SubagentService``."""
    from ...models.subagent import SubagentCreate, SubagentTurnStatus
    from ..maso_discovery import discover_maso_service
    from ..subagents import get_subagent_service
    from ..ttyd_manager import ttyd_manager

    tab = ttyd_manager.get_tab(goal.tab_id)
    if tab is None:
        raise RuntimeError(f"Goal tab {goal.tab_id} no longer exists")
    parent_env = dict(tab.env or {})
    maso_url: str | None = None
    maso_model_ids: list[str] = []
    try:
        discovered_url, discovered_models, _default = await discover_maso_service(parent_env)
        maso_url = discovered_url
        maso_model_ids = [m.id for m in discovered_models]
    except Exception:
        maso_url = None

    candidates = select_cross_reviewer_candidates(
        tab.agent_type,
        parent_env,
        maso_base_url=maso_url,
        maso_models=maso_model_ids,
    )
    if not candidates:
        raise RuntimeError("No eligible Chat provider available for strict cross-review")

    worker_turn_id = goal.completed_turn_ids[-1] if goal.completed_turn_ids else "latest"
    prompt = build_strict_review_prompt(goal, worker_turn_id, assistant_text, round_number)
    service = get_subagent_service()
    errors: list[str] = []

    for index, candidate in enumerate(candidates):
        call_id = f"goal-strict-review:{goal.id}:{worker_turn_id}:r{round_number}:c{index}"
        record_id: str | None = None
        try:
            record, turn = await service.create(
                SubagentCreate(
                    agent_type=AgentType(candidate.provider),
                    model=candidate.model,
                    effort=candidate.effort,
                    env=dict(candidate.env or {}),
                    parent_tab_id=goal.tab_id,
                    caller_tab_id=goal.tab_id,
                    cwd=tab.cwd,
                    name=f"Goal Review R{round_number} ({candidate.provider})",
                    prompt=prompt,
                    keep=False,
                    solo_mode=True,
                    timeout_seconds=_DEFAULT_REVIEW_TIMEOUT_SECONDS,
                    call_id=call_id,
                )
            )
            record_id = record.id
            while True:
                record, turn = await service.wait_turn(record.id, turn.turn_id, wait_seconds=30.0)
                if turn.status != SubagentTurnStatus.RUNNING:
                    break
            if turn.status != SubagentTurnStatus.COMPLETED or not turn.final_text:
                errors.append(f"{candidate.provider}: {turn.error or turn.status.value}")
                continue
            return parse_strict_review_output(
                turn.final_text,
                reviewer_provider=candidate.provider,
                reviewer_model=candidate.model,
                reviewer_subagent_id=record.id,
            )
        except asyncio.CancelledError:
            if record_id is not None:
                try:
                    await asyncio.shield(service.close(record_id, reason="strict review cancelled"))
                except Exception:
                    logger.warning("Failed to close cancelled strict review subagent %s", record_id)
            raise
        except Exception as exc:
            logger.warning(
                "Strict Goal review candidate %s failed on goal %s round %d: %s",
                candidate.provider,
                goal.id,
                round_number,
                exc,
            )
            errors.append(f"{candidate.provider}: {exc}")

    raise RuntimeError(
        "Strict cross-review could not obtain a valid verdict (" + "; ".join(errors[:3]) + ")"
    )
