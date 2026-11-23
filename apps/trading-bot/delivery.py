"""Delivery decisions never mutate the simulated trading ledger."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import math
import sqlite3


def utcnow():
    return datetime.now(timezone.utc)


def missed(store, row, reason, now, check=None):
    key = row['event_key']
    store.db.execute("UPDATE notifications SET state='EXPIRED',last_error=?,check_json=? WHERE event_key=? AND state IN ('PENDING','SENDING')",
                     (reason, json.dumps(check or {'reason':reason}), key))
    if not row['delivery_json']:
        return
    config = json.loads(row['delivery_json'])
    deadline = datetime.fromisoformat(row['expires_at'] or row['eligible_at'] or row['created_at'])+timedelta(seconds=60)
    if now >= deadline:
        return
    store.queue_notification('missed:'+key,
        'ĐÃ BỎ LỠ — KHÔNG VÀO LỆNH\n'+config['label']+'\nLý do: '+reason+
        '\nSự kiện vẫn lưu trong lịch sử mô phỏng; không phải lệnh đã khớp tại broker.',
        row['chat_id'], expires_at=deadline.isoformat(), priority=2)


def reconcile_queue(store):
    now = utcnow()
    with store.transaction():
        for row in store.db.execute("SELECT * FROM notifications WHERE state='PENDING' AND expires_at IS NOT NULL AND expires_at<=?", (now.isoformat(),)).fetchall():
            missed(store, row, 'Hết hạn gửi', now)
        rows = store.db.execute("""SELECT n.*,p.state AS parent_state,p.message_ids AS parent_ids
            FROM notifications n LEFT JOIN notifications p ON p.event_key=n.depends_on
            WHERE n.state='PENDING' AND n.depends_on IS NOT NULL""").fetchall()
        for row in rows:
            state = row['parent_state']
            if state == 'SENT':
                ids = json.loads(row['parent_ids'] or 'null')
                reply = ids if isinstance(ids,int) and not isinstance(ids,bool) else None
                store.db.execute('UPDATE notifications SET reply_message_id=? WHERE event_key=?', (reply,row['event_key']))
            elif state == 'UNKNOWN':
                store.db.execute("UPDATE notifications SET depends_on=NULL,body=?,last_error=? WHERE event_key=?",
                    ('ĐỐI SOÁT — chưa xác minh được tin entry\n'+row['body'], 'parent delivery unknown', row['event_key']))
            elif state in ('EXPIRED','DEAD','SUPPRESSED') or state is None:
                store.db.execute("UPDATE notifications SET state='SUPPRESSED',last_error=? WHERE event_key=?",
                                 ('entry not delivered: '+str(state),row['event_key']))


def latest_quote(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as db:
        return db.execute("SELECT provider_time,received_at,bid,ask FROM tick_events WHERE symbol='XAUUSD' ORDER BY id DESC LIMIT 1").fetchone()


def assess(config, quote, now):
    if quote is None:
        return 'WAIT', {'reason':'Chưa có giá mới'}
    provider, received, bid, ask = quote
    check = dict(checked_at=now.isoformat(),provider_time=provider,received_at=received,bid=bid,ask=ask)
    if not provider or not received or not all(math.isfinite(v) and v>0 for v in (bid,ask)) or ask<bid:
        return 'WAIT', dict(check,reason='Giá không hợp lệ')
    age = (now-datetime.fromisoformat(provider)).total_seconds()
    receive_age = (now-datetime.fromisoformat(received)).total_seconds()
    if not 0<=age<=1 or not 0<=receive_age<=1 or provider>received:
        return 'WAIT',dict(check,reason='Giá cũ hoặc timestamp không hợp lệ',quote_age_seconds=age)
    d=config['direction'];price=ask if d==1 else bid
    fill=price+d*config['slippage']
    gross_risk=(fill-config['sl'])*d
    reward=(config['tp']-fill)*d-config['commission']-config['slippage']
    risk=gross_risk+config['commission']+config['slippage']
    rr=reward/risk if risk>0 else -1
    delta=(price-config['entry'])*d
    check.update(price=price,quote_age_seconds=age,delta_usd=delta,
                 delta_r=delta/config['initial_risk'],net_rr=rr)
    exit_quote=bid if d==1 else ask
    reason = ('Giá đã chạm SL/TP' if (exit_quote-config['sl'])*d<=0 or (exit_quote-config['tp'])*d>=0 or gross_risk<=0 or (config['tp']-fill)*d<=0 else
              'Giá chạy quá giới hạn chase' if max(0,delta)>config['maximum_chase_atr']*config['atr']+1e-9 else
              'Net RR không còn đủ' if rr<config['minimum_net_rr'] else
              'Spread/risk quá cao' if max(ask-bid,config['spread_floor'])/gross_risk>.20 else None)
    return ('SKIP' if reason else 'SEND'),dict(check,reason=reason or 'Còn phù hợp')


def prepare(store,row,now=None):
    """Called after acquiring the destination's sending slot, before HTTP."""
    now=now or utcnow()
    if row['expires_at'] and now>=datetime.fromisoformat(row['expires_at']):
        with store.transaction(): missed(store,row,'Hết hạn gửi',now)
        return None
    if not row['delivery_json']:
        return row['body']
    config=json.loads(row['delivery_json'])
    trade=store.db.execute('SELECT status FROM trades WHERE id=?',(config['trade_id'],)).fetchone()
    if not trade or trade[0]!='OPEN':
        action,check='SKIP',dict(reason='Vị thế mô phỏng đã đóng')
    else:
        try:
            quote=latest_quote(config['market_path'])
            action,check=assess(config,quote,now)
            if action=='SEND' and config.get('engine_version'):
                saved=store.load_engine_state('XAUUSD','M30-H1',config['engine_version'])
                committed=saved['state'].get('last_tick') if saved else None
                if committed is None or committed<datetime.fromisoformat(quote[0]).timestamp():
                    action,check='WAIT',dict(check,reason='Chờ engine xử lý hết tick trước khi gửi')
        except (sqlite3.Error,ValueError,TypeError,OverflowError):
            action,check='WAIT',dict(reason='Chưa đọc được quote hợp lệ')
    with store.transaction():
        store.db.execute('UPDATE notifications SET check_json=? WHERE event_key=?',(json.dumps(check),row['event_key']))
        if action=='SKIP': missed(store,row,check['reason'],now,check)
    if action!='SEND':
        return None
    at=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%H:%M:%S %d/%m/%Y')
    return row['body']+f"\n\nGiá kiểm tra: {check['price']:.3f} · RR còn {check['net_rr']:.2f}\nLệch theo hướng lệnh: {check['delta_usd']:+.3f} USD\nKiểm tra {at} (VN) · giá có thể thay đổi."
