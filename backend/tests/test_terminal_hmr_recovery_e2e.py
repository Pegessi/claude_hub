"""Isolated E2E: terminal history survives TerminalView remount and backend reload.

Uses a private tmux server (never the developer default) and temporary HOME.
The real TerminalView harness runs on an isolated Vite dev server
(``VITE_API_TARGET`` -> isolated backend) at ``test/harness/terminal-hmr-harness.html``,
not via any backend test route.
Includes default tmux session identity oracle and cleanup proofs for uvicorn,
private tmux server, Vite, and isolated tabs.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Generator

import pytest
import requests
from playwright.sync_api import Page

from .conftest import (
    diff_summary,
    local_requests_session,
    normalize_terminal_output,
    scale_timeout,
)
from .test_terminal_replay import normalize_xterm_lines

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
_HARNESS_HTML = "/test/harness/terminal-hmr-harness.html"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _default_tmux_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        env.pop(key, None)
    return env


def _isolated_tmux_env(bindir: Path) -> dict[str, str]:
    env = _default_tmux_env()
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


def _isolated_tmux_server_alive(bindir: Path) -> bool:
    env = _isolated_tmux_env(bindir)
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, env=env)
    return result.returncode == 0


def _kill_isolated_tmux(bindir: Path) -> None:
    env = _isolated_tmux_env(bindir)
    subprocess.run(["tmux", "kill-server"], capture_output=True, env=env)


def _tmux_capture(session_name: str, bindir: Path, start: str = "-100000") -> str:
    env = _isolated_tmux_env(bindir)
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-e", "-S", start, "-t", session_name],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _tmux_send(session_name: str, bindir: Path, *keys: str) -> None:
    env = _isolated_tmux_env(bindir)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, *keys],
        capture_output=True,
        env=env,
    )


def _tmux_has_session(session_name: str, bindir: Path) -> bool:
    env = _isolated_tmux_env(bindir)
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
        env=env,
    )
    return result.returncode == 0


def _seed_markers(session_name: str, bindir: Path, prefix: str, count: int = 48) -> None:
    shell_cmd = f"for i in $(seq 0 {count - 1}); do echo {prefix}_$(printf '%04d' $i); done"
    _tmux_send(session_name, bindir, shell_cmd, "Enter")
    target = f"{prefix}_{count - 1:04d}"
    for _ in range(int(scale_timeout(50))):
        time.sleep(0.2)
        if target in _tmux_capture(session_name, bindir):
            return
    pytest.fail(f"timed out waiting for marker {target}")


def _assert_markers_present(lines: list[str], prefix: str, count: int) -> None:
    joined = "\n".join(lines)
    assert f"{prefix}_0000" in joined, f"missing start marker for {prefix}"
    assert f"{prefix}_{count - 1:04d}" in joined, f"missing end marker for {prefix}"


def _assert_buffer_matches_tmux_truth(
    lines: list[str],
    tmux_truth: list[str],
    *,
    label: str,
) -> None:
    actual = normalize_xterm_lines(lines)
    if actual != tmux_truth:
        pytest.fail(
            f"{label}: normalized xterm buffer != tmux truth\n{diff_summary(actual, tmux_truth)}"
        )


def _assert_harness_matches_tmux_at_refresh_done(
    page: Page,
    session_name: str,
    bindir: Path,
    *,
    label: str,
    reason: str = "bootstrap",
    inflight_marker: str | None = None,
) -> None:
    """Compare the correlated bootstrap refresh-done snapshot to tmux truth."""
    page.wait_for_function(
        """(expectedReason) => {
          const events = window.__harnessRefreshDoneEvents || [];
          const matches = events.filter(function(e) {
            return e &&
              e.reason === expectedReason &&
              e.ok === true &&
              e.expectedRequestId != null &&
              e.requestId === e.expectedRequestId &&
              Array.isArray(e.lines);
          });
          return matches.length === 1;
        }""",
        arg=reason,
        timeout=int(scale_timeout(25) * 1000),
    )
    snapshot: dict[str, Any] | None = page.evaluate(
        """(expectedReason) => {
            const events = window.__harnessRefreshDoneEvents || [];
            const matches = events.filter(function(e) {
              return e &&
                e.reason === expectedReason &&
                e.ok === true &&
                e.expectedRequestId != null &&
                e.requestId === e.expectedRequestId &&
                Array.isArray(e.lines);
            });
            return matches.length === 1 ? matches[0] : null;
        }""",
        arg=reason,
    )
    assert snapshot is not None, f"{label}: correlated bootstrap refresh-done capture missing"
    assert snapshot.get("ok") is True, f"{label}: refresh-done not ok: {snapshot}"
    assert snapshot.get("reason") == reason, f"{label}: unexpected reason: {snapshot}"
    request_id = snapshot.get("requestId")
    expected_request_id = snapshot.get("expectedRequestId")
    assert isinstance(request_id, str) and request_id, f"{label}: missing requestId: {snapshot}"
    assert (
        isinstance(expected_request_id, str) and expected_request_id
    ), f"{label}: missing independently captured expectedRequestId: {snapshot}"
    assert (
        request_id == expected_request_id
    ), f"{label}: refresh-done requestId must match child bootstrap correlation; got {request_id!r} vs {expected_request_id!r}"
    if inflight_marker is not None:
        normalized = normalize_xterm_lines(snapshot["lines"])
        marker_lines = [ln for ln in normalized if ln.strip() == inflight_marker]
        assert (
            len(marker_lines) == 1
        ), f"{label}: inflight marker must appear on exactly one line; lines={marker_lines!r}"
    tmux_truth = normalize_terminal_output(_tmux_capture(session_name, bindir))
    _assert_buffer_matches_tmux_truth(snapshot["lines"], tmux_truth, label=label)


_HARNESS_REFRESH_CAPTURE_INIT = """
(() => {
  function readActiveChildExpectedRequestId() {
    const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
    const child = iframe && iframe.contentWindow;
    if (!child) return null;
    try {
      const ids = child.__harnessBootstrapCorrelationRequestIds;
      if (!Array.isArray(ids) || ids.length === 0) return null;
      return ids[ids.length - 1].requestId || null;
    } catch (error) {
      return null;
    }
  }

  if (window !== window.top) {
    if (window.__harnessIframeBootstrapCaptureInstalled) return;
    window.__harnessIframeBootstrapCaptureInstalled = true;
    window.__harnessBootstrapCorrelationRequestIds = [];
    window.addEventListener('message', function(event) {
      const data = event.data;
      if (!data || data.type !== 'terminal-bootstrap-correlation') return;
      if (!data.requestId) return;
      window.__harnessBootstrapCorrelationRequestIds.push({
        requestId: data.requestId,
        documentGeneration: data.documentGeneration,
        capturedAt: Date.now(),
      });
    });
    return;
  }

  if (window.__harnessRefreshCaptureInstalled) return;
  window.__harnessRefreshCaptureInstalled = true;
  window.__harnessRefreshDoneEvents = [];

  function captureHarnessXtermAtRefreshDone(detail) {
    if (!detail) return;
    const expectedRequestId = readActiveChildExpectedRequestId();
    const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
    const term = iframe && iframe.contentWindow && iframe.contentWindow.term;
    const lines = [];
    if (detail.ok !== false && term && term.buffer && term.buffer.active) {
      const buf = term.buffer.active;
      for (let i = 0; i < buf.length; i++) {
        const line = buf.getLine(i);
        if (line) lines.push(line.translateToString(true));
      }
    }
    window.__harnessRefreshDoneEvents.push({
      requestId: detail.requestId,
      expectedRequestId: expectedRequestId,
      reason: detail.reason,
      ok: detail.ok,
      lines: lines,
    });
  }

  window.addEventListener('terminal-history-refresh-done', function(event) {
    captureHarnessXtermAtRefreshDone(event && event.detail);
  });
})();
"""


def _ensure_harness_refresh_done_capture(page: Page) -> None:
    page.add_init_script(_HARNESS_REFRESH_CAPTURE_INIT)


def _reset_harness_refresh_done_capture(page: Page) -> None:
    page.evaluate("""() => {
          window.__harnessRefreshDoneEvents = [];
          document.querySelectorAll('.hmr-harness-terminal iframe').forEach(function(iframe) {
            try {
              if (iframe.contentWindow) {
                iframe.contentWindow.__harnessBootstrapCorrelationRequestIds = [];
              }
            } catch (error) {}
          });
        }""")


def _assert_harness_matches_tmux(
    page: Page,
    session_name: str,
    bindir: Path,
    *,
    label: str,
) -> None:
    page.wait_for_function(
        """() => {
            const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
            const term = iframe && iframe.contentWindow && iframe.contentWindow.term;
            return !!(term && term.__claudeHubReplayDone === true);
        }""",
        timeout=int(scale_timeout(20) * 1000),
    )
    page.wait_for_function(
        """() => {
            const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
            const term = iframe && iframe.contentWindow && iframe.contentWindow.term;
            return !term || term.__claudeHubReplayBuffering !== true;
        }""",
        timeout=int(scale_timeout(20) * 1000),
    )
    tmux_truth = normalize_terminal_output(_tmux_capture(session_name, bindir))
    lines = _read_harness_xterm_lines(page)
    _assert_buffer_matches_tmux_truth(lines, tmux_truth, label=label)


def _start_isolated_backend(tmp_path: Path) -> dict[str, Any]:
    real_tmux = shutil.which("tmux")
    real_ttyd = shutil.which("ttyd")
    if not real_tmux or not real_ttyd:
        pytest.skip("tmux/ttyd not available on PATH")

    tmp_home = tmp_path / "home"
    tmp_home.mkdir(exist_ok=True)
    bindir = tmp_home / "bin"
    bindir.mkdir(exist_ok=True)
    server_marker = tmp_path / "tmux_server_name"
    if server_marker.exists():
        server_name = server_marker.read_text(encoding="utf-8").strip()
    else:
        server_name = f"ch-hmr-{uuid.uuid4().hex[:12]}"
        server_marker.write_text(server_name, encoding="utf-8")
    wrapper = bindir / "tmux"
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.quote(real_tmux) + " -L " + shlex.quote(server_name) + ' "$@"\n',
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)

    claude_hub_dir = tmp_home / ".claude_hub"
    claude_hub_dir.mkdir(parents=True, exist_ok=True)
    (claude_hub_dir / "remote_profiles.json").write_text(
        '[{"id": "test-loopback", "name": "loopback", "ssh_host": "127.0.0.1"}]',
        encoding="utf-8",
    )

    port = _free_port()
    backend_dir = Path(__file__).parent.parent
    venv_python = backend_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    env = _isolated_tmux_env(bindir)
    env["HOME"] = str(tmp_home)

    log_path = tmp_path / "isolated-backend.log"
    log_file = log_path.open("wb")
    proc = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "uvicorn",
            "claude_hub.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=str(backend_dir),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    session = local_requests_session()
    for _ in range(80):
        try:
            if session.get(f"{base_url}/health", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        log_file.close()
        tail = log_path.read_text(errors="ignore")[-2000:] if log_path.exists() else ""
        pytest.fail(f"isolated backend failed to start: {tail}")

    return {
        "base_url": base_url,
        "session": session,
        "proc": proc,
        "log_file": log_file,
        "bindir": bindir,
        "tmp_home": tmp_home,
        "server_name": server_name,
        "port": port,
    }


def _stop_backend(ctx: dict[str, Any]) -> None:
    proc = ctx.get("proc")
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    ctx["proc"] = None
    log_file = ctx.get("log_file")
    if log_file is not None:
        log_file.close()
        ctx["log_file"] = None


def _start_harness_vite(backend_url: str, tmp_path: Path) -> dict[str, Any]:
    if not (_FRONTEND_DIR / "node_modules").is_dir():
        pytest.skip("frontend node_modules missing; run: cd frontend && pnpm install")

    port = _free_port()
    log_path = tmp_path / "harness-vite.log"
    log_file = log_path.open("wb")
    env = _default_tmux_env()
    env["VITE_API_TARGET"] = backend_url
    env["VITE_PORT"] = str(port)
    # Ephemeral MPA config for harness dev only — frontend/vite.config.ts stays SPA/base.
    harness_config = tmp_path / "vite.harness.config.ts"
    harness_config.write_text(
        f"""import {{ mergeConfig }} from 'vite'
