"""Cheap collection-time checks shared by local verification and CI."""

import json

import pytest

MAX_NODE_ID_CHARS = 512
MAX_EXAMPLES = 5


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Reject payload-sized IDs before pytest prints verbose case names."""
    _check_node_ids(items)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    """Also guard partial collections when an early error skips modifyitems."""
    _check_node_ids(session.items)


def _check_node_ids(items: list[pytest.Item]) -> None:
    count = 0
    examples: list[dict[str, str | int | None]] = []
    for item in items:
        length = len(item.nodeid)
        if length <= MAX_NODE_ID_CHARS:
            continue
        count += 1
        if len(examples) < MAX_EXAMPLES:
            path, line, _name = item.location
            examples.append(
                {
                    "file": path[:160],
                    "line": line + 1 if line is not None else None,
                    "node_id_chars": length,
                }
            )
    if count:
        # pytest still calls collection-finish after UsageError; collection-only
        # reporters would otherwise print the rejected IDs from session.items.
        items.clear()
        raise pytest.UsageError(
            json.dumps(
                {
                    "code": "CI_NODE_ID_TOO_LONG",
                    "limit_chars": MAX_NODE_ID_CHARS,
                    "violations": count,
                    "examples": examples,
                    "action": "Set short ids= or pytest.param(id=); preserve test data. "
                    "See docs/CI_RELIABILITY.md.",
                },
                sort_keys=True,
            )
        )
