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


@pytest.mark.parametrize(
    "template",
    [
        '<template src="./panel.html"></template>',
        "<template\nlang='html' data-note='>'\nsrc=./panel.html></template>",
        '<template src="./panel.html" />',
        "<template SRC='./panel.html'></template>",
    ],
)
def test_full_gate_rejects_external_vue_templates(tmp_path: Path, template: str) -> None:
    checker = _load_checker()
    for name in ("AGENTS.md", "CLAUDE.md"):
        (tmp_path / name).write_text("Guide\n", encoding="utf-8")
    vue_file = tmp_path / "frontend/src/features/example/Panel.vue"
    vue_file.parent.mkdir(parents=True)
    vue_file.write_text(template, encoding="utf-8")

    errors = checker.run_all_checks(tmp_path)

    assert len(errors) == 1
    assert "features/example/Panel.vue uses <template src>" in errors[0]
    assert "vue-tsc" in errors[0]


@pytest.mark.parametrize(
    "source",
    [
        '<template><img src="./image.png" /></template>',
        '<!-- <template src="./example.html"> --><template><div /></template>',
        '<template data-src="documentation"><div /></template>',
        '<template><div /></template><script setup lang="ts">'
        "const example = '<template src=\"./example.html\">';</script>",
    ],
)
def test_template_coverage_allows_inline_content_and_documentation(
    tmp_path: Path, source: str
) -> None:
    checker = _load_checker()
    vue_file = tmp_path / "frontend/src/Panel.vue"
    vue_file.parent.mkdir(parents=True)
    vue_file.write_text(source, encoding="utf-8")

    assert checker.check_vue_template_type_coverage(tmp_path) == []


def test_template_coverage_allows_fixture_without_frontend(tmp_path: Path) -> None:
    assert _load_checker().check_vue_template_type_coverage(tmp_path) == []


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


@pytest.mark.parametrize("permissive", [False, True])
def test_strict_schema_guard_follows_reexports_and_imported_bases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, permissive: bool
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    models = tmp_path / "backend" / "claude_hub" / "models"
    domains = models / "domains"
    domains.mkdir(parents=True)
    (models / "schemas.py").write_text(
        "from .domains.tasks import ActualTask as WorkspaceTask\n", encoding="utf-8"
    )
    (domains / "base.py").write_text(
        "from pydantic import ConfigDict\n"
        "class StrictBase:\n    model_config = ConfigDict(extra='forbid')\n",
        encoding="utf-8",
    )
    body = (
        "    model_config: ConfigDict = ConfigDict(extra='allow')\n" if permissive else "    pass\n"
    )
    (domains / "tasks.py").write_text(
        "from claude_hub.models.domains.base import StrictBase as Base\n"
        "class ActualTask(Base):\n" + body,
        encoding="utf-8",
    )

    errors = checker.check_strict_schemas(tmp_path)

    assert bool(errors) is permissive
    if permissive:
        assert "must declare extra='forbid'" in errors[0]


@pytest.mark.parametrize("failure", ["cycle", "missing", "shadow", "annotated_shadow"])
def test_strict_schema_reexport_failures_do_not_accept_stale_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    models = tmp_path / "backend" / "claude_hub" / "models"
    models.mkdir(parents=True)
    source = "from .tasks import WorkspaceTask\n"
    if failure == "shadow":
        source += "WorkspaceTask = object\n"
    elif failure == "annotated_shadow":
        source += "WorkspaceTask: type = object\n"
    (models / "schemas.py").write_text(source, encoding="utf-8")
    if failure != "missing":
        (models / "tasks.py").write_text(
            (
                "from .schemas import WorkspaceTask\n"
                if failure == "cycle"
                else "class WorkspaceTask:\n    model_config = ConfigDict(extra='forbid')\n"
            ),
            encoding="utf-8",
        )

    assert checker.check_strict_schemas(tmp_path) == [
        "Strict schema class WorkspaceTask is missing from models/schemas.py"
    ]


def test_final_source_policy_has_no_legacy_exemptions() -> None:
    checker = _load_checker()
    assert checker.DEFAULT_BACKEND_MAX_LINES == 800
    assert checker.DEFAULT_FRONTEND_MAX_LINES == 800
    assert checker.LEGACY_LINE_BUDGETS == {}
    assert checker.LEGACY_IMPORT_CYCLES == ()


