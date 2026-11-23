from __future__ import annotations

import sqlite3
import os
import json
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import Event

OUTBOX_WAKE = Event()
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
VALID_SIGNALS = {"MAIN BUY", "MAIN SELL", "CLOUD BUY", "CLOUD SELL"}
VALID_TIMEFRAMES = {"M1", "M3", "M5", "M15", "M30", "H1"}
CLOUD_TRAIL = {"M1": (4.00, 2.00), "M3": (5.00, 2.50), "M5": (6.00, 3.00), "M15": (10.00, 7.50)}
USTEC_CLOUD_TRAIL = {"M1": (7.86, 4.72), "M3": (14.24, 8.55), "M5": (17.55, 10.53), "M15": (39.69, 23.82)}
MODEL_NAMES = {
    101: "Continuation Pullback — Hồi tiếp diễn",
    102: "Continuation Reclaim — Lấy lại basis",
    201: "Structure Breakout — Phá cấu trúc",
    202: "Range Breakout — Phá vùng",
    301: "Structure Reversal — Đảo chiều cấu trúc",
    302: "Exhaustion Reversal — Đảo chiều kiệt sức",
}


@dataclass(frozen=True)
class Trade:
    id: int
    event_key: str
    session: str
    number: int
    timeframe: str
    signal: str
    entry: float
    sl: float
    tp: float
    status: str
    trail_activation: float | None = None
    trail_distance: float | None = None
    confirmation_time: str | None = None
    bar_received_time: str | None = None
    signal_created_time: str | None = None
    telegram_queued_time: str | None = None
    planned_entry: float | None = None
    current_bid: float | None = None
    current_ask: float | None = None
    spread: float | None = None
    tick_age: float | None = None
    price_delta: float | None = None
    slippage_warning: bool = False
    engine_version: str = "LEGACY"
    config_hash: str | None = None
    event_kind: str = "PRIMARY_OPEN"
    campaign_key: str | None = None
    parent_trade_id: int | None = None
    family: int | None = None
    subtype: int | None = None
    grade: int | None = None
    quality: int | None = None
    initial_sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    remaining_share_milli: int = 1000
    tp1_hit_at: str | None = None
    protected_sl: float | None = None
    protected_at: str | None = None
    ambiguous_exit: bool = False
    exit_price: float | None = None
    realized_r: float | None = None
    strategy: str | None = None
    maximum_hold_minutes: int | None = None
    flat_at: float | None = None
    entry_atr: float | None = None


@dataclass(frozen=True)
class InfoSignal:
    id: int
    session: str
    number: int
    timeframe: str
    signal: str
    reference: float
    status: str = "INFO"


def session_key(at: datetime) -> str:
    """Trading day rolls over at 17:00 New York, including DST changes."""
    local = at.astimezone(NEW_YORK)
    day = local.date() if local.time() >= time(17) else local.date() - timedelta(days=1)
    return day.isoformat()


