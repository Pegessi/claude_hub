"""Feishu personal ``/ai <instruction>`` command pipeline and configuration store."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from claude_hub.config import settings
from claude_hub.models import AgentType, SessionKind
from claude_hub.services.agent_stream.providers.spec import (
    ReasoningEffortKind,
    get_spec,
)
from claude_hub.services.feishu_bot import (
    FeishuMessageDedupStore,
    FeishuMessageEvent,
    FeishuReplyReceipt,
)
from claude_hub.services.feishu_message_format import (
    FEISHU_MESSAGE_MAX_CHARS,
    build_feishu_post_content,
)
from claude_hub.services.feishu_user_client import (
    FeishuScopePreflightResult,
    FeishuUserClient,
    FeishuUserMessage,
    FeishuUserScopeError,
)
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenStore,
    get_feishu_user_token_store,
)
from claude_hub.services.local_storage import (
    document_exists,
    read_document,
    write_document,
)
from claude_hub.services.runtime_isolation import resolve_runtime_home
from claude_hub.services.terminal_runtime.defaults import DEFAULT_CLAUDE_LAUNCH_ENV

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
DEFAULT_AI_COMMAND_PREFIX = "/ai"
DEFAULT_AI_COMMAND_ACK_EMOJI = "OnIt"
DEFAULT_AI_COMMAND_DONE_EMOJI = "DONE"
DEFAULT_AI_COMMAND_FOOTER = "> 🤖 AI 代答，仅供参考"
DEFAULT_AI_COMMAND_PROVIDER = AgentType.CODEX
DEFAULT_AI_COMMAND_MODEL = "gpt-5.4"
DEFAULT_AI_COMMAND_REASONING_EFFORT = "high"
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_MAX_ACTIVE_CHATS = 10
DEFAULT_MAX_CONTEXT_MESSAGES = 20
_REACTION_TIMEOUT_SECONDS = 3.0
_MAX_RECENT_RUNS = 30
_MAX_PENDING_QUEUE = 256

SUPPORTED_AI_COMMAND_PROVIDERS: tuple[AgentType, ...] = tuple(
    AgentType(x) for x in "claude codex traex maso cursor antigravity".split()
)
SUPPORTED_REASONING_EFFORTS: tuple[str, ...] = (
    "",
    *"none minimal low medium high xhigh max ultra".split(),
)
PROVIDER_SUPPORTED_REASONING_EFFORTS: dict[str, tuple[str, ...]] = {
    "claude": (),
    "codex": tuple("none minimal low medium high xhigh".split()),
    "traex": tuple("none minimal low medium high xhigh".split()),
    "maso": tuple("none low medium high xhigh max".split()),
    "cursor": tuple("none minimal low medium high xhigh max".split()),
    "antigravity": (),
}
DEFAULT_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("doubao-seed-2.0-code", "claude-sonnet-4-6", "claude-opus-4-8"),
    "codex": ("gpt-5.4", "gpt-5.6-sol", "gpt-5.3-codex", "o3"),
    "traex": ("gpt-5.4", "Seed-2.1-Pro", "doubao-seed-2.0-code"),
    "maso": tuple(
        "gemini-4-argon_reasoning_high gemini-4-argon_reasoning_medium "
        "gemini-4-argon gpt-6-astra_reasoning_high seed-stable-reasoning-high".split()
    ),
    "cursor": (
        "gpt-5.3-codex-high",
        "claude-opus-4-8-thinking-high",
        "claude-4.6-sonnet-medium-thinking",
    ),
    "antigravity": ("gemini-4-carbon-high", "gemini-2.5-pro", "claude-sonnet-4-6"),
}
_MASO_VARIANT_SUFFIX_RE = re.compile(
    r"(?:[_-]reasoning(?:[_-](?:xhigh|high|medium|low|max))?"
    r"|[_-]thinking(?:[_-](?:xhigh|high|medium|low|max))?)$",
    re.IGNORECASE,
)


def _maso_std_variants(base: str, *, has_max: bool = True) -> dict[str, str]:
    hi, xhi = f"{base}_reasoning_high", f"{base}_reasoning_xhigh"
    out = {"none": base, "low": base, "medium": hi, "high": hi, "xhigh": xhi}
    if has_max:
        out["max"] = f"{base}_reasoning_max"
    return out


_MASO_MODEL_VARIANTS: dict[str, dict[str, str]] = {
    "gemini-4-argon": {
        "none": "gemini-4-argon",
        "low": "gemini-4-argon",
        "medium": "gemini-4-argon_reasoning_medium",
        "high": "gemini-4-argon_reasoning_high",
    },
    "gpt-6-astra": _maso_std_variants("gpt-6-astra"),
    "gpt56_sol": _maso_std_variants("gpt56_sol"),
    "gpt55": _maso_std_variants("gpt55", has_max=False),
    "es1-grape-g61s": _maso_std_variants("es1-grape-g61s"),
    **{
        b: {
            "none": b,
            "low": b,
            "medium": f"{b}_thinking",
            "high": f"{b}_thinking",
            "xhigh": f"{b}_thinking_max",
            "max": f"{b}_thinking_max",
        }
        for b in ("es1_orange_o50", "es1_orange_o48")
    },
    **{
        b: {"none": b, "low": b, "medium": f"{b}-reasoning", "high": f"{b}-reasoning-high"}
        for b in ("seed-stable", "seed-nightly", "deepseek-v41-flash")
    },
    "ada-qwen-27b-superfast": {
        "none": "ada-qwen-27b-superfast",
        "low": "ada-qwen-27b-superfast",
        "medium": "ada-qwen-27b-superfast-reasoning",
        "high": "ada-qwen-27b-superfast-reasoning",
        "xhigh": "ada-qwen-27b-superfast-reasoning-xhigh",
    },
}
_CURSOR_EFFORT_SUFFIXES: tuple[str, ...] = tuple(
    "extra-high minimal medium xhigh high none low max".split()
)
_CURSOR_NON_EFFORT_MODELS: frozenset[str] = frozenset(
    "auto auto-smart composer-1.5 claude-4.5-sonnet gpt-5.2".split()
)

_CODEBASE_MR_URL_RE = re.compile(
    r"https?://code\.byted\.org/(?P<repo>[\w.-]+(?:/[\w.-]+)+)/merge_requests/(?P<iid>\d+)",
    re.IGNORECASE,
)
_REPO_BANG_MR_RE = re.compile(r"(?<![\w/.-])(?P<repo>[\w.-]+/[\w.-]+)!(?P<iid>\d+)\b")
_BARE_BANG_MR_RE = re.compile(r"(?<![\w/.-])!(?P<iid>\d+)\b")
_REPO_NAME_RE = re.compile(r"\b([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)\b")
_COMMIT_URL_RE = re.compile(
    r"https?://code\.byted\.org/[\w./-]+/commit/(?P<sha>[0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
_COMMIT_TEXT_RE = re.compile(
    r"(?:commit|sha|版本|提交)[:\s]+(?P<sha>[0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
_HEX_SHA_RE = re.compile(r"\b(?P<sha>[0-9a-f]{8,40})\b")
_FEISHU_DOC_URL_RE = re.compile(
    r"https?://[\w.-]*(?:feishu\.cn|larkoffice\.com|doubao\.com)/"
    r"(?:docx|wiki|sheets|base|docs|minutes)/[A-Za-z0-9_-]+(?:\?[^\s<>\"')]*)?",
    re.IGNORECASE,
)


def _resolve_cursor_model_with_effort(model: str, effort: str) -> str:
    clean_model = (model or "gpt-5.3-codex").strip()
    if not effort:
        return clean_model
    if clean_model.lower() in _CURSOR_NON_EFFORT_MODELS:
        raise ValueError(
            f"Cursor model {clean_model!r} does not support reasoning_effort={effort!r}"
        )
    speed_suffix = ""
    stem = clean_model
    for tail in ("-fast", "-standard"):
        if stem.endswith(tail):
            speed_suffix = tail
            stem = stem[: -len(tail)]
            break

    if stem.endswith("-thinking"):
        pre_thinking = stem[: -len("-thinking")]
        for cand in _CURSOR_EFFORT_SUFFIXES:
            if pre_thinking.endswith(f"-{cand}"):
                base = pre_thinking[: -(len(cand) + 1)]
                if not base or base.lower() in _CURSOR_NON_EFFORT_MODELS:
                    raise ValueError(
                        f"Cursor model {clean_model!r} does not support reasoning_effort={effort!r}"
                    )
                return f"{base}-{effort}-thinking{speed_suffix}"

    for cand in _CURSOR_EFFORT_SUFFIXES:
        if stem.endswith(f"-{cand}"):
            if cand == "max" and stem.endswith("-codex-max"):
                break
            stem = stem[: -(len(cand) + 1)]
            break
    if not stem or stem.lower() in _CURSOR_NON_EFFORT_MODELS:
        raise ValueError(
            f"Cursor model {clean_model!r} does not support reasoning_effort={effort!r}"
        )
    suffix_effort = "extra-high" if (effort == "xhigh" and stem.endswith("-codex-max")) else effort
    return f"{stem}-{suffix_effort}{speed_suffix}"


def _resolve_maso_model_with_effort(model: str, effort: str) -> str:
    clean_model = (model or "gemini-4-argon_reasoning_high").strip()
    if not effort:
        return clean_model
    base = _MASO_VARIANT_SUFFIX_RE.sub("", clean_model)
    base_lower = base.lower()
    if base_lower in _MASO_MODEL_VARIANTS:
        variants = _MASO_MODEL_VARIANTS[base_lower]
        if effort not in variants:
            allowed = ", ".join(variants.keys())
            raise ValueError(
                f"MASO model {base_lower!r} does not support reasoning_effort={effort!r}; "
                f"supported efforts: {allowed}"
            )
        return variants[effort]
    if _MASO_VARIANT_SUFFIX_RE.search(clean_model):
        return base if effort == "none" else f"{base}_reasoning_{effort}"
    for cand in _CURSOR_EFFORT_SUFFIXES:
        if clean_model.endswith(f"-{cand}"):
            stem = clean_model[: -(len(cand) + 1)]
            if stem:
                return f"{stem}-{effort}"
    raise ValueError(f"MASO model {clean_model!r} has no variant for reasoning_effort={effort!r}")


def validate_ai_command_agent_config(
    provider: str | AgentType,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[AgentType, str, str]:
    """Validate and normalize ``(provider, model, reasoning_effort)`` for ``/ai``."""
    if isinstance(provider, AgentType):
        resolved_provider = provider
    else:
        raw_provider = str(provider or "").strip().lower()
        try:
            resolved_provider = AgentType(raw_provider)
        except ValueError as exc:
            allowed = ", ".join(p.value for p in SUPPORTED_AI_COMMAND_PROVIDERS)
            raise ValueError(
                f"Unsupported /ai provider {provider!r}; expected one of: {allowed}"
            ) from exc

    if resolved_provider not in SUPPORTED_AI_COMMAND_PROVIDERS:
        allowed = ", ".join(p.value for p in SUPPORTED_AI_COMMAND_PROVIDERS)
        raise ValueError(
            f"Provider {resolved_provider.value!r} cannot run /ai Chat turns; "
            f"choose one of: {allowed}"
        )

    resolved_model = (model or "").strip()
    resolved_effort = (reasoning_effort or "").strip().lower()
    if resolved_effort not in SUPPORTED_REASONING_EFFORTS:
        allowed_efforts = ", ".join(e for e in SUPPORTED_REASONING_EFFORTS if e)
        raise ValueError(
            f"Invalid reasoning_effort {reasoning_effort!r}; "
            f"expected empty or one of: {allowed_efforts}"
        )

    spec = get_spec(resolved_provider)
    allowed_provider_efforts = PROVIDER_SUPPORTED_REASONING_EFFORTS.get(resolved_provider.value, ())
    if resolved_effort:
        if not allowed_provider_efforts or spec.reasoning_effort in {
            ReasoningEffortKind.NONE,
            ReasoningEffortKind.MODEL_SELECTION,
        }:
            raise ValueError(
                f"Provider {resolved_provider.value!r} does not support a separate "
                f"reasoning_effort={resolved_effort!r}; choose a model variant instead "
                f"or leave reasoning_effort empty"
            )
        if resolved_effort not in allowed_provider_efforts:
            allowed_str = ", ".join(allowed_provider_efforts)
            raise ValueError(
                f"Provider {resolved_provider.value!r} does not support "
                f"reasoning_effort={resolved_effort!r}; supported efforts: {allowed_str}"
            )
        if spec.reasoning_effort == ReasoningEffortKind.MODEL_ID_SUFFIX:
            resolved_model = _resolve_cursor_model_with_effort(resolved_model, resolved_effort)
        elif spec.reasoning_effort == ReasoningEffortKind.MODEL_VARIANTS:
            resolved_model = _resolve_maso_model_with_effort(resolved_model, resolved_effort)

    return resolved_provider, resolved_model, resolved_effort


def resolve_ai_command_agent_env(
    provider: str | AgentType,
    model: str | None = None,
    reasoning_effort: str | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the provider launch/switch environment for ``(provider, model, reasoning_effort)``."""
    resolved_provider, effective_model, resolved_effort = validate_ai_command_agent_config(
        provider, model, reasoning_effort
    )
    env: dict[str, str] = dict(base_env or {})
    if resolved_provider == AgentType.CLAUDE and not env:
        env.update(DEFAULT_CLAUDE_LAUNCH_ENV)

    spec = get_spec(resolved_provider)
    if spec.model_env_key and effective_model:
        env[spec.model_env_key] = effective_model
        if resolved_provider == AgentType.CLAUDE:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = effective_model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = effective_model
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = effective_model

    if spec.effort_env_keys:
        for key in spec.effort_env_keys:
            if resolved_effort:
                env[key] = resolved_effort
            else:
                env.pop(key, None)
    else:
        for key in ("CODEX_REASONING_EFFORT", "TRAEX_REASONING_EFFORT"):
            env.pop(key, None)

    return env


