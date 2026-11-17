"""Feishu OAuth integration."""

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from ..config import settings
from ..services.proxy_rules import create_async_client

logger = logging.getLogger(__name__)

FEISHU_API_BASE_URL = "https://open.feishu.cn"
FEISHU_AUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/index"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_USER_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
FEISHU_USER_REFRESH_URL = "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token"
FEISHU_OAUTH_V2_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

# The shared factory requires an explicit timeout. Keeping HTTPX's own
# default leaves OAuth timing exactly as it was before the proxy fix.
_TIMEOUT_SECONDS = 5.0


def get_feishu_auth_url(state: str = "", scope: str = "") -> str:
    """Get the Feishu OAuth authorization URL."""
    if not settings.feishu_app_id or not settings.feishu_redirect_uri:
        raise ValueError("Feishu app ID and redirect URI must be configured")

    params = {
        "app_id": settings.feishu_app_id,
        "redirect_uri": settings.feishu_redirect_uri,
        "response_type": "code",
    }
    if state:
        params["state"] = state
    if scope:
        params["scope"] = scope

    return f"{FEISHU_AUTH_URL}?{urlencode(params)}"


async def get_app_access_token(
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
) -> Optional[str]:
    """Get Feishu app access token."""
    resolved_app_id = app_id or settings.feishu_app_id
    resolved_app_secret = app_secret or settings.feishu_app_secret
    if not resolved_app_id or not resolved_app_secret:
        return None

    async with create_async_client(FEISHU_API_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(
            FEISHU_TOKEN_URL,
            json={
                "app_id": resolved_app_id,
                "app_secret": resolved_app_secret,
            },
        )
        data = response.json()
        if data.get("code") == 0:
            token: Optional[str] = data.get("app_access_token")
            return token
        logger.error(f"Failed to get app access token: {data}")
        return None


_RECENT_TOKEN_METADATA: Dict[str, Dict[str, Any]] = {}


def pop_user_access_token_metadata(access_token: str) -> Dict[str, Any]:
    """Return any OAuth token metadata captured during ``get_user_access_token``."""
    return _RECENT_TOKEN_METADATA.pop(access_token, {})


async def get_user_access_token_bundle(code: str) -> Optional[Dict[str, Any]]:
    """Exchange authorization code for full user access token payload."""
    app_access_token = await get_app_access_token()
    if not app_access_token:
        return None

    async with create_async_client(FEISHU_API_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(
            FEISHU_USER_TOKEN_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        data = response.json()
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            bundle = dict(data["data"])
            tok = bundle.get("access_token")
            if isinstance(tok, str) and tok:
                if len(_RECENT_TOKEN_METADATA) > 64:
                    _RECENT_TOKEN_METADATA.clear()
                _RECENT_TOKEN_METADATA[tok] = bundle
            return bundle
        logger.error(f"Failed to get user access token: {data}")
        return None


async def get_user_access_token(code: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Exchange authorization code for user access token.

    Returns:
        Tuple of (access_token, refresh_token)
    """
    data_data = await get_user_access_token_bundle(code)
    if not data_data:
        return None, None
    return data_data.get("access_token"), data_data.get("refresh_token")


async def refresh_user_access_token_bundle(
    refresh_token: str,
    *,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Refresh user access token via OAuth v2 or v1 endpoint and return token metadata."""
    resolved_app_id = app_id or settings.feishu_app_id
    resolved_app_secret = app_secret or settings.feishu_app_secret
    if not refresh_token:
        return None

    async with create_async_client(FEISHU_API_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        # Try OAuth v2 token refresh when app_id and app_secret are available
        if resolved_app_id and resolved_app_secret:
            v2_response = await client.post(
                FEISHU_OAUTH_V2_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": resolved_app_id,
                    "client_secret": resolved_app_secret,
                    "refresh_token": refresh_token,
                },
            )
            try:
                v2_data = v2_response.json()
            except ValueError:
                v2_data = None
            if (
                isinstance(v2_data, dict)
                and v2_data.get("code") in (0, None)
                and v2_data.get("access_token")
            ):
                return {
                    "access_token": v2_data.get("access_token"),
                    "refresh_token": v2_data.get("refresh_token") or refresh_token,
                    "expires_in": v2_data.get("expires_in"),
                    "refresh_expires_in": v2_data.get("refresh_token_expires_in")
                    or v2_data.get("refresh_expires_in"),
                    "scope": v2_data.get("scope", ""),
                }

        app_access_token = await get_app_access_token(
            app_id=resolved_app_id,
            app_secret=resolved_app_secret,
        )
        if not app_access_token:
            return None

        response = await client.post(
            FEISHU_USER_TOKEN_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        data = response.json()
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            return dict(data["data"])
        logger.error(f"Failed to refresh user access token: {data}")
        return None


async def refresh_user_access_token(refresh_token: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Refresh user access token.

    Returns:
        Tuple of (new_access_token, new_refresh_token)
    """
    data_data = await refresh_user_access_token_bundle(refresh_token)
    if not data_data:
        return None, None
    return data_data.get("access_token"), data_data.get("refresh_token")


async def get_user_info(access_token: str) -> Optional[Dict[str, Any]]:
    """Get user information from Feishu."""
    async with create_async_client(FEISHU_API_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        response = await client.get(
            FEISHU_USER_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        data = response.json()
        if data.get("code") == 0:
            result: Optional[Dict[str, Any]] = data.get("data")
            return result
        logger.error(f"Failed to get user info: {data}")
        return None
