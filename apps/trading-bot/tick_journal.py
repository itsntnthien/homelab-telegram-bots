"""Durable ordered market observations; readers checkpoint only after state commits."""
import math
from datetime import datetime, timedelta, timezone
from market_quality import utc_timestamp


def install(db):
    db.execute("""CREATE TABLE IF NOT EXISTS tick_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        provider_time TEXT, received_at TEXT NOT NULL,
        bid REAL NOT NULL, ask REAL NOT NULL, timestamp_origin TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS tick_events_symbol_id ON tick_events(symbol,id)")


def append(db, symbol, bid, ask, received_at, provider_time=None):
    if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
        raise ValueError("invalid bid/ask")
    received = utc_timestamp(received_at)
    provider = utc_timestamp(provider_time) if provider_time else None
    if provider and provider > received + timedelta(seconds=5):
        raise ValueError("provider clock is in the future")
    if provider:
        last = db.execute("SELECT provider_time FROM tick_events WHERE symbol=? AND provider_time IS NOT NULL ORDER BY id DESC LIMIT 1",
                          (symbol,)).fetchone()
        if last and provider < utc_timestamp(last[0]):
            return None
    return db.execute(
        "INSERT INTO tick_events(symbol,provider_time,received_at,bid,ask,timestamp_origin) VALUES (?,?,?,?,?,?)",
        (symbol, provider.isoformat() if provider else None, received.isoformat(), bid, ask,
         "provider" if provider else "receive_fallback")).lastrowid


def read_after(db, symbol, cursor, limit=1000):
    return db.execute(
        "SELECT id,provider_time,received_at,bid,ask,timestamp_origin FROM tick_events "
        "WHERE symbol=? AND id>? ORDER BY id LIMIT ?", (symbol, cursor, limit)).fetchall()


def history_request(latest_timestamp, timeframe_seconds, now, warmed):
    """Full initial/reconnect history; three-bar overlap for ordinary synchronization."""
    now = utc_timestamp(now)
    if not warmed or latest_timestamp is None:
        return 2001, int((now - timedelta(seconds=3000 * timeframe_seconds)).timestamp() * 1000)
    latest = utc_timestamp(latest_timestamp)
    start = latest - timedelta(seconds=2 * timeframe_seconds)
    count = max(4, math.ceil((now - start).total_seconds() / timeframe_seconds) + 1)
    if count > 2001:
        return history_request(None, timeframe_seconds, now, False)
    return count, int(start.timestamp() * 1000)
