"""Live work log and activity header acceptance, over the work-log fixture page.

Covers the four states a reader actually meets: a busy turn (step label plus the
counts line, with a failure visible), a turn blocked on a sub-agent, a turn
blocked on the user's answer, and a finished turn's ``Activity`` header. The
fixture mounts the production components with synthetic turns, because a live
provider cannot be asked to produce an in-flight turn with a known number of
edits, commands and failures.

The harness page issues no API traffic of its own; every request off the owned
Vite origin is blocked, and any request to the Hub API is a failure.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_work_log.py \
    http://127.0.0.1:5395/tests/work_log_harness.html /tmp/work-log-ui [browser]
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

HARNESS_PATH = "/tests/work_log_harness.html"
STOPWATCH_PATTERN = re.compile(r"^\d+:\d{2}$")


def case(page, case_id):
    return page.locator(f'[data-case="{case_id}"]')


def stream_event(sequence, event_type, payload, message_id=None):
    return {
        "stream_sequence": sequence,
        "session_id": "s",
        "tab_id": "t",
        "agent_type": "claude",
        "type": event_type,
        "turn_id": "stream-turn",
        "message_id": message_id,
        "call_id": None,
        "payload": payload,
        "created_at": "2026-11-09T10:00:00Z",
        "redacted": False,
    }


async def streaming_footer(page):
    """Read the streaming case's footer text after one or more appended batches."""
    return (await case(page, "streaming").locator(".turn-working").inner_text()).replace(
        "\n", " | "
    )


