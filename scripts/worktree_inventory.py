#!/usr/bin/env python3
"""Read-only inventory of registered worktrees; never remove or repair anything."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Result:
    code: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    elapsed_ms: int = 0


class Runner:
    """Bound subprocess time and retained output; never expose raw diagnostics."""

    def __init__(self, timeout: float = 3, total_timeout: float = 60) -> None:
        self.timeout = timeout
        self.deadline = time.monotonic() + total_timeout
        self.env = os.environ.copy()
        self.env["GIT_OPTIONAL_LOCKS"] = "0"
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.env.pop(name, None)

    def run(self, args: list[str]) -> Result:
        started = time.monotonic()
        remaining = self.deadline - started
        if remaining <= 0:
            return Result(None, error="total_timeout")
        try:
            completed = subprocess.run(
                args, capture_output=True, timeout=min(self.timeout, remaining), env=self.env
            )
        except subprocess.TimeoutExpired:
            return Result(
                None, error="timeout", elapsed_ms=round((time.monotonic() - started) * 1000)
            )
        except OSError:
            return Result(None, error="unavailable")
        elapsed = round((time.monotonic() - started) * 1000)
        if len(completed.stdout) + len(completed.stderr) > 8 * 1024 * 1024:
            return Result(completed.returncode, error="output_limit", elapsed_ms=elapsed)
        return Result(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
            elapsed_ms=elapsed,
        )


def git(repo: Path, *args: str) -> list[str]:
    return [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-C",
        str(repo),
        *args,
    ]


def registered_worktrees(raw: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in raw.split("\0"):
        if not token:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = token.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


@dataclass
class Activity:
    # Raw argv is held only in memory; output contains matched PID/source pairs.
    sightings: list[tuple[str, int, str]]
    unknown: list[str]

    def matches(self, path: Path) -> list[dict[str, Any]]:
        matched: set[tuple[str, int]] = set()
        for source, pid, value in self.sightings:
            if source == "process_argv":
                occupied = str(path) in value  # Conservative: false positives retain.
            else:
                # lsof supplies kernel cwd paths. Do not stat arbitrary process
                # directories outside the repository just to normalize a path.
                occupied = Path(os.path.normpath(value)).is_relative_to(path)
            if occupied:
                matched.add((source, pid))
        return [{"source": source, "pid": pid} for source, pid in sorted(matched)]


def probe_activity(runner: Runner) -> Activity:
    sightings: list[tuple[str, int, str]] = []
    unknown: list[str] = []
    cwd = runner.run(["lsof", "-nP", "-a", "-d", "cwd", "-Fpn"])
    if cwd.error or cwd.code != 0 or cwd.stderr:
        unknown.append("process_cwd:" + (cwd.error or "incomplete"))
    else:
        pid = 0
        for line in cwd.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line == "fcwd":
                continue  # lsof emits the descriptor even with -Fpn.
            elif line.startswith("n") and pid and line[1:].startswith("/"):
                sightings.append(("process_cwd", pid, line[1:]))
            elif line:
                unknown.append("process_cwd:unparseable")
        if not any(source == "process_cwd" for source, _, _ in sightings):
            unknown.append("process_cwd:empty")
    argv = runner.run(["ps", "-axww", "-o", "pid=,command="])
    if argv.error or argv.code != 0 or argv.stderr:
        unknown.append("process_argv:" + (argv.error or "incomplete"))
    else:
        for line in argv.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[0].isdigit():
                sightings.append(("process_argv", int(fields[0]), fields[1]))
            elif line:
                unknown.append("process_argv:unparseable")
        if not any(source == "process_argv" for source, _, _ in sightings):
            unknown.append("process_argv:empty")
    panes = runner.run(
        ["tmux", "-L", "default", "list-panes", "-a", "-F", "#{pane_pid}\t#{pane_current_path}"]
    )
    no_server = panes.code == 1 and (
        "no server running" in panes.stderr
        or ("error connecting to" in panes.stderr and "No such file or directory" in panes.stderr)
    )
    if panes.error or (panes.code != 0 and not no_server):
        unknown.append("tmux_cwd:" + (panes.error or "incomplete"))
    elif not no_server:
        if panes.stderr:
            unknown.append("tmux_cwd:incomplete")
        for line in panes.stdout.splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[0].isdigit() and fields[1].startswith("/"):
                sightings.append(("tmux_cwd", int(fields[0]), fields[1]))
            elif line:
                unknown.append("tmux_cwd:unparseable")
    return Activity(sightings, sorted(set(unknown)))


def status_counts(raw: str) -> dict[str, int]:
    counts = {"tracked": 0, "untracked": 0, "ignored": 0}
    tokens = iter(raw.split("\0"))
    for token in tokens:
        if not token:
            continue
        code = token[:2]
        counts[{"??": "untracked", "!!": "ignored"}.get(code, "tracked")] += 1
        if "R" in code or "C" in code:
            next(tokens, None)  # Porcelain -z gives a second pathname for renames.
    return counts


def inventory(
    repo: Path,
    root: Path,
    *,
    base: str = "main",
    runner: Runner | None = None,
    activity: Activity | None = None,
) -> dict[str, Any]:
    runner = runner or Runner()
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": timestamp(),
        "repo": str(repo),
        "worktree_root": str(root),
        "base": base,
        "items": [],
        "errors": [],
        "read_only": True,
        "limitations": [
            "Snapshot of visible processes and default tmux server only; recheck before any cleanup.",
            "Candidate means manual review only; no files, branches, sessions or registrations are removed.",
        ],
    }
    listing = runner.run(git(repo, "worktree", "list", "--porcelain", "-z"))
    if listing.error or listing.code != 0 or listing.stderr:
        report["errors"].append("worktree_list:" + (listing.error or "failed"))
        return report
    entries = registered_worktrees(listing.stdout)
    if not entries or any("worktree" not in entry for entry in entries):
        report["errors"].append("worktree_list:unparseable")
        return report
    base_result = runner.run(
        git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    )
    base_sha = (
        base_result.stdout.strip()
        if not base_result.error and base_result.code == 0 and not base_result.stderr
        else None
    )
    report["base_sha"] = base_sha
    activity = activity or probe_activity(runner)
    report["activity_unknown"] = activity.unknown
    for index, entry in enumerate(entries):
        path = Path(entry.get("worktree", ""))
        reasons: list[str] = []
        unknown: list[str] = []
        item: dict[str, Any] = {
            "path": str(path),
            "head": entry.get("HEAD"),
            "branch": entry.get("branch"),
            "checked_at": timestamp(),
            "classification": "retain",
            "reasons": reasons,
            "unknown": unknown,
            "checks": {},
            "occupants": [],
        }
        report["items"].append(item)
        if index == 0:
            reasons.append("primary")
        if "locked" in entry:
            reasons.append("locked")
        if "prunable" in entry:
            reasons.append("prunable")
        if "bare" in entry:
            reasons.append("bare")
        try:
            if not path.is_absolute() or path.parent != root:
                reasons.append("outside_canonical_root")
            elif path.is_symlink() or root.is_symlink() or path.resolve().parent != root.resolve():
                reasons.append("symlink_or_noncanonical_path")
            elif not path.is_dir():
                unknown.append("directory_unavailable")
        except (OSError, RuntimeError):
            unknown.append("path_resolution_failed")
        if reasons or unknown:
            continue  # Never inspect primary, outside-root or protected directories.
        item["occupants"] = activity.matches(path.resolve())
        if item["occupants"]:
            reasons.append("occupied")
        unknown.extend(activity.unknown)
        status = runner.run(
            git(
                path,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=normal",
                "--ignored=matching",
            )
        )
        item["checks"]["status"] = {"elapsed_ms": status.elapsed_ms}
        if status.error or status.code != 0 or status.stderr:
            unknown.append("git_status:" + (status.error or "failed"))
        else:
            counts = status_counts(status.stdout)
            item["checks"]["status"].update(counts)
            reasons.extend(f"{kind}_content" for kind, count in counts.items() if count)
        head = runner.run(git(path, "rev-parse", "--verify", "HEAD"))
        if head.error or head.code != 0 or head.stderr:
            unknown.append("head_check:" + (head.error or "failed"))
        elif head.stdout.strip() != entry.get("HEAD"):
            unknown.append("head_changed_during_inventory")
        if not base_sha or not entry.get("HEAD"):
            unknown.append("merge_base_unavailable")
        else:
            merged = runner.run(git(repo, "merge-base", "--is-ancestor", entry["HEAD"], base_sha))
            item["checks"]["merged"] = {"elapsed_ms": merged.elapsed_ms}
            if merged.error or merged.code not in (0, 1) or merged.stderr:
                unknown.append("merge_check:" + (merged.error or "failed"))
            else:
                item["checks"]["merged"]["is_ancestor"] = merged.code == 0
                if merged.code == 1:
                    reasons.append("unmerged_head")
        if not reasons and not unknown:
            item["classification"] = "candidate_for_manual_review"
            reasons.append("merged_clean_no_observed_occupancy")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, required=True, help="Explicit repository; no discovery scan"
    )
    parser.add_argument(
        "--base", default="main", help="Local integration ref (default: main); never fetched"
    )
    parser.add_argument(
        "--timeout", type=float, default=3, help="Per-command timeout, 0 < seconds <= 30"
    )
    parser.add_argument(
        "--total-timeout", type=float, default=60, help="Whole-probe budget, 0 < seconds <= 600"
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable evidence and reasons")
    args = parser.parse_args()
    if not 0 < args.timeout <= 30 or not 0 < args.total_timeout <= 600:
        parser.error("timeout must be in (0, 30] and total-timeout in (0, 600]")
    repo = args.repo.expanduser().absolute()
    report = inventory(
        repo,
        Path.home() / "claude_hub_worktree",
        base=args.base,
        runner=Runner(args.timeout, args.total_timeout),
    )
    report["timeout_seconds"] = args.timeout
    report["total_timeout_seconds"] = args.total_timeout
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"Read-only inventory at {report['checked_at']} (base {args.base}; "
            f"command timeout {args.timeout}s, total budget {args.total_timeout}s)"
        )
        for item in report["items"]:
            reasons = ", ".join(item["reasons"])
            unknown = ", ".join(item["unknown"]) or "none"
            print(
                f"{item['checked_at']} {item['classification']}: {item['path']} "
                f"[{reasons}; unknown={unknown}]"
            )
        for error in report["errors"]:
            print(f"unknown: {error}")
        print("Candidates require fresh manual checks. Nothing was removed.")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
