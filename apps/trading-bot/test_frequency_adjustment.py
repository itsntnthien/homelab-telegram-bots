import unittest,json,sqlite3
from copy import deepcopy
from hybrid_research import scalp_candidate
from intraday_live import COMPONENTS,CONFIG,IntradayEngine,CONFIG_HASH,VERSION
from test_ensemble_research import c
from test_intraday_live import T,pending
from migrate_frequency_config import migrate


class FrequencyTest(unittest.TestCase):
    def choose(self,cs,spec=None,d=1):
        ctx=dict(time=T,regime=7 if d==1 else 8)
        return scalp_candidate(cs,ctx,ctx,spec or COMPONENTS['SCALP'],T)

    def test_reclaim_q100_only_and_existing_threshold_unchanged(self):
        for subtype in (102,201,301,302):
            for q in (89,90,99,100):
                item=dict(c(T,q=q),subtype=subtype)
                self.assertEqual(self.choose([item]) is not None,q>=(100 if subtype==102 else 90))
        self.assertIsNone(self.choose([dict(c(T,q=100),subtype=101)]))
        self.assertFalse(CONFIG['backtest_qualified']);self.assertFalse(CONFIG['pine_parity_verified'])

    def test_filter_before_opposite_veto_and_context_stays_strict(self):
        bull=dict(c(T,q=90),subtype=201);bear=dict(c(T,q=99,d=-1),subtype=102)
        self.assertEqual(self.choose([bull,bear])['direction'],1)
        bear['quality']=100;self.assertIsNone(self.choose([bull,bear]))
        self.assertIsNone(self.choose([bull],d=-1))
        spec=deepcopy(COMPONENTS['SCALP']);spec.pop('minimum_quality_by_subtype')
        self.assertIsNotNone(self.choose([dict(c(T,q=90),subtype=102)],spec))

    def test_trade_store_normalized_identity(self):
        import tempfile
        from pathlib import Path
        from trades import TradeStore
        with tempfile.TemporaryDirectory() as tmp:
            store=TradeStore(Path(tmp)/'state.db')
            state=IntradayEngine().snapshot();state['config_hash']='old'
            store.save_engine_state('XAUUSD','M30-H1',VERSION,'checkpoint',state)
            with store.transaction():migrate(store.db,VERSION,'old',CONFIG_HASH)
            self.assertEqual(store.load_engine_state('XAUUSD','M30-H1',VERSION)['state']['config_hash'],CONFIG_HASH)
            store.close()

    def test_flat_migration_roundtrip_preserves_state_and_history(self):
        db=sqlite3.connect(':memory:')
        db.executescript('CREATE TABLE engine_states(symbol,timeframe,engine_version,last_bar_timestamp,state_json); CREATE TABLE trades(status); CREATE TABLE notifications(state);')
        state=IntradayEngine().snapshot();state.update(cursor=123,last_entry=T,last_entries={'SCALP':T,'CURRENT':T-3600},config_hash='old')
        db.execute('INSERT INTO engine_states VALUES(?,?,?,?,?)',('XAUUSD','m30-h1',VERSION,'checkpoint',json.dumps(state)))
        db.execute("INSERT INTO notifications VALUES('UNKNOWN')");db.commit()
        db.execute('BEGIN IMMEDIATE');migrate(db,VERSION,'old',CONFIG_HASH);db.commit()
        actual=json.loads(db.execute('SELECT state_json FROM engine_states').fetchone()[0]);expected=dict(state,config_hash=CONFIG_HASH)
        self.assertEqual(actual,expected);self.assertEqual(IntradayEngine(actual).s['cursor'],123)
        self.assertEqual(db.execute('SELECT last_bar_timestamp FROM engine_states').fetchone()[0],'checkpoint')
        db.execute('BEGIN');migrate(db,VERSION,CONFIG_HASH,'old');db.commit()
        self.assertEqual(json.loads(db.execute('SELECT state_json FROM engine_states').fetchone()[0]),state)
        state['pending']=pending();db.execute('UPDATE engine_states SET state_json=?',(json.dumps(state),))
        with self.assertRaisesRegex(ValueError,'flat'):migrate(db,VERSION,'old',CONFIG_HASH)
        db.rollback();db.close()
