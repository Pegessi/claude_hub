"""HTTP client for the Claude Hub workspace REST API."""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Any, Dict, Optional, Type

import httpx


class HubError(Exception):
    """Raised when a Claude Hub API request fails.

    ``status`` carries the HTTP status code when the failure originated from an
    error response (>= 400); it is ``None`` for transport-level errors.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _parse_cookie_string(cookie: str) -> Dict[str, str]:
    """Parse a ``"k=v; k2=v2"`` cookie header into a dict."""
    jar: Dict[str, str] = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if not key:
            # Skip malformed parts with an empty key (e.g. "=v").
            continue
        jar[key] = value.strip()
    return jar


class HubClient:
    """Thin typed wrapper around the Claude Hub workspace API.

    Authentication is performed via a session cookie. Pass ``token`` to set the
    ``cookie_name`` cookie directly, or ``cookie`` to supply a raw cookie header
    string (``"k=v; k2=v2"``) that may carry additional cookies.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        cookie: Optional[str] = None,
        cookie_name: str = "claude_hub_session",
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
        verbose: bool = False,
    ) -> None:
        self._verbose = verbose
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            trust_env=False,
            transport=transport,
        )
        if token:
            self._client.cookies.set(cookie_name, token)
        elif cookie:
            for key, value in _parse_cookie_string(cookie).items():
                self._client.cookies.set(key, value)

    def __enter__(self) -> "HubClient":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        # Build the request up front so verbose logging reflects the REAL
        # outgoing URL (httpx's own base_url+path joining), and so the URL is
        # available to log even when sending raises.
        request = self._client.build_request(method, path, json=json, params=params)
        if self._verbose:
            print(f"{request.method} {request.url}", file=sys.stderr)
        try:
            resp = self._client.send(request)
        except httpx.HTTPError as e:
            raise HubError(str(e)) from e

        if resp.status_code >= 400:
            detail: str
            try:
                body = resp.json()
                if isinstance(body, dict) and "detail" in body:
                    detail = str(body["detail"])
                else:
                    detail = resp.text
            except ValueError:
                detail = resp.text
            raise HubError(detail or f"HTTP {resp.status_code}", resp.status_code)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- Workspaces ---------------------------------------------------------

    def list_workspaces(self) -> Any:
        """GET /api/workspaces."""
        return self._request("GET", "/api/workspaces")

    def create_workspace(self, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces."""
        return self._request("POST", "/api/workspaces", json=body)

    def get_board(self, workspace_id: str) -> Any:
        """GET /api/workspaces/{workspace_id}/board."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/board")

    # -- Tasks --------------------------------------------------------------

    def create_task(self, workspace_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/{workspace_id}/tasks."""
        return self._request("POST", f"/api/workspaces/{workspace_id}/tasks", json=body)

    def start_task(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/start."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/start", json=body)

    def continue_task(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/continue."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/continue", json=body)

    def abort_task(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/abort."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/abort", json=body)

    # -- Agents / sessions --------------------------------------------------

    def ensure_agent(self, workspace_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/{workspace_id}/agent."""
        return self._request("POST", f"/api/workspaces/{workspace_id}/agent", json=body)

    def send_session(self, session_id: str, body: Dict[str, Any]) -> None:
        """POST /api/workspaces/sessions/{session_id}/send (204)."""
        self._request("POST", f"/api/workspaces/sessions/{session_id}/send", json=body)

    def create_report(self, session_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/sessions/{session_id}/reports."""
        return self._request("POST", f"/api/workspaces/sessions/{session_id}/reports", json=body)

    # -- Lessons ------------------------------------------------------------

    def list_lessons(self, workspace_id: str, params: Dict[str, Any]) -> Any:
        """GET /api/workspaces/{workspace_id}/lessons."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/lessons", params=params)

    def get_lesson(self, workspace_id: str, lesson_id: str) -> Any:
        """GET /api/workspaces/{workspace_id}/lessons/{lesson_id}."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/lessons/{lesson_id}")
