import json
import unittest
from datetime import datetime, timedelta, timezone

from release1_engine import replay_release1
from signal_engine import Bar


class Release1EngineTest(unittest.TestCase):
    def test_replay_is_deterministic_and_cloud_is_info_only(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = []
        price = 100.0
        for index in range(260):
            close = price + (0.2 if index % 7 else -0.1)
            bars.append(Bar((start + timedelta(minutes=index)).isoformat(), price,
                            max(price, close) + 0.2, min(price, close) - 0.2,
                            close, 100 + index))
            price = close
        first = replay_release1(bars, "m1")
        second = replay_release1(bars, "m1")
        self.assertEqual(json.dumps(first, default=lambda value: value.__dict__, sort_keys=True),
                         json.dumps(second, default=lambda value: value.__dict__, sort_keys=True))
        self.assertEqual(set(first[2]["context"]), {"dow", "wyckoff", "acceptance_rejection"})
        for event in first[0]:
            if event.event_kind == "CLOUD_INFO":
                self.assertEqual((event.entry_ticks, event.sl_ticks, event.tp_ticks),
                                 (None, None, None))
                self.assertEqual(event.engine_version, "xau-scalp-r1")
