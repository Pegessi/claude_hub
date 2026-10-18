"""Pure question payload normalization for provider transports and adapters."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def parse_ask_question_response(text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse a structured ``ask_question_response`` composer payload.

    The approval card submits answers as a JSON string of the form
    ``{"type": "ask_question_response", "answers": [{"questionId",
    "selected": [...]}]}``. Returns the answers list when ``text`` is such a
    payload, else ``None`` so the caller can fall through to normal delivery.
    """
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "ask_question_response":
        return None
    answers = parsed.get("answers")
    if not isinstance(answers, list):
        return None
    return answers


def codex_normalize_questions(raw: Any) -> List[Dict[str, Any]]:
    """Map Codex ``requestUserInput`` questions to the shared card shape.

    Codex sends ``{id, header, question, options: [{label, ...}],
    multiSelect}``; the approval card expects ``{id, prompt,
    options: [{id, label}], allow_multiple}``. Option ids are the labels
    themselves, so a selected label is also the answer value.

    Shared by the transport and adapter so
    ``CodexNativeSession._handle_server_request`` can reuse the exact same
    skip rules to detect a request that yields zero actionable questions and
    auto-dismiss it — keeping the "what makes a question card-worthy" logic in
    one place. The adapter imports and reuses this for card emission.
    """
    if not isinstance(raw, list):
        return []
    questions: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question_id = item.get("id")
        if not isinstance(question_id, str) or not question_id:
            continue
        prompt = item.get("question")
        if not isinstance(prompt, str) or not prompt:
            prompt = item.get("header")
        if not isinstance(prompt, str) or not prompt:
            continue
        raw_options = item.get("options")
        options: List[Dict[str, str]] = []
        if isinstance(raw_options, list):
            for opt in raw_options:
                if not isinstance(opt, dict):
                    continue
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option = {"id": label, "label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                options.append(option)
        # Modern app-server schemas explicitly allow null options for a
        # free-text question. Only reject malformed supplied options; a
        # missing, null, or empty list still produces an actionable card.
        if raw_options is not None and (
            not isinstance(raw_options, list) or (raw_options and not options)
        ):
            continue
        question: Dict[str, Any] = {
            "id": question_id,
            "prompt": prompt,
            "options": options,
            "allow_multiple": item.get("multiSelect") is True,
        }
        if item.get("isSecret") is True:
            question["is_secret"] = True
        questions.append(question)
    return questions
