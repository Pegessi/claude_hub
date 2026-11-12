"""Provider quota readings: Codex rollout parsing, MASO HTTP, and Antigravity CLI.

No test may touch the developer's real ``~/.codex``, ``~/.gemini/antigravity-cli``,
or a live MASO/Antigravity service: homes are redirected per test and every
external call is mocked.
"""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import httpx
import pytest

from claude_hub.services import provider_quota as pq
from claude_hub.services.agent_stream import maso_http
from claude_hub.services.agent_stream.maso_http import MasoServiceUnavailable

FUTURE_RESET = 4102444800  # 2100-01-01, always ahead of the test clock
PAST_RESET = 1600000000  # 2020-09-13, always behind it


def _token_count_line(
    *,
    timestamp: str = "2026-10-15T04:07:36.155Z",
    used_percent: float = 58.0,
    window_minutes: int = 10080,
    resets_at: int = FUTURE_RESET,
    plan_type: str = "pro",
    balance: str = "62500.0000000000",
    secondary: dict | None = None,
    payload_type: str = "token_count",
) -> str:
    """Serialize one Codex ``token_count`` record as it appears on disk."""
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": payload_type,
                "info": {"total_token_usage": {"input_tokens": 1}},
                "rate_limits": {
                    "limit_id": "codex",
                    "limit_name": None,
                    "primary": {
                        "used_percent": used_percent,
                        "window_minutes": window_minutes,
                        "resets_at": resets_at,
                    },
                    "secondary": secondary,
                    "credits": {
                        "has_credits": True,
                        "unlimited": False,
                        "balance": balance,
                    },
                    "individual_limit": None,
                    "spend_control_reached": None,
                    "plan_type": plan_type,
                    "rate_limit_reached_type": None,
                },
            },
        }
    )


def _chat_line(text: str) -> str:
    """Serialize an ordinary conversation record."""
    return json.dumps(
        {
            "timestamp": "2026-10-15T04:07:00.000Z",
            "type": "response_item",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        }
    )


def _write_rollout(
    home: Path,
    name: str,
    lines: list[str],
    mtime: float,
    day: str = "05",
    root: str = "sessions",
) -> Path:
    """Write a rollout file with an explicit mtime, under a dated directory."""
    path = home / root / "2026" / "10" / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(["{}"] + lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(autouse=True)
def clear_quota_cache(tmp_path, monkeypatch):
    """Keep the module-level reading cache and host Antigravity home isolated."""
    pq._QUOTA_CACHE.clear()
    pq._QUOTA_IN_FLIGHT.clear()
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(tmp_path / "antigravity-empty"))
    yield
    pq._QUOTA_CACHE.clear()
    pq._QUOTA_IN_FLIGHT.clear()


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Redirect the Codex home at an empty temporary directory."""
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


# -- Codex -----------------------------------------------------------------


def test_codex_reading_maps_the_live_window(codex_home) -> None:
    _write_rollout(codex_home, "rollout-a.jsonl", [_token_count_line()], mtime=1000.0)

    quota = pq.read_codex_quota()

    assert quota.provider == "codex"
    assert quota.available is True
    assert quota.state == "within"
    assert quota.message == ""
    assert quota.stale is False
    assert quota.as_of == "2026-10-15T04:07:36.155Z"
    assert quota.plan == "pro"
    assert quota.credit_balance == "62500.0000000000"
    assert quota.enforcing is None
    assert len(quota.limits) == 1
    limit = quota.limits[0]
    assert limit.scope == "weekly"
    assert limit.label == "Weekly limit"
    assert limit.state == "within"
    assert limit.used_percent == 58
    assert limit.remaining_percent == 42
    assert limit.resets_at == "2100-01-01T00:00:00Z"


def test_codex_derives_near_and_over_from_hub_thresholds(codex_home) -> None:
    _write_rollout(
        codex_home,
        "rollout-near.jsonl",
        [_token_count_line(used_percent=float(pq.CODEX_NEAR_PERCENT))],
        mtime=1000.0,
    )
    assert pq.read_codex_quota().state == "near"

    _write_rollout(
        codex_home,
        "rollout-over.jsonl",
        [_token_count_line(used_percent=float(pq.CODEX_OVER_PERCENT))],
        mtime=2000.0,
    )
    assert pq.read_codex_quota().state == "over"


def test_codex_window_that_already_rolled_over_is_unknown_and_stale(codex_home) -> None:
    """An elapsed window's percentage describes a window that no longer exists."""
    _write_rollout(
        codex_home,
        "rollout-expired.jsonl",
        [_token_count_line(used_percent=50.0, resets_at=PAST_RESET)],
        mtime=1000.0,
    )

    quota = pq.read_codex_quota()

    assert quota.available is True
    assert quota.stale is True
    assert quota.state == "unknown"
    limit = quota.limits[0]
    assert limit.state == "unknown"
    assert limit.used_percent is None
    assert limit.remaining_percent is None
    # The elapsed boundary is retained as the evidence for withholding a figure.
    assert limit.resets_at == "2020-09-13T12:26:40Z"


