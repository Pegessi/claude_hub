"""Feishu OAuth integration."""

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from ..config import settings
from ..services.proxy_rules import create_async_client

logger = logging.getLogger(__name__)

FEISHU_API_BASE_URL = "https://open.feishu.cn"
FEISHU_AUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/index"
FEISHU_AUTHORIZE_URL = "https://open.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_USER_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
FEISHU_USER_REFRESH_URL = "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token"
FEISHU_OIDC_USER_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_OIDC_USER_REFRESH_URL = (
    "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token"
)
FEISHU_OAUTH_V2_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

DEFAULT_FEISHU_OAUTH_SCOPES = (
    "im:message im:message.send_as_user im:message.reactions:write_only "
    "im:message:readonly im:chat:readonly offline_access"
)

# The shared factory requires an explicit timeout. Keeping HTTPX's own
# default leaves OAuth timing exactly as it was before the proxy fix.
_TIMEOUT_SECONDS = 5.0


def get_feishu_auth_url(state: str = "", scope: str = DEFAULT_FEISHU_OAUTH_SCOPES) -> str:
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


def _remember_token_bundle(bundle: Dict[str, Any]) -> Dict[str, Any]:
    tok = bundle.get("access_token")
    if isinstance(tok, str) and tok:
        if len(_RECENT_TOKEN_METADATA) > 64:
            _RECENT_TOKEN_METADATA.clear()
        _RECENT_TOKEN_METADATA[tok] = dict(bundle)
    return bundle


def _extract_oauth_v2_bundle(
    payload: Any, fallback_refresh_token: str = ""
) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    if payload.get("code") not in (0, None):
        return None
    source: Dict[str, Any] = payload
    if not source.get("access_token") and isinstance(payload.get("data"), dict):
        source = payload["data"]
    access_token = source.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": source.get("refresh_token") or fallback_refresh_token,
        "expires_in": source.get("expires_in"),
        "refresh_expires_in": (
            source.get("refresh_token_expires_in") or source.get("refresh_expires_in")
        ),
        "scope": str(source.get("scope") or ""),
        "token_type": source.get("token_type", "Bearer"),
    }


