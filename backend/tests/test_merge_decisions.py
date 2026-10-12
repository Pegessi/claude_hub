"""Test merge-decision coverage using pytest-owned bare Git repositories."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_merge_decisions.py"


@pytest.fixture
def toy(tmp_path):
    git = shutil.which("git")
    assert git is not None, "Git is required for merge-decision tests"
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Toy",
        "GIT_AUTHOR_EMAIL": "toy@example.invalid",
        "GIT_COMMITTER_NAME": "Toy",
        "GIT_COMMITTER_EMAIL": "toy@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    repo = tmp_path / "repo.git"
    subprocess.run(
        [git, "init", "--bare", "--template=", str(repo)],
        env=env,
        check=True,
        capture_output=True,
        timeout=20,
    )

    class Toy:
        def git(self, *args, data=None):
            return subprocess.run(
                [git, "-C", str(repo), *args],
                input=data,
                env=env,
                check=True,
                capture_output=True,
                timeout=20,
            ).stdout

        def commit(self, files, parents=(), modes=None):
            rows = []
            for name, contents in sorted(files.items()):
                blob = self.git("hash-object", "-w", "--stdin", data=contents).strip()
                mode = (modes or {}).get(name, "100644")
                rows.append(mode.encode() + b" blob " + blob + b"\t" + name.encode() + b"\0")
            tree = self.git("mktree", "-z", data=b"".join(rows)).strip().decode()
            args = ["commit-tree", tree, "-m", "toy"]
            for parent in parents:
                args.extend(["-p", parent])
            return self.git(*args).strip().decode()

        def history(self, *, conflict=True, result=None):
            base_files = {"a": b"base\n", "b": b"base\n"}
            base = self.commit(base_files)
            left_files = {**base_files, "a": b"left\n"}
            right_files = {**base_files, "a" if conflict else "b": b"right\n"}
            left = self.commit(left_files, (base,))
            right = self.commit(right_files, (base,))
            if result is None:
                result = {
                    "a": b"resolved\n" if conflict else b"left\n",
                    "b": b"base\n" if conflict else b"right\n",
                }
            merge = self.commit(result, (left, right))
            return base, left, right, merge

        def call(self, merge, *, command="check", record=None, extra_env=None):
            args = [
                sys.executable,
                "-B",
                str(SCRIPT),
                command,
                "--repo",
                str(repo),
                "--merge",
                merge,
            ]
            if record is not None:
                path = tmp_path / "record.json"
                path.write_text(json.dumps(record), encoding="utf-8")
                args.extend(["--record", str(path)])
            return subprocess.run(
                args, env={**env, **(extra_env or {})}, capture_output=True, timeout=30
            )

        def inspect(self, merge):
            result = self.call(merge, command="inspect")
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        def covered(self, merge):
            record = self.inspect(merge)
            for row in record["decisions"]:
                row["reason"] = "Resolve the conflicting toy value in this fixed merge."
                row["disposition"] = "delete" if row["merged"] is None else "resolve"
            return record

        def snapshot(self):
            return {
                str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()
            }

    instance = Toy()
    instance.repo = repo
    instance.env = env
    instance.executable = git
    return instance


@pytest.mark.parametrize("conflict", [False, True])
def test_native_protocol_and_cli(toy, conflict):
    base, left, right, merge = toy.history(conflict=conflict)
    native = subprocess.run(
        [
            toy.executable,
            "-C",
            str(toy.repo),
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            "-z",
            "--merge-base",
            base,
            left,
            right,
        ],
        env=toy.env,
        capture_output=True,
        timeout=20,
    )
    assert native.returncode == int(conflict), native.stderr
    # Independent protocol oracle: successful output is only OID+NUL;
    # the conflict fixture adds precisely the pathname a+NUL.
    fields = native.stdout.split(b"\0")
    assert len(fields[0]) == len(base)
    int(fields[0], 16)
    assert fields[1:] == ([b"a", b""] if conflict else [b""])
    record = toy.inspect(merge)
    assert record["reconstructed_tree"] == fields[0].decode()
    assert record["conflicts"] == (["a"] if conflict else [])
    assert [row["path"] for row in record["decisions"]] == (["a"] if conflict else [])
    assert toy.call(merge).returncode == int(conflict)
    checked = toy.call(merge, record=toy.covered(merge))
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["semantic_review"] == "not_verified"


def test_unconflicted_extra_edit_is_a_decision(toy):
    *_, merge = toy.history(
        conflict=False,
        result={
            "a": b"left\n",
            "b": b"right\n",
            "extra": b"unplanned\n",
        },
    )
    assert toy.inspect(merge)["conflicts"] == []
    assert [r["path"] for r in toy.inspect(merge)["decisions"]] == ["extra"]
    assert toy.call(merge).returncode == 1


@pytest.mark.parametrize(
    "field",
    [
        "merge",
        "parents",
        "base",
        "tree",
        "reconstructed_tree",
        "git_version",
        "shallow_boundaries",
    ],
)
def test_stale_generated_identity_is_rejected(toy, field):
    *_, merge = toy.history()
    record = toy.covered(merge)
    record[field] = ["stale"] if field in {"parents", "shallow_boundaries"} else "stale"
    assert toy.call(merge, record=record).returncode == 1


@pytest.mark.parametrize("change", ["missing", "extra", "object", "reason", "unknown"])
def test_incomplete_or_modified_decision_is_rejected(toy, change):
    *_, merge = toy.history()
    record = toy.covered(merge)
    if change == "missing":
        record["decisions"] = []
    elif change == "extra":
        record["decisions"].append(dict(record["decisions"][0]))
    elif change == "object":
        record["decisions"][0]["merged"]["oid"] = "0" * 40
    elif change == "reason":
        record["decisions"][0]["reason"] = "  "
    else:
        record["decisions"][0]["disposition"] = {"unknown": True}
    assert toy.call(merge, record=record).returncode == 1


def test_delete_and_transplant_are_explicit(toy):
    *_, merge = toy.history(result={"b": b"base\n", "moved": b"resolved\n"})
    record = toy.covered(merge)
    assert toy.call(merge, record=record).returncode == 0
    deleted = next(row for row in record["decisions"] if row["path"] == "a")
    assert deleted["disposition"] == "delete"
    deleted.update(disposition="transplant", destinations=["moved"])
    assert toy.call(merge, record=record).returncode == 0
    deleted["destinations"] = ["not-in-merge-tree"]
    assert toy.call(merge, record=record).returncode == 1


def test_retain_parent_checks_the_actual_object(toy):
    *_, merge = toy.history(result={"a": b"left\n", "b": b"base\n"})
    record = toy.covered(merge)
    record["decisions"][0]["disposition"] = "retain-parent"
    assert toy.call(merge, record=record).returncode == 0
    *_, resolved = toy.history()
    record = toy.covered(resolved)
    record["decisions"][0]["disposition"] = "retain-parent"
    assert toy.call(resolved, record=record).returncode == 1


def test_later_commit_does_not_change_the_explicit_audit_target(toy):
    *_, merge = toy.history()
    record = toy.covered(merge)
    later = toy.commit({"later": b"not audited\n"}, (merge,))
    toy.git("update-ref", "refs/heads/main", later)
    result = toy.call(merge, record=record)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["later_commits"] == "not_reviewed"
    assert toy.call(later, record=record).returncode == 2
    assert toy.call("HEAD").returncode == 2


def test_binary_and_nonordinary_pathnames(toy):
    filename = "tab\tand\nnewline"
    base = toy.commit({filename: b"\0base"})
    left = toy.commit({filename: b"\0left"}, (base,))
    right = toy.commit({filename: b"\0right"}, (base,))
    merge = toy.commit({filename: b"\0resolved"}, (left, right))
    record = toy.covered(merge)
    assert [row["path"] for row in record["decisions"]] == [filename]
    assert toy.call(merge, record=record).returncode == 0


def test_source_repository_is_unchanged_and_env_driver_not_used(toy, tmp_path):
    marker = tmp_path / "driver-ran"
    attrs = {".gitattributes": b"a merge=tripwire\n"}
    base = toy.commit({**attrs, "a": b"base\n"})
    left = toy.commit({**attrs, "a": b"left\n"}, (base,))
    right = toy.commit({**attrs, "a": b"right\n"}, (base,))
    merge = toy.commit({**attrs, "a": b"resolved\n"}, (left, right))
    before = toy.snapshot()
    result = toy.call(
        merge,
        command="inspect",
        extra_env={
            "GIT_OBJECT_DIRECTORY": str(tmp_path / "must-not-be-used"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "merge.tripwire.driver",
            "GIT_CONFIG_VALUE_0": f"touch {marker}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert toy.snapshot() == before
    assert not marker.exists()


def test_unsupported_histories_fail_closed(toy):
    base, left, right, _ = toy.history()
    octopus = toy.commit({}, (base, left, right))
    assert toy.call(octopus).returncode == 2
    other = toy.commit({"unrelated": b"root"})
    unrelated = toy.commit({}, (left, other))
    assert toy.call(unrelated).returncode == 2
    cross_left = toy.commit({"a": b"cross-left"}, (left, right))
    cross_right = toy.commit({"a": b"cross-right"}, (right, left))
    crossed = toy.commit({}, (cross_left, cross_right))
    assert len(toy.git("merge-base", "--all", cross_left, cross_right).splitlines()) == 2
    assert toy.call(crossed).returncode == 2


@pytest.mark.parametrize("at_base", [False, True])
def test_shallow_boundaries_at_or_before_base_are_supported(toy, at_base):
    old = toy.commit({"a": b"old\n"})
    base = toy.commit({"a": b"base\n"}, (old,))
    left = toy.commit({"a": b"left\n"}, (base,))
    right = toy.commit({"a": b"right\n"}, (base,))
    merge = toy.commit({"a": b"resolved\n"}, (left, right))
    boundary = base if at_base else old
    shallow = toy.repo / "shallow"
    shallow.write_text(boundary + "\n", encoding="ascii")
    before = toy.snapshot()
    record = toy.covered(merge)
    assert record["base"] == base
    assert record["shallow_boundaries"] == [boundary]
    assert toy.call(merge, record=record).returncode == 0
    assert toy.snapshot() == before
    shallow.write_text((old if at_base else base) + "\n", encoding="ascii")
    assert toy.call(merge, record=record).returncode == 1


def test_repeated_shallow_markers_use_the_same_boundary_set(toy):
    base, left, right, merge = toy.history()
    shallow = toy.repo / "shallow"
    shallow.write_text(base + "\n", encoding="ascii")
    record = toy.covered(merge)
    shallow.write_text((base + "\n") * 6, encoding="ascii")
    # Git accepts repeated boundaries. Their multiplicity adds no ancestry.
    assert toy.git("merge-base", "--all", left, right).strip().decode() == base
    result = toy.call(merge, record=record)
    assert result.returncode == 0, result.stderr


def test_shallow_boundary_outside_base_ancestry_is_rejected(toy):
    base = toy.commit({"a": b"base\n"})
    unrelated = toy.commit({"aux": b"old\n"})
    boundary = toy.commit({"aux": b"new\n"}, (unrelated,))
    left = toy.commit({"a": b"left\n"}, (base, boundary))
    right = toy.commit({"a": b"right\n"}, (base,))
    merge = toy.commit({"a": b"resolved\n"}, (left, right))
    (toy.repo / "shallow").write_text(boundary + "\n", encoding="ascii")
    assert toy.git("merge-base", "--all", left, right).strip().decode() == base
    result = toy.call(merge)
    assert result.returncode == 2
    assert b"not a known ancestor" in result.stderr


def test_duplicate_json_keys_are_rejected():
    namespace = runpy.run_path(str(SCRIPT))
    with pytest.raises(namespace["CoverageError"], match="Duplicate JSON key"):
        json.loads('{"schema": 1, "schema": 1}', object_pairs_hook=namespace["unique_object"])


@pytest.mark.parametrize("destinations", [["a"], ["b", "b"], ["missing"], []])
def test_invalid_transplant_destinations_are_rejected(toy, destinations):
    *_, merge = toy.history()
    record = toy.covered(merge)
    record["decisions"][0].update(disposition="transplant", destinations=destinations)
    assert toy.call(merge, record=record).returncode == 1


def test_resolution_cannot_hide_transplant_destinations(toy):
    *_, merge = toy.history()
    record = toy.covered(merge)
    record["decisions"][0]["destinations"] = ["b"]
    assert toy.call(merge, record=record).returncode == 1


@pytest.mark.parametrize("failure", ["missing-option", "no-file-conflict", "unknown-output"])
def test_git_capability_and_output_failures_are_not_success(toy, monkeypatch, failure):
    namespace = runpy.run_path(str(SCRIPT))
    inspect = namespace["inspect_merge"]
    globals_ = inspect.__globals__
    original = globals_["git_run"]
    base, _, _, merge = toy.history()

    def fake(git, repo, env, *args, **kwargs):
        if args[:2] == ("merge-tree", "-h") and failure == "missing-option":
            return subprocess.CompletedProcess(args, 129, b"no supported options", b"")
        if args[:2] == ("merge-tree", "--write-tree"):
            output = base.encode() + b"\0"
            if failure == "unknown-output":
                output += b"\0unexpected-message\0"
            if failure != "missing-option":
                return subprocess.CompletedProcess(args, 1, output, b"")
        return original(git, repo, env, *args, **kwargs)

    monkeypatch.setitem(globals_, "git_run", fake)
    with pytest.raises(namespace["InspectionError"]):
        inspect(toy.repo, merge)