def test_codex_maps_both_windows_with_stable_scopes(codex_home) -> None:
    _write_rollout(
        codex_home,
        "rollout-two.jsonl",
        [
            _token_count_line(
                used_percent=12.0,
                window_minutes=300,
                secondary={
                    "used_percent": 90.4,
                    "window_minutes": 1234,
                    "resets_at": FUTURE_RESET,
                },
            )
        ],
        mtime=1000.0,
    )

    quota = pq.read_codex_quota()

    assert [(limit.scope, limit.label) for limit in quota.limits] == [
        ("5h", "5-hour limit"),
        ("1234m", "1234-minute limit"),
    ]
    assert [limit.used_percent for limit in quota.limits] == [12, 90]
    # The provider badge takes the most severe of its windows.
    assert [limit.state for limit in quota.limits] == ["within", "near"]
    assert quota.state == "near"


def test_codex_falls_back_to_the_next_session_without_a_reading(codex_home) -> None:
    """Roughly one session in five is aborted before any reading is written."""
    _write_rollout(
        codex_home,
        "rollout-aborted.jsonl",
        [_chat_line("please describe the rate_limits payload")],
        mtime=5000.0,
    )
    _write_rollout(
        codex_home,
        "rollout-complete.jsonl",
        [_token_count_line(used_percent=33.0)],
        mtime=4000.0,
    )

    quota = pq.read_codex_quota()

    assert quota.available is True
    assert quota.limits[0].used_percent == 33


def test_codex_orders_by_mtime_not_by_name_or_directory(codex_home) -> None:
    """Resuming an old session appends today's records under its old date."""
    _write_rollout(
        codex_home,
        "rollout-2026-12-31T23-59-59-newest-name.jsonl",
        [_token_count_line(used_percent=11.0)],
        mtime=1000.0,
        day="31",
    )
    _write_rollout(
        codex_home,
        "rollout-2026-01-01T00-00-00-oldest-name.jsonl",
        [_token_count_line(used_percent=77.0)],
        mtime=9000.0,
        day="01",
    )

    quota = pq.read_codex_quota()

    assert quota.limits[0].used_percent == 77


def test_newest_rollout_survives_the_candidate_budget(codex_home, monkeypatch) -> None:
    """Candidates are capped by mtime, not by enumeration order.

    Truncating the scan in filesystem order would drop the newest file before
    anything is sorted, which is the one file this scan exists to find.
    """
    monkeypatch.setattr(pq, "_CODEX_MAX_CANDIDATES", 2)
    for index in range(5):
        _write_rollout(
            codex_home,
            f"rollout-{index}.jsonl",
            [_token_count_line(used_percent=float(index * 10))],
            mtime=1000.0 + index,
        )
    # Filesystem order is arbitrary; pin the worst case, newest enumerated last.
    real_iglob = pq.glob.iglob
    monkeypatch.setattr(
        pq.glob,
        "iglob",
        lambda pattern, recursive=False: sorted(
            real_iglob(pattern, recursive=recursive), key=lambda path: os.stat(path).st_mtime
        ),
    )

    assert pq.read_codex_quota().limits[0].used_percent == 40


def test_archived_sessions_compete_for_the_candidate_budget(codex_home, monkeypatch) -> None:
    """A crowded sessions/ must not evict a newer archived rollout."""
    monkeypatch.setattr(pq, "_CODEX_MAX_CANDIDATES", 2)
    for index in range(3):
        _write_rollout(
            codex_home,
            f"rollout-active-{index}.jsonl",
            [_token_count_line(used_percent=5.0)],
            mtime=1000.0 + index,
        )
    _write_rollout(
        codex_home,
        "rollout-archived.jsonl",
        [_token_count_line(used_percent=66.0)],
        mtime=9000.0,
        root="archived_sessions",
    )

    assert pq.read_codex_quota().limits[0].used_percent == 66


def test_scan_allowance_is_per_root(codex_home, monkeypatch) -> None:
    """The runaway guard is per root, so one root cannot starve the other."""
    monkeypatch.setattr(pq, "_CODEX_MAX_FILES_VISITED", 1)
    for index in range(3):
        _write_rollout(
            codex_home,
            f"rollout-active-{index}.jsonl",
            [_token_count_line(used_percent=5.0)],
            mtime=1000.0 + index,
        )
    _write_rollout(
        codex_home,
        "rollout-archived.jsonl",
        [_token_count_line(used_percent=66.0)],
        mtime=9000.0,
        root="archived_sessions",
    )

    assert pq.read_codex_quota().limits[0].used_percent == 66


