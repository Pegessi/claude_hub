#!/usr/bin/env python3
"""Check candidate Git trees and new history against a private pattern catalog.

The JSON catalog is a mapping of descriptive labels to regular expressions. It
must live outside the repository. Matched content is never printed. The first
commit may remove inherited matches; later patches must be clean in both
additions and deletions. Binary blobs are searched as bytes too.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patterns", required=True, type=Path)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--base")
    scope.add_argument("--all-history", action="store_true")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    pattern_path = args.patterns.resolve()
    if pattern_path.is_relative_to(root):
        parser.error("the pattern catalog must be outside the worktree")
    definitions = json.loads(pattern_path.read_text(encoding="utf-8"))
    if not isinstance(definitions, dict) or not definitions:
        parser.error("the pattern catalog must be a nonempty JSON object")
    patterns = [
        (str(name), re.compile(value.encode("utf-8"), re.IGNORECASE))
        for name, value in definitions.items()
        if isinstance(value, str) and value
    ]
    if len(patterns) != len(definitions):
        parser.error("every pattern must be a nonempty string")
    if args.base:
        git("merge-base", "--is-ancestor", args.base, args.head)
    revision = args.head if args.all_history else f"{args.base}..{args.head}"
    commits = git("rev-list", "--reverse", "--topo-order", revision).decode().splitlines()
    if not commits:
        parser.error("candidate has no commits")
    if args.base:
        parent = git("rev-parse", args.base).decode().strip()
        for commit in commits:
            parents = git("rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
            if parents != [parent]:
                parser.error("candidate history must be linear and based directly on base")
            parent = commit
    failures = 0

    def check(data: bytes, scope: str) -> None:
        nonlocal failures
        for name, pattern in patterns:
            if pattern.search(data):
                print(f"{scope}: {name}")
                failures += 1

    batch = subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    stream_in, stream_out = batch.stdin, batch.stdout
    assert stream_in is not None and stream_out is not None

    blob_cache: dict[bytes, bytes] = {}

    def blob(oid: bytes) -> bytes:
        if oid in blob_cache:
            return blob_cache[oid]
        stream_in.write(oid + b"\n")
        stream_in.flush()
        header = stream_out.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise RuntimeError("unexpected git object response")
        data = stream_out.read(int(header[2]))
        if stream_out.read(1) != b"\n":
            raise RuntimeError("invalid git object framing")
        blob_cache[oid] = data
        return data

    clean_blobs: set[bytes] = set()
    for index, commit in enumerate(commits):
        check(git("show", "-s", "--format=fuller", commit), f"{commit}: metadata")
        entries = git("ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, kind, oid = header.split(b" ", 2)
            check(name, f"{commit}: path")
            if kind == b"blob" and oid not in clean_blobs:
                before = failures
                check(
                    blob(oid),
                    f"{commit}: blob {name.decode('utf-8', 'backslashreplace')}",
                )
                if failures == before:
                    clean_blobs.add(oid)
            elif mode == b"160000":
                print(
                    f"{commit}: submodule requires separate audit: {name.decode('utf-8', 'backslashreplace')}"
                )
                failures += 1
        patch = git(
            "diff-tree",
            "--root",
            "-m",
            "--no-commit-id",
            "-p",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=0",
            commit,
        )
        for line in patch.splitlines():
            if line.startswith((b"+++", b"---")):
                continue
            if line.startswith(b"+") or ((args.all_history or index) and line.startswith(b"-")):
                check(
                    line[1:],
                    f"{commit}: {'added' if line.startswith(b'+') else 'deleted'} line",
                )
    stream_in.close()
    if batch.wait() != 0:
        raise RuntimeError("git cat-file failed")
    print(f"Checked {len(commits)} commits; {failures} matching scopes")
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())
