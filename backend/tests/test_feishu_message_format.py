from __future__ import annotations

from typing import Any, cast

import pytest


def test_feishu_v2_provider_text_adds_delivery_format_guidance() -> None:
    from claude_hub.services.agent_stream.turn_source import (
        FEISHU_PROVIDER_TEXT_FORMAT_V2,
        format_feishu_provider_text_v2,
    )

    result = format_feishu_provider_text_v2(
        "请总结这个函数",
        app_id="cli-bot",
        chat_id="oc-chat",
        message_id="om-source",
        sender_open_id="ou-owner",
    )

    assert FEISHU_PROVIDER_TEXT_FORMAT_V2 == "feishu-v2"
    assert result.startswith(
        "Claude Hub verified message source (feishu-v2): "
        '{"app_id":"cli-bot","chat_id":"oc-chat",'
        '"message_id":"om-source","sender_open_id":"ou-owner"}'
    )
    assert "final response will be delivered to Feishu" in result
    assert "headings, lists, links, blockquotes, inline code, and fenced code blocks" in result
    assert "raw HTML, Markdown tables, or interactive cards" in result
    assert "Do not write mentions or raw Feishu tags such as <at user_id=...>" in result
    assert result.endswith("\n\n请总结这个函数")


def test_markdown_message_uses_feishu_post_md_payload() -> None:
    from claude_hub.services.feishu_message_format import (
        FeishuMessagePayload,
        build_feishu_message_payload,
    )

    markdown = "## 结果\n\n- [文档](https://example.com)\n- `状态正常`"

    payload = build_feishu_message_payload(markdown)

    assert payload == FeishuMessagePayload(
        msg_type="post",
        content={
            "zh_cn": {
                "content": [
                    [
                        {
                            "tag": "md",
                            "text": "##### 结果\n\n- [文档](https://example.com)\n" "- `状态正常`",
                        }
                    ]
                ]
            }
        },
    )


def test_build_feishu_post_content_exposes_unserialized_post_shape() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_post_content

    assert build_feishu_post_content("中文回答") == {
        "zh_cn": {"content": [[{"tag": "md", "text": "中文回答"}]]}
    }


def _message_text(payload: object) -> str:
    content = cast(dict[str, Any], payload.content)  # type: ignore[attr-defined]
    if payload.msg_type == "text":  # type: ignore[attr-defined]
        return cast(str, content["text"])
    return cast(str, content["zh_cn"]["content"][0][0]["text"])


def test_empty_message_uses_stable_placeholder() -> None:
    from claude_hub.services.feishu_message_format import (
        FEISHU_EMPTY_RESPONSE_TEXT,
        build_feishu_message_payload,
    )

    payload = build_feishu_message_payload("  \n")

    assert payload.msg_type == "post"
    assert _message_text(payload) == FEISHU_EMPTY_RESPONSE_TEXT


def test_fenced_code_block_is_preserved_in_post_markdown() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    markdown = "示例：\n\n```python\nprint('飞书')\n```"

    payload = build_feishu_message_payload(markdown)

    assert payload.msg_type == "post"
    assert _message_text(payload) == markdown


def test_headings_are_normalized_for_feishu_post_markdown() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    markdown = "# 一级\n## 二级\n### 三级\n#### 四级\n##### 五级\n###### 六级"

    payload = build_feishu_message_payload(markdown)

    assert _message_text(payload) == (
        "#### 一级\n\n##### 二级\n\n##### 三级\n\n" "#### 四级\n\n##### 五级\n\n###### 六级"
    )


def test_markdown_normalization_does_not_rewrite_fenced_code() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    markdown = "# 标题\n\n```markdown\n# code heading\n\n\nkeep spacing\n```"

    payload = build_feishu_message_payload(markdown)

    assert _message_text(payload) == (
        "#### 标题\n\n```markdown\n# code heading\n\n\nkeep spacing\n```"
    )


def test_control_character_falls_back_to_sanitized_text() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    payload = build_feishu_message_payload("safe\x00unsafe")

    assert payload.msg_type == "text"
    assert payload.content == {"text": "safe\ufffdunsafe"}


def test_unicode_joiners_preserve_emoji_and_scripts() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    text = "工程师 👨‍💻 · فارسی می‌نویسم"

    payload = build_feishu_message_payload(text)

    assert payload.msg_type == "post"
    assert _message_text(payload) == text


def test_bidi_override_falls_back_to_visible_replacement() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    payload = build_feishu_message_payload("report: safe\u202etxt.exe")

    assert payload.msg_type == "text"
    assert _message_text(payload) == "report: safe\ufffdtxt.exe"


def test_unpaired_fence_falls_back_to_text() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    markdown = "```python\nprint('missing close')"

    payload = build_feishu_message_payload(markdown)

    assert payload.msg_type == "text"
    assert _message_text(payload) == markdown