def test_codex_skips_corrupt_and_non_token_count_records(codex_home) -> None:
    _write_rollout(
        codex_home,
        "rollout-mixed.jsonl",
        [
            _token_count_line(used_percent=42.0),
            # A later conversation turn quoting the payload must not be read
            # as a reading, nor may a non-token_count event or a torn line.
            _chat_line('the agent printed {"rate_limits": {"primary": {"used_percent": 99}}}'),
            _token_count_line(used_percent=99.0, payload_type="agent_message"),
            '{"timestamp": "2026-10-15T05:00:00.000Z", "type": "event_msg", "payload": '
            '{"type": "token_count", "rate_limits": {"primary"',
        ],
        mtime=1000.0,
    )

    quota = pq.read_codex_quota()

    assert quota.available is True
    assert quota.limits[0].used_percent == 42


@pytest.mark.parametrize("balance", ["62500", "62500.0000000000"])
def test_codex_credit_balance_is_forwarded_verbatim(codex_home, balance) -> None:
    _write_rollout(
        codex_home, "rollout-credits.jsonl", [_token_count_line(balance=balance)], mtime=1000.0
    )

    assert pq.read_codex_quota().credit_balance == balance


def test_codex_without_any_reading_reports_no_data(codex_home) -> None:
    quota = pq.read_codex_quota()

    assert quota.available is False
    assert quota.state == "no_data"
    assert quota.limits == []
    assert "Codex" in quota.message and "refresh" in quota.message


