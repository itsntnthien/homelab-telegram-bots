"""Flat-only configuration handoff; retain ledger, outbox, calculators and cursor."""
from copy import deepcopy


def migrate(store, source_version, source_hash, target_version, target_hash):
    with store.transaction():
        if store.db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]:
            raise ValueError('open trade blocks cutover')
        if store.db.execute("SELECT COUNT(*) FROM notifications WHERE state IN ('PENDING','FAILED','SENDING')").fetchone()[0]:
            raise ValueError('outbox must drain before cutover')
        source=store.load_engine_state('XAUUSD','M30-H1',source_version)
        if not source or source['state']['config_hash']!=source_hash or source['state']['position']:
            raise ValueError('source state mismatch or occupied')
        target=store.load_engine_state('XAUUSD','M30-H1',target_version)
        if target and target['last_bar_timestamp']>source['last_bar_timestamp']:
            raise ValueError('target checkpoint newer than source')
        state=deepcopy(source['state']);cancelled=state.get('pending')
        state['pending']=None;state['config_hash']=target_hash
        state.setdefault('last_entries',{'CURRENT':state['last_entry'],'Q100':state['last_entry']})
        store.save_engine_state('XAUUSD','M30-H1',target_version,source['last_bar_timestamp'],state)
        return dict(cursor=state['cursor'],checkpoint=source['last_bar_timestamp'],last_entry=state['last_entry'],
                    cancelled_setup=cancelled.get('key') if cancelled else None)
