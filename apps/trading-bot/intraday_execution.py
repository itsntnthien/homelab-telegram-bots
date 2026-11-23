"""Offline intraday execution model. Never imported by the production bot.

Bid/ask M1 OHLC does not reveal tick order. Stop-first and target-first runs
bound ambiguous exits; a retest-minute target is deferred to the next minute.
"""
from dataclasses import dataclass, asdict
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import hashlib
import json
import math


@dataclass(frozen=True)
class Profile:
    timeframe: str = 'm30'
    target_r: float = 2.0
    stop_atr_floor: float = 1.0
    minimum_quality: int = 85
    session: str = 'london_new_york'
    minimum_net_rr: float = 1.2
    maximum_chase_atr: float = .25
    maximum_hold_minutes: int = 240
    maximum_entries_daily: int | None = 3
    daily_loss_limit_r: float | None = 2.0

    def __post_init__(self):
        if self.timeframe not in ('m5','m15','m30','h1') or self.session not in ('london_new_york','new_york','all'):
            raise ValueError('invalid timeframe or session')
        if any(not math.isfinite(v) or v<=0 for v in (self.target_r,self.stop_atr_floor,self.minimum_net_rr,self.maximum_hold_minutes)):
            raise ValueError('positive finite profile values required')
        if any(v is not None and (not math.isfinite(v) or v<=0) for v in (self.maximum_entries_daily,self.daily_loss_limit_r)):
            raise ValueError('daily limits must be positive or None')
        if self.maximum_entries_daily is not None and int(self.maximum_entries_daily)!=self.maximum_entries_daily:
            raise ValueError('entry limit must be an integer')
        if not 0<=self.maximum_chase_atr<=1 or not 0<=self.minimum_quality<=100:
            raise ValueError('invalid chase or quality')

    @property
    def id(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()[:12]


def execution_exit(position, row, policy='stop_first', allow_target=True):
    """Gap SL uses the worse executable open; limit TP gets no gap improvement."""
    side = 0 if position['direction']==1 else 1
    op,hi,lo,cl = row['bid'] if side==0 else row['ask']
    sl,tp,d = position['sl'],position['tp'],position['direction']
    stop_gap = op<=sl if d==1 else op>=sl
    target_gap = op>=tp if d==1 else op<=tp
    if stop_gap:return op,'SL_GAP',False
    if allow_target and target_gap:return tp,'TP',False
    stop = lo<=sl if d==1 else hi>=sl
    target = allow_target and (hi>=tp if d==1 else lo<=tp)
    if stop and target:return (tp,'TP',True) if policy=='target_first' else (sl,'SL',True)
    if stop:return sl,'SL',False
    if target:return tp,'TP',False
    return None


def make_position(candidate,row,profile,cost,entry_reference=None,deferred=False):
    d=candidate['direction']; spread=row['ask'][0]-row['bid'][0]
    if spread<0:raise ValueError('crossed quote')
    entry=(row['ask'][0] if d==1 else row['bid'][0]) if entry_reference is None else entry_reference
    entry += d*cost['slippage']
    anchor=candidate['sl']
    if (entry-anchor)*d<=0:return None
    risk=math.ceil(max((entry-anchor)*d,candidate['atr']*profile.stop_atr_floor)*1000-1e-8)/1000
    if risk<=0 or not math.isfinite(risk):return None
    sl=round(entry-d*risk,3);tp=round(entry+d*risk*profile.target_r,3)
    # R includes commission and adverse stop slippage; spread is paid in entry.
    denominator=risk+cost['commission']+cost['slippage']
    net_rr=(risk*profile.target_r-cost['commission']-cost['slippage'])/denominator
    if net_rr<profile.minimum_net_rr or max(spread,cost['spread_floor'])/risk>.20:return None
    return dict({k:candidate[k] for k in ('subtype','model_id','model_name','ensemble_rule','supporting_models','supporting_confirmations','strategy','timeframe') if k in candidate},
                direction=d,entry=entry,sl=sl,tp=tp,risk=denominator,
                entered=row['time'],entry_day=row['day'],confirmed=candidate.get('confirmation_time',candidate['time']),
                family=candidate['family'],quality=candidate['quality'],deferred=deferred,
                execution_resolution='minute_range' if deferred else 'minute_open',
                signal_reference=candidate['entry'],max_adverse=0.,max_favorable=0.)


def simulate(rows,signals,profile,cost=None,policy='stop_first',start=None,end=None,profiles=None,cooldown_bars=2):
    cost=cost or dict(slippage=.02,commission=0.,spread_floor=.30)
    if set(cost)!= {'slippage','commission','spread_floor'} or any(not math.isfinite(v) or v<0 for v in cost.values()):
        raise ValueError('invalid execution cost')
    if policy not in ('stop_first','target_first'):raise ValueError('invalid ambiguity policy')
    if type(cooldown_bars) is not int or cooldown_bars not in (1,2,3):raise ValueError('invalid cooldown')
    width={'m5':300,'m15':900,'m30':1800,'h1':3600}[profile.timeframe]
    if profiles and any(p.session != 'all' or p.maximum_entries_daily is not None or p.daily_loss_limit_r is not None for p in profiles.values()):
        raise ValueError('hybrid profiles must share the uncapped all-session policy')
    last_entries={}
    trades=[];position=None;pending=None;last_entry=-10**15;day=None;count=0;daily_r=0.;prior=None
    counters=dict(candidates=0,expired=0,ambiguous=0,gap_exits=0,overnight=0,intraminute_retests=0)
    equity=0.;peak=0.;mtm_drawdown=0.
    def close(price,reason,row,ambiguous=False):
        nonlocal position,equity,peak,mtm_drawdown,daily_r
        # TP is a limit fill; adverse slippage applies to market/stop exits.
        fill=price if reason=='TP' else price-position['direction']*cost['slippage']
        result=((fill-position['entry'])*position['direction']-cost['commission'])/position['risk']
        item=dict(position,exit_time=row['time'],exit=fill,reason=reason,r=result,
                  held_minutes=(row['time']-position['entered'])/60,ambiguous=ambiguous)
        trades.append(item);equity+=result;daily_r+=result;peak=max(peak,equity)
        mtm_drawdown=max(mtm_drawdown,peak-equity);position=None
        counters['ambiguous']+=int(ambiguous)
    for original in rows:
        t=original['time']
        if start is not None and t<start:continue
        if end is not None and t>=end:break
        if prior is not None and t<=prior['time']:raise ValueError('unordered minute history')
        row=original
        if cost['spread_floor']:
            # Widen ask above bid to the scenario floor, preserving actual larger spreads.
            row=dict(original,ask=tuple(max(a,b+cost['spread_floor']) for a,b in zip(original['ask'],original['bid'])))
        if row['day']!=day:day=row['day'];count=0;daily_r=0.
        occupied_at_open=position is not None
        if position:
            discontinuity=prior is not None and t-prior['time']>300
            flat_due=t>=position['flat_at'] if profile.session=='all' else row['ny_minute']>=16*60
            overnight=t>=position['rollover_at'] if profile.session=='all' else position['entry_day']!=row['day'] or row['ny_minute']>=17*60
            if discontinuity:
                counters['gap_exits']+=1;counters['overnight']+=int(overnight)
                close(row['bid'][0] if position['direction']==1 else row['ask'][0],'DATA_GAP',row)
            elif flat_due or (t-position['entered'])>=position.get('maximum_hold_minutes',profile.maximum_hold_minutes)*60:
                close(row['bid'][0] if position['direction']==1 else row['ask'][0],
                      'DAY_CLOSE' if flat_due else 'TIME_EXIT',row)
            else:
                hit=execution_exit(position,row,policy)
                if hit:close(*hit[:2],row,hit[2])
                else:
                    adverse=(position['entry']-(row['bid'][2] if position['direction']==1 else row['ask'][1]))*position['direction']/position['risk']
                    favorable=((row['bid'][1] if position['direction']==1 else row['ask'][2])-position['entry'])*position['direction']/position['risk']
                    position['max_adverse']=max(position['max_adverse'],adverse)
                    position['max_favorable']=max(position['max_favorable'],favorable)
                    peak=max(peak,equity+favorable)
                    mtm_drawdown=max(mtm_drawdown,peak-(equity-adverse))
        candidate=signals.get(t)
        if candidate:
            candidate_profile=profiles[candidate['strategy']] if profiles else profile
            candidate_width={'m5':300,'m15':900,'m30':1800,'h1':3600}[candidate_profile.timeframe]
            last_group_entry=last_entries.get(candidate.get('strategy'),-10**15) if profiles else last_entry
            counters['candidates']+=1
            session=(not 15*60+45<=row['ny_minute']<17*60) if profile.session=='all' else (row['london_minute']>=8*60 and 0<=row['ny_minute']<15*60) if profile.session=='london_new_york' else 8*60<=row['ny_minute']<15*60
            if (not position and not occupied_at_open and session and candidate['quality']>=candidate_profile.minimum_quality and
                (profile.maximum_entries_daily is None or count<profile.maximum_entries_daily) and
                (profile.daily_loss_limit_r is None or daily_r>-profile.daily_loss_limit_r+1e-9) and
                t-last_group_entry>=cooldown_bars*candidate_width and (not profiles or t-last_entry>=600)):
                pending=dict(candidate,expires=candidate.get('confirmation_time',t)+candidate_width)
        entry_blackout=15*60+45<=row['ny_minute']<17*60 if profile.session=='all' else row['ny_minute']>=15*60
        if pending and (t>=pending['expires'] or entry_blackout):
            pending=None;counters['expired']+=1
        if pending and not position:
            entry_profile=profiles[pending['strategy']] if profiles else profile
            d=pending['direction'];op=row['bid'][0]
            initial=t==pending['time']
            chase=max(0.,(op-pending['entry'])*d)
            direct=initial and chase<=entry_profile.maximum_chase_atr*pending['atr']
            touched=(row['ask'][2]<=pending['entry'] if d==1 else row['bid'][1]>=pending['entry'])
            if direct or touched:
                ref=None if direct else (min(row['ask'][0],pending['entry']) if d==1 else max(row['bid'][0],pending['entry']))
                position=make_position(pending,row,entry_profile,cost,ref,deferred=not direct)
                pending=None
                if position:
                    if profiles:
                        position['maximum_hold_minutes']=entry_profile.maximum_hold_minutes
                        last_entries[position['strategy']]=t
                    if profile.session=='all':
                        # The trading session crosses calendar midnight, but never its next rollover.
                        session_end=date.fromisoformat(row['day'])+timedelta(days=int(row['ny_minute']>=17*60))
                        boundary=datetime(session_end.year,session_end.month,session_end.day,16,tzinfo=ZoneInfo('America/New_York'))
                        position['flat_at']=int(boundary.timestamp())
                        position['rollover_at']=int(boundary.replace(hour=17).timestamp())
                    count+=1;last_entry=t
                    counters['intraminute_retests']+=int(not direct)
                    # For a limit touched inside M1, the earlier target may precede entry.
                    hit=execution_exit(position,row,policy,allow_target=direct)
                    if hit:close(*hit[:2],row,hit[2])
        prior=row
    if position and prior:
        close(prior['bid'][3] if position['direction']==1 else prior['ask'][3],'END_OF_SAMPLE',dict(prior,time=prior['time']+60))
    return trades,dict(counters,mtm_drawdown_r=mtm_drawdown)