import base from {repr(str(_FRONTEND_DIR / "vite.config.ts"))}

export default mergeConfig(base, {{
  appType: 'mpa',
}})
""",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            "pnpm",
            "exec",
            "vite",
            "--config",
            str(harness_config),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--strictPort",
        ],
        cwd=str(_FRONTEND_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    harness_url = f"http://127.0.0.1:{port}"
    session = local_requests_session()
    for _ in range(120):
        try:
            resp = session.get(f"{harness_url}{_HARNESS_HTML}", timeout=1)
            if resp.status_code == 200 and "terminal-hmr-harness" in resp.text:
                break
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            log_file.close()
            tail = log_path.read_text(errors="ignore")[-2000:] if log_path.exists() else ""
            pytest.fail(f"harness vite exited early: {tail}")
        time.sleep(0.2)
    else:
        proc.kill()
        log_file.close()
        tail = log_path.read_text(errors="ignore")[-2000:] if log_path.exists() else ""
        pytest.fail(f"harness vite failed to start: {tail}")

    return {
        "harness_url": harness_url,
        "vite_proc": proc,
        "vite_log_file": log_file,
        "vite_port": port,
    }


def _stop_harness_vite(ctx: dict[str, Any]) -> None:
    proc = ctx.get("vite_proc")
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    ctx["vite_proc"] = None
    log_file = ctx.get("vite_log_file")
    if log_file is not None:
        log_file.close()
        ctx["vite_log_file"] = None


def _restart_hmr_stack(ctx: dict[str, Any], tmp_path: Path) -> None:
    """Restart isolated backend + Vite harness (new backend port -> new Vite proxy)."""
    _stop_harness_vite(ctx)
    _stop_backend(ctx)
    backend = _start_isolated_backend(tmp_path)
    vite = _start_harness_vite(backend["base_url"], tmp_path)
    ctx.clear()
    ctx.update({**backend, **vite})


def _install_harness_content_pending_observer(page: Page) -> None:
    """Watch the active iframe for content-pending class via MutationObserver."""
    page.evaluate("""() => {
        window.__harnessContentPendingSeen = false;
        window.__harnessContentPendingObserver = null;
        window.__harnessRootObserver = null;
        window.__harnessObservedIframe = null;

        function observeIframe(iframe) {
          if (!iframe) return;
          const check = () => {
            if (iframe.classList.contains('content-pending')) {
              window.__harnessContentPendingSeen = true;
            }
          };
          check();
          if (window.__harnessContentPendingObserver) {
            window.__harnessContentPendingObserver.disconnect();
          }
          const obs = new MutationObserver(check);
          obs.observe(iframe, { attributes: true, attributeFilter: ['class'] });
          window.__harnessContentPendingObserver = obs;
          window.__harnessObservedIframe = iframe;
        }

        const root = document.querySelector('[data-testid="terminal-hmr-harness"]');
        if (!root) throw new Error('terminal-hmr-harness root missing');
        observeIframe(root.querySelector('.hmr-harness-terminal iframe.active'));

        const rootObs = new MutationObserver(() => {
          const iframe = root.querySelector('.hmr-harness-terminal iframe.active');
          if (iframe && iframe !== window.__harnessObservedIframe) {
            observeIframe(iframe);
          }
        });
        rootObs.observe(root, { childList: true, subtree: true });
        window.__harnessRootObserver = rootObs;
      }""")


def _teardown_harness_content_pending_observer(page: Page) -> None:
    page.evaluate("""() => {
        if (window.__harnessContentPendingObserver) {
          window.__harnessContentPendingObserver.disconnect();
          window.__harnessContentPendingObserver = null;
        }
        if (window.__harnessRootObserver) {
          window.__harnessRootObserver.disconnect();
          window.__harnessRootObserver = null;
        }
        window.__harnessObservedIframe = null;
      }""")


def _wait_harness_content_pending(page: Page, timeout_ms: int = 5000) -> None:
    page.wait_for_function(
        "() => window.__harnessContentPendingSeen === true",
        timeout=int(scale_timeout(timeout_ms / 1000) * 1000),
    )


def _harness_state(page: Page) -> dict[str, Any]:
    state: dict[str, Any] | None = page.evaluate(
        "() => window.__claudeHubHmrHarness?.readHarnessState() ?? null"
    )
    assert state is not None, "harness API unavailable"
    return state


def _wait_harness_content_ready(page: Page, timeout_ms: int = 20000) -> None:
    page.wait_for_function(
        """() => {
          const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
          return iframe && !iframe.classList.contains('content-pending');
        }""",
        timeout=int(scale_timeout(timeout_ms / 1000) * 1000),
    )


def _read_harness_xterm_lines(page: Page) -> list[str]:
    result: list[str] | None = page.evaluate("""() => {
          const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
          if (!iframe || !iframe.contentWindow) return null;
          const term = iframe.contentWindow.term;
          if (!term) return null;
          const buf = term.buffer.active;
          const lines = [];
          for (let i = 0; i < buf.length; i++) {
            const line = buf.getLine(i);
            if (line) lines.push(line.translateToString(true));
          }
          return lines;
        }""")
    assert result is not None, "harness iframe xterm buffer unavailable"
    return normalize_xterm_lines(result)


def _promote_tab_to_remote(http: Any, base: str, tab_id: str) -> None:
    """Mark tab as remote Claude metadata only (tmux stays on private server)."""
    resp = http.put(
        f"{base}/api/tabs/{tab_id}",
        json={"agent_type": "claude", "target": "remote", "remote_profile_id": "test-loopback"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("agent_type") == "claude"
    assert body.get("target") == "remote"


def _promote_tab_to_local_claude(http: Any, base: str, tab_id: str) -> None:
    """Mark tab agent_type=claude without starting a provider CLI."""
    resp = http.put(f"{base}/api/tabs/{tab_id}", json={"agent_type": "claude"})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("agent_type") == "claude"


def _bootstrap_terminal_tab_private_tmux(
    page: Page,
    base: str,
    tab_id: str,
    bindir: Path,
) -> str:
    """Open private proxy once to create tmux, then leave before metadata promotion."""
    session_name = f"claude-hub-{tab_id[:8]}"
    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail(f"isolated tmux session {session_name} not created")
    page.goto("about:blank")
    return session_name


def _route_hold_first_fetch(gate: dict[str, Any], route) -> None:
    gate["fetch_count"] += 1
    if gate["fetch_count"] == 1:
        try:
            gate["held_route"] = route
            gate["held_response"] = route.fetch()
        except Exception as exc:
            gate["errors"].append(f"route fetch: {exc}")
            route.abort()
        return
    try:
        route.fulfill(response=route.fetch())
    except Exception as exc:
        gate["errors"].append(f"route retry fulfill: {exc}")
        route.abort()


def _pump_until_held_route(gate: dict[str, Any], page: Page, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if gate.get("held_route") is not None and gate.get("held_response") is not None:
            return
        page.wait_for_timeout(50)
    pytest.fail("held history fetch was not captured by route handler")


def _fulfill_held_route(gate: dict[str, Any]) -> None:
    held_route = gate.get("held_route")
    held_response = gate.get("held_response")
    if held_route is None or held_response is None:
        gate["errors"].append("held fetch missing at fulfill time")
        return
    try:
        held_route.fulfill(response=held_response)
    except Exception as exc:
        gate["errors"].append(f"held route fulfill: {exc}")


def _install_iframe_history_fetch_spy(page: Page) -> None:
    page.evaluate("""() => {
        window.__iframeHistoryFetches = 0;
        function hook(win) {
          if (!win || win.__claudeHubHistoryFetchSpy) return;
          win.__claudeHubHistoryFetchSpy = true;
          const origFetch = win.fetch.bind(win);
          win.fetch = function(input, init) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/api/terminal/history/') >= 0) {
              window.__iframeHistoryFetches += 1;
            }
            return origFetch(input, init);
          };
        }
        document.querySelectorAll('.hmr-harness-terminal iframe').forEach(function(iframe) {
          try { hook(iframe.contentWindow); } catch (e) {}
        });
      }""")


def _initial_inflight_injector_thread(
    gate: dict[str, Any],
    session_name: str,
    bindir: Path,
    inflight_marker: str,
) -> None:
    try:
        if not gate["upstream_ready"].wait(timeout=scale_timeout(30)):
            gate["errors"].append("upstream_ready timeout in initial inflight injector")
            return
        _tmux_send(session_name, bindir, f"echo {inflight_marker}", "Enter")
        deadline = time.time() + scale_timeout(30)
        while time.time() < deadline:
            if inflight_marker in _tmux_capture(session_name, bindir):
                return
            time.sleep(0.2)
        gate["errors"].append("initial inflight marker not written to tmux")
    except Exception as exc:
        gate["errors"].append(f"initial inflight injector: {exc}")
    finally:
        gate["release"].set()


def _wait_harness_bootstrap_boundary(page: Page, timeout_ms: int = 25000) -> None:
    """Wait for bootstrap to arm (content-pending) then refresh-done capture."""
    page.wait_for_selector(".hmr-harness-terminal iframe.active", timeout=15000)
    page.wait_for_function(
        """() => {
          const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
          return iframe && iframe.classList.contains('content-pending');
        }""",
        timeout=int(scale_timeout(10) * 1000),
    )
    page.wait_for_function(
        """(expectedReason) => {
          const events = window.__harnessRefreshDoneEvents || [];
          return events.some(function(e) {
            return e &&
              e.reason === expectedReason &&
              e.ok === true &&
              e.expectedRequestId != null &&
              e.requestId === e.expectedRequestId &&
              Array.isArray(e.lines);
          });
        }""",
        arg="bootstrap",
        timeout=int(scale_timeout(timeout_ms / 1000) * 1000),
    )
    page.wait_for_function(
        """() => {
          const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
          return iframe && !iframe.classList.contains('content-pending');
        }""",
        timeout=int(scale_timeout(10) * 1000),
    )


def _open_harness(
    page: Page,
    harness_url: str,
    tab_id: str,
    min_lines: int | None = None,
    agent_type: str = "terminal",
    alt_tab_id: str | None = None,
) -> None:
    _ensure_harness_refresh_done_capture(page)
    _reset_harness_refresh_done_capture(page)
    query = f"tabId={tab_id}&agentType={agent_type}"
    if alt_tab_id:
        query += f"&altTabId={alt_tab_id}"
    page.goto(f"{harness_url}{_HARNESS_HTML}?{query}")
    page.wait_for_selector('[data-testid="terminal-hmr-harness"]', timeout=15000)
    _wait_harness_bootstrap_boundary(page)
    frame = page.frame_locator(".hmr-harness-terminal iframe.active")
    frame.locator(".xterm").wait_for(timeout=15000)
    if min_lines is not None:
        page.wait_for_function(
            """(minLines) => {
              const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
              const term = iframe && iframe.contentWindow && iframe.contentWindow.term;
              const buf = term && term.buffer && term.buffer.active;
              return !!buf && buf.length >= minLines;
            }""",
            arg=min_lines,
            timeout=int(scale_timeout(20) * 1000),
        )


def _trigger_harness_remount(page: Page) -> None:
    prior_gen = _harness_state(page)["remountGeneration"]
    old_iframe = page.locator(".hmr-harness-terminal iframe.active").element_handle()
    assert old_iframe is not None, "active iframe missing before remount"

    _install_harness_content_pending_observer(page)
    try:
        _reset_harness_refresh_done_capture(page)
        page.evaluate("""async () => {
            window.__harnessRemountDone = false;
            const task = (async () => {
              await window.__claudeHubHmrHarness.remountTerminalView();
              window.__harnessRemountDone = true;
            })();
            window.__harnessRemountTask = task;
          }""")

        _wait_harness_content_pending(page)
        page.wait_for_function(
            "() => window.__harnessRemountDone === true",
            timeout=int(scale_timeout(20) * 1000),
        )

        page.wait_for_function("(el) => !el.isConnected", arg=old_iframe, timeout=5000)
        new_iframe = page.locator(".hmr-harness-terminal iframe.active").element_handle()
        assert new_iframe is not None, "new iframe missing after remount"
        assert page.evaluate(
            "([oldEl, newEl]) => oldEl !== newEl",
            arg=[old_iframe, new_iframe],
        ), "remount must replace the iframe element"

        st = _harness_state(page)
        assert st["remountGeneration"] > prior_gen
        assert st["lastRemountOldIframeDetached"] is True
        _wait_harness_bootstrap_boundary(page)
    finally:
        _teardown_harness_content_pending_observer(page)


def _trigger_harness_iframe_reload(page: Page) -> None:
    prior = _harness_state(page)
    prior_nav = prior["iframeNavGeneration"]
    prior_iframe_src = prior["activeIframeSrc"]
    old_iframe = page.locator(".hmr-harness-terminal iframe.active").element_handle()
    assert old_iframe is not None, "active iframe missing before reload"

    _install_harness_content_pending_observer(page)
    try:
        _reset_harness_refresh_done_capture(page)
        page.evaluate("""async () => {
            window.__harnessReloadDone = false;
            const task = (async () => {
              await window.__claudeHubHmrHarness.reloadIframe();
              window.__harnessReloadDone = true;
            })();
            window.__harnessReloadTask = task;
          }""")

        _wait_harness_content_pending(page)
        page.wait_for_function(
            "() => window.__harnessReloadDone === true",
            timeout=int(scale_timeout(20) * 1000),
        )

        st = _harness_state(page)
        assert st["iframeNavGeneration"] > prior_nav
        assert st["iframeNavSettled"] is True
        nav_marker = f"_hmrNav={st['iframeNavGeneration']}"
        assert st["activeIframeSrc"] and nav_marker in st["activeIframeSrc"]

        iframe = page.locator(".hmr-harness-terminal iframe.active")
        doc_href: str = iframe.evaluate("(el) => el.contentWindow?.location.href ?? ''")
        assert nav_marker in doc_href, f"iframe document URL must carry nav marker: {doc_href}"
        assert prior_iframe_src is None or st["activeIframeSrc"] != prior_iframe_src

        _wait_harness_bootstrap_boundary(page)
    finally:
        _teardown_harness_content_pending_observer(page)


@pytest.fixture
def isolated_hmr_stack(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    backend = _start_isolated_backend(tmp_path)
    vite = _start_harness_vite(backend["base_url"], tmp_path)
    ctx: dict[str, Any] = {**backend, **vite}
    try:
        yield ctx
    finally:
        _stop_harness_vite(ctx)
        _stop_backend(ctx)
        _kill_isolated_tmux(ctx["bindir"])


def test_buffered_capture_coordinator_preserves_write_identity_and_finalizes(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Coordinator restores exact term.write after success, empty, fetch error, and sync apply throw."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]
    timeout_ms = int(scale_timeout(20) * 1000)

    tab_resp = http.post(
        f"{base}/api/tabs", json={"name": "coord-identity", "agent_type": "terminal"}
    )
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"
    history_pattern = f"**/api/terminal/history/{tab_id}?**"

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail("isolated tmux session not created")

    _tmux_send(session_name, bindir, "echo COORD_IDENTITY_SEED", "Enter")
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if "COORD_IDENTITY_SEED" in _tmux_capture(session_name, bindir):
            break
    else:
        pytest.fail("seed marker missing in tmux")

    page.wait_for_function(
        "() => window.term && window.term.__claudeHubReplayDone === true",
        timeout=timeout_ms,
    )
    page.wait_for_function(
        "() => window.term && typeof window.term.__claudeHubRefreshHistory === 'function'",
        timeout=timeout_ms,
    )

    refresh_timeout_ms = timeout_ms

    success = page.evaluate(
        """(timeoutMs) => {
            const term = window.term;
            if (!term || typeof term.write !== 'function') {
                throw new Error('term.write unavailable');
            }
            const writeBeforeRefresh = term.write;
            return new Promise(function(resolve, reject) {
                const timeout = setTimeout(function() {
                    reject(new Error('tab-switch refresh-done timeout'));
                }, timeoutMs);
                function onMessage(event) {
                    if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                    if (event.data.reason !== 'tab-switch') return;
                    window.removeEventListener('message', onMessage);
                    clearTimeout(timeout);
                    resolve({
                        ok: event.data.ok,
                        sameWriteRef: term.write === writeBeforeRefresh,
                    });
                }
                window.addEventListener('message', onMessage);
                window.postMessage({
                    type: 'terminal-history-refresh',
                    reason: 'tab-switch',
                    scrollToBottom: true,
                    preserveUserScroll: true,
                    requestId: 'coord-identity-success',
                }, '*');
            });
        }""",
        arg=refresh_timeout_ms,
    )
    assert success["ok"] is True
    assert success["sameWriteRef"] is True

    empty_calls = {"n": 0}

    def empty_history(route) -> None:
        empty_calls["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"history":"","cursor_x":0,"cursor_y":0}',
        )

    page.route(history_pattern, empty_history)
    try:
        empty_result = page.evaluate(
            """(timeoutMs) => {
                const term = window.term;
                const writeBeforeRefresh = term.write;
                return new Promise(function(resolve, reject) {
                    const timeout = setTimeout(function() {
                        reject(new Error('empty snapshot refresh-done timeout'));
                    }, timeoutMs);
                    function onMessage(event) {
                        if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                        if (event.data.requestId !== 'coord-identity-empty') return;
                        window.removeEventListener('message', onMessage);
                        clearTimeout(timeout);
                        resolve({
                            ok: event.data.ok,
                            sameWriteRef: term.write === writeBeforeRefresh,
                        });
                    }
                    window.addEventListener('message', onMessage);
                    window.postMessage({
                        type: 'terminal-history-refresh',
                        reason: 'manual',
                        scrollToBottom: true,
                        requestId: 'coord-identity-empty',
                    }, '*');
                });
            }""",
            arg=refresh_timeout_ms,
        )
    finally:
        page.unroute(history_pattern, empty_history)

    assert empty_calls["n"] >= 1
    assert empty_result["ok"] is True
    assert empty_result["sameWriteRef"] is True

    def abort_history(route) -> None:
        route.abort()

    page.route(history_pattern, abort_history)
    try:
        fetch_error = page.evaluate(
            """(timeoutMs) => {
                const term = window.term;
                const writeBeforeRefresh = term.write;
                return new Promise(function(resolve, reject) {
                    const timeout = setTimeout(function() {
                        reject(new Error('manual refresh-done timeout'));
                    }, timeoutMs);
                    function onMessage(event) {
                        if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                        if (event.data.requestId !== 'coord-identity-fetch-error') return;
                        window.removeEventListener('message', onMessage);
                        clearTimeout(timeout);
                        resolve({
                            ok: event.data.ok,
                            sameWriteRef: term.write === writeBeforeRefresh,
                        });
                    }
                    window.addEventListener('message', onMessage);
                    window.postMessage({
                        type: 'terminal-history-refresh',
                        reason: 'manual',
                        scrollToBottom: true,
                        requestId: 'coord-identity-fetch-error',
                    }, '*');
                });
            }""",
            arg=refresh_timeout_ms,
        )
    finally:
        page.unroute(history_pattern, abort_history)

    assert fetch_error["ok"] is False
    assert fetch_error["sameWriteRef"] is True

    sync_throw = page.evaluate(
        """(timeoutMs) => {
            const term = window.term;
            const writeBeforeRefresh = term.write;
            const innerWrite = term.__claudeHubInnerWrite || writeBeforeRefresh;
            let threw = false;
            term.__claudeHubInnerWrite = function(data, cb) {
                threw = true;
                throw new Error('deterministic sync apply throw');
            };
            return new Promise(function(resolve, reject) {
                const timeout = setTimeout(function() {
                    term.__claudeHubInnerWrite = innerWrite;
                    reject(new Error('sync-throw refresh-done timeout'));
                }, timeoutMs);
                function onMessage(event) {
                    if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                    if (event.data.requestId !== 'coord-identity-sync-throw') return;
                    window.removeEventListener('message', onMessage);
                    clearTimeout(timeout);
                    term.__claudeHubInnerWrite = innerWrite;
                    resolve({
                        ok: event.data.ok,
                        threw: threw,
                        sameWriteRef: term.write === writeBeforeRefresh,
                    });
                }
                window.addEventListener('message', onMessage);
                window.postMessage({
                    type: 'terminal-history-refresh',
                    reason: 'manual',
                    scrollToBottom: true,
                    requestId: 'coord-identity-sync-throw',
                }, '*');
            });
        }""",
        arg=refresh_timeout_ms,
    )
    assert sync_throw["threw"] is True
    assert sync_throw["ok"] is False
    assert sync_throw["sameWriteRef"] is True

    def _coord_edge_hold_route(gate: dict[str, Any], route) -> None:
        gate["fetch_count"] += 1
        if gate["fetch_count"] == 1:
            try:
                gate["held_route"] = route
                gate["held_response"] = route.fetch()
            except Exception as exc:
                gate["errors"].append(f"route fetch: {exc}")
                route.abort()
            return
        try:
            route.fulfill(response=route.fetch())
        except Exception as exc:
            gate["errors"].append(f"route retry fulfill: {exc}")
            route.abort()

    def _pump_until_held_fetch(gate: dict[str, Any], page: Page, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if gate.get("held_route") is not None and gate.get("held_response") is not None:
                return
            page.wait_for_timeout(50)
        pytest.fail("held history fetch was not captured by route handler")

    def _fulfill_held_fetch(gate: dict[str, Any]) -> None:
        held_route = gate.get("held_route")
        held_response = gate.get("held_response")
        if held_route is None or held_response is None:
            gate["errors"].append("held fetch missing at fulfill time")
            return
        try:
            held_route.fulfill(response=held_response)
        except Exception as exc:
            gate["errors"].append(f"held route fulfill: {exc}")

    def _coord_edge_live_result(page: Page, request_id: str) -> dict[str, Any]:
        return page.evaluate(
            """(requestId) => {
                const edge = window.__coordEdge || {};
                const obs = (window.__coordEdgeObs && window.__coordEdgeObs[requestId]) || { events: [] };
                return {
                    events: obs.events,
                    cbCounts: edge.cbCounts || {},
                    drainOrder: edge.drainOrder || [],
                    wrapperCalls: edge.wrapperCalls || 0,
                    sameWriteRef: edge.writeBefore != null && window.term
                        ? window.term.write === edge.writeBefore
                        : false,
                    writeStillHeldWrapper: edge.heldWrapper != null && window.term
                        ? window.term.write === edge.heldWrapper
                        : false,
                };
            }""",
            arg=request_id,
        )

    def _coord_restore_inner(page: Page) -> None:
        page.evaluate(
            "() => {"
            " if (window.__coordEdge && window.__coordEdge.restoreInner) {"
            " window.term.__claudeHubInnerWrite = window.__coordEdge.restoreInner; } }"
        )

    def _coord_install_forwarding_inner(page: Page, mode: str | None = None) -> None:
        page.evaluate(
            """(mode) => {
                const term = window.term;
                const realInner = term.__claudeHubInnerWrite || term.write.bind(term);
                window.__coordEdge = {
                    writeBefore: term.write,
                    mode: mode || null,
                    snapshotApplyStarted: false,
                    snapshotApplyReleased: false,
                    finalizeDrainStarted: false,
                    oldDrainReleased: false,
                    cbCounts: {},
                    drainOrder: [],
                    wrapperCalls: 0,
                };
                window.__coordEdge.restoreInner = realInner;
                term.__claudeHubInnerWrite = function(data, cb) {
                    const edge = window.__coordEdge || {};
                    const payload = String(data || '');
                    const isSnapshotApply = payload.indexOf('\\x1b[H\\x1b[2J') === 0;
                    if (
                        isSnapshotApply &&
                        edge.mode === 'sync-cb-throw'
                    ) {
                        if (edge.heldWrapper && term.write === edge.heldWrapper) {
                            term.write('QUEUE_ONE\\r\\n', function() {
                                edge.cbCounts.Q1 = (edge.cbCounts.Q1 || 0) + 1;
                                edge.drainOrder.push('cb:Q1');
                            });
                            term.write('QUEUE_TWO\\r\\n', function() {
                                edge.cbCounts.Q2 = (edge.cbCounts.Q2 || 0) + 1;
                                edge.drainOrder.push('cb:Q2');
                            });
                        }
                        if (typeof cb === 'function') cb();
                        throw new Error('sync cb then throw');
                    }
                    if (
                        isSnapshotApply &&
                        !edge.snapshotApplyReleased &&
                        edge.mode === 'finalize-drain'
                    ) {
                        if (!edge.snapshotApplyStarted) {
                            edge.snapshotApplyStarted = true;
                            edge.releaseSnapshotApply = function() {
                                edge.snapshotApplyReleased = true;
                                edge.restoreInner.call(term, data, cb);
                            };
                            return;
                        }
                    }
                    if (
                        edge.mode === 'finalize-drain' &&
                        edge.snapshotApplyReleased &&
                        !edge.oldDrainReleased &&
                        payload.indexOf('OLD_FRAME') >= 0
                    ) {
                        if (!edge.finalizeDrainStarted) {
                            edge.finalizeDrainStarted = true;
                            edge.releaseOld = function() {
                                edge.oldDrainReleased = true;
                                edge.restoreInner.call(term, data, cb);
                            };
                            return;
                        }
                    }
                    return edge.restoreInner.call(term, data, cb);
                };
            }""",
            arg=mode,
        )

    def _run_coord_held_edge_subcase(
        page: Page,
        request_id: str,
        inject_js: str,
        expect_ok: bool,
        inner_mode: str | None = None,
    ) -> dict[str, Any]:
        gate: dict[str, Any] = {
            "fetch_count": 0,
            "held_route": None,
            "held_response": None,
            "errors": [],
        }
        handler = lambda route: _coord_edge_hold_route(gate, route)
        page.route(history_pattern, handler)
        result: dict[str, Any] = {}
        try:
            page.evaluate(
                """(requestId) => {
                    window.__coordEdgeObs = window.__coordEdgeObs || {};
                    window.__coordEdgeObs[requestId] = { events: [], settled: false, done: false };
                    if (!window.__coordEdgeListenerInstalled) {
                        window.addEventListener('message', function(event) {
                            if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                            const rid = event.data.requestId;
                            const obs = window.__coordEdgeObs && window.__coordEdgeObs[rid];
                            if (!obs) return;
                            obs.events.push(event.data);
                            if (obs.settled) return;
                            obs.settled = true;
                            const markDone = function() { obs.done = true; };
                            if (typeof requestAnimationFrame === 'function') {
                                requestAnimationFrame(function() {
                                    requestAnimationFrame(markDone);
                                });
                            } else {
                                markDone();
                            }
                        });
                        window.__coordEdgeListenerInstalled = true;
                    }
                }""",
                arg=request_id,
            )
            _coord_install_forwarding_inner(page, inner_mode)
            page.evaluate(
                """(requestId) => {
                    window.postMessage({
                        type: 'terminal-history-refresh',
                        reason: 'manual',
                        scrollToBottom: true,
                        requestId: requestId,
                    }, '*');
                }""",
                arg=request_id,
            )
            _pump_until_held_fetch(gate, page, scale_timeout(20))
            page.evaluate(inject_js)
            _fulfill_held_fetch(gate)
            page.wait_for_function(
                "(requestId) => window.__coordEdgeObs && window.__coordEdgeObs[requestId]"
                " && window.__coordEdgeObs[requestId].done === true",
                arg=request_id,
                timeout=refresh_timeout_ms,
            )
            result = _coord_edge_live_result(page, request_id)
        finally:
            _coord_restore_inner(page)
            page.unroute(history_pattern, handler)

        assert gate["errors"] == [], f"held-route errors: {gate['errors']}"
        assert gate["fetch_count"] >= 1
        events = [e for e in result["events"] if e.get("requestId") == request_id]
        assert (
            len(events) == 1
        ), f"expected exactly one result for {request_id}: {result['events']!r}"
        assert events[0].get("ok") is expect_ok
        return result

    held_inject_common = """() => {
        const term = window.term;
        const edge = window.__coordEdge;
        edge.cbCounts = { OLD: 0, Q1: 0, Q2: 0 };
        edge.drainOrder = [];
        edge.wrapperCalls = 0;
        const captureWrite = term.write;
        edge.captureWriteRef = captureWrite;
        function heldWrapper(data, cb) {
            edge.wrapperCalls += 1;
            edge.drainOrder.push('wrap:' + String(data).slice(0, 20));
            return captureWrite.call(term, data, cb);
        }
        edge.heldWrapper = heldWrapper;
        term.write = heldWrapper;
        term.write('OLD_FRAME\\r\\n', function() {
            edge.cbCounts.OLD += 1;
            edge.drainOrder.push('cb:OLD');
        });
        term.write('QUEUE_ONE\\r\\n', function() {
            edge.cbCounts.Q1 += 1;
            edge.drainOrder.push('cb:Q1');
        });
        term.write('QUEUE_TWO\\r\\n', function() {
            edge.cbCounts.Q2 += 1;
            edge.drainOrder.push('cb:Q2');
        });
    }"""

    later_wrapper = _run_coord_held_edge_subcase(
        page,
        "coord-held-later-wrapper",
        held_inject_common,
        expect_ok=True,
    )
    assert later_wrapper["writeStillHeldWrapper"] is True
    assert later_wrapper["sameWriteRef"] is False
    assert later_wrapper["wrapperCalls"] >= 3
    assert later_wrapper["cbCounts"]["OLD"] == 1
    assert later_wrapper["cbCounts"]["Q1"] == 1
    assert later_wrapper["cbCounts"]["Q2"] == 1
    order = later_wrapper["drainOrder"]
    assert (
        order.index("cb:OLD") < order.index("cb:Q1") < order.index("cb:Q2")
    ), f"held capture must drain FIFO; order={order!r}"

    finalize_gate: dict[str, Any] = {
        "fetch_count": 0,
        "held_route": None,
        "held_response": None,
        "errors": [],
    }
    finalize_handler = lambda route: _coord_edge_hold_route(finalize_gate, route)
    finalize_request_id = "coord-held-finalize-drain"
    finalize_order: dict[str, Any] = {}
    page.route(history_pattern, finalize_handler)
    try:
        page.evaluate(
            """(requestId) => {
                window.__coordEdgeObs = window.__coordEdgeObs || {};
                window.__coordEdgeObs[requestId] = { events: [], settled: false, done: false };
            }""",
            arg=finalize_request_id,
        )
        _coord_install_forwarding_inner(page, "finalize-drain")
        page.evaluate(
            """(requestId) => {
                window.postMessage({
                    type: 'terminal-history-refresh',
                    reason: 'manual',
                    scrollToBottom: true,
                    requestId: requestId,
                }, '*');
            }""",
            arg=finalize_request_id,
        )
        _pump_until_held_fetch(finalize_gate, page, scale_timeout(20))
        _fulfill_held_fetch(finalize_gate)
        page.wait_for_function(
            "() => window.__coordEdge && window.__coordEdge.snapshotApplyStarted === true",
            timeout=refresh_timeout_ms,
        )
        page.evaluate("""() => {
            const term = window.term;
            const edge = window.__coordEdge;
            edge.cbCounts = { OLD: 0, NEW: 0 };
            edge.drainOrder = [];
            edge.wrapperCalls = 0;
            const captureWrite = term.write;
            function heldWrapper(data, cb) {
                edge.wrapperCalls += 1;
                edge.drainOrder.push('wrap:' + String(data).slice(0, 20));
                return captureWrite.call(term, data, cb);
            }
            edge.heldWrapper = heldWrapper;
            term.write = heldWrapper;
            term.write('OLD_FRAME\\r\\n', function() {
                edge.cbCounts.OLD += 1;
                edge.drainOrder.push('cb:OLD');
            });
        }""")
        page.evaluate(
            "() => { if (window.__coordEdge && window.__coordEdge.releaseSnapshotApply) {"
            " window.__coordEdge.releaseSnapshotApply(); } }"
        )
        page.wait_for_function(
            "() => window.__coordEdge && window.__coordEdge.finalizeDrainStarted === true",
            timeout=refresh_timeout_ms,
        )
        page.evaluate("""() => {
            const term = window.term;
            const edge = window.__coordEdge;
            term.write('NEW_DURING_FINALIZE\\r\\n', function() {
                edge.cbCounts.NEW += 1;
                edge.drainOrder.push('cb:NEW');
            });
        }""")
        page.evaluate(
            "() => { if (window.__coordEdge && window.__coordEdge.releaseOld) {"
            " window.__coordEdge.releaseOld(); } }"
        )
        page.wait_for_function(
            "(requestId) => window.__coordEdgeObs && window.__coordEdgeObs[requestId]"
            " && window.__coordEdgeObs[requestId].done === true",
            arg=finalize_request_id,
            timeout=refresh_timeout_ms,
        )
        finalize_order = _coord_edge_live_result(page, finalize_request_id)
    finally:
        _coord_restore_inner(page)
        page.unroute(history_pattern, finalize_handler)

    assert finalize_gate["errors"] == [], f"finalizing-route errors: {finalize_gate['errors']}"
    assert finalize_gate["fetch_count"] >= 1
    finalize_events = [
        e for e in finalize_order["events"] if e.get("requestId") == finalize_request_id
    ]
    assert (
        len(finalize_events) == 1
    ), f"expected exactly one finalizing result: {finalize_order['events']!r}"
    assert finalize_events[0].get("ok") is True
    assert finalize_order["writeStillHeldWrapper"] is True
    assert finalize_order["sameWriteRef"] is False
    assert finalize_order["wrapperCalls"] >= 1
    assert finalize_order["cbCounts"]["OLD"] == 1
    assert finalize_order["cbCounts"]["NEW"] == 1
    fin_order = finalize_order["drainOrder"]
    assert fin_order.index("cb:OLD") < fin_order.index(
        "cb:NEW"
    ), f"finalizing drain must apply OLD before NEW; order={fin_order!r}"

    sync_cb_throw_gate: dict[str, Any] = {
        "fetch_count": 0,
        "held_route": None,
        "held_response": None,
        "errors": [],
    }
    sync_cb_throw_handler = lambda route: _coord_edge_hold_route(sync_cb_throw_gate, route)
    sync_cb_throw_request_id = "coord-held-sync-cb-throw"
    sync_cb_throw: dict[str, Any] = {}
    page.route(history_pattern, sync_cb_throw_handler)
    try:
        page.evaluate(
            """(requestId) => {
                window.__coordEdgeObs = window.__coordEdgeObs || {};
                window.__coordEdgeObs[requestId] = { events: [], settled: false, done: false };
            }""",
            arg=sync_cb_throw_request_id,
        )
        _coord_install_forwarding_inner(page, "sync-cb-throw")
        page.evaluate(
            """(requestId) => {
                window.postMessage({
                    type: 'terminal-history-refresh',
                    reason: 'manual',
                    scrollToBottom: true,
                    requestId: requestId,
                }, '*');
            }""",
            arg=sync_cb_throw_request_id,
        )
        _pump_until_held_fetch(sync_cb_throw_gate, page, scale_timeout(20))
        page.evaluate("""() => {
            const term = window.term;
            const edge = window.__coordEdge;
            edge.cbCounts = { Q1: 0, Q2: 0 };
            edge.drainOrder = [];
            edge.wrapperCalls = 0;
            const captureWrite = term.write;
            function heldWrapper(data, cb) {
                edge.wrapperCalls += 1;
                return captureWrite.call(term, data, cb);
            }
            edge.heldWrapper = heldWrapper;
            term.write = heldWrapper;
        }""")
        _fulfill_held_fetch(sync_cb_throw_gate)
        page.wait_for_function(
            "(requestId) => window.__coordEdgeObs && window.__coordEdgeObs[requestId]"
            " && window.__coordEdgeObs[requestId].done === true",
            arg=sync_cb_throw_request_id,
            timeout=refresh_timeout_ms,
        )
        sync_cb_throw = _coord_edge_live_result(page, sync_cb_throw_request_id)
    finally:
        _coord_restore_inner(page)
        page.unroute(history_pattern, sync_cb_throw_handler)

    assert (
        sync_cb_throw_gate["errors"] == []
    ), f"sync-cb-throw route errors: {sync_cb_throw_gate['errors']}"
    sync_cb_events = [
        e for e in sync_cb_throw["events"] if e.get("requestId") == sync_cb_throw_request_id
    ]
    assert len(sync_cb_events) == 1
    assert sync_cb_events[0].get("ok") is False
    assert sync_cb_throw["writeStillHeldWrapper"] is True
    assert sync_cb_throw["sameWriteRef"] is False
    assert sync_cb_throw["cbCounts"]["Q1"] == 1
    assert sync_cb_throw["cbCounts"]["Q2"] == 1

    pre_apply_marker = "PRE_APPLY_FB_7731"
    pre_apply_inject_js = """(marker) => {
        const term = window.term;
        window.__coordPreApply = window.__coordPreApply || {};
        const obs = window.__coordPreApply;
        obs.marker = marker;
        obs.cbCounts = { MARKER: 0 };
        const captureWrite = term.write;
        function heldWrapper(data, cb) {
            return captureWrite.call(term, data, cb);
        }
        obs.heldWrapper = heldWrapper;
        term.write = heldWrapper;
        term.write(marker + '\\r\\n', function() {
            obs.cbCounts.MARKER += 1;
        });
    }"""

    def _pre_apply_read(page: Page, marker: str) -> dict[str, Any]:
        return page.evaluate(
            """(marker) => {
                const term = window.term;
                const obs = window.__coordPreApply || {};
                const buf = term.buffer.active;
                let markerLines = 0;
                for (let i = 0; i < buf.length; i++) {
                    const ln = buf.getLine(i);
                    if (ln && ln.translateToString(true).trim() === marker) markerLines++;
                }
                return {
                    markerLines: markerLines,
                    cbMarker: (obs.cbCounts && obs.cbCounts.MARKER) || 0,
                    writeStillHeldWrapper: obs.heldWrapper != null && term.write === obs.heldWrapper,
                };
            }""",
            arg=marker,
        )

    def _wait_fetch_count(gate: dict[str, Any], page: Page, minimum: int, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if gate.get("fetch_count", 0) >= minimum:
                return
            page.wait_for_timeout(50)
        pytest.fail(f"history fetch count did not reach {minimum}; got {gate.get('fetch_count')}")

    pre_apply_fail_gate: dict[str, Any] = {
        "fetch_count": 0,
        "held_route": None,
        "held_response": None,
        "errors": [],
    }
    pre_apply_fail_request_id = "coord-pre-apply-fallback-fail"

    def pre_apply_fail_route(route) -> None:
        pre_apply_fail_gate["fetch_count"] += 1
        if pre_apply_fail_gate["fetch_count"] == 1:
            try:
                pre_apply_fail_gate["held_route"] = route
                pre_apply_fail_gate["held_response"] = route.fetch()
            except Exception as exc:
                pre_apply_fail_gate["errors"].append(f"route fetch: {exc}")
                route.abort()
            return
        route.abort()

    pre_apply_fail: dict[str, Any] = {}
    page.route(history_pattern, pre_apply_fail_route)
    try:
        page.evaluate(
            """(requestId) => {
                window.__coordEdgeObs = window.__coordEdgeObs || {};
                window.__coordEdgeObs[requestId] = { events: [], settled: false, done: false };
            }""",
            arg=pre_apply_fail_request_id,
        )
        page.evaluate(
            """(requestId) => {
                window.postMessage({
                    type: 'terminal-history-refresh',
                    reason: 'manual',
                    scrollToBottom: true,
                    requestId: requestId,
                }, '*');
            }""",
            arg=pre_apply_fail_request_id,
        )
        _pump_until_held_fetch(pre_apply_fail_gate, page, scale_timeout(20))
        page.evaluate(pre_apply_inject_js, arg=pre_apply_marker)
        _fulfill_held_fetch(pre_apply_fail_gate)
        _wait_fetch_count(pre_apply_fail_gate, page, 2, scale_timeout(20))
        page.wait_for_function(
            "(requestId) => window.__coordEdgeObs && window.__coordEdgeObs[requestId]"
            " && window.__coordEdgeObs[requestId].done === true",
            arg=pre_apply_fail_request_id,
            timeout=refresh_timeout_ms,
        )
        pre_apply_fail = page.evaluate(
            """(requestId) => {
                const obs = (window.__coordEdgeObs && window.__coordEdgeObs[requestId]) || { events: [] };
                return { events: obs.events };
            }""",
            arg=pre_apply_fail_request_id,
        )
        pre_apply_fail_read = _pre_apply_read(page, pre_apply_marker)
    finally:
        page.unroute(history_pattern, pre_apply_fail_route)

    pre_apply_fail_events = [
        e for e in pre_apply_fail["events"] if e.get("requestId") == pre_apply_fail_request_id
    ]
    assert pre_apply_fail_gate["errors"] == [], pre_apply_fail_gate["errors"]
    assert len(pre_apply_fail_events) == 1
    assert pre_apply_fail_events[0].get("ok") is False
    assert pre_apply_fail_read["cbMarker"] == 1
    assert pre_apply_fail_read["markerLines"] == 1
    assert pre_apply_fail_read["writeStillHeldWrapper"] is True

    pre_apply_ok_gate: dict[str, Any] = {
        "fetch_count": 0,
        "held_route": None,
        "held_response": None,
        "errors": [],
    }
    pre_apply_ok_request_id = "coord-pre-apply-fallback-ok"

    def pre_apply_ok_route(route) -> None:
        pre_apply_ok_gate["fetch_count"] += 1
        if pre_apply_ok_gate["fetch_count"] == 1:
            try:
                pre_apply_ok_gate["held_route"] = route
                pre_apply_ok_gate["held_response"] = route.fetch()
            except Exception as exc:
                pre_apply_ok_gate["errors"].append(f"route fetch: {exc}")
                route.abort()
            return
        if pre_apply_ok_gate["fetch_count"] == 2:
            _tmux_send(session_name, bindir, f"echo {pre_apply_marker}", "Enter")
            for _ in range(int(scale_timeout(30))):
                if pre_apply_marker in _tmux_capture(session_name, bindir):
                    break
                time.sleep(0.1)
            else:
                pre_apply_ok_gate["errors"].append("marker missing in tmux before retry fetch")
                route.abort()
                return
            try:
                route.fulfill(response=route.fetch())
            except Exception as exc:
                pre_apply_ok_gate["errors"].append(f"retry fulfill: {exc}")
                route.abort()
            return
        try:
            route.fulfill(response=route.fetch())
        except Exception as exc:
            pre_apply_ok_gate["errors"].append(f"extra fulfill: {exc}")
            route.abort()

    pre_apply_ok: dict[str, Any] = {}
    page.route(history_pattern, pre_apply_ok_route)
    try:
        page.evaluate(
            """(requestId) => {
                window.__coordEdgeObs = window.__coordEdgeObs || {};
                window.__coordEdgeObs[requestId] = { events: [], settled: false, done: false };
            }""",
            arg=pre_apply_ok_request_id,
        )
        page.evaluate(
            """(requestId) => {
                window.postMessage({
                    type: 'terminal-history-refresh',
                    reason: 'manual',
                    scrollToBottom: true,
                    requestId: requestId,
                }, '*');
            }""",
            arg=pre_apply_ok_request_id,
        )
        _pump_until_held_fetch(pre_apply_ok_gate, page, scale_timeout(20))
        page.evaluate(pre_apply_inject_js, arg=pre_apply_marker)
        _fulfill_held_fetch(pre_apply_ok_gate)
        _wait_fetch_count(pre_apply_ok_gate, page, 2, scale_timeout(20))
        page.wait_for_function(
            "(requestId) => window.__coordEdgeObs && window.__coordEdgeObs[requestId]"
            " && window.__coordEdgeObs[requestId].done === true",
            arg=pre_apply_ok_request_id,
            timeout=refresh_timeout_ms,
        )
        pre_apply_ok = page.evaluate(
            """(requestId) => {
                const obs = (window.__coordEdgeObs && window.__coordEdgeObs[requestId]) || { events: [] };
                return { events: obs.events };
            }""",
            arg=pre_apply_ok_request_id,
        )
        pre_apply_ok_read = _pre_apply_read(page, pre_apply_marker)
    finally:
        page.unroute(history_pattern, pre_apply_ok_route)

    pre_apply_ok_events = [
        e for e in pre_apply_ok["events"] if e.get("requestId") == pre_apply_ok_request_id
    ]
    assert pre_apply_ok_gate["errors"] == [], pre_apply_ok_gate["errors"]
    assert len(pre_apply_ok_events) == 1
    assert pre_apply_ok_events[0].get("ok") is True
    assert pre_apply_ok_read["cbMarker"] == 1
    assert pre_apply_ok_read["markerLines"] == 1

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def test_terminalview_harness_remount_and_backend_reload(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """TerminalView harness: content-ready + history across remount, iframe reload, backend restart."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    harness_url = ctx["harness_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]

    tab_resp = http.post(f"{base}/api/tabs", json={"name": "hmr-ui", "agent_type": "terminal"})
    assert tab_resp.status_code == 201
    tab = tab_resp.json()
    tab_id = tab["id"]
    session_name = f"claude-hub-{tab_id[:8]}"

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail(f"isolated tmux session {session_name} not created")

    _seed_markers(session_name, bindir, "HMR_UI", count=40)
    page.goto("about:blank")

    initial_inflight = "HMR_INITIAL_INFLIGHT_7711"
    history_pattern = f"**/api/terminal/history/{tab_id}?**"
    initial_gate: dict[str, Any] = {
        "fetch_count": 0,
        "first_has_inflight": None,
        "first_upstream": None,
        "errors": [],
        "upstream_ready": threading.Event(),
        "release": threading.Event(),
        "fulfilled": False,
    }

    def hold_initial_history_fetch(route) -> None:
        initial_gate["fetch_count"] += 1
        if initial_gate["fetch_count"] == 1:
            try:
                upstream = route.fetch()
                initial_gate["first_has_inflight"] = initial_inflight in upstream.text()
                initial_gate["first_upstream"] = upstream
            except Exception as exc:
                initial_gate["errors"].append(f"route fetch: {exc}")
                route.abort()
                return
            initial_gate["upstream_ready"].set()
            if not initial_gate["release"].wait(timeout=scale_timeout(25)):
                initial_gate["errors"].append("release timeout in initial route handler")
                if not initial_gate["fulfilled"]:
                    route.abort()
                return
            if initial_gate["fulfilled"]:
                return
            initial_gate["fulfilled"] = True
            try:
                route.fulfill(response=initial_gate["first_upstream"])
            except Exception as exc:
                if "already handled" not in str(exc):
                    initial_gate["errors"].append(f"route fulfill: {exc}")
            return
        try:
            route.fulfill(response=route.fetch())
        except Exception as exc:
            initial_gate["errors"].append(f"route retry fetch: {exc}")
            route.abort()

    page.route(history_pattern, hold_initial_history_fetch)
    inflight_injector = threading.Thread(
        target=_initial_inflight_injector_thread,
        args=(initial_gate, session_name, bindir, initial_inflight),
        daemon=True,
    )
    try:
        _ensure_harness_refresh_done_capture(page)
        _reset_harness_refresh_done_capture(page)
        inflight_injector.start()
        _open_harness(page, harness_url, tab_id, min_lines=10)
        deadline = time.time() + scale_timeout(15)
        while time.time() < deadline and initial_gate["fetch_count"] == 0:
            time.sleep(0.05)
        assert initial_gate["fetch_count"] >= 1, (
            "initial replay must start a history fetch when capture is active; "
            f"fetch_count={initial_gate['fetch_count']}"
        )
        inflight_injector.join(timeout=scale_timeout(25))
        assert not inflight_injector.is_alive(), "initial inflight injector did not finish"
        assert initial_gate["errors"] == [], f"initial inflight errors: {initial_gate['errors']}"
        assert (
            initial_gate["first_has_inflight"] is False
        ), "inflight marker must not be in first coordinator upstream snapshot"
        assert initial_gate["fetch_count"] >= 2, (
            "nonempty capture during coordinator fetch must quiet-retry; "
            f"fetch_count={initial_gate['fetch_count']}"
        )
        _assert_harness_matches_tmux_at_refresh_done(
            page,
            session_name,
            bindir,
            label="initial content-ready",
            inflight_marker=initial_inflight,
        )
    finally:
        initial_gate["release"].set()
        page.unroute(history_pattern, hold_initial_history_fetch)
        inflight_injector.join(timeout=1)

    initial = _read_harness_xterm_lines(page)
    _assert_markers_present(initial, "HMR_UI", 40)

    _trigger_harness_remount(page)
    _assert_harness_matches_tmux_at_refresh_done(
        page, session_name, bindir, label="remount content-ready"
    )
    remount_lines = _read_harness_xterm_lines(page)
    _assert_markers_present(remount_lines, "HMR_UI", 40)

    _trigger_harness_iframe_reload(page)
    _assert_harness_matches_tmux_at_refresh_done(
        page, session_name, bindir, label="iframe reload content-ready"
    )
    reload_lines = _read_harness_xterm_lines(page)
    _assert_markers_present(reload_lines, "HMR_UI", 40)

    session_before = session_name
    assert _tmux_has_session(session_before, bindir)

    tmp_path = ctx["tmp_home"].parent
    _stop_harness_vite(ctx)
    _stop_backend(ctx)
    assert _tmux_has_session(session_before, bindir), "private tmux must survive backend stop"
    _restart_hmr_stack(ctx, tmp_path)
    base = ctx["base_url"]
    harness_url = ctx["harness_url"]
    http = ctx["session"]
    bindir = ctx["bindir"]

    listed = http.get(f"{base}/api/tabs")
    assert listed.status_code == 200
    assert tab_id in {t["id"] for t in listed.json()}

    _open_harness(page, harness_url, tab_id, min_lines=10)
    _assert_harness_matches_tmux_at_refresh_done(
        page, session_name, bindir, label="backend restart content-ready"
    )
    post_backend = _read_harness_xterm_lines(page)
    _assert_markers_present(post_backend, "HMR_UI", 40)
    assert session_before == f"claude-hub-{tab_id[:8]}"
    assert _tmux_has_session(session_before, bindir)

    _tmux_send(session_name, bindir, "echo HMR_UI_LIVE_8888", "Enter")
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if "HMR_UI_LIVE_8888" in _tmux_capture(session_name, bindir):
            break
    else:
        pytest.fail("live marker missing after backend reload")

    _trigger_harness_iframe_reload(page)
    page.wait_for_function(
        """() => {
          const iframe = document.querySelector('.hmr-harness-terminal iframe.active');
          const term = iframe && iframe.contentWindow && iframe.contentWindow.term;
          const buf = term && term.buffer && term.buffer.active;
          if (!buf) return false;
          for (let i = 0; i < buf.length; i++) {
            const line = buf.getLine(i);
            if (line && line.translateToString(true).includes('HMR_UI_LIVE_8888')) return true;
          }
          return false;
        }""",
        timeout=int(scale_timeout(20) * 1000),
    )

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass

    _stop_harness_vite(ctx)
    _stop_backend(ctx)
    _kill_isolated_tmux(bindir)

    assert ctx.get("proc") is None, "uvicorn must be stopped"
    assert ctx.get("vite_proc") is None, "vite harness must be stopped"
    assert not _isolated_tmux_server_alive(bindir), "private tmux server must be killed"


