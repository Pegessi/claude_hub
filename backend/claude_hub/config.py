import ipaddress
import math
from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    host: str = "127.0.0.1"
    port: int = 8173
    frontend_url: str = "http://localhost:5173"
    ttyd_path: str = "ttyd"
    ttyd_base_port: int = 10000
    default_command: str = "claude"

    # Feishu OAuth settings
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_redirect_uri: Optional[str] = None

    # Session settings
    session_secret_key: str = "change-this-in-production"
    session_expire_days: int = 7
    session_cookie_name: str = "claude_hub_session"

    # Auth settings
    auth_allowed_emails: Optional[str] = None  # Comma-separated list
    auth_allowed_open_ids: Optional[str] = None  # Comma-separated list

    # Agent-stream attachment preview cache limits.
    # The cache stores only browser-generated bounded previews (never the
    # original image bytes). All limits are enforced with FIFO eviction.
    attachment_max_preview_bytes: int = 512 * 1024  # 512 KiB per preview
    attachment_max_session_count: int = 200
    attachment_max_session_bytes: int = 64 * 1024 * 1024  # 64 MiB per session
    attachment_max_global_count: int = 2000
    attachment_max_global_bytes: int = 512 * 1024 * 1024  # 512 MiB global
    # Optional age-based eviction. None disables age TTL.
    attachment_max_age_seconds: Optional[float] = None

    @field_validator(
        "attachment_max_preview_bytes",
        "attachment_max_session_count",
        "attachment_max_session_bytes",
        "attachment_max_global_count",
        "attachment_max_global_bytes",
    )
    @classmethod
    def _attachment_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be positive, got {v}")
        return v

    @field_validator("attachment_max_age_seconds")
    @classmethod
    def _attachment_age_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        # Reject NaN and infinities: a non-finite TTL would make age-based
        # eviction either never fire (inf) or fire unpredictably (NaN).
        if not math.isfinite(v):
            raise ValueError(f"must be finite, got {v}")
        if v <= 0:
            raise ValueError(f"must be positive or None, got {v}")
        return v

    @model_validator(mode="after")
    def _attachment_preview_within_bounds(self) -> "Settings":
        """A single preview cannot exceed the per-session or global byte cap."""
        if self.attachment_max_preview_bytes > self.attachment_max_session_bytes:
            raise ValueError(
                "attachment_max_preview_bytes "
                f"({self.attachment_max_preview_bytes}) must be <= "
                f"attachment_max_session_bytes ({self.attachment_max_session_bytes})"
            )
        if self.attachment_max_preview_bytes > self.attachment_max_global_bytes:
            raise ValueError(
                "attachment_max_preview_bytes "
                f"({self.attachment_max_preview_bytes}) must be <= "
                f"attachment_max_global_bytes ({self.attachment_max_global_bytes})"
            )
        return self

    @property
    def allowed_emails_list(self) -> List[str]:
        """Get allowed emails as a list."""
        if not self.auth_allowed_emails:
            return []
        return [email.strip() for email in self.auth_allowed_emails.split(",") if email.strip()]

    @property
    def allowed_open_ids_list(self) -> List[str]:
        """Get allowed open_ids as a list."""
        if not self.auth_allowed_open_ids:
            return []
        return [
            open_id.strip() for open_id in self.auth_allowed_open_ids.split(",") if open_id.strip()
        ]

    @property
    def auth_enabled(self) -> bool:
        """Check if authentication is enabled."""
        return bool(self.feishu_app_id and self.feishu_app_secret)

    def is_local_network_ip(self, ip_str: str) -> bool:
        """Check if an IP address is from the local network."""
        try:
            ip = ipaddress.ip_address(ip_str)
            # Check if it's a private/local IP
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or
                # 10.0.0.0/8
                (
                    ip.version == 4
                    and ipaddress.IPv4Address("10.0.0.0")
                    <= ip
                    <= ipaddress.IPv4Address("10.255.255.255")
                )
                or
                # 172.16.0.0/12
                (
                    ip.version == 4
                    and ipaddress.IPv4Address("172.16.0.0")
                    <= ip
                    <= ipaddress.IPv4Address("172.31.255.255")
                )
                or
                # 192.168.0.0/16
                (
                    ip.version == 4
                    and ipaddress.IPv4Address("192.168.0.0")
                    <= ip
                    <= ipaddress.IPv4Address("192.168.255.255")
                )
            )
        except ValueError:
            # Invalid IP, default to False
            return False

    class Config:
        env_file = ".env"


settings = Settings()
