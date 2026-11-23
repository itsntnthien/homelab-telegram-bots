import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from market_quality import inspect_bars, utc_timestamp, market_is_open, interval_is_closed


class MarketQualityTest(unittest.TestCase):
    def test_broker_rollover_weekend_and_real_gap(self):
        schedule = {"scheduleTimeZone": "America/New_York", "schedule": [
            {"startSecond": 64920 + day*86400, "endSecond": 147540 + day*86400 - (120 if day == 4 else 0)}
            for day in range(5)]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"broker.json"
            path.write_text(json.dumps(schedule))
            with patch.dict("os.environ", {"MARKET_SCHEDULE_FILE": str(path)}):
                self.assertTrue(interval_is_closed("2026-11-11T22:00:00Z", "2026-11-11T23:02:00Z"))
                self.assertTrue(interval_is_closed("2026-11-06T21:57:00Z", "2026-11-08T23:00:00Z"))
                self.assertFalse(market_is_open("2026-11-08T23:01:59Z"))
                self.assertTrue(market_is_open("2026-11-08T23:02:00Z"))
                self.assertFalse(interval_is_closed("2026-09-07T20:00:00Z", "2026-09-07T22:00:00Z"))
                self.assertFalse(interval_is_closed("2026-11-12T05:00:00Z", "2026-11-12T05:01:00Z"))

    def test_timestamp_variants_are_equal(self):
        self.assertEqual(
            utc_timestamp("2026-10-09T19:00:00.0000000Z"),
            utc_timestamp("2026-10-09T19:00:00+00:00"),
        )

    def test_rejects_forming_invalid_and_gap_bars(self):
        now = datetime(2026, 10, 9, 19, 5, tzinfo=timezone.utc)
        rows = [("2026-10-09T19:04:00Z", 1, 2, 0, 1.5),
                ("2026-10-09T19:02:00Z", 1, 2, 0, 1.5)]
        self.assertEqual(inspect_bars(rows, "m1", now, min_closed_bars=1).status, "GAP")
        forming = [("2026-10-09T19:05:00Z", 1, 2, 0, 1.5)]
        self.assertEqual(inspect_bars(forming, "m1", now, min_closed_bars=1).status, "INVALID")

    def test_ready_requires_warmup(self):
        now = datetime(2026, 10, 9, 19, 5, tzinfo=timezone.utc)
        rows = [(f"2026-10-09T19:{minute:02d}:00Z", 1, 2, 0, 1.5)
                for minute in range(4, -1, -1)]
        result = inspect_bars(rows, "m1", now, min_closed_bars=5)
        self.assertEqual(result.status, "OK")
        self.assertTrue(result.ready)

    def test_age_is_measured_from_bar_close(self):
        now = datetime(2026, 10, 9, 19, 7, tzinfo=timezone.utc)
        result = inspect_bars(
            [("2026-10-09T19:00:00Z", 1, 2, 0, 1.5)],
            "m5", now, min_closed_bars=1, max_age_sec=180,
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.age_sec, 120)

    def test_session_break_is_not_an_in_session_gap(self):
        now = datetime(2026, 10, 8, 22, 5, tzinfo=timezone.utc)
        rows = [("2026-10-08T22:00:00Z", 1, 2, 0, 1.5),
                ("2026-10-08T20:59:00Z", 1, 2, 0, 1.5)]
        self.assertEqual(inspect_bars(rows, "m1", now, min_closed_bars=1).status, "OK")

    def test_m30_and_h1_forming_boundary_and_gap(self):
        now = datetime(2026, 1, 1, 12, 45, tzinfo=timezone.utc)
        m30 = [("2026-01-01T12:30:00+00:00", 1, 2, 0, 1)]
        self.assertEqual(inspect_bars(m30, "m30", now, min_closed_bars=1).status, "INVALID")
        h1 = [("2026-01-01T11:00:00+00:00", 1, 2, 0, 1),
              ("2026-01-01T09:00:00+00:00", 1, 2, 0, 1)]
        self.assertEqual(inspect_bars(h1, "h1", now, min_closed_bars=1).status, "GAP")
        rollover = [("2026-01-01T23:00:00+00:00", 1, 2, 0, 1),
                    ("2026-01-01T21:00:00+00:00", 1, 2, 0, 1)]
        self.assertEqual(inspect_bars(rollover, "h1",
                                     datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc),
                                     min_closed_bars=1).status, "OK")


if __name__ == "__main__":
    unittest.main()
