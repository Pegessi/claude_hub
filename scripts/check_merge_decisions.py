#!/usr/bin/env python3
"""Check decision-record coverage for one immutable two-parent Git merge.

A zero exit does NOT prove semantic correctness: automatically merged semantic
errors are not detected, and later commits are not reviewed. Git must support
the probed merge-tree options and documented NUL output. Records bind the exact
Git version; inspect again after an upgrade. Shallow history is supported only
when every boundary is a known ancestor of the unique merge base. Temporary Git
objects are written, in an isolated bare repository with read-only source alternates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANUAL_KEYS = {"reason", "disposition", "destinations"}


class InspectionError(ValueError):
    pass


class CoverageError(ValueError):
    pass


def git_run(
    git: str,
    repo: Path,
    env: dict[str, str],
    *args: str,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [git, "--no-replace-objects", "-C", str(repo), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if result.returncode not in accepted:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise InspectionError(f"git {args[0]} exited {result.returncode}: {message}")
    return result


def oid(value: str, width: int) -> str:
    if re.fullmatch(rf"[0-9a-f]{{{width}}}", value) is None:
        raise InspectionError("Expected a complete lowercase object ID")
    return value


def path_text(value: bytes) -> str:
    text = value.decode("utf-8", "surrogateescape")
    if not text or text.startswith("/") or any(p in {"", ".", ".."} for p in text.split("/")):
        raise InspectionError("Unsupported path in Git output")
    return text


def nul_records(data: bytes) -> list[bytes]:
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise InspectionError("Expected NUL-terminated Git output")
    records = data[:-1].split(b"\0")
    if any(not item for item in records):
        raise InspectionError("Unexpected empty record in Git output")
    return records


def tree_entries(
    git: str, repo: Path, env: dict[str, str], tree: str, width: int
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    raw = git_run(git, repo, env, "ls-tree", "-r", "-z", tree).stdout
    for row in nul_records(raw):
        metadata, separator, name = row.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise InspectionError("Unknown ls-tree record")
        mode, kind, object_id = (item.decode("ascii") for item in fields)
        if (mode, kind) not in {
            ("100644", "blob"),
            ("100755", "blob"),
            ("120000", "blob"),
            ("160000", "commit"),
        }:
            raise InspectionError("Unknown tree entry mode/type")
        path = path_text(name)
        if path in result:
            raise InspectionError("Duplicate tree path")
        result[path] = {"mode": mode, "type": kind, "oid": oid(object_id, width)}
    return result


def inspect_merge(repo: Path, merge: str) -> tuple[dict[str, Any], set[str]]:
    executable = shutil.which("git")
    if executable is None:
        raise InspectionError("git is required")
    with tempfile.TemporaryDirectory(prefix="ch-merge-decisions-") as temporary:
        home = Path(temporary)
        # No inherited Git overrides, user config, attributes or loader variables.
        env = {
            "PATH": os.defpath,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        version = git_run(executable, repo, env, "--version").stdout.decode("ascii").strip()
        help_result = git_run(executable, repo, env, "merge-tree", "-h", accepted=(0, 129))
        help_text = help_result.stdout + help_result.stderr
        for option in (b"--write-tree", b"merge-base", b"name-only", b"messages", b"-z"):
            if option not in help_text:
                raise InspectionError(f"Required merge-tree option unavailable: {option!r}")
        object_format = (
            git_run(executable, repo, env, "rev-parse", "--show-object-format")
            .stdout.decode("ascii")
            .strip()
        )
        if object_format not in {"sha1", "sha256"}:
            raise InspectionError("Unsupported repository object format")
        width = 40 if object_format == "sha1" else 64
        oid(merge, width)
        common = git_run(
            executable, repo, env, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout
        if not common.endswith(b"\n") or b"\n" in common[:-1] or b"\r" in common:
            raise InspectionError("Unsupported common directory path")
        common_dir = Path(os.fsdecode(common[:-1]))
        objects = common_dir / "objects"
        try:
            shallow_bytes = (common_dir / "shallow").read_bytes()
        except FileNotFoundError:
            shallow_bytes = None
        shallow_boundaries: list[str] = []
        if shallow_bytes is not None:
            if not shallow_bytes or not shallow_bytes.endswith(b"\n"):
                raise InspectionError("Unknown shallow boundary file format")
            shallow_boundaries = [
                oid(line.decode("ascii"), width) for line in shallow_bytes[:-1].split(b"\n")
            ]
            shallow_boundaries = sorted(set(shallow_boundaries))
        if not objects.is_absolute() or not objects.is_dir():
            raise InspectionError("Cannot locate source objects")
        sandbox = home / "git"
        git_run(
            executable,
            home,
            env,
            "init",
            "--bare",
            "--template=",
            f"--object-format={object_format}",
            str(sandbox),
        )
        # New objects stay here; source refs, index, config and hooks are not copied.
        (sandbox / "objects" / "info" / "alternates").write_bytes(os.fsencode(objects) + b"\n")
        if shallow_bytes is not None:
            (sandbox / "shallow").write_bytes(shallow_bytes)
        if git_run(executable, sandbox, env, "cat-file", "-t", merge).stdout != b"commit\n":
            raise InspectionError("The merge object must be a commit")
        headers = git_run(executable, sandbox, env, "cat-file", "-p", merge).stdout
        lines = headers.split(b"\n\n", 1)[0].split(b"\n")
        trees = [oid(row[5:].decode("ascii"), width) for row in lines if row.startswith(b"tree ")]
        parents = [
            oid(row[7:].decode("ascii"), width) for row in lines if row.startswith(b"parent ")
        ]
        if len(trees) != 1 or len(parents) != 2 or parents[0] == parents[1]:
            raise InspectionError("Only ordinary, two-parent merge commits are supported")
        bases = (
            git_run(executable, sandbox, env, "merge-base", "--all", *parents, accepted=(0, 1))
            .stdout.decode("ascii")
            .splitlines()
        )
        if len(bases) != 1:
            raise InspectionError("Exactly one merge base is required")
        base = oid(bases[0], width)
        # Hidden ancestors below these boundaries are also ancestors of base,
        # so they cannot introduce a better or incomparable merge base.
        for boundary in shallow_boundaries:
            known_ancestor = git_run(
                executable,
                sandbox,
                env,
                "merge-base",
                "--is-ancestor",
                boundary,
                base,
                accepted=(0, 1),
            )
            if known_ancestor.returncode != 0:
                raise InspectionError("Shallow boundary is not a known ancestor of the merge base")
        native = git_run(
            executable,
            sandbox,
            env,
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            "-z",
            "--merge-base",
            base,
            *parents,
            accepted=(0, 1),
        )
        records = nul_records(native.stdout)
        if not records:
            raise InspectionError("Missing reconstructed tree")
        reconstructed = oid(records[0].decode("ascii"), width)
        conflicts = sorted({path_text(value) for value in records[1:]})
        # Some directory-rename conflicts have no file stages. Do not call them clean.
        if (native.returncode == 0 and conflicts) or (native.returncode == 1 and not conflicts):
            raise InspectionError("Unsupported merge-tree conflict output")
        merged_tree = trees[0]
        delta = git_run(
            executable,
            sandbox,
            env,
            "diff-tree",
            "-r",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-commit-id",
            reconstructed,
            merged_tree,
        ).stdout
        paths = sorted(set(conflicts) | {path_text(value) for value in nul_records(delta)})
        snapshots = {
            "base": tree_entries(executable, sandbox, env, base, width),
            "parent1": tree_entries(executable, sandbox, env, parents[0], width),
            "parent2": tree_entries(executable, sandbox, env, parents[1], width),
            "reconstructed": tree_entries(executable, sandbox, env, reconstructed, width),
            "merged": tree_entries(executable, sandbox, env, merged_tree, width),
        }
        report = {
            "schema": 1,
            "git_version": version,
            "merge": merge,
            "parents": parents,
            "base": base,
            "shallow_boundaries": shallow_boundaries,
            "tree": merged_tree,
            "reconstructed_tree": reconstructed,
            "conflicts": conflicts,
            "decisions": [
                {
                    "path": path,
                    **{label: entries.get(path) for label, entries in snapshots.items()},
                    "reason": "",
                    "disposition": "",
                    "destinations": [],
                }
                for path in paths
            ],
        }
        return report, set(snapshots["merged"])


def validate_record(current: dict[str, Any], merged_paths: set[str], record: Any) -> None:
    if not isinstance(record, dict) or set(record) != set(current):
        raise CoverageError("Record schema does not match the generated template")
    for key in current.keys() - {"decisions"}:
        if type(record[key]) is not type(current[key]) or record[key] != current[key]:
            raise CoverageError(f"Stale or modified generated field: {key}")
    rows = record["decisions"]
    expected = current["decisions"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise CoverageError("Decision paths are missing or extra")
    for row, required in zip(rows, expected):
        if not isinstance(row, dict) or set(row) != set(required):
            raise CoverageError("Decision schema does not match the template")
        for key in required.keys() - MANUAL_KEYS:
            if row[key] != required[key]:
                raise CoverageError(f"Stale or reordered decision: {required['path']}")
        if not isinstance(row["reason"], str) or not row["reason"].strip():
            raise CoverageError(f"Missing decision explanation: {row['path']}")
        disposition = row["disposition"]
        destinations = row["destinations"]
        if not isinstance(disposition, str):
            raise CoverageError("Disposition must be a string")
        if not isinstance(destinations, list) or any(not isinstance(p, str) for p in destinations):
            raise CoverageError("Destinations must be a list of merge-tree paths")
        if len(set(destinations)) != len(destinations):
            raise CoverageError("Duplicate destinations")
        if disposition == "transplant":
            if not destinations or any(
                p == row["path"] or p not in merged_paths for p in destinations
            ):
                raise CoverageError("Transplants need explicit existing destination paths")
        elif disposition == "delete":
            if row["merged"] is not None or destinations:
                raise CoverageError("Delete requires an absent result and no destinations")
        elif disposition in {"resolve", "retain-parent"}:
            if row["merged"] is None or destinations:
                raise CoverageError("Resolution requires a result path and no destinations")
            if disposition == "retain-parent" and row["merged"] not in (
                row["parent1"],
                row["parent2"],
            ):
                raise CoverageError("Retained object does not match either parent")
        else:
            raise CoverageError(f"Unknown or empty disposition: {row['path']}")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoverageError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "check"))
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--merge", required=True, help="complete immutable merge commit ID")
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    if args.command == "inspect" and args.record is not None:
        parser.error("inspect emits a template to stdout; it does not read a record")
    try:
        current, merged_paths = inspect_merge(args.repo.resolve(), args.merge)
        if args.command == "inspect":
            print(json.dumps(current, indent=2, ensure_ascii=True))
            return 0
        if args.record is None:
            if current["decisions"]:
                raise CoverageError("Unrecorded merge decisions; inspect and review a template")
        else:
            record = json.loads(
                args.record.read_text(encoding="utf-8"), object_pairs_hook=unique_object
            )
            validate_record(current, merged_paths, record)
        print(
            json.dumps(
                {
                    "merge": args.merge,
                    "decision_count": len(current["decisions"]),
                    "record_coverage": "complete",
                    "semantic_review": "not_verified",
                    "later_commits": "not_reviewed",
                }
            )
        )
        return 0
    except CoverageError as exc:
        print(f"UNCOVERED: {exc}", file=sys.stderr)
        return 1
    except (InspectionError, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"UNSUPPORTED_OR_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
