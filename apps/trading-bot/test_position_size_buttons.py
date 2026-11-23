"""Account-unit regressions and real polling with synthetic button messages."""
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import bot
import position_size as sizing
from test_position_size import SPEC, prefs
from trades import TradeStore


def interact(test, text, chat='100', sender=None, kind='private'):
    sent=[]
    tg=SimpleNamespace(send_to=lambda *args,**kwargs:sent.append(args))
    handled=sizing.handle(tg,test.store,{'text':text,'from':{'id':sender or chat}},dict(id=chat,type=kind))
    return handled,sent


class AccountButtons(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'trades.db'
        self.store=TradeStore(self.path);self.addCleanup(self.store.close)

    def test_cent_and_usd_have_equal_budget_but_different_lot_units(self):
        for chat,account,capital in [('100',sizing.USD,'1000'),('200',sizing.CENT,'100000')]:
            for text in (sizing.BUTTON,account,sizing.CAPITAL,capital,sizing.PERCENT,'1'):
                self.assertTrue(interact(self,text,chat)[0])
        a=sizing.profile(self.store,'100');b=sizing.profile(self.store,'200')
        self.assertEqual(a['capital_usd'],b['capital_usd'])
        standard=sizing.calculate(a,SPEC,100,95,1)
        cent=sizing.calculate(b,SPEC,100,95,1)
        self.assertEqual((standard['lot'],cent['lot']),('0.02','2'))
        self.assertEqual(Decimal(standard['estimated_risk_usd']),Decimal(cent['estimated_risk_usd']))
        self.assertEqual(standard['standard_lot'],cent['standard_lot'])
        config=dict(sl=95,direction=1,slippage=0,commission=0,sizing_spec=SPEC,sizing_profile=b)
        text,result=sizing.annotation(config,dict(price=100),'200')
        self.assertIn('2 cent lot (XAUUSDc)',text)
        self.assertIn('100000 USC',text)
        self.assertIn('chưa có giá/spread XAUUSDc',text)

    def test_fixed_cent_risk_and_switching_preserve_real_money(self):
        for text in (sizing.CENT,sizing.CAPITAL,'100000',sizing.MONEY,'1000'):
            interact(self,text)
        before=sizing.profile(self.store,'100')
        self.assertEqual((before['capital_usd'],before['risk_value']),('1000','10'))
        interact(self,sizing.USD)
        after=sizing.profile(self.store,'100')
        self.assertEqual((after['capital_usd'],after['risk_value']),('1000','10'))
        self.assertIn('10 USD/lệnh',sizing.summary(self.store,'100'))
        interact(self,sizing.CENT)
        self.assertIn('1000 USC (Cent)/lệnh',sizing.summary(self.store,'100'))
        interact(self,sizing.MONEY);interact(self,'0,0001')
        self.assertEqual(sizing.profile(self.store,'100')['risk_value'],'0.000001')

    def test_cent_rounding_minimum_cap_and_sell_geometry(self):
        p=dict(prefs('100'),account_type='cent',cent_spec=dict(sizing.CENT_SPEC))
        a=sizing.calculate(p,SPEC,100,95,1,slippage='.02')
        b=sizing.calculate(p,SPEC,100,105,-1,slippage='.02')
        self.assertEqual(a['lot'],b['lot']);self.assertEqual(a['lot'],'0.19')
        self.assertEqual(sizing.calculate(dict(p,capital_usd='0.01'),SPEC,100,95,1)['state'],'BELOW_MINIMUM')
        self.assertEqual(sizing.calculate(dict(p,capital_usd='1000000000'),SPEC,100,95,1)['lot'],'200')
        with self.assertRaises(ValueError):sizing.calculate(dict(p,cent_spec=None),SPEC,100,95,1)
        with self.assertRaises(ValueError):sizing.calculate(p,dict(SPEC,contract_size='1'),100,95,1)

    def test_display_separates_budget_from_rounded_loss_without_changing_math(self):
        from telegram_views import rich_text
        config=dict(sizing_profile=prefs(),sizing_spec=SPEC,sl=3595,direction=1,slippage='.02',commission=0)
        text,result=sizing.annotation(config,dict(price=3600),'100')
        self.assertEqual(Decimal(result['estimated_risk_usd']),Decimal('5.04'))
        self.assertIn('Ngân sách rủi ro: 10 USD/lệnh',text)
        self.assertIn('Lỗ ước tính tại SL: ≈ 5.04 USD',text)
        self.assertIn('<b>Lot tham khảo: 0.01 lot chuẩn (XAUUSD)</b>',rich_text(text))
        self.assertEqual(sizing.estimated_money_text('0.000001','usd'),'< 0.01 USD')
        self.assertEqual(sizing.estimated_money_text('1.2345678','cent'),'≈ 123.46 USC (Cent)')
        cent=dict(prefs(),account_type='cent',cent_spec=dict(sizing.CENT_SPEC))
        text,result=sizing.annotation(dict(config,sizing_profile=cent),dict(price=3600),'100')
        self.assertEqual(result['lot'],'1.98')
        self.assertEqual(Decimal(result['estimated_risk_usd']),Decimal('9.9792'))
        self.assertIn('Lỗ ước tính tại SL: ≈ 997.92 USC (Cent)',text)

    def test_pending_input_survives_restart_and_validation_is_atomic(self):
        for text in (sizing.CENT,sizing.CAPITAL):interact(self,text)
        self.store.close();self.store=TradeStore(self.path);self.addCleanup(self.store.close)
        interact(self,'50000');interact(self,sizing.MONEY);interact(self,'500')
        before=sizing.profile(self.store,'100')
        interact(self,sizing.CAPITAL)
        for invalid in ('0','NaN','1e3','100'):
            _,sent=interact(self,invalid)
            self.assertIn('Chưa lưu',sent[-1][1])
            self.assertEqual(sizing.profile(self.store,'100'),before)
            self.assertEqual(sizing.ui(self.store,'100')[1],'capital')
        interact(self,sizing.CANCEL)
        self.assertFalse(interact(self,'123')[0])
        interact(self,sizing.CAPITAL)
        self.assertFalse(interact(self,sizing.HOME)[0])
        self.assertIsNone(sizing.ui(self.store,'100')[1])

    def test_buttons_and_numbers_cannot_edit_another_user_or_group(self):
        interact(self,sizing.CENT);interact(self,sizing.CAPITAL)
        interact(self,'1000',sender='200')
        interact(self,sizing.USD,sender='200')
        interact(self,sizing.USD,kind='group')
        self.assertEqual(sizing.ui(self.store,'100'),('cent','capital'))
        self.assertIsNone(sizing.profile(self.store,'100'))
        self.assertIsNone(sizing.profile(self.store,'200'))

    def test_delete_requires_button_confirmation_and_is_private(self):
        for chat in ('100','200'):
            sizing.save(self.store,chat,'capital','1000')
        interact(self,sizing.CONFIRM)
        self.assertIsNotNone(sizing.profile(self.store,'100'))
        interact(self,sizing.DELETE);interact(self,sizing.CANCEL)
        self.assertIsNotNone(sizing.profile(self.store,'100'))
        interact(self,sizing.DELETE);interact(self,sizing.CONFIRM)
        self.assertIsNone(sizing.profile(self.store,'100'))
        self.assertIsNotNone(sizing.profile(self.store,'200'))

    def test_additive_ui_schema_preserves_existing_usd_profile(self):
        sizing.save(self.store,'100','capital','1500');sizing.save(self.store,'100','risk','1%')
        before=sizing.profile(self.store,'100')
        self.store.db.execute('DROP TABLE user_risk_ui');self.store.db.commit();self.store.close()
        self.store=TradeStore(self.path);self.addCleanup(self.store.close)
        self.assertEqual(sizing.profile(self.store,'100'),before)
        self.assertEqual(sizing.ui(self.store,'100'),('usd',None))

    def test_snapshot_keeps_account_type_when_user_switches(self):
        for text in (sizing.CENT,sizing.CAPITAL,'100000',sizing.PERCENT,'1'):interact(self,text)
        config=dict(sizing_profile=sizing.profile(self.store,'100'),sizing_spec=SPEC,sl=95,direction=1,slippage=0,commission=0)
        self.store.queue_notification('cent-entry','ENTRY','100',delivery=config)
        interact(self,sizing.USD)
        frozen=json.loads(self.store.db.execute('SELECT delivery_json FROM notifications').fetchone()[0])
        text,result=sizing.annotation(frozen,dict(price=100),'100')
        self.assertEqual(result['lot'],'2');self.assertIn('cent lot (XAUUSDc)',text)
        legacy=dict(config,sizing_profile=prefs())
        self.assertEqual(sizing.annotation(legacy,dict(price=100),'100')[1]['lot'],'0.02')

    def test_real_poll_all_settings_without_a_single_slash_command(self):
        stop=Event();sent=[]
        messages=[(100,sizing.HOME),(100,sizing.BUTTON),(100,sizing.CENT),(200,sizing.USD),
                  (100,sizing.CAPITAL),(200,sizing.CAPITAL),(100,'100000'),(200,'2000'),
                  (100,sizing.PERCENT),(200,sizing.MONEY),(100,'1'),(200,'20'),
                  (999,sizing.CENT),(999,sizing.CAPITAL),(999,'999'),(100,sizing.VIEW)]
        updates=[dict(update_id=i+1,message={'chat':{'id':chat,'type':'private'},'from':{'id':chat},'text':text})
                 for i,(chat,text) in enumerate(messages)]
        def call(method,payload,timeout=None):
            if method=='getUpdates':stop.set();return {'result':updates}
            sent.append(payload);return {'result':{'message_id':len(sent)}}
        with patch.dict(os.environ,{'TELEGRAM_TEST_CHAT_ID':'','TELEGRAM_TEST_STATE_DB':'','SIGNAL_ENGINE':'intraday-live'}):
            tg=bot.Telegram('synthetic','100',chat_ids=['100','200'])
            with patch.object(tg,'call',side_effect=call):tg.poll(self.store,stop)
        self.assertEqual(sizing.profile(self.store,'100')['account_type'],'cent')
        self.assertEqual(sizing.profile(self.store,'200')['account_type'],'usd')
        self.assertEqual(sizing.profile(self.store,'100')['risk_value'],'1')
        self.assertEqual(sizing.profile(self.store,'200')['risk_value'],'20')
        self.assertIsNone(sizing.profile(self.store,'999'))
        self.assertTrue(all('reply_markup' in p for p in sent))
        self.assertFalse(any('/von' in p['text'] or '/ruiro' in p['text'] for p in sent))
        self.assertFalse(any('2000 USD' in p['text'] for p in sent if str(p['chat_id'])=='100'))
