"""Cross-model strict review gate for Hub-managed Chat Goals."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal, Sequence

from ...models import AgentType
from ...models.goal_run import (
    GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS,
    GOAL_STRICT_REVIEW_MAX_ROUNDS,
    GoalCheckpoint,
    GoalRun,
    GoalStrictReviewFinding,
    GoalStrictReviewRound,
)
from .. import maso_discovery
from ..cross_reviewer import (
    _ORIGINAL_DISCOVER_MASO_SERVICE,
    CrossReviewCandidate,
    StrictReviewerTarget,
    canonical_maso_base_model,
    is_candidate_quota_exhausted,
    read_cached_quota_map,
    resolve_active_maso_tab_model,
    select_cross_reviewer_candidates,
)
from ..maso_discovery import discover_maso_service

logger = logging.getLogger(__name__)

_GOAL_REVIEW_RE = re.compile(
    r"<goal-review>\s*(\{.*?\})\s*</goal-review>",
    re.IGNORECASE | re.DOTALL,
)
_WORKTREE_PATH_RE = re.compile(r"(/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*_worktree/[A-Za-z0-9._-]+)")
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_TOKEN_4_RE = re.compile(r"[a-z0-9_]{4,}")
_TOKEN_2_RE = re.compile(r"[a-z0-9_]{2,}")
_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "when",
        "have",
        "does",
        "should",
        "must",
        "into",
        "only",
        "also",
        "still",
    }
)
_MAX_REVIEW_JSON_CHARS = 32 * 1024
_MAX_PARSED_FINDINGS = 500
_MAX_RETAINED_FINDINGS = 30
_DEFAULT_REVIEW_TIMEOUT_SECONDS = 900.0
_MAX_ASSISTANT_EXCERPT_CHARS = 12_000
_GIT_CMD_TIMEOUT_SECONDS = 3.0
_MAX_GIT_STDOUT_BYTES = 256 * 1024
_MAX_GIT_STDERR_BYTES = 64 * 1024
_MAX_GIT_SUMMARY_CHARS = 2_000
_MAX_LESSONS_CHARS = 1_200
_MAX_REVIEW_MD_BYTES = 8_192
_MAX_REVIEW_MD_CHARS = 2_000


class _GitOutputOverflow(Exception):
    """Raised when a git subprocess stream exceeds its byte cap."""


@dataclass(frozen=True)
class StrictReviewOutcome:
    verdict: str  # "PASS" | "BLOCK"
    summary: str
    findings: list[GoalStrictReviewFinding]
    reviewer_provider: str
    reviewer_model: str | None = None
    reviewer_subagent_id: str | None = None


@dataclass(frozen=True)
class StrictReviewContext:
    """Read-only situational context collected for the reviewer subagent."""

    primary_cwd: str
    referenced_worktrees: list[str] = field(default_factory=list)
    is_git_repo: bool = False
    git_summary: str = ""
    workspace_id: str | None = None
    lessons_summary: str = ""
    review_md_excerpt: str = ""

    def render_prompt_block(self) -> str:
        sections: list[str] = []
        if self.referenced_worktrees:
            sections.append(
                "Detected referenced feature worktree(s): "
                + ", ".join(self.referenced_worktrees)
                + f" (primary inspection root: {self.primary_cwd})"
            )
        if self.git_summary:
            sections.append(f"Situational Git / Workspace Snapshot:\n{self.git_summary}")
        if self.lessons_summary:
            sections.append(
                "Workspace Review Checkpoints (verify these lessons against the Objective and diff):\n"
                f"{self.lessons_summary}"
            )
        if self.review_md_excerpt:
            sections.append("Repository REVIEW.md Guidance Excerpt:\n" f"{self.review_md_excerpt}")
        if not sections:
            return ""
        return "\n\n".join(sections) + "\n"


@dataclass(frozen=True)
class ReviewContinuationDecision:
    """Objective continuation decision after a blocking cross-review round."""

    should_continue: bool
    reason: Literal["continue", "converging_grace", "impasse", "max_rounds"]
    effective_max_rounds: int
    status_note: str


StrictReviewerCallback = Callable[[GoalRun, str, int], Awaitable[StrictReviewOutcome]]


async def _read_stream_bounded(
    stream: asyncio.StreamReader | None, max_bytes: int = _MAX_GIT_STDOUT_BYTES
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(4096, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _GitOutputOverflow(f"git stream output exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _run_git_command(
    target_cwd: str,
    *args: str,
    timeout: float = _GIT_CMD_TIMEOUT_SECONDS,
    max_stdout_bytes: int = _MAX_GIT_STDOUT_BYTES,
) -> tuple[int, str]:
    """Run a bounded read-only git command without touching index locks or hanging on grandchild pipes."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            target_cwd,
            "--no-optional-locks",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return 1, ""
    timed_out = False
    overflowed = False
    needs_pg_kill = False
    stdout_bytes = b""
    try:
        async with asyncio.timeout(timeout):
            stdout_task = asyncio.create_task(_read_stream_bounded(proc.stdout, max_stdout_bytes))
            stderr_task = asyncio.create_task(
                _read_stream_bounded(proc.stderr, _MAX_GIT_STDERR_BYTES)
            )
            try:
                stdout_bytes, _, _ = await asyncio.gather(stdout_task, stderr_task, proc.wait())
            finally:
                for reader_task in (stdout_task, stderr_task):
                    if not reader_task.done():
                        reader_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    except _GitOutputOverflow:
        overflowed = True
        needs_pg_kill = True
    except (asyncio.TimeoutError, TimeoutError):
        timed_out = True
        needs_pg_kill = True
    except asyncio.CancelledError:
        needs_pg_kill = True
        raise
    finally:
        if needs_pg_kill or proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
    if timed_out:
        return 124, ""
    if overflowed:
        return 1, ""
    return (proc.returncode or 0), stdout_bytes.decode("utf-8", errors="replace").strip()


