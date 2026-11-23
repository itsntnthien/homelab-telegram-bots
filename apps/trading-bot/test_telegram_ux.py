"""Command-handler integration with an isolated fake Telegram transport, not UI proof."""
import os
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch
import bot
from trades import TradeStore,MODEL_NAMES,entry_message


class TelegramUXTest(unittest.TestCase):
    def test_each_model_survives_restart_and_fake_api_ack_without_duplicate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'SYMBOL':'XAUUSD'}):
            path=Path(directory)/'tv-test.db'
            store=TradeStore(path)
            for subtype in MODEL_NAMES:
                trade,_=store.create(event_key=str(subtype),timeframe='M30',signal='MAIN BUY',
                                     entry=100,sl=99,tp=102,metadata={'family':subtype//100,'subtype':subtype})
                store.queue_notification('model:'+str(subtype),entry_message(trade),'-42')
            store.close()
            store=TradeStore(path);self.addCleanup(store.close)
            tg=bot.Telegram('fake-test-token','-42',chat_ids=['-42']);sent=[]
            def call(method,payload,timeout=None):
                self.assertEqual(method,'sendMessage');self.assertEqual(payload['chat_id'],'-42')
                sent.append(payload['text']);return {'result':{'message_id':len(sent)}}
            with patch.object(tg,'call',side_effect=call):
                bot.deliver_pending(store,tg,limit=10)
                bot.deliver_pending(store,tg,limit=10)
            self.assertEqual(len(sent),6)
            for name in MODEL_NAMES.values():self.assertEqual(sum(name in text for text in sent),1)
            self.assertEqual(store.notification_counts(),{'SENT':6})
            self.assertEqual(sorted(json.loads(r[0]) for r in store.db.execute('SELECT message_ids FROM notifications')),list(range(1,7)))
            store.close()

    def test_commands_and_settings_route_test_chat_without_second_poller(self):
        with tempfile.TemporaryDirectory() as directory:
            prod=TradeStore(Path(directory)/'production.db')
            test=Path(directory)/'tv-test.db'
            stop=Event();calls=[];sent=[]
            commands=['/start','/status','/open','/history','/stats','/news','/settings','/help']
            updates=[{'update_id':i+1,'message':{'chat':{'id':-42},'text':c}} for i,c in enumerate(commands)]
            updates.append({'update_id':9,'callback_query':{'id':'callback','data':'tf:m1','message':{'chat':{'id':-42},'message_id':1}}})
            def call(method,payload,timeout=None):
                calls.append((method,payload))
                if method=='getUpdates':
                    stop.set()
                    return {'result':updates}
                sent.append(payload)
                return {'result':{'message_id':10}}
            with patch.dict(os.environ,{'TELEGRAM_CHAT_IDS':'100','TELEGRAM_TEST_CHAT_ID':'-42','TELEGRAM_TEST_STATE_DB':str(test)}):
                tg=bot.Telegram('local-test-only','100')
                with patch.object(tg,'call',side_effect=call), patch.object(bot,'latest_quote',return_value=None):
                    tg.poll(prod,stop)
            self.assertEqual(sum(m=='getUpdates' for m,p in calls),1)
            self.assertTrue(all(str(p['chat_id'])=='-42' for p in sent if 'chat_id' in p))
            self.assertEqual(prod.checkpoint('telegram_update_id'),'10')
            prod.close()
            saved=TradeStore(test)
            self.assertFalse(saved.user_enabled('-42',bot.SYMBOL,'m1'))
            self.assertIsNone(saved.checkpoint('telegram_update_id'))
            saved.close()

    def test_unauthorized_and_malformed_callback_do_not_poison_update_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            prod=TradeStore(Path(directory)/'production.db');self.addCleanup(prod.close)
            stop=Event();calls=[]
            updates=[{'update_id':10,'message':{'chat':{'id':999},'text':'/start'}},
                     {'update_id':11,'callback_query':{'id':'bad','data':'hist:NaN','message':{'chat':{'id':100},'message_id':1}}},
                     {'update_id':12,'message':{'chat':{'id':999},'text':'/start'}}]
            def call(method,payload,timeout=None):
                calls.append((method,payload))
                if method=='getUpdates':
                    stop.set();return {'result':updates}
                return {'result':True}
            with patch.dict(os.environ,{'TELEGRAM_CHAT_IDS':'100','TELEGRAM_TEST_CHAT_ID':'','TELEGRAM_TEST_STATE_DB':''}):
                tg=bot.Telegram('local-test-only','100')
                with patch.object(tg,'call',side_effect=call):tg.poll(prod,stop)
            self.assertEqual(prod.checkpoint('telegram_update_id'),'13')
            self.assertFalse(any(method=='sendMessage' for method,_ in calls))
            prod.close()

    def test_status_does_not_claim_ready_when_h1_is_blocked(self):
        with patch.object(bot,'STATUS',{'ready':False,'timeframes':{'h1':{'ready':False,'error':'GAP'}}}):
            text=bot.Telegram.status_text()
            self.assertIn('GAP',text)
            self.assertIn('Chưa sẵn sàng',text)
            self.assertNotIn('🟢 Bot đang hoạt động',text)
