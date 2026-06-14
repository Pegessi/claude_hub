"""Regression guard for the terminal input-latency fix.

The terminal output hot path (the injected ``term.write`` wrapper and its
bottom-follow scheduler) must NOT read DOM geometry per frame. Reading
``scrollTop`` / ``scrollHeight`` / ``clientHeight`` (or calling
``querySelector`` / ``getBoundingClientRect``) inside ``viewportIsAtBottom``
or ``needsBottomScroll`` forces a synchronous layout reflow on every output
frame, which is exactly the regression that made typing feel laggy ("不跟手").

These tests parse the injected JavaScript (a Python f-string in
``claude_hub.api.terminal``) and assert the hot path stays geometry-free,
relying instead on the event-driven ``domAtBottomCached`` cache. See
``docs/working-logs/2026-06-14-terminal-input-latency-v3.md``.
"""

from pathlib import Path

import pytest

TERMINAL_SOURCE = (
    Path(__file__).resolve().parent.parent / "claude_hub" / "api" / "terminal.py"
).read_text(encoding="utf-8")

# Reads that force a synchronous layout reflow when done on the per-frame path.
FORBIDDEN_LAYOUT_READS = (
    "scrollTop",
    "scrollHeight",
    "clientHeight",
    "querySelector",
    "getBoundingClientRect",
    "offsetHeight",
)

# The injected JS lives inside a Python f-string, so its literal braces are
# doubled (``{{`` / ``}}``).
HOT_PATH_FUNCTIONS = ("viewportIsAtBottom", "needsBottomScroll")


def _extract_js_function_body(source: str, name: str) -> str:
    """Return the body of an injected JS function ``name`` (doubled braces).

    Finds ``function <name>(...) {{`` and returns text up to the matching
    ``}}``, accounting for the f-string's doubled braces.
    """
    marker = f"function {name}("
    start = source.find(marker)
    assert start != -1, f"hot-path function {name!r} not found in terminal.py"

    open_idx = source.find("{{", start)
    assert open_idx != -1, f"opening brace for {name!r} not found"

    depth = 0
    i = open_idx
    end = -1
    while i < len(source) - 1:
        pair = source[i : i + 2]
        if pair == "{{":
            depth += 1
            i += 2
            continue
        if pair == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
            continue
        i += 1
    assert end != -1, f"matching closing brace for {name!r} not found"
    return source[open_idx:end]


@pytest.mark.parametrize("name", HOT_PATH_FUNCTIONS)
def test_hot_path_function_has_no_layout_reads(name: str) -> None:
    """The per-frame bottom-check functions must not read DOM geometry."""
    body = _extract_js_function_body(TERMINAL_SOURCE, name)
    offenders = [token for token in FORBIDDEN_LAYOUT_READS if token in body]
    assert not offenders, (
        f"{name}() regained forced-reflow DOM read(s) {offenders}; this reintroduces "
        f"per-frame layout thrash on the terminal output path. Read the cached "
        f"`domAtBottomCached` flag instead. See "
        f"docs/working-logs/2026-06-14-terminal-input-latency-v3.md"
    )


@pytest.mark.parametrize("name", HOT_PATH_FUNCTIONS)
def test_hot_path_function_uses_cached_flag(name: str) -> None:
    """The hot path must consult the event-driven cache, not recompute."""
    body = _extract_js_function_body(TERMINAL_SOURCE, name)
    assert "domAtBottomCached" in body, (
        f"{name}() no longer reads the cached `domAtBottomCached` flag; the "
        f"event-driven bottom-tracking mechanism may have been removed."
    )


def _extract_js_function_body_single(source: str, name: str) -> str:
    """Like ``_extract_js_function_body`` but returns only this function's body.

    ``_extract_js_function_body`` returns text from the opening brace to the
    matching close, which already isolates one function. This thin alias names
    the intent at the call site for the v4 per-frame-cost guard.
    """
    return _extract_js_function_body(source, name)


# The per-output-frame stats scan (``terminalDataStats``) runs on every
# ``term.write`` via ``noteLiveWrite`` -> ``noteResyncPressure``. Decoding the
# whole frame (``TextDecoder``) or running regex passes (``.replace(`` /
# ``.match(``) over it on that path starves keystroke echo under heavy output —
# the v4 regression ("不跟手" under load). The fix replaced it with an
# allocation-free byte/char scan; this guard keeps the heavy ops out.
FORBIDDEN_PER_FRAME_OPS = ("TextDecoder", ".replace(", ".match(")


def test_per_frame_stats_scan_has_no_decode_or_regex() -> None:
    """``terminalDataStats`` must stay an O(n) scan — no decode/regex per frame."""
    body = _extract_js_function_body_single(TERMINAL_SOURCE, "terminalDataStats")
    # Strip ``//`` line comments so the forbidden tokens are only matched in
    # real code — the function body documents what it replaced ("old TextDecoder
    # + regex passes"), and that prose must not trip the guard.
    code = "\n".join(line.split("//", 1)[0] for line in body.splitlines())
    offenders = [token for token in FORBIDDEN_PER_FRAME_OPS if token in code]
    assert not offenders, (
        f"terminalDataStats() regained per-frame {offenders}; it runs on every "
        f"output frame and decoding/regex over the whole frame starves keystroke "
        f"echo under heavy output. Keep it an allocation-free byte/char scan. See "
        f"docs/working-logs/2026-06-14-terminal-input-latency-v4.md"
    )


def test_cached_flag_recompute_helper_exists() -> None:
    """The single geometry-reading helper that maintains the cache must exist."""
    assert "function recomputeDomAtBottom(" in TERMINAL_SOURCE, (
        "recomputeDomAtBottom() helper is missing; it is the single place allowed "
        "to read viewport geometry and refresh the `domAtBottomCached` cache."
    )
    # The resize guard refreshes the cache cross-scope through this hook.
    assert "term.__claudeHubRecomputeBottom" in TERMINAL_SOURCE, (
        "term.__claudeHubRecomputeBottom hook is missing; resize/fit layout "
        "changes would leave `domAtBottomCached` stale."
    )