def test_codex_reads_only_the_file_tail(codex_home) -> None:
    """A reading buried far above EOF is out of scope; the next file wins."""
    padding = [_chat_line("x" * 200) for _ in range(pq._CODEX_TAIL_BYTES // 200 + 50)]
    _write_rollout(
        codex_home,
        "rollout-long.jsonl",
        [_token_count_line(used_percent=15.0)] + padding,
        mtime=5000.0,
    )
    _write_rollout(
        codex_home, "rollout-short.jsonl", [_token_count_line(used_percent=25.0)], mtime=4000.0
    )

    assert pq.read_codex_quota().limits[0].used_percent == 25


def test_codex_bounds_the_number_of_files_it_reads(codex_home, monkeypatch) -> None:
    monkeypatch.setattr(pq, "_CODEX_MAX_FILES_READ", 2)
    for index in range(4):
        _write_rollout(
            codex_home,
            f"rollout-{index}.jsonl",
            [_chat_line("no reading here")],
            mtime=1000.0 + index,
        )
    _write_rollout(codex_home, "rollout-old.jsonl", [_token_count_line()], mtime=10.0)

    quota = pq.read_codex_quota()

    assert quota.available is False
    assert quota.state == "no_data"


# -- MASO ------------------------------------------------------------------


MASO_PAYLOAD = {
    "state": "within",
    "models": [
        {
            "model": "gpt-6-astra",
            "state": "over",
            "percentUsed": 95,
            "effectiveState": "within",
            "effectiveUsedPercent": 14,
            "resetAt": "2026-10-15T16:00:00.000Z",
        },
        {
            "model": "ep-20260815235131-vfr5s",
            "state": "no_limit",
            "percentUsed": None,
            "effectiveState": "no_limit",
            "effectiveUsedPercent": None,
            "resetAt": None,
        },
    ],
    "grants": [],
    "enforcing": True,
    "fetchedAt": 1792036198.71,
}


MASO_MODELS = [
    {
        "key": "seed-stable",
        "model_name": "ep-20260815235131-vfr5s",
        "display_name": "seed-stable",
        "selector_primary_key": "seed-stable",
    },
    {
        "key": "seed-stable-reasoning",
        "model_name": "ep-20260815235131-vfr5s",
        "display_name": "seed-stable-reasoning",
        "selector_primary_key": "seed-stable",
    },
    {
        "key": "gpt-6-astra",
        "model_name": "gpt-6-astra",
        "display_name": "gpt-6-astra",
        "selector_primary_key": "gpt-6-astra",
    },
]


@pytest.fixture
def maso_service(monkeypatch):
    """Mock discovery plus the HTTP transport; never touch a live service."""
    state = {
        "payload": MASO_PAYLOAD,
        "status": 200,
        "body": None,
        "catalog": MASO_MODELS,
        "catalog_status": 200,
    }
    calls: list[str] = []
    original = httpx.AsyncClient

    async def discover(env):
        # Discovery fetches the catalog; the options carry both identities.
        return "http://maso.test", maso_http._model_options(state["catalog"]), "seed"

    monkeypatch.setattr(pq, "discover_maso_service", discover)

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/api/models":
            if state["catalog_status"] != 200:
                return httpx.Response(state["catalog_status"], json=[])
            return httpx.Response(200, json=state["catalog"])
        if state["body"] is not None:
            return httpx.Response(state["status"], text=state["body"])
        return httpx.Response(state["status"], json=state["payload"])

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state, calls


@pytest.mark.asyncio
async def test_maso_reading_prefers_the_effective_figures(maso_service) -> None:
    _state, calls = maso_service

    quota = await pq.read_maso_quota()

    assert quota.provider == "maso"
    assert quota.available is True
    assert quota.state == "within"
    assert quota.message == ""
    assert quota.stale is False
    assert quota.enforcing is True
    assert quota.plan is None and quota.credit_balance is None
    assert quota.as_of == "2026-10-15T03:49:58.710000Z"
    assert [limit.scope for limit in quota.limits] == ["gpt-6-astra", "ep-20260815235131-vfr5s"]
    # effectiveState/effectiveUsedPercent win over the raw figures.
    assert quota.limits[0].state == "within"
    assert quota.limits[0].used_percent == 14
    assert quota.limits[0].remaining_percent == 86
    assert quota.limits[0].resets_at == "2026-10-15T16:00:00.000Z"
    assert quota.limits[1].state == "no_limit"
    assert quota.limits[1].used_percent is None
    assert quota.limits[1].remaining_percent is None
    # The endpoint id becomes the model people select; scope keeps the endpoint.
    assert [limit.label for limit in quota.limits] == ["gpt-6-astra", "seed-stable"]
    assert calls == ["http://maso.test/api/llm-quota"]


@pytest.mark.asyncio
async def test_maso_quota_labels_an_unlisted_endpoint_as_unlisted(maso_service) -> None:
    """An endpoint no catalog row names must not pose as a model name."""
    state, calls = maso_service
    # Two selectors without a shared primary marker name one endpoint: the first
    # wins, and the unlisted endpoint is named for what it is.
    state["catalog"] = [
        {"key": "astra-first", "model_name": "gpt-6-astra"},
        {"key": "astra-second", "model_name": "gpt-6-astra"},
    ]

    quota = await pq.read_maso_quota()

    assert quota.available is True
    assert [limit.scope for limit in quota.limits] == ["gpt-6-astra", "ep-20260815235131-vfr5s"]
    assert [limit.label for limit in quota.limits] == [
        "astra-first",
        # The endpoint stays in scope; the label says what it is.
        "Unlisted endpoint · vfr5s",
    ]
    assert quota.limits[1].scope == "ep-20260815235131-vfr5s"
    # The raw catalog was consulted once, and still had no row for it.
    assert calls == ["http://maso.test/api/llm-quota", "http://maso.test/api/models"]


@pytest.mark.asyncio
async def test_maso_quota_names_every_endpoint_of_a_split_selector_group(
    maso_service,
) -> None:
    """One selector group may name several endpoints; none of them is unlisted."""
    state, _calls = maso_service
    state["catalog"] = [
        {"key": "seed", "model_name": "ep-a", "selector_primary_key": "seed"},
        {"key": "seed-high", "model_name": "ep-b", "selector_primary_key": "seed"},
    ]
    state["payload"] = {
        "state": "within",
        "models": [
            {
                "model": "ep-a",
                "state": "within",
                "effectiveState": "within",
                "effectiveUsedPercent": 10,
            },
            {
                "model": "ep-b",
                "state": "within",
                "effectiveState": "within",
                "effectiveUsedPercent": 20,
            },
        ],
        "grants": [],
        "enforcing": True,
        "fetchedAt": 1792036198.71,
    }

    quota = await pq.read_maso_quota()

    # The model picker keeps one option per group (ep-a), so ep-b only has a
    # name because the raw catalog was read.
    assert [limit.label for limit in quota.limits] == ["seed", "seed-high"]


@pytest.mark.asyncio
async def test_maso_quota_names_unlisted_endpoints_with_the_catalog_failing(
    maso_service,
) -> None:
    """A broken catalog never fails the reading; it just leaves rows unlisted."""
    state, _calls = maso_service
    state["catalog_status"] = 500
    state["payload"] = {
        "state": "within",
        "models": [
            {"model": "gpt-6-astra", "state": "within", "effectiveUsedPercent": 14},
            {"model": "ep-20260815235131-vfr5s", "state": "no_limit"},
            {"model": "ep-3-zzz", "state": "no_data"},
        ],
        "grants": [],
        "enforcing": True,
        "fetchedAt": 1792036198.71,
    }

    quota = await pq.read_maso_quota()

    # The options discovery already returned still name what they cover; the
    # endpoint only the failing catalog could have named stays unlisted.
    assert quota.available is True
    assert [limit.label for limit in quota.limits] == [
        "gpt-6-astra",
        "seed-stable",
        "Unlisted endpoint · zzz",
    ]


def test_maso_unlisted_labels_stay_distinct_when_codes_collide() -> None:
    """Two endpoints sharing a trailing code keep their full ids."""
    limits = pq._maso_limits(
        [{"model": "ep-1-abcde"}, {"model": "ep-2-abcde"}, {"model": "ep-3-fghij"}],
        {},
    )

    assert [limit.label for limit in limits] == [
        "ep-1-abcde",
        "ep-2-abcde",
        "Unlisted endpoint · fghij",
    ]


def test_maso_catalog_options_carry_the_endpoint_a_quota_row_names() -> None:
    """Several selector keys share one endpoint; one option names that group."""
    options = maso_http._model_options(
        [
            {
                "key": "seed-stable-reasoning-high",
                "model_name": "ep-1",
                "selector_primary_key": "seed-stable",
            },
            {"key": "seed-stable", "model_name": "ep-1", "selector_primary_key": "seed-stable"},
            {"key": "gone", "model_name": "ep-2", "deprecated": True},
            {"key": "solo", "model_name": "ep-3"},
        ]
    )

    assert [(option.label, option.description) for option in options] == [
        ("seed-stable", "ep-1"),
        ("solo", "ep-3"),
    ]


@pytest.mark.asyncio
async def test_maso_refresh_is_forwarded_upstream(maso_service) -> None:
    _state, calls = maso_service

    await pq.read_maso_quota(refresh=True)

    # The labels come from discovery's catalog, so the reading makes one request.
    assert calls == ["http://maso.test/api/llm-quota?refresh=true"]


@pytest.mark.asyncio
async def test_maso_unavailable_state_is_reported_as_unavailable(maso_service) -> None:
    state, _calls = maso_service
    state["payload"] = {"state": "unavailable", "models": [], "grants": None, "enforcing": True}

    quota = await pq.read_maso_quota()

    assert quota.available is False
    assert quota.state == "unavailable"
    assert quota.limits == []
    assert "MASO" in quota.message


@pytest.mark.asyncio
async def test_maso_http_failure_hides_upstream_detail(maso_service) -> None:
    state, _calls = maso_service
    state["status"] = 503
    state["body"] = "secret upstream response"

    quota = await pq.read_maso_quota()

    assert quota.available is False
    assert quota.state == "unavailable"
    assert "secret" not in quota.message


@pytest.fixture
def maso_deployment(tmp_path, monkeypatch):
    """Own every signal that says whether MASO belongs to this host.

    Returns the registry path so a test can declare an install; discovery is
    stubbed to fail, which is what makes the classification observable.
    """
    monkeypatch.delenv("MASO_BASE_URL", raising=False)
    monkeypatch.delenv("MASO_PORT", raising=False)
    monkeypatch.setattr(maso_http.settings, "maso_base_url", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    registry = tmp_path / ".maso" / "instances.json"
    registry.parent.mkdir()

    async def discover(env):
        raise MasoServiceUnavailable(
            "MASO service is unavailable: no usable local service was found. "
            "Start MASO or set MASO_BASE_URL, then retry."
        )

    monkeypatch.setattr(pq, "discover_maso_service", discover)
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_text", [None, "{}", "not json", '{"a": {"port": "11004"}}'])
async def test_maso_absent_from_this_deployment_is_not_an_alert(
    maso_deployment, registry_text
) -> None:
    """A Hub that never deploys MASO must not be told to go repair it."""
    if registry_text is not None:
        maso_deployment.write_text(registry_text)

    quota = await pq.read_maso_quota()

    assert quota.provider == "maso"
    assert quota.available is False
    assert quota.state == "not_configured"
    assert quota.limits == []
    assert quota.as_of is None
    assert quota.stale is False
    assert "No MASO service is configured" in quota.message
    # A statement of fact, never an instruction to fix something.
    assert "Start MASO" not in quota.message and "retry" not in quota.message


@pytest.mark.asyncio
async def test_registered_but_unreachable_maso_is_a_real_fault(maso_deployment) -> None:
    """A registry entry means MASO lives here, so a failed read is a fault."""
    maso_deployment.write_text('{"11004": {"port": 11004}}')

    quota = await pq.read_maso_quota()

    assert quota.available is False
    assert quota.state == "unavailable"
    assert "Start MASO or set MASO_BASE_URL" in quota.message


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["env_url", "env_port", "settings_url"])
async def test_explicitly_configured_maso_that_is_down_is_a_real_fault(
    maso_deployment, monkeypatch, source
) -> None:
    if source == "env_url":
        monkeypatch.setenv("MASO_BASE_URL", "http://127.0.0.1:11004")
    elif source == "env_port":
        monkeypatch.setenv("MASO_PORT", "11004")
    else:
        monkeypatch.setattr(maso_http.settings, "maso_base_url", "http://127.0.0.1:11004")

    quota = await pq.read_maso_quota()

    assert quota.available is False
    assert quota.state == "unavailable"
    assert "Start MASO or set MASO_BASE_URL" in quota.message


@pytest.mark.asyncio
async def test_unreadable_registry_still_counts_as_an_install(maso_deployment) -> None:
    """A registry too large to parse is evidence, not an absence of MASO."""
    maso_deployment.write_bytes(b" " * (64 * 1024 + 1))

    quota = await pq.read_maso_quota()

    assert quota.state == "unavailable"


@pytest.mark.asyncio
async def test_maso_discovery_failure_is_reported_not_raised(maso_deployment, monkeypatch) -> None:
    maso_deployment.write_text('{"11004": {"port": 11004}}')

    async def discover(env):
        raise MasoServiceUnavailable(
            "Multiple local MASO services are ready (a, b). "
            "Set MASO_BASE_URL to the intended service, then retry."
        )

    monkeypatch.setattr(pq, "discover_maso_service", discover)

    quota = await pq.read_maso_quota()

    assert quota.available is False
    assert quota.state == "unavailable"
    assert "MASO_BASE_URL" in quota.message


# -- Antigravity -----------------------------------------------------------


ANTIGRAVITY_USAGE_PAYLOAD = {
    "conversation_id": "",
    "status": "SUCCESS",
    "response": "Gemini Models\tWeekly Limit Remaining\t97%\t2100-01-01T00:00:00Z",
    "command": {
        "name": "usage",
        "data": {
            "description": "Within each group, models share a weekly limit and a 5-hour limit.",
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.971,
                            "reset_time": "2100-01-06T17:50:30Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.939,
                            "reset_time": "2100-01-01T12:56:36Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.998,
                            "reset_time": "2100-01-07T07:16:52Z",
                        },
                        {
                            "id": "3p-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.85,
                            "reset_time": "2100-01-01T12:16:52Z",
                        },
                    ],
                },
            ],
        },
    },
}


