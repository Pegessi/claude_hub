"""Authentication API routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from ..auth import (
    create_session,
    delete_session,
    get_feishu_auth_url,
    get_user_access_token,
    get_user_info,
)
from ..auth.dependencies import get_current_user, optional_user
from ..config import settings
from ..models.schemas import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Redirect to Feishu OAuth page."""
    if not settings.auth_enabled:
        return RedirectResponse(url=settings.frontend_url)

    # Generate state (optional, for CSRF protection)
    state = ""
    auth_url = get_feishu_auth_url(state)
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(request: Request, code: str, state: str = "") -> RedirectResponse:
    """Handle Feishu OAuth callback."""
    if not settings.auth_enabled:
        return RedirectResponse(url=settings.frontend_url)

    # Exchange code for access token
    access_token, refresh_token = await get_user_access_token(code)
    if not access_token or not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to get access token",
        )

    # Get user info
    user_info = await get_user_info(access_token)
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to get user info",
        )
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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Create session
    session = create_session(user, access_token, refresh_token)

    # Set session cookie and redirect to frontend
    response = RedirectResponse(url=settings.frontend_url)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session.session_id,
        httponly=True,
        samesite="lax",
        max_age=int(settings.session_expire_days * 24 * 60 * 60),
        path="/",
    )

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

    response.delete_cookie(key=settings.session_cookie_name, path="/")

    if current_user:
        logger.info(f"User logged out: {current_user.email}")

    return {"success": True}
