"""Check the selected backend environment without importing application code."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

PACKAGES = {
    "claude-hub": "claude_hub",
    "pytest": "pytest",
    "black": "black",
    "isort": "isort",
    "mypy": "mypy",
}


def inspect_environment(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    backend = root / "backend"
    expected_prefix = (backend / ".venv").resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    executable = Path(sys.executable)
    expected_python = (root / ".python-test-version").read_text().strip()
    if platform.python_version() != expected_python:
        raise ValueError(f"use declared Python {expected_python}")
    if not sys.flags.isolated:
        raise ValueError("backend provenance must run with Python -I")
    if prefix != expected_prefix or prefix == Path(sys.base_prefix).resolve():
        raise ValueError("uv selected an environment other than backend/.venv")
    if executable.parent.resolve() != (backend / ".venv" / "bin").resolve():
        raise ValueError("uv selected a Python entry outside backend/.venv/bin")
    if Path.cwd().resolve().is_relative_to(root):
        raise ValueError("run provenance from a neutral directory outside the checkout")

    packages: dict[str, Any] = {}
    for distribution_name, module_name in PACKAGES.items():
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise ValueError(f"missing concrete module source: {module_name}")
        origin = Path(spec.origin).resolve(strict=True)
        distribution = metadata.distribution(distribution_name)
        distribution_root = Path(str(distribution.locate_file(""))).resolve(strict=True)
        if not distribution_root.is_relative_to(prefix):
            raise ValueError(f"distribution comes from another environment: {distribution_name}")
        entry: dict[str, Any] = {
            "origin": str(origin),
            "distribution_root": str(distribution_root),
            "version": distribution.version,
        }
        if distribution_name == "claude-hub":
            if origin != (backend / "claude_hub" / "__init__.py").resolve(strict=True):
                raise ValueError("claude_hub resolves to another checkout or installation")
            direct = json.loads(distribution.read_text("direct_url.json") or "null")
            if not isinstance(direct, dict) or not isinstance(direct.get("dir_info"), dict):
                raise ValueError("claude-hub requires editable direct_url metadata")
            if direct["dir_info"].get("editable") is not True:
                raise ValueError("claude-hub is not installed as the checkout's editable project")
            url = urlsplit(str(direct.get("url", "")))
            if url.scheme != "file" or url.netloc not in ("", "localhost"):
                raise ValueError("claude-hub editable source must be a local checkout")
            editable_root = Path(unquote(url.path)).resolve(strict=True)
            if editable_root != backend.resolve():
                raise ValueError("claude-hub editable metadata names another checkout")
            entry["editable_root"] = str(editable_root)
        else:
            files = distribution.files
            if files is None:
                raise ValueError(f"missing installed file metadata: {distribution_name}")
            installed_files = {
                Path(str(distribution.locate_file(item))).resolve() for item in files
            }
            if not origin.is_relative_to(prefix) or origin not in installed_files:
                raise ValueError(f"module source does not belong to its environment: {module_name}")
        packages[distribution_name] = entry
    return {
        "python": platform.python_version(),
        "executable": str(executable),
        "prefix": str(prefix),
        "packages": packages,
        "scope": "selected module provenance; not a lock-closure or code-integrity audit",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = inspect_environment(args.root)
    except (OSError, ValueError, ImportError) as exc:
        print(f"Backend environment check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
