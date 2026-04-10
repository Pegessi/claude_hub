import ipaddress
from typing import List, Optional

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
