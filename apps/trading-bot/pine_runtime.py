"""Isolated, read-only-market runner for the versioned Pine engine."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
import json
import os
import sqlite3
import time
import hashlib
from acceptance_run import register_run, release_hash

from market_quality import utc_timestamp, inspect_bars
from pine_config import PineConfig, SECONDS, ENGINE_VERSION, verify_source, require_parity_report, require_acceptance_report
from pine_engine import PineEngine
from signal_engine import Bar
from tick_journal import read_after
from trades import TradeStore, entry_message, info_message, update_message


class PineRunner:
    def __init__(self, market_path, state_path, config=None, telegram=None):
        self.market_path = Path(market_path).resolve()
        self.state_path = Path(state_path).resolve()
        if telegram is None and self.state_path.name != "pine-shadow.db":
            raise ValueError("shadow state must be named pine-shadow.db")
        if self.market_path == self.state_path:
            raise ValueError("market and engine state must be separate databases")
        self.config = config or PineConfig()
        self.market = sqlite3.connect(self.market_path.as_uri()+"?mode=ro", uri=True, timeout=2)
        self.store = TradeStore(self.state_path)
        self.telegram = telegram
        self.engines = {}
        self.statuses = {}
        self.timings = {"decision_ms": [], "tick_ms": []}
        self.started = time.monotonic()
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS shadow_samples(
            at TEXT PRIMARY KEY, market_open INTEGER NOT NULL, statuses_json TEXT NOT NULL,
            source_sha256 TEXT NOT NULL, config_hash TEXT NOT NULL)""")
        self.store.db.commit()
        self.run_id = os.environ.get("ACCEPTANCE_RUN_ID")
        columns = {r[1] for r in self.store.db.execute("PRAGMA table_info(shadow_samples)")}
        if "run_id" not in columns:
            self.store.db.execute("ALTER TABLE shadow_samples ADD COLUMN run_id TEXT")
        if self.run_id:
            calendar = Path(os.environ["MARKET_SCHEDULE_FILE"])
            register_run(self.store.db, self.run_id, self.config, release_hash(),
                         hashlib.sha256(calendar.read_bytes()).hexdigest(), "ctrader:XAUUSD")
        self.store.db.commit()
        self.last_sample = 0.
        for tf in SECONDS:
            saved = self.store.load_engine_state("XAUUSD", tf, ENGINE_VERSION)
            self.engines[tf] = PineEngine(tf, self.config, saved["state"] if saved else None)

    def close(self):
        self.market.close()
        self.store.close()

    def _bar(self, tf, timestamp):
        row = self.market.execute(
            "SELECT timestamp,open,high,low,close,volume FROM bars "
            "WHERE symbol='XAUUSD' AND timeframe=? AND timestamp=?", (tf, timestamp)).fetchone()
        return Bar(*row) if row else None

    def _initialize(self, tf, now):
        engine = self.engines[tf]
        if engine.s["last_close"]:
            return
        rows = self.market.execute(
            "SELECT timestamp,open,high,low,close,volume FROM bars "
            "WHERE symbol='XAUUSD' AND timeframe=? ORDER BY timestamp DESC LIMIT 2000", (tf,)).fetchall()
        if len(rows) < 2000:
            raise ValueError("warmup requires 2000 canonical closed bars")
        quality = inspect_bars(rows, tf, now, min_closed_bars=2000)
        if not quality.ready:
            raise ValueError(f"warmup data {quality.status}; gaps={quality.gap_count}")
        # Require the ordered journal before reconstructing any execution state.
        self.market.execute("SELECT id FROM tick_events LIMIT 1").fetchone()
        for row in reversed(rows):
            if utc_timestamp(row[0])+timedelta(seconds=SECONDS[tf]) > now:
                raise ValueError("forming bar in closed-bar store")
            events, _ = engine.close_bar(Bar(*row), historical=True)
            self._emit(tf, events, None, now, "warmup-ohlc")
        # Start before the first tick of the still-open execution bar.
        cursor = self.market.execute(
            "SELECT COALESCE(MAX(id),0) FROM tick_events WHERE symbol='XAUUSD' "
            "AND COALESCE(provider_time,received_at)<?", (engine.s["last_close"],)).fetchone()[0]
        first = self.market.execute("SELECT provider_time FROM tick_events WHERE symbol='XAUUSD' ORDER BY id LIMIT 1").fetchone()
        pending = engine.execution.state["pending"]
        if pending and (not first or not first[0] or utc_timestamp(first[0]) > utc_timestamp(engine.s["last_close"])):
            event = engine.execution._event(pending, "SETUP_CANCELLED", now.isoformat(), reason="NO_BAR_OPEN_TICK_COVERAGE")
            self._emit(tf, [event], None, now, "warmup-ohlc")
            engine.execution.state["pending"] = None
        self.store.set_checkpoint("pine:tick:"+tf, str(cursor))
        self._save(tf)

    def _save(self, tf):
        engine = self.engines[tf]
        self.store.save_engine_state("XAUUSD", tf, ENGINE_VERSION, engine.s["last_close"], engine.snapshot())

    def _emit(self, tf, events, received_at, now, observation):
        for raw in events:
            event = dict(raw)
            key = f"{ENGINE_VERSION}:{self.config.hash}:{event['event_key']}"
            event["event_key"] = key
            eligible = event.get("eligible_at") or event["event_time"]
            age = max(0., (now-utc_timestamp(eligible)).total_seconds())
            historical = observation in ("warmup-ohlc", "recovery-ohlc")
            event.update(acceptance_run_id=self.run_id, timeframe=tf, source_sha256=self.config.source_sha256, config_hash=self.config.hash,
                         received_at=received_at, decision_at=now.isoformat(),
                         observation=event.get("observation", observation),
                         delivery_status="SHADOW" if self.telegram is None else "HISTORICAL" if historical else "LATE" if age > 5 else "ELIGIBLE")
            if not self.store.record_trade_event(event_key=key, event_type=event["event_type"],
                                                  event_time=event["event_time"],
                                                  campaign_key=event.get("campaign_key"), payload=event):
                continue
            kind = event["event_type"]
            if kind == "PRIMARY_OPEN":
                if self.telegram is not None and (age > 5 or historical):
                    continue
                direction = event["direction"]
                metadata = dict(engine_version=ENGINE_VERSION, config_hash=self.config.hash,
                                campaign_key=event["campaign_key"], event_kind=kind,
                                family=event["family"], subtype=event["subtype"], grade=event["grade"],
                                quality=event["quality"], confirmation_time=event.get("confirmation_time"),
                                telegram_queued_time=now.isoformat(),
                                bar_received_time=received_at, signal_created_time=now.isoformat(),
                                initial_sl=self.config.price(event["sl_ticks"]))
                trade, created = self.store.create(
                    event_key=key, timeframe=tf, signal="MAIN BUY" if direction == 1 else "MAIN SELL",
                    entry=self.config.price(event["entry_ticks"]), sl=self.config.price(event["sl_ticks"]),
                    tp=self.config.price(event["tp_ticks"]), at=utc_timestamp(event["event_time"]), metadata=metadata)
                self.store.db.execute("UPDATE trade_events SET trade_id=? WHERE event_key=?", (trade.id, key))
                if self.telegram and created:
                    deadline = (utc_timestamp(eligible)+timedelta(seconds=5)).isoformat()
                    for chat in self.telegram.chat_ids:
                        if self.store.user_enabled(chat, "XAUUSD", tf):
                            number, _ = self.store.create_user_signal(chat, trade)
                            self.store.queue_notification(f"open:{chat}:{key}", ("[TEST · PYTHON/CTRADER]\n" if self.state_path.name == "tv-test.db" else "") + entry_message(trade, number),
                                                          chat, expires_at=deadline, priority=0, event=event)
            elif kind == "CLOUD_INFO":
                info, created = self.store.create_info(event_key=key, timeframe=tf.upper(),
                                                       signal="CLOUD BUY" if event["direction"] == 1 else "CLOUD SELL",
                                                       reference=self.config.price(event["reference_ticks"]),
                                                       at=utc_timestamp(event["event_time"]))
                if self.telegram and created and age <= 5 and not historical:
                    for chat in self.telegram.chat_ids:
                        if self.store.user_enabled(chat, "XAUUSD", tf):
                            self.store.queue_notification(f"cloud:{chat}:{key}", ("[TEST · PYTHON/CTRADER]\n" if self.state_path.name == "tv-test.db" else "") + info_message(info), chat,
                                                          expires_at=(now+timedelta(seconds=5)).isoformat(),
                                                          priority=5, event=event)
            elif kind in ("SL", "TP2", "SUPERSEDED"):
                campaign = event.get("campaign_key")
                if not campaign:
                    continue
                row = self.store.db.execute("SELECT id,event_key FROM trades WHERE campaign_key=? AND engine_version=? AND status='OPEN'",
                                            (campaign, ENGINE_VERSION)).fetchone()
                if row:
                    if kind == "SUPERSEDED":
                        self.store.db.execute("UPDATE trades SET status='SUPERSEDED',closed_at=? WHERE id=?",
                                              (event["event_time"], row[0]))
                    else:
                        self.store.apply_trade_event(row[0], event, self.config.price(event["price_ticks"]))
                    trade = self.store._trade(self.store.db.execute("SELECT * FROM trades WHERE id=?", (row[0],)).fetchone())
                    self.store.update_user_status(trade.id, trade.status)
                    if self.telegram and kind != "SUPERSEDED":
                        for chat in self.telegram.chat_ids:
                            number = self.store.user_number(chat, trade.id)
                            if number is not None:
                                self.store.queue_notification(f"close:{chat}:{key}", ("[TEST · PYTHON/CTRADER]\n" if self.state_path.name == "tv-test.db" else "") + update_message(trade, number), chat,
                                                              depends_on=f"open:{chat}:{row[1]}", priority=0, event=event)

    def _close_before(self, tf, timestamp, now):
        engine = self.engines[tf]
        boundary = int(utc_timestamp(timestamp).timestamp()) // SECONDS[tf] * SECONDS[tf]
        while engine.s["last_close"] and utc_timestamp(engine.s["last_close"]).timestamp() < boundary:
            start = engine.s["last_close"]
            bar = self._bar(tf, start)
            if bar is None:
                from market_quality import interval_is_closed
                end = datetime.fromtimestamp(boundary, timezone.utc)
                if interval_is_closed(utc_timestamp(start), end):
                    engine.s["last_close"] = end.isoformat()
                    break
                raise ValueError(f"waiting for canonical {tf} bar {start}")
            began = time.perf_counter()
            missed_open = engine.execution.state["bar"] != utc_timestamp(bar.timestamp).isoformat()
            events, _ = engine.close_bar(bar, historical=missed_open)
            received = self.market.execute("SELECT received_at_utc FROM bars WHERE symbol='XAUUSD' AND timeframe=? AND timestamp=?",
                                            (tf, start)).fetchone()[0]
            self._emit(tf, events, received, now, "recovery-ohlc" if missed_open else "closed-bar")
            self._timing("decision_ms", began)

    def _timing(self, name, began):
        self.timings[name].append((time.perf_counter()-began)*1000)
        del self.timings[name][:-10000]

    def step(self, now=None):
        now = now or datetime.now(timezone.utc)
        step_started = time.perf_counter()
        for tf in SECONDS:
            before = self.engines[tf].snapshot()
            try:
                with self.store.transaction():
                    self._initialize(tf, now)
                before = self.engines[tf].snapshot()
                cursor = int(self.store.checkpoint("pine:tick:"+tf) or 0)
                rows = read_after(self.market, "XAUUSD", cursor)
                for tick_id, provider, received, bid, ask, origin in rows:
                    began = time.perf_counter()
                    with self.store.transaction():
                        if provider is None:
                            raise ValueError("provider tick timestamp unavailable")
                        at = utc_timestamp(provider)
                        if at > now+timedelta(seconds=5):
                            raise ValueError("provider clock skew")
                        if at >= utc_timestamp(self.engines[tf].s["last_close"]):
                            self._close_before(tf, provider, now+timedelta(seconds=time.perf_counter()-step_started))
                            events = self.engines[tf].tick(provider, bid, ask)
                            for event in events:
                                event["eligible_at"] = provider
                            decision_at = now+timedelta(seconds=time.perf_counter()-step_started)
                            self._emit(tf, events, received, decision_at, "ordered-tick")
                        self.store.set_checkpoint("pine:tick:"+tf, str(tick_id))
                        self._save(tf)
                    self._timing("tick_ms", began)
                    committed_at = now+timedelta(seconds=time.perf_counter()-step_started)
                    self.timings.setdefault("tick_receive_to_commit_ms", []).append(
                        max(0., (committed_at-utc_timestamp(received)).total_seconds()*1000))
                    del self.timings["tick_receive_to_commit_ms"][:-10000]
                    before = self.engines[tf].snapshot()
                    cursor = tick_id
                latest = self.market.execute("SELECT id,provider_time,received_at FROM tick_events WHERE symbol='XAUUSD' ORDER BY id DESC LIMIT 1").fetchone()
                age = max(0., (now-utc_timestamp(latest[1] or latest[2])).total_seconds()) if latest else None
                pending = self.market.execute("SELECT COUNT(*),MIN(received_at) FROM tick_events WHERE symbol='XAUUSD' AND id>?", (cursor,)).fetchone()
                queue_age = max(0., (now-utc_timestamp(pending[1])).total_seconds()) if pending[1] else 0.
                ready = age is not None and age <= 5 and pending[0] == 0
                self.statuses[tf] = dict(ready=ready, error=None if ready else "STALE_FEED_OR_TICK_BACKLOG",
                                        tick_age_sec=age, queue_age_sec=queue_age, pending_ticks=pending[0],
                                        checkpoint=self.engines[tf].s["last_close"], cursor=cursor)
            except Exception as exc:
                self.engines[tf] = PineEngine(tf, self.config, before)
                self.statuses[tf] = dict(ready=False, error=f"{type(exc).__name__}: {exc}",
                                        checkpoint=self.engines[tf].s["last_close"],
                                        cursor=self.store.checkpoint("pine:tick:"+tf))
        return self.statuses

    def record_health(self, now=None):
        now = now or datetime.now(timezone.utc)
        from bot import market_open_now
        with self.store.transaction():
            self.store.db.execute("INSERT OR REPLACE INTO shadow_samples(at,market_open,statuses_json,source_sha256,config_hash,run_id) VALUES (?,?,?,?,?,?)",
                                  (now.isoformat(), int(market_open_now(now)),
                                   json.dumps(self.statuses, sort_keys=True),
                                   self.config.source_sha256, self.config.hash, self.run_id))

    def metrics(self):
        result = {}
        for name, samples in self.timings.items():
            values = sorted(samples)
            result[name] = dict(count=len(values), p95=values[max(0, math_index(.95, len(values))) ] if values else None,
                                p99=values[max(0, math_index(.99, len(values))) ] if values else None,
                                max=max(values) if values else None)
        return result


