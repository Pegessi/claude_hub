"""Cheap collection-time checks shared by local verification and CI."""

import json

import pytest

MAX_NODE_ID_CHARS = 512
MAX_EXAMPLES = 5


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Reject payload-sized IDs before pytest prints verbose case names."""
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