@dataclass(frozen=True)
class FeishuAiCommandConfig:
    """Persisted configuration for the personal ``/ai`` Feishu assistant."""

    enabled: bool = False
    prefix: str = DEFAULT_AI_COMMAND_PREFIX
    owner_open_id: str = ""
    app_id: str = ""
    provider: AgentType = DEFAULT_AI_COMMAND_PROVIDER
    model: str = DEFAULT_AI_COMMAND_MODEL
    reasoning_effort: str = DEFAULT_AI_COMMAND_REASONING_EFFORT
    cwd: str = ""
    watch_chats: tuple[str, ...] = ()
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_active_chats: int = DEFAULT_MAX_ACTIVE_CHATS
    max_context_messages: int = DEFAULT_MAX_CONTEXT_MESSAGES
    ack_emoji: str = DEFAULT_AI_COMMAND_ACK_EMOJI
    done_emoji: str = DEFAULT_AI_COMMAND_DONE_EMOJI
    footer: str = DEFAULT_AI_COMMAND_FOOTER
    reply_in_thread: bool = True

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["provider"] = self.provider.value
        out["watch_chats"] = list(self.watch_chats)
        return out

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> FeishuAiCommandConfig:
        raw_provider = data.get("provider", DEFAULT_AI_COMMAND_PROVIDER.value)
        raw_model = data.get("model", DEFAULT_AI_COMMAND_MODEL)
        raw_effort = data.get("reasoning_effort", DEFAULT_AI_COMMAND_REASONING_EFFORT)
        provider, model, effort = validate_ai_command_agent_config(
            raw_provider,
            str(raw_model) if raw_model is not None else "",
            str(raw_effort) if raw_effort is not None else "",
        )
        prefix = str(data.get("prefix") or DEFAULT_AI_COMMAND_PREFIX).strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        raw_watch = data.get("watch_chats")
        watch_chats: list[str] = []
        if isinstance(raw_watch, str):
            watch_chats = [
                item.strip()
                for part in raw_watch.replace(",", " ").splitlines()
                for item in part.split()
                if item.strip()
            ]
        elif isinstance(raw_watch, (list, tuple, set)):
            watch_chats = [str(i).strip() for i in raw_watch if isinstance(i, str) and i.strip()]
        poll_interval = max(
            2.0,
            min(
                float(data.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)),
                300.0,
            ),
        )
        max_active = max(1, min(int(data.get("max_active_chats", DEFAULT_MAX_ACTIVE_CHATS)), 50))
        max_ctx = max(
            1,
            min(int(data.get("max_context_messages", DEFAULT_MAX_CONTEXT_MESSAGES)), 100),
        )

        def s(key: str, default: str = "") -> str:
            return str(data.get(key) or default).strip()

        kw: dict[str, Any] = {
            "enabled": bool(data.get("enabled", False)),
            "prefix": prefix,
            "owner_open_id": s("owner_open_id"),
            "app_id": s("app_id"),
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "cwd": s("cwd"),
            "watch_chats": tuple(dict.fromkeys(watch_chats)),
            "poll_interval_seconds": poll_interval,
            "max_active_chats": max_active,
            "max_context_messages": max_ctx,
            "ack_emoji": s("ack_emoji", DEFAULT_AI_COMMAND_ACK_EMOJI),
            "done_emoji": s("done_emoji", DEFAULT_AI_COMMAND_DONE_EMOJI),
            "footer": s("footer", DEFAULT_AI_COMMAND_FOOTER),
            "reply_in_thread": bool(data.get("reply_in_thread", True)),
        }
        return cls(**kw)


