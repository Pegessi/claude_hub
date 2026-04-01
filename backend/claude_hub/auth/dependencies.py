"""FastAPI dependencies for authentication."""

from fastapi import Depends, HTTPException, status, WebSocket, Query, Cookie, Request
from typing import Optional

from ..config import settings
from ..models.schemas import User
from .session import get_session


def get_client_ip(request: Request) -> str:
    """Get client IP address from request."""
    # Check X-Forwarded-For header first (for reverse proxies)
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # X-Forwarded-For format: client_ip, proxy1_ip, proxy2_ip, ...
        return x_forwarded_for.split(",")[0].strip()

    # Fallback to direct client host
    return request.client.host if request.client else "127.0.0.1"


def is_local_network_request(request: Request) -> bool:
    """Check if request is from local network."""
    if not settings.auth_enabled:
        return True

    client_ip = get_client_ip(request)
    return settings.is_local_network_ip(client_ip)


async def get_current_user_from_cookie(
    session_id: Optional[str] = None,
) -> Optional[User]:
    """Get current user from session cookie."""
    if not settings.auth_enabled:
        # Auth not configured, return None
        return None

    if not session_id:
        return None

    session = get_session(session_id)
    if not session:
        return None

    # Check whitelist: open_id first, then email
    allowed_open_ids = settings.allowed_open_ids_list
    allowed_emails = [email.lower() for email in settings.allowed_emails_list]

    access_granted = False

    # Check open_id whitelist if configured
    if allowed_open_ids:
        if session.user.open_id in allowed_open_ids:
            access_granted = True
    # Check email whitelist if configured and open_id check not passed
    elif allowed_emails:
        user_email_lower = session.user.email.lower() if session.user.email else ""
        if user_email_lower in allowed_emails:
            access_granted = True
    # No whitelist configured: allow all authenticated users
    else:
        access_granted = True

    if not access_granted:
        return None

    return session.user


async def get_current_user(
    request: Request,
    session_id: Optional[str] = Cookie(None, alias=settings.session_cookie_name),
) -> User:
    """
    Get current authenticated user.

    Raises HTTPException if not authenticated.
    """
    # Skip auth for local network requests
    if is_local_network_request(request):
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    if not settings.auth_enabled:
        # Auth not configured, return a dummy user for backward compatibility
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    user = await get_current_user_from_cookie(session_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


async def optional_user(
    request: Request,
    session_id: Optional[str] = Cookie(None, alias=settings.session_cookie_name),
) -> Optional[User]:
    """Get current user if authenticated, None otherwise."""
    # Skip auth for local network requests
    if is_local_network_request(request):
        return None

    if not settings.auth_enabled:
        return None
    return await get_current_user_from_cookie(session_id)


def get_client_ip_ws(websocket: WebSocket) -> str:
    """Get client IP address from WebSocket."""
    # Check X-Forwarded-For header first (for reverse proxies)
    x_forwarded_for = websocket.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    # Fallback to direct client host
    return websocket.client.host if websocket.client else "127.0.0.1"


def is_local_network_request_ws(websocket: WebSocket) -> bool:
    """Check if WebSocket request is from local network."""
    if not settings.auth_enabled:
        return True

    client_ip = get_client_ip_ws(websocket)
    return settings.is_local_network_ip(client_ip)


async def get_current_user_ws(
    websocket: WebSocket,
    session_id: Optional[str] = Query(None),
) -> Optional[User]:
    """
    Get current user for WebSocket connections.

    Returns None if not authenticated. Does NOT close the WebSocket -
    that should be handled by the caller.
    """
    import logging
    import http.cookies
    logger = logging.getLogger(__name__)

    # Skip auth for local network requests
    if is_local_network_request_ws(websocket):
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    # Use session_id from query param first, then manually parse from cookie (Cookie decorator doesn't work for WebSocket)
    effective_session_id = session_id
    if not effective_session_id:
        cookie_header = websocket.headers.get("cookie", "")
        if cookie_header:
            cookies = http.cookies.SimpleCookie()
            cookies.load(cookie_header)
            if settings.session_cookie_name in cookies:
                effective_session_id = cookies[settings.session_cookie_name].value

    logger.info(f"WebSocket auth attempt: session_id(query)={session_id}, session_id(cookie)={effective_session_id}, effective={effective_session_id}")

    if not settings.auth_enabled:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    if not effective_session_id:
        logger.warning("WebSocket auth failed: no session_id provided (neither query nor cookie)")
        return None

    session = get_session(effective_session_id)
    if not session:
        logger.warning(f"WebSocket auth failed: session not found for id={effective_session_id}")
        return None

    logger.info(f"Session found: user={session.user.open_id}, email={session.user.email}")

    # Check whitelist: open_id first, then email
    allowed_open_ids = settings.allowed_open_ids_list
    allowed_emails = [email.lower() for email in settings.allowed_emails_list]

    access_granted = False

    # Check open_id whitelist if configured
    if allowed_open_ids:
        if session.user.open_id in allowed_open_ids:
            access_granted = True
    # Check email whitelist if configured and open_id check not passed
    elif allowed_emails:
        user_email_lower = session.user.email.lower() if session.user.email else ""
        if user_email_lower in allowed_emails:
            access_granted = True
    # No whitelist configured: allow all authenticated users
    else:
        access_granted = True

    if not access_granted:
        return None

    return session.user
