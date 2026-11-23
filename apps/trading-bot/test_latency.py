import unittest
from datetime import datetime, timezone

from latency import merge_quote, slippage_observation


class LatencyTest(unittest.TestCase):
    def test_partial_quote_preserves_other_side_and_rejects_old_tick(self):
        state = {"bid": 100.0, "ask": 100.3, "provider_timestamp": "2026-01-01T00:00:02+00:00"}
        state = merge_quote(state, bid=100.2, ask=None, provider_timestamp="2026-01-01T00:00:03Z")
        self.assertEqual((state["bid"], state["ask"]), (100.2, 100.3))
        state = merge_quote(state, bid=99.0, ask=99.1, provider_timestamp="2026-01-01T00:00:02Z")
        self.assertEqual((state["bid"], state["ask"]), (100.2, 100.3))

    def test_slippage_warning_uses_direction_and_threshold(self):
        buy = slippage_observation("BUY", 100.0, 100.8, 100.9, 0.1, 2.0,
                                   datetime.now(timezone.utc))
        sell = slippage_observation("SELL", 100.0, 99.0, 99.1, 0.1, 2.0,
                                    datetime.now(timezone.utc))
        self.assertTrue(buy["warning"])
        self.assertTrue(sell["warning"])
        self.assertEqual(buy["delta"], 0.9)
        self.assertEqual(sell["delta"], -1.0)


if __name__ == "__main__":
    unittest.main()
