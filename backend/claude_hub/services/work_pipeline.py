"""Work Standard Pipeline Engine: isolated feature worktrees, safe remote push, and InfoWall writeback."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from claude_hub.models import ExecutionTarget, Workspace, WorkspaceTask
from claude_hub.services.local_runtime_settings import local_setting

logger = logging.getLogger(__name__)

DEFAULT_INFOWALL_BIN = "/home/tiger/.local/opt/infowall/current/infowall"
DEFAULT_INFOWALL_URL = "http://127.0.0.1:18899"
DEFAULT_INFOWALL_API_KEY_FILE = "/home/tiger/.local/state/infowall/api-key"

PROTECTED_PUSH_BRANCHES = frozenset({"main", "master", "develop", "HEAD", ""})
SUBPROCESS_STREAM_LIMIT = 32 * 1024 * 1024
INFOWALL_SUBPROCESS_TIMEOUT_SECONDS = 20.0
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"

_URL_RE = re.compile(r"https://[^\s<>()\[\]\"'`]+")
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slugify_task_title(title: str, max_len: int = 32) -> str:
    lowered = _SLUG_NON_ALNUM_RE.sub("-", (title or "").strip().lower()).strip("-")
    if not lowered:
        lowered = "task"
    return lowered[:max_len].strip("-") or "task"


def _task_worktree_slug(task: WorkspaceTask) -> str:
    prefix = _slugify_task_title(task.title, max_len=32)
    short_id = re.sub(r"[^a-z0-9]", "", (task.id or "").lower())[:8] or "00000000"
    return f"{prefix}-{short_id}"


class _SubprocessOutputOverflow(RuntimeError):
    """Raised when subprocess stdout+stderr exceeds the configured byte cap."""


async def _read_stream_bounded(
    stream: asyncio.StreamReader | None,
    *,
    max_bytes: int,
    state: dict[str, int],
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        state["total_bytes"] += len(chunk)
        if state["total_bytes"] > max_bytes:
            raise _SubprocessOutputOverflow(f"subprocess output exceeded {max_bytes} bytes cap")
        chunks.append(chunk)
    return b"".join(chunks)


async def _communicate_bounded(
    proc: asyncio.subprocess.Process,
    *,
    stdin_bytes: bytes | None,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    if proc.stdin is not None:
        try:
            if stdin_bytes:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()
    state = {"total_bytes": 0}
    stdout_task = asyncio.create_task(
        _read_stream_bounded(proc.stdout, max_bytes=max_output_bytes, state=state)
    )
    stderr_task = asyncio.create_task(
        _read_stream_bounded(proc.stderr, max_bytes=max_output_bytes, state=state)
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
    finally:
        for reader_task in (stdout_task, stderr_task):
            if not reader_task.done():
                reader_task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    await proc.wait()
    return stdout_bytes, stderr_bytes


async def _run_bounded_subprocess(
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin_bytes: bytes | None = None,
    timeout: float = 20.0,
    max_output_bytes: int = SUBPROCESS_STREAM_LIMIT,
) -> tuple[int, str, str]:
    """Run a subprocess in its own process group with bounded output, timeout, and reaping."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=(asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=65536,
        start_new_session=True,
    )
    needs_pg_kill = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            _communicate_bounded(
                proc,
                stdin_bytes=stdin_bytes,
                max_output_bytes=max_output_bytes,
            ),
            timeout=timeout,
        )
        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout_bytes.decode("utf-8", errors="replace").strip(),
            stderr_bytes.decode("utf-8", errors="replace").strip(),
        )
    except _SubprocessOutputOverflow as exc:
        needs_pg_kill = True
        return 1, "", str(exc)
    except asyncio.TimeoutError:
        needs_pg_kill = True
        return 124, "", f"command timed out after {int(timeout)}s: {' '.join(args[:3])}"
    except BaseException:
        needs_pg_kill = True
        raise
    finally:
        if needs_pg_kill or proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                proc.kill()
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        transport = getattr(proc, "_transport", None)
        if needs_pg_kill and transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2.0)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


