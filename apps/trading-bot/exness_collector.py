"""Exness Bid history and ordered quotes in a dedicated canonical market database."""
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from tick_journal import install, append
from market_quality import utc_timestamp, BAR_SECONDS


def connect(path):
    db=sqlite3.connect(path,timeout=10)
    db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS market_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bars(symbol,timeframe,timestamp,open,high,low,close,volume,
            source,received_at_utc,provider_time_utc,is_closed,price_scale,symbol_digits,quality,
            PRIMARY KEY(symbol,timeframe,timestamp));
        CREATE TABLE IF NOT EXISTS latest_ticks(symbol PRIMARY KEY,timestamp,bid,ask,received_at,provider_timestamp,source);''')
    existing=db.execute("SELECT value FROM market_meta WHERE key='provider'").fetchone()
    if existing and existing[0]!='exness':
        db.close();raise ValueError('refusing mixed providers')
    if db.execute("SELECT 1 FROM bars WHERE source IS NULL OR source!='exness' LIMIT 1").fetchone():
        db.close();raise ValueError('refusing foreign bars')
    install(db)
    with db:
        db.execute("INSERT OR REPLACE INTO market_meta VALUES('provider','exness')")
        db.execute("INSERT OR REPLACE INTO market_meta VALUES('canonical_bars_v1','exness-native-utc')")
    return db


def schedule(conditions, observed=None):
    if conditions.get('instrument')!='XAUUSD' or conditions.get('point_digits')!=3:
        raise ValueError('instrument metadata mismatch')
    def second(value):
        day,clock=value.split(', ');h,m,s=map(int,clock.split(':'))
        if not (0<=h<24 and 0<=m<60 and 0<=s<60):raise ValueError('invalid session')
        return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].index(day)*86400+h*3600+m*60+s
    observed=observed or datetime.now(timezone.utc)
    week_start=observed.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=(observed.weekday()+1)%7)
    def local_second(utc_second):
        at=(week_start+timedelta(seconds=utc_second)).astimezone(ZoneInfo('America/New_York'))
        return ((at.weekday()+1)%7)*86400+at.hour*3600+at.minute*60+at.second
    intervals=[]
    for item in conditions['trade_sessions']:
        if item['trade_mode']!='enabled':continue
        start,end=second(item['week_start_time']),second(item['week_end_time'])+1
        if not 0<=start<end<=604800:raise ValueError('invalid session')
        local_start,local_end=local_second(start),local_second(end)
        if local_end<=local_start:raise ValueError('unsupported week-wrapping session')
        intervals.append(dict(startSecond=local_start,endSecond=local_end))
    if not intervals:raise ValueError('empty trading calendar')
    return dict(provider='exness',scheduleTimeZone='America/New_York',schedule=intervals,holiday=[],
                observed_at=observed.isoformat(),historical_schedule_verified=False,
                derivation='Current authenticated UTC sessions converted to New York local time; historical DST mapping is an inference, not holiday evidence')


def accept(db,event):
    received=utc_timestamp(datetime.fromtimestamp(event['receivedAt']/1000,timezone.utc))
    if abs((datetime.now(timezone.utc)-received).total_seconds())>30:
        raise ValueError('stale transport event')
    with db:
        if event['type']=='metadata':
            calendar=schedule(event['conditions'])
            db.execute("INSERT OR REPLACE INTO market_meta VALUES('conditions',?)",(json.dumps(event['conditions']),))
            db.execute("INSERT OR REPLACE INTO market_meta VALUES('schedule',?)",(json.dumps(calendar),))
        elif event['type']=='history':
            tf=event['timeframe'];seconds=BAR_SECONDS[tf]
            previous=None
            for b in event['bars']:
                at=utc_timestamp(b['time'])
                if previous is not None and at<=previous:raise ValueError('unordered history')
                previous=at
                if int(at.timestamp())%seconds:raise ValueError('misaligned history')
                values=[float(b[k]) for k in ('open','high','low','close','volume')]
                op,hi,lo,cl,vol=values
                if not all(math.isfinite(v) for v in values) or lo<=0 or vol<0 or lo>min(op,cl) or hi<max(op,cl):
                    raise ValueError('invalid history')
                if at+timedelta(seconds=seconds)>received:continue
                stamp=at.isoformat()
                db.execute('''INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,timeframe,timestamp) DO UPDATE SET open=excluded.open,
                    high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume''',
                    ('XAUUSD',tf,stamp,*values,'exness',received.isoformat(),stamp,1,1,3,'OK'))
        elif event['type']=='quote':
            bid,ask=float(event['bid']),float(event['ask']);at=utc_timestamp(event['timestamp'])
            last=db.execute("SELECT timestamp,bid,ask FROM latest_ticks WHERE symbol='XAUUSD'").fetchone()
            if last and (at.isoformat(),bid,ask)==tuple(last):return
            seq=append(db,'XAUUSD',bid,ask,received,at)
            if seq is None:return
            db.execute('INSERT OR REPLACE INTO latest_ticks VALUES(?,?,?,?,?,?,?)',
                       ('XAUUSD',at.isoformat(),bid,ask,received.isoformat(),at.isoformat(),'exness'))
        elif event['type']!='status':raise ValueError('unknown transport event')
        db.execute("INSERT OR REPLACE INTO market_meta VALUES('transport_status',?)",
                   (json.dumps(dict(type=event['type'],state=event.get('state'),code=event.get('code'),received_at=received.isoformat())),))


def main():
    path=Path(os.environ['EXNESS_MARKET_DB']).resolve()
    if path.name!='market-exness.db':raise ValueError('dedicated market-exness.db required')
    db=connect(path)
    command=json.loads(os.environ['EXNESS_TRANSPORT_COMMAND'])
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8')
    def stop(*_):proc.terminate()
    signal.signal(signal.SIGTERM,stop)
    try:
        for line in proc.stdout:
            event=json.loads(line);accept(db,event)
            if event['type']=='metadata':
                p=path.with_name('exness-schedule.json');tmp=p.with_suffix('.tmp')
                tmp.write_text(json.dumps(schedule(event['conditions']),indent=2),encoding='utf-8');tmp.replace(p)
            if event['type'] in ('status','metadata'):
                print(json.dumps({k:event[k] for k in ('type','state','code','receivedAt') if k in event}),flush=True)
        return proc.wait()
    finally:
        if proc.poll() is None:proc.terminate();proc.wait(timeout=10)
        db.close()


if __name__=='__main__':
    raise SystemExit(main())
