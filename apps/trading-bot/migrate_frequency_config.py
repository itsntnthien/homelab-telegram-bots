"""Flat-only hash transition preserving every committed state field and checkpoint."""
import json


def migrate(db,version,old_hash,new_hash):
    if not db.in_transaction:raise ValueError('migration requires caller transaction')
    rows=db.execute('SELECT rowid,state_json FROM engine_states WHERE symbol=? AND timeframe=? AND engine_version=?',('XAUUSD','m30-h1',version)).fetchall()
    if len(rows)!=1:raise ValueError('exactly one saved engine required')
    rowid,raw=rows[0];state=json.loads(raw)
    if state.get('config_hash')!=old_hash:raise ValueError('unexpected configuration')
    if state.get('position') or state.get('pending'):raise ValueError('engine must be flat without pending setup')
    if db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]:raise ValueError('open trade')
    if db.execute("SELECT COUNT(*) FROM notifications WHERE state IN ('PENDING','FAILED','SENDING')").fetchone()[0]:raise ValueError('outbox must drain')
    state['config_hash']=new_hash
    db.execute('UPDATE engine_states SET state_json=? WHERE rowid=?',(json.dumps(state),rowid))
    return dict(cursor=state.get('cursor'),last_tick=state.get('last_tick'),last_entry=state.get('last_entry'),last_entries=state.get('last_entries'))
