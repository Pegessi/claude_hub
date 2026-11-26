"""Real-time Feishu WebSocket event triage, courtesy/self-reply suppression, and Dot event bridge."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pydantic import HttpUrl

from claude_hub.models import WorkspaceAttachmentCreate
from claude_hub.services.dot.events_gateway import (
    ExternalEvent,
    record_suppressed_external_event,
)
from claude_hub.services.feishu_realtime_automation import (
    BOT_SENDER_PREFIXES,
    has_ai_reply_footer,
    is_automated_web_sender,
    is_pool_bot_message,
)
from claude_hub.services.feishu_realtime_matching import (
    _HTTP_URL_RE,
    FeishuRealtimeEventConfig,
    _content_fingerprint,
    _parse_created_ms,
    _valid_http_url,
    extract_mentioned_user_ids,
    extract_message_urls,
    is_courtesy_or_ack_only,
    matches_fault_or_task_url,
)
from claude_hub.services.feishu_realtime_topics import (
    _is_newer_reply,
    _OwnerReplyMark,
    _reply_mark_is_newer_or_equal,
    owner_participated,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage
from claude_hub.services.feishu_web_session import rich as _rich
from claude_hub.services.feishu_web_session.adapter import is_in_topic
from claude_hub.services.feishu_web_session.client import PushedMessage, is_web_entity_id
from claude_hub.services.feishu_web_session.rich import WebMessage, describe

logger = logging.getLogger(__name__)

_MAX_HANDLED_LEDGER = 1024
_MAX_REPLY_TRACKER = 1024
RealtimeEventSink = Callable[[ExternalEvent], Awaitable[bool | Mapping[str, Any] | None]]
_DEFAULT_REALTIME_ROUTER: FeishuRealtimeEventRouter | None = None


def matches_watch_group_url(
    chat_id: str,
    chat_name: str,
    text: str,
    config: FeishuRealtimeEventConfig | None = None,
    **kw: Any,
) -> list[str]:
    router = _DEFAULT_REALTIME_ROUTER
    cfg = config or (
        router.resolve_config() if router is not None else FeishuRealtimeEventConfig.from_runtime()
    )
    cand_ids = {
        cid.strip().casefold()
        for cid in (chat_id, *(kw.get("extra_chat_ids") or ()))
        if isinstance(cid, str) and cid.strip()
    }
    if router is not None:
        cand_ids |= {x for cid in list(cand_ids) for x in router.expand_chat_keys(cid)}
    watch_ids_cf = {cid.strip().casefold() for cid in cfg.watch_chat_ids if cid and cid.strip()}
    norm_name, mid = " ".join((chat_name or "").split()).casefold(), str(kw.get("message_id") or "")
    if not (
        bool(cand_ids & watch_ids_cf)
        or (bool(norm_name) and norm_name in cfg.watch_chat_names)
        or (
            router is not None
            and bool(mid)
            and router.matches_pending_failed_watch_delivery(
                message_id=mid, chat_ids=cand_ids, chat_name=norm_name
            )
        )
    ):
        return []
    found = (_valid_http_url(m.group(0)) for m in _HTTP_URL_RE.finditer(text or ""))
    urls = list(dict.fromkeys([*(kw.get("extra_urls") or ()), *filter(None, found)]))
    return [u for u in urls if matches_fault_or_task_url(u, cfg)]


class _CrossTransportRecord:
    def __init__(self, *args: Any) -> None:
        wmid, cmid, cks, cname, tids, sids, hashes, ms, now = args[:9]
        self.web_msg_id, self.canonical_msg_id, self.chat_name = wmid, cmid, cname
        self.chat_keys = frozenset(cks or ())
        self.thread_ids, self.sender_ids, self.content_hashes = (
            frozenset(tids or ()),
            frozenset(sids or ()),
            frozenset(hashes or ()),
        )
        self.created_at_ms, self.recorded_at = ms, now
        e = lambda i, d: args[i] if len(args) > i else d  # noqa: E731
        self.ambiguous, self.alias_id = bool(e(9, False)), str(e(10, ""))
        self.item_id, self.kind = str(e(11, "")), str(e(12, "handled"))
        self.generation, self.text = int(e(13, 0)), str(e(14, ""))
        self.matched_urls, self.event = tuple(e(15, ())), e(16, None)


_FallbackRecord = _CrossTransportRecord


class FeishuRealtimeEventRouter:
    def __init__(
        self,
        *,
        config: FeishuRealtimeEventConfig | None = None,
        event_sink: RealtimeEventSink | None = None,
    ) -> None:
        self._explicit_config, self._event_sink, self._lock = config, event_sink, threading.RLock()
        self._handled_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._cross_transport_records: OrderedDict[str, _CrossTransportRecord] = OrderedDict()
        self._owner_topic_replies: OrderedDict[tuple[str, str], _OwnerReplyMark] = OrderedDict()
        self._owner_chat_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._owner_parent_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._owner_ai_parent_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._known_open_chat_ids: dict[str, str] = {}
        self._known_chat_names: dict[str, set[str]] = {}
        self._known_msg_aliases: dict[str, set[str]] = {}
        self._known_sender_aliases: dict[str, set[str]] = {}
        self._known_sender_names: dict[str, str] = {}
        self._chat_meta_ts: dict[str, float] = {}
        self._last_web_transport: Any = None
        self._pending_fallbacks: OrderedDict[str, _FallbackRecord] = OrderedDict()
        self.fallback_generation = self.emitted_count = self.suppressed_count = 0
        self.suppressed_courtesy_count = self.suppressed_replied_count = 0
        self.delivery_failed_count = 0
        self.last_delivery_error: str | None = None
        self._triage_slots = asyncio.Semaphore(2)

    @property
    def pending_fallback_count(self) -> int:
        return len(self._pending_fallbacks)

    def reset_account_state(self) -> None:
        """Discard account-scoped caches when a different Web account is selected."""
        with self._lock:
            for cache in (
                self._handled_msg_ids,
                self._cross_transport_records,
                self._owner_topic_replies,
                self._owner_chat_replies,
                self._owner_parent_replies,
                self._owner_ai_parent_replies,
                self._known_open_chat_ids,
                self._known_chat_names,
                self._known_msg_aliases,
                self._known_sender_aliases,
                self._known_sender_names,
                self._chat_meta_ts,
                self._pending_fallbacks,
            ):
                cache.clear()
            self._last_web_transport = None

    @property
    def needs_patrol_fallback(self) -> bool:
        return bool(self._pending_fallbacks)

    def _mark_collisions(self, target: _CrossTransportRecord | _FallbackRecord) -> None:
        sec = target.created_at_ms // 1000
        if sec <= 0 or not target.content_hashes:
            return
        t_ck = {x for k in target.chat_keys for x in self._chat_keys(k)} | set(target.chat_keys)
        if any(
            getattr(target, a, "").startswith("om_")
            for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
        ):
            return
        for rec in (*self._cross_transport_records.values(), *self._pending_fallbacks.values()):
            if (
                rec is not target
                and not (rec.web_msg_id and rec.web_msg_id == target.web_msg_id)
                and not any(
                    getattr(rec, a, "").startswith("om_")
                    for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
                )
                and (rec.created_at_ms // 1000) == sec
                and bool(rec.content_hashes & target.content_hashes)
                and self._chat_compat(t_ck, target.chat_name, rec)
            ):
                target.ambiguous = rec.ambiguous = True

    def _add_fallback_record(self, item_id: str, web_msg_id: str, kind: str, *args: Any) -> None:
        chat_name, chat_keys, thread_id, sender_id, text, created_at_ms = args[:6]
        a = lambda i, d="": args[i] if len(args) > i else d  # noqa: E731
        alias_id, alt_text = a(6), a(7)
        matched_urls, event, error = a(8, ()), a(9, None), a(10, None)
        hashes = {_content_fingerprint(t) for t in (text, alt_text) if t and t.strip()} - {""}
        tids = {thread_id.strip().casefold()} if thread_id and thread_id.strip() else ()
        sids = {sender_id.strip().casefold()} if sender_id and sender_id.strip() else ()
        with self._lock:
            if error is not None:
                self.delivery_failed_count += 1
                self.last_delivery_error = error
            self.fallback_generation += 1
            base = (
                web_msg_id,
                alias_id or item_id,
                chat_keys,
                chat_name,
                tids,
                sids,
                hashes,
                created_at_ms,
                time.time(),
            )
            ext = (
                False,
                alias_id,
                item_id,
                kind,
                self.fallback_generation,
                text,
                matched_urls,
                event,
            )
            rec = _FallbackRecord(*base, *ext)
            self._mark_collisions(rec)
            self._pending_fallbacks[item_id] = rec

    def fallback_kind_since(self, message_id: str, generation: int) -> str | None:
        with self._lock:
            record = self._pending_fallbacks.get(message_id)
            if record is not None and record.generation > generation:
                return record.kind
        return None

    def _link_pair(self, table: dict[str, set[str]], a: str, b: str) -> None:
        ca, cb = (a or "").strip().casefold(), (b or "").strip().casefold()
        if ca and cb and ca != cb:
            merged = {ca, cb} | table.get(ca, set()) | table.get(cb, set())
            for k in merged:
                table[k] = set(merged)

    def bind_message_alias(self, a: str, b: str) -> None:
        is_u = (a or "").strip().startswith("ou_") or (b or "").strip().startswith("ou_")
        with self._lock:
            self._link_pair(self._known_sender_aliases if is_u else self._known_msg_aliases, a, b)
        if fn := getattr(self._last_web_transport, "bind_message_alias", None):
            fn(a, b)

    def bind_topic_alias(self, a: str, b: str) -> None:
        self.bind_message_alias(a, b)

    def bind_sender_alias(self, a: str, b: str, name: str = "") -> None:
        with self._lock:
            self._link_pair(self._known_sender_aliases, a, b)
            if norm_n := " ".join((name or "").split()):
                for k in (a, b):
                    if ck := (k or "").strip().casefold():
                        self._known_sender_names[ck] = norm_n
        if fn := getattr(self._last_web_transport, "bind_sender_alias", None):
            fn(a, b)

    def _link_chat_pair(self, wid: str, oid: str, cname: str = "") -> None:
        w_cf, o_raw = wid.strip().casefold(), oid.strip()
        keys = {k for k in (w_cf, o_raw.casefold()) if k}
        if len(keys) > 1:
            self._link_pair(self._known_msg_aliases, w_cf, o_raw)
        if o_raw.startswith("oc_"):
            for k in keys:
                self._known_open_chat_ids[k] = o_raw
        if (norm_name := " ".join(cname.split()).casefold()) and keys:
            for old_n, old_s in list(self._known_chat_names.items()):
                if old_n != norm_name:
                    old_s.difference_update(keys)
                    if not old_s:
                        self._known_chat_names.pop(old_n, None)
            self._known_chat_names.setdefault(norm_name, set()).update(keys)

    def _sync_transport_chats(self, web_transport: Any = None) -> None:
        wt = web_transport or self._last_web_transport
        if wt is None:
            return
        self._last_web_transport = wt
        with self._lock:
            for m in (getattr(wt, "_web_to_open_chat", {}), getattr(wt, "_open_to_web_chat", {})):
                for k1, k2 in m.items() if isinstance(m, Mapping) else ():
                    is_oc = str(k1).startswith("oc_")
                    self._link_chat_pair(str(k2 if is_oc else k1), str(k1 if is_oc else k2))
            for c in getattr(wt, "_chat_cache", {}).values():
                self._link_chat_pair(
                    str(getattr(c, "id", "") or ""),
                    str(getattr(c, "open_chat_id", "") or ""),
                    str(getattr(c, "name", "") or ""),
                )
            for (
                attr
            ) in "_open_to_web_msg _web_to_open_msg _open_to_web_user _web_to_open_user".split():
                m_map = getattr(wt, attr, {})
                for k1, k2 in m_map.items() if isinstance(m_map, Mapping) else ():
                    s1, s2 = str(k1).strip(), str(k2).strip()
                    is_u = s1.startswith("ou_") or s2.startswith("ou_")
                    self._link_pair(
                        self._known_sender_aliases if is_u else self._known_msg_aliases, s1, s2
                    )
            for ch in getattr(wt, "_chatter_cache", {}).values():
                if (cid := str(getattr(ch, "id", "") or "").strip().casefold()) and (
                    cnm := " ".join(str(getattr(ch, "name", "") or "").split())
                ):
                    self._known_sender_names[cid] = cnm

    def _expand_alias(
        self, raw: str, table: dict[str, set[str]], fn_names: tuple[str, ...], wt: Any = None
    ) -> set[str]:
        self._sync_transport_chats(wt)
        val, clean = (raw or "").strip(), (raw or "").strip().casefold()
        if not clean:
            return set()
        eff_wt = wt or self._last_web_transport
        for fn_name in fn_names:
            if (fn := getattr(eff_wt, fn_name, None)) and (al := fn(val)):
                with self._lock:
                    self._link_pair(table, val, str(al))
        with self._lock:
            return ({clean} | set(table.get(clean, ()))) - {""}

    def _msg_keys(self, mid: str, web_transport: Any = None) -> set[str]:
        return self._expand_alias(
            mid, self._known_msg_aliases, ("get_message_alias",), web_transport
        )

    def _sender_keys(self, sid: str, web_transport: Any = None) -> set[str]:
        return self._expand_alias(
            sid,
            self._known_sender_aliases,
            ("get_sender_alias", "get_message_alias"),
            web_transport,
        )

    def _sender_name_for(
        self, rec: _CrossTransportRecord | _FallbackRecord, web_transport: Any = None
    ) -> str:
        self._sync_transport_chats(web_transport)
        with self._lock:
            for s in rec.sender_ids:
                for sk in {s.casefold()} | set(self._known_sender_aliases.get(s.casefold(), ())):
                    if nm := self._known_sender_names.get(sk):
                        return nm.casefold()
        return ""

    def expand_chat_keys(self, chat_id: str) -> set[str]:
        return self._chat_keys(chat_id)

    def watched_openapi_chat_ids(
        self, cfg: FeishuRealtimeEventConfig | None = None
    ) -> tuple[str, ...]:
        eff_cfg = cfg or self.resolve_config()
        self._sync_transport_chats()
        with self._lock:
            for c in (r.strip() for r in eff_cfg.watch_chat_ids if r.strip().startswith("oc_")):
                self._known_open_chat_ids.setdefault(c.casefold(), c)
            fb_ck = {
                ck
                for r in self._pending_fallbacks.values()
                if r.kind == "failed_delivery" and r.matched_urls
                for ck in r.chat_keys
            }
            nm_ck = {
                ck for n in eff_cfg.watch_chat_names for ck in self._known_chat_names.get(n, ())
            }
            keys = {
                x for s in (set(eff_cfg.watch_chat_ids) | nm_ck | fb_ck) for x in self._chat_keys(s)
            }
            oc_ids = {self._known_open_chat_ids.get(ck, ck) for ck in keys if ck.startswith("oc_")}
            return tuple(sorted(oc_ids))

    def _chat_compat(
        self, cand_ck: set[str], cand_name: str, rec: _CrossTransportRecord | _FallbackRecord
    ) -> bool:
        rec_ck = {x for k in rec.chat_keys for x in self._chat_keys(k)} | set(rec.chat_keys)
        if cand_ck & rec_ck:
            return True
        if (
            any(k.startswith("oc_") for k in cand_ck) and any(k.startswith("oc_") for k in rec_ck)
        ) or (
            any(is_web_entity_id(k) for k in cand_ck) and any(is_web_entity_id(k) for k in rec_ck)
        ):
            return False
        cn = " ".join((cand_name or "").split()).casefold()
        rn = " ".join((rec.chat_name or "").split()).casefold()
        if cn and rn:
            return cn == rn
        if getattr(rec, "kind", "") == "failed_delivery" and getattr(rec, "matched_urls", ()):
            cfg = self.resolve_config()
            w_ids = {c.strip().casefold() for c in cfg.watch_chat_ids if c and c.strip()}
            return bool((cand_ck & w_ids) or (cn and cn in cfg.watch_chat_names))
        return False

    def matches_pending_failed_watch_delivery(
        self, *, message_id: str = "", chat_ids: set[str] | None = None, chat_name: str = ""
    ) -> bool:
        clean_mid = (message_id or "").strip()
        if not clean_mid or not chat_ids:
            return False
        with self._lock:
            return any(
                rec.kind == "failed_delivery"
                and rec.matched_urls
                and bool(mids := self._rec_mkeys(rec))
                and (
                    bool(self._msg_keys(clean_mid) & mids)
                    or (
                        clean_mid.startswith("om_")
                        and any(is_web_entity_id(m) for m in mids)
                        and not any(m.startswith("om_") and m != clean_mid.casefold() for m in mids)
                    )
                )
                and self._chat_compat(chat_ids, chat_name, rec)
                for rec in self._pending_fallbacks.values()
            )

    def _matches_candidate_obj(
        self, rec: _CrossTransportRecord | _FallbackRecord, cand: Any
    ) -> bool:
        message_id = str(getattr(cand, "message_id", "") or "").strip()
        return bool(message_id and self._msg_keys(message_id) & self._rec_mkeys(rec))

    def _rec_mkeys(self, r: _CrossTransportRecord | _FallbackRecord) -> set[str]:
        return {
            x
            for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
            if (m := getattr(r, a, ""))
            for x in self._msg_keys(m)
        }

    def is_pending_failed_watch_candidate(self, candidate: Any) -> bool:
        cmid = str(getattr(candidate, "message_id", "") or "").strip()
        with self._lock:
            return any(
                r.kind == "failed_delivery"
                and r.matched_urls
                and (
                    (cmid and bool(self._msg_keys(cmid) & self._rec_mkeys(r)))
                    or self._matches_candidate_obj(r, candidate)
                )
                for r in self._pending_fallbacks.values()
            )

    def note_candidate_batch(self, candidates: Sequence[Any]) -> None:
        # CLI and Web recovery use separate transports, never content guessing.
        pass

    def acknowledge_patrol_fallback(
        self, *, recovered_ids: Sequence[str] | None = None, **kw: Any
    ) -> None:
        rec_cands, seen_ids, seen_cands = (
            kw.get("recovered_candidates") or (),
            kw.get("seen_ids") or (),
            kw.get("seen_candidates") or (),
        )
        w_start, w_end = kw.get("window_start_ms"), kw.get("window_end_ms")
        max_gen, all_done = kw.get("max_generation"), bool(kw.get("all_candidates_completed", True))
        with self._lock:
            if (
                recovered_ids is None
                and not (rec_cands or seen_ids or seen_cands)
                and w_start is None
            ):
                self._pending_fallbacks.clear()
                self.last_delivery_error = None
                return
            rec_id_set = {str(mid).strip() for mid in (recovered_ids or ()) if str(mid).strip()}
            seen_id_set = {str(mid).strip() for mid in seen_ids if str(mid).strip()} | rec_id_set
            self.note_candidate_batch([*rec_cands, *seen_cands])
            for key, rec in list(self._pending_fallbacks.items()):
                out_w = (
                    w_start is not None
                    and w_end is not None
                    and rec.created_at_ms > 0
                    and not (w_start <= rec.created_at_ms <= w_end)
                )
                if (max_gen is not None and rec.generation > max_gen) or out_w:
                    continue
                r_ids = {rec.item_id, rec.web_msg_id, rec.alias_id} - {""}
                direct = bool(r_ids & rec_id_set)
                alias = rec.alias_id or (rec.item_id if rec.item_id.startswith("om_") else "")
                sids, tids, cks = set(rec.sender_ids), set(rec.thread_ids), set(rec.chat_keys)
                for cand in (() if direct else rec_cands):
                    cmid = str(getattr(cand, "message_id", "") or "").strip()
                    if (
                        not cmid or not rec_id_set or cmid in rec_id_set
                    ) and self._matches_candidate_obj(rec, cand):
                        direct, alias = True, cmid or alias
                        sids |= (
                            {s.strip().casefold()}
                            if (s := str(getattr(cand, "sender_id", "") or ""))
                            else set()
                        )
                        tids |= (
                            {t.strip().casefold()}
                            if (t := str(getattr(cand, "thread_id", "") or ""))
                            else set()
                        )
                        cks |= (
                            self._chat_keys(c)
                            if (c := str(getattr(cand, "chat_id", "") or ""))
                            else set()
                        )
                        break
                if direct:
                    self._record_handled(
                        rec.web_msg_id or rec.item_id,
                        alias,
                        cks,
                        rec.chat_name,
                        tids,
                        sids,
                        rec.text,
                        "",
                        rec.created_at_ms,
                    )
                    self._pending_fallbacks.pop(key, None)
                elif (
                    rec.kind == "uncovered_message"
                    and all_done
                    and (
                        bool(r_ids & seen_id_set)
                        or any(self._matches_candidate_obj(rec, c) for c in seen_cands)
                    )
                ):
                    self._pending_fallbacks.pop(key, None)
            if not self._pending_fallbacks:
                self.last_delivery_error = None

    def resolve_config(self) -> FeishuRealtimeEventConfig:
        return self._explicit_config or FeishuRealtimeEventConfig.from_runtime()

    def _chat_keys(self, chat_id: str, web_transport: Any = None) -> set[str]:
        self._sync_transport_chats(web_transport)
        raw = (chat_id or "").strip()
        clean = raw.casefold()
        if not clean:
            return set()
        with self._lock:
            if raw.startswith("oc_"):
                self._known_open_chat_ids.setdefault(clean, raw)
            return ({clean} | set(self._known_msg_aliases.get(clean, ()))) - {""}

    def _record_handled(
        self, message_id: str, alias_id: str | None = None, *args: Any, **kw: Any
    ) -> None:
        arg = lambda i, k, d=None: args[i] if len(args) > i else kw.get(k, d)  # noqa: E731
        chat_keys, chat_name = arg(0, "chat_keys"), str(arg(1, "chat_name", "") or "")
        thread_ids, sender_ids = arg(2, "thread_ids"), arg(3, "sender_ids")
        text, alt_text = str(arg(4, "text", "") or ""), str(arg(5, "alt_text", "") or "")
        ms, tid = int(arg(6, "created_at_ms", 0) or 0), str(kw.get("thread_id") or "")
        now, cmid, calias = time.time(), (message_id or "").strip(), (alias_id or "").strip()
        raw_t = ({thread_ids} if isinstance(thread_ids, str) else set(thread_ids or ())) | (
            {tid} if tid else set()
        )
        n_tids = {t.strip().casefold() for t in raw_t if t and t.strip()}
        n_sids = {s.strip().casefold() for s in (sender_ids or set()) if s and s.strip()}
        hashes = {_content_fingerprint(t) for t in (text, alt_text) if t and t.strip()} - {""}
        with self._lock:
            for mid in (cmid, calias):
                if mid:
                    self._handled_msg_ids[mid] = now
                    self._handled_msg_ids.move_to_end(mid)
                    self._pending_fallbacks.pop(mid, None)
            if cmid and hashes:
                rec = _CrossTransportRecord(
                    cmid, calias or cmid, chat_keys, chat_name, n_tids, n_sids, hashes, ms, now
                )
                self._mark_collisions(rec)
                self._cross_transport_records[cmid] = rec
                self._cross_transport_records.move_to_end(cmid)
            for store in (self._handled_msg_ids, self._cross_transport_records):
                while len(store) > _MAX_HANDLED_LEDGER:
                    store.popitem(last=False)

    def was_dispatched(self, message_id: str) -> bool:
        return self.is_message_handled(message_id)

    def is_message_handled(self, message_id: str, **kw: Any) -> bool:
        clean_id = (message_id or "").strip()
        if not clean_id:
            return False
        with self._lock:
            return clean_id in self._handled_msg_ids or any(
                key in self._handled_msg_ids for key in self._msg_keys(clean_id)
            )

    def should_suppress_openapi_mention(self, message: Mapping[str, Any], own_id: str) -> bool:
        def g(*ks: str) -> str:
            return next((str(message.get(k) or "").strip() for k in ks if message.get(k)), "")

        mid, cid, tid, rto = (
            g("message_id"),
            g("chat_id"),
            g("thread_id", "root_id"),
            g("reply_to", "parent_id"),
        )
        raw = message.get("content", message.get("text", ""))
        txt = (
            str(raw.get("text") or raw.get("content") or "")
            if isinstance(raw, Mapping)
            else str(raw or "")
        )
        ms, oids = _parse_created_ms(message.get("create_time")), (
            {own_id.strip()} if own_id and own_id.strip() else None
        )
        return bool(
            is_courtesy_or_ack_only(txt)
            or self.is_message_handled(mid, chat_id=cid, thread_id=tid, text=txt, created_at_ms=ms)
            or self.has_owner_replied(
                chat_id=cid,
                thread_id=tid,
                reply_to=rto,
                message_id=mid,
                created_at_ms=ms,
                owner_ids=oids,
            )
        )

    def record_owner_pushed_message(
        self, pushed: PushedMessage, user_msg: FeishuUserMessage, *, web_transport: Any = None
    ) -> None:
        msg = pushed.message
        chat_keys = self._chat_keys(msg.chat_id or user_msg.chat_id, web_transport)
        topic_id = (
            user_msg.thread_id or (msg.thread_id or msg.root_id if is_in_topic(msg) else "") or ""
        ).strip()
        mark = _OwnerReplyMark(
            max(user_msg.create_time_ms, msg.create_time_ms),
            msg.position,
            int(getattr(msg, "topic_position", None) or 0),
            user_msg.message_id,
        )
        is_ai_command = user_msg.text.strip().lower().startswith("/ai")

        def _put_mark(store: OrderedDict[Any, _OwnerReplyMark], k: Any) -> None:
            if store.get(k) is None or _reply_mark_is_newer_or_equal(mark, store[k]):
                store[k] = mark
                store.move_to_end(k)
                while len(store) > _MAX_REPLY_TRACKER:
                    store.popitem(last=False)

        with self._lock:
            if is_ai_command:
                parent = (msg.parent_id or user_msg.parent_id or "").strip()
                if parent:
                    for pk in (
                        {parent, parent.casefold()} | self._msg_keys(parent, web_transport)
                    ) - {""}:
                        _put_mark(self._owner_ai_parent_replies, pk)
                return
            t_keys = ({topic_id, topic_id.casefold()} | self._msg_keys(topic_id, web_transport)) - {
                ""
            }
            for ck in chat_keys:
                for tk in t_keys if topic_id else ("",):
                    _put_mark(
                        self._owner_topic_replies if topic_id else self._owner_chat_replies,
                        (ck, tk) if topic_id else ck,
                    )
            prefs = {
                msg.parent_id.strip(),
                msg.root_id.strip(),
                (user_msg.parent_id or "").strip(),
                (user_msg.root_id or "").strip(),
                topic_id,
            } - {""}
            for p in prefs:
                for pk in ({p, p.casefold()} | self._msg_keys(p, web_transport)) - {""}:
                    _put_mark(self._owner_parent_replies, pk)

    def has_owner_replied(self, *, chat_id: str, **kw: Any) -> bool:
        chat_keys = self._chat_keys(chat_id, kw.get("web_transport"))
        owner_ids, wt = set(kw.get("owner_ids") or ()), kw.get("web_transport")
        clean_tid = str(kw.get("thread_id") or "").strip()
        clean_mid, clean_rto = (
            str(kw.get("message_id") or "").strip(),
            str(kw.get("reply_to") or "").strip(),
        )
        ms, pos, tpos = (
            int(kw.get("created_at_ms") or 0),
            int(kw.get("position") or 0),
            int(kw.get("topic_position") or 0),
        )
        kset = lambda s: ({s, s.casefold()} | self._msg_keys(s, wt)) - {""}  # noqa: E731
        mid_keys, rto_keys, tid_keys = kset(clean_mid), kset(clean_rto), kset(clean_tid)

        def _m_newer(mark: _OwnerReplyMark, da: bool = False) -> bool:
            ok_id = not mark.message_id or mark.message_id.casefold() not in mid_keys
            return ok_id and _is_newer_reply(
                mark.create_time_ms, mark.position, mark.topic_position, ms, pos, tpos, da
            )

        with self._lock:
            if any(
                mk in self._owner_ai_parent_replies
                and _m_newer(self._owner_ai_parent_replies[mk], True)
                for mk in mid_keys
            ):
                return True
            if any(
                mk in self._owner_parent_replies and _m_newer(self._owner_parent_replies[mk], da)
                for ks, da in ((mid_keys, True), (rto_keys, False))
                for mk in ks
            ):
                return True
            for ck in chat_keys:
                marks = (
                    (self._owner_topic_replies.get((ck, tk)) for tk in tid_keys)
                    if clean_tid
                    else (self._owner_chat_replies.get(ck),)
                )
                if any(m is not None and _m_newer(m) for m in marks):
                    return True
        msg_cache = getattr(wt, "_msg_cache", None) if owner_ids else None
        for c in msg_cache.values() if isinstance(msg_cache, Mapping) and owner_ids else ():
            is_ai = isinstance(c, WebMessage) and describe(
                c.type, c.content, ""
            ).strip().lower().startswith("/ai")
            if (
                not isinstance(c, WebMessage)
                or c.recalled
                or not c.id
                or c.id == clean_mid
                or c.from_id not in owner_ids
                or (is_ai and c.parent_id != clean_mid)
                or not (chat_keys & self._chat_keys(c.chat_id, wt))
            ):
                continue
            direct = c.parent_id == clean_mid or c.root_id == clean_mid
            same_p = bool(clean_rto) and c.parent_id == clean_rto
            ctopic = (c.thread_id or c.root_id if is_in_topic(c) else "").strip()
            ctpos = int(getattr(c, "topic_position", None) or 0)
            in_scope = (
                direct
                or same_p
                or (bool(clean_tid) and ctopic == clean_tid)
                or (not clean_tid and not ctopic)
            )
            if in_scope and _is_newer_reply(
                c.create_time_ms, c.position, ctpos, ms, pos, tpos, direct
            ):
                return True
        return False

    def owner_active_in_p2p(
        self,
        chat_id: str,
        window_seconds: float,
        *,
        web_transport: Any = None,
        owner_ids: set[str] | None = None,
    ) -> bool:
        if window_seconds <= 0:
            return False
        keys = self._chat_keys(chat_id, web_transport)
        now_ms = time.time() * 1000
        with self._lock:
            times = [
                mark.create_time_ms for key, mark in self._owner_chat_replies.items() if key in keys
            ]
            times.extend(
                mark.create_time_ms
                for (key, _), mark in self._owner_topic_replies.items()
                if key in keys
            )
        # Warm Web cache covers owner messages from before process startup.
        for msg in getattr(web_transport, "_msg_cache", {}).values():
            if (
                isinstance(msg, WebMessage)
                and not msg.recalled
                and msg.from_id in (owner_ids or set())
                and self._chat_keys(msg.chat_id, web_transport) & keys
                and not describe(msg.type, msg.content, "").strip().lower().startswith("/ai")
            ):
                times.append(msg.create_time_ms)
        return any(0 <= now_ms - stamp < window_seconds * 1000 for stamp in times)

    async def _verify_unanswered_via_client(
        self, *, user_client: Any, chat_id: str, **kw: Any
    ) -> bool:
        tid, mid = str(kw.get("thread_id") or ""), str(kw.get("message_id") or "")
        ms, pos, tpos = (
            int(kw.get("created_at_ms") or 0),
            int(kw.get("position") or 0),
            int(kw.get("topic_position") or 0),
        )
        wt, owner_ids, cid = kw.get("web_transport"), kw.get("owner_ids") or set(), tid or chat_id
        if not cid or not hasattr(user_client, "list_messages_page"):
            return False
        try:
            async with asyncio.timeout(2.5):
                ctype = "thread" if tid else "chat"
                page = await user_client.list_messages_page(
                    container_id_type=ctype,
                    container_id=cid,
                    sort_type="ByCreateTimeDesc",
                    page_size=20,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        msg_cache = getattr(wt, "_msg_cache", None)
        for item in getattr(page, "items", ()):
            if (
                not isinstance(item, FeishuUserMessage)
                or item.message_id == mid
                or item.sender_open_id not in owner_ids
                or (item.text.strip().lower().startswith("/ai") and item.parent_id != mid)
            ):
                continue
            active_seconds = float(kw.get("active_seconds") or 0)
            if (
                active_seconds > 0
                and not item.text.strip().lower().startswith("/ai")
                and 0 <= time.time() * 1000 - item.create_time_ms < active_seconds * 1000
            ):
                return True
            direct = item.parent_id == mid or item.root_id == mid
            wm = msg_cache.get(item.message_id) if isinstance(msg_cache, Mapping) else None
            wpos = wm.position if wm is not None else 0
            wtpos = int(getattr(wm, "topic_position", None) or 0) if wm is not None else 0
            in_s = (
                direct
                or (bool(tid) and (item.thread_id == tid or item.root_id == tid))
                or (not tid and not item.thread_id)
            )
            if in_s and _is_newer_reply(item.create_time_ms, wpos, wtpos, ms, pos, tpos, direct):
                return True
        return False

    async def _ensure_chat_metadata(
        self, web_chat_id: str, web_transport: Any, *, refresh: bool = False
    ) -> tuple[str, str, Any]:
        if web_transport is None or not web_chat_id:
            return "", "", None
        chat_cache, open_map = getattr(web_transport, "_chat_cache", {}), getattr(
            web_transport, "_web_to_open_chat", {}
        )
        cached = chat_cache.get(web_chat_id) if isinstance(chat_cache, Mapping) else None
        open_chat_id = str(
            open_map.get(web_chat_id) or getattr(cached, "open_chat_id", "") or ""
        ).strip()
        chat_name = str(getattr(cached, "name", "") or "").strip()
        web_client = getattr(web_transport, "web_client", None) or getattr(
            web_transport, "_web", None
        )
        now_m = time.monotonic()
        stale = (now_m - self._chat_meta_ts.get(web_chat_id, 0.0)) >= 60.0
        need_fetch = (
            cached is None
            or refresh
            or stale
            or (
                getattr(cached, "type", None) != _rich.CHAT_P2P
                and (not open_chat_id or not chat_name)
            )
        )
        if need_fetch and web_client is not None and hasattr(web_client, "chats"):
            try:
                async with asyncio.timeout(2.5):
                    chats_map = await web_client.chats([web_chat_id])
                fetched = chats_map.get(web_chat_id) if isinstance(chats_map, Mapping) else None
                if fetched is not None:
                    cached = fetched
                    self._chat_meta_ts[web_chat_id] = now_m
                    if hasattr(web_transport, "_remember_chat"):
                        web_transport._remember_chat(fetched)
                    open_chat_id = str(getattr(fetched, "open_chat_id", "") or open_chat_id)
                    chat_name = str(getattr(fetched, "name", "") or chat_name)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if open_chat_id or chat_name:
            with self._lock:
                self._link_chat_pair(web_chat_id, open_chat_id, chat_name)
        return open_chat_id, chat_name, cached

    def _deliver_event_res(self, res: Any) -> bool:
        if isinstance(res, bool):
            return res
        return (
            res.get("status") in {"accepted", "duplicate", True}
            if isinstance(res, Mapping)
            else True
        )

    async def _deliver_event(
        self,
        event: ExternalEvent,
        cfg: FeishuRealtimeEventConfig,
        *,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
    ) -> bool:
        if self._event_sink is not None:
            return self._deliver_event_res(await self._event_sink(event))
        from claude_hub.services.feishu_realtime_delivery import deliver_realtime_triage

        async with self._triage_slots:
            return await deliver_realtime_triage(
                event,
                cfg,
                **({"attachments": attachments} if attachments else {}),
                still_unanswered=lambda: not self.has_owner_replied(
                    chat_id=str(
                        event.payload.get("web_chat_id") or event.payload.get("chat_id") or ""
                    ),
                    thread_id=str(event.payload.get("thread_id") or ""),
                    reply_to=str(event.payload.get("reply_to") or ""),
                    message_id=event.dedupe_key,
                    created_at_ms=int(str(event.payload.get("created_at_ms") or 0)),
                    position=int(str(event.payload.get("position") or 0)),
                    topic_position=int(str(event.payload.get("topic_position") or 0)),
                ),
            )

    async def handle_pushed_message(
        self, pushed: PushedMessage, user_msg: FeishuUserMessage, **kw: Any
    ) -> bool:
        msg, wt, uc = pushed.message, kw.get("web_transport"), kw.get("user_client")
        if pushed.removed or msg.recalled or msg.type == _rich.TYPE_SYSTEM:
            return False
        eff_oids = {o.strip() for o in (kw.get("owner_ids") or set()) if o and o.strip()}
        if (
            kw.get("is_owner_sender")
            or msg.from_id in eff_oids
            or user_msg.sender_open_id in eff_oids
        ):
            self.record_owner_pushed_message(pushed, user_msg, web_transport=wt)
            return False
        cfg, wcid = self.resolve_config(), (msg.chat_id or user_msg.chat_id).strip()
        is_bot = (
            user_msg.sender_type.casefold() in {"app", "bot"}
            or user_msg.sender_open_id.startswith(BOT_SENDER_PREFIXES)
            or msg.from_id.startswith(BOT_SENDER_PREFIXES)
        )
        if not cfg.enabled or not wcid or is_bot:
            return False
        m_ids = (
            *extract_mentioned_user_ids(msg.type, msg.content),
            *getattr(msg, "at_user_ids", ()),
        )
        ftxt = describe(msg.type, msg.content, "").strip() or user_msg.text.strip()
        if has_ai_reply_footer(ftxt) or has_ai_reply_footer(user_msg.text):
            return False
        batch: list[FeishuUserMessage] = kw.get("coalesced_messages") or [user_msg]
        if len(batch) > 1:
            ftxt = "\n".join(item.text for item in batch)
        urls = extract_message_urls(msg.type, msg.content, ftxt)
        is_at_me = bool(eff_oids.intersection(m_ids)) or (
            bool(pushed.at_me) and not (bool(m_ids) and set(m_ids) <= {"all", "@all"})
        )
        m_urls = (
            [u for u in urls if matches_fault_or_task_url(u, cfg)]
            if urls and (cfg.url_patterns or cfg.actionable_hosts)
            else []
        )
        tid = (
            user_msg.thread_id or (msg.thread_id or msg.root_id if is_in_topic(msg) else "") or ""
        ).strip()
        cks, ocid, cname = self._chat_keys(wcid, wt), "", ""
        chat_cache = getattr(wt, "_chat_cache", {})
        chat_meta = chat_cache.get(wcid) if isinstance(chat_cache, Mapping) else None
        is_p2p = pushed.chat_type == _rich.CHAT_P2P or (
            not pushed.chat_type and getattr(chat_meta, "type", None) == _rich.CHAT_P2P
        )
        if is_at_me or m_urls or is_p2p or tid:
            w_cf = {c.strip().casefold() for c in cfg.watch_chat_ids if c and c.strip()}
            cached_nm = " ".join(str(getattr(chat_meta, "name", "") or "").split()).casefold()
            ref = bool(
                m_urls
                and cfg.watch_chat_names
                and not (cks & w_cf)
                and cached_nm not in cfg.watch_chat_names
            )
            ocid, cname, chat_meta = await self._ensure_chat_metadata(wcid, wt, refresh=ref)
            if ocid:
                cks |= self._chat_keys(ocid, wt)
        is_p2p = is_p2p or (
            not pushed.chat_type and getattr(chat_meta, "type", None) == _rich.CHAT_P2P
        )
        if cks & cfg.automated_chat_ids or bool(getattr(chat_meta, "bot", False)):
            return False
        m_urls = (
            matches_watch_group_url(
                wcid, cname, ftxt, cfg, extra_urls=urls, extra_chat_ids=[ocid] if ocid else ()
            )
            if m_urls
            else []
        )
        is_w_url = bool(m_urls)
        fb_base = (cname, cks, tid, user_msg.sender_open_id, ftxt, user_msg.create_time_ms)
        in_topic = False
        if tid:
            with self._lock:
                marks = [self._owner_topic_replies.get((ck, tid)) for ck in cks]
            in_topic = any(
                mark is not None
                and _is_newer_reply(
                    user_msg.create_time_ms,
                    msg.position,
                    int(msg.topic_position or 0),
                    mark.create_time_ms,
                    mark.position,
                    mark.topic_position,
                )
                for mark in marks
            )
        if (
            tid
            and not (is_at_me or is_w_url or is_p2p or in_topic)
            and not is_courtesy_or_ack_only(ftxt)
        ):
            try:
                in_topic = await owner_participated(
                    uc,
                    tid,
                    eff_oids,
                    user_msg.create_time_ms,
                    web_transport=wt,
                    topic_position=msg.topic_position,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._add_fallback_record(
                    user_msg.message_id, user_msg.message_id, "uncovered_message", *fb_base
                )
                return False
        if not (is_at_me or is_w_url or is_p2p or in_topic):
            return False
        sender_keys = self._sender_keys(msg.from_id, wt) | self._sender_keys(
            user_msg.sender_open_id, wt
        )
        if await asyncio.to_thread(is_pool_bot_message, sender_keys, cks, is_p2p=is_p2p):
            return False
        sender_id = msg.from_id or user_msg.sender_open_id
        automated_sender = await is_automated_web_sender(wt, sender_id, is_p2p=is_p2p)
        if automated_sender is None:
            self._add_fallback_record(
                user_msg.message_id, user_msg.message_id, "sender_verification_pending", *fb_base
            )
            return False
        if automated_sender:
            return False
        self._sync_transport_chats(wt)
        with self._lock:
            sender_name = next(
                (
                    self._known_sender_names[key]
                    for key in sorted(sender_keys)
                    if self._known_sender_names.get(key)
                ),
                "",
            )
        # Web DM metadata often omits the chat label; the incoming human is its peer.
        if is_p2p and not cname:
            cname = sender_name
        # Web replay and push share the same numeric ID; changing the event key
        # when an OpenAPI alias happens to be known would break durable dedupe.
        cmid = user_msg.message_id
        s_set = {
            s.strip().casefold()
            for s in (user_msg.sender_open_id, msg.from_id)
            if isinstance(s, str) and s.strip()
        }
        ms = user_msg.create_time_ms
        chk_kw: dict[str, Any] = dict(chat_name=cname, thread_id=tid, text=ftxt, created_at_ms=ms)
        from .feishu_realtime_delivery import has_pending_realtime_followup

        followup_due = await has_pending_realtime_followup(cmid)
        if self.is_message_handled(
            cmid, chat_id=ocid or wcid, sender_id=user_msg.sender_open_id, **chk_kw
        ) or self.is_message_handled(
            user_msg.message_id,
            chat_id=wcid,
            sender_id=msg.from_id or user_msg.sender_open_id,
            **chk_kw,
        ):
            if not followup_due:
                return False
        rec_args = (cks, cname, {tid} if tid else None, s_set, ftxt, user_msg.text, ms)

        async def _suppress(courtesy: bool) -> bool:
            self.suppressed_count += 1
            self.suppressed_courtesy_count += int(courtesy)
            self.suppressed_replied_count += int(not courtesy)
            self._record_handled(user_msg.message_id, cmid, *rec_args)
            for sup_key in {item.message_id for item in batch} | {cmid}:
                try:
                    await record_suppressed_external_event("feishu", sup_key[:256])
                except Exception:
                    pass
            return False

        if not followup_due and not is_w_url and is_courtesy_or_ack_only(ftxt):
            return await _suppress(courtesy=True)
        tpos = int(getattr(msg, "topic_position", None) or 0)
        r_kw: dict[str, Any] = dict(
            chat_id=wcid, thread_id=tid, message_id=user_msg.message_id, created_at_ms=ms
        )
        r_kw.update(
            position=msg.position, topic_position=tpos, web_transport=wt, owner_ids=eff_oids
        )
        if not followup_due and (
            (
                is_p2p
                and self.owner_active_in_p2p(
                    wcid, cfg.p2p_active_seconds, web_transport=wt, owner_ids=eff_oids
                )
            )
            or self.has_owner_replied(reply_to=msg.parent_id or msg.root_id, **r_kw)
            or await self._verify_unanswered_via_client(
                user_client=uc, active_seconds=cfg.p2p_active_seconds if is_p2p else 0, **r_kw
            )
        ):
            if not followup_due:
                return await _suppress(courtesy=False)
        parsed_url: HttpUrl | None = None
        for u in m_urls or urls:
            try:
                parsed_url = HttpUrl(u)
                break
            except Exception:
                pass
        clbl, snd = (cname or ocid or wcid)[:120], user_msg.sender_open_id[:80]
        parts = [p for p in (clbl, snd, ftxt[:600]) if p]
        keys = "chat_id web_chat_id chat_name chat_type message_id web_message_id thread_id sender_id text trigger_reason matched_urls".split()
        ctype = "p2p" if is_p2p else "group"
        trig = (
            "mention"
            if is_at_me
            else "watch_chat_url" if is_w_url else "p2p" if is_p2p else "topic_reply"
        )
        u_slice = [u[:400] for u in m_urls[:5]]
        pl: dict[str, Any] = dict(
            zip(keys[:6], (ocid or wcid, wcid, cname[:200], ctype, cmid, user_msg.message_id))
        )
        pl.update(
            zip(keys[6:10], (tid, user_msg.sender_open_id[:120], ftxt[:12000], trig)),
            matched_urls=u_slice,
            sender_name=sender_name[:200],
            coalesced_message_ids=[item.message_id for item in batch],
            created_at_ms=ms,
            reply_to=msg.parent_id or msg.root_id,
            position=msg.position,
            topic_position=tpos,
        )
        ev_t = f"im.{trig}"
        ev_ttl = {
            "mention": "Feishu mention",
            "watch_chat_url": "Feishu watch-group issue link",
            "p2p": "Feishu direct message",
            "topic_reply": "Feishu topic reply",
        }[trig]
        ev_kw: dict[str, Any] = dict(
            source="feishu", event_type=ev_t, dedupe_key=cmid[:256], severity="normal"
        )
        ev_kw.update(title=ev_ttl, summary=" · ".join(parts)[:1000], url=parsed_url, payload=pl)
        event = ExternalEvent(**ev_kw)
        if not self.resolve_config().enabled:
            return False
        delivery_err: str | None = None
        try:
            attachments = None
            if self._event_sink is None and any(
                item.has_images or item.image_refs for item in batch
            ):
                from .feishu_ai_images import prepare_images

                try:
                    images = []
                    for item in batch:
                        if item.has_images or item.image_refs:
                            prepared, _ = await prepare_images(uc, item, [])
                            images.extend(prepared)
                except Exception:
                    logger.warning("Realtime image acquisition failed for %s", cmid, exc_info=True)
                    event.payload["image_unavailable"] = True
                else:
                    attachments = [
                        WorkspaceAttachmentCreate(
                            filename=i.filename, mime_type=i.mime_type, data_url=i.data_url
                        )
                        for i in images
                    ]
            if not await self._deliver_event(
                event, cfg, **({"attachments": attachments} if attachments else {})
            ):
                delivery_err = "rejected"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivery_err = str(exc) or type(exc).__name__
            logger.warning("Feishu real-time event delivery failed for %s", cmid, exc_info=True)
        if delivery_err is not None:
            fb_tail = (
                cmid if cmid != user_msg.message_id else "",
                user_msg.text,
                m_urls,
                event,
                delivery_err,
            )
            self._add_fallback_record(
                cmid, user_msg.message_id, "failed_delivery", *fb_base, *fb_tail
            )
            return False
        for item in batch:
            if item.message_id != cmid:
                await record_suppressed_external_event("feishu", item.message_id)
                self._record_handled(item.message_id, item.message_id, *rec_args)
        self.emitted_count += 1
        self._record_handled(user_msg.message_id, cmid, *rec_args)
        return True


def get_feishu_realtime_event_router() -> FeishuRealtimeEventRouter:
    global _DEFAULT_REALTIME_ROUTER
    if _DEFAULT_REALTIME_ROUTER is None:
        _DEFAULT_REALTIME_ROUTER = FeishuRealtimeEventRouter()
    return _DEFAULT_REALTIME_ROUTER


def is_feishu_realtime_stream_healthy() -> bool:
    from claude_hub.services.feishu_ai_poller import _DEFAULT_AI_POLLER

    return bool(getattr(_DEFAULT_AI_POLLER, "realtime_stream_healthy", False))
