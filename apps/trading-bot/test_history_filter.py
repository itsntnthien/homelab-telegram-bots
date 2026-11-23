import json
import os
from datetime import datetime,timezone
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import bot
from trades import TradeStore
from telegram_views import history,history_action,history_calendar,history_day,history_filters


class HistoryFilterTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=TradeStore(Path(self.temp.name)/'trades.db');self.addCleanup(self.store.close)

    def trade(self,key,at,chat='100'):
        trade,_=self.store.create(event_key=key,timeframe='M30',signal='MAIN BUY',entry=100,sl=95,tp=110,
            at=datetime.fromisoformat(at),metadata=dict(strategy=key,engine_version='xau-intraday-test'))
        self.store.create_user_signal(chat,trade)
        return trade

    def test_vietnam_midnight_inclusive_end_exclusive_and_entry_date(self):
        self.trade('BEFORE','2026-11-22T16:59:59+00:00')
        start=self.trade('AT_START','2026-11-22T17:00:00+00:00')
        self.trade('LAST_SECOND','2026-11-23T16:59:59.999+00:00')
        self.trade('NEXT_DAY','2026-11-23T17:00:00+00:00')
        self.trade('OTHER_USER','2026-11-23T10:00:00+00:00','200')
        with self.store.transaction():
            self.store.db.execute("UPDATE trades SET status='TP',closed_at='2026-11-24T10:00:00+00:00' WHERE id=?",(start.id,))
        text,more=history(self.store.db,'100',1,day='2026-11-23')
        self.assertIn('AT_START',text);self.assertIn('LAST_SECOND',text)
        for absent in ('BEFORE','NEXT_DAY','OTHER_USER'):self.assertNotIn(absent,text)
        self.assertIn('Ngày phát lệnh: 23/11/2026',text);self.assertFalse(more)
        self.assertEqual(history_day('2026-11-23')[1:],('2026-11-22T17:00:00+00:00','2026-11-23T17:00:00+00:00'))

    def test_filtered_pagination_and_all_history_remain_distinct(self):
        for i in range(11):self.trade('SELECTED_'+str(i),'2026-11-23T10:00:00+00:00')
        self.trade('ANOTHER_DAY','2026-11-20T10:00:00+00:00')
        first,more=history(self.store.db,'100',1,day='2026-11-23')
        second,after=history(self.store.db,'100',2,day='2026-11-23')
        self.assertTrue(more);self.assertFalse(after)
        self.assertEqual(first.count('SELECTED_'),10);self.assertEqual(second.count('SELECTED_'),1)
        self.assertNotIn('ANOTHER_DAY',first+second)
        self.assertIn('ANOTHER_DAY',history(self.store.db,'100',1)[0])
        tg=bot.Telegram('synthetic','100',chat_ids=['100'])
        with patch.dict(os.environ,{'SIGNAL_ENGINE':'intraday-live'}),patch.object(tg,'call') as call:
            tg.history_page(self.store.db,'100',1,42,day='2026-11-23')
        payload=call.call_args.args[1]
        buttons=[b['callback_data'] for row in json.loads(payload['reply_markup'])['inline_keyboard'] for b in row]
        self.assertIn('histdate:2026-11-23:2',buttons);self.assertIn('hist:1',buttons)
        self.assertLess(len(payload['text']),4096)

    def test_calendar_leap_day_month_navigation_and_callback_sizes(self):
        text,markup=history_calendar('2024-02')
        buttons=[b for row in markup['inline_keyboard'] for b in row]
        callbacks={b['callback_data'] for b in buttons}
        self.assertIn('histdate:2024-02-29:1',callbacks)
        self.assertNotIn('histdate:2024-02-30:1',callbacks)
        self.assertIn('histcal:2024-01',callbacks);self.assertIn('histcal:2024-03',callbacks)
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for b in buttons))
        self.assertEqual([b['text'] for b in markup['inline_keyboard'][0]],['T2','T3','T4','T5','T6','T7','CN'])
        december=history_calendar('2026-12')[1]
        self.assertTrue(any(b['callback_data']=='histcal:2027-01' for row in december['inline_keyboard'] for b in row))
        self.assertEqual(history_action('histnoop'),('noop',None,None))

    def test_invalid_dates_and_pages_are_rejected(self):
        for value in ('histdate:2026-02-29:1','histdate:2026-11-31:1','histdate:2026-11-23:0',
                      'histdate:2026-11-23:-1','histdate:2026-11-23:100001','histdate:2026-11-23:１２',
                      "histdate:2026-11-23:1 OR 1=1",'histcal:2026-13','histcal:2026-1','histcal:0001-01'):
            with self.subTest(value=value),self.assertRaises(ValueError):history_action(value)

    def test_today_buttons_follow_vietnam_date_and_empty_day_is_clear(self):
        rows=history_filters(now=datetime(2026,11,22,18,tzinfo=timezone.utc))
        self.assertEqual(rows[0][0]['callback_data'],'histdate:2026-11-23:1')
        self.assertEqual(rows[0][1]['callback_data'],'histdate:2026-11-22:1')
        text,more=history(self.store.db,'100',1,day='2026-11-23')
        self.assertIn('Chưa có lệnh phù hợp.',text);self.assertFalse(more)

    def test_real_poll_calendar_and_date_preserve_private_scope_and_cursor(self):
        self.trade('OWN_SIGNAL','2026-11-23T10:00:00+00:00')
        self.trade('PRIVATE_OTHER','2026-11-23T10:00:00+00:00','200')
        stop=Event();calls=[]
        updates=[dict(update_id=1,message=dict(chat=dict(id=100,type='private'),**{'from':dict(id=100)},text='📋 Lịch sử'))]
        for i,(chat,sender,data) in enumerate([(100,100,'histcal:2026-11'),(100,100,'histdate:2026-11-23:1'),
                (100,200,'histdate:2026-11-23:1'),(999,999,'histcal:2026-11'),(100,100,'histdate:2026-02-30:1'),
                (100,100,'histnoop'),(100,100,'hist:1')],2):
            updates.append(dict(update_id=i,callback_query=dict(id=str(i),data=data,**{'from':dict(id=sender)},
                message=dict(chat=dict(id=chat,type='private'),message_id=42))))
        def call(method,payload,timeout=None):
            calls.append((method,payload))
            if method=='getUpdates':stop.set();return {'result':updates}
            return {'result':{'message_id':42}}
        with patch.dict(os.environ,{'SIGNAL_ENGINE':'intraday-live','TELEGRAM_TEST_CHAT_ID':'','TELEGRAM_TEST_STATE_DB':''}):
            tg=bot.Telegram('synthetic','100',chat_ids=['100','200'])
            with patch.object(tg,'call',side_effect=call):tg.poll(self.store,stop)
        edits=[p for m,p in calls if m=='editMessageText']
        self.assertEqual(len(edits),3)
        self.assertIn('CHỌN NGÀY',edits[0]['text']);self.assertIn('Ngày phát lệnh: 23/11/2026',edits[1]['text'])
        self.assertIn('OWN_SIGNAL',edits[1]['text']);self.assertNotIn('PRIVATE_OTHER',str(edits))
        self.assertEqual(self.store.checkpoint('telegram_update_id'),'9')
        self.assertFalse(any(str(p.get('chat_id'))=='999' for _,p in calls))
