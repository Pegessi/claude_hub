"""Dot proactive scouting: interest radar, dedupe ledger and the two-stage funnel.

The deterministic gating logic is exercised with injected in-memory model and
notifier fakes so no live MASO provider or Feishu binding is required. These
tests pin the cost-control contract: a low-score Stage 1 run must make zero
Stage 2 calls and zero notifications, and ``dry_run`` must never notify.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from claude_hub.services.dot import scouting
from claude_hub.services.dot.scouting import (
    DEFAULT_STAGE1_MODEL,
    INTEREST_LINES,
    SPEAK_BAR_SCORE,
    STAGE1_PASS_SCORE,
    InterestProfile,
    ScoutingEngine,
    ScoutingStore,
    Stage1Candidate,
    Stage2Verdict,
    build_interest_profile,
    canonical_url,
    composite_score,
    parse_stage1,
    parse_stage2,
    render_speak_message,
    score_candidate,
    topic_fingerprint,
    url_fingerprint,
)
from claude_hub.services.dot.storage.cards import Card

# ── fixtures / fakes ───────────────────────────────────────────────────────


class FakeDotStore:
    """Minimal DotStore surface used by ScoutingEngine.collect_local_signals."""

    def __init__(
        self,
        *,
        journal: Optional[list[str]] = None,
        cards: Optional[list[Card]] = None,
        focus: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self._journal = journal or []
        self._cards = cards or []
        self._focus = focus or []

    def recent_journal(self, max_entries: int = 24, max_chars: int = 600) -> list[str]:
        return list(self._journal[-max_entries:])

    def list_cards(self, *args: Any, **kwargs: Any) -> list[Card]:
        return list(self._cards)

    def list_focus(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._focus)


class FakeModel:
    """Records calls and returns queued raw replies."""

    def __init__(self, replies: Optional[list[str]] = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, prompt: str, model: str) -> str:
        self.calls.append((prompt, model))
        if not self.replies:
            raise AssertionError("unexpected model call (no queued reply)")
        return self.replies.pop(0)


class FakeNotifier:
    def __init__(self, result: str = "sent") -> None:
        self.result = result
        self.calls: list[tuple[str, Optional[str], str]] = []

    async def __call__(self, text: str, title: Optional[str], tone: str) -> str:
        self.calls.append((text, title, tone))
        return self.result


@pytest.fixture()
def store(tmp_path: Path) -> ScoutingStore:
    scout_store = ScoutingStore(tmp_path / "dot")
    scout_store.ensure()
    return scout_store


@pytest.fixture(autouse=True)
def isolated_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never read the operator's real tabs.json during unit tests.
    monkeypatch.setattr(scouting, "read_hub_tab_titles", lambda: [])


def _profile() -> InterestProfile:
    return build_interest_profile(
        journal_entries=[
            "Explored FlashAttention and KV cache inference throughput",
            "Designed an MCP based agent harness with proactive routines",
        ],
        card_titles=[("vLLM serving", "paged attention inference")],
        focus_titles=["arxiv MoE training feature"],
        tab_titles=["codex agent run"],
    )


def _candidate(url: str = "https://github.com/example/flash", **kw: Any) -> Stage1Candidate:
    base: dict[str, Any] = dict(
        title="FlashAttention v3 ships faster inference kernels",
        summary="New attention kernels cut decode latency and improve KV cache throughput",
        interest_line=INTEREST_LINES[0],
        url=url,
        why_relevant="matches the user's inference and attention engineering interest",
        relevance=0.9,
        novelty=0.9,
    )
    base.update(kw)
    return Stage1Candidate(**base)


# ── 1. interest profile extraction ─────────────────────────────────────────


def test_profile_extraction_maps_two_interest_lines() -> None:
    profile = _profile()
    train = {term.text for term in profile.terms if term.line == INTEREST_LINES[0]}
    agent = {term.text for term in profile.terms if term.line == INTEREST_LINES[1]}
    assert {"inference", "kv cache", "attention", "training"} & train
    assert {"agent", "harness", "mcp"} & agent
    # Both main lines show a signal count and every source was scanned.
    assert set(profile.signals) == set(INTEREST_LINES)
    assert profile.sources_scanned["journal"] == 2
    assert profile.sources_scanned["focus"] == 1


def test_profile_decays_prior_terms_and_evolves() -> None:
    first = _profile()
    # A term only present in the older profile should decay, not dominate.
    second = build_interest_profile(
        journal_entries=["totally unrelated lunch notes"],
        card_titles=[],
        focus_titles=[],
        tab_titles=[],
        previous=first,
    )
    weights_first = {t.text: t.weight for t in first.terms}
    weights_second = {t.text: t.weight for t in second.terms}
    shared = set(weights_first) & set(weights_second)
    assert shared, "expected some prior terms to carry forward"
    sample = next(iter(shared))
    assert weights_second[sample] <= weights_first[sample]


def test_profile_persists_round_trip(store: ScoutingStore) -> None:
    profile = _profile()
    store.save_profile(profile)
    loaded = store.load_profile()
    assert loaded is not None
    assert loaded.all_terms() == profile.all_terms()
    assert loaded.signals == profile.signals


# ── 2. URL / topic fingerprint dedupe ──────────────────────────────────────


def test_canonical_url_ignores_tracking_and_case() -> None:
    a = canonical_url("https://GitHub.com/foo/bar?utm_source=news#section")
    b = canonical_url("https://github.com/foo/bar?ref=feed")
    assert a == b == "https://github.com/foo/bar"
    assert url_fingerprint("https://github.com/foo/bar?utm_source=x") == url_fingerprint(
        "https://github.com/foo/bar?gclid=y"
    )


def test_topic_fingerprint_collides_rewritten_reposts() -> None:
    t1 = topic_fingerprint("New MoE training paper released", "sparse experts")
    t2 = topic_fingerprint("New MoE training paper released!", "sparse experts.")
    assert t1 == t2
    t3 = topic_fingerprint("Completely different agent harness design", "mcp tools")
    assert t3 != t1


def test_seen_url_and_topic_zero_novelty(store: ScoutingStore) -> None:
    profile = _profile()
    candidate = _candidate()
    # First sighting: novel.
    fresh = score_candidate(candidate, profile, seen_url=False, seen_topic=False)
    assert fresh.novelty > 0
    # Same URL already in the ledger: novelty must collapse to zero.
    store.remember(
        url_fprints={
            url_fingerprint(candidate.url): {
                "url": canonical_url(candidate.url),
                "first_seen": "2026-01-01T00:00:00",
                "title": candidate.title,
            }
        },
        topic_fprints={},
        run=_blank_run(),
    )
    again = score_candidate(
        _candidate(),
        profile,
        seen_url=store.has_seen_url(url_fingerprint(candidate.url)),
        seen_topic=False,
    )
    assert again.novelty == 0
    assert composite_score(again) < STAGE1_PASS_SCORE


# ── 3. low-score Stage 1 short-circuits: zero Stage 2, zero notify ─────────


def _noise_stage1_reply() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "title": "A generic think-piece with no technical substance",
                    "summary": "broad opinion about the industry, lunch, weather",
                    "interest_line": INTEREST_LINES[0],
                    "url": "https://reddit.com/r/random/opinion",
                    "why_relevant": "loosely mentions AI",
                    "relevance": 0.2,
                    "novelty": 0.1,
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_low_score_stage1_makes_zero_stage2_calls_and_zero_notify(
    store: ScoutingStore,
) -> None:
    stage1 = FakeModel([_noise_stage1_reply()])
    stage2 = FakeModel()  # any call here fails the test
    notifier = FakeNotifier()
    engine = ScoutingEngine(store, stage1_caller=stage1, stage2_caller=stage2, notifier=notifier)

    result = await engine.run(FakeDotStore(journal=["inference attention kernels"]), dry_run=True)

    assert result.stage1_invoked is True
    assert len(stage1.calls) == 1
    # The cost-control guarantee: no expensive verification, no interruption.
    assert result.stage2_invoked is False
    assert stage2.calls == []
    assert notifier.calls == []
    assert result.speaks == []
    assert result.notified == []


@pytest.mark.asyncio
async def test_seen_item_is_filtered_before_stage2(store: ScoutingStore) -> None:
    candidate = _candidate()
    # Pre-seed the ledger as if the URL was already scouted.
    store.remember(
        url_fprints={
            url_fingerprint(candidate.url): {
                "url": canonical_url(candidate.url),
                "first_seen": "2026-01-01T00:00:00",
                "title": candidate.title,
            }
        },
        topic_fprints={},
        run=_blank_run(),
    )
    stage1 = FakeModel([json.dumps({"candidates": [candidate.model_dump()]})])
    stage2 = FakeModel()
    notifier = FakeNotifier()
    engine = ScoutingEngine(store, stage1_caller=stage1, stage2_caller=stage2, notifier=notifier)
    result = await engine.run(FakeDotStore(journal=["inference attention"]), dry_run=False)
    assert result.stage2_invoked is False
    assert stage2.calls == []
    assert notifier.calls == []


# ── 4. Stage 2 pass -> Speak-Bar message format ────────────────────────────


def _passing_stage2_reply(url: str) -> str:
    return json.dumps(
        {
            "url": url,
            "title": "FlashAttention v3 verified release",
            "verified": True,
            "published_at": "2026-11-20",
            "primary_source": True,
            "speak_score": 0.9,
            "hit_reason": "直接命中你在做的推理算子/KV cache 优化方向",
            "next_step": "读 release note，评估能否替换当前 attention kernel",
            "evidence": [{"title": "Official release", "url": url, "published_at": "2026-11-20"}],
            "verdict_note": "primary GitHub release, date confirmed",
        }
    )


@pytest.mark.asyncio
async def test_stage2_pass_pushes_formatted_message(store: ScoutingStore) -> None:
    candidate = _candidate()
    stage1 = FakeModel([json.dumps({"candidates": [candidate.model_dump()]})])
    stage2 = FakeModel([_passing_stage2_reply(candidate.url)])
    notifier = FakeNotifier()
    engine = ScoutingEngine(store, stage1_caller=stage1, stage2_caller=stage2, notifier=notifier)

    result = await engine.run(
        FakeDotStore(journal=["FlashAttention KV cache inference throughput"]), dry_run=False
    )

    assert result.stage2_invoked is True
    assert len(stage2.calls) == 1
    assert len(result.speaks) == 1
    assert len(notifier.calls) == 1
    text, title, tone = notifier.calls[0]
    assert title and tone == "info"
    # The high-signal message carries the primary link, hit reason and next step.
    assert candidate.url in text
    assert "命中" in text
    assert "建议抓手" in text
    assert result.notified and result.run.notified == 1


@pytest.mark.asyncio
async def test_dry_run_never_notifies_even_when_speak_bar_passes(
    store: ScoutingStore,
) -> None:
    candidate = _candidate()
    stage1 = FakeModel([json.dumps({"candidates": [candidate.model_dump()]})])
    stage2 = FakeModel([_passing_stage2_reply(candidate.url)])
    notifier = FakeNotifier()
    engine = ScoutingEngine(store, stage1_caller=stage1, stage2_caller=stage2, notifier=notifier)

    result = await engine.run(
        FakeDotStore(journal=["FlashAttention KV cache inference"]), dry_run=True
    )
    # The verdict still passes the bar, but the dry-run gate suppresses the push.
    assert result.speaks and result.stage2_invoked is True
    assert notifier.calls == []
    assert result.notified == []
    assert result.run.dry_run is True


@pytest.mark.asyncio
async def test_stage2_below_speak_bar_is_silenced(store: ScoutingStore) -> None:
    candidate = _candidate()
    weak = json.dumps(
        {
            "url": candidate.url,
            "title": candidate.title,
            "verified": False,
            "primary_source": False,
            "speak_score": SPEAK_BAR_SCORE - 0.2,
            "hit_reason": "only loosely related, secondary blog",
            "next_step": "",
            "evidence": [],
            "verdict_note": "not primary",
        }
    )
    stage1 = FakeModel([json.dumps({"candidates": [candidate.model_dump()]})])
    stage2 = FakeModel([weak])
    notifier = FakeNotifier()
    engine = ScoutingEngine(store, stage1_caller=stage1, stage2_caller=stage2, notifier=notifier)
    result = await engine.run(FakeDotStore(journal=["inference attention"]), dry_run=False)
    assert result.stage2_invoked is True
    assert result.speaks == []
    assert result.silenced and notifier.calls == []


# ── parsing hardening / untrusted input ────────────────────────────────────


def test_parse_stage1_drops_invalid_items_and_code_fence() -> None:
    raw = (
        "```json\n"
        + json.dumps(
            {
                "candidates": [
                    _candidate("https://github.com/a/one").model_dump(),
                    {"title": "no url here"},  # invalid -> dropped
                    "not-an-object",  # invalid -> dropped
                ]
            }
        )
        + "\n```"
    )
    parsed = parse_stage1(raw)
    assert len(parsed) == 1
    assert parsed[0].url == "https://github.com/a/one"


def test_parse_stage1_rejects_non_http_url() -> None:
    raw = json.dumps({"candidates": [{"title": "x", "url": "file:///etc/passwd"}]})
    assert parse_stage1(raw) == []


def test_parse_stage2_reanchors_fallback_url() -> None:
    verdict = parse_stage2("not json at all", "https://github.com/fallback")
    assert verdict is None
    good = parse_stage2(
        _passing_stage2_reply("https://github.com/verified"),
        "https://github.com/fallback",
    )
    assert good is not None and good.speak_score == 0.9


def test_parse_stage2_salvages_truncated_reply() -> None:
    # A model reply cut off mid-string by the token limit must still salvage the
    # complete leading fields instead of dropping the whole verdict.
    truncated = (
        '{"url":"https://github.com/x/y","title":"T","verified":true,'
        '"primary_source":true,"speak_score":0.9,"hit_reason":"r",'
        '"next_step":"n","evidence":[],"verdict_note":"ok'
    )
    verdict = parse_stage2(truncated, "https://github.com/fallback")
    assert verdict is not None
    assert verdict.verified is True
    assert verdict.speak_score == 0.9
    assert verdict.url == "https://github.com/x/y"


def test_parse_stage1_salvages_truncated_tail() -> None:
    truncated = (
        '{"candidates":[{"title":"One","url":"https://github.com/a/one",'
        '"summary":"s","relevance":0.8,"novelty":0.7},{"title":"Two broken"'
    )
    parsed = parse_stage1(truncated)
    # The complete first candidate survives; the partial second is discarded.
    assert len(parsed) == 1
    assert parsed[0].url == "https://github.com/a/one"


def test_render_message_is_bounded_and_contains_link() -> None:
    candidate = _candidate()
    verdict = Stage2Verdict(
        url=candidate.url,
        title="T",
        verified=True,
        primary_source=True,
        speak_score=0.9,
        hit_reason="r",
        next_step="n",
    )
    message = render_speak_message(verdict, candidate)
    assert candidate.url in message
    assert len(message) < 1200


def test_run_record_and_ledger_persist(store: ScoutingStore) -> None:
    profile = _profile()
    store.save_profile(profile)
    store.remember(
        url_fprints={
            "fp1": {
                "url": "https://github.com/x",
                "first_seen": "2026-01-01T00:00:00",
                "title": "t",
            }
        },
        topic_fprints={"tp1": {"first_seen": "2026-01-01T00:00:00", "title": "t"}},
        run=_blank_run(),
    )
    state = store.read_state()
    assert "fp1" in state["seen_urls"]
    assert "tp1" in state["seen_topics"]
    assert len(state["runs"]) == 1
    assert store.has_seen_url("fp1") and store.has_seen_topic("tp1")


def test_default_stage1_model_is_the_required_deepseek() -> None:
    assert DEFAULT_STAGE1_MODEL == "deepseek-v41-flash-reasoning-high"


# ── helpers ────────────────────────────────────────────────────────────────


def _blank_run() -> scouting.RunRecord:
    return scouting.RunRecord(
        at="2026-01-01T00:00:00",
        dry_run=True,
        profile_terms=0,
        stage1_invoked=False,
        stage1_candidates=0,
        stage2_invoked=False,
        stage2_verdicts=0,
        notified=0,
        silenced=0,
    )