@dataclass(frozen=True)
class ParsedAiCommand:
    """Parsed ``/ai`` command including inline overrides and resolved execution config."""

    raw_text: str
    prefix: str
    instruction: str
    provider: AgentType
    model: str
    reasoning_effort: str
    inline_overrides: Mapping[str, str] = field(default_factory=dict)

    @property
    def effective_env(self) -> dict[str, str]:
        return resolve_ai_command_agent_env(self.provider, self.model, self.reasoning_effort)


@dataclass(frozen=True)
class AiCommandContextRefs:
    """Entities extracted from the ``/ai`` message and its surrounding thread/chat context."""

    mr_refs: tuple[str, ...] = ()
    commit_refs: tuple[str, ...] = ()
    doc_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "mr_refs": list(self.mr_refs),
            "commit_refs": list(self.commit_refs),
            "doc_refs": list(self.doc_refs),
        }


@dataclass(frozen=True)
class AiCommandExecutionResult:
    """Structured outcome of processing or dry-running one ``/ai`` command."""

    handled: bool
    dry_run: bool
    message_id: str
    chat_id: str
    thread_id: str | None
    status: str
    reason: str = ""
    parsed: ParsedAiCommand | None = None
    refs: AiCommandContextRefs = field(default_factory=AiCommandContextRefs)
    interim_text: str = ""
    interim_post: Mapping[str, Any] = field(default_factory=dict)
    final_text: str = ""
    final_post: Mapping[str, Any] = field(default_factory=dict)
    ack_reaction: str = ""
    done_reaction: str = ""
    tab_id: str | None = None
    interim_message_id: str | None = None
    final_message_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        p = self.parsed
        out = {k: v for k, v in asdict(self).items() if k not in {"parsed", "refs"}}
        out["instruction"] = p.instruction if p else ""
        out["provider"] = p.provider.value if p else None
        out["model"] = p.model if p else None
        out["reasoning_effort"] = p.reasoning_effort if p else None
        out["inline_overrides"] = dict(p.inline_overrides) if p else {}
        out["effective_env"] = p.effective_env if p else {}
        out["refs"] = self.refs.to_dict()
        out["interim_post"] = dict(self.interim_post)
        out["final_post"] = dict(self.final_post)
        return out


_LEADING_FLAG_RE = re.compile(
    r"^\s*(?:(?P<stop>--)(?:\s+|$)|"
    r"(?P<flag>--provider|--agent-type|-p|--model|-m|--effort|--reasoning-effort|--thinking|-e)"
    r"(?:=(?P<eq_val>\"[^\"]*\"|'[^']*'|\S+)|\s+(?P<sp_val>\"[^\"]*\"|'[^']*'|\S+))"
    r")(?P<tail>[\s\S]*)$",
    re.IGNORECASE,
)


def _strip_quotes(val: str) -> str:
    return val[1:-1] if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"} else val


