"""Pure model-catalog transformations; probing and caching stay in native."""

from __future__ import annotations

import re
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ....models import StreamModelOption, StreamReasoningEffortOption


def model_options_from_ids(model_ids: Sequence[str]) -> List[StreamModelOption]:
    """Build fresh model options without retaining a catalog or cache."""
    return [
        StreamModelOption(id=model_id, label=model_id, description="") for model_id in model_ids
    ]


def codex_cache_rows(payload: Any) -> List[Dict[str, Any]]:
    """Translate the CLI cache schema into the app-server model/list schema."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []
    rows = []
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        raw_efforts = item.get("supported_reasoning_levels")
        rows.append(
            {
                "model": item.get("slug"),
                "displayName": item.get("display_name"),
                "description": item.get("description"),
                "hidden": item.get("visibility") != "list" or item.get("supported_in_api") is False,
                "defaultReasoningEffort": item.get("default_reasoning_level"),
                "supportedReasoningEfforts": [
                    {
                        "reasoningEffort": effort.get("effort"),
                        "description": effort.get("description"),
                    }
                    for effort in (raw_efforts if isinstance(raw_efforts, list) else [])
                    if isinstance(effort, dict)
                ],
            }
        )
    return rows


def parse_codex_models(
    data: Sequence[Any],
    *,
    reasoning_efforts: AbstractSet[str],
    reasoning_labels: Mapping[str, str],
) -> List[StreamModelOption]:
    """Parse highest-priority rows first, including hidden-row exclusions."""
    options: List[StreamModelOption] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        model = item.get("model")
        if not isinstance(model, str) or not model.strip() or model in seen:
            continue
        seen.add(model)
        if item.get("hidden") is True:
            continue
        efforts: List[StreamReasoningEffortOption] = []
        seen_efforts: set[str] = set()
        raw_efforts = item.get("supportedReasoningEfforts")
        for raw in raw_efforts if isinstance(raw_efforts, list) else []:
            if not isinstance(raw, dict):
                continue
            effort = raw.get("reasoningEffort")
            if (
                not isinstance(effort, str)
                or effort not in reasoning_efforts
                or effort in seen_efforts
            ):
                continue
            seen_efforts.add(effort)
            description = raw.get("description")
            efforts.append(
                StreamReasoningEffortOption(
                    id=effort,
                    label=reasoning_labels[effort],
                    description=description if isinstance(description, str) else "",
                )
            )
        default_effort = item.get("defaultReasoningEffort")
        if not isinstance(default_effort, str) or default_effort not in seen_efforts:
            default_effort = None
        label = item.get("displayName")
        description = item.get("description")
        options.append(
            StreamModelOption(
                id=model,
                label=label if isinstance(label, str) and label else model,
                description=description if isinstance(description, str) else "",
                provider_model_id=model,
                default_reasoning_effort=default_effort,
                supported_reasoning_efforts=efforts,
            )
        )
    return options


def claude_env_options(
    env: Mapping[str, str], *, model_env_keys: Sequence[str]
) -> List[StreamModelOption]:
    """Build the Claude catalog advertised by one tab's launch env."""
    model_ids: List[str] = []
    seen: set[str] = set()
    for key in model_env_keys:
        model_id = env.get(key, "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        model_ids.append(model_id)
    return [
        StreamModelOption(id=model_id, label=model_id, description="") for model_id in model_ids
    ]


def parse_list_models(
    raw: str, *, osc_re: re.Pattern[str], ansi_re: re.Pattern[str], marker_re: re.Pattern[str]
) -> List[StreamModelOption]:
    """Parse ``agent --list-models`` output into model options.

    Each non-empty line is ``<id>  - <label>`` with optional ANSI colors and
    trailing ``(default)``/``(current)`` markers. Malformed lines are skipped;
    ids are deduped. An empty result signals the caller to fall back.
    """
    options: List[StreamModelOption] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = osc_re.sub("", line)
        line = ansi_re.sub("", line).strip()
        if not line or " - " not in line:
            continue
        model_id, label = line.split(" - ", 1)
        model_id = model_id.strip()
        label = marker_re.sub("", label.strip()).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        options.append(StreamModelOption(id=model_id, label=label or model_id, description=""))
    return options


_ANTIGRAVITY_MODEL_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")


def parse_antigravity_models(
    raw: str, *, osc_re: re.Pattern[str], ansi_re: re.Pattern[str], marker_re: re.Pattern[str]
) -> List[StreamModelOption]:
    """Parse ``agyx models`` output into model options.

    Each non-empty catalog row is ``<id>\\t<label>`` (when piped) or
    ``<id>  <label>`` (two or more spaces in TTY mode), with optional ANSI
    sequences or carriage-return spinner frames. Malformed lines are skipped;
    ids are deduped. An empty result signals the caller to fall back.
    """
    options: List[StreamModelOption] = []
    seen: set[str] = set()
    for raw_line in raw.split("\n"):
        cleaned = ansi_re.sub("", osc_re.sub("", raw_line))
        segments = [seg.strip() for seg in cleaned.split("\r") if seg.strip()]
        if not segments:
            continue
        line = segments[-1]
        if "\t" in line:
            model_id, label = line.split("\t", 1)
        else:
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            if len(parts) != 2:
                continue
            model_id, label = parts
        model_id = model_id.strip()
        label = marker_re.sub("", label.strip()).strip()
        if not model_id or not _ANTIGRAVITY_MODEL_ID_RE.fullmatch(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        options.append(StreamModelOption(id=model_id, label=label or model_id, description=""))
    return options


def group_cursor_model_options(
    options: Sequence[StreamModelOption],
    *,
    reasoning_efforts: AbstractSet[str],
    reasoning_labels: Mapping[str, str],
) -> List[StreamModelOption]:
    """Collapse Cursor's parameterized model ids into model + effort groups.

    Cursor exposes effort as part of the provider model id (for example
    ``[redacted]-high``), unlike Codex-family app servers which accept model
    and effort separately.  Capabilities normalize both representations while
    retaining the exact Cursor id on each effort option.
    """

    grouped: Dict[Tuple[str, bool], StreamModelOption] = {}
    ordered: List[StreamModelOption] = []
    effort_suffixes = sorted(reasoning_efforts, key=len, reverse=True)
    candidates: List[Tuple[StreamModelOption, str, bool, Optional[str]]] = []
    evidence: Dict[Tuple[str, bool], set[str]] = {}
    provider_ids = {option.id for option in options}
    for option in options:
        provider_id = option.id
        fast = provider_id.endswith("-fast")
        stem = provider_id[:-5] if fast else provider_id
        effort: Optional[str] = None
        # Older Cursor catalogs spell xhigh as ``extra-high`` in some ids.
        if stem.endswith("-extra-high"):
            effort = "xhigh"
            stem = stem[:-11]
        else:
            for candidate in effort_suffixes:
                if stem.endswith(f"-{candidate}"):
                    effort = candidate
                    stem = stem[: -(len(candidate) + 1)]
                    break
        candidates.append((option, stem, fast, effort))
        if effort is not None:
            evidence.setdefault((stem, fast), set()).add(effort)

    for option, stem, fast, effort in candidates:
        provider_id = option.id
        base_id = f"{stem}-fast" if fast else stem
        variants = evidence.get((stem, fast), set())
        # A suffix such as ``-medium`` may be part of a real model name. Only
        # collapse it when the catalog proves this is an effort family.
        is_family = len(variants) >= 2 or base_id in provider_ids
        if effort is None or not is_family:
            standalone = option.model_copy(update={"provider_model_id": provider_id})
            ordered.append(standalone)
            continue

        group_id = base_id
        key = (stem, fast)
        group = grouped.get(key)
        if group is None:
            effort_labels = "|".join(
                re.escape(label)
                for label in ("Extra High", "Minimal", "Medium", "High", "None", "Low", "Max")
            )
            label = re.sub(
                rf"\s+(?:{effort_labels})(?=\s+Thinking$|$)",
                "",
                option.label,
                flags=re.IGNORECASE,
            )
            if fast and not label.lower().endswith(" fast"):
                label = f"{label} Fast"
            group = StreamModelOption(
                id=group_id,
                label=label,
                description=option.description,
                provider_model_id=provider_id,
                default_reasoning_effort=effort,
                supported_reasoning_efforts=[],
            )
            grouped[key] = group
            ordered.append(group)
        group.supported_reasoning_efforts.append(
            StreamReasoningEffortOption(
                id=effort,
                label=reasoning_labels[effort],
                provider_model_id=provider_id,
            )
        )
        # Prefer medium, then high, as the representative id used by Default.
        if effort == "medium" or (effort == "high" and group.default_reasoning_effort != "medium"):
            group.default_reasoning_effort = effort
            group.provider_model_id = provider_id
    grouped_ids = {item.id for item in grouped.values()}
    standalone_by_id = {item.id: item for item in ordered if not item.supported_reasoning_efforts}
    for group in grouped.values():
        default_option = standalone_by_id.get(group.id)
        if default_option is not None:
            group.provider_model_id = default_option.provider_model_id
            group.default_reasoning_effort = "medium"
            if not any(item.id == "medium" for item in group.supported_reasoning_efforts):
                group.supported_reasoning_efforts.append(
                    StreamReasoningEffortOption(
                        id="medium",
                        label=reasoning_labels["medium"],
                        provider_model_id=default_option.provider_model_id,
                    )
                )
    return [
        item for item in ordered if item.supported_reasoning_efforts or item.id not in grouped_ids
    ]
