"""Output rendering helpers for the CLI.

Dependency-free: human output is a simple aligned table (lists of dicts) or
``key: value`` lines (single dict). ``--json`` emits indented JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Mapping, Sequence

MAX_CELL_WIDTH = 60

# Match ANSI escape sequences (CSI/SGR etc.) so they can be stripped from cells.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Match any run of whitespace (incl. tab/newline) plus C0/C1 control chars.
_WS_AND_CONTROL_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]+")


def truncate(value: Any, width: int = MAX_CELL_WIDTH) -> str:
    """Stringify ``value``, neutralize control chars, and truncate to ``width``.

    Cells must stay single-line so table columns stay aligned: ANSI escape
    sequences are removed and every run of whitespace/control characters
    (newlines, tabs, other C0/C1 controls) is collapsed to a single space.
    Normal CJK/unicode text is preserved unchanged.
    """
    text = "" if value is None else str(value)
    text = _ANSI_RE.sub("", text)
    text = _WS_AND_CONTROL_RE.sub(" ", text).strip()
    if len(text) > width:
        return text[: width - 1] + "…"
    return text


def _row_cells(row: Any, columns: List[str]) -> List[str]:
    """Return one rendered cell per column for ``row``.

    Rows are normally dicts. To avoid crashing on malformed server responses
    that contain non-dict elements, a non-dict row is coerced into a single
    cell (its stringified value under the first column) with the remaining
    columns left blank, so the table still renders cleanly.
    """
    if isinstance(row, Mapping):
        return [truncate(row.get(col, "")) for col in columns]
    first = truncate(row)
    return [first] + ["" for _ in columns[1:]]


def render_table(rows: Sequence[Any], columns: List[str]) -> str:
    """Render ``rows`` as an aligned text table over ``columns``."""
    header = columns
    cells: List[List[str]] = [_row_cells(row, columns) for row in rows]
    widths = [len(col) for col in header]
    for line in cells:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))

    def fmt(values: Sequence[str]) -> str:
        return "  ".join(val.ljust(widths[i]) for i, val in enumerate(values))

    out = [fmt(header), fmt(["-" * w for w in widths])]
    out.extend(fmt(line) for line in cells)
    return "\n".join(out)


def print_rows(rows: Sequence[Any], columns: List[str]) -> None:
    """Print a table of ``rows``, or a friendly message when empty."""
    import click

    if not rows:
        click.echo("(none)")
        return
    click.echo(render_table(rows, columns))


def _print_single(data: dict) -> None:
    import click

    if not data:
        click.echo("(empty)")
        return
    width = max(len(str(k)) for k in data)
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, default=str, ensure_ascii=False)
        else:
            rendered = "" if value is None else str(value)
        click.echo(f"{str(key).ljust(width)} : {rendered}")


def emit(data: Any, as_json: bool) -> None:
    """Emit ``data`` as JSON or human-readable text."""
    import click

    if as_json:
        click.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        return

    if isinstance(data, list):
        if data and all(isinstance(item, dict) for item in data):
            keys: List[str] = []
            for item in data:
                for k in item:
                    if k not in keys:
                        keys.append(k)
            print_rows(data, keys)
        elif not data:
            click.echo("(none)")
        else:
            for item in data:
                click.echo(str(item))
    elif isinstance(data, dict):
        _print_single(data)
    elif data is None:
        click.echo("(none)")
    else:
        click.echo(str(data))
