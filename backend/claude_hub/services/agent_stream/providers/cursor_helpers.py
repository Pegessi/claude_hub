"""Helper functions for Cursor persistent ``agent acp`` sessions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Tuple

_ACP_PRELOAD_PATH = Path(__file__).with_name("cursor_acp_preload.cjs").resolve()


def _inject_acp_preload_env(provider_env: Dict[str, str]) -> Dict[str, str]:
    """Attach ``cursor_acp_preload.cjs`` via ``NODE_OPTIONS`` for ``agent acp``.

    Cursor CLI 2026.11.11's ``./src/acp/agent-session.ts`` omits
    ``enableAgentRetries: true`` when calling ``agentClient.run``, causing
    transient ``RetriableError: [resource_exhausted]`` and stream blips to fail
    on ``attempt=0`` instead of resuming from the latest checkpoint via
    ``resumeAction``. The preload enables checkpoint retries on the ACP
    ``runOptions`` object and immediately scrubs itself from ``NODE_OPTIONS``
    so child tool shells do not inherit it.
    """
    if not _ACP_PRELOAD_PATH.is_file():
        return provider_env
    path_str = str(_ACP_PRELOAD_PATH)
    req_flag = f'--require="{path_str}"' if " " in path_str else f"--require={path_str}"
    existing = provider_env.get("NODE_OPTIONS", "").strip()
    if req_flag in existing or path_str in existing:
        return provider_env
    merged = dict(provider_env)
    merged["NODE_OPTIONS"] = f"{existing} {req_flag}".strip() if existing else req_flag
    return merged


_CONTENT_POLICY_REFUSAL_SUBSTR = "The provider refused to serve this request based on the content"
_CONTENT_POLICY_HINT = (
    " (Cursor/upstream model content safety filter rejected this prompt or context — "
    "not a local login expiry; switch models or start a fresh session to trim context)"
)

_CURSOR_EFFORT_SUFFIXES: Tuple[str, ...] = (
    "extra-high",
    "minimal",
    "medium",
    "xhigh",
    "high",
    "none",
    "low",
    "max",
)
_CURSOR_BRACKET_RE = re.compile(r"^([^[\]]+)\[(.*)\]$")
_CURSOR_LEGACY_CLAUDE_RE = re.compile(r"^claude-(\d+)\.(\d+)-(sonnet|opus|haiku)$")
_CURSOR_SHORT_CLAUDE_RE = re.compile(r"^(sonnet|opus|haiku)-(\d+)\.(\d+)$")


def _parse_cursor_model_spec(
    raw_model: str, available_base_ids: AbstractSet[str]
) -> Tuple[Optional[str], Dict[str, str]]:
    """Resolve a ``CURSOR_MODEL`` CLI slug or bracket override against ACP's
    parameterized base model catalog.

    ``agent --list-models`` emits flat legacy slugs (e.g.
    ``gemini-4-carbon-high``, ``cursor-grok-4.6-high-fast``, ``gpt-5.3-codex-xhigh``,
    ``sonnet-4.6-1m-thinking-high``, ``auto``), whereas ``agent acp`` with
    ``parameterizedModelPicker=True`` splits selection into a base model ID
    (``gemini-4-carbon``, ``grok-4.6``, ``gpt-5.3-codex``, ``claude-sonnet-4-6``,
    ``auto-smart``) plus ``session/set_config_option`` parameter values
    (``reasoning_effort``, ``reasoning``, ``effort``, ``fast``, ``thinking``,
    ``context``, ``optimize_for``).
    """
    spec = raw_model.strip()
    if not spec or not available_base_ids:
        return None, {}

    explicit_params: Dict[str, str] = {}
    has_brackets = False
    bracket = _CURSOR_BRACKET_RE.fullmatch(spec)
    if bracket is not None:
        has_brackets = True
        spec = bracket.group(1).strip()
        inner = bracket.group(2).strip()
        if inner:
            for part in inner.split(","):
                if "=" in part:
                    key, val = part.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if key and val:
                        explicit_params[key] = val

    if spec in available_base_ids:
        params = dict(explicit_params)
        if not has_brackets:
            # Bare composer-2 defaults to fast=true in Cursor's model catalog.
            default_fast = "true" if spec.startswith("composer-2") else "false"
            params.setdefault("_fast_default", default_fast)
            params.setdefault("_effort_default", "medium")
            params.setdefault("_thinking_default", "false")
            params.setdefault("_context_default", "default")
        return spec, params

    if spec == "auto" and "auto-smart" in available_base_ids:
        return "auto-smart", dict(explicit_params)

    rem = spec
    fast: Optional[str] = None
    thinking: Optional[str] = None
    effort: Optional[str] = None
    context: Optional[str] = None

    if rem.endswith("-fast") and rem not in available_base_ids:
        fast = "true"
        rem = rem[:-5]
    elif rem.endswith("-standard") and rem not in available_base_ids:
        fast = "false"
        rem = rem[:-9]

    if rem.endswith("-thinking") and rem not in available_base_ids:
        thinking = "true"
        rem = rem[:-9]

    if rem.endswith("-1m") and rem not in available_base_ids:
        context = "1m"
        rem = rem[:-3]

    for eff in _CURSOR_EFFORT_SUFFIXES:
        if rem.endswith(f"-{eff}") and rem not in available_base_ids:
            effort = eff
            rem = rem[: -(len(eff) + 1)]
            break

    if rem.endswith("-thinking") and rem not in available_base_ids:
        thinking = "true"
        rem = rem[:-9]

    if rem.endswith("-1m") and rem not in available_base_ids:
        context = "1m"
        rem = rem[:-3]

    candidates: List[str] = [rem]
    if rem.startswith("cursor-"):
        candidates.append(rem[len("cursor-") :])
    claude_match = _CURSOR_LEGACY_CLAUDE_RE.fullmatch(rem)
    if claude_match is not None:
        candidates.append(
            f"claude-{claude_match.group(3)}-{claude_match.group(1)}-{claude_match.group(2)}"
        )
    short_claude_match = _CURSOR_SHORT_CLAUDE_RE.fullmatch(rem)
    if short_claude_match is not None:
        candidates.append(
            f"claude-{short_claude_match.group(1)}-{short_claude_match.group(2)}-{short_claude_match.group(3)}"
        )

    base_id = next((cand for cand in candidates if cand in available_base_ids), None)
    if base_id is None:
        return None, {}

    params = dict(explicit_params)
    if fast is not None:
        params.setdefault("fast", fast)
    elif not has_brackets:
        default_fast = "true" if base_id.startswith("composer-2") else "false"
        params.setdefault("_fast_default", default_fast)
    if thinking is not None:
        params.setdefault("thinking", thinking)
    elif not has_brackets and (
        effort is not None or claude_match is not None or short_claude_match is not None
    ):
        params.setdefault("_thinking_default", "false")
    if context is not None:
        params.setdefault("context", context)
    elif not has_brackets:
        params.setdefault("_context_default", "default")
    if effort is not None:
        params.setdefault("_effort", effort)
    elif not has_brackets:
        params.setdefault("_effort_default", "medium")
    return base_id, params


def _resolve_config_option_updates(
    params: Mapping[str, str], config_options: Sequence[Dict[str, Any]]
) -> List[Tuple[str, str]]:
    """Determine ``(configId, value)`` updates needed to satisfy ``params``."""
    by_id: Dict[str, Dict[str, Any]] = {
        opt["id"]: opt
        for opt in config_options
        if isinstance(opt, dict) and isinstance(opt.get("id"), str)
    }
    updates: List[Tuple[str, str]] = []

    def _valid_values(opt: Dict[str, Any]) -> set[str]:
        raw_opts = opt.get("options")
        if not isinstance(raw_opts, list):
            return set()
        return {
            item["value"]
            for item in raw_opts
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        }

    for key, val in params.items():
        if key.startswith("_") or key in ("model", "mode"):
            continue
        opt = by_id.get(key)
        if opt is None:
            continue
        if val in _valid_values(opt) and opt.get("currentValue") != val:
            updates.append((key, val))

    effort = params.get("_effort") or params.get("_effort_default")
    if effort:
        for eff_key in ("reasoning_effort", "reasoning", "effort"):
            if eff_key in params:
                break
            opt = by_id.get(eff_key)
            if opt is None:
                continue
            valid = _valid_values(opt)
            target: Optional[str] = None
            if effort in valid:
                target = effort
            elif effort in ("xhigh", "extra-high"):
                for alias in ("xhigh", "extra-high", "max"):
                    if alias in valid:
                        target = alias
                        break
            elif effort == "max":
                for alias in ("max", "xhigh", "extra-high"):
                    if alias in valid:
                        target = alias
                        break
            if target is not None and opt.get("currentValue") != target:
                updates.append((eff_key, target))
            break

    if "thinking" not in params and "_thinking_default" in params:
        opt = by_id.get("thinking")
        if opt is not None:
            target = params["_thinking_default"]
            if target in _valid_values(opt) and opt.get("currentValue") != target:
                updates.append(("thinking", target))

    if "fast" not in params and "_fast_default" in params:
        opt = by_id.get("fast")
        if opt is not None:
            target = params["_fast_default"]
            if target in _valid_values(opt) and opt.get("currentValue") != target:
                updates.append(("fast", target))

    if "context" not in params and "_context_default" in params:
        opt = by_id.get("context")
        if opt is not None:
            target = params["_context_default"]
            if target in _valid_values(opt) and opt.get("currentValue") != target:
                updates.append(("context", target))

    return updates


def _cursor_config_root(env: Dict[str, str], cwd: str) -> Path:
    """Resolve Cursor's config directory matching ``cursor-config/dist/paths.js``."""
    from ..._cursor_verify import resolve_cursor_roots

    _, acp_root = resolve_cursor_roots(env=env, cwd=cwd)
    return acp_root


