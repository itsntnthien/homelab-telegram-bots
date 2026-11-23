"""Isolated six-subtype intraday derivative, never a Pine parity certificate."""
from collections import Counter
from dataclasses import asdict
from itertools import product
import hashlib
import json
from pine_config import PineConfig,SECONDS
from pine_engine import PineEngine
from pine_execution import context_authorized
from signal_engine import Bar
from intraday_execution import Profile
from trades import MODEL_NAMES


def choose_by_subtype(candidates, context, authorization_tf='m30'):
    winners={}
    for c in candidates:
        if context_authorized(authorization_tf,c,context):
            old=winners.get(c['subtype'])
            if old is None or (c['grade'],c['quality'],-c['slot'])>(old['grade'],old['quality'],-old['slot']):
                winners[c['subtype']]=c
    return winners


def advance_subtypes(engine, bar):
    """One closed bar, shared by the research replay and the intraday runtime."""
    from market_quality import utc_timestamp
    t=int(utc_timestamp(bar.timestamp).timestamp());tf=engine.tf;cfg=engine.config
    if t%SECONDS[tf]:raise ValueError('unaligned source')
    if engine.s['last_close'] and t < int(utc_timestamp(engine.s['last_close']).timestamp()):
        raise ValueError('unordered source')
    next_bias=engine._htf(bar);market=engine._market(bar)
    event,broken=engine._structure(bar);candidates=engine._candidates(bar,market,event,broken)
    strength,_=engine._cloud(bar,market)
    context=dict(regime=engine.s['regime'],strength=strength,
                 structure_event=engine.s['latest_event'],break_direction=engine.s['break_direction'])
    winners=choose_by_subtype(candidates,context)
    results={}
    for c in candidates:
        engine.s['candidates'][str(c['slot'])]['state']='CONFIRMED' if c is winners.get(c['subtype']) else 'ARBITRATED'
    if market['warm']:
        for subtype,c in winners.items():
            result=dict(c,time=t+SECONDS[tf],model_id=subtype,model_name=MODEL_NAMES[subtype],context=dict(context))
            for name in ('entry','sl','tp','atr'):result[name]=cfg.price(c[name])
            results[subtype]=result
    engine.s['bias']=next_bias
    from datetime import timedelta
    engine.s['last_close']=(utc_timestamp(bar.timestamp)+timedelta(seconds=SECONDS[tf])).isoformat()
    return results,candidates,context


def build_signals(source,tf):
    cfg=PineConfig(tick_size=.001);engine=PineEngine(tf,cfg)
    signals={k:{} for k in MODEL_NAMES};audit={k:Counter() for k in MODEL_NAMES}
    prior=None
    for t,b in source.items():
        if prior is not None and t<=prior:raise ValueError('unordered source')
        if t%SECONDS[tf]:raise ValueError('unaligned source')
        bar=Bar(b['time'],*[float(b[k]) for k in ('open','high','low','close','volume')])
        results,candidates,context=advance_subtypes(engine,bar)
        combined=max(results.values(),key=lambda c:(c['grade'],c['quality'],-c['family'],-c['slot']),default=None)
        for c in candidates:
            a=audit[c['subtype']];a['eligible_after_grade_rr']+=1
            a['original_tf_context_allowed']+=int(context_authorized(tf,c,context))
            a['intraday_context_allowed']+=int(context_authorized('m30',c,context))
        for subtype,result in results.items():
            signals[subtype][result['time']]=result;audit[subtype]['isolated_decisions']+=1
        if combined:audit[combined['subtype']]['combined_winner']+=1
        prior=t
    return signals,{k:dict(v) for k,v in audit.items()}


def configurations():
    result=[]
    for subtype,tf,q,tp,stop in product(MODEL_NAMES,('m15','m30','h1'),(75,90),(1.5,2.,2.5),(.75,1.25)):
        profile=Profile(tf,tp,stop,q,'all',minimum_net_rr=1.2,
                        maximum_entries_daily=None,daily_loss_limit_r=None)
        spec=dict(model=f'pine_subtype_{subtype}',subtype=subtype,window=5,profile=asdict(profile))
        spec['id']=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:12]
        result.append(spec)
    return result
