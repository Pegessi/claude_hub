"""Pure-Python protobuf wire encoder/decoder for the Feishu web gateway & msg-frontier protocol.

Ported from ``suyue.rd/persona-extensions`` ``extensions/lark-desk/internal/web/codec.go``
(and schema additions in ``feed.go``, ``content.go``, ``media.go``, ``reactions.go``,
``threads.go``, ``calendar.go``). Speaks the exact ``proto2`` wire format used by the
Feishu web client gateway (``/im/gateway/``) and ``msg-frontier`` WebSocket without
requiring external protobuf C/Python runtime packages.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# FieldDescriptorProto.Type constants used in protocol_schema.json
TYPE_INT64 = 3
TYPE_UINT64 = 4
TYPE_INT32 = 5
TYPE_BOOL = 8
TYPE_STRING = 9
TYPE_BYTES = 12
TYPE_UINT32 = 13

# Protobuf wire types
WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5

_VARINT_TYPES = {TYPE_INT64, TYPE_UINT64, TYPE_INT32, TYPE_BOOL, TYPE_UINT32}
_MASK_64 = (1 << 64) - 1
_SIGN_64 = 1 << 63
_MASK_32 = (1 << 32) - 1
_SIGN_32 = 1 << 31


@dataclass(frozen=True)
class Command:
    """One Feishu web gateway command descriptor."""

    cmd: int
    request: str
    response: str


@dataclass(frozen=True)
class _FieldDef:
    name: str
    number: int
    kind: int | str | tuple[str, int, int | str]
    repeated: bool


_PUSH_SCHEMA: dict[str, list[list[Any]]] = {
    "ExtendedEntry": [["key", 1, 9, False], ["value", 2, 9, False]],
    "Frame": [
        ["seqid", 1, 4, False],
        ["logid", 2, 4, False],
        ["service", 3, 5, False],
        ["method", 4, 5, False],
        ["headers", 5, "ExtendedEntry", True],
        ["payloadEncoding", 6, 9, False],
        ["payloadType", 7, 9, False],
        ["payload", 8, 12, False],
    ],
    "PushMessage": [
        ["id", 1, 9, False],
        ["type", 2, 5, False],
        ["fromId", 3, 9, False],
        ["createTime", 4, 3, False],
        ["content", 5, 12, False],
        ["status", 6, 5, False],
        ["rootId", 8, 9, False],
        ["parentId", 9, 9, False],
        ["chatId", 10, 9, False],
        ["position", 13, 5, False],
        ["threadId", 20, 9, False],
        ["isRemoved", 23, 8, False],
        ["chatType", 46, 5, False],
    ],
    "PushMessagesRequest": [
        ["messages", 1, ["map", 9, "PushMessage"], True],
        ["messagesAtMe", 9, ["map", 9, 8], True],
    ],
}

_FEED_SCHEMA: dict[str, list[list[Any]]] = {
    "lark_feed_PullFeedCardsRequest": [
        ["type", 1, 5, False],
        ["pullType", 2, 5, False],
        ["cursor", 3, 3, False],
        ["count", 4, 5, False],
        ["parentCardId", 5, 3, False],
        ["withMessageEntity", 10, 8, False],
    ],
    "lark_feed_PullFeedCardsResponse": [
        ["chats", 1, ["map", 9, "lark_entities_Chat"], True],
        ["messages", 2, ["map", 9, "lark_entities_Message"], True],
        ["cards", 3, "lark_entities_FeedCard", True],
        ["nextCursor", 4, 3, False],
    ],
    "lark_entities_FeedCard": [
        ["id", 1, 9, False],
        ["type", 2, 5, False],
        ["updateTime", 3, 3, False],
        ["parentCardId", 5, 3, False],
        ["rankTime", 6, 3, False],
        ["feedType", 7, 5, False],
        ["updateTimeMs", 9, 3, False],
    ],
    "lark_entities_Chat": [
        ["lastMessageId", 3, 9, False],
        ["updateTime", 9, 3, False],
        ["isRemind", 27, 8, False],
        ["isDissolved", 30, 8, False],
        ["isP2pAi", 144, 8, False],
        ["isP2pOfficialBotAi", 147, 8, False],
    ],
}

_FEED_COMMANDS: dict[str, tuple[int, str, str]] = {
    "PullFeedCardsRequest": (
        1000,
        "lark_feed_PullFeedCardsRequest",
        "lark_feed_PullFeedCardsResponse",
    ),
}

_CONTENT_SCHEMA: dict[str, list[list[Any]]] = {
    "lark_entities_Message": [["status", 6, 5, False], ["isRemoved", 23, 8, False]],
    "lark_entities_FileContent": [
        ["key", 1, 9, False],
        ["name", 2, 9, False],
        ["size", 3, 3, False],
    ],
    "lark_entities_MediaContent": [["name", 2, 9, False], ["duration", 7, 5, False]],
    "lark_entities_AudioContent": [
        ["duration", 2, 5, False],
        ["audioRecognition", 4, "lark_entities_AudioRecognition", False],
    ],
    "lark_entities_AudioRecognition": [["recognitionResult", 1, 9, False]],
    "lark_entities_ShareUserCardContent": [["userId", 1, 9, False], ["name", 3, 9, False]],
    "lark_entities_ShareGroupChatContent": [["chat", 1, "lark_entities_Chat", False]],
    "lark_entities_ShareCalendarEventContent": [
        ["calendarEvent", 1, "lark_entities_CalendarEvent", False]
    ],
    "lark_entities_CalendarEvent": [["summary", 29, 9, False]],
    "lark_entities_LocationContent": [
        ["locationDescription", 1, "lark_entities_LocationContent_LocationDescription", False]
    ],
    "lark_entities_LocationContent_LocationDescription": [
        ["name", 1, 9, False],
        ["description", 2, 9, False],
    ],
    "lark_entities_MergeForwardContent": [
        ["messages", 1, "lark_entities_Message", True],
        ["chatType", 2, 5, False],
        ["groupChatName", 3, 9, False],
        ["p2pCreatorName", 4, 9, False],
        ["p2pPartnerName", 5, 9, False],
    ],
    "lark_entities_CardContent": [
        ["type", 1, 5, False],
        ["richText", 2, "lark_entities_RichText", False],
        ["cardHeader", 8, "lark_entities_CardContent_CardHeader", False],
        ["jsonCard", 20, 9, False],
        ["cardId", 25, 9, False],
    ],
    "lark_entities_CardContent_CardHeader": [
        ["title", 1, 9, False],
        ["subtitle", 7, 9, False],
        ["mainTitle", 10, 9, False],
    ],
    "lark_entities_VideoChatContent": [
        ["type", 1, 5, False],
        ["meetingCard", 2, "lark_entities_VideoChatContent_MeetingCard", False],
    ],
    "lark_entities_VideoChatContent_MeetingCard": [
        ["meetingId", 1, 9, False],
        ["status", 2, 5, False],
        ["startTimeMs", 3, 3, False],
        ["endTimeMs", 4, 3, False],
        ["topic", 5, 9, False],
        ["sponsorId", 6, 9, False],
        ["meetNumber", 8, 9, False],
        [
            "participants",
            9,
            "lark_entities_VideoChatContent_MeetingCard_MeetingParticipant",
            True,
        ],
    ],
    "lark_entities_VideoChatContent_MeetingCard_MeetingParticipant": [["userId", 1, 9, False]],
    "lark_entities_SystemContent": [
        ["messageTemplate", 1, 9, False],
        ["contents", 2, ["map", 9, 9], True],
        [
            "contentValues",
            5,
            ["map", 9, "lark_entities_SystemContent_ContentValue"],
            True,
        ],
        ["typeNum", 6, 5, False],
    ],
    "lark_entities_SystemContent_ContentValue": [
        ["type", 2, 5, False],
        ["ids", 3, 9, True],
        ["text", 4, 9, False],
    ],
    "lark_entities_BatchPullUniversalCardsByBizRequest": [
        ["staticCards", 1, 9, True],
        ["bizId", 2, "lark_entities_BizEntity", True],
        ["bizType", 3, 5, False],
    ],
    "lark_entities_BizEntity": [["bizId", 1, 9, False], ["hostCardId", 3, 9, False]],
    "lark_entities_BatchPullUniversalCardsByBizResponse": [
        ["staticCardsResult", 1, ["map", 9, "lark_entities_CardResult"], True],
        ["bizResults", 2, ["map", 9, "lark_entities_CardResult"], True],
    ],
    "lark_entities_CardResult": [
        ["errCode", 1, 5, False],
        ["errMsg", 2, 9, False],
        ["card", 3, "lark_entities_UniversalCardEntity", False],
    ],
    "lark_entities_UniversalCardEntity": [
        ["cardId", 1, 9, False],
        ["content", 3, "lark_entities_UniversalCardEntity_Content", False],
    ],
    "lark_entities_UniversalCardEntity_Content": [
        ["card", 1, 9, False],
        ["cardSchema", 6, 5, False],
    ],
}

_CONTENT_COMMANDS: dict[str, tuple[int, str, str]] = {
    "BatchPullUniversalCardsByBizRequest": (
        1109003,
        "lark_entities_BatchPullUniversalCardsByBizRequest",
        "lark_entities_BatchPullUniversalCardsByBizResponse",
    ),
}

_MEDIA_SCHEMA: dict[str, list[list[Any]]] = {
    "lark_entities_ImageSetV2": [["crypto", 3, "lark_entities_Crypto", False]],
    "lark_entities_ImageSet": [["origin", 2, "lark_entities_Image", False]],
    "lark_entities_Image": [
        ["key", 1, 9, False],
        ["crypto", 9, "lark_entities_Crypto", False],
    ],
    "lark_entities_Crypto": [
        ["type", 1, 5, False],
        ["cipher", 2, "lark_entities_Cipher", False],
    ],
    "lark_entities_Cipher": [
        ["secret", 1, 12, False],
        ["nonce", 2, 12, False],
        ["additionalData", 3, 12, False],
    ],
    "lark_entities_UniversalCardEntity": [["streamingMode", 8, 8, False]],
    "lark_chats_PullChatChattersRequest": [
        ["chatId", 1, 9, False],
        ["needChatters", 2, 8, False],
        ["limit", 5, 5, False],
    ],
    "lark_chats_PullChatChattersResponse": [
        ["chatters", 1, ["map", 9, "lark_entities_Chatter"], True]
    ],
}

_MEDIA_COMMANDS: dict[str, tuple[int, str, str]] = {
    "PullChatChattersRequest": (
        44,
        "lark_chats_PullChatChattersRequest",
        "lark_chats_PullChatChattersResponse",
    ),
}

_REACTION_SCHEMA: dict[str, list[list[Any]]] = {
    "reactions_PutReactionRequest": [
        ["messageId", 1, 9, False],
        ["reactionType", 2, 9, False],
    ],
    "reactions_PutReactionResponse": [["count", 1, 5, False]],
    "reactions_DeleteReactionRequest": [
        ["messageId", 1, 9, False],
        ["reactionType", 2, 9, False],
    ],
    "reactions_DeleteReactionResponse": [],
}

_REACTION_COMMANDS: dict[str, tuple[int, str, str]] = {
    "PutReactionRequest": (
        25,
        "reactions_PutReactionRequest",
        "reactions_PutReactionResponse",
    ),
    "DeleteReactionRequest": (
        26,
        "reactions_DeleteReactionRequest",
        "reactions_DeleteReactionResponse",
    ),
}

_THREAD_SCHEMA: dict[str, list[list[Any]]] = {
    "thread_PullThreadsByIDsRequest": [
        ["threadIds", 1, 9, True],
        ["lastMessageCount", 2, 5, False],
    ],
    "thread_PullThreadsByIDsResponse": [["threads", 1, ["map", 9, "thread_Thread"], True]],
    "thread_Thread": [
        ["id", 1, 9, False],
        ["channelId", 2, 9, False],
        ["lastMessageId", 8, 9, False],
        ["lastMessagePosition", 9, 5, False],
        ["replyCount", 11, 5, False],
        ["messageStartPosition", 13, 5, False],
        ["latestReplyTime", 31, 3, False],
        ["rootMessageChatPosition", 33, 5, False],
    ],
    "thread_PullThreadMessagesByPositionsRequest": [
        ["threadId", 1, 9, False],
        ["positions", 2, 5, True],
        ["channelId", 9, 9, False],
        ["channelType", 10, 5, False],
    ],
    "thread_PullThreadMessagesByPositionsResponse": [
        ["messages", 1, ["map", 5, "lark_entities_Message"], True],
        ["hasMore", 5, 8, False],
    ],
}

_THREAD_COMMANDS: dict[str, tuple[int, str, str]] = {
    "PullThreadsByIDsRequest": (
        8111,
        "thread_PullThreadsByIDsRequest",
        "thread_PullThreadsByIDsResponse",
    ),
    "PullThreadMessagesByPositionsRequest": (
        8109,
        "thread_PullThreadMessagesByPositionsRequest",
        "thread_PullThreadMessagesByPositionsResponse",
    ),
}

_CALENDAR_SCHEMA: dict[str, list[list[Any]]] = {
    "cal_GetUserPrimaryCalendarRequest": [],
    "cal_GetUserPrimaryCalendarResponse": [["calendar", 1, "cal_Calendar", False]],
    "cal_Calendar": [
        ["id", 1, 9, False],
        ["summary", 2, 9, False],
        ["defaultTimezone", 4, 9, False],
    ],
    "cal_QueryInstanceRequest": [
        ["calendarIds", 1, 9, True],
        ["startTime", 2, 3, False],
        ["endTime", 3, 3, False],
        ["needsBriefEvents", 6, 8, False],
    ],
    "cal_QueryInstanceResponse": [
        ["calendarEventInstance", 1, "cal_CalendarEventInstance", True],
        ["events", 2, ["map", 9, "cal_CalendarEvent"], True],
    ],
    "cal_CalendarEventInstance": [
        ["id", 1, 9, False],
        ["eventRefId", 2, 9, False],
        ["calendarId", 3, 9, False],
        ["beginTime", 4, 3, False],
        ["endTime", 5, 3, False],
    ],
    "cal_CalendarEvent": [
        ["id", 1, 9, False],
        ["uniqueKey", 2, 9, False],
        ["calendarId", 3, 9, False],
        ["originalTime", 4, 3, False],
        ["organizerCalendarId", 6, 9, False],
        ["selfAttendeeStatus", 7, 5, False],
        ["start", 8, 3, False],
        ["startTimezone", 9, 9, False],
        ["end", 10, 3, False],
        ["status", 12, 5, False],
        ["locations", 13, "cal_CalendarEventLocation", True],
        ["rrule", 14, 9, False],
        ["isAllDay", 25, 8, False],
        ["attendees", 26, "cal_CalendarEventAttendee", True],
        ["summary", 29, 9, False],
        ["description", 30, 9, False],
        ["type", 40, 5, False],
        ["meetingId", 41, 9, False],
        ["calendarResources", 49, "cal_CalendarResource", True],
        ["meetingMinuteUrl", 51, 9, False],
        ["vchatUniqueId", 53, 9, False],
    ],
    "cal_CalendarEventLocation": [
        ["name", 1, 9, False],
        ["address", 2, 9, False],
    ],
    "cal_CalendarEventAttendee": [
        ["displayName", 1, 9, False],
        ["attendeeCalendarId", 2, 9, False],
        ["isOrganizer", 3, 8, False],
        ["isResource", 4, 8, False],
        ["isOptional", 5, 8, False],
        ["status", 6, 5, False],
        ["email", 15, 9, False],
    ],
    "cal_CalendarResource": [
        ["id", 1, 9, False],
        ["name", 3, 9, False],
        ["floorName", 9, 9, False],
    ],
    "cal_GetVchatByEventRequest": [
        ["calendarId", 1, 9, False],
        ["uid", 2, 9, False],
        ["originalTime", 3, 3, False],
    ],
    "cal_GetVchatByEventResponse": [["vchat", 1, "cal_Vchat", False]],
    "cal_Vchat": [
        ["uniqueId", 1, 9, False],
        ["meetingNo", 2, 9, False],
        ["isExpired", 3, 8, False],
        ["meetingUrl", 4, 9, False],
    ],
    "cal_CalendarEventKey": [
        ["uniqueKey", 1, 9, False],
        ["calendarId", 2, 9, False],
        ["originalTime", 3, 3, False],
    ],
    "cal_ReplyCalendarEventInvitationRequest": [
        ["calendarEventKey", 1, "cal_CalendarEventKey", False],
        ["replyType", 2, 5, False],
    ],
    "cal_ReplyCalendarEventInvitationResponse": [["calendarEvent", 1, "cal_CalendarEvent", False]],
}

_CALENDAR_COMMANDS: dict[str, tuple[int, str, str]] = {
    "GetUserPrimaryCalendarRequest": (
        1000041,
        "cal_GetUserPrimaryCalendarRequest",
        "cal_GetUserPrimaryCalendarResponse",
    ),
    "QueryInstanceRequest": (
        1010006,
        "cal_QueryInstanceRequest",
        "cal_QueryInstanceResponse",
    ),
    "GetVchatByEventRequest": (
        1010030,
        "cal_GetVchatByEventRequest",
        "cal_GetVchatByEventResponse",
    ),
    "ReplyCalendarEventInvitationRequest": (
        1020002,
        "cal_ReplyCalendarEventInvitationRequest",
        "cal_ReplyCalendarEventInvitationResponse",
    ),
}


class _SchemaRegistry:
    def __init__(self) -> None:
        schema_path = Path(__file__).with_name("protocol_schema.json")
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
        raw_messages: dict[str, list[list[Any]]] = {
            k: [list(f) for f in v] for k, v in raw["messages"].items()
        }
        raw_commands: dict[str, tuple[int, str, str]] = {
            k: (int(v[0]), str(v[1]), str(v[2])) for k, v in raw["commands"].items()
        }

        for k, v in _PUSH_SCHEMA.items():
            raw_messages[k] = [list(f) for f in v]

        for extra_msgs, extra_cmds in (
            (_FEED_SCHEMA, _FEED_COMMANDS),
            (_CONTENT_SCHEMA, _CONTENT_COMMANDS),
            (_MEDIA_SCHEMA, _MEDIA_COMMANDS),
            (_REACTION_SCHEMA, _REACTION_COMMANDS),
            (_THREAD_SCHEMA, _THREAD_COMMANDS),
            (_CALENDAR_SCHEMA, _CALENDAR_COMMANDS),
        ):
            for k, v in extra_msgs.items():
                raw_messages.setdefault(k, []).extend(list(f) for f in v)
            raw_commands.update(extra_cmds)

        self.commands: dict[str, Command] = {
            name: Command(cmd=c[0], request=c[1], response=c[2]) for name, c in raw_commands.items()
        }
        self.by_name: dict[str, dict[str, _FieldDef]] = {}
        self.by_number: dict[str, dict[int, _FieldDef]] = {}

        for msg_name, fields in raw_messages.items():
            name_map: dict[str, _FieldDef] = {}
            num_map: dict[int, _FieldDef] = {}
            for f in fields:
                fname = str(f[0])
                fnum = int(f[1])
                raw_kind = f[2]
                repeated = bool(f[3])
                if isinstance(raw_kind, list):
                    kind: int | str | tuple[str, int, int | str] = (
                        "map",
                        int(raw_kind[1]),
                        raw_kind[2] if isinstance(raw_kind[2], str) else int(raw_kind[2]),
                    )
                elif isinstance(raw_kind, str):
                    kind = raw_kind
                else:
                    kind = int(raw_kind)
                fdef = _FieldDef(name=fname, number=fnum, kind=kind, repeated=repeated)
                name_map[fname] = fdef
                num_map[fnum] = fdef
            self.by_name[msg_name] = name_map
            self.by_number[msg_name] = num_map


_REGISTRY: _SchemaRegistry | None = None


def _registry() -> _SchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _SchemaRegistry()
    return _REGISTRY


def command_of(name: str) -> Command:
    """Return a gateway ``Command`` by its schema name."""
    reg = _REGISTRY or _registry()
    cmd = reg.commands.get(name)
    if cmd is None:
        raise KeyError(f"web schema has no command {name}")
    return cmd


def _encode_varint(val: int) -> bytes:
    if val < 0:
        val &= _MASK_64
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    n = len(data)
    while pos < n:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("protobuf varint overflow")
    raise ValueError("truncated protobuf varint")


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _encode_len_delimited(field_number: int, payload: bytes) -> bytes:
    return _encode_tag(field_number, WIRE_LEN) + _encode_varint(len(payload)) + payload


def _coerce_int(val: Any) -> int:
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        return int(val.strip())
    raise ValueError(f"expected integer value, got {val!r}")


def _encode_scalar_field(field_number: int, kind: int | str, val: Any) -> bytes:
    if isinstance(kind, int):
        if kind == TYPE_BOOL:
            if isinstance(val, str):
                bval = val.strip().lower() in {"true", "1"}
            else:
                bval = bool(val)
            return _encode_tag(field_number, WIRE_VARINT) + _encode_varint(1 if bval else 0)
        if kind in _VARINT_TYPES:
            ival = _coerce_int(val)
            if kind in {TYPE_UINT32, TYPE_UINT64} and ival < 0:
                raise ValueError(f"negative unsigned integer: {ival}")
            return _encode_tag(field_number, WIRE_VARINT) + _encode_varint(ival)
        if kind == TYPE_STRING:
            if not isinstance(val, str):
                val = str(val)
            return _encode_len_delimited(field_number, val.encode("utf-8"))
        if kind == TYPE_BYTES:
            if isinstance(val, (bytes, bytearray)):
                raw = bytes(val)
            elif isinstance(val, str):
                raw = base64.b64decode(val.encode("ascii")) if val else b""
            else:
                raise ValueError(f"expected base64 string or bytes, got {type(val).__name__}")
            return _encode_len_delimited(field_number, raw)
        raise ValueError(f"unsupported scalar type {kind}")
    if not isinstance(val, Mapping):
        raise ValueError(f"expected mapping for message {kind}, got {type(val).__name__}")
    nested = encode(kind, val)
    return _encode_len_delimited(field_number, nested)


def encode(name: str, body: Mapping[str, Any] | None) -> bytes:
    """Encode ``body`` into protobuf wire bytes for schema message ``name``."""
    reg = _REGISTRY or _registry()
    fields = reg.by_name.get(name)
    if fields is None:
        raise KeyError(f"web schema has no message {name}")
    if not body:
        return b""
    if not isinstance(body, Mapping):
        raise ValueError(f"encode {name}: expected mapping, got {type(body).__name__}")

    out = bytearray()
    for key, val in body.items():
        if val is None:
            continue
        fdef = fields.get(key)
        if fdef is None:
            raise ValueError(f"encode {name}: unknown field {key!r}")
        kind = fdef.kind
        if isinstance(kind, tuple) and kind[0] == "map":
            _, key_type, val_type = kind
            if not isinstance(val, Mapping):
                raise ValueError(f"encode {name}.{key}: expected map dict")
            for mk, mv in val.items():
                if mv is None:
                    continue
                entry_bytes = _encode_scalar_field(1, key_type, mk) + _encode_scalar_field(
                    2, val_type, mv
                )
                out.extend(_encode_len_delimited(fdef.number, entry_bytes))
        elif fdef.repeated:
            if not isinstance(val, (list, tuple)):
                raise ValueError(f"encode {name}.{key}: expected list for repeated field")
            for item in val:
                if item is None:
                    continue
                out.extend(_encode_scalar_field(fdef.number, kind, item))  # type: ignore[arg-type]
        else:
            out.extend(_encode_scalar_field(fdef.number, kind, val))  # type: ignore[arg-type]
    return bytes(out)


def _decode_varint_value(raw: int, kind: int) -> Any:
    if kind == TYPE_BOOL:
        return bool(raw)
    if kind == TYPE_INT64:
        val64 = raw & _MASK_64
        if val64 & _SIGN_64:
            val64 -= 1 << 64
        return str(val64)
    if kind == TYPE_UINT64:
        return str(raw & _MASK_64)
    if kind == TYPE_INT32:
        val32 = raw & _MASK_32
        if val32 & _SIGN_32:
            val32 -= 1 << 32
        return int(val32)
    if kind == TYPE_UINT32:
        return int(raw & _MASK_32)
    return int(raw)


def _zero_value_for(kind: int | str) -> Any:
    if isinstance(kind, str):
        return {}
    if kind == TYPE_BOOL:
        return False
    if kind in {TYPE_INT64, TYPE_UINT64}:
        return "0"
    if kind in {TYPE_INT32, TYPE_UINT32}:
        return 0
    return ""


def _decode_map_entry(data: bytes, key_type: int, val_type: int | str) -> tuple[str, Any]:
    pos = 0
    n = len(data)
    k_val: Any = "" if key_type == TYPE_STRING else 0
    v_val: Any = _zero_value_for(val_type)
    while pos < n:
        tag, pos = _decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 1:
            if wire_type == WIRE_VARINT:
                raw, pos = _decode_varint(data, pos)
                k_val = _decode_varint_value(raw, key_type)
            elif wire_type == WIRE_LEN:
                length, pos = _decode_varint(data, pos)
                if pos + length > n:
                    raise ValueError("truncated protobuf map key")
                k_val = data[pos : pos + length].decode("utf-8", errors="replace")
                pos += length
            else:
                pos = _skip_wire_value(data, pos, wire_type)
        elif field_num == 2:
            if isinstance(val_type, int) and val_type in _VARINT_TYPES and wire_type == WIRE_VARINT:
                raw, pos = _decode_varint(data, pos)
                v_val = _decode_varint_value(raw, val_type)
            elif wire_type == WIRE_LEN:
                length, pos = _decode_varint(data, pos)
                if pos + length > n:
                    raise ValueError("truncated protobuf map value")
                chunk = data[pos : pos + length]
                pos += length
                if isinstance(val_type, str):
                    v_val = decode(val_type, chunk)
                elif val_type == TYPE_STRING:
                    v_val = chunk.decode("utf-8", errors="replace")
                elif val_type == TYPE_BYTES:
                    v_val = base64.b64encode(chunk).decode("ascii")
            else:
                pos = _skip_wire_value(data, pos, wire_type)
        else:
            pos = _skip_wire_value(data, pos, wire_type)
    return str(k_val), v_val


def _skip_wire_value(data: bytes, pos: int, wire_type: int) -> int:
    n = len(data)
    if wire_type == WIRE_VARINT:
        _, pos = _decode_varint(data, pos)
        return pos
    if wire_type == WIRE_64BIT:
        if pos + 8 > n:
            raise ValueError("truncated protobuf 64-bit field")
        return pos + 8
    if wire_type == WIRE_LEN:
        length, pos = _decode_varint(data, pos)
        if pos + length > n:
            raise ValueError("truncated protobuf length-delimited field")
        return pos + length
    if wire_type == WIRE_32BIT:
        if pos + 4 > n:
            raise ValueError("truncated protobuf 32-bit field")
        return pos + 4
    raise ValueError(f"unsupported protobuf wire type {wire_type}")


def decode(name: str, data: bytes | bytearray | memoryview | None) -> dict[str, Any]:
    """Decode protobuf wire ``data`` for schema message ``name`` into a JSON-shaped dict."""
    reg = _REGISTRY or _registry()
    fields = reg.by_number.get(name)
    if fields is None:
        raise KeyError(f"web schema has no message {name}")
    if not data:
        return {}
    buf = bytes(data)
    pos = 0
    n = len(buf)
    out: dict[str, Any] = {}

    while pos < n:
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num <= 0:
            raise ValueError(f"decode {name}: invalid field number {field_num}")
        fdef = fields.get(field_num)
        if fdef is None:
            pos = _skip_wire_value(buf, pos, wire_type)
            continue

        kind = fdef.kind
        if isinstance(kind, tuple) and kind[0] == "map":
            if wire_type != WIRE_LEN:
                pos = _skip_wire_value(buf, pos, wire_type)
                continue
            length, pos = _decode_varint(buf, pos)
            if pos + length > n:
                raise ValueError(f"decode {name}.{fdef.name}: truncated map entry")
            entry_chunk = buf[pos : pos + length]
            pos += length
            _, key_type, val_type = kind
            mk, mv = _decode_map_entry(entry_chunk, key_type, val_type)
            target_map = out.setdefault(fdef.name, {})
            target_map[mk] = mv
            continue

        if isinstance(kind, int) and kind in _VARINT_TYPES:
            if wire_type == WIRE_VARINT:
                raw, pos = _decode_varint(buf, pos)
                decoded_val = _decode_varint_value(raw, kind)
                if fdef.repeated:
                    out.setdefault(fdef.name, []).append(decoded_val)
                else:
                    out[fdef.name] = decoded_val
            elif wire_type == WIRE_LEN and fdef.repeated:
                length, pos = _decode_varint(buf, pos)
                if pos + length > n:
                    raise ValueError(f"decode {name}.{fdef.name}: truncated packed varint")
                sub = buf[pos : pos + length]
                pos += length
                spos = 0
                arr = out.setdefault(fdef.name, [])
                while spos < len(sub):
                    raw, spos = _decode_varint(sub, spos)
                    arr.append(_decode_varint_value(raw, kind))
            else:
                pos = _skip_wire_value(buf, pos, wire_type)
            continue

        if wire_type != WIRE_LEN:
            pos = _skip_wire_value(buf, pos, wire_type)
            continue

        length, pos = _decode_varint(buf, pos)
        if pos + length > n:
            raise ValueError(f"decode {name}.{fdef.name}: truncated length-delimited field")
        chunk = buf[pos : pos + length]
        pos += length

        if isinstance(kind, str):
            nested_val = decode(kind, chunk)
            if fdef.repeated:
                out.setdefault(fdef.name, []).append(nested_val)
            else:
                if fdef.name in out and isinstance(out[fdef.name], dict):
                    out[fdef.name].update(nested_val)
                else:
                    out[fdef.name] = nested_val
        elif kind == TYPE_STRING:
            sval = chunk.decode("utf-8", errors="replace")
            if fdef.repeated:
                out.setdefault(fdef.name, []).append(sval)
            else:
                out[fdef.name] = sval
        elif kind == TYPE_BYTES:
            b64 = base64.b64encode(chunk).decode("ascii")
            if fdef.repeated:
                out.setdefault(fdef.name, []).append(b64)
            else:
                out[fdef.name] = b64

    return out