class TradeStore:
    def __init__(self, path: str | Path):
        self._transaction_sequence = 0
        self.path = str(Path(path).expanduser().resolve())
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA busy_timeout=10000;
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS counters (
                session TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL,
                number INTEGER NOT NULL,
                timeframe TEXT NOT NULL,
                signal TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                closed_at TEXT,
                trail_activation REAL,
                trail_distance REAL,
                confirmation_time TEXT,
                bar_received_time TEXT,
                signal_created_time TEXT,
                telegram_queued_time TEXT,
                planned_entry REAL,
                current_bid REAL,
                current_ask REAL,
                spread REAL,
                tick_age REAL,
                price_delta REAL,
                slippage_warning INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session, number)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                event_key TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                chat_id TEXT,
                state TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                failed_at TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chat_id, symbol, timeframe)
            );
            CREATE TABLE IF NOT EXISTS user_news_settings (
                chat_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS user_risk_settings (
                chat_id TEXT PRIMARY KEY,
                capital_usd TEXT,
                risk_mode TEXT CHECK(risk_mode IN ('percent','usd')),
                risk_value TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_risk_ui (
                chat_id TEXT PRIMARY KEY,
                account_type TEXT NOT NULL DEFAULT 'usd' CHECK(account_type IN ('usd','cent')),
                pending TEXT CHECK(pending IN ('capital','percent','money','delete'))
            );
            CREATE TABLE IF NOT EXISTS user_counters (
                chat_id TEXT NOT NULL, session TEXT NOT NULL, value INTEGER NOT NULL,
                PRIMARY KEY(chat_id, session)
            );
            CREATE TABLE IF NOT EXISTS user_signals (
                chat_id TEXT NOT NULL, trade_id INTEGER NOT NULL, event_key TEXT NOT NULL,
                session TEXT NOT NULL, number INTEGER NOT NULL, timeframe TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                PRIMARY KEY(chat_id, trade_id), UNIQUE(chat_id, event_key)
            );
            CREATE TABLE IF NOT EXISTS info_signals (
                id INTEGER PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL,
                number INTEGER NOT NULL,
                timeframe TEXT NOT NULL,
                signal TEXT NOT NULL,
                reference REAL NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session, number)
            );
            CREATE TABLE IF NOT EXISTS early_warnings (
                event_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                bar_start TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS engine_states (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                last_bar_timestamp TEXT,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(symbol,timeframe,engine_version)
            );
            CREATE TABLE IF NOT EXISTS trade_events (
                event_key TEXT PRIMARY KEY,
                trade_id INTEGER,
                campaign_key TEXT,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                price REAL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            );
            CREATE INDEX IF NOT EXISTS idx_trade_events_trade_time
                ON trade_events(trade_id,event_time);
            CREATE INDEX IF NOT EXISTS idx_trade_events_campaign_time
                ON trade_events(campaign_key,event_time);
            """
        )
        if "attempts" not in {row[1] for row in self.db.execute("PRAGMA table_info(notifications)")}:
            self.db.execute("ALTER TABLE notifications ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "chat_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(notifications)")}:
            self.db.execute("ALTER TABLE notifications ADD COLUMN chat_id TEXT")
        notification_columns = {row[1] for row in self.db.execute("PRAGMA table_info(notifications)")}
        if "next_attempt_at" not in notification_columns:
            self.db.execute("ALTER TABLE notifications ADD COLUMN next_attempt_at TEXT")
        if "failed_at" not in notification_columns:
            self.db.execute("ALTER TABLE notifications ADD COLUMN failed_at TEXT")
        if "last_error" not in notification_columns:
            self.db.execute("ALTER TABLE notifications ADD COLUMN last_error TEXT")
        for name, definition in (("message_ids", "TEXT"), ("requested_at", "TEXT"),
                                 ("expires_at", "TEXT"), ("depends_on", "TEXT"),
                                 ("priority", "INTEGER NOT NULL DEFAULT 10"),
                                 ("source_event_key", "TEXT"), ("eligible_at", "TEXT"),
                                 ("received_at", "TEXT"), ("decision_at", "TEXT"),
                                 ("delivery_json", "TEXT"), ("check_json", "TEXT"),
                                 ("reply_message_id", "INTEGER")):
            if name not in notification_columns:
                self.db.execute(f"ALTER TABLE notifications ADD COLUMN {name} {definition}")
        trade_columns = {row[1] for row in self.db.execute("PRAGMA table_info(trades)")}
        for name, definition in (
            ("exit_price", "REAL"), ("realized_r", "REAL"),
            ("strategy", "TEXT"), ("maximum_hold_minutes", "INTEGER"),
            ("flat_at", "REAL"), ("entry_atr", "REAL"),
            ("confirmation_time", "TEXT"), ("bar_received_time", "TEXT"),
            ("signal_created_time", "TEXT"), ("telegram_queued_time", "TEXT"),
            ("planned_entry", "REAL"), ("current_bid", "REAL"),
            ("current_ask", "REAL"), ("spread", "REAL"), ("tick_age", "REAL"),
            ("price_delta", "REAL"), ("slippage_warning", "INTEGER NOT NULL DEFAULT 0"),
            ("engine_version", "TEXT"), ("config_hash", "TEXT"),
            ("event_kind", "TEXT"), ("campaign_key", "TEXT"),
            ("parent_trade_id", "INTEGER"), ("family", "INTEGER"),
            ("subtype", "INTEGER"), ("grade", "INTEGER"), ("quality", "INTEGER"),
            ("initial_sl", "REAL"), ("tp1", "REAL"), ("tp2", "REAL"),
            ("remaining_share_milli", "INTEGER NOT NULL DEFAULT 1000"),
            ("tp1_hit_at", "TEXT"), ("protected_sl", "REAL"),
            ("protected_at", "TEXT"), ("ambiguous_exit", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in trade_columns:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {name} {definition}")
        # Normalize legacy rows created with mixed-case timeframe values.
        rows = self.db.execute(
            "SELECT chat_id,symbol,timeframe,MAX(enabled) FROM user_settings GROUP BY chat_id,symbol,LOWER(timeframe)"
        ).fetchall()
        if rows:
            with self.transaction():
                self.db.execute("DELETE FROM user_settings")
                self.db.executemany(
                    "INSERT INTO user_settings(chat_id,symbol,timeframe,enabled) VALUES (?,?,?,?)",
                    [(r[0], r[1], r[2].lower(), r[3]) for r in rows],
                )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(trades)")}
        if "trail_activation" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN trail_activation REAL")
        if "trail_distance" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN trail_distance REAL")

        self.db.commit()

    @contextmanager
    def transaction(self):
        # Savepoints let event, checkpoint and outbox callers share one commit.
        outermost = not self.db.in_transaction
        if outermost:
            # Reserve the writer before reads to avoid SQLITE_BUSY_SNAPSHOT on upgrade.
            self.db.execute("BEGIN IMMEDIATE")
        self._transaction_sequence += 1
        name = f"store_{self._transaction_sequence}"
        self.db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO {name}")
            self.db.execute(f"RELEASE {name}")
            if outermost:
                self.db.rollback()
            raise
        else:
            self.db.execute(f"RELEASE {name}")
            if outermost:
                self.db.commit()

    def close(self) -> None:
        self.db.close()

    def ensure_users(self, chat_ids: list[str], symbol: str) -> None:
        with self.transaction():
            for chat_id in chat_ids:
                self.db.execute('INSERT OR IGNORE INTO user_news_settings(chat_id,enabled) VALUES(?,1)',(str(chat_id),))
                self.db.executemany(
                    "INSERT OR IGNORE INTO user_settings(chat_id,symbol,timeframe,enabled) VALUES (?,?,?,1)",
                    [(str(chat_id), symbol, tf.lower()) for tf in VALID_TIMEFRAMES],
                )

    def user_enabled(self, chat_id: str, symbol: str, timeframe: str) -> bool:
        row = self.db.execute(
            "SELECT enabled FROM user_settings WHERE chat_id=? AND symbol=? AND timeframe=?",
            (str(chat_id), symbol, timeframe.lower()),
        ).fetchone()
        return row is None or bool(row[0])

    def set_user_timeframe(self, chat_id: str, symbol: str, timeframe: str, enabled: bool) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO user_settings VALUES (?,?,?,?)",
                (str(chat_id), symbol, timeframe.lower(), int(enabled)),
            )

    def user_timeframes(self, chat_id: str, symbol: str) -> set[str]:
        return {r[0] for r in self.db.execute(
            "SELECT timeframe FROM user_settings WHERE chat_id=? AND symbol=? AND enabled=1",
            (str(chat_id), symbol),
        )}

    def news_enabled(self, chat_id: str) -> bool:
        row = self.db.execute("SELECT enabled FROM user_news_settings WHERE chat_id=?", (str(chat_id),)).fetchone()
        return True if row is None else bool(row[0])

    def set_news_enabled(self, chat_id: str, enabled: bool) -> None:
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO user_news_settings(chat_id,enabled) VALUES(?,?)", (str(chat_id), int(enabled)))

    def create_user_signal(self, chat_id: str, trade: Trade) -> tuple[int, bool]:
        chat_id, session = str(chat_id), trade.session
        existing = self.db.execute(
            "SELECT number FROM user_signals WHERE chat_id=? AND trade_id=?", (chat_id, trade.id)
        ).fetchone()
        if existing:
            return existing[0], False
        with self.transaction():
            self.db.execute(
                "INSERT INTO user_counters(chat_id,session,value) VALUES (?,?,1) "
                "ON CONFLICT(chat_id,session) DO UPDATE SET value=value+1",
                (chat_id, session),
            )
            number = self.db.execute(
                "SELECT value FROM user_counters WHERE chat_id=? AND session=?", (chat_id, session)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO user_signals(chat_id,trade_id,event_key,session,number,timeframe) VALUES (?,?,?,?,?,?)",
                (chat_id, trade.id, trade.event_key, session, number, trade.timeframe),
            )
        return number, True

    def user_number(self, chat_id: str, trade_id: int) -> int | None:
        row = self.db.execute(
            "SELECT number FROM user_signals WHERE chat_id=? AND trade_id=?", (str(chat_id), trade_id)
        ).fetchone()
        return row[0] if row else None

    def update_user_status(self, trade_id: int, status: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE user_signals SET status=? WHERE trade_id=?", (status, trade_id))

    def create(
        self,
        *,
        event_key: str,
        timeframe: str,
        signal: str,
        entry: float,
        sl: float,
        tp: float,
        at: datetime | None = None,
        metadata: dict | None = None,
    ) -> tuple[Trade, bool]:
        at = at or datetime.now(timezone.utc)
        timeframe = timeframe.upper()
        signal = signal.upper()
        self._validate(timeframe, signal, entry, sl, tp)
        session = session_key(at)
        trail_activation = trail_distance = None
        if signal.startswith("CLOUD"):
            presets = USTEC_CLOUD_TRAIL if os.environ.get("SYMBOL", "XAUUSD").upper() == "USTEC" else CLOUD_TRAIL
            trail_activation, trail_distance = presets[timeframe]

        with self.transaction():
            existing = self.db.execute(
                "SELECT * FROM trades WHERE event_key = ?", (event_key,)
            ).fetchone()
            if existing:
                return self._trade(existing), False

            self.db.execute(
                """
                INSERT INTO counters(session, value) VALUES (?, 1)
                ON CONFLICT(session) DO UPDATE SET value = value + 1
                """,
                (session,),
            )
            number = self.db.execute(
                "SELECT value FROM counters WHERE session = ?", (session,)
            ).fetchone()[0]
            cursor = self.db.execute(
                """
                INSERT INTO trades(
                    event_key, session, number, timeframe, signal,
                    entry, sl, tp, created_at, trail_activation, trail_distance,
                    confirmation_time, bar_received_time, signal_created_time,
                    telegram_queued_time, planned_entry, current_bid, current_ask,
                    spread, tick_age, price_delta, slippage_warning,
                    engine_version, config_hash, event_kind, campaign_key, parent_trade_id,
                    family, subtype, grade, quality, initial_sl, tp1, tp2,
                    remaining_share_milli, ambiguous_exit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    session,
                    number,
                    timeframe,
                    signal,
                    entry,
                    sl,
                    tp,
                    at.isoformat(),
                    trail_activation,
                    trail_distance,
                    (metadata or {}).get("confirmation_time"),
                    (metadata or {}).get("bar_received_time"),
                    (metadata or {}).get("signal_created_time", at.isoformat()),
                    (metadata or {}).get("telegram_queued_time"),
                    (metadata or {}).get("planned_entry", entry),
                    (metadata or {}).get("current_bid"),
                    (metadata or {}).get("current_ask"),
                    (metadata or {}).get("spread"),
                    (metadata or {}).get("tick_age"),
                    (metadata or {}).get("price_delta"),
                    int(bool((metadata or {}).get("slippage_warning", False))),
                    (metadata or {}).get("engine_version"),
                    (metadata or {}).get("config_hash"),
                    (metadata or {}).get("event_kind", "PRIMARY_OPEN"),
                    (metadata or {}).get("campaign_key"),
                    (metadata or {}).get("parent_trade_id"),
                    (metadata or {}).get("family"), (metadata or {}).get("subtype"),
                    (metadata or {}).get("grade"), (metadata or {}).get("quality"),
                    (metadata or {}).get("initial_sl", sl), (metadata or {}).get("tp1"),
                    (metadata or {}).get("tp2", tp),
                    (metadata or {}).get("remaining_share_milli", 1000),
                    int(bool((metadata or {}).get("ambiguous_exit", False))),
                ),
            )
            for field in ('strategy', 'maximum_hold_minutes', 'flat_at', 'entry_atr'):
                if field in (metadata or {}):
                    self.db.execute(f"UPDATE trades SET {field}=? WHERE id=?", (metadata[field], cursor.lastrowid))
            row = self.db.execute(
                "SELECT * FROM trades WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._trade(row), True

    def update_price(
        self, price: float, at: datetime | None = None
    ) -> list[Trade]:
        return self.update_quote(price, price, at)

    def create_info(self, *, event_key: str, timeframe: str, signal: str,
                    reference: float, at: datetime | None = None) -> tuple[InfoSignal, bool]:
        at = at or datetime.now(timezone.utc)
        timeframe, signal = timeframe.upper(), signal.upper()
        if timeframe not in VALID_TIMEFRAMES or signal not in {"CLOUD BUY", "CLOUD SELL"}:
            raise ValueError("Unsupported informational signal")
        session = session_key(at)
        with self.transaction():
            existing = self.db.execute("SELECT * FROM info_signals WHERE event_key=?", (event_key,)).fetchone()
            if existing:
                return self._info(existing), False
            self.db.execute("""INSERT INTO counters(session, value) VALUES (?, 1)
                ON CONFLICT(session) DO UPDATE SET value=value+1""", (session,))
            number = self.db.execute("SELECT value FROM counters WHERE session=?", (session,)).fetchone()[0]
            cursor = self.db.execute("""INSERT INTO info_signals
                (event_key, session, number, timeframe, signal, reference, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_key, session, number, timeframe, signal, reference, at.isoformat()))
            row = self.db.execute("SELECT * FROM info_signals WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._info(row), True

    def update_quote(
        self, bid: float, ask: float, at: datetime | None = None
    ) -> list[Trade]:
        at = at or datetime.now(timezone.utc)
        closed: list[Trade] = []
        with self.transaction():
            rows = self.db.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id"
            ).fetchall()
            for row in rows:
                buy = row["signal"].endswith("BUY")
                price = bid if buy else ask
                stop_hit = (buy and price <= row["sl"]) or (not buy and price >= row["sl"])
                target_hit = (buy and price >= row["tp"]) or (not buy and price <= row["tp"])
                release1 = (row["engine_version"] if "engine_version" in row.keys() else None) == "xau-scalp-r1"
                if release1:
                    tp1 = row["tp1"]
                    tp1_hit = row["tp1_hit_at"] is not None
                    tp1_now = (tp1 is not None and not tp1_hit and
                               ((buy and price >= tp1) or (not buy and price <= tp1)))
                    if stop_hit:
                        status = "BE" if tp1_hit else "SL"
                    elif target_hit:
                        status = "TP"
                    else:
                        status = None
                    if status:
                        ambiguous = int(bool(stop_hit and target_hit))
                        self.db.execute(
                            "UPDATE trades SET status=?,closed_at=?,ambiguous_exit=? WHERE id=?",
                            (status, at.isoformat(), ambiguous, row["id"]),
                        )
                        self.db.execute(
                            "INSERT OR IGNORE INTO trade_events(event_key,trade_id,campaign_key,event_type,event_time,price,payload_json,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (f"trade:{row['id']}:{status.lower()}:{at.isoformat()}", row["id"],
                             row["campaign_key"], status, at.isoformat(), price,
                             json.dumps({"ambiguous": bool(ambiguous)}), at.isoformat()),
                        )
                    elif tp1_now:
                        self.db.execute(
                            "UPDATE trades SET tp1_hit_at=?,remaining_share_milli=500,protected_sl=entry,protected_at=?,sl=entry WHERE id=?",
                            (at.isoformat(), at.isoformat(), row["id"]),
                        )
                        for event_type in ("TP1", "STOP_PROTECTED"):
                            self.db.execute(
                                "INSERT OR IGNORE INTO trade_events(event_key,trade_id,campaign_key,event_type,event_time,price,payload_json,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (f"trade:{row['id']}:{event_type.lower()}", row["id"], row["campaign_key"],
                                 event_type, at.isoformat(), tp1 if event_type == "TP1" else row["entry"],
                                 "{}", at.isoformat()),
                            )
                    if status:
                        closed.append(self._trade(self.db.execute(
                            "SELECT * FROM trades WHERE id=?", (row["id"],)).fetchone()))
                    continue
                status = "SL" if stop_hit else "TP" if target_hit else None
                if status:
                    self.db.execute(
                        "UPDATE trades SET status = ?, closed_at = ? WHERE id = ?",
                        (status, at.isoformat(), row["id"]),
                    )
                elif row["trail_activation"] is not None:
                    profit = (price - row["entry"]) if buy else (row["entry"] - price)
                    if profit >= row["trail_activation"]:
                        candidate = price - row["trail_distance"] if buy else price + row["trail_distance"]
                        better = candidate > row["sl"] if buy else candidate < row["sl"]
                        if better:
                            self.db.execute("UPDATE trades SET sl=? WHERE id=?", (candidate, row["id"]))
                if status:
                    closed.append(
                        self._trade(
                            self.db.execute(
                                "SELECT * FROM trades WHERE id = ?", (row["id"],)
                            ).fetchone()
                        )
                    )
        return closed

    def queue_notification(self, event_key: str, body: str, chat_id: str | None = None,
                           *, expires_at=None, depends_on=None, priority=10, event=None, delivery=None) -> bool:
        event = event or {}
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO notifications(event_key,body,chat_id,created_at,expires_at,depends_on,priority,"
                "source_event_key,eligible_at,received_at,decision_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_key, body, str(chat_id) if chat_id is not None else None,
                 datetime.now(timezone.utc).isoformat(), expires_at, depends_on, priority,
                 event.get("event_key"), event.get("eligible_at"), event.get("received_at"), event.get("decision_at")),
            )
            if cursor.rowcount and delivery is not None:
                self.db.execute("UPDATE notifications SET delivery_json=? WHERE event_key=?", (json.dumps(delivery), event_key))
        OUTBOX_WAKE.set()
        return cursor.rowcount == 1

    def mark_early_warning(self, event_key: str, symbol: str, timeframe: str,
                           direction: str, bar_start: str) -> bool:
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO early_warnings(event_key,symbol,timeframe,direction,bar_start,created_at) VALUES (?,?,?,?,?,?)",
                (event_key, symbol, timeframe, direction, bar_start, datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def pending_notifications(self, chat_id=None) -> list[sqlite3.Row]:
        from delivery import reconcile_queue
        reconcile_queue(self)
        now = datetime.now(timezone.utc).isoformat()
        query = """SELECT * FROM notifications n WHERE state='PENDING' AND attempts < 3
            AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            AND (depends_on IS NULL OR EXISTS(SELECT 1 FROM notifications parent
                WHERE parent.event_key=n.depends_on AND parent.state='SENT'))"""
        args = [now]
        if chat_id is not None:
            query += " AND chat_id=?"; args.append(str(chat_id))
        return self.db.execute(query+" ORDER BY priority,created_at,event_key LIMIT 100",args).fetchall()

    def claim_notification(self, event_key):
        with self.transaction():
            return self.db.execute("UPDATE notifications SET state='SENDING',requested_at=? "
                                   "WHERE event_key=? AND state='PENDING' AND (expires_at IS NULL OR expires_at>?)",
                                   (datetime.now(timezone.utc).isoformat(), event_key,
                                    datetime.now(timezone.utc).isoformat())).rowcount == 1

    def recover_notifications(self):
        with self.transaction():
            self.db.execute("UPDATE notifications SET state='UNKNOWN',failed_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),last_error='worker interrupted before ACK persisted' "
                            "WHERE state='SENDING'")

    def oldest_pending_age(self):
        value = self.db.execute("SELECT MIN(created_at) FROM notifications WHERE state='PENDING'").fetchone()[0]
        return max(0., (datetime.now(timezone.utc)-datetime.fromisoformat(value)).total_seconds()) if value else 0.

    def notification_counts(self) -> dict[str, int]:
        return {row[0]: row[1] for row in self.db.execute(
            "SELECT state, COUNT(*) FROM notifications GROUP BY state"
        )}

    def session_summary(self, chat_id: str, session: str) -> tuple[int, float, int, int, int]:
        row = self.db.execute("""
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN t.status='TP' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN t.status='SL' THEN 1 ELSE 0 END),0)
            FROM user_signals u JOIN trades t ON t.id=u.trade_id
            WHERE u.chat_id=? AND u.session=? AND t.status<>'OPEN'
        """, (str(chat_id), session)).fetchone()
        pips = self.db.execute("""
            SELECT COALESCE(SUM(CASE WHEN t.exit_price IS NOT NULL THEN
                                     (t.exit_price-t.entry)*(CASE WHEN t.signal LIKE '%BUY' THEN 1 ELSE -1 END)/0.01
                                     WHEN t.status='TP' THEN ABS(t.tp-t.entry)/0.01
                                     WHEN t.status='SL' THEN -ABS(t.sl-t.entry)/0.01 ELSE 0 END),0)
            FROM user_signals u JOIN trades t ON t.id=u.trade_id
            WHERE u.chat_id=? AND u.session=? AND t.status<>'OPEN'
        """, (str(chat_id), session)).fetchone()[0]
        open_count = self.db.execute("""
            SELECT COUNT(*) FROM user_signals u JOIN trades t ON t.id=u.trade_id
            WHERE u.chat_id=? AND u.session=? AND t.status='OPEN'
        """, (str(chat_id), session)).fetchone()[0]
        return int(row[0]), float(pips), int(row[1]), int(row[2]), int(open_count)

    def notification_sent(self, event_key: str, message_ids=None) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE notifications SET state='SENT',sent_at=?,message_ids=?,error=NULL,last_error=NULL,next_attempt_at=NULL WHERE event_key=?",
                (datetime.now(timezone.utc).isoformat(), json.dumps(message_ids), event_key),
            )

    def notification_failed(self, event_key: str, error: str, *, permanent=False, retry_after=None) -> None:
        now = datetime.now(timezone.utc)
        exhausted = permanent or self._notification_attempts(event_key) + 1 >= 3
        delay = max(1., float(retry_after)) if retry_after is not None else 2 ** self._notification_attempts(event_key)
        with self.transaction():
            self.db.execute(
                "UPDATE notifications SET state=?,error=?,last_error=?,attempts=attempts+1,failed_at=?,next_attempt_at=? WHERE event_key=?",
                ("DEAD" if exhausted else "PENDING", error[:500], error[:500],
                 now.isoformat() if exhausted else None,
                 None if exhausted else (now + timedelta(seconds=delay)).isoformat(), event_key),
            )

    def notification_uncertain(self, event_key: str, error: str) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE notifications SET state='UNKNOWN', error=?, last_error=?, failed_at=? WHERE event_key=?",
                (error[:500], error[:500], datetime.now(timezone.utc).isoformat(), event_key),
            )

    def _notification_attempts(self, event_key: str) -> int:
        row = self.db.execute("SELECT attempts FROM notifications WHERE event_key=?", (event_key,)).fetchone()
        return int(row[0]) if row else 0

    def checkpoint(self, name: str) -> str | None:
        row = self.db.execute("SELECT value FROM checkpoints WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def set_checkpoint(self, name: str, value: str) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO checkpoints VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )

    def load_engine_state(self, symbol: str, timeframe: str, engine_version: str) -> dict | None:
        row = self.db.execute(
            "SELECT last_bar_timestamp,state_json FROM engine_states "
            "WHERE symbol=? AND timeframe=? AND engine_version=?",
            (symbol, timeframe.lower(), engine_version),
        ).fetchone()
        if row is None:
            return None
        return {"last_bar_timestamp": row[0], "state": json.loads(row[1])}

    def save_engine_state(self, symbol: str, timeframe: str, engine_version: str,
                          last_bar_timestamp: str | None, state: dict) -> None:
        payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            self.db.execute(
                "INSERT INTO engine_states(symbol,timeframe,engine_version,last_bar_timestamp,state_json,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(symbol,timeframe,engine_version) DO UPDATE SET "
                "last_bar_timestamp=excluded.last_bar_timestamp,state_json=excluded.state_json,updated_at=excluded.updated_at",
                (symbol, timeframe.lower(), engine_version, last_bar_timestamp, payload, now),
            )

    def record_trade_event(self, *, event_key: str, event_type: str, event_time: str,
                           trade_id: int | None = None, campaign_key: str | None = None,
                           price: float | None = None, payload: dict | None = None) -> bool:
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO trade_events(event_key,trade_id,campaign_key,event_type,event_time,price,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event_key, trade_id, campaign_key, event_type, event_time, price,
                 json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
                 datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def apply_trade_event(self, trade_id: int, event: dict, price: float | None = None) -> None:
        """Attach an engine lifecycle event and atomically project it onto its trade."""
        event_type = event["event_type"]
        at = event["event_time"]
        with self.transaction():
            self.db.execute("UPDATE trade_events SET trade_id=? WHERE event_key=?",
                            (trade_id, event["event_key"]))
            if event_type == "TP1":
                self.db.execute(
                    "UPDATE trades SET tp1_hit_at=?,remaining_share_milli=500 WHERE id=?",
                    (at, trade_id))
            elif event_type == "STOP_PROTECTED":
                self.db.execute(
                    "UPDATE trades SET sl=entry,protected_sl=entry,protected_at=? WHERE id=?",
                    (at, trade_id))
            elif event_type in {"SL", "BE", "TP2", "TIME_EXIT", "DAY_CLOSE", "DATA_GAP"}:
                status = "TP" if event_type == "TP2" else event_type
                self.db.execute(
                    "UPDATE trades SET status=?,closed_at=?,ambiguous_exit=?,exit_price=?,realized_r=? WHERE id=?",
                    (status, at, int(bool(event.get("ambiguous"))), price, event.get("realized_r"), trade_id))

    @staticmethod
    def _validate(
        timeframe: str, signal: str, entry: float, sl: float, tp: float
    ) -> None:
        if timeframe not in VALID_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        if signal not in VALID_SIGNALS:
            raise ValueError(f"Unsupported signal: {signal}")
        if signal.endswith("BUY") and not sl < entry < tp:
            raise ValueError("BUY requires SL < entry < TP")
        if signal.endswith("SELL") and not tp < entry < sl:
            raise ValueError("SELL requires TP < entry < SL")

    @staticmethod
    def _trade(row: sqlite3.Row) -> Trade:
        return Trade(
            id=row["id"],
            event_key=row["event_key"],
            session=row["session"],
            number=row["number"],
            timeframe=row["timeframe"],
            signal=row["signal"],
            entry=row["entry"],
            sl=row["sl"],
            tp=row["tp"],
            status=row["status"],
            trail_activation=row["trail_activation"],
            trail_distance=row["trail_distance"],
            confirmation_time=row["confirmation_time"] if "confirmation_time" in row.keys() else None,
            bar_received_time=row["bar_received_time"] if "bar_received_time" in row.keys() else None,
            signal_created_time=row["signal_created_time"] if "signal_created_time" in row.keys() else None,
            telegram_queued_time=row["telegram_queued_time"] if "telegram_queued_time" in row.keys() else None,
            planned_entry=row["planned_entry"] if "planned_entry" in row.keys() else None,
            current_bid=row["current_bid"] if "current_bid" in row.keys() else None,
            current_ask=row["current_ask"] if "current_ask" in row.keys() else None,
            spread=row["spread"] if "spread" in row.keys() else None,
            tick_age=row["tick_age"] if "tick_age" in row.keys() else None,
            price_delta=row["price_delta"] if "price_delta" in row.keys() else None,
            slippage_warning=bool(row["slippage_warning"]) if "slippage_warning" in row.keys() else False,
            engine_version=(row["engine_version"] if "engine_version" in row.keys() and row["engine_version"] else "LEGACY"),
            config_hash=row["config_hash"] if "config_hash" in row.keys() else None,
            event_kind=(row["event_kind"] if "event_kind" in row.keys() and row["event_kind"] else "PRIMARY_OPEN"),
            campaign_key=row["campaign_key"] if "campaign_key" in row.keys() else None,
            parent_trade_id=row["parent_trade_id"] if "parent_trade_id" in row.keys() else None,
            family=row["family"] if "family" in row.keys() else None,
            subtype=row["subtype"] if "subtype" in row.keys() else None,
            grade=row["grade"] if "grade" in row.keys() else None,
            quality=row["quality"] if "quality" in row.keys() else None,
            initial_sl=row["initial_sl"] if "initial_sl" in row.keys() else row["sl"],
            tp1=row["tp1"] if "tp1" in row.keys() else None,
            tp2=(row["tp2"] if "tp2" in row.keys() and row["tp2"] is not None else row["tp"]),
            remaining_share_milli=(row["remaining_share_milli"] if "remaining_share_milli" in row.keys() else 1000),
            tp1_hit_at=row["tp1_hit_at"] if "tp1_hit_at" in row.keys() else None,
            protected_sl=row["protected_sl"] if "protected_sl" in row.keys() else None,
            protected_at=row["protected_at"] if "protected_at" in row.keys() else None,
            ambiguous_exit=bool(row["ambiguous_exit"]) if "ambiguous_exit" in row.keys() else False,
            exit_price=row["exit_price"] if "exit_price" in row.keys() else None,
            realized_r=row["realized_r"] if "realized_r" in row.keys() else None,
            **{k:row[k] if k in row.keys() else None for k in ('strategy','maximum_hold_minutes','flat_at','entry_atr')},
        )

    @staticmethod
    def _info(row: sqlite3.Row) -> InfoSignal:
        return InfoSignal(row["id"], row["session"], row["number"], row["timeframe"],
                          row["signal"], row["reference"])


def model_message(trade: Trade) -> str:
    if os.environ.get('SYMBOL', 'XAUUSD').upper() != 'XAUUSD' or not trade.signal.startswith('MAIN'):
        return ''
    name = MODEL_NAMES.get(trade.subtype)
    if name and trade.family == trade.subtype // 100:
        return f"Mô hình kích hoạt: {name} ({trade.subtype})\n"
    return "Mô hình kích hoạt: chưa có metadata xác thực\n"


def entry_message(trade: Trade, number: int | None = None) -> str:
    number = trade.number if number is None else number
    digits = 3 if trade.engine_version.startswith('xau-intraday-') else 2
    if trade.engine_version.startswith('xau-intraday-'):
        from telegram_views import vn
        side = '🟢 BUY' if trade.signal.endswith('BUY') else '🔴 SELL'
        model = MODEL_NAMES.get(trade.subtype, 'Chưa xác định').split(' — ')[-1]
        return (f"{side} · XAUUSD · #{number}\n"
                f"{trade.strategy or 'Intraday'} · {trade.timeframe} · Q{trade.quality if trade.quality is not None else '—'}\n"
                f"{model}\n\n"
                f"Entry: {trade.entry:.3f}\nSL: {trade.sl:.3f}\nTP: {trade.tp:.3f}\n\n"
                f"Giữ tối đa: {trade.maximum_hold_minutes or '—'} phút\n"
                f"Tín hiệu: {vn(trade.signal_created_time)} (VN)\n"
                "Tham khảo · không tự đặt lệnh. Q không phải tỷ lệ thắng.")
    message = (
        f"#{number} {os.environ.get('SYMBOL', 'XAUUSD')}\n"
        f"Khung: {trade.timeframe}\n"
        f"Tín hiệu: {trade.signal}\n"
        f"{model_message(trade)}"
        f"Entry: {trade.entry:.{digits}f}\nSL: {trade.sl:.{digits}f}\nTP: {trade.tp:.{digits}f}"
    )
    if trade.current_bid is not None and trade.current_ask is not None:
        message += (f"\nGiá lúc tạo tín hiệu: {trade.current_bid:.3f} / {trade.current_ask:.3f}"
                    f"\nSpread: {(trade.spread or 0):.2f}"
                    f"\nĐộ lệch: {(trade.price_delta or 0):+.2f}")
    if trade.confirmation_time:
        message += f"\nXác nhận nến: {trade.confirmation_time}"
    if trade.signal_created_time:
        message += f"\nTạo tín hiệu lúc: {trade.signal_created_time}"
    if trade.slippage_warning:
        message += "\n⚠️ Giá đã chạy xa entry dự kiến. Signal vẫn được gửi theo cấu hình."
    if trade.signal.startswith("CLOUD"):
        presets = USTEC_CLOUD_TRAIL if os.environ.get("SYMBOL", "XAUUSD").upper() == "USTEC" else CLOUD_TRAIL
        activation, distance = presets[trade.timeframe]
        message += f"\nTrail: kích hoạt +{activation:.2f}, khoảng cách {distance:.2f}"
    return message


def update_message(trade: Trade, number: int | None = None) -> str:
    number = trade.number if number is None else number
    icon = "✅" if trade.status in ("TP", "TP2") else "❌"
    outcome = {'TIME_EXIT': 'Đóng theo giới hạn giữ lệnh'+(f' {trade.maximum_hold_minutes} phút' if trade.maximum_hold_minutes else ''), 'DAY_CLOSE': 'Đóng trước rollover',
               'DATA_GAP': 'Đóng tại giá đầu tiên sau gián đoạn dữ liệu'}.get(trade.status, f'{icon} {trade.status} HIT')
    if trade.exit_price is not None:
        outcome += f'\nGiá đóng tham chiếu: {trade.exit_price:.3f}'
    if trade.realized_r is not None:
        outcome += f'\nKết quả mô phỏng: {trade.realized_r:+.3f}R'
    return (
        f"{icon if trade.status in ('TP','TP2','SL') else 'ℹ️'} ĐÓNG TÍN HIỆU · #{number} {os.environ.get('SYMBOL', 'XAUUSD')}\n"
        f"{trade.signal} · {trade.strategy or 'Không xác định'} · {trade.timeframe}\n"
        f"{model_message(trade)}\n"
        f"KẾT QUẢ MÔ PHỎNG\n{outcome}\nKhông phải xác nhận đóng lệnh trên tài khoản broker."
    )


def info_message(signal: InfoSignal) -> str:
    return (
        f"#{signal.number} {os.environ.get('SYMBOL', 'XAUUSD')}\n"
        f"Khung: {signal.timeframe}\n"
        f"Tín hiệu: {signal.signal}\n"
        f"Giá tham chiếu: {signal.reference:.2f}\n"
        "Tín hiệu hướng - không có SL/TP"
    )
