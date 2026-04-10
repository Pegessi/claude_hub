"""Feishu OAuth integration."""

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

FEISHU_AUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/index"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_USER_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


def get_feishu_auth_url(state: str = "") -> str:
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

    return f"{FEISHU_AUTH_URL}?{urlencode(params)}"


async def get_app_access_token() -> Optional[str]:
    """Get Feishu app access token."""
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return None

    async with httpx.AsyncClient() as client:
        response = await client.post(
            FEISHU_TOKEN_URL,
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        data = response.json()
        if data.get("code") == 0:
            token: Optional[str] = data.get("app_access_token")
            return token
        logger.error(f"Failed to get app access token: {data}")
        return None


async def get_user_access_token(code: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Exchange authorization code for user access token.

    Returns:
        Tuple of (access_token, refresh_token)
    """
    app_access_token = await get_app_access_token()
    if not app_access_token:
        return None, None

    async with httpx.AsyncClient() as client:
        response = await client.post(
            FEISHU_USER_TOKEN_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        data = response.json()
        if data.get("code") == 0:
            data_data = data.get("data", {})
            return data_data.get("access_token"), data_data.get("refresh_token")
        logger.error(f"Failed to get user access token: {data}")
        return None, None


async def refresh_user_access_token(refresh_token: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Refresh user access token.

    Returns:
        Tuple of (new_access_token, new_refresh_token)
    """
    app_access_token = await get_app_access_token()
    if not app_access_token:
        return None, None

    async with httpx.AsyncClient() as client:
        response = await client.post(
            FEISHU_USER_TOKEN_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        data = response.json()
        if data.get("code") == 0:
            data_data = data.get("data", {})
            return data_data.get("access_token"), data_data.get("refresh_token")
        logger.error(f"Failed to refresh user access token: {data}")
        return None, None


async def get_user_info(access_token: str) -> Optional[Dict[str, Any]]:
    """Get user information from Feishu."""
    async with httpx.AsyncClient() as client:
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