def math_index(percentile, count):
    import math
    return min(count-1, math.ceil(percentile*count)-1)


def check_legacy_flat(path):
    if not path:
        raise ValueError("PINE_LEGACY_TRADES_DB required to verify no old open campaign")
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        if db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]:
            raise ValueError("old engine still has open trades; cutover refused")


def run():
    import bot
    verify_source()
    mode = os.environ["SIGNAL_ENGINE"].lower()
    live = mode == "pine-live"
    test = mode == "pine-test"
    if "PINE_TICK_SIZE" not in os.environ:
        raise SystemExit("PINE_TICK_SIZE must be explicitly matched to the reference feed")
    config = PineConfig(tick_size=float(os.environ["PINE_TICK_SIZE"]))
    state_path = Path(os.environ.get("PINE_STATE_DB", "pine-live.db" if live else "tv-test.db" if test else "pine-shadow.db")).resolve()
    if not live and not test and state_path.name != "pine-shadow.db":
        raise SystemExit("shadow must use a separate pine-shadow.db")
    if os.environ.get("SYMBOL", "XAUUSD").upper() != "XAUUSD":
        raise SystemExit("Pine mode is restricted to XAUUSD")
    if live:
        if os.environ.get("DRY_RUN", "true").lower() != "false":
            raise SystemExit("pine-live requires explicit DRY_RUN=false")
        require_parity_report(os.environ.get("PINE_PARITY_REPORT"), config)
        require_acceptance_report(os.environ.get("PINE_ACCEPTANCE_REPORT"), config)
        if os.environ.get("PARITY_APPROVED", "false").lower() != "true":
            raise SystemExit("global parity approval required")
    market_path = os.environ.get("MARKET_DB", "market-openapi.db")
    telegram = bot.Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]) if live else None
    if test:
        test_chat = os.environ.get("TELEGRAM_TEST_CHAT_ID")
        prod_chats = os.environ.get("TELEGRAM_CHAT_IDS", os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
        if not test_chat or test_chat in prod_chats or state_path.name != "tv-test.db":
            raise SystemExit("pine-test requires isolated TEST destination and tv-test.db")
        telegram = bot.Telegram("", test_chat, chat_ids=[test_chat])
    stop = Event()
    lock = None
    runner = None
    try:
        if bot.fcntl:
            lock = open("/tmp/xauusd-bot.lock" if live else str(state_path)+".lock", "w")
            bot.fcntl.flock(lock, bot.fcntl.LOCK_EX | bot.fcntl.LOCK_NB)
        elif live:
            raise SystemExit("live requires the Linux single-writer lock")
        if live:
            check_legacy_flat(os.environ.get("PINE_LEGACY_TRADES_DB"))
        runner = PineRunner(market_path, state_path, config, telegram)
        if telegram:
            runner.store.ensure_users(telegram.chat_ids, "XAUUSD")
        if live:
            telegram.set_commands()
            Thread(target=telegram.poll, args=(runner.store, stop), daemon=True).start()
            Thread(target=bot.notification_worker, args=(runner.store.path, telegram, stop), daemon=True).start()
        os.environ.setdefault("BOT_HEALTH_PORT", "8766" if live else "18766")
        server = bot.start_health_server()
        while not stop.is_set():
            statuses = runner.step()
            if time.monotonic()-runner.last_sample >= 10:
                runner.record_health()
                runner.last_sample = time.monotonic()
            bot.METRICS["heartbeats"]["signal"] = datetime.now(timezone.utc).isoformat()
            bot.METRICS["heartbeats"]["tick"] = bot.METRICS["heartbeats"]["signal"]
            worker_health = bot.worker_health(bot.METRICS, live)
            ready = all(item["ready"] for item in statuses.values()) and worker_health["alive"]
            bot.STATUS.update(ok=worker_health["alive"], alive=worker_health["alive"], ready=ready,
                              degraded=not ready, service="xauusd-bot", dry_run=not live, test_mode=test, acceptance_run_id=runner.run_id,
                              market_closed=not bot.market_open_now(), timeframes=statuses,
                              engine=ENGINE_VERSION, config_hash=config.hash,
                              source_sha256=config.source_sha256, latency=runner.metrics(),
                              heartbeats=bot.METRICS["heartbeats"],
                              updated_at=datetime.now(timezone.utc).isoformat())
            stop.wait(.25)
    finally:
        stop.set()
        if "server" in locals():
            server.shutdown()
        if runner:
            runner.close()
        if lock:
            lock.close()
