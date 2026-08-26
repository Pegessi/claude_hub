"""Tests for the env-preset persistence service and REST API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Generator

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.models import (
    EnvPresetBulkImport,
    EnvPresetCreate,
    EnvPresetHiddenRequest,
    EnvPresetUpdate,
)
from claude_hub.services.env_presets import (
    BUILT_IN_PRESET_IDS,
    EnvPresetManager,
)


@pytest.fixture(autouse=True)
def _isolate_env_presets(tmp_path: Path, monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    """Replace the module-level env_preset_manager singleton with a fresh
    temp-file-backed instance so API tests do not touch the real user data
    file (~/.claude_hub/env_presets.json) and do not leak state between tests."""
    import claude_hub.api.env_presets as api_module

    fresh = EnvPresetManager(path=tmp_path / "env_presets.json")
    monkeypatch.setattr(api_module, "env_preset_manager", fresh)
    yield


# ═══════════════════════════════════════════════════════════════════════
# Service-layer unit tests (direct manager, no HTTP)
# ═══════════════════════════════════════════════════════════════════════


class TestEnvPresetManager:
    """Unit tests for EnvPresetManager using a temp file path."""

    @pytest.fixture
    def manager(self, tmp_path: Path) -> EnvPresetManager:
        path = tmp_path / "env_presets.json"
        return EnvPresetManager(path=path)

    def test_empty_state_on_fresh_start(self, manager: EnvPresetManager) -> None:
        state = manager.list_presets()
        assert state["custom_presets"] == []
        assert state["hidden_builtin_ids"] == []

    def test_create_preset(self, manager: EnvPresetManager) -> None:
        preset = manager.create_preset(name="My Proxy", text="HTTP_PROXY=http://proxy:8080")
        assert preset.id.startswith("custom-")
        assert preset.name == "My Proxy"
        assert "HTTP_PROXY" in preset.text

        state = manager.list_presets()
        assert len(state["custom_presets"]) == 1
        assert state["custom_presets"][0]["name"] == "My Proxy"

    def test_create_preset_with_explicit_id(self, manager: EnvPresetManager) -> None:
        preset = manager.create_preset(name="Test", text="FOO=bar", preset_id="custom-test-1")
        assert preset.id == "custom-test-1"

    def test_create_duplicate_id_raises(self, manager: EnvPresetManager) -> None:
        manager.create_preset(name="A", text="A=1", preset_id="dup")
        with pytest.raises(ValueError, match="already exists"):
            manager.create_preset(name="B", text="B=2", preset_id="dup")

    def test_get_preset(self, manager: EnvPresetManager) -> None:
        created = manager.create_preset(name="Test", text="X=1", preset_id="p1")
        fetched = manager.get_preset("p1")
        assert fetched is not None
        assert fetched.name == "Test"
        assert manager.get_preset("nonexistent") is None

    def test_update_preset(self, manager: EnvPresetManager) -> None:
        manager.create_preset(name="Old Name", text="OLD=1", preset_id="p1")
        updated = manager.update_preset("p1", name="New Name", text="NEW=2")
        assert updated is not None
        assert updated.name == "New Name"
        assert updated.text == "NEW=2"
        # Partial update
        manager.update_preset("p1", text="NEW=3")
        assert manager.get_preset("p1").name == "New Name"
        assert manager.get_preset("p1").text == "NEW=3"

    def test_update_nonexistent_returns_none(self, manager: EnvPresetManager) -> None:
        assert manager.update_preset("nope", name="x", text="y") is None

    def test_upsert_creates_when_missing(self, manager: EnvPresetManager) -> None:
        preset = manager.upsert_preset("p1", name="Created", text="C=1")
        assert preset.name == "Created"
        assert len(manager.list_presets()["custom_presets"]) == 1

    def test_upsert_updates_when_exists(self, manager: EnvPresetManager) -> None:
        manager.create_preset(name="Before", text="B=1", preset_id="p1")
        upserted = manager.upsert_preset("p1", name="After", text="A=2")
        assert upserted.name == "After"
        assert len(manager.list_presets()["custom_presets"]) == 1

    def test_delete_custom_preset(self, manager: EnvPresetManager) -> None:
        manager.create_preset(name="D", text="D=1", preset_id="del-me")
        assert manager.delete_preset("del-me") is True
        assert manager.get_preset("del-me") is None
        assert len(manager.list_presets()["custom_presets"]) == 0

    def test_delete_builtin_returns_false(self, manager: EnvPresetManager) -> None:
        # Built-in presets cannot be deleted (only hidden)
        assert manager.delete_preset("local-proxy-7890") is False

    def test_delete_nonexistent_returns_false(self, manager: EnvPresetManager) -> None:
        assert manager.delete_preset("nope") is False

    def test_cannot_delete_none_preset(self, manager: EnvPresetManager) -> None:
        assert manager.delete_preset("none") is False

    def test_hide_and_unhide_builtin(self, manager: EnvPresetManager) -> None:
        assert manager.set_hidden("local-proxy-7890", hidden=True) is True
        assert "local-proxy-7890" in manager.list_presets()["hidden_builtin_ids"]

        assert manager.set_hidden("local-proxy-7890", hidden=False) is True
        assert "local-proxy-7890" not in manager.list_presets()["hidden_builtin_ids"]

    def test_hide_non_builtin_returns_false(self, manager: EnvPresetManager) -> None:
        manager.create_preset(name="c", text="c=1", preset_id="custom-1")
        assert manager.set_hidden("custom-1", hidden=True) is False
        assert manager.set_hidden("nope", hidden=True) is False

    def test_hide_unhide_is_idempotent(self, manager: EnvPresetManager) -> None:
        manager.set_hidden("socks-proxy-1080", True)
        manager.set_hidden("socks-proxy-1080", True)
        assert manager.list_presets()["hidden_builtin_ids"].count("socks-proxy-1080") == 1

        manager.set_hidden("socks-proxy-1080", False)
        manager.set_hidden("socks-proxy-1080", False)
        assert "socks-proxy-1080" not in manager.list_presets()["hidden_builtin_ids"]

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "env_presets.json"
        mgr1 = EnvPresetManager(path=path)
        mgr1.create_preset(name="Persistent", text="P=1", preset_id="p1")
        mgr1.set_hidden("local-proxy-7890", True)

        # Load from same file in a new instance
        mgr2 = EnvPresetManager(path=path)
        state = mgr2.list_presets()
        assert len(state["custom_presets"]) == 1
        assert state["custom_presets"][0]["name"] == "Persistent"
        assert "local-proxy-7890" in state["hidden_builtin_ids"]

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "env_presets.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        mgr = EnvPresetManager(path=path)
        state = mgr.list_presets()
        assert state["custom_presets"] == []
        assert state["hidden_builtin_ids"] == []

    def test_bulk_import_merges_backend_wins(self, manager: EnvPresetManager) -> None:
        # Pre-populate server with one preset
        manager.create_preset(name="Server Preset", text="S=1", preset_id="existing")

        payload = EnvPresetBulkImport(
            custom_presets=[
                {
                    "id": "existing",
                    "name": "Client Overwrite",
                    "text": "C=999",
                },  # conflict - server wins
                {"id": "migrated-1", "name": "Client Preset", "text": "C=2"},
                {"id": "", "name": "", "text": ""},  # invalid - skipped
            ],
            hidden_builtin_ids=["local-proxy-7890", "nonexistent"],
        )
        result = manager.bulk_import(payload)
        ids = {p["id"] for p in result["custom_presets"]}
        assert "existing" in ids
        assert "migrated-1" in ids
        # Server wins on conflict — name should still be "Server Preset"
        existing = next(p for p in result["custom_presets"] if p["id"] == "existing")
        assert existing["name"] == "Server Preset"
        # Invalid entries skipped
        assert "" not in ids
        # Hidden ids merged (invalid builtin filtered)
        assert "local-proxy-7890" in result["hidden_builtin_ids"]
        assert "nonexistent" not in result["hidden_builtin_ids"]

    def test_bulk_import_when_backend_empty(self, manager: EnvPresetManager) -> None:
        payload = EnvPresetBulkImport(
            custom_presets=[{"id": "ls-1", "name": "From LS", "text": "LS=1"}],
            hidden_builtin_ids=["socks-proxy-1080"],
        )
        result = manager.bulk_import(payload)
        assert len(result["custom_presets"]) == 1
        assert result["custom_presets"][0]["name"] == "From LS"
        assert "socks-proxy-1080" in result["hidden_builtin_ids"]

    def test_create_preset_trims_name(self, manager: EnvPresetManager) -> None:
        preset = manager.create_preset(name="  spaced  ", text="A=1", preset_id="p1")
        assert preset.name == "spaced"

    def test_whitespace_entries_skipped_on_load(self, tmp_path: Path) -> None:
        path = tmp_path / "env_presets.json"
        path.write_text(
            json.dumps(
                {
                    "custom_presets": [
                        {"id": "ok", "name": "Good", "text": "G=1"},
                        {"id": "empty-name", "name": "", "text": "E=1"},
                        {"id": "empty-text", "name": "Has Name", "text": ""},
                        {"id": "no-name-field", "text": "X=1"},
                        "not-an-object",
                    ],
                    "hidden_builtin_ids": ["local-proxy-7890", "not-a-builtin", 123],
                }
            ),
            encoding="utf-8",
        )
        mgr = EnvPresetManager(path=path)
        state = mgr.list_presets()
        assert len(state["custom_presets"]) == 1
        assert state["custom_presets"][0]["id"] == "ok"
        assert state["hidden_builtin_ids"] == ["local-proxy-7890"]

    def test_atomic_write_uses_tmp_replace(self, tmp_path: Path) -> None:
        """Verify no tmp files left after a save."""
        path = tmp_path / "env_presets.json"
        mgr = EnvPresetManager(path=path)
        mgr.create_preset(name="A", text="A=1", preset_id="a1")
        # Should not leave .tmp files
        assert not (tmp_path / "env_presets.json.tmp").exists()
        assert path.exists()


# ═══════════════════════════════════════════════════════════════════════
# API-layer integration tests (httpx AsyncClient against FastAPI app)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/env-presets")
    assert resp.status_code == 200
    body = resp.json()
    assert "custom_presets" in body
    assert "hidden_builtin_ids" in body


@pytest.mark.asyncio
async def test_create_preset_api(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/env-presets",
        json=EnvPresetCreate(name="Test Preset", text="FOO=bar").model_dump(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Test Preset"
    assert body["text"] == "FOO=bar"
    assert body["id"].startswith("custom-")


@pytest.mark.asyncio
async def test_create_preset_rejects_empty_name(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/env-presets",
        json={"name": "", "text": "X=1"},
    )
    # 422 validation error
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upsert_preset_api(client: AsyncClient) -> None:
    # Create via PUT (upsert)
    resp = await client.put(
        "/api/env-presets/custom-abc",
        json=EnvPresetCreate(name="Upserted", text="U=1").model_dump(),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "custom-abc"

    # Update via PUT
    resp2 = await client.put(
        "/api/env-presets/custom-abc",
        json=EnvPresetCreate(name="Upserted v2", text="U=2").model_dump(),
    )
    assert resp2.status_code == 200
    assert resp2.json()["name"] == "Upserted v2"


@pytest.mark.asyncio
async def test_upsert_rejects_builtin_id(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/env-presets/none",
        json={"name": "Hack", "text": "H=1"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_preset_api(client: AsyncClient) -> None:
    await client.post(
        "/api/env-presets",
        json={"name": "Before", "text": "B=1"},
    )
    # Get the created id
    lst = await client.get("/api/env-presets")
    pid = lst.json()["custom_presets"][0]["id"]

    # Partial update (name only)
    resp = await client.patch(
        f"/api/env-presets/{pid}",
        json=EnvPresetUpdate(name="After").model_dump(exclude_none=True),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "After"
    assert resp.json()["text"] == "B=1"  # text unchanged


@pytest.mark.asyncio
async def test_patch_nonexistent_returns_404(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/env-presets/nonexistent-id",
        json={"name": "X"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_builtin_rejected(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/env-presets/local-proxy-7890",
        json={"name": "Wut"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_custom_preset_api(client: AsyncClient) -> None:
    # Create
    create_resp = await client.post(
        "/api/env-presets",
        json={"name": "To Delete", "text": "D=1"},
    )
    pid = create_resp.json()["id"]

    # Delete
    del_resp = await client.delete(f"/api/env-presets/{pid}")
    assert del_resp.status_code == 204

    # Verify gone
    lst = await client.get("/api/env-presets")
    ids = [p["id"] for p in lst.json()["custom_presets"]]
    assert pid not in ids


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_404(client: AsyncClient) -> None:
    resp = await client.delete("/api/env-presets/nope-xyz")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_builtin_via_delete_endpoint_rejected(client: AsyncClient) -> None:
    resp = await client.delete("/api/env-presets/local-proxy-7890")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_hide_builtin_api(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/env-presets/hidden/local-proxy-7890",
        json=EnvPresetHiddenRequest(hidden=True).model_dump(),
    )
    assert resp.status_code == 204

    lst = await client.get("/api/env-presets")
    assert "local-proxy-7890" in lst.json()["hidden_builtin_ids"]

    # Unhide
    resp2 = await client.put(
        "/api/env-presets/hidden/local-proxy-7890",
        json={"hidden": False},
    )
    assert resp2.status_code == 204
    lst2 = await client.get("/api/env-presets")
    assert "local-proxy-7890" not in lst2.json()["hidden_builtin_ids"]


@pytest.mark.asyncio
async def test_hide_nonexistent_builtin_returns_404(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/env-presets/hidden/not-a-real-preset",
        json={"hidden": True},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_import_api(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/env-presets/bulk-import",
        json={
            "custom_presets": [
                {"id": "mig1", "name": "Migrated", "text": "M=1"},
            ],
            "hidden_builtin_ids": ["socks-proxy-1080"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = {p["id"] for p in body["custom_presets"]}
    assert "mig1" in ids
    assert "socks-proxy-1080" in body["hidden_builtin_ids"]

    # Second import should not overwrite existing (backend wins)
    resp2 = await client.post(
        "/api/env-presets/bulk-import",
        json={
            "custom_presets": [
                {"id": "mig1", "name": "Should Not Overwrite", "text": "M=999"},
                {"id": "mig2", "name": "Second Wave", "text": "M2=1"},
            ],
            "hidden_builtin_ids": [],
        },
    )
    body2 = resp2.json()
    mig1 = next(p for p in body2["custom_presets"] if p["id"] == "mig1")
    assert mig1["name"] == "Migrated"  # server wins
    assert any(p["id"] == "mig2" for p in body2["custom_presets"])


@pytest.mark.asyncio
async def test_create_preset_preserves_hash_in_value(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/env-presets",
        json={"name": "Hashy", "text": "TOKEN=abc#def\nURL=https://x.test/p#frag"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["text"] == "TOKEN=abc#def\nURL=https://x.test/p#frag"


@pytest.mark.asyncio
async def test_create_preset_rejects_invalid_text_without_leaking_secret(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/env-presets",
        json={"name": "Bad", "text": "export 1BAD=super-secret-token"},
    )
    assert resp.status_code == 400
    assert "super-secret-token" not in resp.text


@pytest.mark.asyncio
async def test_full_crud_lifecycle(client: AsyncClient) -> None:
    """End-to-end CRUD: create → list → update → delete."""
    # Create
    c = await client.post("/api/env-presets", json={"name": "LC", "text": "LC=1"})
    assert c.status_code == 201
    pid = c.json()["id"]

    # List contains it
    lst = await client.get("/api/env-presets")
    assert any(p["id"] == pid for p in lst.json()["custom_presets"])

    # Update
    u = await client.patch(f"/api/env-presets/{pid}", json={"name": "LC2"})
    assert u.status_code == 200
    assert u.json()["name"] == "LC2"

    # Delete
    d = await client.delete(f"/api/env-presets/{pid}")
    assert d.status_code == 204

    # Gone
    lst2 = await client.get("/api/env-presets")
    assert not any(p["id"] == pid for p in lst2.json()["custom_presets"])


def test_builtin_preset_ids_constant_matches_frontend() -> None:
    """Sanity: backend BUILT_IN_PRESET_IDS should cover the same set as the
    frontend's BUILT_IN_PRESET_IDS (none, local-proxy-7890, socks-proxy-1080,
    volcengine-coding-plan)."""
    expected = {"none", "local-proxy-7890", "socks-proxy-1080", "volcengine-coding-plan"}
    assert set(BUILT_IN_PRESET_IDS) == expected
