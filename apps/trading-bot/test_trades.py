import tempfile
import unittest
from datetime import datetime, timezone

from trades import TradeStore, entry_message, info_message, session_key, update_message


class TradeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TradeStore(f"{self.tmp.name}/trades.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_numbering_reset_dedup_and_hits(self):
        before_reset = datetime(2026, 9, 27, 20, 59, tzinfo=timezone.utc)
        after_reset = datetime(2026, 9, 27, 21, 1, tzinfo=timezone.utc)
        self.assertNotEqual(session_key(before_reset), session_key(after_reset))

        first, created = self.store.create(
            event_key="scalp:m5:100:main-buy",
            timeframe="M5",
            signal="MAIN BUY",
            entry=4280,
            sl=4275,
            tp=4290,
            at=before_reset,
        )
        duplicate, duplicate_created = self.store.create(
            event_key="scalp:m5:100:main-buy",
            timeframe="M5",
            signal="MAIN BUY",
            entry=4280,
            sl=4275,
            tp=4290,
            at=before_reset,
        )
        second, _ = self.store.create(
            event_key="vsa:m1:101:cloud-sell",
            timeframe="M1",
            signal="CLOUD SELL",
            entry=4280,
            sl=4285,
            tp=4270,
            at=after_reset,
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.id, duplicate.id)
        self.assertEqual((first.number, second.number), (1, 1))
        self.assertIn("#1 XAUUSD", entry_message(first))
        closed = self.store.update_price(4290)
        self.assertEqual([(trade.id, trade.status) for trade in closed], [(first.id, "TP"), (second.id, "SL")])
        self.assertIn("TP HIT", update_message(closed[0]))

    def test_persistent_notification_and_checkpoint(self):
        self.assertTrue(self.store.queue_notification("open:1", "message"))
        self.assertFalse(self.store.queue_notification("open:1", "duplicate"))
        self.assertEqual(self.store.pending_notifications()[0]["body"], "message")
        self.store.notification_sent("open:1")
        self.assertEqual(self.store.pending_notifications(), [])
        self.store.set_checkpoint("m1", "2026-09-28T00:00:00Z")
        self.assertEqual(self.store.checkpoint("m1"), "2026-09-28T00:00:00Z")

    def test_failed_notification_dead_letters_after_three_attempts(self):
        self.store.queue_notification("failing", "message")
        for _ in range(3):
            self.store.notification_failed("failing", "network")
            self.store.db.execute("UPDATE notifications SET next_attempt_at=NULL WHERE event_key='failing'")
        row = self.store.db.execute("SELECT state,attempts,last_error,failed_at FROM notifications WHERE event_key='failing'").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("DEAD", 3, "network"))

    def test_cloud_info_shares_session_counter(self):
        at = datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
        main, _ = self.store.create(event_key="main", timeframe="M1", signal="MAIN BUY",
                                    entry=100, sl=99, tp=102, at=at)
        cloud, created = self.store.create_info(event_key="cloud", timeframe="M5",
                                                signal="CLOUD SELL", reference=101, at=at)
        self.assertTrue(created)
        self.assertEqual((main.number, cloud.number), (1, 2))
        self.assertIn("không có SL/TP", info_message(cloud))

    def test_trade_observation_metadata_is_persisted(self):
        trade, _ = self.store.create(event_key="metadata", timeframe="M1", signal="MAIN BUY",
                                     entry=100, sl=99, tp=102, metadata={"planned_entry": 100.0,
                                     "price_delta": 0.5, "slippage_warning": 1})
        row = self.store.db.execute("SELECT planned_entry,price_delta,slippage_warning FROM trades WHERE id=?",
                                    (trade.id,)).fetchone()
        self.assertEqual(tuple(row), (100.0, 0.5, 1))

    def test_early_warning_is_idempotent_and_not_a_trade(self):
        key = "early_warning:XAUUSD:m1:2026-01-01T00:00:00+00:00:BUY"
        self.assertTrue(self.store.mark_early_warning(key, "XAUUSD", "m1", "BUY", "2026-01-01T00:00:00+00:00"))
        self.assertFalse(self.store.mark_early_warning(key, "XAUUSD", "m1", "BUY", "2026-01-01T00:00:00+00:00"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)

    def test_release1_schema_state_and_lifecycle_are_idempotent(self):
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(trades)")}
        self.assertTrue({"engine_version", "config_hash", "campaign_key", "tp1", "tp2"} <= columns)
        state = {"setup": {"state": "WAIT_RETEST"}}
        self.store.save_engine_state("XAUUSD", "M30", "xau-scalp-r1", "bar-1", state)
        self.store.save_engine_state("XAUUSD", "M30", "xau-scalp-r1", "bar-2", {"setup": None})
        restored = self.store.load_engine_state("XAUUSD", "m30", "xau-scalp-r1")
        self.assertEqual(restored, {"last_bar_timestamp": "bar-2", "state": {"setup": None}})
        self.assertTrue(self.store.record_trade_event(
            event_key="campaign:1:tp1", event_type="TP1", event_time="bar-2",
            campaign_key="campaign:1", price=101.0))
        self.assertFalse(self.store.record_trade_event(
            event_key="campaign:1:tp1", event_type="TP1", event_time="bar-2",
            campaign_key="campaign:1", price=101.0))

    def test_release1_metadata_and_new_timeframes(self):
        trade, _ = self.store.create(
            event_key="r1", timeframe="H1", signal="MAIN BUY", entry=100, sl=99, tp=102,
            metadata={"engine_version": "xau-scalp-r1", "config_hash": "abc",
                      "campaign_key": "campaign:r1", "family": 1, "subtype": 101,
                      "grade": 2, "quality": 80, "tp1": 101, "tp2": 102})
        self.assertEqual((trade.engine_version, trade.campaign_key, trade.tp1, trade.tp2),
                         ("xau-scalp-r1", "campaign:r1", 101, 102))
        info, created = self.store.create_info(
            event_key="cloud:m30", timeframe="M30", signal="CLOUD SELL", reference=100)
        self.assertTrue(created)
        self.assertEqual(info.timeframe, "M30")

    def test_release1_tick_lifecycle_tp1_protection_and_be(self):
        trade, _ = self.store.create(
            event_key="runner", timeframe="M5", signal="MAIN BUY", entry=100, sl=99, tp=102,
            metadata={"engine_version": "xau-scalp-r1", "campaign_key": "campaign:runner",
                      "initial_sl": 99, "tp1": 101, "tp2": 102})
        self.assertEqual(self.store.update_quote(101, 101), [])
        protected = self.store.db.execute(
            "SELECT sl,remaining_share_milli,tp1_hit_at FROM trades WHERE id=?", (trade.id,)).fetchone()
        self.assertEqual((protected[0], protected[1]), (100, 500))
        closed = self.store.update_quote(100, 100)
        self.assertEqual(closed[0].status, "BE")
        events = [row[0] for row in self.store.db.execute(
            "SELECT event_type FROM trade_events WHERE trade_id=? ORDER BY created_at,event_type", (trade.id,))]
        self.assertEqual(set(events), {"TP1", "STOP_PROTECTED", "BE"})

    def test_user_timeframe_storage_is_canonical_lowercase(self):
        self.store.ensure_users(["1"], "XAUUSD")
        self.store.set_user_timeframe("1", "XAUUSD", "M30", False)
        rows = self.store.db.execute(
            "SELECT timeframe,enabled FROM user_settings WHERE chat_id='1' AND symbol='XAUUSD' AND lower(timeframe)='m30'").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("m30", 0)])

    def test_same_bar_lifecycle_does_not_leave_open_trade(self):
        trade, _ = self.store.create(
            event_key="same-bar", timeframe="M1", signal="MAIN BUY", entry=100, sl=99, tp=102,
            metadata={"engine_version": "xau-scalp-r1", "campaign_key": "campaign:same"})
        event = {"event_key": "campaign:same:sl:bar", "event_type": "SL",
                 "event_time": "2026-01-01T00:00:00+00:00", "campaign_key": "campaign:same",
                 "ambiguous": True}
        self.store.record_trade_event(event_key=event["event_key"], event_type="SL",
                                      event_time=event["event_time"], campaign_key=event["campaign_key"])
        self.store.apply_trade_event(trade.id, event)
        row = self.store.db.execute("SELECT status,ambiguous_exit FROM trades WHERE id=?", (trade.id,)).fetchone()
        self.assertEqual(tuple(row), ("SL", 1))


if __name__ == "__main__":
    unittest.main()
