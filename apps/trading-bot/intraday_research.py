"""Data audit and independent intraday research, not a Pine parity certificate."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import numpy as np
from pine_config import PineConfig,SECONDS
from pine_engine import PineEngine
from pine_execution import context_authorized
from signal_engine import Bar


def epoch(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp())


def load_file(path):
    rows={};duplicates=0
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        b=json.loads(line);t=epoch(b['time'])
        vals=tuple(float(b[k]) for k in ('open','high','low','close'))
        op,hi,lo,cl=vals
        if not all(math.isfinite(v) and v>0 for v in vals) or not lo<=min(op,cl)<=max(op,cl)<=hi:
            raise ValueError('invalid broker OHLC')
        if t in rows:
            duplicates+=1
            if rows[t]!=b:raise ValueError('conflicting history duplicate')
        rows[t]=b
    return dict(sorted(rows.items())),duplicates


def execution_rows(folder):
    bids,bd=load_file(Path(folder)/'m1-bid.jsonl');asks,ad=load_file(Path(folder)/'m1-ask.jsonl')
    common=sorted(bids.keys()&asks.keys());rows=[];spreads=[];gaps=[]
    ny,ld=ZoneInfo('America/New_York'),ZoneInfo('Europe/London')
    prior=None
    for t in common:
        bid,ask=(tuple(float(x[t][k]) for k in ('open','high','low','close')) for x in (bids,asks))
        if ask[0]<bid[0] or ask[3]<bid[3]:raise ValueError('crossed historical bid/ask')
        dt=datetime.fromtimestamp(t,timezone.utc);n=dt.astimezone(ny);l=dt.astimezone(ld)
        rows.append(dict(time=t,bid=bid,ask=ask,day=n.date().isoformat(),ny_minute=n.hour*60+n.minute,london_minute=l.hour*60+l.minute))
        spreads.extend([ask[0]-bid[0],ask[3]-bid[3]])
        if prior and t-prior>60:gaps.append({'from':prior,'to':t,'missing_minutes':(t-prior)//60-1})
        prior=t
    return rows,dict(paired_minutes=len(rows),bid_only=len(bids.keys()-asks.keys()),ask_only=len(asks.keys()-bids.keys()),
                     duplicates=bd+ad,first=common[0],last=common[-1],gaps=gaps,
                     spread=dict(zip(['min','median','p95','p99','max'],map(float,np.quantile(spreads,[0,.5,.95,.99,1])))))


def build_signals(folder,tf,baseline=False):
    cfg=PineConfig(tick_size=.001);engine=PineEngine(tf,cfg)
    source,_=load_file(Path(folder)/(tf+'-bid.jsonl'));signals={};audit=Counter()
    for t,b in source.items():
        bar=Bar(b['time'],*[float(b[k]) for k in ('open','high','low','close','volume')])
        if baseline:
            _,diag=engine.close_bar(bar)
            winner=diag['winner'];context=engine.s['last_context']
        else:
            # Reuse the original features and grading; intraday deliberately admits
            # continuation/breakout on M15 and changes stop/target/execution policy.
            next_bias=engine._htf(bar);m=engine._market(bar)
            event,broken=engine._structure(bar)
            candidates=engine._candidates(bar,m,event,broken)
            strength,_=engine._cloud(bar,m)
            context=dict(regime=engine.s['regime'],strength=strength,
                         structure_event=engine.s['latest_event'],break_direction=engine.s['break_direction'])
            authorized=[c for c in candidates if c['family'] in (1,2) and context_authorized('m30',c,context)]
            winner=max(authorized,key=lambda c:(c['grade'],c['quality'],-c['family'],-c['slot']),default=None)
            for c in candidates:engine.s['candidates'][str(c['slot'])]['state']='CONFIRMED' if c is winner else 'ARBITRATED'
            engine.s['bias']=next_bias
        if winner:
            c=dict(winner)
            for name in ['entry','sl','tp','atr']:c[name]=cfg.price(c[name])
            c['time']=t+SECONDS[tf];c['context']=context
            signals[c['time']]=c;audit[str(c['family'])]+=1
    return signals,dict(raw_bars=len(source),candidate_count=len(signals),families=dict(audit),
                        source_hash=cfg.source_sha256,parity_verified=False)


def metrics(trades):
    rs=np.array([t['r'] for t in trades],dtype=float)
    if not len(rs):return dict(n=0,total_r=0.,mean_r=0.,profit_factor=0.,win_rate=0.,drawdown_r=0.,median_hold_minutes=0.)
    equity=np.r_[0.,np.cumsum(rs)];profit=rs[rs>0].sum();loss=-rs[rs<0].sum()
    return dict(n=len(rs),total_r=float(rs.sum()),mean_r=float(rs.mean()),
                profit_factor=float(profit/loss) if loss>0 else None,win_rate=float((rs>0).mean()),
                drawdown_r=float(np.max(np.maximum.accumulate(equity)-equity)),
                median_hold_minutes=float(np.median([t['held_minutes'] for t in trades])))


def bootstrap_days(trades,iterations=2000):
    days={}
    for t in trades:days.setdefault(t['entry_day'],[]).append(t['r'])
    groups=list(days.values());rng=np.random.default_rng(20261118)
    if not groups:return {'days':0,'mean_r_95ci':None}
    means=[];drawdowns=[]
    for _ in range(iterations):
        rs=np.concatenate([groups[i] for i in rng.integers(0,len(groups),len(groups))])
        means.append(rs.mean());eq=np.r_[0,np.cumsum(rs)]
        drawdowns.append(np.max(np.maximum.accumulate(eq)-eq))
    return dict(days=len(groups),iterations=iterations,mean_r_95ci=list(map(float,np.quantile(means,[.025,.975]))),
                drawdown_r_p95=float(np.quantile(drawdowns,.95)),positive_expectancy_frequency=float(np.mean(np.array(means)>0)))


def without_best(trades,count=5):
    excluded=set(sorted(range(len(trades)),key=lambda i:trades[i]['r'],reverse=True)[:count])
    return [t for i,t in enumerate(trades) if i not in excluded]


def file_hashes(folder):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(folder).glob('*.jsonl')}
