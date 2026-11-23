import os
import unittest
from pathlib import Path
from unittest.mock import patch
from market_quality import market_is_open, inspect_bars, utc_timestamp


class HolidayTest(unittest.TestCase):
    def test_actual_broker_published_november_holidays(self):
        path=Path(__file__).parent/'reference'/'ctrader-XAUUSD-schedule-20261112.json'
        with patch.dict(os.environ,{'MARKET_SCHEDULE_FILE':str(path)}):
            self.assertTrue(market_is_open('2026-11-26T19:29:59Z'))
            self.assertFalse(market_is_open('2026-11-26T19:30:00Z'))
            self.assertFalse(market_is_open('2026-11-26T21:59:59Z'))
            self.assertFalse(market_is_open('2026-11-27T19:45:00Z'))
            # No invented September holiday: unknown history remains a real blocker.
            self.assertTrue(market_is_open('2026-09-07T19:00:00Z'))

    def test_numeric_string_ohlc_not_lexicographic(self):
        row=('2026-11-17T12:00:00Z','9','10','8','9')
        q=inspect_bars([row],'m1',utc_timestamp('2026-11-17T12:01:00Z'),1)
        self.assertTrue(q.ready)