def test_harness_working_claude_remount_waits_bootstrap_not_early_reveal(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Working local Claude must wait iframe bootstrap done (no parent early contentReady)."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    harness_url = ctx["harness_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]

    tab_resp = http.post(f"{base}/api/tabs", json={"name": "hmr-claude", "agent_type": "terminal"})
    assert tab_resp.status_code == 201
    tab = tab_resp.json()
    tab_id = tab["id"]
    _bootstrap_terminal_tab_private_tmux(page, base, tab_id, bindir)
    _promote_tab_to_local_claude(http, base, tab_id)

    page.route(
        "**/api/tabs/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=f'[{{"tab_id":"{tab_id}","status":"working"}}]',
        ),
    )

    _open_harness(page, harness_url, tab_id, agent_type="claude")
    _wait_harness_content_ready(page)

    _install_harness_content_pending_observer(page)
    try:
        page.evaluate("async () => { await window.__claudeHubHmrHarness.remountTerminalView(); }")
        page.wait_for_function(
            """() => {
              const st = window.__claudeHubHmrHarness?.readHarnessState();
              return !!st && st.remountSettled;
            }""",
            timeout=int(scale_timeout(20) * 1000),
        )
        _wait_harness_content_ready(page)
        pending_seen: bool = page.evaluate("() => window.__harnessContentPendingSeen === true")
        assert pending_seen, "working local agent remount must wait bootstrap (content-pending)"
    finally:
        _teardown_harness_content_pending_observer(page)

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass
    page.unroute("**/api/tabs/status")


