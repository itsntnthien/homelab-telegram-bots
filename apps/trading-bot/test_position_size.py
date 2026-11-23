import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import bot
import delivery
from intraday_live import IntradayRunner, IntradayEngine, iso
from position_size import calculate, save, profile, annotation, market_spec
from trades import TradeStore
from test_intraday_live import pending, T

SPEC = dict(instrument='XAUUSD',quote_currency='USD',contract_size='100',volume_min='0.01',volume_max='200',volume_step='0.01')


def prefs(capital='1000', risk='1', mode='percent', chat='100'):
    return dict(chat_id=chat,capital_usd=capital,risk_mode=mode,risk_value=risk)


class ArithmeticTest(unittest.TestCase):
    def test_two_capitals_and_fixed_money(self):
        self.assertEqual(calculate(prefs(),SPEC,100,95,1)['lot'],'0.02')
        self.assertEqual(calculate(prefs('2000'),SPEC,100,95,1)['lot'],'0.04')
        self.assertEqual(calculate(prefs('2000','10','usd'),SPEC,100,95,1)['lot'],'0.02')

    def test_decimal_floor_never_exceeds_budget_for_buy_or_sell(self):
        for d in (-1,1):
            for capital in ('1000','1999.99','2000.01'):
                r=calculate(prefs(capital),SPEC,100,100-d*5,d,slippage='.02',commission='.01')
                self.assertLessEqual(Decimal(r['estimated_risk_usd']),Decimal(r['budget_usd']))
                self.assertEqual(Decimal(r['lot'])%Decimal('.01'),0)
        self.assertEqual(calculate(prefs(),SPEC,100,95,1,slippage='.02')['lot'],'0.01')

    def test_minimum_and_maximum_are_respected(self):
        self.assertEqual(calculate(prefs('100'),SPEC,100,95,1)['state'],'BELOW_MINIMUM')
        self.assertEqual(calculate(prefs('1000000000','100'),SPEC,100,95,1)['lot'],'200')

    def test_bad_geometry_nonfinite_and_wrong_contract_fail(self):
        for price,sl,d in ((100,101,1),(100,99,-1),(100,100,1),('NaN',99,1),(100,0,1),(100,99,True)):
            with self.subTest(price=price,sl=sl,d=d),self.assertRaises(ValueError):
                calculate(prefs(),SPEC,price,sl,d)
        for spec in (dict(SPEC,instrument='XAUUSDc'),dict(SPEC,quote_currency='EUR'),dict(SPEC,volume_step='0'),dict(SPEC,volume_min='300')):
            with self.assertRaises(ValueError):calculate(prefs(),spec,100,95,1)


class PersonalizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'trades.db'
        self.store=TradeStore(self.path);self.addCleanup(self.store.close)

    def test_settings_survive_restart_and_do_not_cross_users(self):
        save(self.store,'100','capital','1000');save(self.store,'100','risk','1%')
        save(self.store,'200','capital','2000');save(self.store,'200','risk','15usd')
        with self.store.transaction():self.store.set_user_timeframe('200','XAUUSD','m30',False)
        self.store.close();self.store=TradeStore(self.path);self.addCleanup(self.store.close)
        self.assertEqual(profile(self.store,'100')['capital_usd'],'1000')
        self.assertEqual(profile(self.store,'200')['risk_value'],'15')
        self.assertFalse(self.store.user_enabled('200','XAUUSD','m30'))
        self.assertIsNone(profile(self.store,'300'))

    def test_invalid_input_is_atomic_and_risk_units_are_explicit(self):
        save(self.store,'100','capital','1000');save(self.store,'100','risk','10usd')
        original=profile(self.store,'100')
        for field,value in [('capital','0'),('capital','-1'),('capital','1e6'),('capital','NaN'),('capital','5'),
                            ('capital','1,000,000'),('risk','1'),('risk','101%'),('risk','1001usd'),('risk','0%')]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):save(self.store,'100',field,value)
            self.assertEqual(profile(self.store,'100'),original)
        save(self.store,'100','risk','0,5%')
        self.assertEqual(profile(self.store,'100')['risk_value'],'0.5')

    def test_additive_schema_preserves_ledger_outbox_preferences_and_checkpoint(self):
        s=self.store;s.ensure_users(['100','200'],'XAUUSD')
        s.create(event_key='existing',timeframe='M30',signal='MAIN BUY',entry=100,sl=95,tp=115)
        s.queue_notification('existing-note','old message','100')
        s.set_checkpoint('telegram_update_id','123')
        tables=('trades','notifications','user_settings','checkpoints')
        before={t:sorted((tuple(r) for r in s.db.execute('SELECT * FROM '+t)),key=repr) for t in tables}
        s.db.execute('DROP TABLE user_risk_settings');s.db.commit();s.close()
        self.store=TradeStore(self.path);self.addCleanup(self.store.close)
        after={t:sorted((tuple(r) for r in self.store.db.execute('SELECT * FROM '+t)),key=repr) for t in tables}
        self.assertEqual(before,after)
        self.assertIsNone(profile(self.store,'100'))
        save(self.store,'100','capital','1000')
        self.assertEqual(self.store.checkpoint('telegram_update_id'),'123')

    def test_telegram_commands_only_write_the_authorized_private_owner(self):
        stop=Event();sent=[]
        commands=[(100,'private',100,'/von 1000'),(100,'private',100,'/ruiro 1%'),
                  (200,'private',200,'/von 2000'),(200,'private',200,'/ruiro 10usd'),
                  (999,'private',999,'/von 9999'),(100,'group',100,'/von 8888'),
                  (100,'private',200,'/von 7777'),(100,'private',100,'/lot'),
                  (200,'private',200,'/lot off')]
        updates=[dict(update_id=i+1,message={'chat':{'id':chat,'type':kind},'from':{'id':sender},'text':text})
                 for i,(chat,kind,sender,text) in enumerate(commands)]
        def call(method,payload,timeout=None):
            if method=='getUpdates':stop.set();return {'result':updates}
            sent.append(payload);return {'result':{'message_id':len(sent)}}
        with patch.dict(os.environ,{'TELEGRAM_TEST_CHAT_ID':'','TELEGRAM_TEST_STATE_DB':'','SIGNAL_ENGINE':'intraday-live'}):
            tg=bot.Telegram('synthetic','100',chat_ids=['100','200'])
            with patch.object(tg,'call',side_effect=call):tg.poll(self.store,stop)
        self.assertEqual(profile(self.store,'100')['capital_usd'],'1000')
        self.assertIsNone(profile(self.store,'200'));self.assertIsNone(profile(self.store,'999'))
        self.assertEqual(self.store.checkpoint('telegram_update_id'),'10')
        self.assertTrue(all(str(p['chat_id']) in ('100','200') for p in sent))
        self.assertFalse(any('2000 USD' in p.get('text','') for p in sent if str(p['chat_id'])=='100'))

    def test_entry_emit_snapshots_each_recipient_without_changing_engine_state(self):
        for chat,capital in [('100','1000'),('200','2000')]:
            save(self.store,chat,'capital',capital);save(self.store,chat,'risk','1%')
        engine=IntradayEngine();engine.s['pending']=pending(T)
        events=engine.tick(T,100,100.24);before=engine.snapshot()
        market=sqlite3.connect(':memory:');self.addCleanup(market.close)
        market.execute('CREATE TABLE market_meta(key TEXT PRIMARY KEY,value TEXT)')
        market.execute('INSERT INTO market_meta VALUES(?,?)',('conditions',json.dumps(SPEC)))
        runner=object.__new__(IntradayRunner)
        runner.store=self.store;runner.market=market;runner.market_path=Path(self.tmp.name)/'market.db'
        runner.telegram=SimpleNamespace(chat_ids=['100','200'])
        runner.emit(events,iso(T),datetime.fromtimestamp(T,timezone.utc))
        rows=self.store.db.execute('SELECT chat_id,delivery_json FROM notifications ORDER BY chat_id').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual([json.loads(r[1])['sizing_profile']['capital_usd'] for r in rows],['1000','2000'])
        self.assertEqual(engine.snapshot(),before)
        save(self.store,'100','capital','5000')
        self.assertEqual(json.loads(rows[0][1])['sizing_profile']['capital_usd'],'1000')

    def test_delivery_rechecks_price_and_uses_only_queued_personal_snapshot(self):
        s=self.store;now=datetime.now(timezone.utc)
        trade,_=s.create(event_key='signal',timeframe='M30',signal='MAIN BUY',entry=100,sl=95,tp=115)
        base=dict(trade_id=trade.id,entry=100,sl=95,tp=115,direction=1,slippage=.02,commission=0,
                  initial_risk=5.04,atr=10,maximum_chase_atr=.25,minimum_net_rr=1.2,spread_floor=.3,
                  market_path='unused',sizing_spec=SPEC)
        for chat,capital in [('100','1000'),('200','2000')]:
            save(s,chat,'capital',capital);save(s,chat,'risk','1%')
            s.queue_notification('entry:'+chat,'ENTRY',chat,delivery=dict(base,sizing_profile=profile(s,chat)))
        save(s,'100','capital','9000')
        rows=s.db.execute('SELECT * FROM notifications ORDER BY chat_id').fetchall()
        q=(now.isoformat(),now.isoformat(),99.9,100)
        with patch.object(delivery,'latest_quote',return_value=q):
            a=delivery.prepare(s,rows[0],now);b=delivery.prepare(s,rows[1],now)
        self.assertIn('0.01 lot',a);self.assertIn('0.03 lot',b)
        self.assertIn('Vốn 1000 USD',a);self.assertNotIn('9000',a);self.assertNotIn('2000',a)
        checks=[json.loads(r[0])['position_size'] for r in s.db.execute('SELECT check_json FROM notifications ORDER BY chat_id')]
        self.assertEqual([r['lot'] for r in checks],['0.01','0.03'])
        with patch.object(delivery,'latest_quote',return_value=(now.isoformat(),now.isoformat(),101.9,102)):
            moved=delivery.prepare(s,rows[1],now)
        self.assertIn('0.02 lot',moved)
        self.assertEqual(s.db.execute('SELECT entry,sl,tp,status FROM trades').fetchone()[:],(100,95,115,'OPEN'))
        sent=[];tg=bot.Telegram('synthetic','100',chat_ids=['100','200'])
        def call(method,payload,timeout=None):
            self.assertEqual(method,'sendMessage');sent.append(payload)
            return {'result':{'message_id':len(sent)}}
        def fresh(_):
            at=(datetime.now(timezone.utc)-timedelta(milliseconds=50)).isoformat()
            return at,at,99.9,100
        with patch.object(delivery,'latest_quote',side_effect=fresh),patch.object(tg,'call',side_effect=call):
            bot.deliver_pending(s,tg);bot.deliver_pending(s,tg)
        self.assertEqual(len(sent),2)
        messages={str(p['chat_id']):p['text'] for p in sent}
        self.assertIn('0.01 lot',messages['100']);self.assertIn('0.03 lot',messages['200'])
        self.assertNotIn('2000 USD',messages['100'])
        self.assertEqual(s.notification_counts(),{'SENT':2})

    def test_unconfigured_and_wrong_destination_never_leak_another_profile(self):
        base=dict(sl=95,direction=1,slippage=0,commission=0,sizing_spec=SPEC)
        text,result=annotation(dict(base,sizing_profile=None),dict(price=100),'100')
        self.assertEqual(result['state'],'UNCONFIGURED')
        text,result=annotation(dict(base,sizing_profile=prefs(chat='200')),dict(price=100),'100')
        self.assertEqual(result['state'],'INVALID_PROFILE');self.assertNotIn('1000',text)
        text,result=annotation(dict(base,sizing_profile=prefs(),sizing_spec=None),dict(price=100),'100')
        self.assertEqual(result['state'],'UNAVAILABLE')

    def test_missing_spec_and_below_minimum_do_not_silently_supply_a_lot(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('CREATE TABLE market_meta(key TEXT PRIMARY KEY,value TEXT)')
        self.assertIsNone(market_spec(db))
        config=dict(sl=95,direction=1,slippage=0,commission=0,sizing_spec=SPEC,sizing_profile=prefs('100'))
        text,result=annotation(config,dict(price=100),'100')
        self.assertEqual(result['state'],'BELOW_MINIMUM');self.assertIn('Không tự nâng lot',text)
