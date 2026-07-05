"""``storage`` command group — local read-only storage verification tools.

These commands operate directly against on-disk state (default
``~/.claude_hub/workspaces``) without requiring a running server. They are
strictly read-only with respect to the source state root: SQLite artifacts
and export trees are created in a TemporaryDirectory and torn down before the
command returns. They exist to let an operator pre-flight the opt-in SQLite
backend before any phase-4 shadow-write rollout.
"""

from __future__ import annotations

import json as _json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import click

from claude_hub.cli.main import as_json

if TYPE_CHECKING:
    from claude_hub.services.storage.verify import VerificationReport

from claude_hub.services.storage.verify import (
    VerificationError,
    verify_state_dir,
)

DEFAULT_STATE_ROOT = Path.home() / ".claude_hub" / "workspaces"


@click.group()
def storage() -> None:
    """Local storage utilities (read-only; server not required)."""


@storage.command("verify")
@click.option(
    "--state-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help=f"JSON state root to verify (default: {DEFAULT_STATE_ROOT}).",
)
@click.option(
    "--copy/--no-copy",
    "do_copy",
    default=True,
    help="Copy state root to a temp dir before verifying (default on; avoids torn reads if the server is running).",
)
@click.pass_context
def storage_verify(
    ctx: click.Context,
    state_root: Path | None,
    do_copy: bool,
) -> None:
    """Read-only SQLite round-trip verification.

    Loads the JSON state root, writes it into a temp SQLite DB, runs
    integrity_check, exports back to JSON, and compares fingerprints. Never
    writes to the state root. Exits 0 on success, 1 on verification failure,
    2 on usage / IO errors.
    """
    root = state_root or DEFAULT_STATE_ROOT
    if not root.exists():
        raise click.ClickException(
            f"state root does not exist: {root}\n"
            "Hint: pass --state-root to point at a workspace JSON directory, "
            "or run the server once to create the default layout."
        )

    try:
        if do_copy:
            # Snapshot the root into a temp dir so concurrent server saves
            # cannot cause a torn-read JSONDecodeError. This is a one-time
            # file copy; verify_state_dir then runs entirely inside its own
            # temp dir (for SQLite/export), and this copy is removed on return.
            with tempfile.TemporaryDirectory(prefix="claude-hub-verify-src-") as copy_tmp:
                copy_root = Path(copy_tmp) / "state"
                shutil.copytree(
                    root,
                    copy_root,
                    symlinks=False,
                    ignore=shutil.ignore_patterns("*.sqlite3*", "*.bak", "*.staging"),
                )
                report = verify_state_dir(copy_root)
        else:
            report = verify_state_dir(root)
    except VerificationError as e:
        _emit_failure(ctx, e)
        sys.exit(1)
    except (OSError, PermissionError) as e:
        raise click.ClickException(f"IO error reading state root {root}: {e}") from e

    # Re-point report.state_root at the real root (not the temp copy) for display.
    report.state_root = root
    if as_json(ctx):
        click.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        _emit_human(report)
    sys.exit(0)


def _emit_human(report: "VerificationReport") -> None:
    click.echo(f"storage verify: {report.state_root}")
    click.echo(
        f"  source           : "
        f"{report.source_counts.get('workspaces',0)} workspaces, "
        f"{report.source_counts.get('tasks',0)} tasks, "
        f"{report.source_counts.get('sessions',0)} sessions, "
        f"{report.source_counts.get('reports',0)} reports"
    )
    click.echo(f"  integrity_check  : {'ok' if report.integrity_ok else 'FAIL'}")
    click.echo(f"  sqlite roundtrip : {'ok' if report.sqlite_roundtrip_ok else 'FAIL'}")
    click.echo(f"  json export r/t  : {'ok' if report.exported_json_roundtrip_ok else 'FAIL'}")
    if report.warnings:
        click.echo("  warnings:")
        for w in report.warnings:
            click.echo(f"    - {w}")
    click.echo("  result: PASS")


def _emit_failure(ctx: click.Context, err: VerificationError) -> None:
    report = err.report
    if as_json(ctx):
        payload = report.to_dict()
        payload["error"] = str(err)
        payload["ok"] = False
        click.echo(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    click.echo(f"storage verify: {report.state_root}", err=True)
    click.echo(f"  result: FAIL — {err}", err=True)
    for d in report.diffs:
        if d.ok:
            continue
        click.echo(
            f"  diff[{d.kind}]: missing={d.missing} extra={d.extra} changed={d.changed}",
            err=True,
        )
