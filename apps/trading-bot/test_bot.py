import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from bot import (METRICS, bar_quality, bar_quality_detail, deliver_pending,
                 display_vietnam, owned_lifecycle_events, run_once, split_message)
from trades import TradeStore


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


class BotTest(unittest.TestCase):
    def test_bar_quality_rejects_invalid_ohlc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/market.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE bars (symbol TEXT,timeframe TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL)")
            db.execute("INSERT INTO bars VALUES ('XAUUSD','m1','2026-01-01T00:00:00+00:00',2,1,0,1)")
            db.commit(); db.close()
            self.assertEqual(bar_quality(path, "m1", "2026-01-01T00:00:00+00:00"), "INVALID")

    def test_missing_symbol_digits_fails_closed_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/market.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE bars (symbol TEXT,timeframe TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,symbol_digits INTEGER)")
            db.execute("INSERT INTO bars VALUES ('XAUUSD','m1','2026-01-01T00:00:00+00:00',1,2,0,1,NULL)")
            db.commit(); db.close()
            detail = bar_quality_detail(path, "m1")
            self.assertEqual((detail.status, detail.age_sec, detail.ready), ("INVALID", None, False))

    def test_outbox_delivers_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TradeStore(f"{directory}/trades.db")
            store.queue_notification("event", "hello")
            telegram = FakeTelegram()
            deliver_pending(store, telegram)
            deliver_pending(store, telegram)
            self.assertEqual(telegram.messages, ["hello"])
            store.close()

    def test_split_message_preserves_line_boundaries(self):
        chunks = split_message("alpha\nbeta\ngamma", limit=11)
        self.assertEqual(chunks, ["alpha\nbeta", "gamma"])

    def test_display_vietnam_converts_utc(self):
        self.assertEqual(display_vietnam("2026-01-01T00:00:00+00:00"), "01/01 07:00:00")

    def test_lifecycle_ownership_stops_at_dca_open(self):
        lifecycle = [
            {"event_key": "primary", "event_type": "PRIMARY_OPEN"},
            {"event_key": "tp1", "event_type": "TP1"},
            {"event_key": "dca", "event_type": "DCA_OPEN"},
            {"event_key": "after", "event_type": "STOP_PROTECTED"},
        ]
        primary = SimpleNamespace(event_key="primary", event_kind="PRIMARY_OPEN")
        dca = SimpleNamespace(event_key="dca", event_kind="DCA_OPEN")
        self.assertEqual([item["event_type"] for item in owned_lifecycle_events(lifecycle, primary)], ["TP1"])
        self.assertEqual([item["event_type"] for item in owned_lifecycle_events(lifecycle, dca)], ["STOP_PROTECTED"])

    def test_restart_rebuilds_tick_health_from_checkpointed_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            market = f"{directory}/market.db"
            db = sqlite3.connect(market)
            db.execute("CREATE TABLE latest_ticks(symbol TEXT,timestamp TEXT,bid REAL,ask REAL,received_at TEXT,provider_timestamp TEXT)")
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO latest_ticks VALUES('XAUUSD',?,100,101,?,?)", (now, now, now))
            db.commit(); db.close()
            store = TradeStore(f"{directory}/trades.db")
            store.set_checkpoint("tick", now)
            METRICS["last_tick_age"] = None
            run_once(store, market, None)
            self.assertIsNotNone(METRICS["last_tick_age"])
            store.close()


if __name__ == "__main__":
    unittest.main()
