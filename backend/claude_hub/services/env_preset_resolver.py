"""Resolve persisted and built-in env presets for CLI/API agent launches."""

from __future__ import annotations

import re
from typing import Dict, Optional

from .env_presets import BUILT_IN_PRESET_IDS, env_preset_manager

_EXPORT_PREFIX = re.compile(r"^export\s+", re.IGNORECASE)
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Built-in preset text kept in sync with frontend useLaunchEnvPresets.ts.
# Never log preset text — it may contain secrets.
BUILT_IN_PRESET_TEXT: dict[str, str] = {
    "none": "",
    "local-proxy-7890": "\n".join(
        [
            "HTTP_PROXY=http://127.0.0.1:7890",
            "HTTPS_PROXY=http://127.0.0.1:7890",
            "ALL_PROXY=socks5://127.0.0.1:7890",
            "NO_PROXY=localhost,127.0.0.1,::1",
        ]
    ),
    "socks-proxy-1080": "\n".join(
        [
            "ALL_PROXY=socks5://127.0.0.1:1080",
            "NO_PROXY=localhost,127.0.0.1,::1",
        ]
    ),
    "volcengine-coding-plan": "\n".join(
        [
            "ANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/coding",
            "ANTHROPIC_MODEL=doubao-seed-2.0-code",
            "ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-code",
            "ANTHROPIC_DEFAULT_SONNET_MODEL=doubao-seed-2.0-code",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL=doubao-seed-2.0-code",
            "CLAUDE_CODE_SUBAGENT_MODEL=doubao-seed-2.0-code",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        ]
    ),
}

BUILT_IN_PRESET_NAMES: dict[str, str] = {
    "none": "No custom env",
    "local-proxy-7890": "Local proxy :7890",
    "socks-proxy-1080": "SOCKS proxy :1080",
    "volcengine-coding-plan": "Volcengine Coding Plan",
}


class EnvPresetNotFoundError(ValueError):
    """Raised when a preset name/id cannot be resolved."""


class EnvPresetParseError(ValueError):
    """Raised when preset text cannot be parsed (message must not include values)."""

    def __init__(
        self,
        reason: str,
        *,
        line_no: int | None = None,
        key: str | None = None,
    ) -> None:
        self.reason = reason
        self.line_no = line_no
        self.key = self._safe_key(key)
        super().__init__(self._format_message())

    @staticmethod
    def _safe_key(key: str | None) -> str | None:
        if not key:
            return None
        candidate = key.strip()
        if _KEY_RE.fullmatch(candidate):
            return candidate
        return None

    def _format_message(self) -> str:
        if self.line_no is not None:
            prefix = f"env preset line {self.line_no}"
        else:
            prefix = "env preset"
        if self.key:
            return f"{prefix}: {self.reason} (key {self.key})"
        return f"{prefix}: {self.reason}"

    def with_preset_context(self, preset_id: str) -> EnvPresetParseError:
        """Re-wrap for custom preset resolution without chaining unsafe causes."""
        reason = (
            f"preset {preset_id!r} has invalid text; " f"fix it in /api/env-presets ({self.reason})"
        )
        return EnvPresetParseError(reason, line_no=self.line_no, key=self.key)


def _strip_shell_quotes(raw: str) -> str:
    """Match frontend ``stripShellQuotes`` — outer quotes only, preserve ``#`` in values."""
    trimmed = raw.strip()
    if len(trimmed) >= 2 and trimmed[0] in ("'", '"') and trimmed[-1] == trimmed[0]:
        return trimmed[1:-1]
    return trimmed


def effective_launch_envs_match(left: Dict[str, str], right: Dict[str, str]) -> bool:
    """True when two resolved launch env dicts are identical (reuse compatibility)."""
    return dict(left or {}) == dict(right or {})


def _parse_env_assignment(raw_line: str, *, line_no: int) -> tuple[str, str]:
    line = raw_line.strip()
    line = _EXPORT_PREFIX.sub("", line, count=1).strip()
    if not line or line.startswith("#"):
        raise EnvPresetParseError("empty assignment", line_no=line_no)
    if "=" not in line:
        raise EnvPresetParseError(
            "expected KEY=VALUE or export KEY=VALUE",
            line_no=line_no,
        )
    key, _, value_part = line.partition("=")
    key = key.strip()
    if not key:
        raise EnvPresetParseError("assignment has empty key", line_no=line_no)
    if not _KEY_RE.fullmatch(key):
        raise EnvPresetParseError(
            "invalid environment variable name",
            line_no=line_no,
            key=key,
        )
    return key, _strip_shell_quotes(value_part)


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse KEY=VALUE / export KEY=VALUE lines; ignore blanks and # comments."""
    env: Dict[str, str] = {}
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            key, value = _parse_env_assignment(line, line_no=line_no)
        except EnvPresetParseError:
            raise
        except ValueError:
            raise EnvPresetParseError("invalid assignment", line_no=line_no) from None
        env[key] = value
    return env


def _lookup_custom_preset(name_or_id: str) -> Optional[tuple[str, str]]:
    needle = name_or_id.strip()
    if not needle:
        return None
    state = env_preset_manager.list_presets()
    custom = state.get("custom_presets") or []
    if not isinstance(custom, list):
        return None
    by_id = {
        str(item.get("id")): str(item.get("text") or "")
        for item in custom
        if isinstance(item, dict) and item.get("id")
    }
    if needle in by_id:
        return needle, by_id[needle]
    lowered = needle.casefold()
    for item in custom:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        preset_id = item.get("id")
        if isinstance(name, str) and name.casefold() == lowered and isinstance(preset_id, str):
            return preset_id, str(item.get("text") or "")
    return None


def resolve_env_preset(name_or_id: str) -> tuple[str, Dict[str, str]]:
    """Resolve preset by id or name. Returns (resolved_id, env dict)."""
    needle = name_or_id.strip()
    if not needle:
        raise EnvPresetNotFoundError("env preset name/id cannot be empty")

    if needle in BUILT_IN_PRESET_IDS:
        return needle, parse_env_text(BUILT_IN_PRESET_TEXT.get(needle, ""))

    lowered = needle.casefold()
    for preset_id, display_name in BUILT_IN_PRESET_NAMES.items():
        if display_name.casefold() == lowered:
            return preset_id, parse_env_text(BUILT_IN_PRESET_TEXT.get(preset_id, ""))

    custom = _lookup_custom_preset(needle)
    if custom is not None:
        preset_id, text = custom
        try:
            return preset_id, parse_env_text(text)
        except EnvPresetParseError as exc:
            raise exc.with_preset_context(preset_id) from None

    raise EnvPresetNotFoundError(
        "Unknown env preset. Use a built-in id "
        f"({', '.join(sorted(BUILT_IN_PRESET_IDS))}) "
        "or a custom preset name/id from /api/env-presets."
    )


def merge_env_with_preset(
    *,
    preset: Optional[str],
    explicit_env: Dict[str, str],
) -> Dict[str, str]:
    """Merge preset env with explicit overrides (explicit wins)."""
    merged: Dict[str, str] = {}
    if preset:
        _, preset_env = resolve_env_preset(preset)
        merged.update(preset_env)
    merged.update(explicit_env)
    return merged


def validate_env_preset_text(text: str) -> Dict[str, str]:
    """Validate custom preset text using the shared parser (raises on invalid input)."""
    return parse_env_text(text)


def resolved_env_preset_keys(env: Dict[str, str]) -> list[str]:
    """Return env keys only — safe for logs/JSON without leaking values."""
    return sorted(env.keys())
