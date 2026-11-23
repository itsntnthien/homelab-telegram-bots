"""Non-destructive migration from mixed-format market bars to canonical UTC bars."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from market_quality import utc_timestamp


def migrate_market_db(path: str | Path) -> None:
    path = str(Path(path).expanduser().resolve())
    db = sqlite3.connect(path, timeout=20)
    db.execute("PRAGMA busy_timeout=20000")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS market_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        migrated = db.execute("SELECT value FROM market_meta WHERE key='canonical_bars_v1'").fetchone()
        if migrated:
            db.commit()
            return
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "bars" not in tables:
            db.execute("INSERT OR REPLACE INTO market_meta VALUES ('canonical_bars_v1', 'empty')")
            db.commit()
            return
        db.execute("ALTER TABLE bars RENAME TO bars_legacy")
        db.execute("""CREATE TABLE bars(
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            source TEXT DEFAULT 'legacy', received_at_utc TEXT,
            provider_time_utc TEXT, is_closed INTEGER DEFAULT 1,
            price_scale REAL, symbol_digits INTEGER, quality TEXT DEFAULT 'OK',
            PRIMARY KEY(symbol,timeframe,timestamp)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS market_anomalies(
            id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT, timestamp TEXT,
            kind TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL
        )""")
        rows = db.execute("SELECT symbol,timeframe,timestamp,open,high,low,close,volume FROM bars_legacy ORDER BY timestamp DESC").fetchall()
        chosen = {}
        duplicate_counts = {}
        for row in rows:
            try:
                timestamp = utc_timestamp(row[2]).isoformat()
            except (TypeError, ValueError):
                db.execute("INSERT INTO market_anomalies(symbol,timeframe,timestamp,kind,details,created_at) VALUES (?,?,?,?,?,?)",
                           (row[0], row[1], str(row[2]), "INVALID_TIMESTAMP", "unparseable", datetime.now(timezone.utc).isoformat()))
                continue
            key = (row[0], row[1], timestamp)
            if key in chosen:
                duplicate_counts[key] = duplicate_counts.get(key, 1) + 1
                continue
            chosen[key] = row
        for (symbol, timeframe, timestamp), row in chosen.items():
            db.execute("""INSERT INTO bars(
                symbol,timeframe,timestamp,open,high,low,close,volume,source,
                received_at_utc,provider_time_utc,is_closed,quality
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                symbol, timeframe, timestamp, float(row[3]), float(row[4]), float(row[5]), float(row[6]), float(row[7]),
                "legacy", datetime.now(timezone.utc).isoformat(), timestamp, 1, "OK"))
        for (symbol, timeframe, timestamp), count in duplicate_counts.items():
            db.execute("INSERT INTO market_anomalies(symbol,timeframe,timestamp,kind,details,created_at) VALUES (?,?,?,?,?,?)",
                       (symbol, timeframe, timestamp, "DUPLICATE_TIMESTAMP", str(count), datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT OR REPLACE INTO market_meta VALUES ('canonical_bars_v1', 'complete')")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
