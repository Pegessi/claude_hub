"""Standardized work pipeline automation: worktree provisioning, remote branch push, and InfoWall sync."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

from ..models import Workspace, WorkspaceTask
from .workspace_identity import git_repo_identity

logger = logging.getLogger(__name__)

DEFAULT_INFOWALL_BIN = "/home/tiger/.local/opt/infowall/current/infowall"
DEFAULT_INFOWALL_URL = "http://127.0.0.1:18899"
DEFAULT_INFOWALL_API_KEY_FILE = "/home/tiger/.local/state/infowall/api-key"

PROTECTED_PUSH_BRANCHES = frozenset({"main", "master", "develop", "HEAD", ""})
_URL_PATTERN = re.compile(r"https?://[^\s)>\]\"'`]+")


def slugify_task_title(title: str, task_id: str) -> str:
    """Build a deterministic ASCII kebab-case slug ``<title-slug>-<task_id[:6]>``."""
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if len(ascii_slug) > 32:
        ascii_slug = ascii_slug[:32].rstrip("-")
    if not ascii_slug:
        ascii_slug = "task"
    short_id = re.sub(r"[^a-z0-9]", "", (task_id or "").lower())[:6] or "000000"
    return f"{ascii_slug}-{short_id}"


async def _run_git(
    repo_dir: str | Path,
    *args: str,
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_dir),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git {' '.join(args)} timed out after {timeout:.0f}s"
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace").strip(),
        stderr_bytes.decode("utf-8", errors="replace").strip(),
    )


async def provision_task_worktree(
    workspace: Workspace,
    task: WorkspaceTask,
) -> tuple[str, str] | None:
    """Create or reuse an isolated sibling worktree for a local workspace task.

    Places worktrees under ``<canonical_repo_parent>/<canonical_repo_name>_worktree/<slug>``
    on branch ``feat/<slug>``. Never touches or resets the primary checkout.
    """
    identity = git_repo_identity(workspace.path)
    if not identity.is_git:
        logger.warning(
            "Cannot provision auto_worktree for non-git workspace_id=%s path=%s",
            workspace.id,
            workspace.path,
        )
        return None

    canonical_root = Path(identity.primary_worktree).resolve()
    repo_name = canonical_root.name or "repo"
    slug = slugify_task_title(task.title, task.id)
    worktree_parent = canonical_root.parent / f"{repo_name}_worktree"
    worktree_path = worktree_parent / slug
    worktree_branch = f"feat/{slug}"

    if worktree_path.exists() and (worktree_path / ".git").exists():
        return str(worktree_path.resolve()), worktree_branch

    worktree_parent.mkdir(parents=True, exist_ok=True)

    default_branch = (workspace.default_branch or "main").strip() or "main"
    rc_ref, _, _ = await _run_git(
        canonical_root, "rev-parse", "--verify", "--quiet", default_branch
    )
    base_ref = default_branch if rc_ref == 0 else "HEAD"

    rc_branch, _, _ = await _run_git(
        canonical_root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{worktree_branch}",
    )
    if rc_branch == 0:
        rc_add, _, err_add = await _run_git(
            canonical_root,
            "worktree",
            "add",
            str(worktree_path),
            worktree_branch,
        )
    else:
        rc_add, _, err_add = await _run_git(
            canonical_root,
            "worktree",
            "add",
            str(worktree_path),
            "-b",
            worktree_branch,
            base_ref,
        )

    if rc_add != 0:
        if worktree_path.exists() and (worktree_path / ".git").exists():
            return str(worktree_path.resolve()), worktree_branch
        logger.warning(
            "Failed to create git worktree for task_id=%s path=%s branch=%s error=%s",
            task.id,
            worktree_path,
            worktree_branch,
            err_add,
        )
        return None

    return str(worktree_path.resolve()), worktree_branch


async def push_task_feature_branch(
    workspace: Workspace,
    task: WorkspaceTask,
    *,
    cwd: str | None = None,
) -> tuple[str | None, str | None]:
    """Safely push a reviewed feature branch to ``origin`` without ``--force``.

    Returns ``(remote_pushed_ref, remote_push_error)``. Refuses to push ``main``,
    ``master``, ``develop``, detached ``HEAD``, or ``workspace.default_branch``.
    """
    target_cwd = cwd or task.worktree_path or workspace.path
    if not target_cwd or not Path(target_cwd).exists():
        return None, f"Target checkout does not exist: {target_cwd}"

    rc_branch, branch, err_branch = await _run_git(target_cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if rc_branch != 0:
        return None, f"Failed to inspect git branch: {err_branch or 'unknown error'}"[:400]

    default_branch = (workspace.default_branch or "main").strip()
    if branch in PROTECTED_PUSH_BRANCHES or branch == default_branch:
        return (
            None,
            f"Refused to push protected or default branch '{branch or 'HEAD'}'",
        )

    rc_sha, sha, err_sha = await _run_git(target_cwd, "rev-parse", "HEAD")
    if rc_sha != 0 or not sha:
        return None, f"Failed to resolve HEAD commit SHA: {err_sha or 'unknown error'}"[:400]

    rc_push, _out_push, err_push = await _run_git(
        target_cwd,
        "push",
        "-u",
        "origin",
        branch,
        timeout=30.0,
    )
    if rc_push != 0:
        detail = err_push or f"git push exited with code {rc_push}"
        return None, detail[:400]

    return f"origin/{branch}@{sha[:7]}", None


def resolve_infowall_binary() -> str | None:
    """Locate the InfoWall CLI binary without reading any credential files."""
    env_bin = os.environ.get("INFOWALL_BIN", "").strip()
    if env_bin and Path(env_bin).is_file() and os.access(env_bin, os.X_OK):
        return env_bin
    if Path(DEFAULT_INFOWALL_BIN).is_file() and os.access(DEFAULT_INFOWALL_BIN, os.X_OK):
        return DEFAULT_INFOWALL_BIN
    found = shutil.which("infowall")
    return found


def _infowall_subprocess_env() -> dict[str, str]:
    """Construct subprocess env passing only the API key file path, never reading it."""
    env = dict(os.environ)
    env["INFOWALL_URL"] = env.get("INFOWALL_URL", "").strip() or DEFAULT_INFOWALL_URL
    env["INFOWALL_API_KEY_FILE"] = (
        env.get("INFOWALL_API_KEY_FILE", "").strip() or DEFAULT_INFOWALL_API_KEY_FILE
    )
    return env


def extract_canonical_links(
    summary: str,
    *,
    extra_urls: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """Extract and classify canonical HTTP(S) links for InfoWall progress entries."""
    raw_urls: list[str] = []
    for match in _URL_PATTERN.findall(summary or ""):
        cleaned = match.rstrip(".,;:!?")
        if cleaned and cleaned not in raw_urls:
            raw_urls.append(cleaned)
    for item in extra_urls or ():
        if not isinstance(item, str):
            continue
        for match in _URL_PATTERN.findall(item):
            cleaned = match.rstrip(".,;:!?")
            if cleaned and cleaned not in raw_urls:
                raw_urls.append(cleaned)

    links: list[dict[str, str]] = []
    for url in raw_urls:
        lower = url.lower()
        if "code.byted.org" in lower and "/merge_requests/" in lower:
            mr_num = url.rstrip("/").rsplit("/", 1)[-1]
            links.append(
                {
                    "kind": "mr",
                    "external_id": f"mr:{mr_num}",
                    "title": f"MR !{mr_num}" if mr_num.isdigit() else "Codebase MR",
                    "url": url,
                    "state": "open",
                    "dedupe_key": url,
                }
            )
        elif "feishu.cn" in lower or "larkoffice.com" in lower:
            links.append(
                {
                    "kind": "doc",
                    "external_id": url,
                    "title": "Feishu Document",
                    "url": url,
                    "state": "active",
                    "dedupe_key": url,
                }
            )
        elif "seed.bytedance" in lower or "merlin" in lower:
            links.append(
                {
                    "kind": "eval",
                    "external_id": url,
                    "title": "Seed / Merlin Link",
                    "url": url,
                    "state": "active",
                    "dedupe_key": url,
                }
            )
        else:
            links.append(
                {
                    "kind": "link",
                    "external_id": url,
                    "title": url,
                    "url": url,
                    "state": "active",
                    "dedupe_key": url,
                }
            )
    return links


async def read_infowall_demand_summary(demand_id: str) -> str | None:
    """Best-effort fetch of an InfoWall demand summary to enrich task delegation context."""
    cleaned_id = (demand_id or "").strip()
    if not cleaned_id:
        return None
    binary = resolve_infowall_binary()
    if not binary:
        return None
    env = _infowall_subprocess_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "demand",
            "get",
            cleaned_id,
            "--json",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except Exception:
        logger.debug("InfoWall demand get failed for demand_id=%s", cleaned_id, exc_info=True)
        return None

    if proc.returncode != 0 or not stdout_bytes:
        return None
    try:
        data = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    nested_demand = data.get("demand")
    demand: dict[str, Any] = nested_demand if isinstance(nested_demand, dict) else data
    title = str(demand.get("title") or "").strip()
    status = str(demand.get("status") or "").strip()
    priority = str(demand.get("priority") or "").strip()
    next_action = str(demand.get("next_action") or "").strip()
    description = str(demand.get("description") or "").strip()

    lines = [f"InfoWall Demand [{cleaned_id}]: {title or '(untitled)'}"]
    meta = [
        part
        for part in (
            f"priority={priority}" if priority else "",
            f"status={status}" if status else "",
        )
        if part
    ]
    if meta:
        lines[0] += f" ({', '.join(meta)})"
    if next_action:
        lines.append(f"Next action: {next_action}")
    if description:
        lines.append(f"Background: {description[:500]}")
    return "\n".join(lines)


async def sync_task_progress_to_infowall(
    task: WorkspaceTask,
    *,
    milestone: str,
    summary: str,
    links: Sequence[str | dict[str, Any]] | None = None,
) -> bool:
    """Idempotently append a task milestone progress entry to InfoWall via ``demand apply``."""
    demand_id = (task.infowall_demand_id or "").strip()
    if not demand_id:
        return False

    binary = resolve_infowall_binary()
    if not binary:
        return False

    env = _infowall_subprocess_env()
    try:
        health_proc = await asyncio.create_subprocess_exec(
            binary,
            "health",
            "--json",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        health_out, _ = await asyncio.wait_for(health_proc.communicate(), timeout=5.0)
        if health_proc.returncode != 0 or not health_out:
            return False
        health_data = json.loads(health_out.decode("utf-8", errors="replace"))
        if not isinstance(health_data, dict):
            return False
        if health_data.get("instance_role") != "primary" or health_data.get("read_only") is True:
            logger.warning(
                "Skipping InfoWall writeback for task_id=%s: instance_role=%r read_only=%r",
                task.id,
                health_data.get("instance_role"),
                health_data.get("read_only"),
            )
            return False
    except Exception:
        logger.debug("InfoWall health check failed for task_id=%s", task.id, exc_info=True)
        return False

    dedupe_key = f"claude-hub-pipeline:{task.id}:{milestone}"
    extra_urls: list[str] = []
    structured_links: list[dict[str, Any]] = []
    for item in links or ():
        if isinstance(item, str):
            extra_urls.append(item)
        elif isinstance(item, dict) and item.get("url"):
            structured_links.append(dict(item))
    for extracted in extract_canonical_links(summary, extra_urls=extra_urls):
        if not any(existing.get("url") == extracted["url"] for existing in structured_links):
            structured_links.append(extracted)

    branch_note = f" [pushed: {task.remote_pushed_ref}]" if task.remote_pushed_ref else ""
    clean_summary = (summary or "").strip() or f"Task {task.title} reached {milestone}."
    progress_text = f"[{milestone}] {task.title}{branch_note}: {clean_summary}"[:1500]

    payload = {
        "demands": [
            {
                "id": demand_id,
                "progress": [
                    {
                        "id": dedupe_key,
                        "text": progress_text,
                        "dedupe_key": dedupe_key,
                        "links": structured_links,
                    }
                ],
            }
        ]
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        apply_proc = await asyncio.create_subprocess_exec(
            binary,
            "demand",
            "apply",
            "--input",
            "-",
            "--json",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(
            apply_proc.communicate(input=payload_bytes), timeout=10.0
        )
        if apply_proc.returncode != 0:
            logger.warning(
                "InfoWall demand apply failed for task_id=%s demand_id=%s: %s",
                task.id,
                demand_id,
                err.decode("utf-8", errors="replace")[:300],
            )
            return False
        return True
    except Exception:
        logger.debug("InfoWall demand apply errored for task_id=%s", task.id, exc_info=True)
        return False