def parse_ai_command(
    text: str,
    *,
    prefix: str = DEFAULT_AI_COMMAND_PREFIX,
    default_provider: str | AgentType = DEFAULT_AI_COMMAND_PROVIDER,
    default_model: str = DEFAULT_AI_COMMAND_MODEL,
    default_reasoning_effort: str = DEFAULT_AI_COMMAND_REASONING_EFFORT,
    footer: str = DEFAULT_AI_COMMAND_FOOTER,
) -> ParsedAiCommand | None:
    """Parse an inbound message for the ``/ai`` prefix and optional inline flags."""
    if not isinstance(text, str):
        return None
    if (footer and footer in text) or DEFAULT_AI_COMMAND_FOOTER in text:
        return None

    clean_prefix = (prefix or DEFAULT_AI_COMMAND_PREFIX).strip()
    pattern = re.compile(
        rf"^\s*{re.escape(clean_prefix)}(?:\b|(?=\s)|$)(?P<rest>[\s\S]*)$",
        re.IGNORECASE,
    )
    match = pattern.match(text)
    if not match:
        return None

    overrides: dict[str, str] = {}
    remaining = (match.group("rest") or "").strip()
    while remaining.startswith("-"):
        flag_match = _LEADING_FLAG_RE.match(remaining)
        if not flag_match:
            break
        if flag_match.group("stop"):
            remaining = (flag_match.group("tail") or "").strip()
            break
        flag_lower = flag_match.group("flag").lower()
        raw_val = flag_match.group("eq_val")
        if raw_val is None:
            raw_val = flag_match.group("sp_val") or ""
        val = _strip_quotes(raw_val.strip())
        if not val:
            break
        if flag_lower in {"--provider", "--agent-type", "-p"}:
            overrides["provider"] = val
        elif flag_lower in {"--model", "-m"}:
            overrides["model"] = val
        elif flag_lower in {"--effort", "--reasoning-effort", "--thinking", "-e"}:
            overrides["reasoning_effort"] = val
        remaining = (flag_match.group("tail") or "").strip()

    provider_in = overrides.get("provider", default_provider)
    base_provider, _, _ = validate_ai_command_agent_config(default_provider, "", "")
    target_provider, _, _ = validate_ai_command_agent_config(provider_in, "", "")
    if "model" in overrides:
        model_in = overrides["model"]
    elif target_provider != base_provider:
        defaults = DEFAULT_PROVIDER_MODELS.get(target_provider.value, ())
        model_in = defaults[0] if defaults else ""
    else:
        model_in = default_model

    if "reasoning_effort" in overrides:
        effort_in = overrides["reasoning_effort"]
    elif not PROVIDER_SUPPORTED_REASONING_EFFORTS.get(target_provider.value):
        effort_in = ""
    elif (
        target_provider != base_provider
        and default_reasoning_effort
        not in PROVIDER_SUPPORTED_REASONING_EFFORTS.get(target_provider.value, ())
    ):
        effort_in = "high"
    else:
        effort_in = default_reasoning_effort

    resolved_provider, resolved_model, resolved_effort = validate_ai_command_agent_config(
        target_provider, model_in, effort_in
    )
    return ParsedAiCommand(
        text, clean_prefix, remaining, resolved_provider, resolved_model, resolved_effort, overrides
    )


def extract_context_references(
    texts: Sequence[str], *, chat_name: str = ""
) -> AiCommandContextRefs:
    """Extract MR references, commit SHAs, and Feishu doc URLs from thread/chat texts."""
    combined_texts = [t for t in texts if isinstance(t, str) and t.strip()]
    mr_refs: list[str] = []
    commit_refs: list[str] = []
    doc_refs: list[str] = []
    known_repos = [
        m.group(1)
        for m in (_REPO_NAME_RE.finditer(chat_name) if chat_name else ())
        if not m.group(1).startswith(("http", "open-apis"))
    ]
    for text in combined_texts:
        for regex in (_CODEBASE_MR_URL_RE, _REPO_BANG_MR_RE):
            for m in regex.finditer(text):
                repo, iid = m.group("repo"), m.group("iid")
                if repo not in known_repos:
                    known_repos.append(repo)
                if f"{repo}!{iid}" not in mr_refs:
                    mr_refs.append(f"{repo}!{iid}")
    default_repo = known_repos[0] if known_repos else ""
    for text in combined_texts:
        for m in _BARE_BANG_MR_RE.finditer(text):
            iid = m.group("iid")
            if not any(existing.endswith(f"!{iid}") for existing in mr_refs):
                ref = f"{default_repo}!{iid}" if default_repo else f"!{iid}"
                if ref not in mr_refs:
                    mr_refs.append(ref)
        for regex in (_COMMIT_URL_RE, _COMMIT_TEXT_RE):
            for m in regex.finditer(text):
                sha = m.group("sha").lower()[:10]
                if sha not in commit_refs:
                    commit_refs.append(sha)
        for m in _HEX_SHA_RE.finditer(text):
            sha = m.group("sha").lower()
            if not sha.isdigit() and sha[:10] not in commit_refs:
                commit_refs.append(sha[:10])
        for m in _FEISHU_DOC_URL_RE.finditer(text):
            if m.group(0) not in doc_refs:
                doc_refs.append(m.group(0))
    return AiCommandContextRefs(tuple(mr_refs), tuple(commit_refs), tuple(doc_refs))


def ensure_ai_footer(text: str, footer: str = DEFAULT_AI_COMMAND_FOOTER) -> str:
    clean_body = text.strip() or "已完成处理。"
    clean_footer = (footer or DEFAULT_AI_COMMAND_FOOTER).strip()
    if clean_footer in clean_body:
        return clean_body
    max_body = max(200, FEISHU_MESSAGE_MAX_CHARS - len(clean_footer) - 10)
    if len(clean_body) > max_body:
        clean_body = clean_body[: max_body - 1] + "…"
    return f"{clean_body}\n{clean_footer}"


def build_ai_post_payload(text: str, *, footer: str = DEFAULT_AI_COMMAND_FOOTER) -> dict[str, Any]:
    with_footer = ensure_ai_footer(text, footer=footer)
    try:
        return dict(build_feishu_post_content(with_footer))
    except ValueError:
        return {"zh_cn": {"content": [[{"tag": "text", "text": with_footer}]]}}


def build_interim_reply_text(
    parsed: ParsedAiCommand, refs: AiCommandContextRefs, *, footer: str = DEFAULT_AI_COMMAND_FOOTER
) -> str:
    action_desc = parsed.instruction.strip()
    if refs.mr_refs:
        mr = refs.mr_refs[0]
        target = f"{mr}（最新 commit {refs.commit_refs[0][:10]}）" if refs.commit_refs else mr
        is_rev = not action_desc or "review" in action_desc.lower() or "审查" in action_desc
        phrase = "做逐文件深度审查" if is_rev else f"执行「{action_desc.splitlines()[0][:48]}」"
        body = f"正在派 Subagent 对 {target}{phrase}，待审查完成后执行回复。"
    else:
        short_inst = (action_desc or "当前上下文").splitlines()[0][:48]
        body = f"正在派 Subagent 处理「{short_inst}」，待完成后执行回复。"
    return ensure_ai_footer(body, footer=footer)


def bounded_message_uuid(message_id: str, stage: str) -> str:
    digest = hashlib.sha256(f"{message_id}:{stage}".encode("utf-8")).hexdigest()[:32]
    return f"ai:{stage[:8]}:{digest}"[:50]


def build_ai_dispatch_prompt(
    message: FeishuUserMessage,
    parsed: ParsedAiCommand,
    context_messages: Sequence[FeishuUserMessage],
    refs: AiCommandContextRefs,
) -> str:
    lines: list[str] = [
        "You are executing a personal Feishu `/ai` command on behalf of the account owner.",
        "Respond with a clear, concise, evidence-backed Chinese reply suitable for posting "
        "directly in the Feishu conversation (do NOT append the `> 🤖 AI 代答，仅供参考` footer "
        "yourself; Claude Hub appends it automatically).",
        "",
        f"Command instruction: {parsed.instruction or '(follow up on the conversation context below)'}",
        f"Chat ID: {message.chat_id}",
    ]
    if message.thread_id:
        lines.append(f"Thread ID: {message.thread_id}")
    if refs.mr_refs:
        lines.append(f"Extracted MR references: {', '.join(refs.mr_refs)}")
    if refs.commit_refs:
        lines.append(f"Extracted commit SHAs: {', '.join(refs.commit_refs)}")
    if refs.doc_refs:
        lines.append(f"Extracted document links: {', '.join(refs.doc_refs)}")
    if context_messages:
        lines.extend(["", '<feishu_thread_context trust="untrusted">'])
        for ctx in context_messages[-30:]:
            safe_text = ctx.text.replace("<", "&lt;").replace(">", "&gt;")
            lines.append(
                f"[{ctx.message_id} · sender={ctx.sender_open_id} · type={ctx.msg_type}] {safe_text}"
            )
        lines.append("</feishu_thread_context>")
    return "\n".join(lines)