@pytest.mark.parametrize("suffix", [".vue", ".ts", ".js", ".css", ".html"])
def test_feature_sources_cannot_bypass_limit_by_file_extension(tmp_path: Path, suffix: str) -> None:
    checker = _load_checker()
    source = tmp_path / "frontend" / "src" / "features" / "chat" / f"oversized{suffix}"
    source.parent.mkdir(parents=True)
    source.write_text("line\n" * 801, encoding="utf-8")
    assert checker.check_file_size_ratchet(tmp_path) == [
        f"frontend/src/features/chat/oversized{suffix} has 801 lines, exceeding ceiling of 800 lines"
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "model_config = ConfigDict(extra='allow')",
        "del model_config",
        "model_config['extra'] = 'allow'",
        "model_config.update(extra='allow')",
        "model_config = make_permissive(extra='forbid')",
        "model_config = {'extra': 'forbid', 'extra': 'allow'}",
        "model_config = {'extra': 'forbid', **other}",
        "cfg = model_config\n    cfg['extra'] = 'allow'",
        "mutate(model_config)",
        "@mutate(model_config)\n    def method(self):\n        pass",
        "def method(self, value=mutate(model_config)):\n        pass",
        "class Nested(mutate(model_config)):\n        pass",
        "if enabled:\n        model_config = ConfigDict(extra='allow')",
    ],
)
def test_strict_schema_rejects_later_config_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    source = tmp_path / "backend" / "claude_hub" / "models" / "schemas.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from pydantic import ConfigDict\n"
        "class WorkspaceTask:\n    model_config = ConfigDict(extra='forbid')\n    "
        + mutation
        + "\n",
        encoding="utf-8",
    )
    assert checker.check_strict_schemas(tmp_path)


@pytest.mark.parametrize("base", ["ExternalPermissive", "external.Permissive", "Generic[T]"])
def test_strict_schema_rejects_unknown_additional_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    source = tmp_path / "backend" / "claude_hub" / "models" / "schemas.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class StrictLocal:\n    model_config = ConfigDict(extra='forbid')\n"
        f"class WorkspaceTask(StrictLocal, {base}):\n    pass\n",
        encoding="utf-8",
    )
    assert checker.check_strict_schemas(tmp_path)


@pytest.mark.parametrize(
    "shadow",
    [
        "import arbitrary as WorkspaceTask",
        "del WorkspaceTask",
        "if enabled:\n    WorkspaceTask = object",
        "try:\n    WorkspaceTask = object\nexcept Exception:\n    pass",
    ],
)
def test_strict_schema_rejects_ambiguous_export_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: str
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    source = tmp_path / "backend" / "claude_hub" / "models" / "schemas.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class WorkspaceTask:\n    model_config = ConfigDict(extra='forbid')\n" + shadow + "\n",
        encoding="utf-8",
    )
    assert checker.check_strict_schemas(tmp_path)


def test_strict_schema_resolver_prefers_package_over_same_named_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    models = tmp_path / "backend" / "claude_hub" / "models"
    (models / "tasks").mkdir(parents=True)
    (models / "schemas.py").write_text("from .tasks import WorkspaceTask\n", encoding="utf-8")
    (models / "tasks.py").write_text(
        "class WorkspaceTask:\n    model_config = ConfigDict(extra='forbid')\n", encoding="utf-8"
    )
    (models / "tasks" / "__init__.py").write_text(
        "class WorkspaceTask:\n    model_config = ConfigDict(extra='allow')\n", encoding="utf-8"
    )
    assert checker.check_strict_schemas(tmp_path)


@pytest.mark.parametrize("initial_extra", ["allow", "forbid"])
def test_strict_schema_inherits_base_at_class_definition_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_extra: str
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    models = tmp_path / "backend" / "claude_hub" / "models"
    models.mkdir(parents=True)
    opposite = "forbid" if initial_extra == "allow" else "allow"
    (models / "initial.py").write_text(
        f"class Base:\n    model_config = {{'extra': '{initial_extra}'}}\n", encoding="utf-8"
    )
    (models / "replacement.py").write_text(
        f"class Base:\n    model_config = {{'extra': '{opposite}'}}\n", encoding="utf-8"
    )
    (models / "schemas.py").write_text(
        "from .initial import Base\n"
        "class WorkspaceTask(Base):\n    pass\n"
        "from .replacement import Base\n",
        encoding="utf-8",
    )
    assert bool(checker.check_strict_schemas(tmp_path)) is (initial_extra == "allow")


@pytest.mark.parametrize(
    ("prelude", "accepted"),
    [
        ("from pydantic import ConfigDict\n", True),
        ("", False),
        ("def ConfigDict(**kwargs):\n    return {'extra': 'allow'}\n", False),
        ("from pydantic import ConfigDict\nConfigDict = other_factory\n", False),
        ("from pydantic import ConfigDict\nif enabled:\n    ConfigDict = other_factory\n", False),
    ],
)
def test_strict_schema_proves_config_constructor_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prelude: str, accepted: bool
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "STRICT_TASK_STATE_SCHEMAS", ("WorkspaceTask",))
    source = tmp_path / "backend" / "claude_hub" / "models" / "schemas.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        prelude + "class WorkspaceTask:\n    model_config = ConfigDict(extra='forbid')\n",
        encoding="utf-8",
    )
    assert (checker.check_strict_schemas(tmp_path) == []) is accepted


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
