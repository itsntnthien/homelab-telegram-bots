import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
from intraday_live import IntradayEngine,IntradayRunner,COMPONENTS,CONFIG,CONFIG_HASH,VERSION,scheduled_flat
from test_intraday_live import pending,T
import test_intraday_live as base_tests
from test_ensemble_research import c
from trades import TradeStore
from migrate_combined_state import migrate
import bot


class CombinedLiveTest(unittest.TestCase):
    def setup(self,group='Q100'):
        e=IntradayEngine();e.s['pending']=dict(pending(),strategy=group,quality=100)
        return e

    def select(self,e,a,b):
        with patch('intraday_live.advance_subtypes',return_value=({},[],{})),patch('intraday_live.combine',side_effect=[a,b]):
            return e.bar('m30',None)

    def test_priority_conflict_and_independent_q100(self):
        e=IntradayEngine();a={T:c(T)};b={T:c(T,q=100)}
        self.select(e,a,b);self.assertEqual(e.s['pending']['strategy'],'Q100');self.assertTrue(e.s['pending']['dual_agreement'])
        e=IntradayEngine();self.assertEqual(self.select(e,a,{T:c(T,d=-1,q=100)}),[])
        e=IntradayEngine();self.select(e,{},b);self.assertEqual(e.s['pending']['strategy'],'Q100')
        self.assertFalse(CONFIG['backtest_qualified']);self.assertFalse(CONFIG['pine_parity_verified'])

    def test_cooldown_is_individual_persistent_and_shared(self):
        e=IntradayEngine();e.s['last_entry']=T;e.s['last_entries']={'CURRENT':T-3600,'Q100':T}
        e=IntradayEngine(json.loads(json.dumps(e.snapshot())))
        self.assertEqual(self.select(e,{T+599:c(T+599)},{}),[])
        self.assertTrue(self.select(e,{T+600:c(T+600)},{}))
        e.s['pending']=None
        self.assertEqual(self.select(e,{}, {T+5399:c(T+5399,q=100)}),[])
        self.assertTrue(self.select(e,{}, {T+5400:c(T+5400,q=100)}))

    def test_q100_geometry_chase_and_six_hour_exit(self):
        e=self.setup();self.assertEqual(e.tick(T,100.3,100.54),[])
        event=e.tick(T+1,99.7,99.94)[0];p=event['position']
        self.assertAlmostEqual(p['entry']-p['sl'],3.,places=3)
        self.assertAlmostEqual(p['tp']-p['entry'],9.,places=3)
        self.assertEqual(p['maximum_hold_minutes'],360)
        e.s['last_tick']=T+14400
        self.assertEqual(e.tick(T+14401,100,100.24),[])
        e.s['last_tick']=T+21600
        self.assertEqual(e.tick(T+21601,100,100.24)[0]['event_type'],'TIME_EXIT')

    def test_early_known_close_and_no_fictional_quote(self):
        with patch('intraday_live.market_is_open',side_effect=lambda at:at.timestamp()<T+3600):
            self.assertEqual(scheduled_flat(T),T+2700)
            e=self.setup();p=e.tick(T,100,100.24)[0]['position'];self.assertEqual(p['flat_at'],T+2700)
            e=IntradayEngine(json.loads(json.dumps(e.snapshot())))
            self.assertTrue(e.s['position'])
            e.s['last_tick']=T+2699
            self.assertEqual(e.tick(T+2700,100,100.24)[0]['event_type'],'DAY_CLOSE')

    def test_message_has_component_and_correct_hold_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tg=bot.Telegram('fake','-42',chat_ids=['-42'])
            r=base_tests.IntradayLiveTest().runner(tmp,tg);r.store.ensure_users(['-42'],'XAUUSD')
            r.engine.s['pending'].update(strategy='Q100',quality=100)
            events=r.engine.tick(T,100,100.24)
            with r.store.transaction():r.emit(events,datetime.fromtimestamp(T,timezone.utc).isoformat(),datetime.fromtimestamp(T,timezone.utc));r.save()
            body=r.store.db.execute('SELECT body FROM notifications').fetchone()[0]
            self.assertIn('Q100',body);self.assertIn('360 phút',body);self.assertNotIn('H1:201',body)
            from setup_view import lookup, explain
            record=lookup(r.store.db,'-42',str(r.store.db.execute('SELECT id FROM trades').fetchone()[0]))
            self.assertIn('H1:201',explain(record))
            market,state=r.market_path,r.state_path;r.close();r=IntradayRunner(market,state,tg)
            self.assertEqual(r.engine.s['position']['strategy'],'Q100');r.close()

    def test_flat_migration_preserves_cursor_throttle_unknown_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=TradeStore(Path(tmp)/'state.db');old=IntradayEngine().snapshot()
            old.update(config_hash='oldhash',cursor=321,last_entry=T,pending=pending())
            old.pop('last_entries');store.save_engine_state('XAUUSD','M30-H1','old',str(T),old)
            store.queue_notification('unknown','old','-42');store.notification_uncertain('unknown','timeout')
            report=migrate(store,'old','oldhash',VERSION,CONFIG_HASH)
            state=store.load_engine_state('XAUUSD','M30-H1',VERSION)['state']
            self.assertEqual(state['cursor'],321);self.assertEqual(state['last_entries'],{'CURRENT':T,'Q100':T})
            self.assertIsNone(state['pending']);self.assertEqual(report['cancelled_setup'],old['pending']['key'])
            self.assertEqual(store.notification_counts(),{'UNKNOWN':1})
            self.assertEqual(store.load_engine_state('XAUUSD','M30-H1','old')['state'],old)
            migrate(store,VERSION,CONFIG_HASH,'old','oldhash')
            self.assertEqual(store.load_engine_state('XAUUSD','M30-H1','old')['state']['cursor'],321)
            store.create(event_key='busy',timeframe='M30',signal='MAIN BUY',entry=100,sl=99,tp=102)
            with self.assertRaisesRegex(ValueError,'open trade'):migrate(store,'old','oldhash',VERSION,CONFIG_HASH)
            store.close()


if __name__=='__main__':unittest.main()
