#!/usr/bin/env python3
"""AI Native engineering metrics ratchet and architecture boundary checker.

This script uses only the Python standard library so it can run inside the
tool-free ``./scripts/verify.sh docs`` gate as well as focused unit tests.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence

ENTRY_DOC_MAX_LINES = 150
# The 800-line refactoring target is advisory, not a verification threshold.
DEFAULT_BACKEND_MAX_LINES = 1500
DEFAULT_FRONTEND_MAX_LINES = 2500

# Legacy source and import-cycle debt has been eliminated. Keep the empty
# baselines explicit so regression tests reject reintroducing exemptions.
LEGACY_LINE_BUDGETS: dict[str, int] = {}
LEGACY_IMPORT_CYCLES: tuple[frozenset[str], ...] = ()

REQUIRED_WARNING_NOISE_FILTERS: tuple[str, ...] = (
    "ignore:websockets.InvalidStatusCode is deprecated:DeprecationWarning:lark_oapi.ws.client",
    "ignore:websockets.legacy is deprecated.*:DeprecationWarning:websockets.legacy",
)
REQUIRED_WARNING_ERROR_FILTERS: tuple[str, ...] = (
    "error::pydantic.warnings.PydanticDeprecatedSince20",
    "error:Duplicate Operation ID.*:UserWarning",
    "error:Setting per-request cookies=.*:DeprecationWarning",
)

# This bounded contract covers durable task/schedule state and the envelopes that
# transfer task execution authority. Other request models remain API-owned and
# are not implied to be strict by this repository-level metric.
STRICT_TASK_STATE_SCHEMAS: tuple[str, ...] = (
    "TaskSourceReference",
    "TaskExecutionReference",
    "TaskRuntimeObservation",
    "TaskProgressRequest",
    "TaskExecutionHandoffRequest",
    "WorkspaceTaskCreate",
    "WorkspaceTaskUpdate",
    "WorkspaceTask",
    "ScheduledTask",
    "ScheduledTaskCreate",
    "ScheduledTaskUpdate",
)


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def check_entry_docs(root: Path) -> list[str]:
    errors: list[str] = []
    agents_md = root / "AGENTS.md"
    claude_md = root / "CLAUDE.md"
    if not agents_md.is_file() or not claude_md.is_file():
        errors.append("AGENTS.md and CLAUDE.md must both exist at repository root")
        return errors

    agents_bytes = agents_md.read_bytes()
    claude_bytes = claude_md.read_bytes()
    if agents_bytes != claude_bytes:
        errors.append("AGENTS.md and CLAUDE.md must be byte-identical")

    for doc in (agents_md, claude_md):
        lines = _line_count(doc)
        if lines > ENTRY_DOC_MAX_LINES:
            errors.append(f"{doc.name} has {lines} lines (budget: <= {ENTRY_DOC_MAX_LINES} lines)")
    return errors


def check_file_size_ratchet(root: Path) -> list[str]:
    errors: list[str] = []
    for rel, budget in sorted(LEGACY_LINE_BUDGETS.items()):
        path = root / rel
        scope_root = (
            root / "backend" / "claude_hub"
            if rel.startswith("backend/")
            else root / "frontend" / "src"
        )
        if not scope_root.is_dir():
            continue
        default_budget = (
            DEFAULT_BACKEND_MAX_LINES if rel.startswith("backend/") else DEFAULT_FRONTEND_MAX_LINES
        )
        if budget <= default_budget:
            errors.append(
                f"{rel} no longer needs a legacy line ceiling; remove its {budget}-line entry"
            )
        if not path.is_file():
            errors.append(f"Legacy line ceiling points to missing file: {rel}")
            continue
        lines = _line_count(path)
        if lines < budget:
            errors.append(
                f"{rel} shrank to {lines} lines; lower its legacy ceiling from {budget} "
                "in this change"
            )

    backend_root = root / "backend" / "claude_hub"
    if backend_root.is_dir():
        for py_file in sorted(backend_root.rglob("*.py")):
            rel = py_file.relative_to(root).as_posix()
            lines = _line_count(py_file)
            budget = LEGACY_LINE_BUDGETS.get(rel, DEFAULT_BACKEND_MAX_LINES)
            if lines > budget:
                errors.append(f"{rel} has {lines} lines, exceeding ceiling of {budget} lines")

    frontend_src = root / "frontend" / "src"
    if frontend_src.is_dir():
        for fe_file in sorted(frontend_src.rglob("*")):
            if not fe_file.is_file() or fe_file.suffix not in {
                ".vue",
                ".ts",
                ".js",
                ".css",
                ".html",
            }:
                continue
            rel = fe_file.relative_to(root).as_posix()
            lines = _line_count(fe_file)
            budget = LEGACY_LINE_BUDGETS.get(rel, DEFAULT_FRONTEND_MAX_LINES)
            if lines > budget:
                errors.append(f"{rel} has {lines} lines, exceeding ceiling of {budget} lines")
    return errors


class _VueTemplateSourceDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_external_template = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "template" and any(name == "src" for name, _ in attrs):
            self.has_external_template = True


def check_vue_template_type_coverage(root: Path) -> list[str]:
    """Reject external SFC templates that the current vue-tsc silently skips."""
    errors: list[str] = []
    for vue_file in sorted((root / "frontend" / "src").rglob("*.vue")):
        parser = _VueTemplateSourceDetector()
        parser.feed(vue_file.read_text(encoding="utf-8"))
        parser.close()
        if parser.has_external_template:
            errors.append(
                f"{vue_file.relative_to(root).as_posix()} uses <template src>, which bypasses "
                "vue-tsc template checks; keep templates inline and split typed components/composables"
            )
    return errors


def _resolve_relative_import(
    file_parts: list[str], is_init: bool, level: int, module: str | None
) -> str:
    anchor = file_parts if is_init else file_parts
    up = level - 1 if is_init else level
    base = anchor[:-up] if up > 0 else list(anchor)
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _iter_claude_hub_imports(py_file: Path, backend_dir: Path) -> list[tuple[str, str]]:
    rel_parts = list(py_file.relative_to(backend_dir).with_suffix("").parts)
    is_init = rel_parts[-1] == "__init__"
    mod_parts = rel_parts[:-1] if is_init else rel_parts
    mod_name = ".".join(mod_parts)

    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    imports: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("claude_hub"):
                    imports.append((mod_name, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                target = _resolve_relative_import(
                    mod_parts if is_init else rel_parts,
                    is_init,
                    node.level,
                    node.module,
                )
            else:
                target = node.module or ""
            if target.startswith("claude_hub"):
                if node.names and target in {"claude_hub", "claude_hub.services"}:
                    for alias in node.names:
                        imports.append((mod_name, f"{target}.{alias.name}"))
                else:
                    imports.append((mod_name, target))
    return imports


def check_layering_boundaries(root: Path) -> list[str]:
    errors: list[str] = []
    backend_dir = root / "backend"
    pkg_dir = backend_dir / "claude_hub"
    if not pkg_dir.is_dir():
        return errors

    for py_file in sorted(pkg_dir.rglob("*.py")):
        for mod_name, target in _iter_claude_hub_imports(py_file, backend_dir):
            if mod_name.startswith("claude_hub.models"):
                if any(
                    target == f"claude_hub.{layer}" or target.startswith(f"claude_hub.{layer}.")
                    for layer in ("api", "services", "cli")
                ):
                    errors.append(
                        f"Layer violation: {mod_name} imports {target} "
                        "(models must not depend on api, services, or cli)"
                    )
            elif mod_name == "claude_hub.services.workspace_state_policy":
                allowed = (
                    target == "claude_hub.models"
                    or target.startswith("claude_hub.models.")
                    or target == "claude_hub.services.agent_status_markers"
                    or target.startswith("claude_hub.services.agent_status_markers.")
                )
                if not allowed:
                    errors.append(
                        f"Layer violation: {mod_name} imports {target} "
                        "(workspace_state_policy must remain a pure policy module)"
                    )
            elif mod_name.startswith("claude_hub.cli"):
                forbidden_prefixes = (
                    "claude_hub.api",
                    "claude_hub.services.workspace_manager",
                    "claude_hub.services.ttyd_manager",
                )
                if any(
                    target == prefix or target.startswith(f"{prefix}.")
                    for prefix in forbidden_prefixes
                ):
                    errors.append(
                        f"Layer violation: {mod_name} imports {target} "
                        "(CLI must not directly import server API or runtime managers)"
                    )
    return errors


def _module_name(py_file: Path, backend_dir: Path) -> tuple[str, list[str]]:
    rel_parts = list(py_file.relative_to(backend_dir).with_suffix("").parts)
    if rel_parts[-1] == "__init__":
        return ".".join(rel_parts[:-1]), rel_parts[:-1]
    return ".".join(rel_parts), rel_parts[:-1]


def _is_type_checking_guard(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "TYPE_CHECKING"


def _module_scope_imports(statements: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    imports: list[ast.Import | ast.ImportFrom] = []
    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            imports.append(statement)
        elif isinstance(statement, ast.If):
            if not _is_type_checking_guard(statement.test):
                imports.extend(_module_scope_imports(statement.body))
            imports.extend(_module_scope_imports(statement.orelse))
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            imports.extend(_module_scope_imports(statement.body))
            imports.extend(_module_scope_imports(statement.orelse))
            imports.extend(_module_scope_imports(statement.finalbody))
            for handler in statement.handlers:
                imports.extend(_module_scope_imports(handler.body))
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            imports.extend(_module_scope_imports(statement.body))
    return imports


def _runtime_import_graph(root: Path) -> dict[str, set[str]]:
    backend_dir = root / "backend"
    pkg_dir = backend_dir / "claude_hub"
    modules = {_module_name(py_file, backend_dir)[0]: py_file for py_file in pkg_dir.rglob("*.py")}
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, py_file in modules.items():
        _, package_parts = _module_name(py_file, backend_dir)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in _module_scope_imports(tree.body):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            else:
                if node.level:
                    upward = node.level - 1
                    base = package_parts[:-upward] if upward else list(package_parts)
                    if node.module:
                        base.extend(node.module.split("."))
                    target = ".".join(base)
                else:
                    target = node.module or ""
                candidates.append(target)
                candidates.extend(
                    f"{target}.{alias.name}" for alias in node.names if alias.name != "*"
                )
            graph[module].update(candidate for candidate in candidates if candidate in modules)
    return graph


def _cyclic_components(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[frozenset[str]] = []

    def visit(module: str) -> None:
        indices[module] = len(indices)
        lowlinks[module] = indices[module]
        stack.append(module)
        on_stack.add(module)
        for dependency in graph[module]:
            if dependency not in indices:
                visit(dependency)
                lowlinks[module] = min(lowlinks[module], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[dependency])
        if lowlinks[module] != indices[module]:
            return
        component: set[str] = set()
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == module:
                break
        if len(component) > 1 or module in graph[module]:
            components.append(frozenset(component))

    for module in sorted(graph):
        if module not in indices:
            visit(module)
    return components


def check_import_cycle_ratchet(root: Path) -> list[str]:
    pkg_dir = root / "backend" / "claude_hub"
    if not pkg_dir.is_dir():
        return []
    graph = _runtime_import_graph(root)
    actual = set(_cyclic_components(graph))
    baseline = set(LEGACY_IMPORT_CYCLES)
    errors: list[str] = []
    for component in sorted(actual - baseline, key=lambda item: sorted(item)):
        members = ", ".join(sorted(component))
        errors.append(f"New or changed backend import cycle: {members}")
    for component in sorted(baseline - actual, key=lambda item: sorted(item)):
        members = ", ".join(sorted(component))
        errors.append(
            "Legacy import-cycle baseline no longer matches the current graph; "
            f"replace it with the smaller residual cycle(s): {members}"
        )
    return errors


def _writes_binding(node: ast.AST, name: str) -> bool:
    """Conservatively detect writes without interpreting arbitrary Python."""
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any(
            alias.name == "*" or (alias.asname or alias.name.split(".")[0]) == name
            for alias in node.names
        )
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.Delete)):
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
        return any(
            isinstance(part, ast.Name) and part.id == name
            for target in targets
            for part in ast.walk(target)
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if any(
            isinstance(part, ast.Name) and part.id == name for part in ast.walk(node.func.value)
        ):
            return True
    return any(_writes_binding(child, name) for child in ast.iter_child_nodes(node))


def _reads_binding(node: ast.AST, name: str) -> bool:
    evaluated: list[ast.AST]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        evaluated = [*node.decorator_list, node.args]
        if node.returns is not None:
            evaluated.append(node.returns)
        return any(_reads_binding(part, name) for part in evaluated)
    if isinstance(node, ast.ClassDef):
        evaluated = [*node.decorator_list, *node.bases, *node.keywords]
        return any(_reads_binding(part, name) for part in evaluated)
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name:
        return True
    return any(_reads_binding(child, name) for child in ast.iter_child_nodes(node))


def _has_extra_forbid(
    node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    seen: set[str] | None = None,
) -> bool:
    visited = set() if seen is None else seen
    if node.name in visited:
        return False
    visited.add(node.name)
    val: ast.expr | None
    for stmt in reversed(node.body):
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            val = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            val = stmt.value
        else:
            if _writes_binding(stmt, "model_config") or _reads_binding(stmt, "model_config"):
                return False
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "model_config" for target in targets
        ):
            if _writes_binding(stmt, "model_config") or _reads_binding(stmt, "model_config"):
                return False
            continue
        if isinstance(val, ast.Dict):
            if any(key is None for key in val.keys):
                return False
            for key, value in reversed(list(zip(val.keys, val.values))):
                if isinstance(key, ast.Constant) and key.value == "extra":
                    return isinstance(value, ast.Constant) and value.value == "forbid"
        elif isinstance(val, ast.Call):
            if not isinstance(val.func, ast.Name) or val.func.id != "ConfigDict":
                return False
            if any(keyword.arg is None for keyword in val.keywords):
                return False
            for keyword in val.keywords:
                if keyword.arg == "extra":
                    return (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value == "forbid"
                    )
        return False
    if any(not isinstance(base, ast.Name) or base.id not in classes for base in node.bases):
        return False
    parents = [classes[base.id] for base in node.bases if isinstance(base, ast.Name)]
    return bool(parents) and all(
        _has_extra_forbid(parent, classes, visited.copy()) for parent in parents
    )


def _resolve_schema_class(
    root: Path,
    module: Path,
    name: str,
    cache: dict[Path, ast.Module],
    seen: frozenset[tuple[Path, str]] = frozenset(),
    *,
    before_line: int | None = None,
) -> tuple[Path, ast.ClassDef] | None:
    """Follow explicit model reexports without importing application code."""
    key = (module, name)
    models_root = root / "backend" / "claude_hub" / "models"
    if key in seen or not module.is_relative_to(models_root) or not module.is_file():
        return None
    if module not in cache:
        cache[module] = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    for node in reversed(cache[module].body):
        if before_line is not None and node.lineno >= before_line:
            continue
        if isinstance(node, ast.ClassDef) and node.name == name:
            return module, node
        if not isinstance(node, ast.ImportFrom):
            if _writes_binding(node, name):
                return None
            continue
        if any(alias.name == "*" for alias in node.names):
            return None
        for alias in node.names:
            if (alias.asname or alias.name) != name:
                continue
            if node.level:
                target = module.parent
                for _ in range(node.level - 1):
                    target = target.parent
                if node.module:
                    target = target.joinpath(*node.module.split("."))
            else:
                target = (root / "backend").joinpath(*(node.module or "").split("."))
            target = (
                target / "__init__.py"
                if (target / "__init__.py").is_file()
                else target.with_suffix(".py")
            )
            return _resolve_schema_class(root, target, alias.name, cache, seen | {key})
    return None


def _trusted_config_constructor(module: ast.Module, cls: ast.ClassDef) -> bool:
    """Trust ConfigDict calls only when the constructor is imported from Pydantic."""
    value: ast.expr | None
    for statement in cls.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        if not isinstance(value, ast.Call) or not any(
            isinstance(target, ast.Name) and target.id == "model_config" for target in targets
        ):
            continue
        if not isinstance(value.func, ast.Name) or value.func.id != "ConfigDict":
            return False
        if any(_writes_binding(local, "ConfigDict") for local in cls.body):
            return False
        for binding in reversed(module.body):
            if binding.lineno >= cls.lineno:
                continue
            if (
                isinstance(binding, ast.ImportFrom)
                and binding.module == "pydantic"
                and not binding.level
            ):
                for alias in binding.names:
                    if (alias.asname or alias.name) == "ConfigDict":
                        return alias.name == "ConfigDict"
            if _writes_binding(binding, "ConfigDict"):
                return False
        return False
    return True


def _schema_has_extra_forbid(
    root: Path,
    resolved: tuple[Path, ast.ClassDef],
    cache: dict[Path, ast.Module],
    seen: frozenset[tuple[Path, str]] = frozenset(),
) -> bool:
    module, node = resolved
    key = (module, node.name)
    if key in seen:
        return False
    if any(_writes_binding(statement, "model_config") for statement in node.body):
        return _trusted_config_constructor(cache[module], node) and _has_extra_forbid(node, {})
    parents: list[tuple[Path, ast.ClassDef]] = []
    for base in node.bases:
        if not isinstance(base, ast.Name):
            return False
        parent = _resolve_schema_class(root, module, base.id, cache, before_line=node.lineno)
        if parent is None:
            return False
        parents.append(parent)
    return bool(parents) and all(
        _schema_has_extra_forbid(root, parent, cache, seen | {key}) for parent in parents
    )


def check_strict_schemas(root: Path) -> list[str]:
    errors: list[str] = []
    schemas_path = root / "backend" / "claude_hub" / "models" / "schemas.py"
    if not schemas_path.is_file():
        return errors

    cache: dict[Path, ast.Module] = {}
    for cls_name in STRICT_TASK_STATE_SCHEMAS:
        resolved = _resolve_schema_class(root, schemas_path, cls_name, cache)
        if resolved is None:
            errors.append(f"Strict schema class {cls_name} is missing from models/schemas.py")
            continue
        if not _schema_has_extra_forbid(root, resolved, cache):
            errors.append(f"Schema {cls_name} in models/schemas.py must declare extra='forbid'")
    return errors


def check_warning_error_policy(root: Path) -> list[str]:
    pyproject = root / "backend" / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [f"Cannot parse backend/pyproject.toml: {exc}"]
    filters = list(
        config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("filterwarnings", [])
    )
    required_filters = (*REQUIRED_WARNING_NOISE_FILTERS, *REQUIRED_WARNING_ERROR_FILTERS)
    missing = sorted(set(required_filters) - set(filters))
    errors = [
        f"backend pytest warning filters must include: {warning_filter}"
        for warning_filter in missing
    ]
    missing_error_filters = set(REQUIRED_WARNING_ERROR_FILTERS) - set(filters)
    required_count = len(REQUIRED_WARNING_ERROR_FILTERS)
    if (
        not missing_error_filters
        and tuple(filters[-required_count:]) != REQUIRED_WARNING_ERROR_FILTERS
    ):
        errors.append(
            "backend pytest warning error filters must be the final filters so later rules "
            "cannot override them"
        )
    return errors


def _override_affects_workspace_manager(module_pattern: str) -> bool:
    package = "claude_hub.services.workspace_manager"
    return (
        module_pattern == package
        or module_pattern.startswith(f"{package}.")
        or fnmatch.fnmatchcase(package, module_pattern)
        or fnmatch.fnmatchcase(f"{package}._probe", module_pattern)
    )


def _mypy_weakening_details(settings: dict[str, object]) -> list[str]:
    details: list[str] = []
    if settings.get("ignore_errors") is True:
        details.append("ignore_errors=true")
    disabled = settings.get("disable_error_code", [])
    if isinstance(disabled, str):
        disabled_codes = {disabled}
    elif isinstance(disabled, list):
        disabled_codes = {code for code in disabled if isinstance(code, str)}
    else:
        disabled_codes = set()
    details.extend(sorted(disabled_codes & {"attr-defined", "no-any-return"}))
    return details


def check_mypy_mixin_blindspot_removed(root: Path) -> list[str]:
    pyproject = root / "backend" / "pyproject.toml"
    if not pyproject.is_file():
        return []

    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [f"Cannot parse backend/pyproject.toml: {exc}"]

    mypy_settings = config.get("tool", {}).get("mypy", {})
    errors: list[str] = []
    global_weakening = _mypy_weakening_details(mypy_settings)
    if global_weakening:
        errors.append(
            "backend/pyproject.toml must not globally weaken WorkspaceManager mixin checks "
            f"({', '.join(global_weakening)})"
        )

    overrides = mypy_settings.get("overrides", [])
    for override in overrides:
        raw_modules = override.get("module", [])
        modules = [raw_modules] if isinstance(raw_modules, str) else raw_modules
        if not any(
            isinstance(module, str) and _override_affects_workspace_manager(module)
            for module in modules
        ):
            continue

        weakening = _mypy_weakening_details(override)
        if weakening:
            errors.append(
                "backend/pyproject.toml must not weaken WorkspaceManager mixin checks "
                f"({', '.join(weakening)})"
            )
    return errors


def run_all_checks(root: Path) -> list[str]:
    return [
        *check_entry_docs(root),
        *check_file_size_ratchet(root),
        *check_vue_template_type_coverage(root),
        *check_layering_boundaries(root),
        *check_import_cycle_ratchet(root),
        *check_strict_schemas(root),
        *check_warning_error_policy(root),
        *check_mypy_mixin_blindspot_removed(root),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root directory",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = run_all_checks(root)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
