"""Unit and fault-injection tests for scripts/check_ai_metrics.py."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_checker() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_ai_metrics.py"
    spec = importlib.util.spec_from_file_location("check_ai_metrics", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_ai_metrics"] = module
    spec.loader.exec_module(module)
    return module


def test_real_repository_passes_ai_metrics_gate() -> None:
    checker = _load_checker()
    repo_root = Path(__file__).resolve().parents[2]
    errors = checker.run_all_checks(repo_root)
    assert errors == []
    assert checker.main(["--root", str(repo_root)]) == 0


def test_terminal_proxy_modules_are_under_default_backend_budget() -> None:
    checker = _load_checker()
    repo_root = Path(__file__).resolve().parents[2]
    api_dir = repo_root / "backend/claude_hub/api"
    paths = [api_dir / "terminal.py", *sorted(api_dir.glob("_terminal_injection*.py"))]
    for path in paths:
        relative_path = path.relative_to(repo_root).as_posix()
        assert relative_path not in checker.LEGACY_LINE_BUDGETS
        assert (
            len(path.read_text(encoding="utf-8").splitlines()) <= checker.DEFAULT_BACKEND_MAX_LINES
        )


def test_entry_doc_budget_and_byte_identity_rejects_drift(tmp_path: Path) -> None:
    checker = _load_checker()
    (tmp_path / "AGENTS.md").write_text("line\n" * 151, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("different\n", encoding="utf-8")
    errors = checker.check_entry_docs(tmp_path)
    assert any("byte-identical" in err for err in errors)
    assert any("AGENTS.md has 151 lines" in err for err in errors)


def test_file_size_ratchet_rejects_oversized_new_or_legacy_files(tmp_path: Path) -> None:
    checker = _load_checker()
    py_file = tmp_path / "backend" / "claude_hub" / "services" / "new_giant.py"
    py_file.parent.mkdir(parents=True)
    py_file.write_text("x = 1\n" * (checker.DEFAULT_BACKEND_MAX_LINES + 1), encoding="utf-8")

    fe_file = tmp_path / "frontend" / "src" / "components" / "NewGiant.vue"
    fe_file.parent.mkdir(parents=True)
    fe_file.write_text("<div/>\n" * (checker.DEFAULT_FRONTEND_MAX_LINES + 1), encoding="utf-8")

    errors = checker.check_file_size_ratchet(tmp_path)
    assert any("new_giant.py" in err for err in errors)
    assert any("NewGiant.vue" in err for err in errors)


def test_file_size_ratchet_allows_fixture_without_source_tree(tmp_path: Path) -> None:
    checker = _load_checker()

    assert checker.check_file_size_ratchet(tmp_path) == []


def test_file_size_ratchet_requires_baseline_update_after_shrink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load_checker()
    legacy = tmp_path / "backend" / "claude_hub" / "legacy.py"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(checker, "DEFAULT_BACKEND_MAX_LINES", 0)
    monkeypatch.setattr(
        checker,
        "LEGACY_LINE_BUDGETS",
        {"backend/claude_hub/legacy.py": 2},
    )

    errors = checker.check_file_size_ratchet(tmp_path)

    assert errors == [
        "backend/claude_hub/legacy.py shrank to 1 lines; lower its legacy ceiling "
        "from 2 in this change"
    ]


def test_file_size_ratchet_removes_legacy_entry_below_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load_checker()
    legacy = tmp_path / "backend" / "claude_hub" / "small.py"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        checker,
        "LEGACY_LINE_BUDGETS",
        {"backend/claude_hub/small.py": 1},
    )

    errors = checker.check_file_size_ratchet(tmp_path)

    assert errors == [
        "backend/claude_hub/small.py no longer needs a legacy line ceiling; "
        "remove its 1-line entry"
    ]


def test_layering_boundaries_reject_forbidden_imports(tmp_path: Path) -> None:
    checker = _load_checker()
    models_dir = tmp_path / "backend" / "claude_hub" / "models"
    services_dir = tmp_path / "backend" / "claude_hub" / "services"
    cli_dir = tmp_path / "backend" / "claude_hub" / "cli"
    models_dir.mkdir(parents=True)
    services_dir.mkdir(parents=True)
    cli_dir.mkdir(parents=True)

    (models_dir / "bad_model.py").write_text(
        "from ..services import workspace_manager\n", encoding="utf-8"
    )
    (services_dir / "workspace_state_policy.py").write_text(
        "from . import ttyd_manager\n", encoding="utf-8"
    )
    (cli_dir / "bad_cli.py").write_text(
        "from claude_hub.services.workspace_manager import workspace_manager\n",
        encoding="utf-8",
    )

    errors = checker.check_layering_boundaries(tmp_path)
    assert len(errors) == 3
    assert any("claude_hub.models.bad_model" in err for err in errors)
    assert any("claude_hub.services.workspace_state_policy" in err for err in errors)
    assert any("claude_hub.cli.bad_cli" in err for err in errors)


def test_import_cycle_ratchet_rejects_new_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "LEGACY_IMPORT_CYCLES", ())
    package = tmp_path / "backend" / "claude_hub"
    package.mkdir(parents=True)
    (package / "alpha.py").write_text("from claude_hub import beta\n", encoding="utf-8")
    (package / "beta.py").write_text("from claude_hub import alpha\n", encoding="utf-8")

    errors = checker.check_import_cycle_ratchet(tmp_path)

    assert errors == ["New or changed backend import cycle: claude_hub.alpha, claude_hub.beta"]


def test_import_cycle_baseline_must_match_current_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load_checker()
    package = tmp_path / "backend" / "claude_hub"
    package.mkdir(parents=True)
    (package / "alpha.py").write_text("from claude_hub import beta\n", encoding="utf-8")
    (package / "beta.py").write_text("from claude_hub import alpha\n", encoding="utf-8")
    (package / "gamma.py").write_text("VALUE = 1\n", encoding="utf-8")
    alpha_beta = frozenset({"claude_hub.alpha", "claude_hub.beta"})
    stale_larger_baseline = alpha_beta | {"claude_hub.gamma"}
    monkeypatch.setattr(checker, "LEGACY_IMPORT_CYCLES", (stale_larger_baseline,))

    errors = checker.check_import_cycle_ratchet(tmp_path)

    assert any("New or changed" in error for error in errors)
    assert any("no longer matches" in error for error in errors)

    monkeypatch.setattr(checker, "LEGACY_IMPORT_CYCLES", (alpha_beta,))
    assert checker.check_import_cycle_ratchet(tmp_path) == []

    (package / "beta.py").write_text("from claude_hub import alpha, gamma\n", encoding="utf-8")
    (package / "gamma.py").write_text("from claude_hub import alpha\n", encoding="utf-8")
    assert any("New or changed" in error for error in checker.check_import_cycle_ratchet(tmp_path))

    (package / "gamma.py").unlink()
    monkeypatch.setattr(checker, "LEGACY_IMPORT_CYCLES", (stale_larger_baseline,))
    assert any(
        "no longer matches" in error for error in checker.check_import_cycle_ratchet(tmp_path)
    )


def test_import_cycle_graph_keeps_type_checking_else_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "LEGACY_IMPORT_CYCLES", ())
    package = tmp_path / "backend" / "claude_hub"
    package.mkdir(parents=True)
    (package / "alpha.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from claude_hub import type_only\n"
        "else:\n"
        "    from claude_hub import beta\n",
        encoding="utf-8",
    )
    (package / "beta.py").write_text("from claude_hub import alpha\n", encoding="utf-8")

    errors = checker.check_import_cycle_ratchet(tmp_path)

    assert errors == ["New or changed backend import cycle: claude_hub.alpha, claude_hub.beta"]


def test_strict_schema_guard_rejects_missing_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load_checker()
    schemas = tmp_path / "backend" / "claude_hub" / "models" / "schemas.py"
    schemas.parent.mkdir(parents=True)
    schemas.write_text("class WorkspaceTask:\n    pass\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("ok\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("ok\n", encoding="utf-8")

    assert checker.main(["--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "Schema WorkspaceTask in models/schemas.py must declare extra='forbid'" in captured.err


def test_strict_schema_guard_accepts_inherited_config() -> None:
    checker = _load_checker()
    tree = ast.parse(
        "class StrictBase:\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "class WorkspaceTask(StrictBase):\n"
        "    pass\n"
        "class PermissiveTask(StrictBase):\n"
        "    model_config = ConfigDict(extra='allow')\n"
        "class StrictLeft(StrictBase):\n"
        "    pass\n"
        "class StrictRight(StrictBase):\n"
        "    pass\n"
        "class StrictDiamond(StrictLeft, StrictRight):\n"
        "    pass\n"
    )
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    assert checker._has_extra_forbid(classes["WorkspaceTask"], classes)
    assert not checker._has_extra_forbid(classes["PermissiveTask"], classes)
    assert checker._has_extra_forbid(classes["StrictDiamond"], classes)


def test_warning_policy_requires_cleaned_warning_families(tmp_path: Path) -> None:
    checker = _load_checker()
    pyproject = tmp_path / "backend" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text(
        '[tool.pytest.ini_options]\nfilterwarnings = ["error:other"]\n',
        encoding="utf-8",
    )

    errors = checker.check_warning_error_policy(tmp_path)

    assert len(errors) == len(
        checker.REQUIRED_WARNING_NOISE_FILTERS + checker.REQUIRED_WARNING_ERROR_FILTERS
    )


def test_warning_policy_rejects_later_override(tmp_path: Path) -> None:
    checker = _load_checker()
    pyproject = tmp_path / "backend" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    filters = [
        *checker.REQUIRED_WARNING_NOISE_FILTERS,
        *checker.REQUIRED_WARNING_ERROR_FILTERS,
        "ignore::UserWarning",
    ]
    pyproject.write_text(
        f"[tool.pytest.ini_options]\nfilterwarnings = {json.dumps(filters)}\n",
        encoding="utf-8",
    )

    errors = checker.check_warning_error_policy(tmp_path)

    assert errors == [
        "backend pytest warning error filters must be the final filters so later rules "
        "cannot override them"
    ]


@pytest.mark.parametrize(
    "global_setting",
    [
        'disable_error_code = ["attr-defined", "no-any-return"]\n',
        "ignore_errors = true\n",
    ],
)
def test_mypy_blindspot_guard_rejects_global_weakening(tmp_path: Path, global_setting: str) -> None:
    checker = _load_checker()
    pyproject = tmp_path / "backend" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text(f"[tool.mypy]\n{global_setting}", encoding="utf-8")

    errors = checker.check_mypy_mixin_blindspot_removed(tmp_path)

    assert len(errors) == 1
    assert "must not globally weaken WorkspaceManager mixin checks" in errors[0]


@pytest.mark.parametrize(
    "override",
    [
        "module = 'claude_hub.services.workspace_manager.*'\n"
        "disable_error_code = ['attr-defined']\n",
        'module = ["claude_hub.services.workspace_manager._dispatch"]\n'
        'disable_error_code = "no-any-return"\n',
        'module = "claude_hub.services.*"\nignore_errors = true\n',
    ],
)
def test_mypy_blindspot_guard_parses_workspace_manager_overrides(
    tmp_path: Path, override: str
) -> None:
    checker = _load_checker()
    pyproject = tmp_path / "backend" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text(f"[[tool.mypy.overrides]]\n{override}", encoding="utf-8")

    errors = checker.check_mypy_mixin_blindspot_removed(tmp_path)

    assert len(errors) == 1
    assert "must not weaken WorkspaceManager mixin checks" in errors[0]
