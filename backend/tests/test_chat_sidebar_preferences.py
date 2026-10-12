from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_hub.services.chat_sidebar_preferences import ChatSidebarPreferencesStore


def test_preferences_are_per_user_and_survive_new_store_instances(tmp_path: Path) -> None:
    store = ChatSidebarPreferencesStore(tmp_path)

    assert store.set_pinned("user-a", "tab-a", True) == ["tab-a"]
    assert store.set_pinned("user-b", "tab-b", True) == ["tab-b"]

    reloaded = ChatSidebarPreferencesStore(tmp_path)
    assert reloaded.get_pinned("user-a") == ["tab-a"]
    assert reloaded.get_pinned("user-b") == ["tab-b"]


def test_single_tab_mutations_do_not_lose_concurrent_pins(tmp_path: Path) -> None:
    tab_ids = [f"tab-{index}" for index in range(16)]

    def pin(tab_id: str) -> None:
        ChatSidebarPreferencesStore(tmp_path).set_pinned("user", tab_id, True)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(pin, tab_ids))

    assert set(ChatSidebarPreferencesStore(tmp_path).get_pinned("user")) == set(tab_ids)


def test_legacy_browser_pins_migrate_only_once(tmp_path: Path) -> None:
    store = ChatSidebarPreferencesStore(tmp_path)

    migrated = store.migrate_browser_pins("user", [" legacy-pin ", "legacy-pin", ""])
    repeated = store.migrate_browser_pins("user", ["resurrected-from-another-origin"])

    assert migrated == ["legacy-pin"]
    assert repeated == migrated


def test_authoritative_mutation_prevents_stale_browser_resurrection(tmp_path: Path) -> None:
    store = ChatSidebarPreferencesStore(tmp_path)

    store.set_pinned("user", "new-server-pin", True)

    assert store.migrate_browser_pins("user", ["stale-browser-pin"]) == ["new-server-pin"]


def test_delete_cleanup_preserves_other_users(tmp_path: Path) -> None:
    store = ChatSidebarPreferencesStore(tmp_path)
    for user_id in ("user-a", "user-b"):
        store.set_pinned(user_id, "keep", True)
        store.set_pinned(user_id, "delete", True)
    store.remove_tab("delete")

    assert store.get_pinned("user-a") == ["keep"]
    assert store.get_pinned("user-b") == ["keep"]
