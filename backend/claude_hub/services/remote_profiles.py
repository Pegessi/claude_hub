"""Capability resolution for remote SSH profiles.

There are three effective capability states for a remote alias:

* ``normal`` — a conventional sshd. ``ssh host 'cmd'`` runs the argv command and
  a persistent ``ssh -tt host '<bootstrap>'`` carries the interactive tmux
  session. One-shot capture uses the argv channel.
* ``pty_gateway`` — a jump/gateway sshd such as the merlin_dev Trial proxy. The
  OpenSSH command channel is swallowed (``ssh host 'cmd'`` → rc 0, empty) and a
  non-tty stdin is ignored, *but* an argv-less ``ssh -tt`` connection lands in a
  real interactive PTY after a short connect flash. Interactive tabs are
  supported (bootstrap is typed after the real prompt) and one-shot commands go
  through the PTY-exec primitive rather than argv.
* browse-only — a profile that additionally has no drivable interactive PTY
  (``interactive=False``). Terminal tabs and remote agents are rejected; only
  directory listing is offered.

The transport is normally inferred from host/alias signatures, but it can be
pinned explicitly per profile (``"transport": "pty_gateway"``) or via the
``CLAUDE_HUB_PTY_GATEWAY_HOSTS`` env var (comma-separated host substrings).
Detection is deliberately signature-based rather than a single hard-coded alias
so every merlin/seedjob gateway is classified consistently.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..models import RemoteProfile
from ..models.schemas import NONINTERACTIVE_REMOTE_UNSUPPORTED, RemoteTransport

logger = logging.getLogger(__name__)

REMOTE_PROFILES_FILE = Path.home() / ".claude_hub" / "remote_profiles.json"
SSH_CONFIG_FILE = Path.home() / ".ssh" / "config"
_PROFILE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Aliases known to be merlin-style PTY gateways. Host/user signatures below are
# the primary classifier; this set is a convenience for short ssh-config aliases.
_PTY_GATEWAY_ALIAS_IDS = frozenset({"merlin_dev", "merlin_dev_2", "merlin_dev_evo"})
# Substrings in ssh_host that identify a gateway sshd.
_PTY_GATEWAY_HOST_SIGNATURES = ("merlin-ssh-proxy",)
# (host substring, user substring) composite signatures, e.g. ssh-candy.
_PTY_GATEWAY_HOST_USER_SIGNATURES = (("workspace.byted.org", ".seedjob."),)
# Comma-separated extra host substrings treated as gateways (operational override).
_GATEWAY_HOSTS_ENV = "CLAUDE_HUB_PTY_GATEWAY_HOSTS"


@dataclass(frozen=True)
class RemoteCapabilities:
    """Resolved transport capabilities for a profile."""

    transport: RemoteTransport
    interactive_supported: bool

    @property
    def is_pty_gateway(self) -> bool:
        return self.transport == RemoteTransport.PTY_GATEWAY

    # One-shot argv commands are swallowed on a gateway; they must be typed into
    # a real PTY via services.pty_exec.
    @property
    def requires_pty_exec(self) -> bool:
        return self.is_pty_gateway

    @property
    def argv_command_works(self) -> bool:
        return self.transport == RemoteTransport.NORMAL


def _env_gateway_signatures() -> tuple[str, ...]:
    raw = os.environ.get(_GATEWAY_HOSTS_ENV, "")
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _coerce_transport(value: object) -> Optional[RemoteTransport]:
    if value is None:
        return None
    if isinstance(value, RemoteTransport):
        return value
    try:
        return RemoteTransport(str(value))
    except ValueError:
        logger.warning("Ignoring invalid remote transport override: %r", value)
        return None


def resolve_transport(profile: RemoteProfile) -> RemoteTransport:
    """Resolve the effective transport for a profile.

    Explicit ``profile.transport`` wins; otherwise host/alias/env signatures are
    used. Defaults to a conventional ``normal`` host.
    """

    explicit = _coerce_transport(profile.transport)
    if explicit is not None:
        return explicit

    alias = profile.id.strip().lower()
    host = profile.ssh_host.strip().lower()
    user = (profile.user or "").strip().lower()

    if alias in _PTY_GATEWAY_ALIAS_IDS:
        return RemoteTransport.PTY_GATEWAY
    for signature in (*_PTY_GATEWAY_HOST_SIGNATURES, *_env_gateway_signatures()):
        if signature in host or signature in alias:
            return RemoteTransport.PTY_GATEWAY
    for host_sig, user_sig in _PTY_GATEWAY_HOST_USER_SIGNATURES:
        if host_sig in host and user_sig in user:
            return RemoteTransport.PTY_GATEWAY
    # Generic seedjob worker identity (ssh-candy / Trial proxies).
    if ".worker_" in user and ".seedjob." in user:
        return RemoteTransport.PTY_GATEWAY
    return RemoteTransport.NORMAL


def profile_is_pty_gateway(profile: RemoteProfile) -> bool:
    return resolve_transport(profile) == RemoteTransport.PTY_GATEWAY


def profile_interactive_supported(profile: RemoteProfile) -> bool:
    """Whether Terminal tabs / remote agents may run on this profile.

    Both normal hosts and PTY gateways are interactive. Only an explicit
    ``interactive=False`` marks a target as browse-only.
    """

    return profile.interactive is not False


def profile_capabilities(profile: RemoteProfile) -> RemoteCapabilities:
    return RemoteCapabilities(
        transport=resolve_transport(profile),
        interactive_supported=profile_interactive_supported(profile),
    )


def profile_uses_stdin_shell(profile: RemoteProfile) -> bool:
    """Back-compat: True when ``ssh host 'cmd'`` is swallowed (a PTY gateway).

    Historically this also meant "no interactive TTY". That is no longer true for
    merlin-style gateways — those are fully interactive through a real PTY — so
    new code must use :func:`profile_capabilities` instead.
    """

    return profile_is_pty_gateway(profile)


def reject_unsupported_interactive(profile: RemoteProfile | None) -> None:
    """Raise if interactive Terminal/agent use is impossible on this profile."""

    if profile is not None and not profile_interactive_supported(profile):
        raise ValueError(NONINTERACTIVE_REMOTE_UNSUPPORTED)


# Deprecated name: gateways are interactive now, so this is identical to the
# explicit non-interactive gate. Retained for older importers.
def reject_stdin_shell_interactive(profile: RemoteProfile | None) -> None:
    reject_unsupported_interactive(profile)


def _with_transport_flags(profile: RemoteProfile) -> RemoteProfile:
    capabilities = profile_capabilities(profile)
    return profile.model_copy(
        update={
            "stdin_shell": capabilities.is_pty_gateway,
            "transport": capabilities.transport,
        }
    )


class RemoteProfileManager:
    """Loads configured SSH targets for remote tabs."""

    def __init__(self, path: Path = REMOTE_PROFILES_FILE) -> None:
        self.path = path

    def list_profiles(self) -> list[RemoteProfile]:
        profiles = self._load_configured_profiles()
        seen_ids = {profile.id for profile in profiles}
        for profile in self._discover_ssh_config_profiles():
            if profile.id not in seen_ids:
                profiles.append(profile)
                seen_ids.add(profile.id)
        return [_with_transport_flags(profile) for profile in profiles]

    def _load_configured_profiles(self) -> list[RemoteProfile]:
        if not self.path.exists():
            return []

        try:
            with open(self.path, "r") as f:
                raw_profiles = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load remote profiles from {self.path}: {e}")
            return []

        profiles = []
        for raw_profile in raw_profiles:
            try:
                profiles.append(RemoteProfile(**raw_profile))
            except Exception as e:
                logger.warning(f"Skipping invalid remote profile: {e}")
        return profiles

    def _discover_ssh_config_profiles(self) -> list[RemoteProfile]:
        if not SSH_CONFIG_FILE.exists():
            return []

        profiles: list[RemoteProfile] = []
        try:
            lines = SSH_CONFIG_FILE.read_text(errors="ignore").splitlines()
        except OSError as e:
            logger.warning(f"Failed to read SSH config from {SSH_CONFIG_FILE}: {e}")
            return []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # OpenSSH treats `#` as the start of a comment even mid-line.
            # Without this, `Host foo  # ASCII only` is parsed as hosts
            # `foo` and `ASCII`.
            stripped = stripped.split("#", 1)[0].strip()
            if not stripped:
                continue

            parts = stripped.split()
            if len(parts) < 2 or parts[0].lower() != "host":
                continue

            for host_alias in parts[1:]:
                if any(char in host_alias for char in "*?!"):
                    continue
                profile_id = _PROFILE_ID_RE.sub("_", host_alias).strip("_")
                if not profile_id:
                    continue
                profiles.append(
                    RemoteProfile(
                        id=profile_id,
                        name=host_alias,
                        ssh_host=host_alias,
                    )
                )

        return profiles

    def get_profile(self, profile_id: str) -> Optional[RemoteProfile]:
        for profile in self.list_profiles():
            if profile.id == profile_id:
                return profile
        return None


remote_profile_manager = RemoteProfileManager()
