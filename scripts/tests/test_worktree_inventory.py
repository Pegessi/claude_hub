"""Synthetic Git repositories only; never registers a Claude Hub worktree."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "worktree_inventory.py"
SPEC = importlib.util.spec_from_file_location("worktree_inventory", SCRIPT)
assert SPEC and SPEC.loader
inventory_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory_module
SPEC.loader.exec_module(inventory_module)
Activity = inventory_module.Activity
Result = inventory_module.Result
Runner = inventory_module.Runner


class SelectiveRunner(Runner):
    def __init__(self, probe: str, result: Result) -> None:
        super().__init__()
        self.probe = probe
        self.result = result

    def run(self, args: list[str]) -> Result:
        if self.probe in args:
            return self.result
        return super().run(args)


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-worktree-inventory-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.repo = self.directory / "repo"
        self.root = self.directory / "worktrees"
        self.root.mkdir()
        self.git("init", "-b", "main", str(self.repo), cwd=self.directory)
        (self.repo / "tracked.txt").write_text("baseline\n")
        (self.repo / ".gitignore").write_text("ignored/\n")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Inventory Test",
                "-c",
                "user.email=test@example.invalid",
                "-C",
                str(cwd or self.repo),
                *args,
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()

    def worktree(self, name: str, *, outside: bool = False) -> Path:
        path = (self.directory if outside else self.root) / name
        self.git("worktree", "add", "-b", name, str(path))
        return path

    def inspect(self, path: Path, *, activity=None, runner=None) -> dict:
        report = inventory_module.inventory(
            self.repo,
            self.root,
            activity=activity or Activity([], []),
            runner=runner,
        )
        return next(item for item in report["items"] if item["path"] == str(path))

    def test_merged_clean_worktree_is_only_a_manual_candidate(self) -> None:
        worktree = self.worktree("merged")
        item = self.inspect(worktree)
        self.assertEqual(item["classification"], "candidate_for_manual_review")
        self.assertTrue(item["checks"]["merged"]["is_ancestor"])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(self.git("branch", "--list", "merged"), "+ merged")
        primary = self.inspect(self.repo)
        self.assertEqual(primary["reasons"], ["primary", "outside_canonical_root"])
        self.assertEqual(primary["checks"], {})

    def test_dirty_untracked_and_ignored_content_are_all_retained(self) -> None:
        worktree = self.worktree("content")
        (worktree / "tracked.txt").write_text("changed\n")
        (worktree / "untracked").write_text("protected\n")
        (worktree / "ignored").mkdir()
        (worktree / "ignored/data").write_text("protected\n")
        item = self.inspect(worktree)
        self.assertEqual(item["classification"], "retain")
        self.assertEqual(
            set(item["reasons"]), {"tracked_content", "untracked_content", "ignored_content"}
        )

    def test_unmerged_head_is_retained(self) -> None:
        worktree = self.worktree("unmerged")
        (worktree / "tracked.txt").write_text("feature\n")
        self.git("commit", "-am", "feature", cwd=worktree)
        self.assertIn("unmerged_head", self.inspect(worktree)["reasons"])

    def test_outside_root_and_locked_are_not_inspected(self) -> None:
        outside = self.worktree("outside", outside=True)
        locked = self.worktree("locked")
        self.git("worktree", "lock", str(locked))
        for path, reason in ((outside, "outside_canonical_root"), (locked, "locked")):
            item = self.inspect(path)
            self.assertIn(reason, item["reasons"])
            self.assertEqual(item["checks"], {})

    def test_registered_symlink_is_preserved_without_git_status(self) -> None:
        target = self.directory / "target"
        target.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        listing = self.git("worktree", "list", "--porcelain", "-z")
        raw = listing.rstrip("\0") + "\0\0worktree " + str(alias) + "\0HEAD abc\0\0"
        item = self.inspect(alias, runner=SelectiveRunner("list", Result(0, raw)))
        self.assertIn("symlink_or_noncanonical_path", item["reasons"])
        self.assertEqual(item["checks"], {})

    def test_prunable_and_missing_paths_are_preserved(self) -> None:
        path = self.root / "missing"
        listing = self.git("worktree", "list", "--porcelain", "-z")
        for flags, reason in (("prunable missing\0", "prunable"), ("", "directory_unavailable")):
            raw = listing.rstrip("\0") + f"\0\0worktree {path}\0HEAD abc\0{flags}\0"
            item = self.inspect(path, runner=SelectiveRunner("list", Result(0, raw)))
            self.assertIn(reason, [*item["reasons"], *item["unknown"]])
            self.assertEqual(item["checks"], {})

    def test_cwd_argv_and_tmux_occupancy_only_expose_pid_and_source(self) -> None:
        path = self.worktree("occupied")
        for source, value in (
            ("process_cwd", str(path / "subdir")),
            ("process_argv", f"server --repo={path} --token=never-print-this"),
            ("tmux_cwd", str(path)),
        ):
            activity = Activity([(source, 123, value)], [])
            item = self.inspect(path, activity=activity)
            self.assertIn("occupied", item["reasons"])
            self.assertEqual(item["occupants"], [{"source": source, "pid": 123}])
            self.assertNotIn("never-print-this", json.dumps(item))

    def test_unknown_probe_never_becomes_candidate(self) -> None:
        path = self.worktree("unknown")
        item = self.inspect(path, activity=Activity([], ["process_cwd:timeout"]))
        self.assertEqual(item["classification"], "retain")
        self.assertIn("process_cwd:timeout", item["unknown"])

    def test_failed_and_timed_out_git_checks_never_become_candidates(self) -> None:
        path = self.worktree("errors")
        for result, reason in (
            (Result(128, stderr="private diagnostic"), "failed"),
            (Result(0, stderr="partial result warning"), "failed"),
            (Result(None, error="timeout"), "timeout"),
        ):
            item = self.inspect(path, runner=SelectiveRunner("status", result))
            self.assertEqual(item["classification"], "retain")
            self.assertIn("git_status:" + reason, item["unknown"])
            self.assertNotIn("private diagnostic", json.dumps(item))

    def test_unavailable_base_and_changed_head_are_unknown(self) -> None:
        path = self.worktree("head")
        runner = SelectiveRunner("rev-parse", Result(128))
        item = self.inspect(path, runner=runner)
        self.assertIn("merge_base_unavailable", item["unknown"])
        item = self.inspect(path, runner=SelectiveRunner("HEAD", Result(0, "different\n")))
        self.assertIn("head_changed_during_inventory", item["unknown"])

    def test_inventory_does_not_refresh_index(self) -> None:
        path = self.worktree("index")
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir", cwd=path))
        before = (gitdir / "index").stat().st_mtime_ns
        self.inspect(path)
        self.assertEqual((gitdir / "index").stat().st_mtime_ns, before)


class ProbeTests(unittest.TestCase):
    def test_cli_requires_explicit_repo_and_renders_bounded_human_output(self) -> None:
        missing = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--repo", missing.stderr)
        invalid = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                "/nonexistent-inventory-test",
                "--timeout",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("command timeout 1.0s", invalid.stdout)
        self.assertIn("unknown: worktree_list:failed", invalid.stdout)
        self.assertNotIn("fatal:", invalid.stdout)

    def test_timeouts_and_total_budget_are_bounded(self) -> None:
        runner = Runner(timeout=0.01)
        result = runner.run([sys.executable, "-c", "import time; time.sleep(1)"])
        self.assertEqual(result.error, "timeout")
        self.assertLess(result.elapsed_ms, 1000)
        self.assertEqual(Runner(total_timeout=0).run(["unused"]).error, "total_timeout")

    def test_process_probe_failure_and_empty_output_are_unknown(self) -> None:
        class FakeRunner:
            def run(self, args: list[str]) -> Result:
                return {
                    "lsof": Result(0, stderr="permission denied for private path"),
                    "ps": Result(0),
                    "tmux": Result(None, error="unavailable"),
                }[args[0]]

        activity = inventory_module.probe_activity(FakeRunner())
        self.assertEqual(
            activity.unknown,
            ["process_argv:empty", "process_cwd:incomplete", "tmux_cwd:unavailable"],
        )
        self.assertNotIn("private path", repr(activity))

    def test_probe_parser_collects_all_three_sources_and_no_server_is_known(self) -> None:
        class FakeRunner:
            def run(self, args: list[str]) -> Result:
                return {
                    "lsof": Result(0, "p42\nfcwd\nn/repo\n"),
                    "ps": Result(0, "42 python /repo/server.py --token=secret\n"),
                    "tmux": Result(0, "42\t/repo/subdir\n"),
                }[args[0]]

        activity = inventory_module.probe_activity(FakeRunner())
        self.assertEqual(activity.unknown, [])
        self.assertEqual(len(activity.matches(Path("/repo"))), 3)
        self.assertNotIn("secret", json.dumps(activity.matches(Path("/repo"))))

        class NoServerRunner(FakeRunner):
            def run(self, args: list[str]) -> Result:
                if args[0] == "tmux":
                    return Result(1, stderr="no server running on /private/socket")
                return super().run(args)

        absent = inventory_module.probe_activity(NoServerRunner())
        self.assertEqual(absent.unknown, [])
        self.assertEqual(len(absent.matches(Path("/repo"))), 2)

    def test_missing_repository_fails_without_raw_error_output(self) -> None:
        report = inventory_module.inventory(
            Path("/unavailable"),
            Path("/scope"),
            runner=SelectiveRunner("list", Result(128, stderr="private error")),
        )
        self.assertEqual(report["errors"], ["worktree_list:failed"])
        self.assertNotIn("private error", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
