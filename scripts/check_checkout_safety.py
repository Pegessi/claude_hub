"""Check protected repository content in the index or an immutable commit."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

PATHS = ("start.sh", "frontend/pnpm-workspace.yaml")
HOST = "${CLAUDE_HUB_HOST:-0.0.0.0}"
MODULE = "claude_hub.service_launcher"
# These select a different snapshot than the requested checkout. Reject rather
# than silently inspecting a default index while the caller intended another.
GIT_REDIRECTS = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_SHALLOW_FILE",
)


def git(root: Path, *args: str) -> bytes:
    redirected = [name for name in GIT_REDIRECTS if name in os.environ]
    if redirected:
        raise ValueError("unset Git snapshot redirects before checking: " + ", ".join(redirected))
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
        },
        timeout=30,
    ).stdout


def startup_errors(content: bytes) -> list[str]:
    try:
        source = content.decode("utf-8").replace("\\\n", " ")
        commands: list[tuple[list[str], list[str]]] = []
        for line in source.splitlines():
            if MODULE not in line:
                continue
            tokens = shlex.split(line, comments=True, posix=True)
            if MODULE in tokens:
                position = tokens.index(MODULE)
                if position > 0 and tokens[position - 1] == "-m":
                    commands.append((tokens, shlex.split(line, comments=True, posix=False)))
    except (UnicodeError, ValueError):
        return ["start.sh production command cannot be checked as UTF-8 shell text"]
    if len(commands) != 1:
        return ["start.sh must retain one checkable production service_launcher command"]
    tokens, quoted_tokens = commands[0]
    if tokens.count("--host") != 1:
        return ["start.sh production command must retain one explicit --host argument"]
    position = tokens.index("--host")
    if position + 1 >= len(tokens) or tokens[position + 1] != HOST:
        return ["start.sh production host must retain ${CLAUDE_HUB_HOST:-0.0.0.0}"]
    if ("--host", '"' + HOST + '"') not in zip(quoted_tokens, quoted_tokens[1:]):
        return ["start.sh production host expansion must remain double-quoted"]
    return []


def read_entries(root: Path, commit: str | None) -> tuple[str, dict[str, tuple[str, str]]]:
    entries: dict[str, tuple[str, str]] = {}
    if commit is None:
        label = "index"
        output = git(root, "ls-files", "--stage", "-z", "--", *PATHS)
    else:
        label = (
            git(root, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
            .decode("ascii")
            .strip()
        )
        output = git(root, "ls-tree", "-r", "-z", label, "--", *PATHS)
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if commit is None:
            mode, object_id, stage = fields
            if stage != "0":
                raise ValueError(f"unmerged protected index entry: {path}")
        else:
            mode, kind, object_id = fields
            if kind != "blob":
                raise ValueError(f"protected path is not a file: {path}")
        entries[path] = (mode, object_id)
    return label, entries


def check(root: Path, commit: str | None) -> tuple[str, list[str]]:
    label, entries = read_entries(root, commit)
    errors: list[str] = []
    if "frontend/pnpm-workspace.yaml" in entries:
        errors.append("frontend/pnpm-workspace.yaml is local-only and must not be committed")
    if "start.sh" in entries:
        mode, object_id = entries["start.sh"]
        if mode not in {"100644", "100755"}:
            errors.append("start.sh must remain a regular tracked script")
        else:
            errors.extend(startup_errors(git(root, "cat-file", "blob", object_id)))
    return label, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--commit")
    args = parser.parse_args()
    try:
        root = Path(git(args.repo, "rev-parse", "--show-toplevel").decode().strip())
        label, errors = check(root, args.commit)
    except ValueError as exc:
        print(f"Checkout safety failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("Checkout safety failed: cannot read the selected Git index/commit", file=sys.stderr)
        return 1
    for error in errors:
        print(f"Checkout safety failed: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"Checkout safety PASS: {label}")
    print("Scope: start.sh when present and frontend/pnpm-workspace.yaml; working tree not checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
