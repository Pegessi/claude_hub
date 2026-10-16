"""Private deployment proxy defaults for local provider subprocesses.

Settings reads backend/.env; do not export its contents into os.environ or
persist these defaults in tab.env. Session/preset values retain precedence.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional
from urllib.parse import unquote, urlsplit

from ..config import settings
from .proxy_rules import with_proxy_env_aliases

_PROVIDERS = frozenset({"claude", "codex", "cursor", "traex"})
_CONFIG_KEYS = frozenset(
    f"{provider}_{suffix}" for provider in _PROVIDERS for suffix in ("proxy_url", "no_proxy")
)


def has_provider_proxy_settings(provider: str) -> bool:
    if provider not in _PROVIDERS:
        return False
    url = getattr(settings, f"{provider}_proxy_url")
    return (
        bool(url and url.get_secret_value())
        or getattr(settings, f"{provider}_no_proxy") is not None
    )


def provider_subprocess_env(
    provider: str,
    inherited: Mapping[str, str],
    overrides: Optional[Mapping[str, Optional[str]]] = None,
) -> dict[str, str]:
    """Merge process < deployment provider < explicit session environment.

    Strip all scoped settings from children, so one provider cannot inherit
    another provider's credentials when configured via process variables.
    """
    env = with_proxy_env_aliases(inherited)
    if provider in _PROVIDERS:
        configured_url = getattr(settings, f"{provider}_proxy_url")
        url = configured_url.get_secret_value() if configured_url is not None else ""
        if url:
            for name in ("http_proxy", "https_proxy", "all_proxy"):
                # Empty values also suppress higher-priority CLI settings
                # and stale tmux variables when materialized in launch files.
                env[name] = env[name.upper()] = ""
            if url != "direct":
                for name in ("http_proxy", "https_proxy"):
                    env[name] = env[name.upper()] = url
        configured_bypass = getattr(settings, f"{provider}_no_proxy")
        if configured_bypass is not None:
            env["no_proxy"] = env["NO_PROXY"] = configured_bypass.get_secret_value()
    for key, value in with_proxy_env_aliases(overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return {key: value for key, value in env.items() if key.lower() not in _CONFIG_KEYS}


def redact_provider_proxy_diagnostic(text: str) -> str:
    """Remove configured private proxy material before publishing diagnostics."""
    for provider in _PROVIDERS:
        configured = getattr(settings, f"{provider}_proxy_url")
        url = configured.get_secret_value() if configured is not None else ""
        parsed = urlsplit(url) if url and url != "direct" else urlsplit("")
        # Provider CLI failures may print the full URL, just the DNS host,
        # or separate auth fields. Do not rely on token-prefix redaction.
        secrets = {
            url if url != "direct" else "",
            parsed.netloc,
            parsed.password or "",
            parsed.username or "",
        }
        if parsed.username:
            secrets.add(unquote(parsed.username))
        if parsed.password:
            secrets.add(unquote(parsed.password))
        bypass = getattr(settings, f"{provider}_no_proxy")
        bypass_hosts: set[str] = set()
        if bypass is not None:
            for entry in bypass.get_secret_value().split(","):
                entry = entry.strip()
                if entry and entry != "*":
                    bypass_hosts.update({entry, entry.lstrip("*.")})
        for secret in sorted(secrets - {""}, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        if parsed.hostname:
            text = re.sub(re.escape(parsed.hostname), "[REDACTED]", text, flags=re.IGNORECASE)
        for host in sorted(bypass_hosts - {""}, key=len, reverse=True):
            text = re.sub(re.escape(host), "[REDACTED]", text, flags=re.IGNORECASE)
    return text
