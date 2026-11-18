"""Data models, provider/effort validation, and pure parsing/formatting helpers for Feishu ``/ai``."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from claude_hub.models import AgentType
from claude_hub.services.agent_stream.providers.spec import (
    ReasoningEffortKind,
    get_spec,
)
from claude_hub.services.feishu_message_format import (
    FEISHU_MESSAGE_MAX_CHARS,
    build_feishu_post_content,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage
from claude_hub.services.terminal_runtime.defaults import DEFAULT_CLAUDE_LAUNCH_ENV

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
    "default",
    *"none minimal low medium high xhigh max ultra".split(),
)
PROVIDER_SUPPORTED_REASONING_EFFORTS: dict[str, tuple[str, ...]] = {
    "claude": (),
    "codex": tuple("none minimal low medium high xhigh max ultra".split()),
    "traex": tuple("none minimal low medium high xhigh max ultra".split()),
    "maso": tuple("default none low medium high xhigh max".split()),
    "cursor": tuple("none minimal low medium high xhigh max".split()),
    "antigravity": (),
}
DEFAULT_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "claude": (
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "doubao-seed-2.0-code",
    ),
    "codex": (
        "gpt-5.4",
        "gpt-6.2-sol",
        "gpt-6.2-astra",
        "gpt-6.1-terra",
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "o3",
    ),
    "traex": (
        "Seed-Evolving",
        "Seed-2.1-Pro",
        "Seed-2.1-Turbo",
        "Seed-Code",
        "Seed-Dogfooding-2.0",
        "GPT-6-Astra",
        "GPT-5.6-Sol",
        "GPT-5.6-Terra",
        "GPT-5.6-Luna",
        "GPT-5.5",
        "GPT-5.4",
        "GPT-5.2",
        "DeepSeek-V4-Pro",
        "DeepSeek-V4-Flash",
        "Gemini-3.1-Pro-Preview",
        "Gemini-3-Flash-Preview",
    ),
    "maso": (
        "gpt-6.1-sol_reasoning_high",
        "gpt-6-astra_reasoning_high",
        "gpt56_sol_reasoning_high",
        "gemini-4-carbon_reasoning_high",
        "es1-grape-g61s_reasoning_high",
        "seed-stable-reasoning-high",
        "gemini-4-argon_reasoning_high",
        "gemini-4-argon_reasoning_medium",
        "gemini-4-argon",
    ),
    "cursor": (
        "gpt-5.3-codex-high",
        "claude-opus-4-8-thinking-high",
        "claude-opus-4-8-high",
        "claude-4.6-sonnet-medium-thinking",
        "claude-4.6-sonnet-medium",
        "claude-4.5-sonnet",
        "gpt-5.2",
        "gemini-3.7-flash-high",
        "cursor-grok-4.6-high",
    ),
    "antigravity": (
        "gemini-4-carbon-high",
        "gemini-4-carbon-medium",
        "gemini-4-carbon-low",
        "gemini-4-argon-high",
        "gemini-4-argon-medium",
        "gemini-4-argon-low",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "claude-opus-5-5-high",
        "claude-opus-5-5-medium",
        "claude-opus-5-5-low",
        "claude-sonnet-5-5-high",
        "claude-sonnet-5-5-medium",
        "claude-sonnet-5-5-low",
        "gpt-oss-120b-medium",
    ),
}
_MASO_VARIANT_SUFFIX_RE = re.compile(
    r"(?:[_-]reasoning(?:[_-](?:xhigh|high|medium|low|max))?"
    r"|[_-]thinking(?:[_-](?:xhigh|high|medium|low|max))?)$",
    re.IGNORECASE,
)


def _maso_std_variants(base: str, *, has_max: bool = True) -> dict[str, str]:
    hi, xhi = f"{base}_reasoning_high", f"{base}_reasoning_xhigh"
    out = {
        "default": base,
        "none": base,
        "low": base,
        "medium": hi,
        "high": hi,
        "xhigh": xhi,
    }
    if has_max:
        out["max"] = f"{base}_reasoning_max"
    return out


_DYNAMIC_MASO_MODEL_VARIANTS: dict[str, dict[str, str]] = {}


def register_live_maso_model_variants(options: Sequence[Any]) -> None:
    """Sync live MASO ``StreamModelOption`` variant mappings into runtime validation."""
    fresh: dict[str, dict[str, str]] = {}
    for opt in options:
        opt_id = str(getattr(opt, "id", "") or "").strip()
        if not opt_id:
            continue
        efforts = getattr(opt, "supported_reasoning_efforts", None) or ()
        variant_map: dict[str, str] = {}
        for eff in efforts:
            eff_id = str(getattr(eff, "id", "") or "").strip().lower()
            pmid = str(
                getattr(eff, "provider_model_id", None)
                or getattr(opt, "provider_model_id", None)
                or opt_id
            ).strip()
            if eff_id and pmid:
                variant_map[eff_id] = pmid
        if variant_map:
            fresh[opt_id.lower()] = variant_map
            opt_pmid = str(getattr(opt, "provider_model_id", "") or "").strip()
            if opt_pmid:
                fresh.setdefault(opt_pmid.lower(), variant_map)
            for pmid in set(variant_map.values()):
                fresh.setdefault(pmid.lower(), variant_map)
    _DYNAMIC_MASO_MODEL_VARIANTS.clear()
    _DYNAMIC_MASO_MODEL_VARIANTS.update(fresh)


_MASO_MODEL_VARIANTS: dict[str, dict[str, str]] = {
    "gpt-6.1-sol": _maso_std_variants("gpt-6.1-sol"),
    "gpt-6-astra": _maso_std_variants("gpt-6-astra"),
    "gpt56_sol": _maso_std_variants("gpt56_sol"),
    "gemini-4-carbon": {
        "none": "gemini-4-carbon",
        "low": "gemini-4-carbon",
        "medium": "gemini-4-carbon_reasoning_medium",
        "high": "gemini-4-carbon_reasoning_high",
    },
    "es1-grape-g61s": _maso_std_variants("es1-grape-g61s"),
    "gpt55": _maso_std_variants("gpt55", has_max=False),
    "gemini-4-argon": {
        "none": "gemini-4-argon",
        "low": "gemini-4-argon",
        "medium": "gemini-4-argon_reasoning_medium",
        "high": "gemini-4-argon_reasoning_high",
    },
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
    clean_model = (model or "").strip()
    if not clean_model or not effort:
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
    clean_model = (model or "").strip()
    if not clean_model or not effort:
        return clean_model
    base = _MASO_VARIANT_SUFFIX_RE.sub("", clean_model)
    base_lower = base.lower()
    dyn_variants = _DYNAMIC_MASO_MODEL_VARIANTS.get(
        clean_model.lower()
    ) or _DYNAMIC_MASO_MODEL_VARIANTS.get(base_lower)
    if dyn_variants and effort in dyn_variants:
        return dyn_variants[effort]
    if base_lower in _MASO_MODEL_VARIANTS:
        variants = _MASO_MODEL_VARIANTS[base_lower]
        if effort in variants:
            return variants[effort]
        if effort == "default":
            return variants.get("none") or variants.get("low") or base
        allowed = ", ".join(variants.keys())
        raise ValueError(
            f"MASO model {base_lower!r} does not support reasoning_effort={effort!r}; "
            f"supported efforts: {allowed}"
        )
    if dyn_variants is not None:
        allowed = ", ".join(dyn_variants.keys())
        raise ValueError(
            f"MASO model {base_lower!r} does not support reasoning_effort={effort!r}; "
            f"supported efforts: {allowed}"
        )
    if effort == "default":
        return base
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
    if resolved_effort == "default" and resolved_provider != AgentType.MASO:
        resolved_effort = ""
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
            if not resolved_model:
                resolved_effort = ""
            else:
                resolved_model = _resolve_cursor_model_with_effort(resolved_model, resolved_effort)
        elif spec.reasoning_effort == ReasoningEffortKind.MODEL_VARIANTS:
            if not resolved_model:
                resolved_effort = ""
            else:
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

    if (
        "reasoning_effort" in overrides
        and not (model_in or "").strip()
        and get_spec(target_provider).reasoning_effort
        in {ReasoningEffortKind.MODEL_ID_SUFFIX, ReasoningEffortKind.MODEL_VARIANTS}
    ):
        defaults = DEFAULT_PROVIDER_MODELS.get(target_provider.value, ())
        model_in = defaults[0] if defaults else ""

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
