"""Experimental M30/H1 signal publisher. No brokerage order submission."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import os
import signal
import sqlite3
import time

from ensemble_research import combine
from intraday_execution import Profile, make_position
from market_quality import inspect_bars, interval_is_closed, market_is_open, utc_timestamp, broker_schedule
from pine_config import PineConfig, SECONDS, SOURCE_SHA256, verify_source
from pine_engine import PineEngine
from signal_engine import Bar
from subtype_research import advance_subtypes
from tick_journal import read_after
from trades import MODEL_NAMES, TradeStore, entry_message, update_message

VERSION = 'xau-intraday-combined-q100-v1'
PROFILE = Profile('m30', 2.5, 1., 75, 'all', minimum_net_rr=1.2,
                  maximum_entries_daily=None, daily_loss_limit_r=None)
COST = dict(slippage=.02, commission=0., spread_floor=.30)
CONFIG = dict(version=VERSION, research_spec='36e256104780', profile=asdict(PROFILE),
              rule='h1_support', members=list(MODEL_NAMES), mintick=.001, cost=COST,
              warmup_bars=1000, cooldown_seconds=3600, open_grace_seconds=5,
              entry_delivery_max_age_seconds=5, source_sha256=SOURCE_SHA256,
              execution='ordered_exness_ticks; actual_bid_ask; modeled_slippage',
              experimental=True, pine_parity_verified=False)
COMPONENTS = {
    'CURRENT': dict(profile=asdict(PROFILE), rule='h1_support', members=list(MODEL_NAMES), cooldown_seconds=3600),
    'Q100': dict(profile=asdict(replace(PROFILE, target_r=3., stop_atr_floor=1.5, minimum_quality=100,
                 maximum_chase_atr=.1, maximum_hold_minutes=360)), rule='best_unopposed',
                 members=[101,201,202,301,302], cooldown_seconds=5400)}
CONFIG.update(research_spec='current-plus-f17d652e66be32852dcf', components=COMPONENTS,
              priority='Q100', opposite_policy='VETO_BOTH', shared_spacing_seconds=600,
              backtest_qualified=False, deployment_authorization='explicit user request despite failed research gates',
              holiday_coverage_verified=False, close_before_known_session_end_seconds=900)
CONFIG_HASH = hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest()
NY = ZoneInfo('America/New_York')


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def blackout(stamp):
    local = datetime.fromtimestamp(stamp, NY)
    return 945 <= local.hour*60+local.minute < 1020


def scheduled_flat(stamp):
    local = datetime.fromtimestamp(stamp, NY)
    day = local.date()+timedelta(days=int(local.hour >= 17))
    flat = datetime(day.year, day.month, day.day, 16, tzinfo=NY).timestamp()
    # Only known broker closures are actionable; never invent a fill during a feed gap.
    broker_schedule.cache_clear()
    probe = math.ceil(stamp/60)*60
    while probe <= flat+900:
        if not market_is_open(datetime.fromtimestamp(probe, timezone.utc)):
            return min(flat, probe-900)
        probe += 60
    return flat


def component(item):
    return COMPONENTS[item.get('strategy', 'CURRENT')]


class IntradayEngine:
    def __init__(self, state=None):
        if state and state.get('config_hash') != CONFIG_HASH:
            raise ValueError('intraday config mismatch; explicit migration required')
        self.s = deepcopy(state) if state else dict(config_hash=CONFIG_HASH, calculators={},
            higher={}, pending=None, position=None, last_entry=-1e15, last_tick=None, cursor=0, last_entries={})
        self.engines = {tf: PineEngine(tf, PineConfig(tick_size=.001), self.s['calculators'].get(tf))
                        for tf in ('h1', 'm30')}

    def snapshot(self):
        result = deepcopy(self.s)
        result['calculators'] = {tf: e.snapshot() for tf, e in self.engines.items()}
        return result

    def bar(self, tf, bar, historical=False):
        results, _, _ = advance_subtypes(self.engines[tf], bar)
        if tf == 'h1':
            for sub, c in results.items():
                if c['quality'] >= 75:
                    self.s['higher'][str(sub)] = c
            return []
        if historical:
            return []
        streams = {(sub, frame): {} for sub in MODEL_NAMES for frame in ('m30', 'h1')}
        for sub, c in results.items():
            streams[sub, 'm30'][c['time']] = c
        for c in self.s['higher'].values():
            streams[c['subtype'], 'h1'][c['time']] = c
        current = combine(streams, COMPONENTS['CURRENT'])
        q100 = combine(streams, COMPONENTS['Q100'])
        candidates = {}
        for t in sorted(current.keys() | q100.keys()):
            a, b = current.get(t), q100.get(t)
            if a and b and a['direction'] != b['direction']:
                continue
            candidates[t] = dict(b or a, strategy='Q100' if b else 'CURRENT', dual_agreement=bool(a and b))
        events = []
        for t, c in candidates.items():
            last = self.s.get('last_entries', {}).get(c['strategy'], -1e15)
            if (self.s['position'] or blackout(t) or t-self.s['last_entry'] < 600 or
                    t-last < component(c)['cooldown_seconds']):
                continue
            c = dict(c, expires=t+1800, key=f'{VERSION}:{CONFIG_HASH}:{t}:{c["subtype"]}:{c["direction"]}',
                     initial_checked=False)
            self.s['pending'] = c
            events.append(self.event(c, 'SETUP', t))
        return events

    @staticmethod
    def event(item, kind, stamp, **values):
        return dict(event_key=item['key']+':'+kind, campaign_key=item['key'], event_type=kind,
                    event_time=iso(stamp), eligible_at=iso(stamp), **values)

    def tick(self, stamp, bid, ask, allow_entry=True):
        if not all(math.isfinite(x) for x in (stamp, bid, ask)) or bid <= 0 or ask < bid:
            raise ValueError('invalid quote')
        previous = self.s['last_tick']
        if previous is not None and stamp < previous:
            raise ValueError('out-of-order tick')
        self.s['last_tick'] = stamp
        events = []
        p = self.s['position']
        occupied = p is not None
        if p:
            d = p['direction']; price = bid if d == 1 else ask
            reason = ('DATA_GAP' if previous is not None and stamp-previous > 300 else
                      'DAY_CLOSE' if stamp >= p['flat_at'] else
                      'TIME_EXIT' if stamp-p['entered'] >= p.get('maximum_hold_minutes', 240)*60 else
                      'SL' if (price-p['sl'])*d <= 0 else
                      'TP2' if (price-p['tp'])*d >= 0 else None)
            if reason:
                fill = p['tp'] if reason == 'TP2' else price-d*COST['slippage']
                realized = ((fill-p['entry'])*d-COST['commission'])/p['risk']
                events.append(self.event(p, reason, stamp, price=fill, realized_r=realized))
                self.s['position'] = None
        pending = self.s['pending']
        if pending and (stamp >= pending['expires'] or blackout(stamp)):
            events.append(self.event(pending, 'EXPIRED', stamp))
            self.s['pending'] = None
        pending = self.s['pending']
        if not pending or occupied or self.s['position'] or not allow_entry or stamp < pending['time']:
            return events
        d = pending['direction']
        initial = not pending['initial_checked'] and 0 <= stamp-pending['time'] <= 5
        pending['initial_checked'] = True
        chase = max(0., (bid-pending['entry'])*d)
        entry_profile = Profile(**component(pending)['profile'])
        direct = initial and chase <= entry_profile.maximum_chase_atr*pending['atr']
        touched = ask <= pending['entry'] if d == 1 else bid >= pending['entry']
        if not direct and not touched:
            return events
        local = datetime.fromtimestamp(stamp, NY)
        row = dict(time=stamp, day=local.date().isoformat(), bid=(bid,)*4, ask=(ask,)*4)
        # Live uses the current executable quote, never a past minute's high/low.
        flat_at = scheduled_flat(stamp)
        if stamp >= flat_at-900:
            events.append(self.event(pending, 'REJECTED', stamp, reason='SESSION_END_NEAR'))
            self.s['pending'] = None
            return events
        p = make_position(pending, row, entry_profile, COST, deferred=not direct)
        self.s['pending'] = None
        if not p:
            events.append(self.event(pending, 'REJECTED', stamp, reason='PRICE_OR_COST_RISK'))
            return events
        p.update(key=pending['key'], grade=pending['grade'], flat_at=flat_at,
                 strategy=pending.get('strategy', 'CURRENT'), dual_agreement=pending.get('dual_agreement', False),
                 maximum_hold_minutes=entry_profile.maximum_hold_minutes)
        self.s['position'] = p
        self.s['last_entry'] = stamp
        self.s.setdefault('last_entries', {})[p['strategy']] = stamp
        events.append(self.event(p, 'PRIMARY_OPEN', stamp, position=deepcopy(p),
                                 confirmation_time=iso(pending['time']), bid=bid, ask=ask))
        return events


class IntradayRunner:
    def __init__(self, market_path, state_path, telegram=None):
        self.market_path = Path(market_path).resolve()
        self.state_path = Path(state_path).resolve()
        if self.market_path == self.state_path:
            raise ValueError('market and state must be separate')
        if telegram is None and not self.state_path.name.startswith('intraday-shadow'):
            raise ValueError('shadow requires an isolated intraday-shadow database')
        self.market = sqlite3.connect(self.market_path.as_uri()+'?mode=ro', uri=True, timeout=2)
        provider = self.market.execute("SELECT value FROM market_meta WHERE key='provider'").fetchone()
        if not provider or provider[0] != 'exness':
            self.market.close()
            raise ValueError('this deployment requires Exness canonical data')
        self.store = TradeStore(self.state_path)
        self.store.db.execute('PRAGMA busy_timeout=1000')
        self.telegram = telegram
        saved = self.store.load_engine_state('XAUUSD', 'M30-H1', VERSION)
        self.engine = IntradayEngine(saved['state'] if saved else None)
        self.errors = {}; self.timings = {}; self.last_error = None
        self.verify_position()

    def verify_position(self):
        rows = self.store.db.execute("SELECT campaign_key,engine_version FROM trades WHERE status='OPEN'").fetchall()
        p = self.engine.s['position']
        if len(rows) != int(p is not None) or p and (rows[0][0] != p['key'] or rows[0][1] != VERSION):
            raise ValueError('open trade/state mismatch or old engine still has open trades')

    def close(self):
        self.market.close(); self.store.close()

    def save(self):
        self.store.save_engine_state('XAUUSD', 'M30-H1', VERSION,
            self.engine.engines['m30'].s['last_close'], self.engine.snapshot())

    def initialize(self, now):
        if self.engine.engines['m30'].s['last_close']:
            return
        before = self.engine.snapshot()
        try:
            with self.store.transaction():
                warmup = {}
                for tf in ('h1', 'm30'):
                    rows = self.market.execute("SELECT timestamp,open,high,low,close,volume FROM bars "
                        "WHERE symbol='XAUUSD' AND timeframe=? AND is_closed=1 ORDER BY timestamp DESC LIMIT 1000", (tf,)).fetchall()
                    q = inspect_bars(rows, tf, now, min_closed_bars=1000)
                    if not q.ready:
                        raise ValueError(f'{tf} warmup: {q.status}')
                    for row in reversed(rows):
                        self.engine.bar(tf, Bar(*row), historical=True)
                    warmup[tf] = dict(count=len(rows), start=rows[-1][0], end=rows[0][0],
                        sha256=hashlib.sha256(json.dumps(rows).encode()).hexdigest())
                # Do not reconstruct live entries from historical OHLC or old quotes.
                self.engine.s['cursor'] = self.market.execute("SELECT COALESCE(MAX(id),0) FROM tick_events WHERE symbol='XAUUSD'").fetchone()[0]
                self.engine.s['warmup'] = warmup
                self.save()
        except Exception:
            self.engine = IntradayEngine(before)
            raise

    def advance(self, stamp, received):
        events = []
        for tf in ('h1', 'm30'):
            e = self.engine.engines[tf]
            start = e.s['last_close']
            cutoff = iso(math.floor(stamp/SECONDS[tf])*SECONDS[tf]-SECONDS[tf])
            rows = self.market.execute("SELECT timestamp,open,high,low,close,volume,received_at_utc "
                "FROM bars WHERE symbol='XAUUSD' AND timeframe=? AND timestamp>=? AND timestamp<=? "
                "AND is_closed=1 AND received_at_utc<=? ORDER BY timestamp LIMIT 100",
                (tf, start, cutoff, received)).fetchall()
            self.errors.pop(tf, None)
            for row in rows:
                at = utc_timestamp(row[0]); prior = utc_timestamp(e.s['last_close'])
                if at > prior and not interval_is_closed(prior, at):
                    self.errors[tf] = f'GAP after {prior.isoformat()}'
                    break
                q = inspect_bars([row[:6]], tf, datetime.fromtimestamp(stamp, timezone.utc), min_closed_bars=1)
                if not q.ready:
                    self.errors[tf] = q.status
                    break
                confirmed = at.timestamp()+SECONDS[tf]
                if tf == 'm30' and utc_timestamp(self.engine.engines['h1'].s['last_close']).timestamp() < math.floor(confirmed/3600)*3600:
                    self.errors[tf] = 'WAITING_FOR_CLOSED_H1'
                    break
                events.extend(self.engine.bar(tf, Bar(*row[:6])))
        return events

    def emit(self, events, received, now):
        for event in events:
            event.update(source_sha256=SOURCE_SHA256, config_hash=CONFIG_HASH, timeframe='m30',
                         received_at=received, decision_at=now.isoformat(), observation='ordered-exness-tick',
                         experimental=True)
            key = event['event_key']; kind = event['event_type']
            if not self.store.record_trade_event(event_key=key, event_type=kind, event_time=event['event_time'],
                    campaign_key=event['campaign_key'], payload=event, price=event.get('price')):
                continue
            if kind == 'PRIMARY_OPEN':
                p = event['position']
                metadata = dict(engine_version=VERSION, config_hash=CONFIG_HASH, campaign_key=p['key'],
                    family=p['family'], subtype=p['subtype'], grade=p['grade'], quality=p['quality'],
                    confirmation_time=event['confirmation_time'], bar_received_time=received,
                    signal_created_time=now.isoformat(), telegram_queued_time=now.isoformat(),
                    initial_sl=p['sl'], current_bid=event['bid'], current_ask=event['ask'],
                    spread=event['ask']-event['bid'])
                trade, created = self.store.create(event_key=key, timeframe='M30',
                    signal='MAIN BUY' if p['direction'] == 1 else 'MAIN SELL', entry=p['entry'], sl=p['sl'], tp=p['tp'],
                    at=utc_timestamp(event['event_time']), metadata=metadata)
                self.store.db.execute('UPDATE trade_events SET trade_id=? WHERE event_key=?', (trade.id, key))
                if created and self.telegram:
                    text = entry_message(trade)+'\nHỗ trợ: '+', '.join(p['supporting_models'])
                    text += '\nBộ tín hiệu: '+p['strategy']+(' · Hai bộ đồng thuận' if p.get('dual_agreement') else '')
                    text += '\nIntraday kết hợp thử nghiệm · dự phòng trượt 0,02.'
                    text += '\nKhông tự đặt lệnh. Hạn đóng: '+iso(p['flat_at'])+'; giữ tối đa '+str(p['maximum_hold_minutes'])+' phút.'
                    for chat in self.telegram.chat_ids:
                        if self.store.user_enabled(chat, 'XAUUSD', 'm30'):
                            number, _ = self.store.create_user_signal(chat, trade)
                            body = text.replace(f'#{trade.number} ', f'#{number} ', 1)
                            self.store.queue_notification('open:'+str(chat)+':'+key, body, chat,
                                expires_at=iso(utc_timestamp(event['eligible_at']).timestamp()+5), priority=0, event=event)
            elif kind in ('SL', 'TP2', 'TIME_EXIT', 'DAY_CLOSE', 'DATA_GAP'):
                row = self.store.db.execute("SELECT id,event_key FROM trades WHERE campaign_key=? AND engine_version=? AND status='OPEN'",
                                           (event['campaign_key'], VERSION)).fetchone()
                if row is None:
                    raise ValueError('exit has no open trade')
                self.store.apply_trade_event(row[0], event, event['price'])
                trade = self.store._trade(self.store.db.execute('SELECT * FROM trades WHERE id=?', (row[0],)).fetchone())
                self.store.update_user_status(trade.id, trade.status)
                if self.telegram:
                    for chat in self.telegram.chat_ids:
                        number = self.store.user_number(chat, trade.id)
                        if number is not None:
                            self.store.queue_notification('close:'+str(chat)+':'+key, update_message(trade, number), chat,
                                depends_on='open:'+str(chat)+':'+row[1], priority=0, event=event)

    def timing(self, name, milliseconds):
        self.timings.setdefault(name, []).append(milliseconds)
        del self.timings[name][:-10000]

    def step(self, now=None):
        now = now or datetime.now(timezone.utc)
        step_started = time.perf_counter()
        self.initialize(now)
        rows = read_after(self.market, 'XAUUSD', self.engine.s['cursor'], limit=500)
        self.last_error = None
        for tick_id, provider, received, bid, ask, origin in rows:
            before = self.engine.snapshot(); began = time.perf_counter()
            decision_now = now+timedelta(seconds=began-step_started)
            try:
                with self.store.transaction():
                    if not provider:
                        raise ValueError('provider timestamp missing')
                    stamp = utc_timestamp(provider).timestamp()
                    if stamp > decision_now.timestamp()+5:
                        raise ValueError('provider clock skew')
                    events = self.advance(stamp, received)
                    fresh = 0 <= decision_now.timestamp()-stamp <= 5 and 0 <= decision_now.timestamp()-utc_timestamp(received).timestamp() <= 5
                    events += self.engine.tick(stamp, bid, ask, allow_entry=fresh and not self.errors and market_is_open(utc_timestamp(provider)))
                    self.emit(events, received, decision_now)
                    self.engine.s['cursor'] = tick_id
                    self.save()
                self.timing('tick_processing_ms', (time.perf_counter()-began)*1000)
                self.timing('tick_receive_to_commit_ms', max(0, (decision_now-utc_timestamp(received)).total_seconds()*1000)+(time.perf_counter()-began)*1000)
            except Exception as exc:
                self.engine = IntradayEngine(before)
                self.last_error = f'{type(exc).__name__}: {exc}'
                break
        return self.health(now)

    def health(self, now):
        latest = self.market.execute("SELECT id,provider_time,received_at FROM tick_events WHERE symbol='XAUUSD' ORDER BY id DESC LIMIT 1").fetchone()
        age = max(0., now.timestamp()-utc_timestamp(latest[1] or latest[2]).timestamp()) if latest else None
        pending = self.market.execute("SELECT COUNT(*),MIN(received_at) FROM tick_events WHERE symbol='XAUUSD' AND id>?", (self.engine.s['cursor'],)).fetchone()
        frames = {}
        for tf, e in self.engine.engines.items():
            closed = utc_timestamp(e.s['last_close']).timestamp()
            expected = math.floor(now.timestamp()/SECONDS[tf])*SECONDS[tf]
            current = closed >= expected or interval_is_closed(utc_timestamp(e.s['last_close']), datetime.fromtimestamp(expected, timezone.utc))
            error = self.errors.get(tf) or (None if current else 'WAITING_FOR_CANONICAL_CLOSE')
            frames[tf] = dict(ready=not error, error=error, checkpoint=e.s['last_close'], role='entry' if tf == 'm30' else 'context')
        metrics = {}
        for name, values in self.timings.items():
            ordered = sorted(values); n = len(ordered)
            metrics[name] = dict(count=n, p95=ordered[math.ceil(.95*n)-1], p99=ordered[math.ceil(.99*n)-1], max=ordered[-1])
        p = self.engine.s['position']
        overdue = bool(p and now.timestamp() >= min(p['flat_at'], p['entered']+p.get('maximum_hold_minutes', 240)*60))
        ready = all(x['ready'] for x in frames.values()) and age is not None and age <= 5 and not pending[0] and not self.last_error and not overdue
        error = self.last_error or ('EXIT_DUE_WAITING_FOR_QUOTE' if overdue else
            'TICK_BACKLOG' if pending[0] else 'STALE_FEED' if age is None or age > 5 else None)
        return dict(ready=bool(ready), timeframes=frames, tick_age_sec=age, pending_ticks=pending[0],
            queue_age_sec=max(0, now.timestamp()-utc_timestamp(pending[1]).timestamp()) if pending[1] else 0,
            cursor=self.engine.s['cursor'], error=error, overdue_exit=overdue, latency=metrics,
            pending_setup=bool(self.engine.s['pending']), open_position=bool(p),
            position_component=p.get('strategy') if p else None, holiday_coverage_verified=False, warmup=self.engine.s.get('warmup'))


def run():
    import bot
    verify_source()
    live = os.environ.get('SIGNAL_ENGINE') == 'intraday-live'
    if bot.SYMBOL != 'XAUUSD' or os.environ.get('MARKET_SOURCE') != 'exness':
        raise SystemExit('intraday deployment is restricted to Exness XAUUSD')
    if live and (os.environ.get('INTRADAY_DEPLOY_CONFIG_HASH') != CONFIG_HASH or os.environ.get('DRY_RUN') != 'false'):
        raise SystemExit('explicit experimental deployment config hash required')
    path = os.environ.get('TRADES_DB') if live else os.environ['INTRADAY_SHADOW_DB']
    lock = open('/tmp/xauusd-bot.lock' if live else str(path)+'.lock', 'w')
    if not bot.fcntl:
        raise SystemExit('runtime requires Linux single-writer lock')
    bot.fcntl.flock(lock, bot.fcntl.LOCK_EX | bot.fcntl.LOCK_NB)
    telegram = bot.Telegram(os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID']) if live else None
    runner = IntradayRunner(os.environ['MARKET_DB'], path, telegram)
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if telegram:
        runner.store.ensure_users(telegram.chat_ids, 'XAUUSD')
        Thread(target=telegram.poll, args=(runner.store, stop), daemon=True).start()
        Thread(target=bot.notification_worker, args=(runner.store.path, telegram, stop), daemon=True).start()
    server = bot.start_health_server(); last_news = 0.
    try:
        while not stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                h = runner.step(now)
                if telegram and time.monotonic()-last_news >= 30:
                    bot.queue_news_alerts(runner.store, bot.NEWS_DB)
                    last_news = time.monotonic()
            except Exception as exc:
                h = dict(ready=False, error=f'{type(exc).__name__}: {exc}', timeframes={})
            bot.METRICS['heartbeats'].update(signal=now.isoformat(), tick=now.isoformat())
            workers = bot.worker_health(bot.METRICS, live)
            counts = runner.store.notification_counts()
            bot.METRICS.update(last_tick_age=h.get('tick_age_sec'), telegram_pending=counts.get('PENDING', 0),
                telegram_unknown=counts.get('UNKNOWN', 0), telegram_dead=counts.get('DEAD', 0))
            ready = h['ready'] and workers['alive'] and market_is_open(now)
            bot.STATUS.update(h, ready=bool(ready), alive=workers['alive'], ok=bool(ready), degraded=not ready,
                service='xauusd-bot', dry_run=not live, market_closed=not market_is_open(now), workers=workers['workers'],
                data_source='exness', config=CONFIG, config_hash=CONFIG_HASH, parity_approved=False,
                source_acceptance='experimental user-authorized; Pine parity and 48h shadow unverified',
                signal_engine=dict(mode='intraday-live' if live else 'intraday-shadow', version=VERSION,
                                   config_hash=CONFIG_HASH, parity_approved=False),
                metrics=bot.METRICS, updated_at=now.isoformat())
            stop.wait(.2)
    finally:
        stop.set(); server.shutdown(); runner.close(); lock.close()