AiTurnDispatcher = Callable[[str, str, str, str, Mapping[str, Any]], Awaitable[str]]


class FeishuAiCommandService:
    """Coordinates ``/ai`` configuration, deduplication, context fetch, reactions, and replies."""

    def __init__(self, **kwargs: Any) -> None:
        runtime_home = resolve_runtime_home()
        self._state_path: Path = kwargs.get("state_path") or (
            runtime_home / "feishu_ai_command.json"
        )
        self._token_store: FeishuUserTokenStore = (
            kwargs.get("token_store") or get_feishu_user_token_store()
        )
        self._dedup: FeishuMessageDedupStore = (
            kwargs.get("dedup_store") or FeishuMessageDedupStore()
        )
        self._user_client_factory: Callable[[FeishuAiCommandConfig], FeishuUserClient] | None = (
            kwargs.get("user_client_factory")
        )
        self._tab_creator = kwargs.get("tab_creator")
        self._tab_getter = kwargs.get("tab_getter")
        self._tab_env_switcher = kwargs.get("tab_env_switcher")
        self._turn_dispatcher: AiTurnDispatcher | None = kwargs.get("turn_dispatcher")
        self._lock = threading.RLock()
        self._channel_locks: dict[str, asyncio.Lock] = {}

    def _load_state(self) -> dict[str, Any]:
        default_doc: dict[str, Any] = {
            "version": _STATE_VERSION,
            "config": FeishuAiCommandConfig().to_dict(),
            "watermarks": {},
            "scan_cursors": {},
            "channel_tabs": {},
            "recent_runs": [],
            "pending_queue": [],
        }
        if not document_exists(self._state_path):
            return default_doc
        try:
            raw = read_document(self._state_path)
        except (FileNotFoundError, ValueError, OSError):
            return default_doc
        return dict(raw) if isinstance(raw, Mapping) else default_doc

    def _save_state(self, state: Mapping[str, Any]) -> None:
        write_document(self._state_path, dict(state))

    def is_enabled(self) -> bool:
        """Fast check whether /ai command is enabled without touching token storage."""
        with self._lock:
            if not document_exists(self._state_path):
                return False
            raw_cfg = self._load_state().get("config")
            return bool(isinstance(raw_cfg, Mapping) and raw_cfg.get("enabled"))

    def get_config(self, *, auto_import_lark_cli: bool | None = None) -> FeishuAiCommandConfig:
        with self._lock:
            raw_cfg = self._load_state().get("config")
            cfg = (
                FeishuAiCommandConfig.from_mapping(raw_cfg)
                if isinstance(raw_cfg, Mapping)
                else FeishuAiCommandConfig()
            )
            should_auto = cfg.enabled if auto_import_lark_cli is None else auto_import_lark_cli
            if not cfg.owner_open_id or not cfg.app_id:
                record = self._token_store.resolve_token(
                    open_id=cfg.owner_open_id or None,
                    app_id=cfg.app_id or None,
                    auto_import_lark_cli=should_auto,
                )
                if record is not None:
                    cfg = replace(
                        cfg,
                        owner_open_id=cfg.owner_open_id or record.open_id,
                        app_id=cfg.app_id or record.app_id,
                    )
            return cfg

    def update_config(self, patch: Mapping[str, Any]) -> FeishuAiCommandConfig:
        with self._lock:
            state = self._load_state()
            current_raw = state.get("config")
            merged: dict[str, Any] = (
                dict(current_raw)
                if isinstance(current_raw, Mapping)
                else FeishuAiCommandConfig().to_dict()
            )
            old_provider = str(merged.get("provider") or DEFAULT_AI_COMMAND_PROVIDER.value)
            for key, value in patch.items():
                if value is not None:
                    merged[key] = value
            new_provider = str(merged.get("provider") or old_provider).strip().lower()
            if "provider" in patch and patch["provider"] is not None:
                if "model" not in patch or patch["model"] is None:
                    if new_provider != old_provider.strip().lower():
                        defaults = DEFAULT_PROVIDER_MODELS.get(new_provider, ())
                        if defaults:
                            merged["model"] = defaults[0]
                if "reasoning_effort" not in patch or patch["reasoning_effort"] is None:
                    supported = PROVIDER_SUPPORTED_REASONING_EFFORTS.get(new_provider, ())
                    if not supported:
                        merged["reasoning_effort"] = ""
                    elif str(merged.get("reasoning_effort") or "") not in supported:
                        merged["reasoning_effort"] = "high" if "high" in supported else supported[0]
            updated = FeishuAiCommandConfig.from_mapping(merged)
            state["config"] = updated.to_dict()
            self._save_state(state)
            return updated

    def enqueue_pending_command(
        self,
        message: FeishuUserMessage,
        *,
        source: str = "poller",
        seeded_context: Sequence[FeishuUserMessage] | None = None,
    ) -> bool:
        """Durably record an accepted ``/ai`` command before advancing the poller watermark."""
        with self._lock:
            state = self._load_state()
            queue = [dict(x) for x in (state.get("pending_queue") or []) if isinstance(x, Mapping)]
            mid = message.message_id
            if any(
                isinstance(x.get("message"), Mapping) and x["message"].get("message_id") == mid
                for x in queue
            ):
                return False
            if any(
                isinstance(r, Mapping) and r.get("message_id") == mid
                for r in (state.get("recent_runs") or [])
            ):
                return False
            seeded = [m.to_dict() for m in seeded_context] if seeded_context is not None else None
            queue.append(
                {
                    "message": message.to_dict(),
                    "source": source,
                    "seeded_context": seeded,
                    "state": "queued",
                    "enqueued_at": time.time(),
                }
            )
            state["pending_queue"] = queue
            self._save_state(state)
            return True

    def mark_pending_command_state(self, message_id: str, cmd_state: str) -> None:
        """Update lifecycle state (`queued`/`in_flight`/`interrupted`) of a pending command."""
        with self._lock:
            state = self._load_state()
            changed = False
            queue: list[dict[str, Any]] = []
            for item in state.get("pending_queue") or []:
                if not isinstance(item, Mapping):
                    continue
                row = dict(item)
                msg = row.get("message")
                if (
                    isinstance(msg, Mapping)
                    and msg.get("message_id") == message_id
                    and row.get("state") != cmd_state
                ):
                    row["state"] = cmd_state
                    changed = True
                queue.append(row)
            if changed:
                state["pending_queue"] = queue
                self._save_state(state)

    def reconcile_interrupted_command(self, message_id: str) -> bool:
        """Reconcile an interrupted in-flight command on restart; returns True if runnable."""
        with self._lock:
            state = self._load_state()
            if any(
                isinstance(r, Mapping) and r.get("message_id") == message_id
                for r in (state.get("recent_runs") or [])
            ):
                self.dequeue_pending_command(message_id)
                return False
            cfg = FeishuAiCommandConfig.from_mapping(state.get("config") or {})
            self._dedup.release(f"ai_cmd:{cfg.owner_open_id or 'owner'}:{message_id}")
            self.mark_pending_command_state(message_id, "queued")
            return True

    def dequeue_pending_command(self, message_id: str) -> None:
        """Remove a completed/handled command from the durable pending queue."""
        with self._lock:
            state = self._load_state()
            state["pending_queue"] = [
                dict(x)
                for x in (state.get("pending_queue") or [])
                if isinstance(x, Mapping)
                and not (
                    isinstance(x.get("message"), Mapping)
                    and x["message"].get("message_id") == message_id
                )
            ]
            self._save_state(state)

    def list_pending_commands(self) -> list[dict[str, Any]]:
        """Return all durably enqueued ``/ai`` commands awaiting execution."""
        with self._lock:
            return [
                dict(x)
                for x in (self._load_state().get("pending_queue") or [])
                if isinstance(x, Mapping)
            ]

    def get_scan_cursor(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            cursors = self._load_state().get("scan_cursors")
            raw = cursors.get(chat_id) if isinstance(cursors, Mapping) else None
            return dict(raw) if isinstance(raw, Mapping) else None

    def set_scan_cursor(self, chat_id: str, cursor_state: Mapping[str, Any] | None) -> None:
        with self._lock:
            state = self._load_state()
            cursors = dict(state.get("scan_cursors") or {})
            if cursor_state is None:
                if chat_id not in cursors:
                    return
                cursors.pop(chat_id, None)
            else:
                cursors[chat_id] = dict(cursor_state)
            state["scan_cursors"] = cursors
            self._save_state(state)

    def get_watermarks(self) -> dict[str, int]:
        with self._lock:
            wm = self._load_state().get("watermarks")
            return (
                {
                    k: int(v)
                    for k, v in wm.items()
                    if isinstance(k, str) and isinstance(v, (int, float))
                }
                if isinstance(wm, Mapping)
                else {}
            )

    def set_watermark(self, chat_id: str, create_time_ms: int) -> None:
        with self._lock:
            state = self._load_state()
            wm = dict(state.get("watermarks") or {})
            if create_time_ms > int(wm.get(chat_id) or 0):
                wm[chat_id] = int(create_time_ms)
                state["watermarks"] = wm
                self._save_state(state)

    def get_recent_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = self._load_state().get("recent_runs")
            return list(runs) if isinstance(runs, list) else []

    def _record_run(self, result: AiCommandExecutionResult, *, source: str) -> None:
        with self._lock:
            state = self._load_state()
            runs = list(state.get("recent_runs") or [])
            d = result.to_dict()
            keys = "message_id chat_id thread_id status reason provider model reasoning_effort refs tab_id".split()
            runs.insert(0, {k: d[k] for k in keys} | {"source": source, "at": time.time()})
            state["recent_runs"] = runs[:_MAX_RECENT_RUNS]
            self._save_state(state)

    def build_user_client(self, config: FeishuAiCommandConfig | None = None) -> FeishuUserClient:
        cfg = config or self.get_config()
        if self._user_client_factory is not None:
            return self._user_client_factory(cfg)
        return FeishuUserClient(
            self._token_store,
            open_id=cfg.owner_open_id or None,
            app_id=cfg.app_id or settings.feishu_app_id or None,
        )

    def preflight_status(self, *, auto_import_lark_cli: bool = True) -> FeishuScopePreflightResult:
        cfg = self.get_config()
        rec = self._token_store.resolve_token(
            open_id=cfg.owner_open_id or None,
            app_id=cfg.app_id or None,
            auto_import_lark_cli=auto_import_lark_cli,
        )
        return self.build_user_client(cfg).preflight_scopes(rec)

    def capabilities_dict(self) -> dict[str, Any]:
        model_efforts: dict[str, list[str]] = {}
        for base, variants in _MASO_MODEL_VARIANTS.items():
            effs = list(variants.keys())
            model_efforts[base] = effs
            for v in set(variants.values()):
                model_efforts[v] = effs
        return {
            "providers": [p.value for p in SUPPORTED_AI_COMMAND_PROVIDERS],
            "reasoning_efforts": [e for e in SUPPORTED_REASONING_EFFORTS if e],
            "provider_reasoning_efforts": {
                k: list(v) for k, v in PROVIDER_SUPPORTED_REASONING_EFFORTS.items()
            },
            "model_reasoning_efforts": model_efforts,
            "provider_models": {k: list(v) for k, v in DEFAULT_PROVIDER_MODELS.items()},
        }

    def is_owner_ai_command_candidate(
        self,
        *,
        sender_open_id: str,
        sender_type: str,
        text: str,
        config: FeishuAiCommandConfig | None = None,
    ) -> bool:
        cfg = config or self.get_config()
        if not cfg.enabled or sender_type != "user" or not cfg.owner_open_id or not sender_open_id:
            return False
        if not hmac.compare_digest(sender_open_id.strip(), cfg.owner_open_id.strip()):
            return False
        try:
            parsed = parse_ai_command(
                text,
                prefix=cfg.prefix,
                default_provider=cfg.provider,
                default_model=cfg.model,
                default_reasoning_effort=cfg.reasoning_effort,
                footer=cfg.footer,
            )
        except ValueError:
            return False
        return parsed is not None

    async def _fetch_context_and_refs(
        self,
        client: FeishuUserClient,
        message: FeishuUserMessage,
        cfg: FeishuAiCommandConfig,
        *,
        seeded_context: Sequence[FeishuUserMessage] | None = None,
    ) -> tuple[list[FeishuUserMessage], AiCommandContextRefs]:
        context_msgs: list[FeishuUserMessage] = list(seeded_context or [])
        if not context_msgs:
            if message.thread_id:
                try:
                    context_msgs = await client.list_messages(
                        container_id_type="thread",
                        container_id=message.thread_id,
                        sort_type="ByCreateTimeAsc",
                        page_size=cfg.max_context_messages,
                    )
                except Exception:
                    logger.warning(
                        "Failed to list Feishu thread messages for %s",
                        message.thread_id,
                    )
            if not context_msgs and message.chat_id:
                try:
                    desc_msgs = await client.list_messages(
                        container_id_type="chat",
                        container_id=message.chat_id,
                        sort_type="ByCreateTimeDesc",
                        page_size=cfg.max_context_messages,
                    )
                    context_msgs = list(reversed(desc_msgs))
                except Exception:
                    logger.warning("Failed to list Feishu chat messages for %s", message.chat_id)
            if message.parent_id and all(m.message_id != message.parent_id for m in context_msgs):
                try:
                    parent_msg = await client.get_message(message.parent_id)
                    if parent_msg is not None:
                        context_msgs.insert(0, parent_msg)
                except Exception:
                    pass

        texts = [m.text for m in context_msgs if m.text]
        if message.text and message.text not in texts:
            texts.append(message.text)
        return context_msgs, extract_context_references(texts)

    async def _ensure_execution_tab(
        self,
        message: FeishuUserMessage,
        parsed: ParsedAiCommand,
        cfg: FeishuAiCommandConfig,
    ) -> str:
        channel_key = f"{message.chat_id}:{message.thread_id or 'main'}:{parsed.provider.value}"
        with self._lock:
            existing_tab_id = dict(self._load_state().get("channel_tabs") or {}).get(channel_key)

        target_env = parsed.effective_env
        if existing_tab_id:
            tab = self._get_tab(existing_tab_id)
            if tab is not None and not getattr(tab, "archived", False):
                current_env = dict(getattr(tab, "env", {}) or {})
                merged_env = resolve_ai_command_agent_env(
                    parsed.provider,
                    parsed.model,
                    parsed.reasoning_effort,
                    base_env=current_env,
                )
                if merged_env != current_env:
                    await self._switch_tab_env(str(existing_tab_id), merged_env, tab=tab)
                return str(existing_tab_id)

        cwd = cfg.cwd.strip() or str(Path.home())
        if not os.path.isdir(cwd):
            cwd = str(Path.home())
        short_target = (message.thread_id or message.chat_id)[-6:]
        created = await self._create_tab(
            name=f"Feishu /ai · {short_target}"[:64],
            session_kind=SessionKind.CHAT,
            agent_type=parsed.provider,
            cwd=cwd,
            solo_mode=True,
            env=target_env,
        )
        tab_id = created if isinstance(created, str) else getattr(created, "id", "")
        if not isinstance(tab_id, str) or not tab_id.strip():
            raise RuntimeError("Failed to create Hub Chat tab for Feishu /ai command")
        tab_id = tab_id.strip()
        with self._lock:
            state = self._load_state()
            channel_tabs = dict(state.get("channel_tabs") or {})
            channel_tabs[channel_key] = tab_id
            state["channel_tabs"] = channel_tabs
            self._save_state(state)
        return tab_id

    def _get_tab(self, tab_id: str) -> Any:
        if self._tab_getter is not None:
            return self._tab_getter(tab_id)
        from claude_hub.services import ttyd_manager

        return ttyd_manager.get_tab(tab_id)

    async def _switch_tab_env(self, tab_id: str, env: dict[str, str], *, tab: Any = None) -> None:
        if self._tab_env_switcher is not None:
            res = self._tab_env_switcher(tab_id, env)
            if inspect.isawaitable(res):
                await res
            return
        switch_fn = getattr(tab, "switch_env", None) if tab is not None else None
        if callable(switch_fn):
            res = switch_fn(env)
            if inspect.isawaitable(res):
                await res
            return
        from claude_hub.services import ttyd_manager

        res = ttyd_manager.switch_env(tab_id, env)
        updated_tab = await res if inspect.isawaitable(res) else res
        try:
            from claude_hub.api.agent_stream import (
                _get_tab_tailer_manager,
                _terminal_tab_stream_session,
            )

            stream_session = _terminal_tab_stream_session(tab_id)
            if stream_session is not None:
                effective_env = dict(getattr(updated_tab, "env", None) or env)
                _get_tab_tailer_manager().set_env(stream_session, effective_env)
        except Exception:
            logger.debug(
                "Could not sync live stream tailer env for tab %s",
                tab_id,
                exc_info=True,
            )

    async def _create_tab(self, **kwargs: Any) -> Any:
        if self._tab_creator is not None:
            res = self._tab_creator(**kwargs)
            return await res if inspect.isawaitable(res) else res
        from claude_hub.services import ttyd_manager

        res = ttyd_manager.create_tab(**kwargs)
        return await res if inspect.isawaitable(res) else res

    async def _dispatch_turn(
        self,
        tab_id: str,
        prompt: str,
        turn_id: str,
        visible_text: str,
        turn_metadata: Mapping[str, Any],
    ) -> str:
        if self._turn_dispatcher is not None:
            return await self._turn_dispatcher(tab_id, prompt, turn_id, visible_text, turn_metadata)
        from claude_hub.api.agent_stream import dispatch_tab_chat_and_wait

        return await dispatch_tab_chat_and_wait(
            tab_id,
            prompt,
            turn_id,
            visible_text=visible_text,
            turn_metadata=dict(turn_metadata),
        )

    def _skip_result(self, msg: FeishuUserMessage, **kw: Any) -> AiCommandExecutionResult:
        kw.setdefault("status", "skipped")
        return AiCommandExecutionResult(
            False, bool(kw.pop("dry_run", False)), msg.message_id, msg.chat_id, msg.thread_id, **kw
        )

    def _done_result(self, msg: FeishuUserMessage, **kw: Any) -> AiCommandExecutionResult:
        tid = kw.pop("thread_id", msg.thread_id)
        dry = bool(kw.pop("dry_run", False))
        return AiCommandExecutionResult(True, dry, msg.message_id, msg.chat_id, tid, **kw)

    async def handle_message(
        self,
        message: FeishuUserMessage,
        *,
        source: str = "poller",
        dry_run: bool = False,
        seeded_context: Sequence[FeishuUserMessage] | None = None,
    ) -> AiCommandExecutionResult:
        """Run the full ``/ai`` command pipeline for one candidate message."""
        cfg = self.get_config()
        if not dry_run and not cfg.enabled:
            return self._skip_result(message, dry_run=dry_run, reason="disabled")
        if message.sender_type != "user":
            return self._skip_result(message, dry_run=dry_run, reason="not_user_sender")
        if not dry_run:
            if not cfg.owner_open_id.strip():
                return self._skip_result(message, dry_run=dry_run, reason="no_owner_open_id")
            if not hmac.compare_digest(message.sender_open_id.strip(), cfg.owner_open_id.strip()):
                return self._skip_result(message, dry_run=dry_run, reason="sender_not_owner")

        try:
            parsed = parse_ai_command(
                message.text,
                prefix=cfg.prefix,
                default_provider=cfg.provider,
                default_model=cfg.model,
                default_reasoning_effort=cfg.reasoning_effort,
                footer=cfg.footer,
            )
        except ValueError as exc:
            return self._skip_result(
                message, dry_run=dry_run, status="invalid_flags", reason=str(exc)
            )

        if parsed is None:
            return self._skip_result(message, dry_run=dry_run, reason="prefix_not_matched")

        dedup_claim_key = f"ai_cmd:{cfg.owner_open_id or 'owner'}:{message.message_id}"
        if not dry_run and not self._dedup.claim(dedup_claim_key):
            return self._skip_result(
                message,
                dry_run=dry_run,
                status="duplicate",
                reason="already_claimed",
                parsed=parsed,
            )

        user_client = self.build_user_client(cfg)
        ack_reaction_id: str | None = None
        reply_in_thread = bool(message.thread_id) or cfg.reply_in_thread
        status_val = "completed"
        reason_val = ""
        tab_id: str | None = None
        interim_receipt = FeishuReplyReceipt()
        final_receipt = FeishuReplyReceipt()

        try:
            if dry_run:
                context_msgs = list(seeded_context or [])
                texts = [m.text for m in context_msgs if m.text]
                if message.text:
                    texts.append(message.text)
                refs = extract_context_references(texts)
                interim_text = build_interim_reply_text(parsed, refs, footer=cfg.footer)
                interim_post = build_ai_post_payload(interim_text, footer=cfg.footer)
                preview_final = ensure_ai_footer(
                    f"[Dry-run preview] Will dispatch to provider={parsed.provider.value}, "
                    f"model={parsed.model or '(default)'}, "
                    f"reasoning_effort={parsed.reasoning_effort or '(default)'}.",
                    footer=cfg.footer,
                )
                return self._done_result(
                    message,
                    status="dry_run",
                    parsed=parsed,
                    dry_run=True,
                    refs=refs,
                    interim_text=interim_text,
                    interim_post=interim_post,
                    final_text=preview_final,
                    final_post=build_ai_post_payload(preview_final, footer=cfg.footer),
                    ack_reaction=cfg.ack_emoji,
                    done_reaction=cfg.done_emoji,
                )

            user_client.require_send_as_user_scope()
            if cfg.ack_emoji:
                try:
                    async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                        ack_reaction_id = await user_client.add_reaction_as_user(
                            message.message_id, cfg.ack_emoji
                        )
                except Exception:
                    logger.warning(
                        "Failed to add %s reaction on %s",
                        cfg.ack_emoji,
                        message.message_id,
                    )

            context_msgs, refs = await self._fetch_context_and_refs(
                user_client, message, cfg, seeded_context=seeded_context
            )
            interim_text = build_interim_reply_text(parsed, refs, footer=cfg.footer)
            interim_post = build_ai_post_payload(interim_text, footer=cfg.footer)
            interim_receipt = await user_client.reply_as_user(
                message.message_id,
                "post",
                interim_post,
                reply_in_thread=reply_in_thread,
                uuid=bounded_message_uuid(message.message_id, "interim"),
            )

            lock_key = f"{message.chat_id}:{message.thread_id or 'main'}"
            with self._lock:
                channel_lock = self._channel_locks.setdefault(lock_key, asyncio.Lock())

            async with channel_lock:
                tab_id = await self._ensure_execution_tab(message, parsed, cfg)
                prompt = build_ai_dispatch_prompt(message, parsed, context_msgs, refs)
                turn_metadata = {
                    "origin": "feishu_ai_command",
                    "source": source,
                    "message_id": message.message_id,
                    "chat_id": message.chat_id,
                    "thread_id": message.thread_id,
                    "provider": parsed.provider.value,
                    "model": parsed.model,
                    "reasoning_effort": parsed.reasoning_effort,
                    **refs.to_dict(),
                }
                answer = await self._dispatch_turn(
                    tab_id,
                    prompt,
                    f"feishu-ai-{message.message_id}",
                    parsed.instruction or message.text,
                    turn_metadata,
                )

            final_text = ensure_ai_footer(answer, footer=cfg.footer)
            final_post = build_ai_post_payload(final_text, footer=cfg.footer)
            final_receipt = await user_client.reply_as_user(
                message.message_id,
                "post",
                final_post,
                reply_in_thread=reply_in_thread,
                uuid=bounded_message_uuid(message.message_id, "final"),
            )
            result = self._done_result(
                message,
                status="completed",
                parsed=parsed,
                thread_id=message.thread_id or interim_receipt.thread_id,
                refs=refs,
                interim_text=interim_text,
                interim_post=interim_post,
                final_text=final_text,
                final_post=final_post,
                ack_reaction=cfg.ack_emoji,
                done_reaction=cfg.done_emoji,
                tab_id=tab_id,
                interim_message_id=interim_receipt.message_id,
                final_message_id=final_receipt.message_id,
            )
            self._record_run(result, source=source)
            return result

        except FeishuUserScopeError as exc:
            status_val, reason_val = "scope_blocked", str(exc)
            logger.error("Feishu /ai command blocked by missing scope: %s", exc)
            result = self._done_result(message, status=status_val, reason=reason_val, parsed=parsed)
            self._record_run(result, source=source)
            return result

        except asyncio.CancelledError:
            status_val, reason_val = "cancelled", "cancelled"
            if tab_id is None:
                self._dedup.release(dedup_claim_key)
            raise

        except Exception as exc:
            status_val = "failed"
            reason_val = str(exc) or type(exc).__name__
            logger.exception("Feishu /ai command execution failed for %s", message.message_id)
            error_text = ensure_ai_footer(
                f"没能生成回答，请稍后再试或直接联系本人。（{reason_val[:160]}）",
                footer=cfg.footer,
            )
            error_post = build_ai_post_payload(error_text, footer=cfg.footer)
            try:
                final_receipt = await user_client.reply_as_user(
                    message.message_id,
                    "post",
                    error_post,
                    reply_in_thread=reply_in_thread,
                    uuid=bounded_message_uuid(message.message_id, "error"),
                )
            except Exception:
                logger.warning("Failed to post /ai error fallback reply for %s", message.message_id)
            result = self._done_result(
                message,
                status=status_val,
                reason=reason_val,
                parsed=parsed,
                final_text=error_text,
                final_post=error_post,
                tab_id=tab_id,
                interim_message_id=interim_receipt.message_id,
                final_message_id=final_receipt.message_id,
            )
            self._record_run(result, source=source)
            return result

        finally:
            if not dry_run:
                if ack_reaction_id:
                    try:
                        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                            await user_client.delete_reaction_as_user(
                                message.message_id, ack_reaction_id
                            )
                    except Exception:
                        logger.debug(
                            "Could not remove ack reaction %s on %s",
                            ack_reaction_id,
                            message.message_id,
                        )
                if status_val == "completed" and cfg.done_emoji:
                    try:
                        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                            await user_client.add_reaction_as_user(
                                message.message_id, cfg.done_emoji
                            )
                    except Exception:
                        logger.warning(
                            "Failed to add %s reaction on %s",
                            cfg.done_emoji,
                            message.message_id,
                        )
                if status_val != "cancelled":
                    self._dedup.finish(
                        dedup_claim_key,
                        status_val,
                        stage="ai_command",
                        reason=reason_val or None,
                    )

    async def handle_ws_event(self, event: FeishuMessageEvent) -> bool:
        """Handle a WebSocket ``FeishuMessageEvent`` if it matches the owner's ``/ai`` command."""
        cfg = self.get_config()
        if not self.is_owner_ai_command_candidate(
            sender_open_id=event.sender_open_id,
            sender_type="user",
            text=event.text,
            config=cfg,
        ):
            return False
        user_msg = FeishuUserMessage(
            event.message_id,
            event.chat_id,
            event.sender_open_id,
            "user",
            event.message_type,
            event.text,
            event.message_created_at_ms,
            event.thread_id,
            event.parent_id,
            event.root_id,
        )
        outcome = await self.handle_message(user_msg, source="websocket")
        return outcome.handled or outcome.status in {
            "duplicate",
            "scope_blocked",
            "failed",
        }


_DEFAULT_AI_COMMAND_SERVICE: FeishuAiCommandService | None = None


def get_feishu_ai_command_service() -> FeishuAiCommandService:
    global _DEFAULT_AI_COMMAND_SERVICE
    if _DEFAULT_AI_COMMAND_SERVICE is None:
        _DEFAULT_AI_COMMAND_SERVICE = FeishuAiCommandService()
    return _DEFAULT_AI_COMMAND_SERVICE
