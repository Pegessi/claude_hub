"""Authentication API routes."""

import hmac
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth import (
    create_session,
    delete_session,
    get_feishu_auth_url,
    get_user_access_token,
    get_user_info,
)
from ..auth.dependencies import get_current_user, optional_user
from ..auth.proxy import scheme_from_request
from ..config import settings
from ..models.schemas import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Short-lived cookie carrying the OAuth login CSRF state. Scoped to the auth
# routes so it is only sent back on /login and /callback.
OAUTH_STATE_COOKIE_NAME = "claude_hub_oauth_state"
OAUTH_STATE_MAX_AGE_SECONDS = 600
OAUTH_COOKIE_PATH = "/api/auth"


def _use_secure_cookie(request: Request) -> bool:
    """Whether auth cookies should carry the Secure attribute.

    The effective scheme comes from the shared trusted-proxy helper, which
    honors ``X-Forwarded-Proto`` only when the *direct* TCP peer is a
    configured trusted proxy; a remote client cannot send the header to
    upgrade cookie security. HTTPS is also inferred from a configured
    public/frontend URL (TLS terminated upstream without a proto header).
    Plain-HTTP localhost stays without Secure so local dev keeps working.
    """
    if scheme_from_request(request) == "https":
        return True
    configured_url = settings.public_url or settings.frontend_url
    return urlsplit(configured_url).scheme == "https"


def _clear_state_cookie(response: Response, request: Request) -> None:
    """Expire the single-use OAuth state cookie on a response."""
    response.delete_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        path=OAUTH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_use_secure_cookie(request),
    )


def _callback_error(request: Request, status_code: int, detail: str) -> JSONResponse:
    """Failed OAuth callback response that also drops the state cookie.

    The login CSRF cookie is single-use: any failed callback must expire it so
    a rejected/expired state value cannot linger in the browser.
    """
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    _clear_state_cookie(response, request)
    return response


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Redirect to Feishu OAuth page."""
    if not settings.auth_enabled:
        return RedirectResponse(url=settings.frontend_url)

    # Random state stored in an httponly cookie and verified on callback to
    # protect the login flow against CSRF.
    state = secrets.token_urlsafe(32)
    auth_url = get_feishu_auth_url(state)
    response = RedirectResponse(url=auth_url)
    response.set_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        value=state,
        max_age=OAUTH_STATE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_use_secure_cookie(request),
        path=OAUTH_COOKIE_PATH,
    )
    return response


@router.get("/callback")
async def callback(request: Request, code: str, state: str = "") -> Response:
    """Handle Feishu OAuth callback."""
    if not settings.auth_enabled:
        return RedirectResponse(url=settings.frontend_url)

    # Verify the login CSRF state: the callback must carry the same random
    # value issued by /login. Compare in constant time. On any mismatch the
    # state cookie is expired so a rejected login cannot leave it behind.
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    if not expected_state or not state or not hmac.compare_digest(expected_state, state):
        logger.warning("OAuth callback rejected: missing or mismatched state")
        return _callback_error(request, status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")

    # Exchange code for access token
    access_token, refresh_token = await get_user_access_token(code)
    if not access_token or not refresh_token:
        return _callback_error(request, status.HTTP_400_BAD_REQUEST, "Failed to get access token")

    # Get user info
    user_info = await get_user_info(access_token)
    if not user_info:
        return _callback_error(request, status.HTTP_400_BAD_REQUEST, "Failed to get user info")
    logger.info(f"Feishu user info received: {user_info}")

    # Create user object
    user = User(
        open_id=user_info.get("open_id", ""),
        name=user_info.get("name", ""),
        email=user_info.get("email", ""),
        avatar_url=user_info.get("avatar_url"),
    )

    # Check whitelist: open_id first, then email
    allowed_open_ids = settings.allowed_open_ids_list
    allowed_emails = [email.lower() for email in settings.allowed_emails_list]

    access_granted = False

    # Check open_id whitelist if configured
    if allowed_open_ids:
        if user.open_id in allowed_open_ids:
            access_granted = True
        else:
            logger.warning(f"Access denied for user: {user.open_id} (open_id not in whitelist)")
    # Check email whitelist if configured and open_id check not passed
    elif allowed_emails:
        user_email_lower = user.email.lower() if user.email else ""
        if user_email_lower in allowed_emails:
            access_granted = True
        else:
            logger.warning(f"Access denied for user: {user.email} (email not in whitelist)")
    # No whitelist configured: allow all authenticated users
    else:
        access_granted = True

    if not access_granted:
        return _callback_error(request, status.HTTP_403_FORBIDDEN, "Access denied")

    # Create session
    session = create_session(user, access_token, refresh_token)

    # Set session cookie and redirect to frontend
    response = RedirectResponse(url=settings.frontend_url)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session.session_id,
        httponly=True,
        samesite="lax",
        secure=_use_secure_cookie(request),
        max_age=int(settings.session_expire_days * 24 * 60 * 60),
        path="/",
    )
    # Single-use state: drop the login CSRF cookie on success.
    _clear_state_cookie(response, request)

    logger.info(f"User logged in: {user.email}")
    return response


@router.get("/me", response_model=User)
async def get_me(current_user: User = Depends(get_current_user)) -> User:
    """Get current authenticated user."""
    return current_user


@router.get("/check")
async def check_auth(
    request: Request,
    current_user: User | None = Depends(optional_user),
) -> dict:
    """Check if user is authenticated. Returns {authenticated: bool, user: User|null}."""
    from ..auth.dependencies import is_local_network_request

    # Check if request is from local network
    if is_local_network_request(request):
        return {"authenticated": False, "auth_required": False, "user": None}

    if not settings.auth_enabled:
        return {"authenticated": False, "auth_required": False, "user": None}
    if current_user:
        return {"authenticated": True, "auth_required": True, "user": current_user}
    return {"authenticated": False, "auth_required": True, "user": None}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    current_user: User | None = Depends(optional_user),
) -> dict:
    """Logout the current user."""
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        delete_session(session_id)

    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_use_secure_cookie(request),
    )

    if current_user:
        logger.info(f"User logged out: {current_user.email}")

    return {"success": True}
