"""Command-line interface for Claude Hub.

This package wraps the Claude Hub workspace REST API in a Click-based CLI. The
console entry point is exposed as ``claude-hub`` (see ``pyproject.toml``) and the
package is also runnable via ``python -m claude_hub.cli``.
"""

from claude_hub.cli.main import cli

__all__ = ["cli"]