def test_feishu_mentions_are_neutralized_in_post_markdown() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    markdown = '请联系 <AT   user_id="ou-user">张三< /at > 或 <at user_id=all>所有人</AT>'

    payload = build_feishu_message_payload(markdown)
    safe_text = _message_text(payload)

    assert payload.msg_type == "post"
    assert safe_text == (
        '请联系 ＜AT   user_id="ou-user"＞张三＜ /at ＞ ' "或 ＜at user_id=all＞所有人＜/AT＞"
    )
    assert "<at" not in safe_text.lower()
    assert "</at" not in safe_text.lower()


def test_feishu_mentions_are_neutralized_in_text_fallback() -> None:
    from claude_hub.services.feishu_message_format import build_feishu_message_payload

    payload = build_feishu_message_payload("```\n<at user_id=all>all</at>")

    assert payload.msg_type == "text"
    assert _message_text(payload) == "```\n＜at user_id=all＞all＜/at＞"


def test_long_markdown_is_bounded() -> None:
    from claude_hub.services.feishu_message_format import (
        FEISHU_MESSAGE_MAX_CHARS,
        build_feishu_message_payload,
    )

    payload = build_feishu_message_payload("中" * (FEISHU_MESSAGE_MAX_CHARS + 10))
    bounded = _message_text(payload)

    assert payload.msg_type == "post"
    assert len(bounded) == FEISHU_MESSAGE_MAX_CHARS
    assert bounded.endswith("…")


def test_long_fenced_code_is_truncated_with_balanced_fence() -> None:
    from claude_hub.services.feishu_message_format import (
        FEISHU_MESSAGE_MAX_CHARS,
        build_feishu_message_payload,
    )

    markdown = "```python\n" + ("print('飞书')\n" * 2_000) + "```"

    payload = build_feishu_message_payload(markdown)
    bounded = _message_text(payload)

    assert payload.msg_type == "post"
    assert len(bounded) <= FEISHU_MESSAGE_MAX_CHARS
    assert bounded.endswith("…\n```")
    assert bounded.count("```") == 2


@pytest.mark.parametrize("closing_fence", ["~~~", "```"])
def test_truncation_after_natural_closing_fence_keeps_it_closed(
    closing_fence: str,
) -> None:
    from claude_hub.services.feishu_message_format import (
        FEISHU_MESSAGE_MAX_CHARS,
        _open_fence,
        build_feishu_message_payload,
    )

    bounded_prefix_length = FEISHU_MESSAGE_MAX_CHARS - len("\n…")
    opening_fence = f"{closing_fence}text\n"
    closing_line = f"\n{closing_fence}"
    markdown = (
        opening_fence
        + ("x" * (bounded_prefix_length - len(opening_fence) - len(closing_line)))
        + closing_line
    )
    markdown += "\ntruncated tail"

    payload = build_feishu_message_payload(markdown)
    bounded = _message_text(payload)

    assert payload.msg_type == "post"
    assert len(bounded) == FEISHU_MESSAGE_MAX_CHARS
    assert bounded.endswith(f"{closing_fence}\n…")
    assert _open_fence(bounded) is None


def test_truncation_recomputes_mixed_fence_state_at_each_boundary() -> None:
    from claude_hub.services.feishu_message_format import (
        FEISHU_MESSAGE_MAX_CHARS,
        _open_fence,
        build_feishu_message_payload,
    )

    mixed_fences = "```\n```` lang\n````\n```` lang\n````"
    for offset in range(-12, 13):
        padding = "x" * (FEISHU_MESSAGE_MAX_CHARS - len(mixed_fences) + offset)
        markdown = f"{padding}\n{mixed_fences}\n" + ("tail" * 20)

        payload = build_feishu_message_payload(markdown)
        bounded = _message_text(payload)

        assert payload.msg_type == "post", offset
        assert len(bounded) <= FEISHU_MESSAGE_MAX_CHARS, offset
        assert _open_fence(bounded) is None, offset


def test_stored_v2_turn_rebuilds_the_versioned_provider_text() -> None:
    from claude_hub.services.agent_stream.turn_source import (
        FEISHU_PROVIDER_TEXT_FORMAT_V2,
        format_feishu_provider_text_v2,
        provider_text_for_stored_turn,
    )

    metadata = {
        "origin": "feishu",
        "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V2,
        "feishu": {
            "app_id": "cli-bot",
            "chat_id": "oc-chat",
            "message_id": "om-source",
            "sender_open_id": "ou-owner",
        },
    }

    assert provider_text_for_stored_turn("edited question", metadata) == (
        format_feishu_provider_text_v2(
            "edited question",
            app_id="cli-bot",
            chat_id="oc-chat",
            message_id="om-source",
            sender_open_id="ou-owner",
        )
    )