@pytest.fixture
def antigravity_service(tmp_path, monkeypatch):
    """Mock Antigravity login token, binary presence, and ``/usage`` output."""
    home = tmp_path / "antigravity"
    home.mkdir(parents=True, exist_ok=True)
    (home / "antigravity-oauth-token").write_text("{}")
    monkeypatch.setenv("ANTIGRAVITY_HOME", str(home))
    monkeypatch.setattr(pq.shutil, "which", lambda binary: f"/fake/{binary}")
    state = {"payload": ANTIGRAVITY_USAGE_PAYLOAD, "error": None}
    calls: list[int] = []

    async def fake_run():
        calls.append(1)
        if state["error"] is not None:
            raise state["error"]
        return state["payload"]

    monkeypatch.setattr(pq, "_run_antigravity_usage", fake_run)
    return state, calls


@pytest.mark.asyncio
async def test_antigravity_reading_maps_groups_and_buckets(antigravity_service) -> None:
    _state, calls = antigravity_service

    quota = await pq.read_antigravity_quota()

    assert quota.provider == "antigravity"
    assert quota.available is True
    assert quota.state == "within"
    assert quota.message == ""
    assert quota.stale is False
    assert quota.as_of is not None
    assert [(limit.scope, limit.label) for limit in quota.limits] == [
        ("gemini-weekly", "Gemini Models · Weekly limit"),
        ("gemini-5h", "Gemini Models · 5-hour limit"),
        ("3p-weekly", "Claude and GPT models · Weekly limit"),
        ("3p-5h", "Claude and GPT models · 5-hour limit"),
    ]
    assert [limit.remaining_percent for limit in quota.limits] == [97, 94, 100, 85]
    assert [limit.used_percent for limit in quota.limits] == [3, 6, 0, 15]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_antigravity_derives_near_over_disabled_and_expired_windows(
    antigravity_service,
) -> None:
    state, _calls = antigravity_service
    state["payload"] = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "near-bucket",
                                "window": "5h",
                                "remaining_fraction": 0.15,
                                "reset_time": "2100-01-01T00:00:00Z",
                            },
                            {
                                "id": "disabled-bucket",
                                "window": "weekly",
                                "remaining_fraction": 0.50,
                                "disabled": True,
                                "reset_time": "2100-01-01T00:00:00Z",
                            },
                            {
                                "id": "expired-bucket",
                                "name": "Custom Window",
                                "remaining_fraction": 0.40,
                                "reset_time": "2020-01-01T00:00:00Z",
                            },
                        ],
                    }
                ]
            },
        },
    }

    quota = await pq.read_antigravity_quota()

    assert quota.available is True
    assert quota.stale is True
    assert quota.state == "over"
    near, disabled, expired = quota.limits
    assert near.state == "near" and near.used_percent == 85 and near.remaining_percent == 15
    assert disabled.state == "over" and disabled.used_percent == 50
    assert (
        expired.state == "unknown"
        and expired.used_percent is None
        and expired.remaining_percent is None
    )
    assert expired.resets_at == "2020-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_antigravity_absent_from_deployment_is_not_an_alert() -> None:
    quota = await pq.read_antigravity_quota()

    assert quota.provider == "antigravity"
    assert quota.available is False
    assert quota.state == "not_configured"
    assert quota.limits == []
    assert quota.as_of is None
    assert "No Antigravity CLI is configured" in quota.message