async def _run_git(
    args: list[str],
    *,
    cwd: str,
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return await _run_bounded_subprocess(
            ["git", *args],
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
    except OSError as exc:
        return 1, "", str(exc)


async def _resolve_canonical_git_repo_root(workspace_path: str) -> Path:
    """Resolve the canonical primary Git repository root without blocking the event loop."""
    candidate = Path(workspace_path).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        raise RuntimeError(f"Workspace path does not exist or is not a directory: {workspace_path}")
    resolved = candidate.resolve()
    rc, common_out, err = await _run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(resolved),
        timeout=10.0,
    )
    if rc != 0 or not common_out:
        detail = err or common_out or "not a git repository"
        raise RuntimeError(
            f"Workspace path '{workspace_path}' is not a valid Git repository ({detail})"
        )
    common_dir = Path(common_out.splitlines()[0].strip()).resolve()
    if common_dir.name == ".git":
        return common_dir.parent
    rc_top, top_out, _ = await _run_git(
        ["rev-parse", "--path-format=absolute", "--show-toplevel"],
        cwd=str(resolved),
        timeout=10.0,
    )
    if rc_top == 0 and top_out:
        return Path(top_out.splitlines()[0].strip()).resolve()
    return resolved


async def _is_valid_git_worktree(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    rc, out, _ = await _run_git(
        ["rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        timeout=10.0,
    )
    return rc == 0 and out.strip().lower() == "true"


async def is_confirmed_non_git_directory(workspace_path: str) -> bool:
    """Return True only when ``workspace_path`` is provably a plain non-Git directory.

    Timeouts, permission errors, safe.directory errors, Git env overrides, and
    broken/corrupted ``.git`` or bare-repo markers return False so callers keep
    isolation fail-closed instead of writing into a primary checkout.
    """
    if any(os.environ.get(key) for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")):
        return False
    try:
        directory = Path(workspace_path).expanduser().resolve(strict=True)
    except OSError:
        return False
    if not directory.is_dir():
        return False
    rc, _, error = await _run_bounded_subprocess(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(directory),
        env={**os.environ, "LC_ALL": "C"},
        timeout=10.0,
    )
    if rc != 128 or not error.startswith("fatal: not a git repository"):
        return False
    for parent in (directory, *directory.parents):
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            pass
        else:
            return False
        try:
            (parent / "HEAD").lstat()
            (parent / "objects").lstat()
        except FileNotFoundError:
            pass
        else:
            return False
    return True


async def provision_task_worktree(
    workspace: Workspace,
    task: WorkspaceTask,
) -> tuple[str, str]:
    """Create or reuse an isolated Git feature worktree for a local task.

    Fails closed with ``RuntimeError`` when ``workspace.target == LOCAL`` and
    a valid Git worktree cannot be provisioned or verified.
    """
    if workspace.target != ExecutionTarget.LOCAL:
        raise RuntimeError("Isolated git worktree provisioning requires a local workspace")

    slug = _task_worktree_slug(task)
    branch_name = task.worktree_branch or f"feat/{slug}"

    if task.worktree_path:
        existing = Path(task.worktree_path)
        if await _is_valid_git_worktree(existing):
            return str(existing.resolve()), branch_name

    repo_root = await _resolve_canonical_git_repo_root(workspace.path)
    worktree_root = repo_root.parent / f"{repo_root.name}_worktree"
    worktree_dir = worktree_root / slug

    if worktree_dir.exists():
        if await _is_valid_git_worktree(worktree_dir):
            rc, current_branch, _ = await _run_git(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(worktree_dir),
                timeout=10.0,
            )
            resolved_branch = current_branch if rc == 0 and current_branch else branch_name
            return str(worktree_dir.resolve()), resolved_branch
        raise RuntimeError(
            f"Worktree path '{worktree_dir}' already exists but is not a valid Git worktree"
        )

    try:
        worktree_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to create worktree root directory '{worktree_root}': {exc}"
        ) from exc

    base_ref = (workspace.default_branch or "main").strip() or "main"
    rc_verify, _, _ = await _run_git(
        ["rev-parse", "--verify", base_ref],
        cwd=str(repo_root),
        timeout=10.0,
    )
    if rc_verify != 0:
        base_ref = "HEAD"

    rc_branch, _, _ = await _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=str(repo_root),
        timeout=10.0,
    )
    if rc_branch == 0:
        cmd = ["worktree", "add", str(worktree_dir), branch_name]
    else:
        cmd = ["worktree", "add", "-b", branch_name, str(worktree_dir), base_ref]

    rc_add, out_add, err_add = await _run_git(cmd, cwd=str(repo_root), timeout=20.0)
    if rc_add != 0 or not await _is_valid_git_worktree(worktree_dir):
        detail = (err_add or out_add or f"exit {rc_add}")[:300]
        raise RuntimeError(
            f"Failed to provision isolated git worktree at '{worktree_dir}' "
            f"on branch '{branch_name}': {detail}"
        )

    return str(worktree_dir.resolve()), branch_name


async def push_task_feature_branch(
    workspace: Workspace,
    task: WorkspaceTask,
    *,
    cwd: str | None = None,
    before_push: Callable[[], bool] | None = None,
) -> tuple[str | None, str | None]:
    """Push the task's reviewed commit on its feature branch to origin (never protected/default, never --force).

    Returns ``(remote_pushed_ref, remote_push_error)``. Pushes the immutable
    ``<head_sha>:refs/heads/<branch>`` refspec and checks ``before_push`` after
    sampling ``HEAD`` so a concurrent reopen into a new review round can never
    publish an unreviewed commit or record a mismatched SHA.
    """
    if workspace.target != ExecutionTarget.LOCAL:
        return None, "Remote branch push is only supported for local workspaces"

    if before_push is not None and not before_push():
        return task.remote_pushed_ref, task.remote_push_error

    target_cwd = cwd or task.worktree_path or workspace.path
    if not target_cwd or not Path(target_cwd).exists():
        return task.remote_pushed_ref, f"Worktree path does not exist: {target_cwd}"

    rc_branch, branch_out, err_branch = await _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=target_cwd,
        timeout=10.0,
    )
    if rc_branch != 0:
        return (
            task.remote_pushed_ref,
            f"git rev-parse --abbrev-ref HEAD failed: {err_branch or branch_out}"[:300],
        )

    branch = branch_out.strip()
    default_branch = (workspace.default_branch or "main").strip()
    if branch in PROTECTED_PUSH_BRANCHES or branch == default_branch:
        return (
            task.remote_pushed_ref,
            f"Refused to push protected or default branch '{branch}'",
        )

    rc_sha, sha_out, err_sha = await _run_git(
        ["rev-parse", "HEAD"],
        cwd=target_cwd,
        timeout=10.0,
    )
    if rc_sha != 0 or not sha_out:
        return (
            task.remote_pushed_ref,
            f"git rev-parse HEAD failed: {err_sha or sha_out}"[:300],
        )
    if before_push is not None and not before_push():
        return task.remote_pushed_ref, task.remote_push_error

    head_sha = sha_out.strip()
    expected_ref = f"origin/{branch}@{head_sha[:7]}"
    if task.remote_pushed_ref == expected_ref:
        return expected_ref, None

    rc_push, out_push, err_push = await _run_git(
        ["push", "-u", "origin", f"{head_sha}:refs/heads/{branch}"],
        cwd=target_cwd,
        timeout=30.0,
    )
    if rc_push != 0:
        detail = (err_push or out_push or f"git push exited with {rc_push}")[:300]
        return task.remote_pushed_ref, detail

    return expected_ref, None


def _resolve_infowall_bin() -> str | None:
    env_bin = os.environ.get("INFOWALL_BIN", "").strip()
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        found_env = shutil.which(env_bin)
        if found_env:
            return found_env
    if os.path.isfile(DEFAULT_INFOWALL_BIN) and os.access(DEFAULT_INFOWALL_BIN, os.X_OK):
        return DEFAULT_INFOWALL_BIN
    return shutil.which("infowall")


def _is_loopback_infowall_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _infowall_env() -> dict[str, str]:
    """Build subprocess environment for InfoWall CLI without reading the API key file in Python."""
    env = dict(os.environ)
    infowall_url = env.get("INFOWALL_URL", "").strip() or DEFAULT_INFOWALL_URL
    env["INFOWALL_URL"] = infowall_url
    if not env.get("INFOWALL_API_KEY_FILE", "").strip():
        env["INFOWALL_API_KEY_FILE"] = DEFAULT_INFOWALL_API_KEY_FILE
    if _is_loopback_infowall_url(infowall_url):
        env["NO_PROXY"] = LOOPBACK_NO_PROXY
        env["no_proxy"] = LOOPBACK_NO_PROXY
    return env


def _classify_link_kind(url: str) -> str:
    lowered = url.lower()
    if "/merge_requests/" in lowered or "/pull/" in lowered:
        return "mr"
    if (
        "larkoffice.com" in lowered
        or "feishu.cn" in lowered
        or "/docx/" in lowered
        or "/wiki/" in lowered
    ):
        return "doc"
    markers = local_setting("CLAUDE_HUB_EVALUATION_LINK_MARKERS", "merlin,arena")
    if any(marker.strip().lower() in lowered for marker in markers.split(",") if marker.strip()):
        return "eval"
    return "link"


def extract_canonical_links(text: str, extra_urls: list[str] | None = None) -> list[dict[str, str]]:
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    candidates: list[str] = []
    if text:
        candidates.extend(_URL_RE.findall(text))
    if extra_urls:
        for item in extra_urls:
            if isinstance(item, str) and item.strip().startswith("https://"):
                candidates.append(item.strip())
    for raw in candidates:
        cleaned = raw.rstrip(".,;:!?)")
        if not cleaned.startswith("https://") or cleaned in seen:
            continue
        seen.add(cleaned)
        kind = _classify_link_kind(cleaned)
        links.append(
            {
                "kind": kind,
                "label": kind.upper() if kind == "mr" else kind.capitalize(),
                "url": cleaned,
            }
        )
    return links


async def _check_infowall_writable(infowall_bin: str, env: dict[str, str]) -> bool:
    try:
        rc, out, _ = await _run_bounded_subprocess(
            [infowall_bin, "health", "--json"],
            env=env,
            timeout=10.0,
        )
    except Exception:
        return False
    if rc != 0 or not out:
        return False
    try:
        payload = json.loads(out)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    role = payload.get("instance_role")
    read_only = payload.get("read_only")
    if role is not None and role != "primary":
        return False
    if read_only is True:
        return False
    return True


async def read_infowall_demand_summary(demand_id: str) -> str | None:
    """Best-effort fetch of an InfoWall demand's title, priority/status, and next_action."""
    cleaned_id = (demand_id or "").strip()
    if not cleaned_id:
        return None
    infowall_bin = _resolve_infowall_bin()
    if not infowall_bin:
        return None
    env = _infowall_env()
    try:
        rc, out, _ = await _run_bounded_subprocess(
            [infowall_bin, "demand", "get", cleaned_id, "--json"],
            env=env,
            timeout=INFOWALL_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.debug("InfoWall demand get failed for demand_id=%s", cleaned_id, exc_info=True)
        return None

    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
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
    links: list[str] | None = None,
) -> bool:
    """Idempotently write task progress back to InfoWall via ``infowall demand apply``."""
    demand_id = (task.infowall_demand_id or "").strip()
    if not demand_id:
        return False

    infowall_bin = _resolve_infowall_bin()
    if not infowall_bin:
        logger.info(
            "Skipping InfoWall sync for task_id=%s demand_id=%s: infowall binary not found",
            task.id,
            demand_id,
        )
        return False

    env = _infowall_env()
    if not await _check_infowall_writable(infowall_bin, env):
        logger.info(
            "Skipping InfoWall sync for task_id=%s demand_id=%s: InfoWall primary not writable",
            task.id,
            demand_id,
        )
        return False

    dedupe_key = f"claude-hub-pipeline:{task.id}:{milestone}"
    progress_id = f"prog-{hashlib.sha1(dedupe_key.encode('utf-8')).hexdigest()[:12]}"

    summary_clean = " ".join((summary or "").strip().split())
    if len(summary_clean) > 360:
        summary_clean = summary_clean[:357] + "..."

    parts = [f"[Claude Hub {milestone}] {task.title}"]
    if task.remote_pushed_ref:
        parts.append(f"branch={task.remote_pushed_ref}")
    if summary_clean:
        parts.append(summary_clean)
    progress_text = " — ".join(parts)

    structured_links = extract_canonical_links(summary or "", extra_urls=links)
    progress_entry: dict[str, Any] = {
        "id": progress_id,
        "text": progress_text,
        "dedupe_key": dedupe_key,
        "source": "claude_hub_pipeline",
    }
    if structured_links:
        progress_entry["links"] = structured_links

    apply_payload = {
        "demands": [
            {
                "id": demand_id,
                "progress": [progress_entry],
            }
        ]
    }
    stdin_bytes = json.dumps(apply_payload, ensure_ascii=False).encode("utf-8")

    try:
        rc, out, err = await _run_bounded_subprocess(
            [infowall_bin, "demand", "apply", "--input", "-", "--json"],
            env=env,
            stdin_bytes=stdin_bytes,
            timeout=INFOWALL_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "InfoWall demand apply failed for task_id=%s demand_id=%s",
            task.id,
            demand_id,
            exc_info=True,
        )
        return False

    if rc != 0:
        logger.warning(
            "InfoWall demand apply exited %s for task_id=%s demand_id=%s: %s",
            rc,
            task.id,
            demand_id,
            (err or out)[:300],
        )
        return False
    return True
