import json
import logging
import re
from pathlib import Path
from typing import Optional

from ..models import RemoteProfile

logger = logging.getLogger(__name__)

REMOTE_PROFILES_FILE = Path.home() / ".claude_hub" / "remote_profiles.json"
SSH_CONFIG_FILE = Path.home() / ".ssh" / "config"
_PROFILE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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
        return profiles

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
