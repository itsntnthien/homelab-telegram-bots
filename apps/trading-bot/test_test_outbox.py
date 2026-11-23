import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import bot
from trades import TradeStore


class TestOutboxTest(unittest.TestCase):
    def test_single_owner_only_dispatches_test_destination(self):
        with tempfile.TemporaryDirectory() as d:
            prod=Path(d)/'production.db';test=Path(d)/'tv-test.db'
            store=TradeStore(prod);store.close()
            store=TradeStore(test)
            store.queue_notification('good','TEST message','-42')
            store.queue_notification('foreign','DO NOT SEND','100')
            store.queue_notification('broadcast','DO NOT SEND')
            store.close()
            stop=SimpleNamespace(is_set=Mock(side_effect=[False,True]),wait=Mock())
            tg=SimpleNamespace(chat_ids=['100'],send_to=Mock(return_value=42))
            with patch.dict(os.environ,{'TELEGRAM_TEST_STATE_DB':str(test),'TELEGRAM_TEST_CHAT_ID':'-42'}):
                bot.notification_worker(prod,tg,stop)
            tg.send_to.assert_called_once_with('-42','TEST message')
            store=TradeStore(test)
            try:self.assertEqual(store.notification_counts(),{'DEAD':2,'SENT':1})
            finally:store.close()

    def test_test_destination_cannot_be_a_production_recipient(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ,{'TELEGRAM_TEST_STATE_DB':str(Path(d)/'tv-test.db'),'TELEGRAM_TEST_CHAT_ID':'100'}):
                with self.assertRaises(ValueError):
                    bot.notification_worker(Path(d)/'production.db',SimpleNamespace(chat_ids=['100']),None)
