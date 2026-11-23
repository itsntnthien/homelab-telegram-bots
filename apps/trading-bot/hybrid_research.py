"""Causal M5 scalp candidates and a single-position intraday/scalp portfolio."""
from bisect import bisect_right
from dataclasses import asdict
from itertools import product
import hashlib
import json

from intraday_execution import Profile
from pine_config import PineConfig, SECONDS
from pine_engine import PineEngine
from signal_engine import Bar
from subtype_research import advance_subtypes
from trades import MODEL_NAMES


def configurations():
    specs=[]
    for members,context,q,tp,hold,stop in product(
            [list(MODEL_NAMES),[201,301,302]], ['aligned','nonopposed'],
            [75,90],[1.5,2.],[30,60],[.5,1.]):
        profile=Profile('m5',tp,stop,q,'all',maximum_hold_minutes=hold,
                        minimum_net_rr=1.2,maximum_entries_daily=None,daily_loss_limit_r=None)
        spec=dict(members=members,context=context,profile=asdict(profile))
        spec['id']=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:12]
        specs.append(spec)
    return specs


def build_frame(source,tf):
    engine=PineEngine(tf,PineConfig(tick_size=.001));candidates={};contexts={}
    for t,b in source.items():
        bar=Bar(b['time'],*[float(b[k]) for k in ('open','high','low','close','volume')])
        cs,_,context=advance_subtypes(engine,bar)
        stamp=t+SECONDS[tf]
        candidates[stamp]=list(cs.values())
        contexts[stamp]=dict(context,time=stamp)
    return candidates,contexts


def context_direction(context):
    return 1 if context['regime'] in (3,7) else -1 if context['regime'] in (4,8) else 0


def scalp_candidate(current,m15,h1,spec,stamp):
    if not m15 or not h1 or not 0<=stamp-m15['time']<900 or not 0<=stamp-h1['time']<3600:
        return None
    cs=[c for c in current if c['subtype'] in spec['members'] and c['quality']>=max(spec['profile']['minimum_quality'],spec.get('minimum_quality_by_subtype',{}).get(str(c['subtype']),0))]
    if not cs or len({c['direction'] for c in cs})!=1:return None
    primary=max(cs,key=lambda c:(c['grade'],c['quality'],-c['family'],-c['slot']))
    d=primary['direction'];d15=context_direction(m15);d1=context_direction(h1)
    if d15!=d or (d1!=d if spec['context']=='aligned' else d1==-d):return None
    return dict(primary,strategy='SCALP',timeframe='m5',
                supporting_models=[f'M5:{c["subtype"]}' for c in cs]+['M15:CONTEXT','H1:CONTEXT'],
                supporting_confirmations=[dict(timeframe=tf,confirmed=ctx['time'],regime=ctx['regime'])
                                          for tf,ctx in [('m15',m15),('h1',h1)]])


def scalp_signals(frames,spec):
    output={};m15=frames['m15'][1];h1=frames['h1'][1];t15=sorted(m15);t1=sorted(h1)
    for stamp,cs in frames['m5'][0].items():
        if not cs:continue
        i=bisect_right(t15,stamp)-1;j=bisect_right(t1,stamp)-1
        c=scalp_candidate(cs,m15[t15[i]] if i>=0 else None,h1[t1[j]] if j>=0 else None,spec,stamp)
        if c:output[stamp]=c
    return output


def merge_signals(intraday,scalp):
    """Intraday takes priority at the same timestamp; opposing simultaneous setups veto both."""
    merged={}
    for t in sorted(intraday.keys()|scalp.keys()):
        a,b=intraday.get(t),scalp.get(t)
        if a and b and a['direction']!=b['direction']:continue
        chosen=dict(a,strategy='INTRADAY',timeframe='m30') if a else dict(b)
        if a and b:
            chosen['supporting_models']=list(dict.fromkeys(a.get('supporting_models',[])+b.get('supporting_models',[])))
        merged[t]=chosen
    return merged


def neighbors(a,b):
    if a['id']==b['id'] or a['members']!=b['members'] or a['context']!=b['context']:return False
    return sum(a['profile'][k]!=b['profile'][k] for k in a['profile'])==1