def test_isolated_overlapping_refresh_fifo(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Overlapping forced refreshes FIFO-complete with distinct requestIds (isolated backend)."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]

    tab_resp = http.post(f"{base}/api/tabs", json={"name": "fifo", "agent_type": "terminal"})
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail("tmux session not created")

    _seed_markers(session_name, bindir, "FIFO", count=40)
    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    page.wait_for_function(
        "() => window.term && window.term.__claudeHubReplayDone === true",
        timeout=int(scale_timeout(20) * 1000),
    )

    first_request_id = "iso-overlap-first"
    second_request_id = "iso-overlap-second"
    page.evaluate(
        """(ids) => {
            window.__claudeHubRefreshEvents = [];
            window.addEventListener('message', function(event) {
                if (event.data && event.data.type === 'terminal-history-refresh-done') {
                    window.__claudeHubRefreshEvents.push(event.data);
                }
            });
            window.postMessage({
                type: 'terminal-history-refresh',
                reason: 'test-overlap-first',
                scrollToBottom: true,
                requestId: ids.first,
            }, '*');
            window.postMessage({
                type: 'terminal-history-refresh',
                reason: 'test-overlap-second',
                scrollToBottom: true,
                requestId: ids.second,
            }, '*');
        }""",
        arg={"first": first_request_id, "second": second_request_id},
    )

    page.wait_for_function(
        """(ids) => {
            const events = window.__claudeHubRefreshEvents || [];
            return events.some(function(e) { return e.requestId === ids.first && e.ok === true; })
                && events.some(function(e) { return e.requestId === ids.second && e.ok === true; });
        }""",
        arg={"first": first_request_id, "second": second_request_id},
        timeout=15000,
    )

    events = page.evaluate("() => window.__claudeHubRefreshEvents || []")
    first_events = [e for e in events if e.get("requestId") == first_request_id]
    second_events = [e for e in events if e.get("requestId") == second_request_id]
    assert len(first_events) == 1
    assert len(second_events) == 1
    assert events.index(first_events[0]) < events.index(second_events[0])

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def test_harness_cached_tab_switch_no_snapshot_fetch(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Rapid cached tab switches must not fetch tmux history from child iframes."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    harness_url = ctx["harness_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]

    tab_a = http.post(f"{base}/api/tabs", json={"name": "sw-a", "agent_type": "terminal"}).json()
    tab_b = http.post(f"{base}/api/tabs", json={"name": "sw-b", "agent_type": "terminal"}).json()
    tab_a_id = tab_a["id"]
    tab_b_id = tab_b["id"]

    for tab_id in (tab_a_id, tab_b_id):
        page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
        page.wait_for_selector(".xterm", timeout=15000)
        session_name = f"claude-hub-{tab_id[:8]}"
        for _ in range(int(scale_timeout(30))):
            time.sleep(0.2)
            if _tmux_has_session(session_name, bindir):
                break
        else:
            pytest.fail(f"tmux session not created for {tab_id}")
        _seed_markers(session_name, bindir, f"SW_{tab_id[:4].upper()}", count=20)

    _open_harness(page, harness_url, tab_a_id, alt_tab_id=tab_b_id, min_lines=10)
    page.evaluate("async () => { await window.__claudeHubHmrHarness.switchCachedTab(); }")
    _wait_harness_content_ready(page)
    page.wait_for_timeout(int(scale_timeout(2) * 1000))

    _install_iframe_history_fetch_spy(page)
    for _ in range(4):
        page.evaluate("async () => { await window.__claudeHubHmrHarness.switchCachedTab(); }")
        page.wait_for_timeout(120)

    fetch_count: int = page.evaluate("() => window.__iframeHistoryFetches || 0")
    assert (
        fetch_count == 0
    ), f"cached tab switch must not fetch history from child iframes: {fetch_count}"

    try:
        http.delete(f"{base}/api/tabs/{tab_a_id}", timeout=5)
        http.delete(f"{base}/api/tabs/{tab_b_id}", timeout=5)
    except requests.RequestException:
        pass


@pytest.mark.parametrize("agent_status", ["working", None])
def test_remote_bootstrap_defers_recovery(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
    agent_status: str | None,
) -> None:
    """Remote bootstrap defers snapshot recovery for working/unknown agent status."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]
    request_id = "remote-working-req" if agent_status == "working" else "remote-unknown-req"

    tab_resp = http.post(
        f"{base}/api/tabs",
        json={"name": f"remote-defer-{agent_status or 'unknown'}", "agent_type": "terminal"},
    )
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    _bootstrap_terminal_tab_private_tmux(page, base, tab_id, bindir)
    _promote_tab_to_remote(http, base, tab_id)

    history_fetches = {"n": 0}

    def count_history(route) -> None:
        history_fetches["n"] += 1
        route.continue_()

    page.route("**/api/terminal/history/**", count_history)
    try:
        page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
        page.wait_for_selector(".xterm", timeout=15000)
        page.evaluate(
            """(args) => {
            window.__remoteBootEvents = [];
            window.addEventListener('message', function(event) {
                if (event.data && event.data.type === 'terminal-history-refresh-done') {
                    window.__remoteBootEvents.push(event.data);
                }
            });
            window.postMessage({
                type: 'terminal-bootstrap-correlation',
                requestId: args.requestId,
                agentStatus: args.agentStatus,
            }, '*');
        }""",
            arg={"requestId": request_id, "agentStatus": agent_status},
        )
        page.wait_for_function(
            """(requestId) => (window.__remoteBootEvents || []).some(function(e) {
                return e.requestId === requestId;
            })""",
            arg=request_id,
            timeout=int(scale_timeout(15) * 1000),
        )
        events = [
            e
            for e in page.evaluate("() => window.__remoteBootEvents || []")
            if e.get("requestId") == request_id
        ]
        assert len(events) == 1
        assert events[0].get("ok") is True
        assert events[0].get("deferredRecovery") is True
        assert history_fetches["n"] == 0
    finally:
        page.unroute("**/api/terminal/history/**", count_history)

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def test_plain_initial_replay_failure_retry_recovers(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Plain terminal initial coordinator failure must Retry via request-correlated recovery refresh."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]
    retry_marker = "PLAIN_RETRY_RECOVER_9911"

    tab_resp = http.post(f"{base}/api/tabs", json={"name": "plain-retry", "agent_type": "terminal"})
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"
    history_pattern = f"**/api/terminal/history/{tab_id}?**"
    fail_once = {"n": 0}

    def abort_first_fetch(route) -> None:
        fail_once["n"] += 1
        if fail_once["n"] == 1:
            route.abort()
            return
        route.fulfill(response=route.fetch())

    page.route(history_pattern, abort_first_fetch)
    try:
        page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
        page.wait_for_selector(".xterm", timeout=15000)
        for _ in range(int(scale_timeout(30))):
            time.sleep(0.2)
            if _tmux_has_session(session_name, bindir):
                break
        else:
            pytest.fail("isolated tmux session not created")

        page.wait_for_function(
            "() => window.term && window.term.__claudeHubInitialReplayFailed === true",
            timeout=int(scale_timeout(20) * 1000),
        )

        _tmux_send(session_name, bindir, f"echo {retry_marker}", "Enter")
        for _ in range(int(scale_timeout(30))):
            time.sleep(0.2)
            if retry_marker in _tmux_capture(session_name, bindir):
                break
        else:
            pytest.fail("retry marker missing in tmux")

        page.evaluate("""() => {
            window.__plainRetryEvents = [];
            window.addEventListener('message', function(event) {
                if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                window.__plainRetryEvents.push(event.data);
            });
        }""")
        page.evaluate("""() => {
            window.postMessage({
                type: 'terminal-bootstrap-correlation',
                requestId: 'plain-retry-req',
                agentStatus: 'idle',
            }, '*');
        }""")
        page.wait_for_function(
            """() => (window.__plainRetryEvents || []).some(function(e) {
                return e.reason === 'bootstrap' && e.requestId === 'plain-retry-req';
            })""",
            timeout=int(scale_timeout(20) * 1000),
        )
        events = page.evaluate("() => window.__plainRetryEvents || []")
        retry_events = [e for e in events if e.get("requestId") == "plain-retry-req"]
        assert len(retry_events) == 1
        assert retry_events[0].get("ok") is True

        page.wait_for_function(
            f"() => {{ const t = window.term; if (!t || !t.buffer) return false;"
            f" const b = t.buffer.active; if (!b) return false;"
            f" for (let i = 0; i < b.length; i++) {{"
            f" const ln = b.getLine(i); if (ln && ln.translateToString(true).indexOf('{retry_marker}') >= 0) return true;"
            f" }} return false; }}",
            timeout=int(scale_timeout(15) * 1000),
        )
    finally:
        page.unroute(history_pattern, abort_first_fetch)

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def test_local_agent_preload_failure_retry_recovers(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Local agent preload failure must Retry with a fresh preload fetch then recovery refresh."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]
    retry_marker = "LOCAL_PRELOAD_RETRY_8820"

    tab_resp = http.post(
        f"{base}/api/tabs", json={"name": "local-preload-retry", "agent_type": "terminal"}
    )
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"
    history_pattern = f"**/api/terminal/history/{tab_id}?**"
    fail_once = {"n": 0}

    def abort_first_preload(route) -> None:
        fail_once["n"] += 1
        if fail_once["n"] == 1:
            route.abort()
            return
        route.fulfill(response=route.fetch())

    page.route(history_pattern, abort_first_preload)
    try:
        _bootstrap_terminal_tab_private_tmux(page, base, tab_id, bindir)
        _promote_tab_to_local_claude(http, base, tab_id)
        page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
        page.wait_for_selector(".xterm", timeout=15000)
        deadline = time.time() + scale_timeout(20)
        while time.time() < deadline and fail_once["n"] < 1:
            time.sleep(0.1)
        assert fail_once["n"] >= 1, "preload fetch must fail once before Retry"
        page.wait_for_function(
            "() => window.term && typeof window.term.write === 'function'",
            timeout=int(scale_timeout(15) * 1000),
        )
        time.sleep(0.3)

        _seed_markers(session_name, bindir, "LPR", count=8)
        for _ in range(int(scale_timeout(40))):
            time.sleep(0.2)
            if retry_marker in _tmux_capture(session_name, bindir):
                break
            _tmux_send(session_name, bindir, f"echo {retry_marker}", "Enter")
        else:
            pytest.fail("retry marker missing in tmux")

        page.evaluate("""() => {
            window.__localPreloadRetryEvents = [];
            window.addEventListener('message', function(event) {
                if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                window.__localPreloadRetryEvents.push(event.data);
            });
        }""")
        page.evaluate("""() => {
            window.postMessage({
                type: 'terminal-bootstrap-correlation',
                requestId: 'local-preload-retry-req',
                agentStatus: 'idle',
            }, '*');
        }""")
        page.wait_for_function(
            """() => (window.__localPreloadRetryEvents || []).some(function(e) {
                return e.reason === 'bootstrap' && e.requestId === 'local-preload-retry-req';
            })""",
            timeout=int(scale_timeout(25) * 1000),
        )
        events = page.evaluate("() => window.__localPreloadRetryEvents || []")
        retry_events = [e for e in events if e.get("requestId") == "local-preload-retry-req"]
        assert len(retry_events) == 1
        assert retry_events[0].get("ok") is True
        assert fail_once["n"] >= 2, "Retry must re-fetch preload history"
    finally:
        page.unroute(history_pattern, abort_first_preload)

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def test_remote_stable_bootstrap_single_owner_and_status(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Remote stable bootstrap is single-owner; pending correlations reuse persisted agentStatus."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]

    tab_resp = http.post(
        f"{base}/api/tabs", json={"name": "remote-single-owner", "agent_type": "terminal"}
    )
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail("isolated tmux session not created")

    _tmux_send(session_name, bindir, "echo RMT_OWNER_SEED", "Enter")
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if "RMT_OWNER_SEED" in _tmux_capture(session_name, bindir):
            break
    else:
        pytest.fail("seed marker missing in tmux")

    http.put(
        f"{base}/api/tabs/{tab_id}",
        json={"agent_type": "claude", "target": "remote", "remote_profile_id": "test-loopback"},
    )

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    page.wait_for_function(
        "() => window.term && typeof window.term.write === 'function'",
        timeout=int(scale_timeout(15) * 1000),
    )

    history_pattern = f"**/api/terminal/history/{tab_id}?**"
    gate: dict[str, Any] = {
        "fetch_count": 0,
        "held_route": None,
        "held_response": None,
        "errors": [],
    }

    def stable_owner_route(route) -> None:
        _route_hold_first_fetch(gate, route)

    page.route(history_pattern, stable_owner_route)
    try:
        page.evaluate("""() => {
            window.__remoteOwnerEvents = [];
            window.addEventListener('message', function(event) {
                if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                window.__remoteOwnerEvents.push(event.data);
            });
        }""")
        page.evaluate("""() => {
            window.postMessage({
                type: 'terminal-bootstrap-correlation',
                requestId: 'remote-owner-a',
                agentStatus: 'idle',
            }, '*');
        }""")
        _pump_until_held_route(gate, page, scale_timeout(20))
        owner_state = page.evaluate("""() => {
            const inFlight = !!(window.term && window.term.__claudeHubRemoteStableBootstrapInFlight);
            window.postMessage({
                type: 'terminal-bootstrap-correlation',
                requestId: 'remote-owner-b',
            }, '*');
            return {
                inFlightBeforeB: inFlight,
                inFlightAfterB: !!(window.term && window.term.__claudeHubRemoteStableBootstrapInFlight),
            };
        }""")
        assert owner_state["inFlightBeforeB"] is True
        assert (
            owner_state["inFlightAfterB"] is True
        ), "second correlation must not start another stable bootstrap"
        _fulfill_held_route(gate)
        page.wait_for_function(
            """() => {
                const events = window.__remoteOwnerEvents || [];
                return events.some(function(e) { return e.requestId === 'remote-owner-a' && e.ok === true; })
                    && events.some(function(e) { return e.requestId === 'remote-owner-b' && e.ok === true; });
            }""",
            timeout=int(scale_timeout(25) * 1000),
        )
        events = page.evaluate("() => window.__remoteOwnerEvents || []")
        a_events = [e for e in events if e.get("requestId") == "remote-owner-a"]
        b_events = [e for e in events if e.get("requestId") == "remote-owner-b"]
        assert (
            gate["fetch_count"] == 1
        ), f"expected exactly one history fetch; got {gate['fetch_count']}"
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0].get("deferredRecovery") is not True
        assert b_events[0].get("deferredRecovery") is not True
        assert gate["errors"] == [], f"route errors: {gate['errors']}"
    finally:
        page.unroute(history_pattern, stable_owner_route)

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def _during_capture_injector_thread(
    gate: dict[str, Any],
    session_name: str,
    bindir: Path,
    during_marker: str,
) -> None:
    try:
        if not gate["upstream_ready"].wait(timeout=scale_timeout(30)):
            gate["errors"].append("upstream_ready timeout in injector thread")
            return
        _tmux_send(session_name, bindir, f"echo {during_marker}", "Enter")
        deadline = time.time() + scale_timeout(30)
        while time.time() < deadline:
            if during_marker in _tmux_capture(session_name, bindir):
                return
            time.sleep(0.2)
        gate["errors"].append("during-capture marker not written to tmux")
    except Exception as exc:
        gate["errors"].append(f"injector thread: {exc}")
    finally:
        gate["release"].set()


def test_remote_stable_bootstrap_during_capture_retries_without_duplicate(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Live frame during fetch must skip snapshot apply, drain, quiet-retry without loss/duplicate."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]
    during_marker = "REMOTE_DURING_CAPTURE_5511"

    tab_resp = http.post(
        f"{base}/api/tabs", json={"name": "remote-during-cap", "agent_type": "terminal"}
    )
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail("tmux session not created")

    _tmux_send(session_name, bindir, "echo RMT_SEED_MARKER", "Enter")
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if "RMT_SEED_MARKER" in _tmux_capture(session_name, bindir):
            break
    else:
        pytest.fail("seed marker missing in tmux")

    http.put(
        f"{base}/api/tabs/{tab_id}",
        json={"agent_type": "claude", "target": "remote", "remote_profile_id": "test-loopback"},
    )

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    page.wait_for_function(
        "() => window.term && typeof window.term.write === 'function'",
        timeout=int(scale_timeout(15) * 1000),
    )
    page.wait_for_function(
        """() => {
            const term = window.term;
            return !term || term.__claudeHubReplayBuffering !== true;
        }""",
        timeout=int(scale_timeout(15) * 1000),
    )
    page.wait_for_function(
        """async (tabId) => {
            const response = await fetch(`/api/terminal/history/${tabId}?lines=500`, {
                credentials: 'same-origin',
            });
            if (!response.ok) return false;
            const payload = await response.json();
            return (payload.history || '').indexOf('RMT_SEED_MARKER') >= 0;
        }""",
        arg=tab_id,
        timeout=int(scale_timeout(30) * 1000),
    )

    history_pattern = f"**/api/terminal/history/{tab_id}?**"
    gate: dict[str, Any] = {
        "fetch_count": 0,
        "fetch_started": False,
        "first_body_has_marker": None,
        "first_upstream": None,
        "errors": [],
        "upstream_ready": threading.Event(),
        "release": threading.Event(),
        "fulfilled": False,
    }

    def inject_during_first_fetch(route) -> None:
        gate["fetch_count"] += 1
        if gate["fetch_count"] == 1:
            gate["fetch_started"] = True
            try:
                upstream = route.fetch()
                gate["first_body_has_marker"] = during_marker in upstream.text()
                gate["first_upstream"] = upstream
            except Exception as exc:
                gate["errors"].append(f"route fetch: {exc}")
                if not gate["fulfilled"]:
                    route.abort()
                return
            gate["upstream_ready"].set()
            if not gate["release"].wait(timeout=scale_timeout(25)):
                gate["errors"].append("release timeout in route handler")
                return
            if gate["fulfilled"]:
                return
            gate["fulfilled"] = True
            try:
                route.fulfill(response=gate["first_upstream"])
            except Exception as exc:
                if "already handled" not in str(exc):
                    gate["errors"].append(f"route fulfill: {exc}")
            return
        try:
            route.fulfill(response=route.fetch())
        except Exception as exc:
            gate["errors"].append(f"route retry fetch: {exc}")
            if not gate["fulfilled"]:
                route.abort()

    page.route(history_pattern, inject_during_first_fetch)
    injector = threading.Thread(
        target=_during_capture_injector_thread,
        args=(gate, session_name, bindir, during_marker),
        daemon=True,
    )
    try:
        page.evaluate("""() => {
            window.__remoteDuringCaptureReady = null;
            window.addEventListener('message', function(event) {
                if (!event.data || event.data.type !== 'terminal-history-refresh-done') return;
                if (event.data.requestId !== 'remote-during-cap-req') return;
                const term = window.term;
                const buf = term && term.buffer && term.buffer.active;
                const lines = [];
                if (buf) {
                    for (let i = 0; i < buf.length; i++) {
                        const line = buf.getLine(i);
                        if (line) lines.push(line.translateToString(true));
                    }
                }
                window.__remoteDuringCaptureReady = {
                    event: event.data,
                    text: lines.join('\\n'),
                };
            });
        }""")
        injector.start()
        page.evaluate("""() => {
            window.postMessage({
                type: 'terminal-bootstrap-correlation',
                requestId: 'remote-during-cap-req',
                agentStatus: 'idle',
            }, '*');
        }""")
        page.wait_for_function(
            """() => {
                const term = window.term;
                return !!(
                    term &&
                    (term.__claudeHubRemoteStableBootstrapInFlight === true ||
                     term.__claudeHubReplayBuffering === true)
                );
            }""",
            timeout=int(scale_timeout(15) * 1000),
        )
        deadline = time.time() + scale_timeout(20)
        while time.time() < deadline and not gate["fetch_started"]:
            time.sleep(0.05)
        assert gate["fetch_started"], (
            "bootstrap did not start history fetch; " f"fetch_count={gate['fetch_count']}"
        )
        page.wait_for_function(
            "() => window.__remoteDuringCaptureReady !== null",
            timeout=int(scale_timeout(25) * 1000),
        )
        injector.join(timeout=scale_timeout(25))
        assert not injector.is_alive(), "during-capture injector thread did not finish"
        assert gate["errors"] == [], f"injector/route errors: {gate['errors']}"
        payload = page.evaluate("() => window.__remoteDuringCaptureReady")
        assert (
            gate["first_body_has_marker"] is False
        ), "during-capture marker must not be in first upstream snapshot"
        assert gate["fetch_count"] >= 2, (
            f"nonempty capture at fetch completion must reject first attempt and retry; "
            f"got fetch_count={gate['fetch_count']}"
        )
        assert payload["event"].get("ok") is True
        at_ready: str = payload["text"]
        assert (
            during_marker in at_ready
        ), f"during-capture marker must survive drain+retry; tail: {at_ready[-500:]}"
        during_lines = [ln.strip() for ln in at_ready.splitlines() if ln.strip() == during_marker]
        assert (
            len(during_lines) == 1
        ), f"during-capture marker output must appear exactly once; lines={during_lines!r}"
        assert "RMT_SEED_MARKER" in at_ready
        seed_lines = [ln.strip() for ln in at_ready.splitlines() if ln.strip() == "RMT_SEED_MARKER"]
        assert len(seed_lines) == 1, "seed marker output must not duplicate across retry"
    finally:
        gate["release"].set()
        page.unroute(history_pattern, inject_during_first_fetch)

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass


def test_terminalview_deferred_ready_refreshes_when_already_stable(
    isolated_hmr_stack: dict[str, Any],
    page: Page,
) -> None:
    """Deferred bootstrap ready while agent is already stable must issue safe refresh immediately."""
    ctx = isolated_hmr_stack
    base = ctx["base_url"]
    harness_url = ctx["harness_url"]
    bindir = ctx["bindir"]
    http = ctx["session"]

    tab_resp = http.post(
        f"{base}/api/tabs", json={"name": "defer-stable", "agent_type": "terminal"}
    )
    assert tab_resp.status_code == 201
    tab_id = tab_resp.json()["id"]
    session_name = f"claude-hub-{tab_id[:8]}"

    page.goto(f"{base}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if _tmux_has_session(session_name, bindir):
            break
    else:
        pytest.fail("tmux session not created")

    _tmux_send(session_name, bindir, "echo DEFER_STABLE_MARKER", "Enter")
    for _ in range(int(scale_timeout(30))):
        time.sleep(0.2)
        if "DEFER_STABLE_MARKER" in _tmux_capture(session_name, bindir):
            break
    else:
        pytest.fail("seed marker missing in tmux")

    http.put(
        f"{base}/api/tabs/{tab_id}",
        json={"agent_type": "claude", "target": "remote", "remote_profile_id": "test-loopback"},
    )

    page.goto(f"{harness_url}{_HARNESS_HTML}?tabId={tab_id}&agentType=claude")
    page.wait_for_selector('[data-testid="terminal-hmr-harness"]', timeout=15000)

    page.evaluate("""async () => {
        await window.__claudeHubHmrHarness.seedAgentStatus('working');
        await window.__claudeHubHmrHarness.remountTerminalView();
    }""")
    _install_iframe_history_fetch_spy(page)
    page.evaluate("""async () => {
        await window.__claudeHubHmrHarness.seedAgentStatus('idle');
    }""")
    _wait_harness_content_ready(page)

    fetch_count: int = page.evaluate("() => window.__iframeHistoryFetches || 0")
    assert fetch_count >= 1, (
        "deferred bootstrap ready while already stable must issue deferred-status-refresh; "
        f"iframeHistoryFetches={fetch_count}"
    )

    try:
        http.delete(f"{base}/api/tabs/{tab_id}", timeout=5)
    except requests.RequestException:
        pass