def _ensure_acp_meta_file(target_dir: Path, cwd: str) -> None:
    meta_path = target_dir / "meta.json"
    if meta_path.is_symlink():
        raise OSError("refusing to write symlinked meta.json")
    if meta_path.exists():
        return
    real_cwd = os.path.realpath(cwd)
    tmp_meta = target_dir / f"meta.json.tmp-{uuid.uuid4().hex}"
    try:
        tmp_meta.write_text(
            json.dumps({"schemaVersion": 1, "cwd": real_cwd}),
            encoding="utf-8",
        )
        tmp_meta.replace(meta_path)
    finally:
        tmp_meta.unlink(missing_ok=True)


def _migrate_legacy_cli_store_for_acp(sid: str, cwd: str, env: Dict[str, str]) -> bool:
    """Copy a verified legacy ``~/.cursor/chats/<md5(cwd)>/.../store.db`` into
    ``<config_root>/acp-sessions/<sid>/`` when upgrading an existing Chat tab.

    ``agent --print --resume <sid>`` and ``agent acp`` share the exact same
    SQLite blob-store format (``store.db`` + ``meta.json``), differing only in
    their parent directory.  Never follows symlinks or overwrites an existing
    ACP store, and uses SQLite online backup so uncheckpointed ``-wal`` frames
    are preserved atomically.
    """
    if not sid or not cwd or "/" in sid or "\\" in sid or sid in {".", ".."}:
        return False
    from ..._cursor_verify import (
        _cwd_hash,
        _expand_with_home,
        _read_agent_id_from_db,
        resolve_cursor_roots,
    )

    chats_root, config_root = resolve_cursor_roots(env=env, cwd=cwd)
    acp_sessions = config_root / "acp-sessions"
    target_dir = acp_sessions / sid
    target_db = target_dir / "store.db"
    try:
        if (
            config_root.is_symlink()
            or acp_sessions.is_symlink()
            or target_dir.is_symlink()
            or target_db.is_symlink()
        ):
            return False
        if target_db.exists():
            if (
                not target_db.is_file()
                or target_db.resolve().parent.parent != acp_sessions.resolve()
            ):
                return False
            _ensure_acp_meta_file(target_dir, cwd)
            return True
    except OSError:
        return False

    raw_home = env.get("HOME")
    effective_home = Path(raw_home).expanduser() if raw_home else Path.home()
    data_dir = env.get("CURSOR_DATA_DIR")
    if isinstance(data_dir, str) and data_dir.strip():
        try:
            resolved_data = _expand_with_home(data_dir, effective_home, cwd)
            if resolved_data.resolve() != chats_root.resolve():
                return False
        except OSError:
            return False

    bucket = chats_root / "chats" / _cwd_hash(cwd)
    tmp_db: Optional[Path] = None
    try:
        if not bucket.is_dir() or bucket.is_symlink():
            return False
        matched_db: Optional[Path] = None
        for index, chat_dir in enumerate(bucket.iterdir()):
            if index >= 1000:
                return False
            db = chat_dir / "store.db"
            if chat_dir.is_symlink() or db.is_symlink() or not db.is_file():
                continue
            if db.resolve().parent.parent != bucket.resolve():
                continue
            if _read_agent_id_from_db(db) == sid:
                if matched_db is not None:
                    # Ambiguous duplicate stores — fail closed.
                    return False
                matched_db = db
        if matched_db is None:
            return False
        acp_sessions.mkdir(parents=True, exist_ok=True)
        if acp_sessions.is_symlink() or target_dir.is_symlink():
            return False
        target_dir.mkdir(exist_ok=True)
        if target_dir.is_symlink() or target_dir.resolve().parent != acp_sessions.resolve():
            return False
        tmp_db = target_dir / f"store.db.migrate-tmp-{uuid.uuid4().hex}"
        src_uri = matched_db.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(src_uri, uri=True) as src_conn:
            with sqlite3.connect(str(tmp_db)) as dst_conn:
                src_conn.backup(dst_conn)
        if target_db.is_symlink():
            return False
        tmp_db.replace(target_db)
        _ensure_acp_meta_file(target_dir, cwd)
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        if tmp_db is not None:
            try:
                tmp_db.unlink(missing_ok=True)
            except OSError:
                pass


def _format_acp_error_text(text: str) -> str:
    cleaned = text.strip()
    if _CONTENT_POLICY_REFUSAL_SUBSTR in cleaned and _CONTENT_POLICY_HINT not in cleaned:
        return f"{cleaned}{_CONTENT_POLICY_HINT}"
    return cleaned


def _extract_permission_detail(tool_call: Dict[str, Any]) -> str:
    content = tool_call.get("content")
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "content":
            inner = item.get("content")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                text = inner["text"].strip()
                if text:
                    parts.append(text)
        elif itype == "diff":
            path = item.get("path")
            if isinstance(path, str) and path.strip():
                parts.append(f"Path: {path.strip()}")
    return "\n".join(parts)


