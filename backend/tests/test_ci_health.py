"""Run the real collection policy against isolated pytest subprocesses."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest


def _run_pytest(
    tmp_path: Path, source: str, args: tuple[str, ...] = ("-q",)
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text(source)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys, pytest; sys.path.insert(0, sys.argv[1]); "
            "from tests import ci_health; "
            "raise SystemExit(pytest.main([*sys.argv[2:], '--confcutdir=.', '.'], "
            "plugins=[ci_health]))",
            str(Path(__file__).resolve().parents[1]),
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _error(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    line = next(line for line in result.stderr.splitlines() if line.startswith("ERROR: "))
    return cast(dict[str, Any], json.loads(line.removeprefix("ERROR: ")))


def test_payload_id_stops_before_execution_with_bounded_diagnostics(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path,
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('payload', [b'x' * (8 * 1024 * 1024)])\n"
        "def test_payload(payload):\n    Path('executed').touch()\n",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    report = _error(result)
    assert report["code"] == "CI_NODE_ID_TOO_LONG"
    assert report["violations"] == 1
    assert report["examples"][0]["file"] == "test_sample.py"
    assert report["examples"][0]["node_id_chars"] > 8 * 1024 * 1024
    assert len(result.stdout) + len(result.stderr) < 4096
    assert not (tmp_path / "executed").exists()


@pytest.mark.parametrize("args", [("--collect-only", "-q"), ("--collect-only", "-vv")])
def test_collection_only_keeps_rejected_ids_out_of_terminal_reports(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    result = _run_pytest(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.parametrize('payload', [b'x' * (8 * 1024 * 1024)])\n"
        "def test_payload(payload):\n    assert False\n",
        args=args,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert _error(result)["code"] == "CI_NODE_ID_TOO_LONG"
    output_length = len(result.stdout) + len(result.stderr)
    assert output_length < 4096


@pytest.mark.parametrize("verbosity", ["-q", "-vv"])
def test_early_collection_error_keeps_partial_ids_out_of_terminal_reports(
    tmp_path: Path, verbosity: str
) -> None:
    (tmp_path / "test_t_error.py").write_text("raise RuntimeError('collection-sentinel')\n")
    (tmp_path / "test_z_tail.py").write_text("def test_tail():\n    assert False\n")
    result = _run_pytest(
        tmp_path,
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('payload', [b'x' * (8 * 1024 * 1024)])\n"
        "def test_payload(payload):\n    Path('executed').touch()\n",
        args=("--collect-only", verbosity, "-x"),
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert _error(result)["code"] == "CI_NODE_ID_TOO_LONG"
    assert "collection-sentinel" in result.stdout + result.stderr
    assert len(result.stdout) + len(result.stderr) < 4096
    assert not (tmp_path / "executed").exists()


def test_short_ids_preserve_early_collection_failure(tmp_path: Path) -> None:
    (tmp_path / "test_t_error.py").write_text("raise RuntimeError('collection-sentinel')\n")
    (tmp_path / "test_z_tail.py").write_text("def test_tail():\n    assert False\n")
    result = _run_pytest(
        tmp_path,
        "from pathlib import Path\n" "def test_payload():\n    Path('executed').touch()\n",
        args=("--collect-only", "-q", "-x"),
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED
    assert "collection-sentinel" in result.stdout + result.stderr
    assert "CI_NODE_ID_TOO_LONG" not in result.stdout + result.stderr
    assert not (tmp_path / "executed").exists()


def test_short_id_preserves_large_input_and_executes_assertions(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path,
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('payload', ["
        "pytest.param(b'x' * (8 * 1024 * 1024), id='large-payload')])\n"
        "def test_payload(payload):\n"
        "    assert len(payload) == 8 * 1024 * 1024\n"
        "    Path('executed').touch()\n",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert (tmp_path / "executed").exists()


@pytest.mark.parametrize("node_id_chars,expected_exit", [(512, 0), (513, 4)])
def test_full_node_id_budget_boundary(
    tmp_path: Path, node_id_chars: int, expected_exit: int
) -> None:
    label = "x" * (node_id_chars - len("test_sample.py::test_payload[]"))
    result = _run_pytest(
        tmp_path,
        "import pytest\n"
        f"@pytest.mark.parametrize('payload', [0], ids=[{label!r}])\n"
        "def test_payload(payload):\n    assert payload == 0\n",
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr


def test_collection_diagnostics_limit_examples_without_printing_ids(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.parametrize('payload', ['x' * 100_000] * 8)\n"
        "def test_payload(payload):\n    assert False\n",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    report = _error(result)
    assert report["violations"] == 8
    assert len(report["examples"]) == 5
    assert len(result.stdout) + len(result.stderr) < 4096


def test_normal_assertion_failure_remains_a_failure(tmp_path: Path) -> None:
    result = _run_pytest(tmp_path, "def test_payload():\n    assert False\n")
    assert result.returncode == pytest.ExitCode.TESTS_FAILED
