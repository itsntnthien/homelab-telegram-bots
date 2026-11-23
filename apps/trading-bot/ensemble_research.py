"""Causal combinations of the six existing intraday research subtypes."""
from collections import defaultdict
from dataclasses import asdict
from itertools import combinations,product
import hashlib
import json
from intraday_execution import Profile
from pine_config import SECONDS
from trades import MODEL_NAMES

EVIDENCE={101:'basis',102:'basis',201:'structure',202:'range',301:'structure',302:'swing_sweep'}


def configurations():
    specs=[]
    def add(members,rule,tf,q,tp):
        profile=Profile(tf,tp,1.,q,'all',minimum_net_rr=1.2,
                        maximum_entries_daily=None,daily_loss_limit_r=None)
        label=rule+':'+','.join(map(str,members))
        spec=dict(model=label,members=list(members),rule=rule,window=1,profile=asdict(profile),
                  control=len(members)==1)
        spec['id']=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:12]
        specs.append(spec)
    for n in range(1,7):
        for members in combinations(MODEL_NAMES,n):
            for tf,q,tp in product(('m15','m30','h1'),(75,90),(2.,2.5)):
                add(members,'best_unopposed',tf,q,tp)
    for rule in ('agree2_samebar','agree2_recent','diverse2_recent'):
        for tf,q,tp in product(('m15','m30','h1'),(75,90),(2.,2.5)):
            add(tuple(MODEL_NAMES),rule,tf,q,tp)
    for tf,q,tp in product(('m15','m30'),(75,90),(2.,2.5)):
        add(tuple(MODEL_NAMES),'h1_support',tf,q,tp)
    return specs


def signal_key(spec):
    return tuple(spec['members']),spec['rule'],spec['profile']['timeframe'],spec['profile']['minimum_quality']


def combine(streams,spec):
    """Only current closed-bar candidates trigger; recent votes never trigger alone."""
    members,rule,tf,quality=signal_key(spec);width=SECONDS[tf]
    grouped=defaultdict(list);higher=defaultdict(list)
    for subtype in members:
        for t,c in streams[subtype,tf].items():
            if c['quality']>=quality:grouped[t].append(c)
        if rule=='h1_support':
            for t,c in streams[subtype,'h1'].items():
                if c['quality']>=quality:higher[t].append(c)
    recent={};htf_recent={};output={}
    times=sorted(set(grouped)|set(higher))
    def usable(c,t,ttl,price):
        return 0<=t-c['time']<=ttl and (price-c['sl'])*c['direction']>0
    for t in times:
        for c in higher.get(t,[]):htf_recent[c['subtype']]=c
        current=grouped.get(t,[])
        if not current:continue
        if any(c['time']!=t for c in current):raise ValueError('candidate confirmation timestamp mismatch')
        for c in current:recent[c['subtype']]=c
        if len({c['direction'] for c in current})!=1:continue
        primary=max(current,key=lambda c:(c['grade'],c['quality'],-c['family'],-c['slot']))
        direction=primary['direction'];price=primary['entry']
        voters=current;vote_tf=tf
        if rule in ('agree2_recent','diverse2_recent'):
            voters=[c for c in recent.values() if usable(c,t,width,price)]
        elif rule=='h1_support':
            # Strictly younger than one complete H1 interval; exactly stale votes expire.
            voters=[c for c in htf_recent.values() if usable(c,t,SECONDS['h1']-1,price)]
            vote_tf='h1'
        if any(c['direction']!=direction for c in voters):continue
        if rule in ('agree2_samebar','agree2_recent') and len({c['subtype'] for c in voters})<2:continue
        if rule=='diverse2_recent' and len({EVIDENCE[c['subtype']] for c in voters})<2:continue
        if rule=='h1_support' and not voters:continue
        evidence=[dict(timeframe=tf,subtype=c['subtype'],confirmed=c['time']) for c in current]
        evidence.extend(dict(timeframe=vote_tf,subtype=c['subtype'],confirmed=c['time']) for c in voters)
        evidence=sorted({(e['timeframe'],e['subtype'],e['confirmed']) for e in evidence})
        output[t]=dict(primary,ensemble_rule=rule,
                       supporting_models=[f'{frame.upper()}:{sub}' for frame,sub,_ in evidence],
                       supporting_confirmations=[dict(timeframe=frame,subtype=sub,confirmed=stamp) for frame,sub,stamp in evidence])
    return output
