import base64

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.mark.asyncio
async def test_upload_clipboard_image_sets_macos_clipboard(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("claude_hub.api.clipboard.CLIPBOARD_IMAGE_DIR", tmp_path)
    monkeypatch.setattr("claude_hub.api.clipboard.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "claude_hub.api.clipboard.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    response = await client.post(
        "/api/clipboard/image",
        files={"image": ("clipboard.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["content_type"] == "image/png"
    assert data["size"] == len(PNG_BYTES)
    assert (tmp_path / data["path"].split("/")[-1]).read_bytes() == PNG_BYTES

    assert captured["args"][0:2] == ("osascript", "-e")
    script = captured["args"][2]
    assert "set the clipboard to" in script
    assert "«class PNGf»" in script
    assert data["path"] in script


@pytest.mark.asyncio
async def test_upload_clipboard_image_rejects_non_images(client: AsyncClient) -> None:
    response = await client.post(
        "/api/clipboard/image",
        files={"image": ("clipboard.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 415
