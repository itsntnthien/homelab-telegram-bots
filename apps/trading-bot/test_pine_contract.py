"""Hand-calculated contract examples, not a claim of TradingView parity."""
import json
import hashlib
import sqlite3
import tempfile
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bot
from market_quality import inspect_bars
from pine_config import PineConfig, SECONDS, ENGINE_VERSION, require_parity_report
from pine_engine import PineEngine
from pine_execution import PineExecution, context_authorized
from pine_runtime import PineRunner
from signal_engine import Bar
from tick_journal import install, append, read_after, history_request
from trades import TradeStore

AT = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def candidate(tf="m1", **changes):
    c = dict(key="literal:"+tf, entry=400000, sl=399800, tp=400400,
             direction=1, family=1, subtype=101, quality=90, grade=2,
             regime=7, atr=200, rr=2000, target_id=None)
    if tf == "m15":
        c.update(family=3, subtype=302, regime=2)
    c.update(changes)
    return c


def context(tf="m1", **changes):
    c = dict(regime=2 if tf == "m15" else 7, strength=80,
             structure_event=1, break_direction=1)
    c.update(changes)
    return c


class PineContractTest(unittest.TestCase):
    def test_intrabar_context_does_not_commit_future_ohlc(self):
        engine = PineEngine("m1")
        for i in range(60):
            price = 4000+i*.1
            engine.close_bar(Bar((AT+timedelta(minutes=i)).isoformat(), price, price+.2, price-.1, price+.1, 1), historical=True)
        fields = ("bars", "fast", "slow", "atr", "count", "bias")
        before = {k: json.dumps(engine.s[k]) for k in fields}
        at = AT+timedelta(hours=1)
        first = engine._intrabar_context(at.isoformat(), 4006)
        saved = engine.snapshot()
        second = engine._intrabar_context((at+timedelta(seconds=1)).isoformat(), 3990)
        self.assertEqual(before, {k: json.dumps(engine.s[k]) for k in fields})
        self.assertEqual(saved["live_bar"]["low"], 4006)
        self.assertEqual(engine.s["live_bar"]["low"], 3990)
        self.assertNotEqual(first["strength"], second["strength"])

    def test_pine_width_na_is_preserved_until_reversal_pinch(self):
        engine = PineEngine("m1")
        for i in range(60):
            engine.close_bar(Bar((AT+timedelta(minutes=i)).isoformat(), 4000, 4001, 3999, 4000, 1), historical=True)
        self.assertIsNotNone(engine.s["sigma_baseline"])
        self.assertTrue(engine.s["polarity_width_na"])
        self.assertIsNone(engine.polarity.half_width)
        restored = PineEngine("m1", state=json.loads(json.dumps(engine.snapshot())))
        self.assertEqual(restored.snapshot(), engine.snapshot())

    def test_external_parity_checks_negative_steps_and_rejects_self_generated_origin(self):
        from pine_parity import report
        cfg = PineConfig()
        self.assertFalse(report()["ok"])
        self.assertTrue(all(v["status"] == "BLOCKED" for v in report()["timeframes"].values()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = dict(reference_origin="tradingview-pine", source_sha256=cfg.source_sha256,
                            config_hash=cfg.hash, provider_symbol="TEST:HAND_WRITTEN",
                            input_mode="closed_bar_and_ordered_ticks", timeframes={})
            for tf, seconds in SECONDS.items():
                rows = [dict(kind="bar", bar=dict(timestamp=AT.isoformat(), open=100, high=101, low=99, close=100, volume=1)),
                        dict(kind="tick", timestamp=(AT+timedelta(seconds=seconds)).isoformat(), bid=100, ask=100.3)]
                expected = [{"state": {"bar_time": AT.isoformat(), "regime": 1, "bias": 0, "strength": 0, "winner": None}, "events": []},
                            {"state": None, "events": []}]
                spec = {}
                for label, data in (("inputs", rows), ("expected", expected)):
                    raw = "\n".join(json.dumps(row) for row in data).encode()
                    file = root/(tf+"-"+label+".jsonl")
                    file.write_bytes(raw)
                    spec[label] = dict(path=file.name, sha256=hashlib.sha256(raw).hexdigest())
                manifest["timeframes"][tf] = spec
            file = root/"manifest.json"
            file.write_text(json.dumps(manifest))
            result = report(file)
            self.assertTrue(all(v["mismatch_count"] == 0 for v in result["timeframes"].values()))
            self.assertFalse(result["ok"])  # No positive entry coverage: negatives alone cannot approve.
            manifest["reference_origin"] = "python-engine"
            file.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                report(file)

    def test_seed_and_population_bollinger_are_literal(self):
        engine = PineEngine("m1")
        first = engine._market(Bar(AT.isoformat(), 100, 102, 99, 101, 1))
        self.assertEqual((engine.s["fast"], engine.s["slow"], engine.s["atr"]), (101, 101, 3))
        engine._market(Bar((AT+timedelta(minutes=1)).isoformat(), 101, 104, 100, 103, 1))
        self.assertAlmostEqual(engine.s["fast"], 101+4/9)
        self.assertAlmostEqual(engine.s["slow"], 101+4/22)
        self.assertAlmostEqual(engine.s["atr"], 3+1/14)
        flat = PineEngine("m1")
        for i in range(20):
            m = flat._market(Bar((AT+timedelta(minutes=i)).isoformat(), 100, 101, 99, 100, 1))
        self.assertEqual((m["basis"], m["sigma"], m["upper"], m["lower"]), (100, 0, 100, 100))

    def test_rounding_and_locked_config(self):
        cfg = PineConfig()
        self.assertEqual([cfg.ticks(x) for x in (1.005, 1.004, 4000.01)], [101, 100, 400001])
        self.assertEqual(cfg.spread_ticks, 30)
        with self.assertRaises(ValueError):
            PineConfig(spread_price=0)
        with self.assertRaises(ValueError):
            PineConfig(tick_size=float("nan"))
        with self.assertRaises(ValueError):
            require_parity_report(None, cfg)

    def test_six_timeframes_next_open_net_rr_literal(self):
        # (400-30)/(200+30) = 1.608695 -> floor 1608.
        for tf in SECONDS:
            with self.subTest(tf=tf):
                engine = PineExecution(tf)
                engine.arm(candidate(tf), context(tf), AT.isoformat())
                events = engine.on_bar_open(AT.isoformat(), 400000)
                self.assertEqual([e["event_type"] for e in events], ["PRIMARY_OPEN"])
                self.assertEqual(events[0]["rr_milli"], 1608)
                self.assertEqual(engine.on_bar_open(AT.isoformat(), 400000), [])

    def test_retest_spike_and_expiry_all_timeframes(self):
        for tf, seconds in SECONDS.items():
            with self.subTest(tf=tf):
                engine = PineExecution(tf)
                engine.arm(candidate(tf), context(tf), AT.isoformat())
                self.assertEqual(engine.on_bar_open(AT.isoformat(), 400010), [])
                self.assertIsNotNone(engine.state["deferred"])
                self.assertEqual(engine.on_tick((AT+timedelta(seconds=1)).isoformat(), 400005, 400035), [])
                events = engine.on_tick((AT+timedelta(seconds=2)).isoformat(), 399999, 400029)
                self.assertEqual([e["event_type"] for e in events], ["PRIMARY_OPEN"])
                self.assertTrue(events[0]["deferred"])
                events = engine.on_tick((AT+timedelta(seconds=3)).isoformat(), 399790, 399820)
                self.assertEqual([e["event_type"] for e in events], ["SL"])
                self.assertEqual(engine.on_tick((AT+timedelta(seconds=4)).isoformat(), 400100, 400130), [])
                expired = PineExecution(tf)
                expired.arm(candidate(tf), context(tf), AT.isoformat())
                expired.on_bar_open(AT.isoformat(), 400010)
                events = expired.on_tick((AT+timedelta(seconds=seconds)).isoformat(), 400000, 400030)
                self.assertEqual([e["event_type"] for e in events], ["SETUP_EXPIRED"])

    def test_context_exceptions_and_cost_risk(self):
        self.assertFalse(context_authorized("m3", candidate(), context(regime=3)))
        self.assertFalse(context_authorized("m5", candidate(grade=3), context()))
        self.assertFalse(context_authorized("m15", candidate("m15", subtype=301), context("m15")))
        self.assertFalse(context_authorized("m1", candidate(family=3, subtype=302), context()))
        self.assertFalse(context_authorized("m1", candidate(subtype=102), context(strength=79)))
        c = candidate(sl=399900, tp=400300)
        self.assertFalse(PineExecution("m1")._rr(c, 400000)[1])  # spread/risk .30 > .25
        self.assertFalse(PineExecution("m3")._rr(c, 400000)[1])
        self.assertTrue(PineExecution("m5")._rr(c, 400000)[1])

    def test_throttle_campaign_and_restart(self):
        engine = PineExecution("m1")
        engine.arm(candidate(), context(), AT.isoformat())
        engine.on_bar_open(AT.isoformat(), 400000)
        engine.arm(candidate(key="repeat"), context(), (AT+timedelta(minutes=1)).isoformat())
        self.assertIsNone(engine.state["pending"])  # same thesis, no upgrade
        restored = PineExecution("m1", state=json.loads(json.dumps(engine.snapshot())))
        self.assertEqual(restored.snapshot(), engine.snapshot())
        c = candidate(direction=-1, sl=400200, tp=399600)
        self.assertTrue(restored._density_ready(c, 1608))  # direction changes spacing only
        restored.state["accepted"] *= 3
        self.assertFalse(restored._density_ready(c, 1608))
        for e in (engine, PineExecution("m1", state=engine.snapshot())):
            events = e.on_tick((AT+timedelta(seconds=2)).isoformat(), 400401, 400431)
            self.assertEqual(events[0]["event_type"], "TP2")

    def test_out_of_order_and_ohlc_ambiguity(self):
        engine = PineExecution("m1")
        engine.arm(candidate(), context(), AT.isoformat())
        engine.on_bar_open(AT.isoformat(), 400000)
        engine.on_tick((AT+timedelta(seconds=2)).isoformat(), 400001, 400031)
        before = engine.snapshot()
        with self.assertRaises(ValueError):
            engine.on_tick(AT.isoformat(), 399000, 399030)
        self.assertEqual(engine.snapshot(), before)
        events = engine.observe_range((AT+timedelta(minutes=1)).isoformat(), 400500, 399700)
        self.assertEqual(events[0]["event_type"], "SL")
        self.assertTrue(events[0]["ambiguous"])

    def test_cloud_rejected_outcome_consumes_throttle(self):
        engine = PineExecution("m1")
        engine.state["bar_number"] = 1
        self.assertEqual(engine.cloud_event(SimpleNamespace(epoch=1, qualified=False, direction=1), AT.isoformat(), 400000), [])
        engine.state["bar_number"] = 3
        self.assertEqual(engine.cloud_event(SimpleNamespace(epoch=2, qualified=True, direction=1), AT.isoformat(), 400000), [])
        engine.state["bar_number"] = 5
        event = engine.cloud_event(SimpleNamespace(epoch=3, qualified=True, direction=1), AT.isoformat(), 400000)[0]
        self.assertEqual(event["event_type"], "CLOUD_INFO")
        self.assertNotIn("sl_ticks", event)

    def test_health_feed_is_not_worker_liveness(self):
        metrics = {"heartbeats": {k: AT.isoformat() for k in ("signal", "tick")}, "last_tick_age": 900}
        self.assertTrue(bot.worker_health(metrics, now=AT)["alive"])
        self.assertFalse(bot.worker_health(metrics, now=AT+timedelta(seconds=6))["alive"])
        self.assertFalse(bot.worker_health(metrics, telegram_required=True, now=AT)["alive"])
        rows = [(AT.isoformat(), 1, 2, 0, 1), ((AT-timedelta(hours=1)).isoformat(), 1, 2, 0, 1)]
        self.assertEqual(inspect_bars(rows, "m1", AT+timedelta(minutes=1), 1).status, "GAP")

    def test_health_endpoint_does_not_restart_for_stale_feed(self):
        now = datetime.now(timezone.utc).isoformat()
        with patch.dict("os.environ", {"BOT_HEALTH_PORT": "0"}), \
             patch.object(bot, "STATUS", {"ok": False, "ready": False, "dry_run": True}), \
             patch.object(bot, "METRICS", {"heartbeats": {"signal": now, "tick": now}}):
            server = bot.start_health_server()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health") as response:
                    result = json.load(response)
                    self.assertEqual(response.status, 200)
                self.assertTrue(result["alive"])
                self.assertFalse(result["ready"])
                self.assertFalse(result["ok"])
                self.assertTrue(result["degraded"])
            finally:
                server.shutdown()
                server.server_close()


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = TradeStore(self.path/"state.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_outbox_timeout_is_unknown_permanent_dead_and_ack_id(self):
        for key in ("timeout", "permanent", "ok", "limit"):
            self.store.queue_notification(key, "body", "test")
        telegram = SimpleNamespace(send_to=Mock(side_effect=[42, bot.TelegramApiError(429, 30), bot.TelegramApiError(403), TimeoutError()]))
        # deterministic priority order
        with self.store.transaction():
            for key, priority in (("ok", 0), ("limit", 1), ("permanent", 2), ("timeout", 3)):
                self.store.db.execute("UPDATE notifications SET priority=? WHERE event_key=?", (priority, key))
        bot.deliver_pending(self.store, telegram)
        states = {r["event_key"]: r for r in self.store.db.execute("SELECT * FROM notifications")}
        self.assertEqual(states["ok"]["state"], "SENT")
        self.assertEqual(json.loads(states["ok"]["message_ids"]), 42)
        self.assertEqual(states["timeout"]["state"], "UNKNOWN")
        self.assertEqual(states["permanent"]["state"], "DEAD")
        self.assertEqual(states["limit"]["state"], "PENDING")
        self.assertGreater((datetime.fromisoformat(states["limit"]["next_attempt_at"])-datetime.now(timezone.utc)).total_seconds(), 28)
        self.assertEqual(self.store.pending_notifications(), [])

    def test_outbox_restart_dependencies_expiry(self):
        self.store.queue_notification("open", "body", "test")
        self.store.queue_notification("close", "body", "test", depends_on="open")
        self.store.queue_notification("expired", "body", "test", expires_at="2000-01-01T00:00:00+00:00")
        self.assertEqual([r["event_key"] for r in self.store.pending_notifications()], ["open"])
        self.store.claim_notification("open")
        self.store.recover_notifications()
        self.assertEqual(self.store.notification_counts(), {"UNKNOWN": 1, "PENDING": 1, "EXPIRED": 1})
        pending=self.store.pending_notifications()
        self.assertEqual([r['event_key'] for r in pending],['close'])
        self.assertIn('ĐỐI SOÁT',pending[0]['body'])

    def test_db_lock_does_not_advance_checkpoint(self):
        other = sqlite3.connect(self.store.path)
        self.store.db.execute("PRAGMA busy_timeout=1")
        try:
            other.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                self.store.set_checkpoint("tick", "1")
            self.assertIsNone(self.store.checkpoint("tick"))
            other.rollback()
            self.store.set_checkpoint("tick", "1")
            self.assertEqual(self.store.checkpoint("tick"), "1")
        finally:
            other.close()

    def test_cutover_rejects_old_open_trade(self):
        from pine_runtime import check_legacy_flat
        self.store.create(event_key="old", timeframe="m1", signal="MAIN BUY", entry=4000, sl=3998, tp=4004)
        with self.assertRaises(ValueError):
            check_legacy_flat(self.store.path)
        with self.store.transaction():
            self.store.db.execute("UPDATE trades SET status='SL'")
        check_legacy_flat(self.store.path)

    def test_tick_journal_preserves_spikes_and_rejects_backwards(self):
        db = sqlite3.connect(":memory:")
        try:
            install(db)
            for second, bid in ((0, 4000.1), (1, 3999), (2, 4005), (3, 4000.1)):
                at = AT+timedelta(seconds=second)
                append(db, "XAUUSD", bid, bid+.3, at, at)
            self.assertEqual([r[3] for r in read_after(db, "XAUUSD", 0)], [4000.1, 3999, 4005, 4000.1])
            self.assertIsNone(append(db, "XAUUSD", 3990, 3990.3, AT, AT))
            with self.assertRaises(ValueError):
                append(db, "XAUUSD", 4000, 4000.3, AT, AT+timedelta(seconds=6))
            self.assertEqual(len(read_after(db, "XAUUSD", 0)), 4)
        finally:
            db.close()

    def test_history_overlap_and_reconnect(self):
        count, start = history_request((AT-timedelta(minutes=1)).isoformat(), 60, AT, True)
        self.assertEqual(count, 4)
        self.assertEqual(start, int((AT-timedelta(minutes=3)).timestamp()*1000))
        self.assertEqual(history_request(AT.isoformat(), 60, AT, False)[0], 2001)

    def test_empty_evidence_never_passes_acceptance(self):
        from pine_acceptance import report, distribution
        from pine_config import require_acceptance_report
        self.assertEqual(distribution(range(1, 101)), dict(count=100, p95=95, p99=99, max=100))
        result = report(self.store.path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ready_market_hours"], 0)
        file = self.path/"acceptance.json"
        file.write_text(json.dumps(result))
        with self.assertRaises(ValueError):
            require_acceptance_report(file, PineConfig())

    def test_runner_atomic_tick_restart_and_no_network(self):
        market = self.path/"market.db"
        db = sqlite3.connect(market)
        install(db)
        db.execute("CREATE TABLE bars(symbol,timeframe,timestamp,open,high,low,close,volume,received_at_utc)")
        for second, bid in ((0, 4000.1), (1, 4000), (2, 3997.9), (3, 4001)):
            at = AT+timedelta(seconds=second)
            append(db, "XAUUSD", bid, bid+.3, at, at)
        db.commit()
        db.close()
        state_path = self.path/"pine-shadow.db"
        runner = PineRunner(market, state_path)
        try:
            for tf, engine in runner.engines.items():
                engine.s.update(last_close=AT.isoformat(), count=21, fast=4000., slow=3999., atr=2.,
                                regime=2 if tf == "m15" else 7, micro=1, latest_event=1,
                                core_history=[3997., 3998., 3999.])
                engine.execution.arm(candidate(tf), context(tf), AT.isoformat())
                runner._save(tf)
            # Crash after event creation must roll back state, event and checkpoint.
            original = runner._emit
            def fail(tf, events, *args):
                original(tf, events, *args)
                if tf == "m1" and any(e["event_type"] == "PRIMARY_OPEN" for e in events):
                    raise RuntimeError("simulated crash")
            with patch.object(runner, "_emit", side_effect=fail), patch.object(bot.Telegram, "send_to", side_effect=AssertionError("shadow sends")):
                statuses = runner.step(AT+timedelta(seconds=4))
            self.assertFalse(statuses["m1"]["ready"])
            self.assertEqual(runner.store.checkpoint("pine:tick:m1"), "1")
            self.assertIsNotNone(runner.engines["m1"].execution.state["deferred"])
            self.assertTrue(statuses["h1"]["ready"])
        finally:
            runner.close()
        runner = PineRunner(market, state_path)
        try:
            statuses = runner.step(AT+timedelta(seconds=4))
            self.assertTrue(all(s["ready"] for s in statuses.values()))
            counts = dict(runner.store.db.execute("SELECT event_type,COUNT(*) FROM trade_events GROUP BY event_type").fetchall())
            self.assertEqual(counts, {"PRIMARY_OPEN": 6, "SL": 6})
            self.assertEqual(runner.store.notification_counts(), {})
            runner.step(AT+timedelta(seconds=4))
            self.assertEqual(runner.store.db.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0], 12)
        finally:
            runner.close()


if __name__ == "__main__":
    unittest.main()