async def get_user_access_token_bundle(code: str) -> Optional[Dict[str, Any]]:
    """Exchange authorization code for full user access token payload (with granted ``scope``)."""
    resolved_app_id = settings.feishu_app_id
    resolved_app_secret = settings.feishu_app_secret
    app_access_token = await get_app_access_token(
        app_id=resolved_app_id,
        app_secret=resolved_app_secret,
    )
    if not app_access_token and not (resolved_app_id and resolved_app_secret):
        return None

    async with create_async_client(FEISHU_API_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        # 1. Try OAuth v2 token endpoint (/open-apis/authen/v2/oauth/token) which returns granted scope
        if resolved_app_id and resolved_app_secret:
            v2_body: Dict[str, Any] = {
                "grant_type": "authorization_code",
                "client_id": resolved_app_id,
                "client_secret": resolved_app_secret,
                "code": code,
            }
            if settings.feishu_redirect_uri:
                v2_body["redirect_uri"] = settings.feishu_redirect_uri
            v2_resp = await client.post(FEISHU_OAUTH_V2_TOKEN_URL, json=v2_body)
            try:
                v2_data = v2_resp.json()
            except ValueError:
                v2_data = None
            v2_bundle = _extract_oauth_v2_bundle(v2_data)
            if v2_bundle is not None and v2_bundle.get("scope"):
                return _remember_token_bundle(v2_bundle)
            if (
                v2_resp.status_code != 404
                and isinstance(v2_data, dict)
                and v2_data.get("code") not in (0, None)
            ):
                logger.error(f"Failed to get user access token via OAuth v2: {v2_data}")
                return None

        if not app_access_token:
            return _remember_token_bundle(v2_bundle) if v2_bundle is not None else None

        # 2. Try OIDC v1 token endpoint (/open-apis/authen/v1/oidc/access_token) which includes scope in data
        oidc_resp = await client.post(
            FEISHU_OIDC_USER_TOKEN_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        try:
            oidc_data = oidc_resp.json()
        except ValueError:
            oidc_data = None
        if isinstance(oidc_data, dict):
            if oidc_data.get("code") == 0 and isinstance(oidc_data.get("data"), dict):
                oidc_bundle = dict(oidc_data["data"])
                if oidc_bundle.get("access_token"):
                    return _remember_token_bundle(oidc_bundle)
            elif oidc_resp.status_code != 404 and oidc_data.get("code") not in (
                0,
                None,
            ):
                logger.error(f"Failed to get user access token via OIDC v1: {oidc_data}")
                return None

        if v2_bundle is not None:
            return _remember_token_bundle(v2_bundle)

        # 3. Legacy v1 fallback (/open-apis/authen/v1/access_token)
        response = await client.post(
            FEISHU_USER_TOKEN_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        data = response.json()
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            bundle = dict(data["data"])
            # If legacy v1 omitted scope, attempt authoritative scope lookup via OIDC/v2 refresh if available
            v1_rt = bundle.get("refresh_token")
            if not bundle.get("scope") and isinstance(v1_rt, str) and v1_rt:
                oidc_rt_resp = await client.post(
                    FEISHU_OIDC_USER_REFRESH_URL,
                    headers={"Authorization": f"Bearer {app_access_token}"},
                    json={"grant_type": "refresh_token", "refresh_token": v1_rt},
                )
                try:
                    oidc_rt_data = oidc_rt_resp.json()
                except ValueError:
                    oidc_rt_data = None
                if (
                    isinstance(oidc_rt_data, dict)
                    and oidc_rt_data.get("code") == 0
                    and isinstance(oidc_rt_data.get("data"), dict)
                    and oidc_rt_data["data"].get("access_token")
                    and oidc_rt_data["data"].get("scope")
                ):
                    enriched = {**bundle, **dict(oidc_rt_data["data"])}
                    return _remember_token_bundle(enriched)
            return _remember_token_bundle(bundle)
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
    """Refresh user access token via OAuth v2 / OIDC v1 / v1 refresh endpoint and return token metadata."""
    resolved_app_id = app_id or settings.feishu_app_id
    resolved_app_secret = app_secret or settings.feishu_app_secret
    if not refresh_token:
        return None

    app_access_token = await get_app_access_token(
        app_id=resolved_app_id,
        app_secret=resolved_app_secret,
    )
    if not app_access_token and not (resolved_app_id and resolved_app_secret):
        return None

    async with create_async_client(FEISHU_API_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        v2_bundle: Optional[Dict[str, Any]] = None
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
            v2_bundle = _extract_oauth_v2_bundle(v2_data, fallback_refresh_token=refresh_token)
            if v2_bundle is not None and v2_bundle.get("scope"):
                return _remember_token_bundle(v2_bundle)
            v2_code = v2_data.get("code") if isinstance(v2_data, dict) else None
            # Legacy v1 refresh tokens do not use the v2 `rt-` prefix and return
            # HTTP 400 / code 20026 (`invalid_refresh_token`) on `/v2/oauth/token`.
            # Allow legacy-shaped tokens to fall through to the v1 refresh endpoints
            # while still failing fast for genuinely invalid v2 (`rt-...`) tokens.
            is_v2_token_shape = refresh_token.startswith("rt-")
            if (
                v2_response.status_code != 404
                and isinstance(v2_data, dict)
                and v2_code not in (0, None)
                and (is_v2_token_shape or v2_code not in {20001, 20024, 20026})
            ):
                logger.error(f"Failed to refresh user access token via OAuth v2: {v2_data}")
                return None

        if not app_access_token:
            return _remember_token_bundle(v2_bundle) if v2_bundle is not None else None

        # Try OIDC v1 refresh endpoint (/open-apis/authen/v1/oidc/refresh_access_token)
        oidc_resp = await client.post(
            FEISHU_OIDC_USER_REFRESH_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        try:
            oidc_data = oidc_resp.json()
        except ValueError:
            oidc_data = None
        if isinstance(oidc_data, dict):
            if oidc_data.get("code") == 0 and isinstance(oidc_data.get("data"), dict):
                oidc_bundle = dict(oidc_data["data"])
                if oidc_bundle.get("access_token"):
                    oidc_bundle.setdefault("refresh_token", refresh_token)
                    return _remember_token_bundle(oidc_bundle)
            elif (
                oidc_resp.status_code != 404
                and oidc_data.get("code") not in (0, None, 20001, 20024, 20026)
                and is_v2_token_shape
            ):
                logger.error(f"Failed to refresh user access token via OIDC v1: {oidc_data}")
                return None

        if v2_bundle is not None:
            return _remember_token_bundle(v2_bundle)

        # Legacy v1 refresh endpoint (/open-apis/authen/v1/refresh_access_token)
        response = await client.post(
            FEISHU_USER_REFRESH_URL,
            headers={"Authorization": f"Bearer {app_access_token}"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        data = response.json()
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            bundle = dict(data["data"])
            if bundle.get("access_token"):
                bundle.setdefault("refresh_token", refresh_token)
                return _remember_token_bundle(bundle)
        logger.error(f"Failed to refresh user access token: {data}")
        return None


async def refresh_user_access_token(
    refresh_token: str,
) -> Tuple[Optional[str], Optional[str]]:
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
