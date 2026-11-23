import json
import os
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from intraday_live import (IntradayEngine, IntradayRunner, CONFIG, CONFIG_HASH, VERSION, iso, blackout)
from subtype_research import build_signals, advance_subtypes
from pine_engine import PineEngine
from pine_config import PineConfig
from signal_engine import Bar
from tick_journal import append as journal_append
from trades import TradeStore, MODEL_NAMES, update_message
from test_ensemble_research import c
import bot

def append_tick(db, symbol, provider, received, bid, ask, origin):
    return journal_append(db, symbol, bid, ask, received, provider)


T = datetime(2026, 11, 17, 12, tzinfo=timezone.utc).timestamp()


def pending(t=T, direction=1):
    result = c(t, d=direction)
    result.update(key='test:'+str(t), expires=t+1800, initial_checked=False,
                  supporting_models=['M30:101', 'H1:201'], supporting_confirmations=[], model_name=MODEL_NAMES[101])
    return result


class IntradayLiveTest(unittest.TestCase):
    def engine(self, t=T, direction=1):
        e = IntradayEngine(); e.s['pending'] = pending(t, direction)
        return e

    def test_fixed_selected_config_not_overall_research_winner(self):
        p = CONFIG['profile']
        self.assertEqual((CONFIG['research_spec'], p['timeframe'], p['target_r'], p['minimum_quality']),
                         ('current-plus-f17d652e66be32852dcf', 'm30', 2.5, 75))
        self.assertEqual(CONFIG['members'], list(MODEL_NAMES))
        self.assertIsNone(p['maximum_entries_daily'])
        self.assertFalse(CONFIG['pine_parity_verified'])
        self.assertTrue(CONFIG['experimental'])

    def test_direct_uses_actual_bid_ask_and_costs(self):
        e = self.engine(); ev = e.tick(T+.2, 100, 100.24)
        p = ev[0]['position']
        self.assertAlmostEqual(p['entry'], 100.26)
        self.assertAlmostEqual(p['tp']-p['entry'], 2.5*(p['entry']-p['sl']), places=3)
        self.assertGreaterEqual(p['entry']-p['sl'], 2.)

    def test_retest_uses_current_tick_not_past_bar_low(self):
        e = self.engine()
        self.assertFalse(e.tick(T+.1, 101, 101.24))
        self.assertFalse(e.tick(T+1, 100.3, 100.54))
        ev = e.tick(T+2, 99.7, 99.94)
        self.assertEqual(ev[0]['event_type'], 'PRIMARY_OPEN')
        self.assertAlmostEqual(ev[0]['position']['entry'], 99.96)
        self.assertTrue(ev[0]['position']['deferred'])

    def test_late_close_cannot_backfill_bar_open(self):
        e = self.engine()
        self.assertFalse(e.tick(T+30, 100, 100.24))
        self.assertIsNone(e.s['position'])
        self.assertTrue(e.tick(T+31, 99.7, 99.94))

    def test_future_expired_blackout_and_stale_entries(self):
        e = self.engine()
        self.assertFalse(e.tick(T-1, 99.7, 99.94))
        self.assertFalse(e.tick(T, 100, 100.24, allow_entry=False))
        self.assertEqual(e.tick(T+1800, 99.7, 99.94)[0]['event_type'], 'EXPIRED')
        for text in ('2026-07-01T19:45:00+00:00', '2026-11-17T20:45:00+00:00'):
            at = datetime.fromisoformat(text).timestamp()
            self.assertTrue(blackout(at))
            e = self.engine(at)
            self.assertEqual(e.tick(at, 100, 100.24)[0]['event_type'], 'EXPIRED')

    def test_spike_stop_then_recovery_is_not_lost(self):
        e = self.engine(); e.tick(T, 100, 100.24)
        ev = e.tick(T+1, 97, 97.24)
        self.assertEqual(ev[0]['event_type'], 'SL')
        self.assertLess(ev[0]['realized_r'], -1)
        self.assertFalse(e.tick(T+2, 104, 104.24))
        self.assertIsNone(e.s['position'])

    def test_sell_uses_ask_exit_and_target_before_later_stop(self):
        e = self.engine(direction=-1); e.tick(T, 100, 100.24)
        p = e.s['position']; ev = e.tick(T+1, p['tp']-.3, p['tp'])
        self.assertEqual(ev[0]['event_type'], 'TP2')
        self.assertFalse(e.tick(T+2, 103, 103.24))

    def test_time_exit_flat_day_and_data_gap(self):
        for kind, at in [('TIME_EXIT', T+14400), ('DAY_CLOSE', datetime(2026,11,17,21,tzinfo=timezone.utc).timestamp())]:
            e = self.engine(); e.tick(T,100,100.24)
            if kind == 'DAY_CLOSE':e.s['position']['entered'] = at-60
            e.s['last_tick'] = at-1
            ev = e.tick(at,101,101.24)
            self.assertEqual(ev[0]['event_type'], kind)
        e = self.engine(); e.tick(T,100,100.24)
        self.assertEqual(e.tick(T+301,101,101.24)[0]['event_type'], 'DATA_GAP')

    def test_restart_matches_uninterrupted_execution(self):
        e = self.engine(); e.tick(T,101,101.24)
        restored = IntradayEngine(json.loads(json.dumps(e.snapshot())))
        for t,bid,ask in [(T+1,99.7,99.94),(T+2,100,100.24),(T+3,106,106.24)]:
            self.assertEqual(e.tick(t,bid,ask),restored.tick(t,bid,ask))
        self.assertEqual(e.snapshot(),restored.snapshot())

    def test_invalid_quotes_and_config_fail_closed(self):
        for bid,ask in [(float('nan'),1),(101,100),(-1,1)]:
            with self.assertRaises(ValueError):self.engine().tick(T,bid,ask)
        e = self.engine(); e.tick(T,101,101.24)
        with self.assertRaises(ValueError):e.tick(T-1,100,100.24)
        state = e.snapshot(); state['config_hash'] = 'wrong'
        with self.assertRaises(ValueError):IntradayEngine(state)

    def test_single_position_and_throttle_survive_restart(self):
        e = self.engine(); e.tick(T,100,100.24)
        old = e.s['position']['key']; e.s['pending'] = pending(T+10)
        self.assertFalse(e.tick(T+10,100,100.24))
        self.assertEqual(e.s['position']['key'],old)
        e = IntradayEngine(e.snapshot())
        self.assertEqual(e.s['last_entry'],T)

    def test_shared_incremental_calculation_restart_equals_research(self):
        rng=random.Random(42); source={};price=2000
        for i in range(600):
            op=price;price+=rng.uniform(-3,3);t=int(T)+i*1800
            source[t]=dict(time=iso(t),open=op,high=max(op,price)+.4,low=min(op,price)-.4,close=price,volume=10)
        expected,_=build_signals(source,'m30');e=PineEngine('m30',PineConfig(tick_size=.001))
        actual={sub:{} for sub in MODEL_NAMES}
        for i,b in enumerate(source.values()):
            if i==300:e=PineEngine('m30',PineConfig(tick_size=.001),json.loads(json.dumps(e.snapshot())))
            cs,_,_=advance_subtypes(e,Bar(*[b[k] for k in ('time','open','high','low','close','volume')]))
            for sub,candidate in cs.items():actual[sub][candidate['time']]=candidate
        self.assertEqual(actual,expected)
        self.assertGreater(sum(map(len,actual.values())),0)

    def test_h1_support_quality_conflict_and_future(self):
        e = IntradayEngine()
        with patch('intraday_live.advance_subtypes',return_value=({201:c(T,201)},[],{})):
            e.bar('h1',None)
        with patch('intraday_live.advance_subtypes',return_value=({101:c(T)},[],{})):
            self.assertEqual(e.bar('m30',None)[0]['event_type'],'SETUP')
        self.assertIn('H1:201',e.s['pending']['supporting_models'])
        e.s['pending']=None;e.s['higher']['201']=c(T+3600,201)
        with patch('intraday_live.advance_subtypes',return_value=({101:c(T)},[],{})):
            self.assertFalse(e.bar('m30',None))
        e.s['higher']['201']=c(T,201,-1)
        with patch('intraday_live.advance_subtypes',return_value=({101:c(T)},[],{})):
            self.assertFalse(e.bar('m30',None))

    def runner(self, directory, telegram=None):
        market=Path(directory)/'market.db';db=sqlite3.connect(market)
        db.executescript("CREATE TABLE market_meta(key PRIMARY KEY,value); INSERT INTO market_meta VALUES('provider','exness'); "
            "CREATE TABLE bars(symbol,timeframe,timestamp,open,high,low,close,volume,received_at_utc,is_closed);")
        from tick_journal import install as ensure_tick_journal
        ensure_tick_journal(db);db.commit();db.close()
        r=IntradayRunner(market,Path(directory)/'intraday-shadow.db',telegram)
        for e in r.engine.engines.values():e.s['last_close']=iso(T)
        r.engine.s['pending']=pending()
        return r

    def test_atomic_ledger_state_outbox_restart_and_fake_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            tg=bot.Telegram('fake','-42',chat_ids=['-42']);r=self.runner(d,tg);r.store.ensure_users(['-42'],'XAUUSD')
            db=sqlite3.connect(r.market_path)
            append_tick(db,'XAUUSD',iso(T),iso(T),100,100.24,'provider');db.commit()
            h=r.step(datetime.fromtimestamp(T,timezone.utc));self.assertIsNone(h['error'])
            self.assertEqual(r.store.db.execute('select count(*) from trades').fetchone()[0],1)
            self.assertEqual(r.store.notification_counts(),{'PENDING':1})
            from setup_view import lookup, explain
            identity=str(r.store.db.execute('SELECT id FROM trades').fetchone()[0])
            self.assertIn('H1:201',explain(lookup(r.store.db,'-42',identity)))
            self.assertTrue(r.engine.s['position'])
            r.close();r=IntradayRunner(Path(d)/'market.db',Path(d)/'intraday-shadow.db',tg)
            r.step(datetime.fromtimestamp(T,timezone.utc))
            self.assertEqual(r.store.db.execute('select count(*) from trades').fetchone()[0],1)
            p=r.engine.s['position']
            append_tick(db,'XAUUSD',iso(T+1),iso(T+1),p['tp'],p['tp']+.24,'provider');db.commit()
            r.step(datetime.fromtimestamp(T+1,timezone.utc))
            trade=r.store._trade(r.store.db.execute('select * from trades').fetchone())
            self.assertEqual(trade.status,'TP');self.assertGreater(trade.realized_r,2.)
            self.assertIn('R',update_message(trade))
            # The test uses historical timestamps, so explicitly move notification deadlines only in this fixture.
            r.store.db.execute('update notifications set expires_at=NULL,delivery_json=NULL');r.store.db.commit()
            sent=[]
            with patch.object(tg,'call',side_effect=lambda *args,**kwargs:(sent.append(args) or {'result':{'message_id':len(sent)}})):
                bot.deliver_pending(r.store,tg);bot.deliver_pending(r.store,tg)
            self.assertEqual(len(sent),2)
            self.assertEqual(r.store.notification_counts(),{'SENT':2})
            r.close();db.close()

    def test_failure_rolls_back_tick_entry_outbox_and_retries(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.runner(d);db=sqlite3.connect(r.market_path)
            append_tick(db,'XAUUSD',iso(T),iso(T),100,100.24,'provider');db.commit()
            before=r.engine.snapshot()
            with patch.object(r,'save',side_effect=sqlite3.OperationalError('database is locked')):
                h=r.step(datetime.fromtimestamp(T,timezone.utc))
            self.assertIn('locked',h['error']);self.assertEqual(r.engine.snapshot(),before)
            self.assertEqual(r.store.db.execute('select count(*) from trades').fetchone()[0],0)
            self.assertIsNone(r.step(datetime.fromtimestamp(T,timezone.utc))['error'])
            self.assertTrue(r.engine.s['position']);r.close();db.close()

    def test_recovery_stale_ticks_do_not_publish_old_entries(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.runner(d);db=sqlite3.connect(r.market_path)
            append_tick(db,'XAUUSD',iso(T),iso(T),100,100.24,'provider');db.commit()
            r.step(datetime.fromtimestamp(T+100,timezone.utc))
            self.assertIsNone(r.engine.s['position'])
            self.assertEqual(r.store.db.execute('select count(*) from trades').fetchone()[0],0)
            r.close();db.close()

    def test_menu_status_describe_only_entry_m30_and_context_h1(self):
        with patch.dict(os.environ,{'SIGNAL_ENGINE':'intraday-live'}):
            markup=bot.Telegram.menu_markup(['m30'])
            self.assertEqual(len(markup['inline_keyboard']),1)
            text=bot.Telegram.status_text()
            self.assertIn('thử nghiệm',text);self.assertIn('M30 + H1',text)
            self.assertNotIn('M1:',text)

    def test_bar_visibility_waits_for_h1_and_orders_h1_first(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.runner(d);r.engine.s['pending']=None
            r.engine.engines['h1'].s['last_close']=iso(T-3600)
            r.engine.engines['m30'].s['last_close']=iso(T-1800)
            db=sqlite3.connect(r.market_path)
            for tf,width,delay in [('h1',3600,2),('m30',1800,1)]:
                db.execute('insert into bars values(?,?,?,?,?,?,?,?,?,1)',
                           ('XAUUSD',tf,iso(T-width),100,101,99,100,1,iso(T+delay)))
            db.commit();order=[]
            def close(tf,bar,historical=False):
                order.append(tf);r.engine.engines[tf].s['last_close']=iso(T);return []
            with patch.object(r.engine,'bar',side_effect=close):
                r.advance(T+1,iso(T+1))
                self.assertEqual(order,[])
                self.assertEqual(r.errors['m30'],'WAITING_FOR_CLOSED_H1')
                r.advance(T+2,iso(T+2))
                self.assertEqual(order,['h1','m30'])
            r.close();db.close()

    def test_missing_or_invalid_bar_never_advances_calculator_but_exit_works(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.runner(d);r.engine.tick(T,100,100.24)
            db=sqlite3.connect(r.market_path)
            db.execute('insert into bars values(?,?,?,?,?,?,?,?,?,1)',
                       ('XAUUSD','m30',iso(T+1800),100,101,99,100,1,iso(T+3600)))
            db.commit();prior=r.engine.engines['m30'].s['last_close']
            r.advance(T+3600,iso(T+3600))
            self.assertIn('GAP',r.errors['m30'])
            self.assertEqual(r.engine.engines['m30'].s['last_close'],prior)
            self.assertEqual(r.engine.tick(T+1,97,97.24,allow_entry=False)[0]['event_type'],'SL')
            r.close();db.close()

    def test_existing_old_trade_blocks_new_engine(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.runner(d)
            r.store.create(event_key='old',timeframe='M30',signal='MAIN BUY',entry=100,sl=99,tp=102)
            with self.assertRaisesRegex(ValueError,'old engine'):
                r.verify_position()
            r.close()

    def test_additive_migration_preserves_unknown_and_old_history(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.db';store=TradeStore(path)
            store.create(event_key='old',timeframe='M30',signal='MAIN BUY',entry=100,sl=99,tp=102)
            store.queue_notification('unknown','old','-42');store.notification_uncertain('unknown','timeout')
            store.close();store=TradeStore(path)
            self.assertEqual(store.notification_counts(),{'UNKNOWN':1})
            self.assertEqual(store.db.execute('select count(*) from trades').fetchone()[0],1)
            self.assertEqual(store.pending_notifications(),[])
            store.close()


if __name__=='__main__':unittest.main()
