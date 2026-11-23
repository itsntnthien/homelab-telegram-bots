import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from intraday_live import IntradayEngine, IntradayRunner, COMPONENTS, VERSION, iso
from test_intraday_live import T, pending
import test_intraday_live
from test_ensemble_research import c
from signal_engine import Bar
from trades import entry_message


def closed(e, tf, stamp=T, direction=1, quality=90, sub=201):
    width={'h1':3600,'m30':1800,'m15':900,'m5':300}[tf]
    bar=Bar(iso(stamp-width),100,101,99,100,1)
    ctx={'regime':7 if direction==1 else 8}
    with patch('intraday_live.advance_subtypes',return_value=({sub:c(stamp,sub,direction,quality)},[],ctx)):
        return e.bar(tf,bar)


def scalp():
    e=IntradayEngine()
    closed(e,'h1');closed(e,'m15');closed(e,'m5')
    return e


class ScalpLiveTests(unittest.TestCase):
    def test_m5_profile_and_four_calculators(self):
        e=IntradayEngine()
        self.assertEqual(set(e.engines),{'m5','m15','m30','h1'})
        p=COMPONENTS['SCALP']['profile']
        self.assertEqual((p['minimum_quality'],p['target_r'],p['maximum_hold_minutes']),(90,1.5,30))
        self.assertIsNone(p['maximum_entries_daily'])

    def test_closed_context_required_no_future_stale_or_opposed(self):
        for stamp,direction in ((T+900,1),(T-900,1),(T,-1)):
            e=IntradayEngine();closed(e,'h1');closed(e,'m15',stamp,direction)
            self.assertFalse(closed(e,'m5'));self.assertIsNone(e.s['pending'])
        e=IntradayEngine();self.assertFalse(closed(e,'m5'))

    def test_quality_and_model_filter(self):
        for q,sub in ((89,201),(100,101),(99,102),(100,202)):
            e=IntradayEngine();closed(e,'h1');closed(e,'m15')
            self.assertFalse(closed(e,'m5',quality=q,sub=sub))

    def test_reclaim_q100_enters_after_restart(self):
        e=IntradayEngine();closed(e,'h1');closed(e,'m15')
        self.assertTrue(closed(e,'m5',quality=100,sub=102))
        e=IntradayEngine(json.loads(json.dumps(e.snapshot())))
        event=e.tick(T,100,100.24)[0]
        self.assertEqual((event['position']['subtype'],event['position']['quality']),(102,100))

    def test_entry_and_thirty_minute_exit(self):
        e=scalp();self.assertEqual(e.s['pending']['expires'],T+300)
        events=e.tick(T,100,100.24);p=events[0]['position']
        self.assertEqual((p['strategy'],p['timeframe'],p['maximum_hold_minutes']),('SCALP','m5',30))
        self.assertAlmostEqual(p['tp']-p['entry'],1.5*(p['entry']-p['sl']),places=3)
        for offset in range(200,1800,200):self.assertFalse(e.tick(T+offset,100,100.24))
        self.assertEqual(e.tick(T+1800,100,100.24)[0]['event_type'],'TIME_EXIT')

    def test_retest_and_expiry_use_ticks(self):
        e=scalp();self.assertFalse(e.tick(T,101,101.24))
        self.assertEqual(e.tick(T+1,99.7,99.94)[0]['event_type'],'PRIMARY_OPEN')
        e=scalp();self.assertEqual(e.tick(T+300,99.7,99.94)[0]['event_type'],'EXPIRED')

    def test_intraday_priority_and_opposite_veto(self):
        e=IntradayEngine();closed(e,'h1');closed(e,'m15');closed(e,'m30',quality=100)
        closed(e,'m5');self.assertEqual(e.s['pending']['strategy'],'Q100')
        e=IntradayEngine();closed(e,'h1',direction=-1);closed(e,'m15',direction=-1)
        closed(e,'m30',quality=100);closed(e,'m5',direction=-1)
        self.assertIsNone(e.s['pending'])

    def test_intraday_conflict_cannot_be_bypassed_by_scalp(self):
        e=IntradayEngine();closed(e,'h1');closed(e,'m15')
        with patch('intraday_live.combine',side_effect=[{T:c(T,d=1)},{T:c(T,d=-1)}]):closed(e,'m30')
        self.assertFalse(closed(e,'m5'));self.assertIsNone(e.s['pending'])
        self.assertEqual(e.s['conflict_time'],T)

    def test_m5_cannot_replace_unexpired_intraday_pending(self):
        e=IntradayEngine();closed(e,'h1');closed(e,'m15');closed(e,'m30')
        key=e.s['pending']['key'];closed(e,'m5',T+300)
        self.assertEqual(e.s['pending']['key'],key)

    def test_single_position_restart_and_scalp_cooldown(self):
        e=scalp();e.tick(T,100,100.24)
        saved=json.loads(json.dumps(e.snapshot()));r=IntradayEngine(saved)
        self.assertEqual(e.tick(T+1,97,97.24),r.tick(T+1,97,97.24))
        closed(r,'m5',T+300);self.assertIsNone(r.s['pending'])
        closed(r,'m5',T+600);self.assertEqual(r.s['pending']['strategy'],'SCALP')

    def test_scalp_outbox_metadata_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            import bot
            tg=bot.Telegram('fake','42',chat_ids=['42'])
            r=test_intraday_live.IntradayLiveTest().runner(d,tg);r.store.ensure_users(['42'],'XAUUSD')
            r.engine=scalp();events=r.engine.tick(T,100,100.24)
            with r.store.transaction():
                r.emit(events,iso(T),datetime.fromtimestamp(T,timezone.utc));r.save()
            trade=r.store._trade(r.store.db.execute('select * from trades').fetchone())
            self.assertEqual((trade.timeframe,trade.strategy,trade.maximum_hold_minutes),('M5','SCALP',30))
            body=r.store.db.execute('select body from notifications').fetchone()[0]
            self.assertIn('SCALP · M5',body);self.assertIn('30 phút',body);self.assertNotIn('· M30 ·',body)
            state=r.engine.snapshot();mp=r.market_path;sp=r.state_path;r.close()
            r=IntradayRunner(mp,sp,tg);self.assertEqual(r.engine.snapshot(),state);r.close()