async def footer(page, case_id):
    """The live footer's two lines, read as text and as rendered state."""
    section = case(page, case_id)
    counts_line = section.locator(".turn-working-counts-text")
    failed_phrase = section.locator(".turn-working-counts-failed")
    has_counts = await counts_line.count() > 0
    has_failed = await failed_phrase.count() > 0
    return {
        "word": await section.locator(".turn-working-label").all_inner_texts(),
        "step": (await section.locator(".turn-working-step").inner_text()).strip(),
        "elapsed": (await section.locator(".turn-working-elapsed").inner_text()).strip(),
        "counts": await counts_line.all_inner_texts(),
        "counts_class": await counts_line.first.get_attribute("class") if has_counts else None,
        "failed_phrase": (await failed_phrase.inner_text()).strip() if has_failed else None,
        "failed_colour": (
            await failed_phrase.evaluate("element => getComputedStyle(element).color")
            if has_failed else None
        ),
        "orb_animation": await section.locator(".turn-working-orb").evaluate(
            "element => getComputedStyle(element).animationName"
        ),
        "orb_background": await section.locator(".turn-working-orb").evaluate(
            "element => getComputedStyle(element).backgroundColor"
        ),
        "waiting_class": "turn-working--waiting"
        in (await section.locator(".turn-working").get_attribute("class") or ""),
    }


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    url, output_arg = sys.argv[1:3]
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "::1")
        or parsed.port in (None, 5173, 8173, 10025, 18183)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != HARNESS_PATH
    ):
        raise SystemExit("Use an owned numeric-loopback Vite server and the work-log harness path")

    output = Path(output_arg)
    output.mkdir(parents=True, exist_ok=True)
    own_origin = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    page_errors: list[str] = []
    blocked_requests: list[tuple[str, str]] = []
    api_requests: list[tuple[str, str]] = []
    observed: dict[str, dict] = {}

    async with browser_run.context(
        viewport={"width": 1440, "height": 900},
        launch_args=["--no-proxy-server"],
    ) as context:
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        async def route(request_route) -> None:
            target = urlparse(request_route.request.url)
            if f"{target.scheme}://{target.hostname}:{target.port}" == own_origin:
                if target.path.startswith("/api/"):
                    api_requests.append((request_route.request.method, target.path))
                await request_route.continue_()
                return
            blocked_requests.append((request_route.request.method, request_route.request.url))
            await request_route.abort("blockedbyclient")

        await context.route("**/*", route)

        await page.goto(url, wait_until="networkidle")
        await expect(case(page, "busy")).to_be_visible()

        # ── batches appended after mount: the footer must follow every one ───
        # The pane hands the footer a fresh array per batch whose turn objects
        # were mutated in place, so a footer that caches the turn object would
        # freeze on its first label for the whole turn.
        observed["streaming"] = []
        batches = [
            [
                stream_event(1, "turn_started", {"summary": "investigate"}),
                stream_event(2, "thinking_delta", {"text": "look at the reducer"}),
            ],
            [
                stream_event(
                    3,
                    "tool_call_started",
                    {
                        "tool_call_id": "r1",
                        "name": "Read",
                        "args": {"file_path": "src/a.ts"},
                    },
                )
            ],
            [
                stream_event(
                    4,
                    "tool_call_started",
                    {
                        "tool_call_id": "r2",
                        "name": "Read",
                        "args": {"file_path": "src/b.ts"},
                    },
                )
            ],
            [stream_event(5, "tool_call_completed", {"tool_call_id": "r1", "status": "failed"})],
        ]
        expectations = (
            ("· Thinking", None),
            ("· Reading src/a.ts", "1 read"),
            ("Reading 2 files", "2 reads"),
            ("1 failed", None),
        )
        for index, batch in enumerate(batches):
            await page.evaluate("events => window.__workLogStreamProbe.push(events)", batch)
            await page.wait_for_timeout(120)
            text = await streaming_footer(page)
            observed["streaming"].append(text)
            step, counts = expectations[index]
            assert "Working" in text, text
            assert step in text, (index, step, text)
            if counts is not None:
                assert counts in text, (index, counts, text)
            if index == 0:
                assert "Reading" not in text, text
        assert observed["streaming"][0] != observed["streaming"][-1], observed["streaming"]
        assert "1 failed" in observed["streaming"][-1], observed["streaming"][-1]

        # ── a busy turn: the step it is on, and what it has done ──────────────
        busy = await footer(page, "busy")
        observed["busy"] = busy
        assert busy["word"] == ["Working"], busy["word"]
        assert busy["step"] == "· Reading 2 files", busy["step"]
        assert len(busy["counts"]) == 1, busy["counts"]
        assert busy["counts"][0] == "2 edits · 2 commands · 2 reads · 1 failed", busy["counts"][0]
        # A partial failure marks the phrase that names it; the summary line is
        # not repainted, so one failed call cannot read as a stalled turn.
        assert "failed" not in (busy["counts_class"] or ""), busy["counts_class"]
        assert busy["failed_phrase"] == "· 1 failed", busy["failed_phrase"]
        neutral_colour = await case(page, "busy").locator(
            ".turn-working-counts-text"
        ).evaluate("element => getComputedStyle(element).color")
        assert busy["failed_colour"] != neutral_colour, (
            "the failure must be visibly distinct from the neutral counts",
            busy["failed_colour"],
            neutral_colour,
        )
        assert STOPWATCH_PATTERN.match(busy["elapsed"]), busy["elapsed"]
        assert not busy["waiting_class"], "a busy turn must not use the blocked styling"
        assert busy["orb_animation"] != "none", "a busy turn keeps its pulse"

        # ── blocked on a sub-agent: waiting must not read as activity ─────────
        waiting_child = await footer(page, "waiting-subagent")
        observed["waiting-subagent"] = waiting_child
        assert waiting_child["word"] == [], "a blocked turn must not say Working"
        assert waiting_child["step"] == "Waiting on 1 sub-agent", waiting_child["step"]
        assert waiting_child["counts"] == ["1 sub-agent"], waiting_child["counts"]
        assert waiting_child["waiting_class"], "blocked styling must be applied"
        assert waiting_child["orb_animation"] == "none", waiting_child["orb_animation"]
        assert "rgba(0, 0, 0, 0)" == waiting_child["orb_background"], waiting_child[
            "orb_background"
        ]

        # ── blocked on the user: same rule, different sentence ────────────────
        waiting_answer = await footer(page, "waiting-answer")
        observed["waiting-answer"] = waiting_answer
        assert waiting_answer["word"] == [], waiting_answer["word"]
        assert waiting_answer["step"] == "Waiting for your answer", waiting_answer["step"]
        assert waiting_answer["orb_animation"] == "none"
        assert waiting_answer["counts"] == [], "there is no work to count yet"

        # ── nothing reported yet: the clock runs and the state is named ───────
        awaiting = await footer(page, "awaiting-first")
        observed["awaiting-first"] = awaiting
        assert awaiting["word"] == [], "nothing has started, so no Working word"
        assert awaiting["step"] == "Waiting for the first response", awaiting["step"]
        assert STOPWATCH_PATTERN.match(awaiting["elapsed"]), awaiting["elapsed"]
        assert awaiting["counts"] == [], "there is no work to count yet"
        assert not awaiting["waiting_class"], "the turn is live, not blocked by the user"
        assert awaiting["orb_animation"] != "none", "a live turn keeps its pulse"
        assert await page.locator(".agent-waiting-card").count() == 0, (
            "the old three-dot placeholder must be gone"
        )

        # ── finished turns: human activity, never a raw event count ───────────
        counts_header = case(page, "completed-counts")
        meta = (await counts_header.locator(".process-fold-meta").inner_text()).strip()
        observed["completed-counts"] = {"meta": meta}
        assert meta == "Activity · 18 steps · 1 failed · 21m", meta
        toggle = counts_header.locator('[data-testid="history-details-toggle"]')
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        assert await toggle.get_attribute("aria-label") == "Show activity details"

        lazy_header = case(page, "completed-lazy")
        lazy_meta = (await lazy_header.locator(".process-fold-meta").inner_text()).strip()
        lazy_status = (await lazy_header.locator(".process-fold-status").inner_text()).strip()
        observed["completed-lazy"] = {"meta": lazy_meta, "status": lazy_status}
        assert lazy_meta == "Activity · 21m", lazy_meta
        assert lazy_status == "Loading…", lazy_status

        no_duration = case(page, "completed-none")
        bare_meta = (await no_duration.locator(".process-fold-meta").inner_text()).strip()
        observed["completed-none"] = {"meta": bare_meta}
        assert bare_meta == "Activity", bare_meta

        # The raw transport count is what this change removes; it must be gone
        # from every header on the page.
        headers = await page.locator(".process-fold-meta").all_inner_texts()
        assert not [text for text in headers if "event" in text.lower()], headers

        await page.screenshot(path=str(output / "work-log-fixture-desktop.png"), full_page=True)

        # ── mobile: one line each, nothing clipped, no page-level overflow ────
        await page.set_viewport_size({"width": 390, "height": 844})
        await expect(case(page, "busy")).to_be_visible()
        geometry = await page.evaluate(
            """() => [...document.querySelectorAll('.turn-working-line, .turn-working-counts, .process-fold')]
              .map(el => ({ cls: el.className, scroll: el.scrollWidth, client: el.clientWidth }))"""
        )
        observed["mobile_geometry"] = geometry
        assert geometry, "the footer and headers must render at 390px"
        assert all(item["scroll"] <= item["client"] + 1 for item in geometry), geometry
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        mobile_busy = await footer(page, "busy")
        assert mobile_busy["step"] == "· Reading 2 files", mobile_busy["step"]
        assert mobile_busy["counts"][0] == "2 edits · 2 commands · 2 reads · 1 failed"
        await page.screenshot(path=str(output / "work-log-fixture-mobile.png"), full_page=True)

        assert not api_requests, api_requests
        assert not blocked_requests, blocked_requests
        assert not page_errors, page_errors

    report = {
        "result": "PASS",
        "observed": observed,
        "api_requests": api_requests,
        "blocked_requests": blocked_requests,
        "page_errors": page_errors,
        "boundary": (
            "Real ChatTurnWorking/ChatTurnParts with fixture turns and fixture owners; "
            "no Hub API traffic and no live provider."
        ),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
