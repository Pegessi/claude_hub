"""Feishu personal ``/ai <instruction>`` command pipeline and configuration store.

Implements Option 1 from the Feishu ``/ai`` research design:
- Matches the owner's own ``/ai <instruction>`` messages (ignoring bots and other users),
  with optional inline overrides ``--provider``, ``--model``, and ``--effort`` /
  ``--reasoning-effort`` / ``--thinking``.
- Reuses ``FeishuMessageDedupStore`` for at-most-once claim/finish/release semantics.
- Fetches thread and chat context via ``FeishuUserClient`` and extracts Codebase MR
  references (``group/project!1234``), commit SHAs, and Feishu doc/wiki URLs.
- Manages the two-stage user reaction lifecycle (``OnIt`` -> delete -> ``DONE``).
- Posts both interim (``正在派 Subagent...``) and final replies as the user
  (``msg_type="post"``) with the ``> 🤖 AI 代答，仅供参考`` footer and idempotent UUIDs.
- Dispatches work to a Hub Chat session configured with the resolved
  ``(provider, model, reasoning_effort)`` via ``dispatch_tab_chat_and_wait``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import logging
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field, replace
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
from claude_hub.services.local_storage import document_exists, read_document, write_document
from claude_hub.services.runtime_isolation import resolve_runtime_home
from claude_hub.services.terminal_runtime.defaults import DEFAULT_CLAUDE_LAUNCH_ENV

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
DEFAULT_AI_COMMAND_PREFIX = "/ai"
DEFAULT_AI_COMMAND_ACK_EMOJI = "OnIt"
DEFAULT_AI_COMMAND_DONE_EMOJI = "DONE"
DEFAULT_AI_COMMAND_FOOTER = "> 🤖 AI 代答，仅供参考"
DEFAULT_AI_COMMAND_PROVIDER = AgentType.CLAUDE
DEFAULT_AI_COMMAND_MODEL = "doubao-seed-2.0-code"
DEFAULT_AI_COMMAND_REASONING_EFFORT = "high"
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_MAX_ACTIVE_CHATS = 10
DEFAULT_MAX_CONTEXT_MESSAGES = 20
_REACTION_TIMEOUT_SECONDS = 3.0
_MAX_RECENT_RUNS = 30

SUPPORTED_AI_COMMAND_PROVIDERS: tuple[AgentType, ...] = (
    AgentType.CLAUDE,
    AgentType.CODEX,
    AgentType.TRAEX,
    AgentType.MASO,
    AgentType.CURSOR,
    AgentType.ANTIGRAVITY,
)

SUPPORTED_REASONING_EFFORTS: tuple[str, ...] = (
    "",
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

DEFAULT_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    AgentType.CLAUDE.value: (
        "doubao-seed-2.0-code",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ),
    AgentType.CODEX.value: (
        "gpt-5.4",
        "gpt-5.3-codex",
        "o3",
    ),
    AgentType.TRAEX.value: (
        "gpt-5.4",
        "doubao-seed-2.0-code",
    ),
    AgentType.MASO.value: (
        "gemini-4-argon_reasoning_high",
        "gpt-6-astra_reasoning_high",
        "seed-stable-reasoning-high",
    ),
    AgentType.CURSOR.value: (
        "gpt-5.3-codex-high",
        "claude-4.6-sonnet-thinking",
    ),
    AgentType.ANTIGRAVITY.value: (
        "gemini-2.5-pro",
        "claude-sonnet-4-6",
    ),
}

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
    return resolved_provider, resolved_model, resolved_effort


def resolve_ai_command_agent_env(
    provider: str | AgentType,
    model: str | None = None,
    reasoning_effort: str | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the provider launch/switch environment for ``(provider, model, reasoning_effort)``."""
    resolved_provider, resolved_model, resolved_effort = validate_ai_command_agent_config(
        provider, model, reasoning_effort
    )
    env: dict[str, str] = dict(base_env or {})
    if resolved_provider == AgentType.CLAUDE and not env:
        env.update(DEFAULT_CLAUDE_LAUNCH_ENV)

    spec = get_spec(resolved_provider)
    effective_model = resolved_model

    if spec.reasoning_effort == ReasoningEffortKind.MODEL_ID_SUFFIX and resolved_effort:
        if effective_model:
            suffix = "-extra-high" if resolved_effort == "xhigh" else f"-{resolved_effort}"
            if (
                resolved_effort not in {"none", "default"}
                and not effective_model.endswith(suffix)
                and not any(
                    effective_model.endswith(f"-{eff}")
                    for eff in ("low", "medium", "high", "extra-high", "max")
                )
            ):
                effective_model = f"{effective_model}{suffix}"

    if spec.model_env_key:
        if effective_model:
            env[spec.model_env_key] = effective_model
            if resolved_provider == AgentType.CLAUDE:
                env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = effective_model
                env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = effective_model
                env["CLAUDE_CODE_SUBAGENT_MODEL"] = effective_model

    if spec.effort_env_keys:
        if resolved_effort:
            for key in spec.effort_env_keys:
                env[key] = resolved_effort
        else:
            for key in spec.effort_env_keys:
                env.pop(key, None)

    if resolved_effort:
        env["CLAUDE_HUB_REASONING_EFFORT"] = resolved_effort
        if resolved_provider == AgentType.CLAUDE:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = resolved_effort
    else:
        env.pop("CLAUDE_HUB_REASONING_EFFORT", None)
        env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)

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
        return {
            "enabled": self.enabled,
            "prefix": self.prefix,
            "owner_open_id": self.owner_open_id,
            "app_id": self.app_id,
            "provider": self.provider.value,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "cwd": self.cwd,
            "watch_chats": list(self.watch_chats),
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_active_chats": self.max_active_chats,
            "max_context_messages": self.max_context_messages,
            "ack_emoji": self.ack_emoji,
            "done_emoji": self.done_emoji,
            "footer": self.footer,
            "reply_in_thread": self.reply_in_thread,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FeishuAiCommandConfig":
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
                part.strip()
                for part in raw_watch.replace(",", " ").splitlines()
                for item in part.split()
                if item.strip()
            ]
        elif isinstance(raw_watch, (list, tuple, set)):
            watch_chats = [
                str(item).strip() for item in raw_watch if isinstance(item, str) and item.strip()
            ]
        poll_interval = float(data.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
        poll_interval = max(2.0, min(poll_interval, 300.0))
        max_active = int(data.get("max_active_chats", DEFAULT_MAX_ACTIVE_CHATS))
        max_active = max(1, min(max_active, 50))
        max_ctx = int(data.get("max_context_messages", DEFAULT_MAX_CONTEXT_MESSAGES))
        max_ctx = max(1, min(max_ctx, 100))
        ack_emoji = str(data.get("ack_emoji") or DEFAULT_AI_COMMAND_ACK_EMOJI).strip()
        done_emoji = str(data.get("done_emoji") or DEFAULT_AI_COMMAND_DONE_EMOJI).strip()
        footer = str(data.get("footer") or DEFAULT_AI_COMMAND_FOOTER).strip()
        return cls(
            enabled=bool(data.get("enabled", False)),
            prefix=prefix,
            owner_open_id=str(data.get("owner_open_id") or "").strip(),
            app_id=str(data.get("app_id") or "").strip(),
            provider=provider,
            model=model,
            reasoning_effort=effort,
            cwd=str(data.get("cwd") or "").strip(),
            watch_chats=tuple(dict.fromkeys(watch_chats)),
            poll_interval_seconds=poll_interval,
            max_active_chats=max_active,
            max_context_messages=max_ctx,
            ack_emoji=ack_emoji,
            done_emoji=done_emoji,
            footer=footer,
            reply_in_thread=bool(data.get("reply_in_thread", True)),
        )


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
        return {
            "handled": self.handled,
            "dry_run": self.dry_run,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "reason": self.reason,
            "instruction": self.parsed.instruction if self.parsed else "",
            "provider": self.parsed.provider.value if self.parsed else None,
            "model": self.parsed.model if self.parsed else None,
            "reasoning_effort": self.parsed.reasoning_effort if self.parsed else None,
            "inline_overrides": dict(self.parsed.inline_overrides) if self.parsed else {},
            "effective_env": self.parsed.effective_env if self.parsed else {},
            "refs": self.refs.to_dict(),
            "ack_reaction": self.ack_reaction,
            "done_reaction": self.done_reaction,
            "interim_text": self.interim_text,
            "interim_post": dict(self.interim_post),
            "final_text": self.final_text,
            "final_post": dict(self.final_post),
            "tab_id": self.tab_id,
            "interim_message_id": self.interim_message_id,
            "final_message_id": self.final_message_id,
        }


_LEADING_FLAG_RE = re.compile(
    r"^\s*(?:(?P<stop>--)(?:\s+|$)|"
    r"(?P<flag>--provider|--agent-type|-p|--model|-m|--effort|--reasoning-effort|--thinking|-e)"
    r"(?:=(?P<eq_val>\"[^\"]*\"|'[^']*'|\S+)|\s+(?P<sp_val>\"[^\"]*\"|'[^']*'|\S+))"
    r")(?P<tail>[\s\S]*)$",
    re.IGNORECASE,
)


def _strip_quotes(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
        return val[1:-1]
    return val


def parse_ai_command(
    text: str,
    *,
    prefix: str = DEFAULT_AI_COMMAND_PREFIX,
    default_provider: str | AgentType = DEFAULT_AI_COMMAND_PROVIDER,
    default_model: str = DEFAULT_AI_COMMAND_MODEL,
    default_reasoning_effort: str = DEFAULT_AI_COMMAND_REASONING_EFFORT,
    footer: str = DEFAULT_AI_COMMAND_FOOTER,
) -> ParsedAiCommand | None:
    """Parse an inbound message for the ``/ai`` prefix and optional inline flags.

    Supports leading flags while preserving multi-line/quoted instruction formatting:
    - ``--provider <provider>`` / ``--agent-type <provider>`` / ``-p <provider>``
    - ``--model <model>`` / ``-m <model>``
    - ``--effort <effort>`` / ``--reasoning-effort <effort>`` / ``--thinking <effort>`` / ``-e <effort>``
    """
    if not isinstance(text, str):
        return None
    if footer and footer in text:
        return None
    if DEFAULT_AI_COMMAND_FOOTER in text:
        return None

    clean_prefix = (prefix or DEFAULT_AI_COMMAND_PREFIX).strip()
    pattern = re.compile(
        rf"^\s*{re.escape(clean_prefix)}(?:\b|(?=\s)|$)(?P<rest>[\s\S]*)$",
        re.IGNORECASE,
    )
    match = pattern.match(text)
    if not match:
        return None

    rest = (match.group("rest") or "").strip()
    overrides: dict[str, str] = {}
    remaining = rest

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

    instruction = remaining

    provider_in = overrides.get("provider", default_provider)
    # If provider was overridden inline without an explicit --model, only keep default_model
    # if the provider did not change; otherwise use the new provider's primary default model.
    base_provider, _, _ = validate_ai_command_agent_config(
        default_provider, default_model, default_reasoning_effort
    )
    target_provider, _, _ = validate_ai_command_agent_config(provider_in, "", "")
    if "model" in overrides:
        model_in = overrides["model"]
    elif target_provider != base_provider:
        defaults = DEFAULT_PROVIDER_MODELS.get(target_provider.value, ())
        model_in = defaults[0] if defaults else ""
    else:
        model_in = default_model

    effort_in = overrides.get("reasoning_effort", default_reasoning_effort)
    resolved_provider, resolved_model, resolved_effort = validate_ai_command_agent_config(
        target_provider,
        model_in,
        effort_in,
    )
    return ParsedAiCommand(
        raw_text=text,
        prefix=clean_prefix,
        instruction=instruction,
        provider=resolved_provider,
        model=resolved_model,
        reasoning_effort=resolved_effort,
        inline_overrides=overrides,
    )


def extract_context_references(
    texts: Sequence[str],
    *,
    chat_name: str = "",
) -> AiCommandContextRefs:
    """Extract MR references, commit SHAs, and Feishu doc URLs from thread/chat texts."""
    combined_texts = [t for t in texts if isinstance(t, str) and t.strip()]
    mr_refs: list[str] = []
    commit_refs: list[str] = []
    doc_refs: list[str] = []

    known_repos: list[str] = []
    if chat_name:
        for repo_match in _REPO_NAME_RE.finditer(chat_name):
            candidate = repo_match.group(1)
            if not candidate.startswith(("http", "open-apis")):
                known_repos.append(candidate)

    for text in combined_texts:
        for m in _CODEBASE_MR_URL_RE.finditer(text):
            repo = m.group("repo")
            iid = m.group("iid")
            if repo not in known_repos:
                known_repos.append(repo)
            ref = f"{repo}!{iid}"
            if ref not in mr_refs:
                mr_refs.append(ref)
        for m in _REPO_BANG_MR_RE.finditer(text):
            repo = m.group("repo")
            iid = m.group("iid")
            if repo not in known_repos:
                known_repos.append(repo)
            ref = f"{repo}!{iid}"
            if ref not in mr_refs:
                mr_refs.append(ref)

    default_repo = known_repos[0] if known_repos else ""
    for text in combined_texts:
        for m in _BARE_BANG_MR_RE.finditer(text):
            iid = m.group("iid")
            if any(existing.endswith(f"!{iid}") for existing in mr_refs):
                continue
            ref = f"{default_repo}!{iid}" if default_repo else f"!{iid}"
            if ref not in mr_refs:
                mr_refs.append(ref)

    for text in combined_texts:
        for m in _COMMIT_URL_RE.finditer(text):
            sha = m.group("sha").lower()[:10]
            if sha not in commit_refs:
                commit_refs.append(sha)
        for m in _COMMIT_TEXT_RE.finditer(text):
            sha = m.group("sha").lower()[:10]
            if sha not in commit_refs:
                commit_refs.append(sha)
        for m in _HEX_SHA_RE.finditer(text):
            sha = m.group("sha").lower()
            # Filter out purely numeric strings so message IDs/timestamps are not mistaken for SHAs
            if sha.isdigit():
                continue
            short_sha = sha[:10]
            if short_sha not in commit_refs:
                commit_refs.append(short_sha)
        for m in _FEISHU_DOC_URL_RE.finditer(text):
            url = m.group(0)
            if url not in doc_refs:
                doc_refs.append(url)

    return AiCommandContextRefs(
        mr_refs=tuple(mr_refs),
        commit_refs=tuple(commit_refs),
        doc_refs=tuple(doc_refs),
    )


def ensure_ai_footer(text: str, footer: str = DEFAULT_AI_COMMAND_FOOTER) -> str:
    clean_body = text.strip()
    clean_footer = (footer or DEFAULT_AI_COMMAND_FOOTER).strip()
    if not clean_body:
        clean_body = "已完成处理。"
    if clean_footer in clean_body:
        return clean_body
    max_body = max(200, FEISHU_MESSAGE_MAX_CHARS - len(clean_footer) - 10)
    if len(clean_body) > max_body:
        clean_body = clean_body[: max_body - 1] + "…"
    return f"{clean_body}\n{clean_footer}"


def build_ai_post_payload(
    text: str,
    *,
    footer: str = DEFAULT_AI_COMMAND_FOOTER,
) -> dict[str, Any]:
    """Build a ``msg_type="post"`` content dictionary with the mandatory AI footer."""
    with_footer = ensure_ai_footer(text, footer=footer)
    try:
        return dict(build_feishu_post_content(with_footer))
    except ValueError:
        return {"zh_cn": {"content": [[{"tag": "text", "text": with_footer}]]}}


def build_interim_reply_text(
    parsed: ParsedAiCommand,
    refs: AiCommandContextRefs,
    *,
    footer: str = DEFAULT_AI_COMMAND_FOOTER,
) -> str:
    """Format the immediate ``正在派 Subagent...`` status post matching Feishu ``/ai`` UX."""
    action_desc = parsed.instruction.strip()
    if refs.mr_refs:
        primary_mr = refs.mr_refs[0]
        if refs.commit_refs:
            target = f"{primary_mr}（最新 commit {refs.commit_refs[0][:10]}）"
        else:
            target = primary_mr
        if not action_desc or "review" in action_desc.lower() or "审查" in action_desc:
            task_phrase = "做逐文件深度审查"
        else:
            short_inst = action_desc.splitlines()[0][:48]
            task_phrase = f"执行「{short_inst}」"
        body = f"正在派 Subagent 对 {target}{task_phrase}，待审查完成后执行回复。"
    else:
        short_inst = (action_desc or "当前上下文").splitlines()[0][:48]
        body = f"正在派 Subagent 处理「{short_inst}」，待完成后执行回复。"
    return ensure_ai_footer(body, footer=footer)


def bounded_message_uuid(message_id: str, stage: str) -> str:
    """Return a deterministic Feishu idempotency UUID <= 50 characters."""
    digest = hashlib.sha256(f"{message_id}:{stage}".encode("utf-8")).hexdigest()[:32]
    return f"ai:{stage[:8]}:{digest}"[:50]


def build_ai_dispatch_prompt(
    message: FeishuUserMessage,
    parsed: ParsedAiCommand,
    context_messages: Sequence[FeishuUserMessage],
    refs: AiCommandContextRefs,
) -> str:
    """Format the full prompt sent to the execution agent."""
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
        lines.append("")
        lines.append('<feishu_thread_context trust="untrusted">')
        for ctx in context_messages[-30:]:
            safe_text = ctx.text.replace("<", "&lt;").replace(">", "&gt;")
            lines.append(
                f"[{ctx.message_id} · sender={ctx.sender_open_id} · type={ctx.msg_type}] {safe_text}"
            )
        lines.append("</feishu_thread_context>")

    return "\n".join(lines)


AiTurnDispatcher = Callable[
    [str, str, str, str, Mapping[str, Any]],
    Awaitable[str],
]


class FeishuAiCommandService:
    """Coordinates ``/ai`` configuration, deduplication, context fetch, reactions, and replies."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        token_store: FeishuUserTokenStore | None = None,
        dedup_store: FeishuMessageDedupStore | None = None,
        user_client_factory: Callable[[FeishuAiCommandConfig], FeishuUserClient] | None = None,
        tab_creator: Callable[..., Any] | None = None,
        tab_getter: Callable[[str], Any] | None = None,
        tab_env_switcher: Callable[[str, dict[str, str]], Any] | None = None,
        turn_dispatcher: AiTurnDispatcher | None = None,
    ) -> None:
        runtime_home = resolve_runtime_home()
        self._state_path = state_path or (runtime_home / "feishu_ai_command.json")
        self._token_store = token_store or get_feishu_user_token_store()
        self._dedup = dedup_store or FeishuMessageDedupStore()
        self._user_client_factory = user_client_factory
        self._tab_creator = tab_creator
        self._tab_getter = tab_getter
        self._tab_env_switcher = tab_env_switcher
        self._turn_dispatcher = turn_dispatcher
        self._lock = threading.RLock()
        self._channel_locks: dict[str, asyncio.Lock] = {}

    def _load_state(self) -> dict[str, Any]:
        default_doc: dict[str, Any] = {
            "version": _STATE_VERSION,
            "config": FeishuAiCommandConfig().to_dict(),
            "watermarks": {},
            "channel_tabs": {},
            "recent_runs": [],
        }
        if not document_exists(self._state_path):
            return default_doc
        try:
            raw = read_document(self._state_path)
        except (FileNotFoundError, ValueError, OSError):
            return default_doc
        if not isinstance(raw, Mapping):
            return default_doc
        return dict(raw)

    def _save_state(self, state: Mapping[str, Any]) -> None:
        write_document(self._state_path, dict(state))

    def is_enabled(self) -> bool:
        """Fast check whether /ai command is enabled without touching token storage."""
        with self._lock:
            if not document_exists(self._state_path):
                return False
            state = self._load_state()
            raw_cfg = state.get("config")
            return bool(isinstance(raw_cfg, Mapping) and raw_cfg.get("enabled"))

    def get_config(self, *, auto_import_lark_cli: bool | None = None) -> FeishuAiCommandConfig:
        with self._lock:
            state = self._load_state()
            raw_cfg = state.get("config")
            cfg = (
                FeishuAiCommandConfig.from_mapping(raw_cfg)
                if isinstance(raw_cfg, Mapping)
                else FeishuAiCommandConfig()
            )
            should_auto_import = (
                cfg.enabled if auto_import_lark_cli is None else auto_import_lark_cli
            )
            # Auto-fill owner_open_id / app_id from token store if not explicitly set
            if not cfg.owner_open_id or not cfg.app_id:
                record = self._token_store.resolve_token(
                    open_id=cfg.owner_open_id or None,
                    app_id=cfg.app_id or settings.feishu_app_id or None,
                    auto_import_lark_cli=should_auto_import,
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
            for key, value in patch.items():
                if value is not None:
                    merged[key] = value
            updated = FeishuAiCommandConfig.from_mapping(merged)
            state["config"] = updated.to_dict()
            self._save_state(state)
            return updated

    def get_watermarks(self) -> dict[str, int]:
        with self._lock:
            state = self._load_state()
            wm = state.get("watermarks")
            if not isinstance(wm, Mapping):
                return {}
            out: dict[str, int] = {}
            for k, v in wm.items():
                if isinstance(k, str) and isinstance(v, (int, float)):
                    out[k] = int(v)
            return out

    def set_watermark(self, chat_id: str, create_time_ms: int) -> None:
        with self._lock:
            state = self._load_state()
            wm = dict(state.get("watermarks") or {})
            prev = int(wm.get(chat_id) or 0)
            if create_time_ms > prev:
                wm[chat_id] = int(create_time_ms)
                state["watermarks"] = wm
                self._save_state(state)

    def get_recent_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            state = self._load_state()
            runs = state.get("recent_runs")
            return list(runs) if isinstance(runs, list) else []

    def _record_run(self, result: AiCommandExecutionResult, *, source: str) -> None:
        with self._lock:
            state = self._load_state()
            runs = list(state.get("recent_runs") or [])
            runs.insert(
                0,
                {
                    "message_id": result.message_id,
                    "chat_id": result.chat_id,
                    "thread_id": result.thread_id,
                    "source": source,
                    "status": result.status,
                    "reason": result.reason,
                    "provider": result.parsed.provider.value if result.parsed else None,
                    "model": result.parsed.model if result.parsed else None,
                    "reasoning_effort": (result.parsed.reasoning_effort if result.parsed else None),
                    "refs": result.refs.to_dict(),
                    "tab_id": result.tab_id,
                    "at": time.time(),
                },
            )
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

    def preflight_status(
        self,
        *,
        auto_import_lark_cli: bool = True,
    ) -> FeishuScopePreflightResult:
        cfg = self.get_config()
        record = self._token_store.resolve_token(
            open_id=cfg.owner_open_id or None,
            app_id=cfg.app_id or settings.feishu_app_id or None,
            auto_import_lark_cli=auto_import_lark_cli,
        )
        client = self.build_user_client(cfg)
        return client.preflight_scopes(record)

    def capabilities_dict(self) -> dict[str, Any]:
        return {
            "providers": [p.value for p in SUPPORTED_AI_COMMAND_PROVIDERS],
            "reasoning_efforts": [e for e in SUPPORTED_REASONING_EFFORTS if e],
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
        if not cfg.enabled:
            return False
        if sender_type != "user":
            return False
        if not cfg.owner_open_id or not sender_open_id:
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
        chat_name = ""
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
                        "Failed to list Feishu thread messages for %s; falling back to chat tail",
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
                    logger.warning(
                        "Failed to list Feishu chat messages for %s",
                        message.chat_id,
                    )
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
        refs = extract_context_references(texts, chat_name=chat_name)
        return context_msgs, refs

    async def _ensure_execution_tab(
        self,
        message: FeishuUserMessage,
        parsed: ParsedAiCommand,
        cfg: FeishuAiCommandConfig,
    ) -> str:
        channel_key = f"{message.chat_id}:{message.thread_id or 'main'}:{parsed.provider.value}"
        with self._lock:
            state = self._load_state()
            channel_tabs = dict(state.get("channel_tabs") or {})
            existing_tab_id = channel_tabs.get(channel_key)

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
        tab_name = f"Feishu /ai · {short_target}"[:64]
        created = await self._create_tab(
            name=tab_name,
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

    async def _switch_tab_env(
        self,
        tab_id: str,
        env: dict[str, str],
        *,
        tab: Any = None,
    ) -> None:
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
        if inspect.isawaitable(res):
            await res

    async def _create_tab(self, **kwargs: Any) -> Any:
        if self._tab_creator is not None:
            res = self._tab_creator(**kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        from claude_hub.services import ttyd_manager

        res = ttyd_manager.create_tab(**kwargs)
        if inspect.isawaitable(res):
            return await res
        return res

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
            return AiCommandExecutionResult(
                handled=False,
                dry_run=dry_run,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                status="skipped",
                reason="disabled",
            )
        if message.sender_type != "user":
            return AiCommandExecutionResult(
                handled=False,
                dry_run=dry_run,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                status="skipped",
                reason="not_user_sender",
            )
        if not dry_run:
            if not cfg.owner_open_id.strip():
                return AiCommandExecutionResult(
                    handled=False,
                    dry_run=dry_run,
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    status="skipped",
                    reason="no_owner_open_id",
                )
            if not hmac.compare_digest(message.sender_open_id.strip(), cfg.owner_open_id.strip()):
                return AiCommandExecutionResult(
                    handled=False,
                    dry_run=dry_run,
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    status="skipped",
                    reason="sender_not_owner",
                )

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
            return AiCommandExecutionResult(
                handled=False,
                dry_run=dry_run,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                status="invalid_flags",
                reason=str(exc),
            )

        if parsed is None:
            return AiCommandExecutionResult(
                handled=False,
                dry_run=dry_run,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                status="skipped",
                reason="prefix_not_matched",
            )

        dedup_claim_key = f"ai_cmd:{cfg.owner_open_id or 'owner'}:{message.message_id}"
        if not dry_run:
            if not self._dedup.claim(dedup_claim_key):
                return AiCommandExecutionResult(
                    handled=False,
                    dry_run=dry_run,
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
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
                final_post = build_ai_post_payload(preview_final, footer=cfg.footer)
                return AiCommandExecutionResult(
                    handled=True,
                    dry_run=True,
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    status="dry_run",
                    parsed=parsed,
                    refs=refs,
                    interim_text=interim_text,
                    interim_post=interim_post,
                    final_text=preview_final,
                    final_post=final_post,
                    ack_reaction=cfg.ack_emoji,
                    done_reaction=cfg.done_emoji,
                )

            # Verify scope upfront before posting reactions/replies
            user_client.require_send_as_user_scope()

            # Stage 1: Add OnIt reaction as user
            if cfg.ack_emoji:
                try:
                    async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                        ack_reaction_id = await user_client.add_reaction_as_user(
                            message.message_id, cfg.ack_emoji
                        )
                except Exception:
                    logger.warning(
                        "Failed to add %s reaction on %s", cfg.ack_emoji, message.message_id
                    )

            # Stage 2: Fetch thread/chat context & extract MR/commit/doc references
            context_msgs, refs = await self._fetch_context_and_refs(
                user_client, message, cfg, seeded_context=seeded_context
            )

            # Stage 3: Post interim reply ('正在派 Subagent...') as user (msg_type=post)
            interim_text = build_interim_reply_text(parsed, refs, footer=cfg.footer)
            interim_post = build_ai_post_payload(interim_text, footer=cfg.footer)
            interim_receipt = await user_client.reply_as_user(
                message.message_id,
                "post",
                interim_post,
                reply_in_thread=reply_in_thread,
                uuid=bounded_message_uuid(message.message_id, "interim"),
            )

            # Stage 4: Dispatch work to Hub Chat turn (serialized per channel)
            lock_key = f"{message.chat_id}:{message.thread_id or 'main'}"
            with self._lock:
                channel_lock = self._channel_locks.setdefault(lock_key, asyncio.Lock())

            async with channel_lock:
                tab_id = await self._ensure_execution_tab(message, parsed, cfg)
                prompt = build_ai_dispatch_prompt(message, parsed, context_msgs, refs)
                turn_id = f"feishu-ai-{message.message_id}"
                turn_metadata = {
                    "origin": "feishu_ai_command",
                    "source": source,
                    "message_id": message.message_id,
                    "chat_id": message.chat_id,
                    "thread_id": message.thread_id,
                    "mr_refs": list(refs.mr_refs),
                    "commit_refs": list(refs.commit_refs),
                    "doc_refs": list(refs.doc_refs),
                    "provider": parsed.provider.value,
                    "model": parsed.model,
                    "reasoning_effort": parsed.reasoning_effort,
                }
                answer = await self._dispatch_turn(
                    tab_id,
                    prompt,
                    turn_id,
                    parsed.instruction or message.text,
                    turn_metadata,
                )

            # Stage 5: Post final reply as user (msg_type=post) with footer
            final_text = ensure_ai_footer(answer, footer=cfg.footer)
            final_post = build_ai_post_payload(final_text, footer=cfg.footer)
            final_receipt = await user_client.reply_as_user(
                message.message_id,
                "post",
                final_post,
                reply_in_thread=reply_in_thread,
                uuid=bounded_message_uuid(message.message_id, "final"),
            )

            result = AiCommandExecutionResult(
                handled=True,
                dry_run=False,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id or interim_receipt.thread_id,
                status="completed",
                parsed=parsed,
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
            status_val = "scope_blocked"
            reason_val = str(exc)
            logger.error("Feishu /ai command blocked by missing scope: %s", exc)
            result = AiCommandExecutionResult(
                handled=True,
                dry_run=False,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                status=status_val,
                reason=reason_val,
                parsed=parsed,
            )
            self._record_run(result, source=source)
            return result

        except asyncio.CancelledError:
            status_val = "cancelled"
            reason_val = "cancelled"
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
            result = AiCommandExecutionResult(
                handled=True,
                dry_run=False,
                message_id=message.message_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
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
            message_id=event.message_id,
            chat_id=event.chat_id,
            sender_open_id=event.sender_open_id,
            sender_type="user",
            msg_type=event.message_type,
            text=event.text,
            create_time_ms=event.message_created_at_ms,
            thread_id=event.thread_id,
            parent_id=event.parent_id,
            root_id=event.root_id,
        )
        outcome = await self.handle_message(user_msg, source="websocket")
        return outcome.handled or outcome.status in {"duplicate", "scope_blocked", "failed"}


_DEFAULT_AI_COMMAND_SERVICE: FeishuAiCommandService | None = None


def get_feishu_ai_command_service() -> FeishuAiCommandService:
    global _DEFAULT_AI_COMMAND_SERVICE
    if _DEFAULT_AI_COMMAND_SERVICE is None:
        _DEFAULT_AI_COMMAND_SERVICE = FeishuAiCommandService()
    return _DEFAULT_AI_COMMAND_SERVICE