def _detect_referenced_worktrees_sync(
    cwd: str,
    *,
    objective: str,
    assistant_text: str,
    checkpoint: GoalCheckpoint | None,
) -> tuple[list[str], list[str]]:
    """Detect referenced *_worktree/<slug> directories and filter existing inspection dirs off-loop."""
    checkpoint_blob = (
        checkpoint.model_dump_json(exclude={"turn_id", "created_at"})
        if checkpoint is not None
        else ""
    )
    corpus = f"{checkpoint_blob}\n{assistant_text}\n{objective}"
    try:
        resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else ""
    except OSError:
        resolved_cwd = cwd
    found: list[str] = []
    for raw_match in _WORKTREE_PATH_RE.findall(corpus):
        candidate = raw_match.rstrip(".,;:)'\"`]")
        try:
            path = Path(candidate).expanduser()
            if not path.is_dir():
                continue
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved != resolved_cwd and resolved not in found:
            found.append(resolved)
        if len(found) >= 3:
            break

    existing_dirs: list[str] = []
    for d in [*found, cwd]:
        if not d or d in existing_dirs:
            continue
        try:
            if Path(d).expanduser().is_dir():
                existing_dirs.append(d)
        except OSError:
            continue
    return found, existing_dirs


async def _collect_single_dir_git_snapshot(target_cwd: str) -> tuple[bool, str, str | None]:
    """Return (is_git, formatted_snapshot, canonical_repo_root)."""
    rc, inside = await _run_git_command(target_cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or inside.lower() != "true":
        return False, "", None

    _, branch = await _run_git_command(target_cwd, "rev-parse", "--abbrev-ref", "HEAD")
    _, head_sha = await _run_git_command(target_cwd, "rev-parse", "--short", "HEAD")
    _, status_out = await _run_git_command(target_cwd, "status", "--short")
    _, wt_diff_stat = await _run_git_command(target_cwd, "diff", "--stat", "HEAD")

    canonical_root: str | None = None
    rc_common, common_dir = await _run_git_command(target_cwd, "rev-parse", "--git-common-dir")
    if rc_common == 0 and common_dir:
        try:
            common_path = Path(target_cwd, common_dir).resolve()
            canonical_root = str(common_path.parent if common_path.name == ".git" else common_path)
        except OSError:
            canonical_root = None

    branch_diff_stat = ""
    if branch and branch not in {"main", "master", "develop", "HEAD"}:
        for base_ref in ("main", "master", "origin/main", "origin/master"):
            rc_mb, merge_base = await _run_git_command(target_cwd, "merge-base", "HEAD", base_ref)
            if rc_mb == 0 and merge_base:
                _, branch_diff_stat = await _run_git_command(
                    target_cwd, "diff", "--stat", f"{merge_base}..HEAD"
                )
                break

    status_lines = status_out.splitlines()[:40] if status_out else []
    status_block = "\n".join(status_lines) if status_lines else "(clean working tree)"
    lines = [
        f"- Directory: {target_cwd} (branch: {branch or 'unknown'} @ {head_sha or 'unknown'})",
        f"  git status --short:\n{_indent_block(status_block, '    ')}",
    ]
    if branch_diff_stat:
        lines.append(
            f"  committed branch diff (--stat vs base):\n{_indent_block(branch_diff_stat, '    ')}"
        )
    if wt_diff_stat:
        lines.append(
            f"  uncommitted working-tree diff (--stat vs HEAD):\n{_indent_block(wt_diff_stat, '    ')}"
        )
    return True, "\n".join(lines), canonical_root


def _indent_block(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def _read_bounded_review_md(target_dir: str) -> str:
    """Safely read up to 8 KiB from <target_dir>/REVIEW.md, rejecting symlinks/FIFOs outside root."""
    if not target_dir:
        return ""
    try:
        root_real = Path(target_dir).expanduser().resolve(strict=True)
        review_file = Path(target_dir).expanduser() / "REVIEW.md"
        if review_file.is_symlink():
            resolved_target = review_file.resolve(strict=True)
            if not resolved_target.is_relative_to(root_real):
                return ""
            open_path = resolved_target
        else:
            lstat_res = review_file.lstat()
            if not stat.S_ISREG(lstat_res.st_mode):
                return ""
            resolved_target = review_file.resolve(strict=True)
            if not resolved_target.is_relative_to(root_real):
                return ""
            open_path = review_file

        if not stat.S_ISREG(open_path.lstat().st_mode):
            return ""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(str(open_path), flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return ""
            with contextlib.suppress(OSError):
                os.set_blocking(fd, True)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw_bytes = handle.read(_MAX_REVIEW_MD_BYTES)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        return raw_bytes.decode("utf-8", errors="replace").strip()[:_MAX_REVIEW_MD_CHARS]
    except OSError:
        return ""


def _collect_workspace_memory_sync(
    candidate_paths: Sequence[str],
    *,
    objective: str,
    assistant_text: str,
) -> tuple[str | None, str, str]:
    """Match candidate paths against registered workspaces and read lessons + REVIEW.md off-loop."""
    workspace_id: str | None = None
    lessons_summary = ""
    review_md_excerpt = ""

    for path_str in candidate_paths:
        excerpt = _read_bounded_review_md(path_str)
        if excerpt:
            review_md_excerpt = excerpt
            break

    try:
        from ..workspace_manager import workspace_manager

        workspaces = list((getattr(workspace_manager, "workspaces", None) or {}).values())
        matched_ws = None
        best_len = -1
        resolved_candidates: list[Path] = []
        for raw_p in candidate_paths:
            if not raw_p:
                continue
            try:
                resolved_candidates.append(Path(raw_p).expanduser().resolve())
            except OSError:
                continue

        for ws in workspaces:
            ws_path_raw = getattr(ws, "path", None)
            if not ws_path_raw:
                continue
            try:
                ws_path = Path(ws_path_raw).expanduser().resolve()
            except OSError:
                continue
            for cand_path in resolved_candidates:
                if cand_path == ws_path or ws_path in cand_path.parents:
                    score = len(str(ws_path))
                    if score > best_len:
                        best_len = score
                        matched_ws = ws

        if matched_ws is not None:
            from ..feedback.common import INLINE_LESSONS_MAX_COUNT, render_inline_lessons_block

            workspace_id = matched_ws.id
            query = f"{objective}\n{assistant_text[:1000]}"
            raw_items = workspace_manager._lesson_context_payload(matched_ws, query)
            feedback_store = None
            try:
                feedback_store = workspace_manager._feedback_store()
            except Exception:
                feedback_store = None

            enriched_items: list[dict[str, object]] = []
            matched_ids: list[str] = []
            for item in (raw_items or [])[:INLINE_LESSONS_MAX_COUNT]:
                if not isinstance(item, dict):
                    continue
                lid = str(item.get("id") or "").strip()
                if not lid:
                    continue
                enriched: dict[str, object] = dict(item)
                has_actionable = bool(
                    str(enriched.get("do") or "").strip()
                    or str(enriched.get("avoid") or "").strip()
                    or str(enriched.get("summary") or "").strip()
                )
                if feedback_store is not None and not has_actionable:
                    try:
                        full = feedback_store.get_lesson(matched_ws.id, lid)
                        enriched["title"] = enriched.get("title") or getattr(full, "title", "")
                        enriched["summary"] = getattr(full, "summary", "") or ""
                        enriched["applies_when"] = list(getattr(full, "applies_when", []) or [])
                        enriched["do"] = getattr(full, "do", "") or ""
                        enriched["avoid"] = getattr(full, "avoid", "") or ""
                    except Exception:
                        pass
                enriched_items.append(enriched)
                matched_ids.append(lid)

            if enriched_items:
                lessons_summary = render_inline_lessons_block(
                    enriched_items,
                    workspace_id=matched_ws.id,
                    role="strict_review",
                    max_chars=_MAX_LESSONS_CHARS,
                    max_items=INLINE_LESSONS_MAX_COUNT,
                )
                if feedback_store is not None and matched_ids:
                    dedupe_digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()[:16]
                    try:
                        feedback_store.record_lesson_hit(
                            matched_ws.id,
                            matched_ids,
                            task_id=f"strict-review:{dedupe_digest}",
                        )
                    except Exception:
                        pass
    except Exception:
        logger.debug("Skipping workspace lesson lookup during Goal strict review", exc_info=True)

    return workspace_id, lessons_summary, review_md_excerpt


async def collect_strict_review_context(
    cwd: str | None,
    *,
    objective: str,
    assistant_text: str,
    checkpoint: GoalCheckpoint | None = None,
) -> StrictReviewContext:
    """Gather objective situational facts (Git diff/status, Workspace lessons, REVIEW.md)."""
    effective_cwd = (cwd or "").strip()
    referenced, inspect_dirs = await asyncio.to_thread(
        _detect_referenced_worktrees_sync,
        effective_cwd,
        objective=objective,
        assistant_text=assistant_text,
        checkpoint=checkpoint,
    )
    primary_cwd = referenced[0] if referenced else effective_cwd

    any_git = False
    git_blocks: list[str] = []
    canonical_roots: list[str] = []

    for d in inspect_dirs:
        is_git, block, root = await _collect_single_dir_git_snapshot(d)
        if is_git:
            any_git = True
            git_blocks.append(block)
            if root and root not in canonical_roots:
                canonical_roots.append(root)

    if any_git:
        git_summary = "\n".join(git_blocks)[:_MAX_GIT_SUMMARY_CHARS]
    else:
        git_summary = (
            "Target directory is not a Git repository; verify files, outputs, "
            "and evidence directly against the Objective."
        )

    all_candidate_paths = [p for p in [*referenced, effective_cwd, *canonical_roots] if p]
    workspace_id, lessons_summary, review_md_excerpt = await asyncio.to_thread(
        _collect_workspace_memory_sync,
        all_candidate_paths,
        objective=objective,
        assistant_text=assistant_text,
    )

    return StrictReviewContext(
        primary_cwd=primary_cwd,
        referenced_worktrees=referenced,
        is_git_repo=any_git,
        git_summary=git_summary,
        workspace_id=workspace_id,
        lessons_summary=lessons_summary,
        review_md_excerpt=review_md_excerpt,
    )


def _normalize_finding_file(raw_file: str | None) -> str:
    if not raw_file:
        return ""
    cleaned = raw_file.strip().replace("\\", "/").lower()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = _LINE_SUFFIX_RE.sub("", cleaned)
    return cleaned


def _finding_tokens(summary: str) -> set[str]:
    lowered = summary.lower()
    tokens = {tok for tok in _TOKEN_4_RE.findall(lowered) if tok not in _STOPWORDS}
    if not tokens:
        tokens = set(_TOKEN_2_RE.findall(lowered))
    return tokens


def _findings_match(a: GoalStrictReviewFinding, b: GoalStrictReviewFinding) -> bool:
    file_a = _normalize_finding_file(a.file)
    file_b = _normalize_finding_file(b.file)
    tokens_a = _finding_tokens(a.summary)
    tokens_b = _finding_tokens(b.summary)
    if not tokens_a and not tokens_b:
        jaccard = 1.0 if a.summary.strip().lower() == b.summary.strip().lower() else 0.0
    else:
        union = tokens_a | tokens_b
        jaccard = (len(tokens_a & tokens_b) / len(union)) if union else 0.0

    if file_a and file_b:
        return file_a == file_b and jaccard >= 0.35
    if not file_a and not file_b:
        return jaccard >= 0.50
    return jaccard >= 0.65


def _blocking_findings(round_record: GoalStrictReviewRound) -> list[GoalStrictReviewFinding]:
    return [f for f in round_record.findings if f.severity in {"P0", "P1"}]


def _blocker_count(round_record: GoalStrictReviewRound) -> int:
    blockers = _blocking_findings(round_record)
    if round_record.verdict == "BLOCK":
        return max(1, len(blockers))
    return len(blockers)


def _active_epoch_history(
    history: Sequence[GoalStrictReviewRound],
) -> list[GoalStrictReviewRound]:
    items = list(history)
    if not items:
        return []
    current_cycle = getattr(items[-1], "cycle", 1)
    cycle_items = [item for item in items if getattr(item, "cycle", 1) == current_cycle]
    for idx in range(len(cycle_items) - 1, -1, -1):
        if cycle_items[idx].round_number == 1:
            return cycle_items[idx:]
    return cycle_items


def evaluate_review_continuation(
    history: Sequence[GoalStrictReviewRound],
    *,
    base_max_rounds: int = GOAL_STRICT_REVIEW_MAX_ROUNDS,
    hard_max_rounds: int = GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS,
) -> ReviewContinuationDecision:
    """Determine whether a BLOCK review round should continue, grant convergence grace, or stop."""
    epoch = _active_epoch_history(history)
    if not epoch:
        return ReviewContinuationDecision(
            should_continue=True,
            reason="continue",
            effective_max_rounds=base_max_rounds,
            status_note="",
        )

    current = epoch[-1]
    r = len(epoch)
    b_curr = _blocker_count(current)

    if len(epoch) >= 2:
        prev = epoch[-2]
        b_prev = _blocker_count(prev)
        curr_blockers = _blocking_findings(current)
        prev_blockers = _blocking_findings(prev)
        if b_curr >= b_prev and curr_blockers and prev_blockers:
            matched = sum(
                1 for fa in curr_blockers if any(_findings_match(fa, fb) for fb in prev_blockers)
            )
            if (matched / b_curr) >= 0.5:
                return ReviewContinuationDecision(
                    should_continue=False,
                    reason="impasse",
                    effective_max_rounds=max(base_max_rounds, r),
                    status_note=(
                        f"Strict cross-review stopped early on round {r} due to a recurring "
                        "blocker impasse; steer the Goal or complete manually"
                    ),
                )

        if r >= base_max_rounds:
            if r < hard_max_rounds and b_curr < b_prev:
                effective_max = min(hard_max_rounds, r + 1)
                return ReviewContinuationDecision(
                    should_continue=True,
                    reason="converging_grace",
                    effective_max_rounds=effective_max,
                    status_note=(
                        f"Blocking findings decreased ({b_prev} -> {b_curr}); "
                        f"granted convergence grace round {r + 1}/{effective_max}"
                    ),
                )
            return ReviewContinuationDecision(
                should_continue=False,
                reason="max_rounds",
                effective_max_rounds=max(base_max_rounds, r),
                status_note=(
                    f"Strict cross-review reached {r}/{max(base_max_rounds, r)} rounds "
                    "without consensus"
                ),
            )

    if r >= base_max_rounds:
        return ReviewContinuationDecision(
            should_continue=False,
            reason="max_rounds",
            effective_max_rounds=max(base_max_rounds, r),
            status_note=(
                f"Strict cross-review reached {r}/{max(base_max_rounds, r)} rounds "
                "without consensus"
            ),
        )

    return ReviewContinuationDecision(
        should_continue=True,
        reason="continue",
        effective_max_rounds=base_max_rounds,
        status_note="",
    )


def build_strict_review_prompt(
    goal: GoalRun,
    worker_turn_id: str,
    assistant_text: str,
    round_number: int,
    *,
    context: StrictReviewContext | None = None,
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
    effective_max = max(goal.strict_review_max_rounds, round_number)
    prior_rounds = ""
    active_history = [
        item
        for item in goal.strict_review_history
        if getattr(item, "cycle", 1) == getattr(goal, "strict_review_cycle", 1)
    ] or list(goal.strict_review_history)
    if active_history:
        lines: list[str] = []
        for item in active_history:
            lines.append(
                f"- Round {item.round_number} ({item.reviewer_provider}"
                f"{'/' + item.reviewer_model if item.reviewer_model else ''}): "
                f"{item.verdict} — {item.summary}"
            )
            for finding in item.findings:
                if finding.severity in {"P0", "P1"}:
                    loc = finding.file or "(general)"
                    fix_hint = f" (Suggested fix: {finding.fix})" if finding.fix else ""
                    lines.append(f"  * [{finding.severity}] {loc} — {finding.summary}{fix_hint}")
        prior_rounds = (
            "\nPrevious strict review rounds & prior P0/P1 blocker ledger:\n"
            + "\n".join(lines)
            + "\n"
            "Prior-round review fairness rules:\n"
            "1. Verify each prior P0/P1 item above is either genuinely resolved OR validly "
            "refuted by the worker with concrete code/test/runtime evidence (accept valid "
            "evidence-backed rebuttals).\n"
            "2. Focus any new findings on regressions introduced since the previous round or "
            "genuine P0/P1 defects missed earlier; do NOT shift goalposts onto subjective preferences.\n"
        )

    context_block = f"\n{context.render_prompt_block()}" if context is not None else ""

    return f"""You are an independent adversarial reviewer verifying whether this Goal is genuinely complete.
This is a READ-ONLY review (round {round_number} of {effective_max}).
Do NOT edit, write, commit, delete, or mutate repository files or services.

Authoritative Goal Objective:
{goal.objective}
{prior_rounds}{context_block}
Worker's latest checkpoint:
{checkpoint_json}

Worker's completion claim (turn {worker_turn_id}):
{excerpt}

Instructions & Surgical Review Rubric:
1. Full-Diff & Situational Scope:
   - If the target directory (or a referenced feature worktree) is a Git repository, inspect the COMPLETE actual git status and diff across all modified/untracked files, never restricting your review only to the subset of files cited in the worker's prose.
   - For read-only investigations, benchmark analyses, or non-Git directory tasks, verify outputs, calculations, and evidence directly against the Objective without demanding Git worktree/commit ceremony.
2. Causal P0/P1 Bar (Evidence-First, Anti-Dogmatic):
   - Every P0 (critical breakage/data loss/security flaw) or P1 (functional bug/unmet explicit Objective requirement/broken verification) finding MUST state a concrete, reachable causal counterexample ("When X happens, current Y causes Z") with exact file:line or command/artifact evidence.
   - Anti-Nitpick Guard: pure style/naming preferences, speculative defensive checks for unreachable internal states, or unrequested scope expansion beyond Objective MUST NOT be rated P0 or P1 (classify as P2/P3, which do not block completion).
3. Verification & Baseline Honesty:
   - Check whether the worker's claimed tests/commands/metrics actually establish the outcome (e.g., syntax checks or mocks cannot substitute for required runtime verification; performance/memory claims require comparable before-vs-after baseline evidence or honest disclosure of environment limits).
4. Verdict Rule:
   - Verdict MUST be "PASS" ONLY if there are ZERO P0 and ZERO P1 defects and the Objective is verified.
   - Otherwise verdict MUST be "BLOCK" with actionable file/line evidence and minimal fix guidance.

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
    """Parse ``<goal-review>`` JSON, enforcing fail-closed P0/P1 blocking rules across all findings."""
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
    if len(raw_findings) > _MAX_PARSED_FINDINGS:
        raise ValueError(f"reviewer findings list exceeds {_MAX_PARSED_FINDINGS} items")
    all_findings = [GoalStrictReviewFinding.model_validate(item) for item in raw_findings]
    blocking = [item for item in all_findings if item.severity in {"P0", "P1"}]
    advisory = [item for item in all_findings if item.severity not in {"P0", "P1"}]
    # Fail closed: any P0/P1 finding anywhere in the full list forces BLOCK even if the model wrote PASS.
    if blocking:
        verdict = "BLOCK"
    findings = (
        blocking[:_MAX_RETAINED_FINDINGS]
        + advisory[: max(0, _MAX_RETAINED_FINDINGS - len(blocking))]
    )[:_MAX_RETAINED_FINDINGS]
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
        "\nFix the blocking defects (or provide concrete code/runtime evidence refuting any "
        "false positive), re-run verification, update <goal-checkpoint>, "
        'and emit <goal-status state="complete"> only when ready for re-review.'
    )
    return "\n".join(lines)


async def run_subagent_strict_review(
    goal: GoalRun,
    assistant_text: str,
    round_number: int,
) -> StrictReviewOutcome:
    """Default production reviewer backed by ``SubagentService``."""
    from ...models.subagent import SubagentCreate, SubagentTurnStatus
    from .. import subagents as subagents_mod
    from ..ttyd_manager import ttyd_manager

    tab = ttyd_manager.get_tab(goal.tab_id)
    if tab is None:
        raise RuntimeError(f"Goal tab {goal.tab_id} no longer exists")
    parent_env = dict(tab.env or {})
    if tab.agent_type == AgentType.MASO and not parent_env.get("MASO_MODEL"):
        active_model = resolve_active_maso_tab_model(goal.tab_id)
        if active_model:
            parent_env["MASO_MODEL"] = active_model

    maso_url: str | None = None
    maso_model_ids: list[str] = []
    default_maso_model: str | None = None
    should_probe_maso = not (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("CLAUDE_HUB_ENABLE_CROSS_REVIEW_PROBE_IN_TESTS") != "1"
        and discover_maso_service is _ORIGINAL_DISCOVER_MASO_SERVICE
        and maso_discovery.discover_maso_service is _ORIGINAL_DISCOVER_MASO_SERVICE
    )
    if should_probe_maso:
        probe = (
            discover_maso_service
            if discover_maso_service is not _ORIGINAL_DISCOVER_MASO_SERVICE
            else maso_discovery.discover_maso_service
        )
        try:
            discovered_url, discovered_models, discovered_default = await probe(parent_env)
            maso_url = discovered_url
            maso_model_ids = [m.id for m in discovered_models]
            default_maso_model = discovered_default
        except Exception:
            maso_url = None

    quota_map = await read_cached_quota_map()
    candidates = select_cross_reviewer_candidates(
        tab.agent_type,
        parent_env,
        maso_base_url=maso_url,
        maso_models=maso_model_ids,
        default_maso_model=default_maso_model,
        quota_by_provider=quota_map,
    )
    if not candidates:
        raise RuntimeError(
            "No eligible Chat reviewer subagent candidate available for strict cross-review"
        )

    context = await collect_strict_review_context(
        tab.cwd,
        objective=goal.objective,
        assistant_text=assistant_text,
        checkpoint=goal.checkpoint,
    )
    worker_turn_id = goal.completed_turn_ids[-1] if goal.completed_turn_ids else "latest"
    prompt = build_strict_review_prompt(
        goal,
        worker_turn_id,
        assistant_text,
        round_number,
        context=context,
    )
    service = subagents_mod.get_subagent_service()
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
                    cwd=context.primary_cwd or tab.cwd,
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


__all__ = [
    "CrossReviewCandidate",
    "ReviewContinuationDecision",
    "StrictReviewContext",
    "StrictReviewOutcome",
    "StrictReviewerCallback",
    "StrictReviewerTarget",
    "build_strict_review_prompt",
    "canonical_maso_base_model",
    "collect_strict_review_context",
    "evaluate_review_continuation",
    "format_strict_review_feedback",
    "is_candidate_quota_exhausted",
    "parse_strict_review_output",
    "run_subagent_strict_review",
    "select_cross_reviewer_candidates",
]