@pytest.mark.asyncio
async def test_antigravity_configured_but_failing_reports_unavailable(
    antigravity_service, monkeypatch
) -> None:
    state, _calls = antigravity_service
    state["error"] = RuntimeError("proxy unreachable")

    quota = await pq.read_antigravity_quota()
    assert quota.available is False
    assert quota.state == "unavailable"
    assert "Antigravity quota is unavailable" in quota.message

    monkeypatch.setattr(pq.shutil, "which", lambda _binary: None)
    missing_bin = await pq.read_antigravity_quota()
    assert missing_bin.available is False
    assert missing_bin.state == "unavailable"


# -- Combined reading ------------------------------------------------------


@pytest.mark.asyncio
async def test_providers_are_returned_in_a_fixed_order(
    codex_home, maso_service, antigravity_service
) -> None:
    _write_rollout(codex_home, "rollout-a.jsonl", [_token_count_line()], mtime=1000.0)

    response = await pq.read_provider_quota()

    assert [provider.provider for provider in response.providers] == [
        "codex",
        "maso",
        "antigravity",
    ]
    assert all(provider.available for provider in response.providers)


@pytest.mark.asyncio
async def test_reading_is_cached_until_refresh(
    codex_home, maso_service, antigravity_service
) -> None:
    _state, calls = maso_service
    _agy_state, agy_calls = antigravity_service
    _write_rollout(codex_home, "rollout-a.jsonl", [_token_count_line()], mtime=1000.0)

    first = await pq.read_provider_quota()
    assert await pq.read_provider_quota() == first
    assert len(calls) == 1
    assert len(agy_calls) == 1

    await pq.read_provider_quota(refresh=True)
    assert len(calls) == 2
    assert len(agy_calls) == 2


