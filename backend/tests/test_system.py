import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

import claude_hub.api.system as system_api


@pytest.mark.asyncio
async def test_network_access_returns_filtered_ipv4_addresses(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_api.socket, "gethostname", lambda: "dev-mac")
    monkeypatch.setattr(
        system_api,
        "_hostname_ipv4_addresses",
        lambda: {"127.0.0.1", "10.0.0.5", "169.254.1.2"},
    )
    monkeypatch.setattr(
        system_api,
        "_default_route_ipv4_addresses",
        lambda: {"192.168.1.20", "10.0.0.5"},
    )
    monkeypatch.setattr(
        system_api,
        "_interface_ipv4_addresses",
        lambda: {
            "10.0.0.5": "en0",
            "172.16.10.2": "en5",
            "169.254.1.2": "en6",
        },
    )

    response = await client.get("/api/system/network-access")

    assert response.status_code == 200
    assert response.json() == {
        "hostname": "dev-mac",
        "addresses": [
            {"address": "10.0.0.5", "label": "en0"},
            {"address": "172.16.10.2", "label": "en5"},
            {"address": "192.168.1.20", "label": "LAN IP"},
        ],
    }
