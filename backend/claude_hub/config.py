import ipaddress
import math
from typing import List, Optional
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(env_file=".env", hide_input_in_errors=True)

    host: str = "127.0.0.1"
    port: int = 8173
    frontend_url: str = "http://localhost:5173"
    ttyd_path: str = "ttyd"
    ttyd_base_port: int = 10000
    default_command: str = "claude"

    # Service-wide MASO target, read from the environment or backend/.env.
    # None/empty allows local discovery. Validate at readiness time so a bad
    # MASO address does not prevent other providers from starting.
    maso_base_url: Optional[str] = None

    # Deployment-only defaults, never serialized into public settings or tabs.
    # Empty/unset URL inherits the process proxy; "direct" disables it.
    claude_proxy_url: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    codex_proxy_url: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    cursor_proxy_url: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    traex_proxy_url: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    claude_no_proxy: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    codex_no_proxy: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    cursor_no_proxy: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    traex_no_proxy: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    cursor_models_probe_timeout_seconds: float = 5.0

    @field_validator("claude_proxy_url", "codex_proxy_url", "cursor_proxy_url", "traex_proxy_url")
    @classmethod
    def _provider_proxy_url(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if not raw or raw == "direct":
            return SecretStr(raw)
        try:
            parsed = urlsplit(raw)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() or ord(char) < 32 for char in raw)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("provider proxy must be an HTTP(S) proxy URL or 'direct'") from None
        return SecretStr(raw)

    @field_validator("cursor_models_probe_timeout_seconds")
    @classmethod
    def _cursor_probe_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("cursor_models_probe_timeout_seconds must be positive and finite")
        return value

    @field_validator("feishu_message_future_skew_seconds")
    @classmethod
    def _feishu_message_future_skew(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("feishu_message_future_skew_seconds must be finite and non-negative")
        return value

    # Keep the long-lived backend log bounded. RotatingFileHandler retains the
    # active file plus ``backend_log_backup_count`` numbered backups.
    backend_log_max_bytes: int = 10 * 1024 * 1024
    backend_log_backup_count: int = 5

    # Serve the built frontend (frontend/dist) from FastAPI at the same origin
    # as the API. start.sh sets SERVE_FRONTEND=true in production mode so the
    # backend serves the SPA directly — no vite dev server, no HMR WebSocket
    # (which force-reloads background tabs), no CORS/proxy. Off by default so
    # dev/CI (no build present) keeps the JSON root endpoint and the backend
    # tests that assert on it stay green.
    serve_frontend: bool = False

    # Feishu OAuth settings
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_redirect_uri: Optional[str] = None

    # Feishu Bot message timestamps can arrive a few milliseconds ahead of the
    # Hub host clock. Keep activation lower bounds exact; this only tolerates
    # small future skew on the upper bound.
    feishu_message_future_skew_seconds: float = Field(
        default=5.0, validation_alias="CLAUDE_HUB_FEISHU_MESSAGE_FUTURE_SKEW_SECONDS"
    )

    # Session settings
    session_secret_key: str = "change-this-in-production"
    session_expire_days: int = 7
    session_cookie_name: str = "claude_hub_session"

    # Auth settings
    auth_allowed_emails: Optional[str] = None  # Comma-separated list
    auth_allowed_open_ids: Optional[str] = None  # Comma-separated list

    # Agent-stream Chat image cache limits, enforced least-recently-used first.
    # The composer stores the original image when it fits the per-image cap
    # (its own 8 MiB upload limit), so sent images keep their quality. The
    # session limits default to the global ones: one 1 GiB LRU pool.
    attachment_max_preview_bytes: int = 8 * 1024 * 1024  # 8 MiB per image
    attachment_max_session_count: int = 5000
    attachment_max_session_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    attachment_max_global_count: int = 5000
    attachment_max_global_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    # Optional age-based eviction. None disables age TTL.
    attachment_max_age_seconds: Optional[float] = None

    # Fork / tab growth limits. A fork loop must not be able to exhaust ttyd
    # ports, processes, and tmux sessions. ``max_forks_per_tab`` bounds how
    # many forks a single source tab can have; ``max_total_tabs`` is a global
    # backstop on the number of live (non-archived) tabs. Both are
    # configurable via env vars.
    max_forks_per_tab: int = 12
    max_total_tabs: int = 200

    @field_validator(
        "max_forks_per_tab",
        "max_total_tabs",
        "backend_log_max_bytes",
        "backend_log_backup_count",
    )
    @classmethod
    def _positive_limit(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be positive, got {v}")
        return v

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


settings = Settings()