@pytest.mark.asyncio
async def test_unexpected_provider_failure_does_not_fail_the_reading(
    monkeypatch, maso_service, antigravity_service
) -> None:
    def explode() -> pq.ProviderQuota:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(pq, "read_codex_quota", explode)

    response = await pq.read_provider_quota()

    codex, maso, antigravity = response.providers
    assert codex.available is False and codex.state == "unavailable"
    assert maso.available is True
    assert antigravity.available is True


@pytest.mark.asyncio
@pytest.mark.parametrize("first_refresh", [False, True], ids=["join-passive", "join-forced"])
async def test_concurrent_refreshes_share_one_query_and_survive_a_cancel(
    monkeypatch, first_refresh
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def read(refresh=False):
        calls.append(refresh)
        started.set()
        await release.wait()
        return pq.ProviderQuota(provider="antigravity", available=True, state="within")

    monkeypatch.setattr(pq, "read_antigravity_quota", read)
    first = asyncio.create_task(pq.read_provider_quota(first_refresh, "antigravity"))
    await asyncio.wait_for(started.wait(), 1)
    followers = [asyncio.create_task(pq.read_provider_quota(True, "antigravity")) for _ in range(5)]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # Let all followers attach before the provider finishes.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [first_refresh]
    release.set()
    results = await asyncio.wait_for(asyncio.gather(*followers), 1)
    assert all(result.providers[0].available for result in results)
    assert calls == [first_refresh]
    assert not pq._QUOTA_IN_FLIGHT
    assert await pq.read_provider_quota(provider="antigravity") == results[0]
    assert calls == [first_refresh]


@pytest.mark.asyncio
async def test_force_refresh_waits_for_cached_query_then_refreshes_once(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    force_started = asyncio.Event()
    force_release = asyncio.Event()
    calls = []

    async def read(refresh=False):
        calls.append(refresh)
        if refresh:
            force_started.set()
            await force_release.wait()
        else:
            started.set()
            await release.wait()
        return pq.ProviderQuota(provider="maso", available=True, state="within", as_of=str(refresh))

    monkeypatch.setattr(pq, "read_maso_quota", read)
    normal = asyncio.create_task(pq.read_provider_quota(provider="maso"))
    await asyncio.wait_for(started.wait(), 1)
    forced = [asyncio.create_task(pq.read_provider_quota(True, "maso")) for _ in range(3)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [False]
    release.set()
    await asyncio.wait_for(force_started.wait(), 1)
    assert calls == [False, True]
    force_release.set()
    assert (await normal).providers[0].as_of == "False"
    results = await asyncio.wait_for(asyncio.gather(*forced), 1)
    assert all(result.providers[0].as_of == "True" for result in results)
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_codex_filesystem_scan_does_not_block_other_providers(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def scan():
        started.set()
        assert release.wait(3)
        return pq.ProviderQuota(provider="codex", available=True, state="within")

    async def read(refresh=False):
        return pq.ProviderQuota(provider="maso", available=True, state="within")

    monkeypatch.setattr(pq, "read_codex_quota", scan)
    monkeypatch.setattr(pq, "read_maso_quota", read)
    codex = asyncio.create_task(pq.read_provider_quota(provider="codex"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        maso = await asyncio.wait_for(pq.read_provider_quota(provider="maso"), 1)
        assert maso.providers[0].available
        assert not codex.done()
    finally:
        release.set()
        await codex


@pytest.mark.asyncio
async def test_provider_cache_expires_independently(antigravity_service) -> None:
    _state, calls = antigravity_service
    await pq.read_provider_quota(provider="antigravity")
    _, reading = pq._QUOTA_CACHE["antigravity"]
    pq._QUOTA_CACHE["antigravity"] = (0, reading)
    await pq.read_provider_quota(provider="antigravity")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_refresh_retains_last_success_without_claiming_freshness(
    antigravity_service,
) -> None:
    state, _calls = antigravity_service
    first = (await pq.read_provider_quota(provider="antigravity")).providers[0]
    state["error"] = TimeoutError()
    for _ in range(2):
        failed = (await pq.read_provider_quota(True, "antigravity")).providers[0]
        assert failed.available is False and failed.state == "unavailable"
        assert failed.stale is True
        assert failed.as_of == first.as_of
        assert failed.limits == first.limits
        assert "timed out" in failed.message
        assert "last successful reading" in failed.message
    state["error"] = None
    recovered = (await pq.read_provider_quota(True, "antigravity")).providers[0]
    assert recovered.available is True and recovered.stale is False
    assert recovered.message == ""


@pytest.mark.asyncio
async def test_unconfigured_provider_does_not_retain_previous_account_reading(
    antigravity_service, monkeypatch
) -> None:
    await pq.read_provider_quota(provider="antigravity")
    monkeypatch.setattr(pq, "_antigravity_absent_from_deployment", lambda: True)
    missing = (await pq.read_provider_quota(True, "antigravity")).providers[0]
    assert missing.state == "not_configured"
    assert missing.limits == [] and missing.as_of is None


def test_retained_values_are_cleared_when_reset_has_passed() -> None:
    old = pq.ProviderQuota(
        provider="antigravity",
        available=True,
        state="over",
        limits=[
            pq.QuotaLimit(
                scope="weekly",
                label="Weekly",
                state="over",
                remaining_percent=0,
                used_percent=100,
                resets_at="2020-01-01T00:00:00Z",
            )
        ],
    )
    retained = pq._retain_last_reading(pq._antigravity_unavailable(), old)
    assert retained.limits[0].state == "unknown"
    assert retained.limits[0].remaining_percent is None
    assert retained.limits[0].used_percent is None
    assert old.limits[0].used_percent == 100


@pytest.mark.asyncio
async def test_antigravity_timeout_is_not_reported_as_a_login_failure(antigravity_service) -> None:
    state, _calls = antigravity_service
    state["error"] = TimeoutError()
    reading = await pq.read_antigravity_quota()
    assert reading.state == "unavailable" and not reading.available
    assert "timed out" in reading.message
    assert "login" not in reading.message
    assert pq._ANTIGRAVITY_TIMEOUT_S > 12  # A successful live /usage took 12.001s.


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
async def test_antigravity_query_reaps_its_process_on_timeout_or_cancel(
    monkeypatch, cancel
) -> None:
    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        proc = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(pq.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(pq, "_ANTIGRAVITY_TIMEOUT_S", 5 if cancel else 0.01)
    task = asyncio.create_task(pq._run_antigravity_usage())
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await asyncio.wait_for(task, 2)
    assert processes[0].returncode is not None
