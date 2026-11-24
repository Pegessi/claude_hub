"""Bounded image reads using the same personal identity as message reads (never a Bot)."""

from __future__ import annotations

from typing import Any, Mapping, cast
from urllib.parse import quote

import httpx

from claude_hub.services.feishu_bot import FeishuBotClient, FeishuBotError
from claude_hub.services.feishu_user_message import FeishuUserMessage

MAX_IMAGE_BYTES = 20 * 1024 * 1024


async def read_image_response(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str]
) -> bytes:
    # No redirects: a resource response must never forward credentials to another origin.
    async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
        if response.status_code != 200:
            # Consume only a bounded error body; token expiry can use the existing OAuth retry.
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > 65536:
                    break
            try:
                import json

                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("code"):
                raise FeishuBotClient._remote_error(response, payload, "personal image download")
            if response.status_code == 401:
                response.raise_for_status()  # Existing user-token retry owns refresh.
            raise FeishuBotError(f"Personal image download HTTP {response.status_code}")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise FeishuBotError("Personal image exceeds attachment size limit")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise FeishuBotError("Personal image exceeds attachment size limit")
            chunks.append(chunk)
        return b"".join(chunks)


async def download_user_image(client: Any, message: FeishuUserMessage, key: str) -> bytes:
    if (message.message_id, key) not in message.image_refs:
        raise FeishuBotError("Image resource is not associated with this message")
    if not message.message_id.startswith("om_"):
        # _get_web_transport enforces owner + region binding. A numeric Web resource
        # cannot be downloaded with an unrelated OAuth/Bot credential.
        transport = client._get_web_transport()
        if transport is None:
            raise FeishuBotError("Personal Web image session is unavailable or not authorized")
        return cast(bytes, await transport.download_message_image(message, key))

    async def read(token: str) -> bytes:
        url = (
            f"{client._api_base_url}/open-apis/im/v1/messages/"
            f"{quote(message.message_id, safe='')}/resources/{quote(key, safe='')}?type=image"
        )
        headers = {"Authorization": f"Bearer {token}"}
        if client._client is not None:
            return await read_image_response(client._client, url, headers)
        async with httpx.AsyncClient(timeout=30) as http:
            return await read_image_response(http, url, headers)

    return cast(bytes, await client._with_user_token_retry(read))
