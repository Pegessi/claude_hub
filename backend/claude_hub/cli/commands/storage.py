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
from typing import TYPE_CHECKING, Callable

import click

from claude_hub.cli.main import as_json

if TYPE_CHECKING:
    from claude_hub.services.storage.shadow import ShadowDrift
    from claude_hub.services.storage.verify import VerificationReport

from claude_hub.services.storage import (
    ShadowDriftWarning,
    ShadowError,
    ShadowStorageBackend,
    assert_path_outside_root,
)
from claude_hub.services.storage.json_backend import JsonStorageBackend
from claude_hub.services.storage.sqlite_backend import SqliteStorageBackend
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


@storage.command("shadow")
@click.option(
    "--state-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help=f"JSON state root to shadow (default: {DEFAULT_STATE_ROOT}).",
)
@click.option(
    "--db-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to write the shadow SQLite DB (default: a temp file deleted on exit).",
)
@click.option(
    "--copy/--no-copy",
    "do_copy",
    default=True,
    help="Copy state root to a temp dir first (default on; avoids torn reads).",
)
@click.option(
    "--clean/--no-clean",
    "do_clean",
    default=True,
    help="Remove any existing file at --db-path before writing (default on).",
)
@click.pass_context
def storage_shadow(
    ctx: click.Context,
    state_root: Path | None,
    db_path: Path | None,
    do_copy: bool,
    do_clean: bool,
) -> None:
    """One-shot shadow dry-run.

    Copies the JSON state root, loads it through the JSON backend, dual-writes
    into a SQLite secondary through :class:`ShadowStorageBackend`, and compares
    fingerprints. Prints a drift summary and exits 0 if the secondary matches
    the primary, 1 if drift is detected or an error occurs.

    The secondary DB path is refused if it lives under the state root (live-root
    guard) so shadow artifacts cannot accidentally land alongside live JSON.
    """
    root = state_root or DEFAULT_STATE_ROOT
    if not root.exists():
        raise click.ClickException(
            f"state root does not exist: {root}\n"
            "Hint: pass --state-root or run the server once to create the default layout."
        )

    # Resolve the db path (use a temp file if not supplied) and enforce the
    # live-root guard.
    tmp_db_dir: tempfile.TemporaryDirectory | None = None
    if db_path is None:
        tmp_db_dir = tempfile.TemporaryDirectory(prefix="claude-hub-shadow-db-")
        resolved_db = Path(tmp_db_dir.name) / "state.sqlite3"
    else:
        resolved_db = db_path
        # Guard: refuse paths under the source state root.
        try:
            assert_path_outside_root(resolved_db, root, label="--db-path")
        except ShadowError as e:
            raise click.ClickException(str(e)) from e
        if do_clean and resolved_db.exists():
            try:
                resolved_db.unlink()
            except OSError as e:
                raise click.ClickException(
                    f"could not remove existing --db-path {resolved_db}: {e}"
                ) from e

    errors: list[Exception] = []

    def on_error(exc: Exception) -> None:
        errors.append(exc)

    try:
        if do_copy:
            with tempfile.TemporaryDirectory(prefix="claude-hub-shadow-src-") as copy_tmp:
                copy_root = Path(copy_tmp) / "state"
                shutil.copytree(
                    root,
                    copy_root,
                    symlinks=False,
                    ignore=shutil.ignore_patterns("*.sqlite3*", "*.bak", "*.staging"),
                )
                drift = _run_shadow_cycle(copy_root, resolved_db, on_error)
        else:
            drift = _run_shadow_cycle(root, resolved_db, on_error)
    except (OSError, PermissionError) as e:
        raise click.ClickException(f"IO error running shadow against {root}: {e}") from e
    finally:
        if tmp_db_dir is not None:
            tmp_db_dir.cleanup()

    # Build result
    secondary_errors = [e for e in errors if not isinstance(e, ShadowDriftWarning)]
    drift_warnings = [e for e in errors if isinstance(e, ShadowDriftWarning)]
    ok = not secondary_errors and (drift is not None and drift.ok)

    if as_json(ctx):
        payload = {
            "state_root": str(root),
            "db_path": str(resolved_db),
            "ok": ok,
            "drift": drift.to_dict() if drift else None,
            "secondary_errors": [str(e) for e in secondary_errors],
            "temporary_db": db_path is None,
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        click.echo(f"storage shadow: {root}")
        click.echo(
            f"  db            : {resolved_db}{' (temporary, deleted on exit)' if db_path is None else ''}"
        )
        click.echo(f"  primary→secondary : {'ok' if drift and drift.ok else 'DRIFT'}")
        if drift and not drift.ok:
            click.echo(f"  drift detail  : {drift.describe()}")
        if secondary_errors:
            click.echo(f"  secondary errors: {len(secondary_errors)}")
            for err in secondary_errors:
                click.echo(f"    - {err}")
        click.echo(f"  result: {'PASS' if ok else 'FAIL'}")

    sys.exit(0 if ok else 1)


def _run_shadow_cycle(
    primary_root: Path,
    db_path: Path,
    on_error: Callable[[Exception], None],
) -> "ShadowDrift | None":
    """Run one shadow load→dual-save→compare cycle and return the ShadowDrift."""
    from claude_hub.services.storage import ShadowDrift

    db_path.parent.mkdir(parents=True, exist_ok=True)
    primary = JsonStorageBackend(primary_root)
    secondary = SqliteStorageBackend(db_path)
    shadow = ShadowStorageBackend(primary, secondary, on_error=on_error)
    # Load via primary (authoritative), then dual-save through shadow.
    # save() runs a post-save compare and surfaces drift via on_error.
    snapshot = shadow.load()
    shadow.save(snapshot)
    drift: "ShadowDrift | None" = shadow.last_drift
    return drift
