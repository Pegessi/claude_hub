"""Exercise provenance decisions with synthetic modules and installation metadata."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def environment_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    checker = importlib.import_module("check_backend_environment")
    assert checker.__file__ is not None
    assert Path(checker.__file__).resolve() == scripts / "check_backend_environment.py"
    root = tmp_path / "checkout with space"
    backend = root / "backend"
    prefix = backend / ".venv"
    site = prefix / "lib" / "site-packages"
    site.mkdir(parents=True)
    (prefix / "bin").mkdir()
    base = tmp_path / "base-python"
    base.mkdir()
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    (root / ".python-test-version").write_text("3.11.9\n")
    origins: dict[str, Path | None] = {}
    distributions: dict[str, Any] = {}

    def source(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('package code must not be imported')\n")
        return path

    for distribution_name, module_name in checker.PACKAGES.items():
        relative = Path(module_name) / "__init__.py"
        origin = source(backend / relative if module_name == "claude_hub" else site / relative)
        origins[module_name] = origin
        direct = (
            json.dumps({"url": backend.as_uri(), "dir_info": {"editable": True}})
            if module_name == "claude_hub"
            else None
        )
        distributions[distribution_name] = SimpleNamespace(
            version="fixture-version",
            files=[relative],
            locate_file=lambda item, directory=site: directory / str(item),
            read_text=lambda _name, value=direct: value,
        )

    requested: list[str] = []

    def find_spec(name: str) -> SimpleNamespace | None:
        assert "." not in name, "dotted find_spec can execute a parent package"
        requested.append(name)
        origin = origins.get(name)
        return SimpleNamespace(origin=str(origin)) if origin is not None else None

    fake_sys = SimpleNamespace(
        prefix=str(prefix),
        base_prefix=str(base),
        executable=str(prefix / "bin" / "python"),
        flags=SimpleNamespace(isolated=True),
    )
    monkeypatch.setattr(checker, "sys", fake_sys)
    monkeypatch.setattr(checker, "platform", SimpleNamespace(python_version=lambda: "3.11.9"))
    monkeypatch.setattr(
        checker, "importlib", SimpleNamespace(util=SimpleNamespace(find_spec=find_spec))
    )
    monkeypatch.setattr(
        checker, "metadata", SimpleNamespace(distribution=distributions.__getitem__)
    )
    monkeypatch.chdir(neutral)
    return SimpleNamespace(
        checker=checker,
        root=root,
        prefix=prefix,
        site=site,
        sys=fake_sys,
        origins=origins,
        distributions=distributions,
        requested=requested,
        source=source,
    )


def test_environment_sources_are_checked_without_importing_packages(
    environment_case: SimpleNamespace,
) -> None:
    case = environment_case
    report = case.checker.inspect_environment(case.root)
    assert report["prefix"] == str(case.prefix)
    assert report["packages"]["claude-hub"]["editable_root"] == str(case.root / "backend")
    assert set(case.requested) == set(case.checker.PACKAGES.values())
    assert set(report["packages"]) == set(case.checker.PACKAGES)


@pytest.mark.parametrize(
    "fault",
    [
        "prefix",
        "entry",
        "non-isolated",
        "checkout-cwd",
        "app-origin",
        "editable",
        "distribution",
        "tool-origin",
        "in-prefix-shadow",
        "missing-tool",
    ],
)
@pytest.mark.parametrize("python_version", ["3.11.9", "3.12.99"])
def test_environment_rejects_inconsistent_sources(
    environment_case: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    python_version: str,
) -> None:
    case = environment_case
    case.checker.platform.python_version = lambda: python_version
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    if fault == "prefix":
        case.sys.prefix = str(foreign)
    elif fault == "entry":
        case.sys.executable = str(foreign / "python")
    elif fault == "non-isolated":
        case.sys.flags.isolated = False
    elif fault == "checkout-cwd":
        monkeypatch.chdir(case.root)
    elif fault == "app-origin":
        case.origins["claude_hub"] = case.source(foreign / "claude_hub" / "__init__.py")
    elif fault == "editable":
        direct = json.dumps({"url": foreign.as_uri(), "dir_info": {"editable": True}})
        case.distributions["claude-hub"].read_text = lambda _name: direct
    elif fault == "distribution":
        case.distributions["pytest"].locate_file = lambda item: foreign / str(item)
    elif fault == "tool-origin":
        case.origins["pytest"] = case.source(foreign / "pytest.py")
    elif fault == "in-prefix-shadow":
        case.origins["pytest"] = case.source(case.prefix / "shadow" / "pytest.py")
    else:
        case.origins["mypy"] = None
    with pytest.raises(ValueError):
        case.checker.inspect_environment(case.root)


def test_environment_reports_version_drift_without_owning_version_policy(
    environment_case: SimpleNamespace,
) -> None:
    case = environment_case
    case.checker.platform.python_version = lambda: "3.12.99"
    report = case.checker.inspect_environment(case.root)
    assert report["python"] == "3.12.99"
    assert report["prefix"] == str(case.prefix)
    assert report["packages"]["claude-hub"]["editable_root"] == str(case.root / "backend")
