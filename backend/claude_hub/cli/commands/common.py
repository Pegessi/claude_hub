"""Shared helpers for CLI command modules."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

import click


def parse_json_object(value: Optional[str], option_name: str = "--payload-json") -> Dict[str, Any]:
    """Parse a JSON option that must decode to an object."""
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"invalid {option_name}: {e}") from e
    if not isinstance(parsed, dict):
        raise click.ClickException(f"{option_name} must decode to a JSON object.")
    return parsed


def parse_kv_pairs(values: Iterable[str], option_name: str) -> Dict[str, str]:
    """Parse repeated KEY=VALUE options."""
    parsed: Dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise click.ClickException(f"{option_name} values must be KEY=VALUE.")
        parsed[key] = value
    return parsed


def parse_query_pairs(values: Iterable[str]) -> Dict[str, Any]:
    """Parse repeated query KEY=VALUE options."""
    return parse_kv_pairs(values, "--query")


def merge_payload(payload_json: Optional[str], **values: Any) -> Dict[str, Any]:
    """Merge JSON payload with explicit CLI values.

    Explicit values override JSON fields. Values set to ``None`` are omitted;
    empty tuples are omitted too, which lets repeatable flags remain optional.
    """
    body = parse_json_object(payload_json)
    for key, value in values.items():
        if value is None or value == ():
            continue
        body[key] = list(value) if isinstance(value, tuple) else value
    return body


def parse_attachment_json(values: Iterable[str]) -> list[Dict[str, Any]]:
    """Parse repeated attachment JSON object flags."""
    attachments = []
    for value in values:
        attachments.append(parse_json_object(value, "--attachment-json"))
    return attachments
