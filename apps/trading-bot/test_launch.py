import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from launch import configure


class LaunchTest(unittest.TestCase):
    def test_missing_feed_fails_before_starting_workers(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MARKET_DB': str(Path(folder)/'absent.db')}, clear=True):
            with self.assertRaisesRegex(SystemExit, 'collector first'):
                configure()

    def test_defaults_are_shadow_and_explicit_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('market.db', 'schedule.json'):
                (root/name).touch()
            env = {'MARKET_DB': str(root/'market.db'), 'MARKET_SCHEDULE_FILE': str(root/'schedule.json'),
                   'INTRADAY_SHADOW_DB': str(root/'state/shadow.db'), 'TRADES_DB': str(root/'state/live.db'),
                   'NEWS_DB': str(root/'state/news.db'), 'BOT_HEALTH_PORT': '19001'}
            with patch.dict(os.environ, env, clear=True):
                configure()
                self.assertEqual(os.environ['SIGNAL_ENGINE'], 'intraday-shadow')
                self.assertEqual(os.environ['DRY_RUN'], 'true')
                self.assertEqual(os.environ['BOT_HEALTH_PORT'], '19001')
                self.assertTrue((root/'state').is_dir())
