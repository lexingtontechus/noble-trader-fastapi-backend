"""
Tests for the Supabase Realtime broadcast publisher (Talaria dashboard).

Covers:
  - build_signal_payload: shape, 6dp full-value price rule, kelly preference
  - broadcast_signal / broadcast_paper wire the correct phoenix frames
    (phx_join on the right topic + broadcast with the right event/payload)
  - plan-scoped signal topics: plan_ids → realtime:signals.<slug> routing
    (fail-open to BOTH topics when no plan UUIDs are configured / unmatched)
  - env toggle NOBLE_REALTIME_BROADCAST=0 disables publishing
  - no running event loop → skip silently (never raises)
  - client failure → swallowed + counted, never raised
  - wiring modules still import cleanly after the broadcast call sites
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator import realtime_publisher as rp

SCOUT_UUID = "df980ef1-e41f-41db-9d04-2ad09da69626"
PRO_UUID = "1b66e78e-e8d1-46b6-9887-b36e038131c5"


# ─── Fakes ─────────────────────────────────────────────────────────────


class FakeWS:
    """Minimal websocket stand-in: records sent frames, replies to joins."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))

    async def recv(self):
        # Server reply to the phx_join (status ok).
        return json.dumps({
            "topic": "realtime:signals.signal_scout",
            "event": "phx_reply",
            "payload": {"status": "ok", "response": {}},
            "ref": "1",
        })

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture(autouse=True)
def _enabled_settings(monkeypatch):
    """Default test env: Supabase configured + broadcast enabled."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://abc.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "service-role-key")
    monkeypatch.setattr(settings, "NOBLE_REALTIME_BROADCAST", "1")
    monkeypatch.setattr(settings, "NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID", SCOUT_UUID)
    monkeypatch.setattr(settings, "NOBLE_TRADER_PLAN_PRECISION_PRO_ID", PRO_UUID)
    rp._failures.clear()
    yield


# ─── Payload builder ───────────────────────────────────────────────────


class TestBuildSignalPayload:
    def test_shape_and_full_value_prices(self):
        payload = rp.build_signal_payload(
            symbol="XAUUSD",
            direction="buy",
            entry_price=4030.325456789,
            stop_loss=4027.325456789,
            take_profit=4033.325456789,
            effective_kelly=0.123456789,
        )
        assert set(payload) == {
            "symbol", "direction", "kelly",
            "entry_price", "stop_loss", "take_profit", "ts",
        }
        # 6dp FULL value — never truncated to per-symbol decimals (2dp would
        # flatten XAUUSD 4030.32…); same rule as _build_signal_sim_payload.
        assert payload["entry_price"] == pytest.approx(4030.325457)
        assert payload["stop_loss"] == pytest.approx(4027.325457)
        assert payload["take_profit"] == pytest.approx(4033.325457)
        assert payload["symbol"] == "XAUUSD"
        assert payload["direction"] == "buy"

    def test_kelly_prefers_effective(self):
        payload = rp.build_signal_payload(
            symbol="EURUSD", direction="sell", entry_price=1.10,
            stop_loss=1.11, take_profit=1.09,
            kelly_f=0.08, effective_kelly=0.12,
        )
        assert payload["kelly"] == pytest.approx(0.12)

    def test_kelly_falls_back_to_kelly_f(self):
        payload = rp.build_signal_payload(
            symbol="EURUSD", direction="sell", entry_price=1.10,
            stop_loss=1.11, take_profit=1.09, kelly_f=0.08,
        )
        assert payload["kelly"] == pytest.approx(0.08)

    def test_kelly_none_when_both_missing(self):
        payload = rp.build_signal_payload(
            symbol="EURUSD", direction="buy", entry_price=1.10,
            stop_loss=1.11, take_profit=1.09,
        )
        assert payload["kelly"] is None

    def test_ts_passthrough(self):
        payload = rp.build_signal_payload(
            symbol="AUDUSD", direction="buy", entry_price=0.69,
            stop_loss=0.688, take_profit=0.694, ts=1234.5,
        )
        assert payload["ts"] == pytest.approx(1234.5)


# ─── Plan-scoped topic resolution ──────────────────────────────────────


class TestPlanSignalTopics:
    def test_no_plan_ids_fails_open_to_both(self):
        assert rp._plan_signal_topics(None) == [
            rp.SIGNAL_TOPIC_SCOUT, rp.SIGNAL_TOPIC_PRO,
        ]
        assert rp._plan_signal_topics([]) == [
            rp.SIGNAL_TOPIC_SCOUT, rp.SIGNAL_TOPIC_PRO,
        ]

    def test_scout_only(self):
        assert rp._plan_signal_topics([SCOUT_UUID]) == [rp.SIGNAL_TOPIC_SCOUT]

    def test_pro_only(self):
        assert rp._plan_signal_topics([PRO_UUID]) == [rp.SIGNAL_TOPIC_PRO]

    def test_both_plans(self):
        # Order-insensitive: sorted() yields precision_pro before
        # signal_scout — the set is what matters (each topic gets its own
        # broadcast).
        assert set(rp._plan_signal_topics([SCOUT_UUID, PRO_UUID])) == {
            rp.SIGNAL_TOPIC_SCOUT, rp.SIGNAL_TOPIC_PRO,
        }

    def test_unrecognized_plan_id_fails_open_to_both(self):
        assert rp._plan_signal_topics(["479635b8-8d1f-40b2-9692-fd0118f72e7a"]) == [
            rp.SIGNAL_TOPIC_SCOUT, rp.SIGNAL_TOPIC_PRO,
        ]

    def test_case_and_whitespace_insensitive(self):
        assert rp._plan_signal_topics([f"  {SCOUT_UUID.upper()}  "]) == [
            rp.SIGNAL_TOPIC_SCOUT,
        ]

    def test_no_configured_uuids_fails_open_to_both(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID", "")
        monkeypatch.setattr(settings, "NOBLE_TRADER_PLAN_PRECISION_PRO_ID", "")
        assert rp._plan_signal_topics([SCOUT_UUID]) == [
            rp.SIGNAL_TOPIC_SCOUT, rp.SIGNAL_TOPIC_PRO,
        ]


# ─── Transport: phoenix frames over the mocked websocket ───────────────


class TestBroadcastTransport:
    def _publish(self, topic, event, payload, monkeypatch):
        fake = FakeWS()
        mock_connect = MagicMock(return_value=fake)
        monkeypatch.setattr(rp.websockets, "connect", mock_connect)
        asyncio.run(rp._publish(topic, event, payload))
        return fake, mock_connect

    def test_broadcast_signal_joins_scout_topic_and_sends(self, monkeypatch):
        payload = {"symbol": "XAUUSD", "direction": "buy", "ts": 1.0}
        fake, mock_connect = self._publish(
            rp.SIGNAL_TOPIC_SCOUT, rp.SIGNALS_EVENT, payload, monkeypatch,
        )

        # Connection: /realtime/v1/websocket with apikey + vsn query params.
        url = mock_connect.call_args.args[0]
        assert "/realtime/v1/websocket" in url
        assert "apikey=service-role-key" in url
        assert "vsn=1.0.0" in url

        # Frame 1: phoenix join on the plan-scoped signals topic.
        join = fake.sent[0]
        assert join["topic"] == "realtime:signals.signal_scout"
        assert join["event"] == "phx_join"

        # Frame 2: the broadcast event.
        bcast = fake.sent[1]
        assert bcast["topic"] == "realtime:signals.signal_scout"
        assert bcast["event"] == "broadcast"
        assert bcast["payload"]["type"] == "broadcast"
        assert bcast["payload"]["event"] == "signal"
        assert bcast["payload"]["payload"] == payload

    def test_broadcast_paper_joins_portfolio_topic_and_sends(self, monkeypatch):
        payload = {"type": "opened", "symbol": "EURUSD", "ts": 2.0}
        fake, _ = self._publish(rp.PAPER_TOPIC, rp.PAPER_EVENT, payload, monkeypatch)

        assert fake.sent[0]["topic"] == "realtime:portfolio"
        bcast = fake.sent[1]
        assert bcast["event"] == "broadcast"
        assert bcast["payload"]["event"] == "paper"
        assert bcast["payload"]["payload"] == payload

    def test_publish_swallows_client_failure_and_counts(self, monkeypatch):
        monkeypatch.setattr(
            rp.websockets, "connect",
            MagicMock(side_effect=ConnectionError("boom")),
        )
        # Must NOT raise out of _publish (the task-level catch).
        asyncio.run(rp._publish(rp.SIGNAL_TOPIC_SCOUT, rp.SIGNALS_EVENT, {"symbol": "X"}))
        assert rp.failure_count(rp.SIGNAL_TOPIC_SCOUT) == 1
        # A later successful publish resets nothing but keeps counting sane:
        assert rp.failure_count(rp.PAPER_TOPIC) == 0

    def test_publish_swallows_timeout(self, monkeypatch):
        def _timeout(*args, **kwargs):
            raise asyncio.TimeoutError("timed out")

        monkeypatch.setattr(rp.websockets, "connect", _timeout)
        asyncio.run(rp._publish(rp.SIGNAL_TOPIC_SCOUT, rp.SIGNALS_EVENT, {"symbol": "X"}))
        assert rp.failure_count(rp.SIGNAL_TOPIC_SCOUT) == 1


# ─── Fire-and-forget semantics ─────────────────────────────────────────


class TestFireAndForget:
    def test_disabled_when_toggle_0(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_REALTIME_BROADCAST", "0")
        calls = []
        monkeypatch.setattr(rp, "_fire_and_forget", lambda *a, **k: calls.append(a))
        rp.broadcast_signal({"symbol": "XAUUSD"})
        rp.broadcast_paper({"type": "opened"})
        assert calls == []  # nothing scheduled, nothing published

    def test_disabled_when_supabase_not_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "SUPABASE_URL", "")
        calls = []
        monkeypatch.setattr(rp, "_fire_and_forget", lambda *a, **k: calls.append(a))
        rp.broadcast_signal({"symbol": "XAUUSD"})
        assert calls == []

    def test_no_running_loop_skips_silently(self):
        # Called outside any event loop — must return quietly, never raise.
        assert rp.broadcast_signal({"symbol": "XAUUSD"}) is None
        assert rp.broadcast_paper({"type": "opened"}) is None

    def test_schedules_task_on_running_loop(self, monkeypatch):
        scheduled = []

        async def fake_publish(topic, event, payload):
            scheduled.append((topic, event, payload))

        monkeypatch.setattr(rp, "_publish", fake_publish)

        async def _run():
            rp.broadcast_signal({"symbol": "XAUUSD", "ts": 1.0})
            await asyncio.sleep(0)  # let the scheduled tasks execute

        asyncio.run(_run())
        # No plan_ids → fail-open to BOTH plan topics.
        assert scheduled == [
            (rp.SIGNAL_TOPIC_SCOUT, rp.SIGNALS_EVENT, {"symbol": "XAUUSD", "ts": 1.0}),
            (rp.SIGNAL_TOPIC_PRO, rp.SIGNALS_EVENT, {"symbol": "XAUUSD", "ts": 1.0}),
        ]

    def test_schedules_scout_topic_when_plan_ids_scout(self, monkeypatch):
        scheduled = []

        async def fake_publish(topic, event, payload):
            scheduled.append((topic, event, payload))

        monkeypatch.setattr(rp, "_publish", fake_publish)

        async def _run():
            rp.broadcast_signal({"symbol": "XAUUSD", "ts": 1.0}, plan_ids=[SCOUT_UUID])
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert scheduled == [
            (rp.SIGNAL_TOPIC_SCOUT, rp.SIGNALS_EVENT, {"symbol": "XAUUSD", "ts": 1.0}),
        ]

    def test_paper_schedules_task_on_running_loop(self, monkeypatch):
        scheduled = []

        async def fake_publish(topic, event, payload):
            scheduled.append((topic, event, payload))

        monkeypatch.setattr(rp, "_publish", fake_publish)

        async def _run():
            rp.broadcast_paper({"type": "closed"})
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert scheduled == [(rp.PAPER_TOPIC, rp.PAPER_EVENT, {"type": "closed"})]


# ─── Wiring smoke test ─────────────────────────────────────────────────


class TestWiringIntegrity:
    def test_wiring_modules_import_cleanly(self):
        # The three wired modules import without error after adding the
        # broadcast call sites (catches CRLF/syntax breakage in the wiring).
        import regime_platform.sweep_orchestrator.orchestrator  # noqa: F401
        import regime_platform.sweep_orchestrator.heavy_sweep  # noqa: F401
        import regime_platform.sweep_orchestrator.paper_book  # noqa: F401
        assert rp.SIGNAL_TOPIC_SCOUT == "realtime:signals.signal_scout"
        assert rp.SIGNAL_TOPIC_PRO == "realtime:signals.precision_pro"
        assert rp.PAPER_TOPIC == "realtime:portfolio"
