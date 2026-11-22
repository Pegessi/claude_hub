"""Proactive interest radar and cost-controlled scouting funnel for Dot.

This module owns the live adapters (read-only ephemeral MASO Workspace Chat
calls with strong-model discovery and fallback), the orchestrating
:class:`ScoutingEngine` and active Hub-tab discovery. All deterministic state,
untrusted-input models, parsing, scoring and the JSON ledger live in
:mod:`claude_hub.services.dot.scouting_core`, re-exported here so existing
imports (``from ...scouting import X``) keep working.

Durable state lives in ``<dot home>/scouting.json`` rather than the resident
``session_pack``/``turn_pack``, so scouting memory stays on demand and never
grows the prompt of every Dot turn.

Pipeline
--------
1. ``build_interest_profile`` deterministically scans recent Dot journal
   entries, memory cards, focus items and the active Hub chat tabs and updates
   two evolving interest lines ("training/inference engineering features" and
   "frontier agent tooling / harness architecture").
2. Stage 1 fans a configured or discovered inexpensive model
   across primary public sources. Candidates are scored for relevance against the
   profile and novelty against a persistent URL/topic-fingerprint ledger. When
   nothing clears the bar the run short-circuits with **zero** Stage 2 calls and
   **zero** notifications.
3. At most ``MAX_SPEAK_CANDIDATES`` high-potential candidates per run reach a
   strong reasoning Stage 2 model that verifies the primary URL/date in a strict
   read-only manner and ties the item to the user's current work.
4. Only items that pass the Speak-Bar threshold reach the notifier (``dot
   notify``). Everything else is persisted silently. ``dry_run`` never notifies.

All external web/model content is treated as untrusted, read-only input.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional

from ..runtime_isolation import resolve_runtime_home
from .scouting_core import *  # noqa: F401,F403  (public API governed by core __all__)
from .scouting_core import (
    DEFAULT_STAGE1_MODEL,
    DEFAULT_STAGE2_MODEL,
    MAX_SPEAK_CANDIDATES,
    STAGE1_MODEL_SETTING,
    STAGE2_FALLBACK_MODELS,
    STAGE2_MODEL_PREFERENCE,
    STAGE2_MODEL_SETTING,
    InterestProfile,
    ModelCaller,
    Notifier,
    RunRecord,
    ScoutingStore,
    ScoutResult,
    Stage1Candidate,
    Stage2Verdict,
    _now_iso,
    build_stage1_prompt,
    build_stage2_prompt,
    composite_score,
    parse_stage1,
    parse_stage2,
    render_speak_message,
    score_candidate,
    topic_fingerprint,
    url_fingerprint,
)

logger = logging.getLogger(__name__)


# ── default model adapter (read-only ephemeral Workspace Chat) --------------


async def default_model_caller(prompt: str, model: str) -> str:
    """Run one read-only model turn via an ephemeral Workspace Chat session.

    Mirrors ``demand_patrol.feishu_triage.diagnose_message``: provision a
    solo, ephemeral MASO/Chat session, send the prompt, await the answer and
    archive the session. A single fresh-session retry absorbs the transient
    empty-turn/startup flakiness that rapid ephemeral provisioning can hit.
    Imported lazily so the deterministic core stays importable in contexts
    without the full workspace stack.
    """
    if not model:
        raise RuntimeError("a concrete model id is required for the default scouting caller")

    last_exc: Optional[BaseException] = None
    for attempt in (1, 2):
        try:
            return await _ephemeral_model_turn(prompt, model)
        except (RuntimeError, TimeoutError) as exc:
            last_exc = exc
            logger.warning("scouting model turn failed (attempt %s)", attempt, exc_info=True)
    assert last_exc is not None
    raise last_exc


async def resolve_maso_base_url() -> str:
    """Return a verified-ready MASO base URL via service discovery.

    Honours an explicit ``MASO_BASE_URL`` first; otherwise probes the local
    registry/loopback candidates through ``discover_maso_service`` instead of
    assuming the compiled-in default port. Raises ``MasoServiceUnavailable``
    when no usable local service is found.
    """
    import os

    from ..maso_discovery import discover_maso_service

    env = {"MASO_BASE_URL": os.environ["MASO_BASE_URL"]} if os.environ.get("MASO_BASE_URL") else {}
    base_url, _models, _default = await discover_maso_service(env)
    return base_url


async def advertised_model_ids() -> set[str]:
    """Discover the live MASO service and return every model id it advertises."""
    import os

    from ..cross_reviewer import _advertised_model_ids
    from ..maso_discovery import discover_maso_service

    env = {"MASO_BASE_URL": os.environ["MASO_BASE_URL"]} if os.environ.get("MASO_BASE_URL") else {}
    try:
        _base, models, _default = await discover_maso_service(env)
    except Exception:  # noqa: BLE001 - catalog is best effort; callers fall back
        logger.debug("scouting: MASO model catalog unavailable", exc_info=True)
        return set()
    return set(_advertised_model_ids(models))


def resolve_stage2_model(advertised: set[str], configured: str) -> str:
    """Choose an explicit model or a deterministic reasoning model from the catalog."""
    if configured:
        return configured
    return next(iter(sorted(advertised, key=lambda m: ("high" not in m.lower(), m))), "")


def _stage2_fallback_chain(primary: str, advertised: set[str] | None = None) -> list[str]:
    """Try the configured model followed by distinct live catalog choices."""
    return list(dict.fromkeys(model for model in [primary, *sorted(advertised or ())] if model))[:4]


async def _ephemeral_model_turn(prompt: str, model: str) -> str:
    from claude_hub.models import (
        AgentType,
        EnsureWorkspaceAgentRequest,
        SessionKind,
        WorkspaceSessionRole,
    )
    from claude_hub.services.agent_stream.providers.spec import all_specs
    from claude_hub.services.workspace_manager import workspace_manager

    maso_spec = all_specs().get(AgentType.MASO)
    env: dict[str, str] = {}
    if maso_spec is not None and maso_spec.model_env_key:
        env[maso_spec.model_env_key] = model
    # Discover a verified-ready endpoint rather than trusting a default port.
    try:
        env["MASO_BASE_URL"] = await resolve_maso_base_url()
    except Exception:  # noqa: BLE001 - leave env unset so the provider self-discovers
        logger.debug(
            "scouting: MASO discovery failed; letting provider self-discover", exc_info=True
        )

    request = EnsureWorkspaceAgentRequest(
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        role=WorkspaceSessionRole.WORKER,
        title=f"Dot scouting: {model}",
        solo_mode=True,
        reuse_existing=False,
        ephemeral=True,
        caller_owned_ephemeral=True,
        env=env,
    )
    workspace = _resolve_scout_workspace()
    session = await workspace_manager.ensure_workspace_agent(workspace, request, bootstrap=False)
    try:
        return await _run_ephemeral_turn(session, prompt)
    finally:
        # Shield cleanup: an archive failure must not mask the turn result or
        # propagate out of the model call (the session is quaranteed elsewhere).
        await _shield_archive(workspace, session.id)


async def _shield_archive(workspace_id: str, session_id: str) -> None:
    from claude_hub.services.workspace_manager import workspace_manager

    try:
        await workspace_manager.archive_session(workspace_id, session_id)
    except Exception:  # pragma: no cover - cleanup is best effort
        logger.warning(
            "scouting: failed to archive ephemeral session %s", session_id, exc_info=True
        )


def _resolve_scout_workspace() -> str:
    from claude_hub.models import ExecutionTarget
    from claude_hub.services.workspace_manager import workspace_manager

    for workspace in workspace_manager.list_workspaces():
        if workspace.target == ExecutionTarget.LOCAL:
            return workspace.id
    workspaces = workspace_manager.list_workspaces()
    if workspaces:
        return workspaces[0].id
    raise RuntimeError("No Agent Workspace is available for Dot scouting")


async def _run_ephemeral_turn(session: Any, prompt: str) -> str:
    import asyncio

    from claude_hub.api.agent_stream import get_managed_tailer_manager
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.services import managed_chat
    from claude_hub.services.agent_stream.base import is_substantive_answer
    from claude_hub.services.workspace_manager import workspace_manager

    manager = get_managed_tailer_manager()
    call_id = f"dot-scouting-{session.id}"
    queue = await manager.subscribe_completion(session)
    try:
        await workspace_manager.send_chat_session_message(session.id, prompt, call_id=call_id)
        deadline = asyncio.get_running_loop().time() + 600
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await managed_chat.cancel(session, expected_turn_id=call_id)
                raise TimeoutError("Dot scouting turn timed out")
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                await managed_chat.cancel(session, expected_turn_id=call_id)
                raise TimeoutError("Dot scouting turn timed out") from None
            if event.turn_id != call_id:
                continue
            if event.type == AgentStreamEventType.APPROVAL_REQUIRED:
                await manager.retire_external_turn(session, expected_turn_id=call_id)
                raise RuntimeError("Scouting turn unexpectedly required approval")
            if event.type == AgentStreamEventType.ERROR:
                message = event.payload.get("message")
                if isinstance(message, str) and message.strip():
                    raise RuntimeError(f"Scouting turn error: {message}")
                continue
            if event.type != AgentStreamEventType.TURN_COMPLETED:
                continue
            # A completed turn can still carry a failure status (provider error,
            # cancellation, empty answer). Treat those as failures, not success.
            status_value = event.payload.get("status")
            if status_value not in (None, "completed", "success"):
                error_message = event.payload.get("error_message")
                raise RuntimeError(
                    error_message
                    if isinstance(error_message, str) and error_message
                    else f"Scouting turn ended with status {status_value}"
                )
            assistant_text = event.payload.get("assistant_text")
            if isinstance(assistant_text, str) and is_substantive_answer(assistant_text):
                final_text = event.payload.get("final_text")
                if isinstance(final_text, str) and is_substantive_answer(final_text):
                    return final_text.strip()
                return assistant_text.strip()
            raise RuntimeError("Scouting turn completed without substantive text")
    finally:
        manager.unsubscribe_completion(session.id, queue)


# ── engine ------------------------------------------------------------------


class ScoutingEngine:
    """Orchestrates the radar update and the two-stage cost-controlled funnel."""

    def __init__(
        self,
        store: Optional[ScoutingStore] = None,
        *,
        stage1_caller: Optional[ModelCaller] = None,
        stage2_caller: Optional[ModelCaller] = None,
        notifier: Optional[Notifier] = None,
        stage1_model: Optional[str] = None,
        stage2_model: Optional[str] = None,
    ) -> None:
        self.store = store or ScoutingStore()
        self._stage1_caller = stage1_caller
        self._stage2_caller = stage2_caller
        self._notifier = notifier
        self.stage1_model = stage1_model or _setting(STAGE1_MODEL_SETTING, DEFAULT_STAGE1_MODEL)
        self.stage2_model = stage2_model or _setting(STAGE2_MODEL_SETTING, DEFAULT_STAGE2_MODEL)
        # Resolved lazily against the live MASO catalog; cached for the run.
        self._resolved_stage2_model: Optional[str] = None

    # ── local signal collection ───────────────────────────────────────────

    def collect_local_signals(
        self, dot_store: Any
    ) -> tuple[list[str], list[tuple[str, str]], list[str], list[str]]:
        """Read bounded local signals from the Dot store and Hub tabs.

        Returns ``(journal_entries, card_titles, focus_titles, tab_titles)``.
        Failures reading one source degrade to empty; scouting never blocks on
        an unreadable optional source.
        """
        journal = _safe(lambda: dot_store.recent_journal(MAX_JOURNAL_ENTRIES, 600), [])
        cards = _safe(lambda: dot_store.list_cards(), [])
        card_titles = [
            (str(card.title), str(card.summary or ""))
            for card in cards
            if getattr(card, "title", None)
        ]
        focus = _safe(lambda: dot_store.list_focus(), [])
        focus_titles = [str(item.get("title", "")) for item in focus if item.get("title")]
        tab_titles = _safe(read_hub_tab_titles, [])
        return journal, card_titles, focus_titles, tab_titles

    def refresh_profile(self, dot_store: Any) -> InterestProfile:
        journal, cards, focus, tabs = self.collect_local_signals(dot_store)
        profile = build_interest_profile(
            journal_entries=journal,
            card_titles=cards,
            focus_titles=focus,
            tab_titles=tabs,
            previous=self.store.load_profile(),
        )
        self.store.save_profile(profile)
        return profile

    # ── callers (injectable, default to the ephemeral Chat adapter) ───────

    async def _call_stage1(self, prompt: str) -> str:
        if self._stage1_caller is not None:
            return await self._stage1_caller(prompt, self.stage1_model)
        model = self.stage1_model
        if not model:
            advertised = await advertised_model_ids()
            model = next(iter(sorted(advertised, key=lambda m: ("flash" not in m.lower(), m))), "")
        self.stage1_model = model
        return await default_model_caller(prompt, model)

    async def _resolve_stage2_model(self) -> str:
        """Resolve a real strong Stage 2 model from the live MASO catalog."""
        if self._resolved_stage2_model is not None:
            return self._resolved_stage2_model
        if self._stage2_caller is not None:
            # Injected/test caller: honour the configured model without discovery.
            self._resolved_stage2_model = self.stage2_model or self.stage1_model
            return self._resolved_stage2_model
        advertised = await advertised_model_ids()
        if not self.stage2_model and self.stage1_model:
            from ..cross_reviewer import maso_model_family

            family = maso_model_family({"MASO_MODEL": self.stage1_model})
            different = {m for m in advertised if maso_model_family({"MASO_MODEL": m}) != family}
            advertised = different or advertised
        self._resolved_stage2_model = resolve_stage2_model(advertised, self.stage2_model)
        return self._resolved_stage2_model

    async def _call_stage2(self, prompt: str) -> str:
        if self._stage2_caller is not None:
            return await self._stage2_caller(prompt, self.stage2_model or self.stage1_model)
        primary = await self._resolve_stage2_model()
        # Try the strong model first, then a short family-diverse fallback chain
        # so one unavailable/failing model does not abort verification.
        chain = _stage2_fallback_chain(primary, await advertised_model_ids())
        if not chain:
            raise RuntimeError("No scouting verifier model is configured or advertised")
        last_exc: Optional[BaseException] = None
        for model in chain:
            try:
                return await default_model_caller(prompt, model)
            except (RuntimeError, TimeoutError) as exc:
                last_exc = exc
                logger.warning("scouting stage2 model %s failed", model, exc_info=True)
        assert last_exc is not None
        raise last_exc

    async def _notify(self, text: str, title: Optional[str], tone: str) -> str:
        if self._notifier is not None:
            return await self._notifier(text, title, tone)
        # Late import keeps the deterministic core free of the service singleton.
        from .service import get_dot_service

        return await get_dot_service().notify(text, title=title, tone=tone)

    # ── main run ──────────────────────────────────────────────────────────

    async def run(self, dot_store: Any, *, dry_run: bool = False) -> ScoutResult:
        self.store.ensure()
        errors: list[str] = []
        profile = self.refresh_profile(dot_store)

        stage1_invoked = False
        stage2_invoked = False
        raw_candidates: list[Stage1Candidate] = []
        verdicts: list[Stage2Verdict] = []
        speaks: list[Stage2Verdict] = []
        silenced: list[Stage2Verdict] = []
        notified: list[Stage2Verdict] = []

        state = self.store.read_state()
        seen_urls = state.get("seen_urls", {})
        seen_topics = state.get("seen_topics", {})

        # ── Stage 1: cheap wide net ────────────────────────────────────────
        try:
            stage1_invoked = True
            raw = await self._call_stage1(build_stage1_prompt(profile))
            raw_candidates = parse_stage1(raw)
        except Exception as exc:  # noqa: BLE001 - record and degrade, never crash
            errors.append(f"stage1 failed: {type(exc).__name__}: {exc}")
            logger.warning("scouting stage1 failed", exc_info=True)
            raw_candidates = []

        scored: list[tuple[float, Stage1Candidate]] = []
        # All observed fingerprints this run, keyed for a possible ledger commit.
        observed_url_fps: dict[str, dict[str, str]] = {}
        observed_topic_fps: dict[str, dict[str, str]] = {}
        # Fingerprints that must NOT be committed: Stage 2/notify failures (live)
        # or every observation during a dry run. Keeping them out lets the item
        # resurface instead of being silently suppressed forever.
        withheld_url_fps: set[str] = set()
        withheld_topic_fps: set[str] = set()
        for candidate in raw_candidates:
            ufp = url_fingerprint(candidate.url)
            tfp = topic_fingerprint(candidate.title, candidate.summary)
            scored_candidate = score_candidate(
                candidate,
                profile,
                seen_url=ufp in seen_urls or ufp in observed_url_fps,
                seen_topic=tfp in seen_topics or tfp in observed_topic_fps,
            )
            total = composite_score(scored_candidate)
            scored.append((total, scored_candidate))
            observed_url_fps.setdefault(
                ufp,
                {
                    "url": canonical_url(candidate.url),
                    "first_seen": _now_iso(),
                    "title": scored_candidate.title[:200],
                },
            )
            observed_topic_fps.setdefault(
                tfp,
                {"first_seen": _now_iso(), "title": scored_candidate.title[:200]},
            )

        # High-potential only; deterministic threshold, max two into Stage 2.
        ranked = sorted(scored, key=lambda pair: pair[0], reverse=True)
        high_potential = [
            candidate
            for total, candidate in ranked
            if total >= STAGE1_PASS_SCORE and candidate.novelty > 0
        ][:MAX_SPEAK_CANDIDATES]

        def _withhold(candidate: Stage1Candidate) -> None:
            """Keep a failed/uncertain candidate out of the seen ledger."""
            withheld_url_fps.add(url_fingerprint(candidate.url))
            withheld_topic_fps.add(topic_fingerprint(candidate.title, candidate.summary))

        # ── Stage 2: strong verification, gated on high-potential exists ───
        if high_potential:
            stage2_invoked = True
        for candidate in high_potential:
            try:
                raw_verdict = await self._call_stage2(build_stage2_prompt(candidate, profile))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"stage2 failed for {candidate.url}: {type(exc).__name__}: {exc}")
                logger.warning("scouting stage2 failed for %s", candidate.url, exc_info=True)
                _withhold(candidate)
                continue
            verdict = parse_stage2(raw_verdict, candidate.url)
            if verdict is None:
                errors.append(f"stage2 returned unparseable verdict for {candidate.url}")
                _withhold(candidate)
                continue
            # Re-anchor to the candidate URL so a model cannot redirect trust.
            verdict.url = candidate.url
            verdicts.append(verdict)
            if (
                verdict.verified
                and verdict.primary_source
                and verdict.speak_score >= SPEAK_BAR_SCORE
            ):
                speaks.append(verdict)
            else:
                # Verified-but-below-bar is a resolved decision: safe to remember.
                silenced.append(verdict)

        # ── Speak-Bar notification (no-op under dry_run) ───────────────────
        for verdict in speaks:
            candidate = _matching_candidate(verdict, raw_candidates)
            message = render_speak_message(verdict, candidate)
            if dry_run:
                # Dry runs never deliver and must never poison the seen ledger.
                _withhold(candidate)
                continue
            try:
                delivery = await self._notify(message, "🛰️ 主动情报雷达", "info")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"notify failed: {type(exc).__name__}: {exc}")
                logger.warning("scouting notify failed", exc_info=True)
                # Delivery failed: withhold so the item can be retried later.
                _withhold(candidate)
                continue
            if delivery == "sent":
                notified.append(verdict)
            elif delivery == "repeat":
                # De-duplicated by the Dot notifier; treat as handled, not error.
                notified.append(verdict)
            else:
                errors.append(f"notify returned {delivery}")
                _withhold(candidate)

        run = RunRecord(
            at=_now_iso(),
            dry_run=dry_run,
            profile_terms=len(profile.terms),
            stage1_invoked=stage1_invoked,
            stage1_candidates=len(raw_candidates),
            stage2_invoked=stage2_invoked,
            stage2_verdicts=len(verdicts),
            notified=len(notified),
            silenced=len(silenced),
            errors=errors,
            speaks=[v.model_dump() for v in speaks],
        )
        if dry_run:
            # A dry run is a preview: record the run for observability but leave
            # both dedupe ledgers untouched so a later real run sees the items.
            self.store.append_run(run)
        else:
            # Commit only the fingerprints that reached a resolved outcome
            # (verified/silenced or successfully notified); withhold failures.
            commit_url_fps = {
                fp: meta for fp, meta in observed_url_fps.items() if fp not in withheld_url_fps
            }
            commit_topic_fps = {
                fp: meta for fp, meta in observed_topic_fps.items() if fp not in withheld_topic_fps
            }
            self.store.remember(url_fprints=commit_url_fps, topic_fprints=commit_topic_fps, run=run)
        return ScoutResult(
            profile=profile,
            stage1_invoked=stage1_invoked,
            stage1_candidates=[c for _, c in ranked],
            stage2_invoked=stage2_invoked,
            verdicts=verdicts,
            speaks=speaks,
            silenced=silenced,
            notified=notified,
            dry_run=dry_run,
            errors=errors,
            run=run,
        )


def _matching_candidate(
    verdict: Stage2Verdict, candidates: Sequence[Stage1Candidate]
) -> Stage1Candidate:
    for candidate in candidates:
        if candidate.url == verdict.url:
            return candidate
    return Stage1Candidate(title=verdict.title or "Scouting item", url=verdict.url)


def _setting(name: str, default: str) -> str:
    from ..local_runtime_settings import local_setting

    value = local_setting(name, default).strip()
    return value or default


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional source must not break scouting
        logger.debug("scouting: optional signal source failed", exc_info=True)
        return default


#: Internal ephemeral tab titles that scouting itself (and patrol) provision;
#: these are machinery, not user interests, so they never feed the radar.
_INTERNAL_TAB_PATTERN = re.compile(r"(?:dot scouting|patrol triage|scouting|triage)", re.IGNORECASE)


def _is_active_chat_tab(tab: Mapping[str, Any]) -> bool:
    if not isinstance(tab, dict):
        return False
    if tab.get("session_kind") != "chat" or tab.get("archived"):
        return False
    name = str(tab.get("name") or "").strip()
    if not name or _INTERNAL_TAB_PATTERN.search(name):
        return False
    return True


def _tab_recency_key(tab: Mapping[str, Any]) -> str:
    # Prefer the last viewed time, then creation time; "" sorts oldest.
    for key in ("last_viewed_at", "created_at"):
        value = tab.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_hub_tab_titles() -> list[str]:
    """Read the most recent active Hub chat tab titles from ``tabs.json``.

    Archived and internal ``Dot scouting:``/``Patrol triage:`` tabs are excluded.
    Active tabs are stored oldest-first, so the *newest*
    :data:`MAX_TAB_TITLES` are returned. Bounded and read-only; a missing or
    corrupt file yields an empty list.
    """
    path = resolve_runtime_home() / "tabs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    tabs = data.get("tabs") if isinstance(data, dict) else None
    if not isinstance(tabs, list):
        return []
    active = [tab for tab in tabs if _is_active_chat_tab(tab)]
    active.sort(key=_tab_recency_key)  # stable oldest -> newest
    newest = active[-MAX_TAB_TITLES:]
    return [str(tab.get("name") or "").strip() for tab in newest if tab.get("name")]