class ScalpRunnerTests(unittest.TestCase):
    def test_backlog_closes_are_processed_chronologically(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            r=test_intraday_live.IntradayLiveTest().runner(d)
            db=sqlite3.connect(r.market_path)
            for tf,width in [('h1',3600),('m30',1800),('m15',900),('m5',300)]:
                for offset in range(0,3600,width):
                    db.execute('insert into bars values(?,?,?,?,?,?,?,?,?,?)',
                        ('XAUUSD',tf,iso(T+offset),100,101,99,100,1,iso(T+3600),1))
            db.commit();seen=[]
            def accept(tf,bar):
                end=datetime.fromisoformat(bar.timestamp).timestamp()+{'h1':3600,'m30':1800,'m15':900,'m5':300}[tf]
                r.engine.engines[tf].s['last_close']=iso(end);seen.append((end,tf));return []
            with patch.object(r.engine,'bar',side_effect=accept):r.advance(T+3600,iso(T+3600))
            self.assertFalse(r.errors);self.assertEqual([t for t,tf in seen],sorted(t for t,tf in seen))
            self.assertEqual([tf for t,tf in seen if t==T+3600],['h1','m30','m15','m5'])
            r.close();db.close()

    def test_missing_m15_blocks_m5_checkpoint(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            r=test_intraday_live.IntradayLiveTest().runner(d)
            for tf in ('m5','m30'):r.engine.engines[tf].s['last_close']=iso(T+600 if tf=='m5' else T)
            db=sqlite3.connect(r.market_path)
            db.execute('insert into bars values(?,?,?,?,?,?,?,?,?,?)',('XAUUSD','m5',iso(T+600),100,101,99,100,1,iso(T+900),1));db.commit()
            r.advance(T+900,iso(T+900))
            self.assertEqual(r.errors.get('m5'),'WAITING_FOR_CLOSED_M15')
            self.assertEqual(r.engine.engines['m5'].s['last_close'],iso(T+600))
            r.close();db.close()

if __name__=='__main__':unittest.main()
