"""HTTP client for the Claude Hub workspace REST API."""

from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Optional, Type

import httpx

from claude_hub.models import redact_session_json_payload


class HubError(Exception):
    """Raised when a Claude Hub API request fails.

    ``status`` carries the HTTP status code when the failure originated from an
    error response (>= 400); it is ``None`` for transport-level errors.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _format_detail(detail: Any) -> str:
    """Render a FastAPI error ``detail`` into a readable string.

    FastAPI validation errors (HTTP 422) carry a *list* of error dicts in
    ``detail``; str()-ing that verbatim yields an ugly raw repr. For lists we
    extract each item's ``msg`` (prefixed with the offending field name from the
    last element of ``loc`` when available) and join with ``"; "``. Plain string
    details are returned unchanged.
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                msg = item.get("msg")
                loc = item.get("loc")
                field = None
                if isinstance(loc, (list, tuple)) and loc:
                    field = str(loc[-1])
                if msg is not None:
                    parts.append(f"{field}: {msg}" if field else str(msg))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)


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
        data: Any = None,
        files: Any = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        resp = self.request_response(
            method,
            path,
            json=json,
            data=data,
            files=files,
            params=params,
            timeout=timeout,
        )

        if resp.status_code == 204 or not resp.content:
            return None
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            return resp.text
        return resp.json()

    def request_response(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        # Build the request up front so verbose logging reflects the REAL
        # outgoing URL (httpx's own base_url+path joining), and so the URL is
        # available to log even when sending raises.
        # httpx 0.28 Client.send has no timeout kwarg. Per-request timeout
        # belongs on build_request, which stores it in request.extensions.
        build_kwargs: Dict[str, Any] = {
            "json": json,
            "data": data,
            "files": files,
            "params": params,
        }
        if timeout is not None:
            build_kwargs["timeout"] = timeout
        request = self._client.build_request(method, path, **build_kwargs)
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
                    detail = _format_detail(body["detail"])
                else:
                    detail = resp.text
            except ValueError:
                detail = resp.text
            raise HubError(detail or f"HTTP {resp.status_code}", resp.status_code)

        return resp

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Generic REST escape hatch for current and future API routes."""
        return self._request(method, path, json=json, params=params)

    # -- Auth / system ------------------------------------------------------

    def auth_login_response(self) -> httpx.Response:
        """GET /api/auth/login."""
        return self.request_response("GET", "/api/auth/login")

    def auth_callback_response(self, code: str, state: str = "") -> httpx.Response:
        """GET /api/auth/callback."""
        return self.request_response(
            "GET", "/api/auth/callback", params={"code": code, "state": state}
        )

    def get_current_user(self) -> Any:
        """GET /api/auth/me."""
        return self._request("GET", "/api/auth/me")

    def check_auth(self) -> Any:
        """GET /api/auth/check."""
        return self._request("GET", "/api/auth/check")

    def logout(self) -> Any:
        """POST /api/auth/logout."""
        return self._request("POST", "/api/auth/logout")

    def get_network_access(self) -> Any:
        """GET /api/system/network-access."""
        return self._request("GET", "/api/system/network-access")

    # -- Workspaces ---------------------------------------------------------

    def list_workspaces(self) -> Any:
        """GET /api/workspaces."""
        return self._request("GET", "/api/workspaces")

    def create_workspace(self, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces."""
        return self._request("POST", "/api/workspaces", json=body)

    def ensure_workspace(self, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/ensure."""
        return self._request("POST", "/api/workspaces/ensure", json=body)

    def update_workspace(self, workspace_id: str, body: Dict[str, Any]) -> Any:
        """PATCH /api/workspaces/{workspace_id}."""
        return self._request("PATCH", f"/api/workspaces/{workspace_id}", json=body)

    def get_board(self, workspace_id: str) -> Any:
        """GET /api/workspaces/{workspace_id}/board."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/board")

    def dispatch_workspace(self, workspace_id: str) -> None:
        """POST /api/workspaces/{workspace_id}/dispatch (204)."""
        self._request("POST", f"/api/workspaces/{workspace_id}/dispatch")

    def preview_workspace_artifact(
        self,
        workspace_id: str,
        path: str,
        report_id: Optional[str] = None,
    ) -> Any:
        """GET /api/workspaces/{workspace_id}/artifacts/preview."""
        params: Dict[str, Any] = {"path": path}
        if report_id is not None:
            params["report_id"] = report_id
        return self._request(
            "GET",
            f"/api/workspaces/{workspace_id}/artifacts/preview",
            params=params,
        )

    def get_attachment_response(self, attachment_id: str) -> httpx.Response:
        """GET /api/workspaces/attachments/{attachment_id}."""
        return self.request_response("GET", f"/api/workspaces/attachments/{attachment_id}")

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

    def request_task_review(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/request-review."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/request-review", json=body)

    def update_task(self, task_id: str, body: Dict[str, Any]) -> Any:
        """PATCH /api/workspaces/tasks/{task_id}."""
        return self._request("PATCH", f"/api/workspaces/tasks/{task_id}", json=body)

    def delete_task(self, task_id: str) -> None:
        """DELETE /api/workspaces/tasks/{task_id}."""
        self._request("DELETE", f"/api/workspaces/tasks/{task_id}")

    def cleanup_task(self, task_id: str) -> Any:
        """POST /api/workspaces/tasks/{task_id}/cleanup."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/cleanup")

    def reap_task_feedback(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/feedback/reap."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/feedback/reap", json=body)

    def spawn_worker(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/spawn."""
        return self._request("POST", f"/api/workspaces/tasks/{task_id}/spawn", json=body)

    def apply_dispatch_decision(self, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/tasks/{task_id}/dispatch-decision."""
        return self._request(
            "POST",
            f"/api/workspaces/tasks/{task_id}/dispatch-decision",
            json=body,
        )

    def get_task_reports(self, workspace_id: str, task_id: str) -> Any:
        """GET /api/workspaces/{workspace_id}/tasks/{task_id}/reports."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/tasks/{task_id}/reports")

    def list_task_tree(self, workspace_id: str, task_id: Optional[str] = None) -> Any:
        """GET /api/workspaces/{workspace_id}/tasks/tree or .../tasks/{task_id}/tree."""
        if task_id:
            return self._request("GET", f"/api/workspaces/{workspace_id}/tasks/{task_id}/tree")
        return self._request("GET", f"/api/workspaces/{workspace_id}/tasks/tree")

    def get_task_events(
        self,
        workspace_id: str,
        task_id: str,
        since_sequence: int = 0,
        subtree: bool = False,
    ) -> Any:
        """GET Task mailbox events for ``task:<task_id>``."""
        params: Dict[str, Any] = {
            "since_sequence": since_sequence,
            "subtree": subtree,
        }
        return self._request(
            "GET",
            f"/api/workspaces/{workspace_id}/tasks/{task_id}/events",
            params=params,
        )

    def wait_task_events(
        self,
        workspace_id: str,
        task_id: str,
        since_sequence: int = 0,
        subtree: bool = False,
        timeout_seconds: float = 30.0,
    ) -> Any:
        """POST Task mailbox wait (directed long-poll)."""
        params: Dict[str, Any] = {
            "since_sequence": since_sequence,
            "subtree": subtree,
            "timeout_seconds": timeout_seconds,
        }
        return self._request(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks/{task_id}/wait",
            params=params,
            timeout=timeout_seconds + 5.0,
        )

    def ack_task_events(
        self,
        workspace_id: str,
        task_id: str,
        sequence: int,
    ) -> Any:
        """POST Task mailbox ACK (advance consumer cursor)."""
        body = {"sequence": sequence}
        return self._request(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks/{task_id}/ack",
            json=body,
        )

    def followup_task(self, workspace_id: str, task_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/{workspace_id}/tasks/{task_id}/followup."""
        return self._request(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks/{task_id}/followup",
            json=body,
        )

    # -- Agents / sessions --------------------------------------------------

    def ensure_agent(self, workspace_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/{workspace_id}/agent."""
        data = self._request("POST", f"/api/workspaces/{workspace_id}/agent", json=body)
        return redact_session_json_payload(data)

    def delete_session(self, session_id: str) -> None:
        """DELETE /api/workspaces/sessions/{session_id}."""
        self._request("DELETE", f"/api/workspaces/sessions/{session_id}")

    def send_session(self, session_id: str, body: Dict[str, Any]) -> None:
        """POST /api/workspaces/sessions/{session_id}/send (204)."""
        self._request("POST", f"/api/workspaces/sessions/{session_id}/send", json=body)

    def get_terminal_history(self, tab_id: str, lines: int = 100) -> Any:
        """GET /api/terminal/history/{tab_id}?lines=N."""
        return self._request("GET", f"/api/terminal/history/{tab_id}", params={"lines": lines})

    def create_report(self, session_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/sessions/{session_id}/reports."""
        return self._request("POST", f"/api/workspaces/sessions/{session_id}/reports", json=body)

    # -- Tabs / terminal / filesystem / remote -----------------------------

    def list_tabs(self) -> Any:
        """GET /api/tabs."""
        return self._request("GET", "/api/tabs")

    def list_tab_statuses(self) -> Any:
        """GET /api/tabs/status."""
        return self._request("GET", "/api/tabs/status")

    def create_tab(self, body: Dict[str, Any]) -> Any:
        """POST /api/tabs."""
        return self._request("POST", "/api/tabs", json=body)

    def update_tab_order(self, tab_ids: list[str]) -> Any:
        """PUT /api/tabs/order."""
        return self._request("PUT", "/api/tabs/order", json={"tab_ids": tab_ids})

    def duplicate_tab(self, tab_id: str) -> Any:
        """POST /api/tabs/{tab_id}/duplicate."""
        return self._request("POST", f"/api/tabs/{tab_id}/duplicate")

    def get_tab(self, tab_id: str) -> Any:
        """GET /api/tabs/{tab_id}."""
        return self._request("GET", f"/api/tabs/{tab_id}")

    def update_tab(self, tab_id: str, body: Dict[str, Any]) -> Any:
        """PUT /api/tabs/{tab_id}."""
        return self._request("PUT", f"/api/tabs/{tab_id}", json=body)

    def delete_tab(self, tab_id: str) -> None:
        """DELETE /api/tabs/{tab_id}."""
        self._request("DELETE", f"/api/tabs/{tab_id}")

    def list_directory(self, path: Optional[str] = None) -> Any:
        """GET /api/filesystem/list."""
        params = {"path": path} if path is not None else None
        return self._request("GET", "/api/filesystem/list", params=params)

    def get_home_directory(self) -> Any:
        """GET /api/filesystem/home."""
        return self._request("GET", "/api/filesystem/home")

    def list_remote_profiles(self) -> Any:
        """GET /api/remote/profiles."""
        return self._request("GET", "/api/remote/profiles")

    def list_remote_directory(self, profile_id: str, path: Optional[str] = None) -> Any:
        """GET /api/remote/filesystem/list."""
        params: Dict[str, Any] = {"profile_id": profile_id}
        if path is not None:
            params["path"] = path
        return self._request("GET", "/api/remote/filesystem/list", params=params)

    def upload_clipboard_image(
        self,
        path: str,
        content_type: Optional[str] = None,
    ) -> Any:
        """POST /api/clipboard/image."""
        image_path = Path(path)
        with image_path.open("rb") as f:
            files = {
                "image": (
                    image_path.name,
                    f,
                    content_type or "application/octet-stream",
                )
            }
            return self._request("POST", "/api/clipboard/image", files=files)

    # -- Lessons ------------------------------------------------------------

    def list_lessons(self, workspace_id: str, params: Dict[str, Any]) -> Any:
        """GET /api/workspaces/{workspace_id}/lessons."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/lessons", params=params)

    def get_lesson(self, workspace_id: str, lesson_id: str) -> Any:
        """GET /api/workspaces/{workspace_id}/lessons/{lesson_id}."""
        return self._request("GET", f"/api/workspaces/{workspace_id}/lessons/{lesson_id}")

    def delete_lesson(self, workspace_id: str, lesson_id: str) -> Any:
        """DELETE /api/workspaces/{workspace_id}/lessons/{lesson_id}.

        Returns the archived lesson JSON, or ``None`` when the server replies
        with 204 No Content.
        """
        return self._request("DELETE", f"/api/workspaces/{workspace_id}/lessons/{lesson_id}")

    def create_lesson(self, workspace_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/{workspace_id}/lessons."""
        return self._request("POST", f"/api/workspaces/{workspace_id}/lessons", json=body)

    def summarize_lessons(self, workspace_id: str, body: Dict[str, Any]) -> Any:
        """POST /api/workspaces/{workspace_id}/lessons/summarize."""
        return self._request(
            "POST",
            f"/api/workspaces/{workspace_id}/lessons/summarize",
            json=body,
        )
