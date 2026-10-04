"""The prompt probe must ignore inherited live settings and import its checkout."""

import os
import subprocess
import sys
from pathlib import Path


def test_measure_prompts_isolates_before_import_and_cleans_up(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    inherited_home = tmp_path / "inherited-hub"
    inherited_home.mkdir()
    sentinel = inherited_home / "index.json"
    sentinel.write_text("DO NOT LOAD OR REWRITE")
    env = os.environ.copy()
    env.update(
        CLAUDE_HUB_HOME=str(inherited_home),
        CLAUDE_HUB_STATE_ROOT=str(inherited_home),
        CLAUDE_HUB_TMUX_SOCKET="",
        CLAUDE_HUB_ALLOW_LIVE_RUNTIME="1",
        PYTHONPATH=str(tmp_path / "unrelated-pythonpath"),
    )
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "measure_prompts.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    assert (
        f"Prompt source: {repo}/backend/claude_hub/services/workspace_manager/_prompts.py"
        in result.stdout
    )
    runtime_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Temporary runtime")
    )
    runtime = Path(runtime_line.split(": ", 1)[1])
    assert not runtime.exists()
    assert sentinel.read_text() == "DO NOT LOAD OR REWRITE"
    assert list(inherited_home.iterdir()) == [sentinel]
    assert "ASSIGN autonomous+simple" in result.stdout
    assert "[tiered]" in result.stdout
