"""Contract checks for isolated backend test sharding."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_verify_shards.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("verify_shards_runner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_partition_preserves_every_item_and_keeps_files_together():
    module = load_runner()
    items = [
        {"file": name, "nodeid": f"{name}::{index}"}
        for name in ("a", "b", "c")
        for index in range(3)
    ]
    groups = module.partition(items, 2)
    assert sorted(item["nodeid"] for group in groups for item in group) == sorted(
        item["nodeid"] for item in items
    )
    assert all(
        sum(any(item["file"] == name for item in group) for group in groups) == 1
        for name in ("a", "b", "c")
    )


def test_isolated_env_has_distinct_paths_and_sockets(tmp_path):
    module = load_runner()
    left = module.isolated_env(tmp_path / "left", tmp_path / "cov-left", "0")
    right = module.isolated_env(tmp_path / "right", tmp_path / "cov-right", "1")
    for key in (
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "CLAUDE_HUB_HOME",
        "CLAUDE_HUB_STATE_ROOT",
        "CLAUDE_HUB_TMUX_SOCKET",
        "COVERAGE_FILE",
    ):
        assert left[key] != right[key]


def run_fixture(tmp_path, fail=False):
    backend = tmp_path / "backend"
    (backend / "tests").mkdir(parents=True)
    (backend / "claude_hub").mkdir()
    (backend / "tests" / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parents[1]))\n"
    )
    (backend / "claude_hub" / "__init__.py").write_text("def value():\n    return 2\n")
    (backend / "claude_hub" / "zero.py").write_text("value = 42\n")
    (backend / "tests" / "test_zero.py").write_text("import claude_hub.zero\n")
    (backend / "tests" / "test_one.py").write_text(
        "from claude_hub import value\ndef test_one():\n    assert value() == 2\n"
    )
    (backend / "tests" / "test_two.py").write_text(
        "from claude_hub import value\ndef test_two():\n    assert value() == "
        + ("3" if fail else "2")
        + "\n"
    )
    evidence = tmp_path / "evidence"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--backend",
            str(backend),
            "--runtime",
            str(tmp_path / "runtime"),
            "--evidence",
            str(evidence),
            "--workers",
            "2",
        ],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, backend, evidence


@pytest.mark.skipif(sys.platform != "linux", reason="parallel shard runner requires Linux")
def test_real_pytest_shards_combine_coverage(tmp_path):
    result, backend, evidence = run_fixture(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads((evidence / "collection.json").read_text())) == 2
    xml = ET.parse(backend / "coverage.xml")
    zero = next(c for c in xml.findall(".//class") if c.get("filename", "").endswith("zero.py"))
    assert zero.find(".//line").get("hits") == "1"
    assert all((evidence / f"shard-{i}" / ".coverage").is_file() for i in range(2))
    assert "All 2 collected tests completed" in result.stdout
    serial_xml = backend / "serial.xml"
    env = {**os.environ, "COVERAGE_FILE": str(evidence / ".coverage.serial")}
    serial = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pytest",
            "-q",
            "--cov=claude_hub",
            f"--cov-report=xml:{serial_xml}",
        ],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert serial.returncode == 0, serial.stdout + serial.stderr

    def lines(path):
        root = ET.parse(path)
        return {
            (cls.get("filename"), line.get("number"), int(line.get("hits")) > 0)
            for cls in root.findall(".//class")
            for line in cls.findall("./lines/line")
        }

    assert lines(backend / "coverage.xml") == lines(serial_xml)


@pytest.mark.skipif(sys.platform != "linux", reason="parallel shard runner requires Linux")
def test_failure_returns_nonzero_and_keeps_shard_logs(tmp_path):
    result, _, evidence = run_fixture(tmp_path, fail=True)
    assert result.returncode != 0
    assert any("FAILED" in path.read_text() for path in evidence.glob("shard-*.log"))


@pytest.mark.skipif(sys.platform != "linux", reason="parallel shard runner requires Linux")
def test_stop_terminates_descendant_after_shard_leader_exits(tmp_path):
    import time

    module = load_runner()
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "open(sys.argv[1],'w').write(str(p.pid))"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(pidfile)], start_new_session=True)
    deadline = time.monotonic() + 5
    while module.status(proc) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert module.status(proc) == 0
    child = int(pidfile.read_text())
    module.stop([proc])
    status_file = Path(f"/proc/{child}/status")
    if status_file.exists():
        assert "State:\tZ" in status_file.read_text()


@pytest.mark.skipif(sys.platform != "linux", reason="parallel shard runner requires Linux")
def test_interrupt_stops_running_shards(tmp_path):
    import signal
    import time

    backend = tmp_path / "backend"
    (backend / "tests").mkdir(parents=True)
    (backend / "claude_hub").mkdir()
    (backend / "claude_hub" / "__init__.py").write_text("")
    (backend / "tests" / "test_wait.py").write_text(
        "import os, time, pytest\nfrom pathlib import Path\n"
        "@pytest.fixture\n"
        "def cleanup():\n"
        "    yield\n"
        "    time.sleep(1.2)\n"
        "    Path(os.environ['TMPDIR'], 'finalized').touch()\n"
        "def test_wait(cleanup):\n"
        "    Path(os.environ['TMPDIR'], 'ready').touch()\n"
        "    time.sleep(30)\n"
    )
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--backend",
            str(backend),
            "--runtime",
            str(runtime),
            "--evidence",
            str(evidence),
            "--workers",
            "2",
        ],
        cwd=backend,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ready = runtime / "shard-0" / "tmp" / "ready"
        deadline = time.monotonic() + 20
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
        assert (runtime / "shard-0" / "tmp" / "finalized").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="parallel shard runner requires Linux")
@pytest.mark.parametrize("fail", [False, True])
def test_detached_session_descendant_is_cleaned(tmp_path, fail):
    backend = tmp_path / "backend"
    (backend / "tests").mkdir(parents=True)
    (backend / "claude_hub").mkdir()
    (backend / "claude_hub" / "__init__.py").write_text("")
    pidfile = tmp_path / "escaped.pid"
    ready = tmp_path / "child-ready"
    child = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(ready)!r}, 'w').write('ready'); time.sleep(60)"
    )
    (backend / "tests" / "test_escape.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "def test_escape():\n"
        f"    p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"    open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        f"    ready = Path({str(ready)!r})\n"
        "    deadline = time.monotonic() + 5\n"
        "    while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "    assert ready.exists()\n"
        + ("    assert False, 'intentional shard failure'\n" if fail else "")
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--backend",
            str(backend),
            "--runtime",
            str(tmp_path / "runtime"),
            "--evidence",
            str(tmp_path / "evidence"),
            "--workers",
            "2",
        ],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=30,
    )
    pid = int(pidfile.read_text()) if pidfile.exists() else None
    try:
        assert (proc.returncode != 0) == fail, proc.stdout + proc.stderr
        assert pid is not None and not Path(f"/proc/{pid}").exists()
    finally:
        if pid is not None:
            cmdline = Path(f"/proc/{pid}/cmdline")
            if cmdline.exists() and b"time.sleep(60)" in cmdline.read_bytes():
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="parallel shard runner requires Linux")
def test_running_shard_reaps_adopted_zombies_before_group_check(tmp_path):
    backend = tmp_path / "backend"
    (backend / "tests").mkdir(parents=True)
    (backend / "claude_hub").mkdir()
    (backend / "claude_hub" / "__init__.py").write_text("")
    # Leader exits after creating a child in its own group. The still-running
    # shard waits for that group to vanish; a subreaper retaining the orphaned
    # zombie makes killpg(..., 0) report the group alive until the test fails.
    leader = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)'])"
    (backend / "tests" / "test_group.py").write_text(
        "import os, subprocess, sys, time\n"
        "def test_group():\n"
        f"    p = subprocess.Popen([sys.executable, '-c', {leader!r}], "
        "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    assert p.wait(timeout=3) == 0\n"
        "    deadline = time.monotonic() + 3\n"
        "    while time.monotonic() < deadline:\n"
        "        try: os.killpg(p.pid, 0)\n"
        "        except ProcessLookupError: return\n"
        "        time.sleep(0.05)\n"
        "    assert False, 'orphan zombie kept process group alive'\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--backend",
            str(backend),
            "--runtime",
            str(tmp_path / "runtime"),
            "--evidence",
            str(tmp_path / "evidence"),
            "--workers",
            "2",
        ],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        result.stdout + result.stderr + "\n" + (tmp_path / "evidence" / "shard-0.log").read_text()
    )
