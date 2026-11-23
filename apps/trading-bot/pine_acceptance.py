"""Report observed shadow coverage and latency; missing evidence never passes."""
import argparse
import json
import math
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from market_quality import utc_timestamp
from pine_config import PineConfig, SECONDS


def distribution(values):
    values = sorted(values)
    return dict(count=len(values), p95=values[math.ceil(len(values)*.95)-1] if values else None,
                p99=values[math.ceil(len(values)*.99)-1] if values else None,
                max=values[-1] if values else None)


def report(path, config=None, run_id=None):
    config = config or PineConfig()
    result = dict(ok=False, source_sha256=config.source_sha256, config_hash=config.hash,
                  acceptance_run_id=run_id, active_market_hours=0, ready_market_hours=0, health_samples=0,
                  degraded_samples=0, uncovered_seconds=0, sessions=[], rollover=False,
                  latency_ms={}, telegram_states={}, blockers=[])
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not run_id:
            result["blockers"].append("Acceptance run identity required")
            return result
        run = db.execute("SELECT source_sha256,config_hash,release_sha256,calendar_sha256,provider,started_at FROM acceptance_runs WHERE run_id=?", (run_id,)).fetchone() if "acceptance_runs" in tables else None
        if not run:
            result["blockers"].append("Unknown acceptance run")
            return result
        if tuple(run[:2]) != (config.source_sha256, config.hash):
            result["blockers"].append("Run source/config mismatch")
            return result
        result.update(release_sha256=run[2],calendar_sha256=run[3],provider=run[4],started_at=run[5])
        samples = db.execute("SELECT at,market_open,statuses_json FROM shadow_samples WHERE source_sha256=? AND config_hash=? AND run_id=? ORDER BY at",
                             (config.source_sha256, config.hash, run_id)).fetchall() if "shadow_samples" in tables else []
        sessions = set()
        for i, (stamp, opened, statuses) in enumerate(samples):
            at = utc_timestamp(stamp)
            states = json.loads(statuses)
            ready = set(states) == set(SECONDS) and all(s.get("ready") for s in states.values())
            result["degraded_samples"] += bool(opened and not ready)
            if opened:
                sessions.add("ASIA" if at.hour < 7 else "EUROPE" if at.hour < 13 else "US")
            if i:
                previous = utc_timestamp(samples[i-1][0])
                delta = (at-previous).total_seconds()
                if delta > 30:
                    result["uncovered_seconds"] += delta
                    continue
                if opened and samples[i-1][1]:
                    result["active_market_hours"] += delta/3600
                    prior_states = json.loads(samples[i-1][2])
                    if ready and set(prior_states) == set(SECONDS) and all(s.get("ready") for s in prior_states.values()):
                        result["ready_market_hours"] += delta/3600
                local = at.astimezone(ZoneInfo("America/New_York"))
                prior_local = previous.astimezone(ZoneInfo("America/New_York"))
                if prior_local.hour == 17 and local.hour == 18:
                    result["rollover"] = True
        result.update(health_samples=len(samples), sessions=sorted(sessions))
        latencies = {"decision_after_receive": [], "tick_entry_exit_after_receive": [], "entry_receive_to_telegram_ack": []}
        per_frame = {tf: {name: [] for name in latencies} for tf in SECONDS}
        incidents = {"historical_events_excluded": 0, "invalid_timing": 0}
        if "trade_events" in tables:
            for (raw,) in db.execute("SELECT payload_json FROM trade_events"):
                event = json.loads(raw)
                if event.get("config_hash") != config.hash or event.get("source_sha256") != config.source_sha256 or event.get("acceptance_run_id") != run_id:
                    continue
                if event.get("observation") in ("warmup-ohlc", "recovery-ohlc", "ohlc-stop-first"):
                    incidents["historical_events_excluded"] += 1
                    continue
                if event.get("received_at") and event.get("decision_at"):
                    ms = (utc_timestamp(event["decision_at"])-utc_timestamp(event["received_at"])).total_seconds()*1000
                    if ms < 0:
                        incidents["invalid_timing"] += 1
                        continue
                    latencies["decision_after_receive"].append(ms)
                    tf = event.get("timeframe")
                    if tf in per_frame:
                        per_frame[tf]["decision_after_receive"].append(ms)
                    if event["event_type"] in ("PRIMARY_OPEN", "SL", "TP2"):
                        latencies["tick_entry_exit_after_receive"].append(ms)
                        if tf in per_frame:
                            per_frame[tf]["tick_entry_exit_after_receive"].append(ms)
        if "notifications" in tables:
            result["telegram_states_all_runs"] = dict(db.execute("SELECT state,COUNT(*) FROM notifications GROUP BY state"))
            columns = {r[1] for r in db.execute("PRAGMA table_info(notifications)")}
            if {"source_event_key", "received_at", "message_ids"} <= columns:
                for received, sent, raw in db.execute("SELECT n.received_at,n.sent_at,e.payload_json FROM notifications n JOIN trade_events e ON n.source_event_key=e.event_key WHERE n.state='SENT' AND n.message_ids IS NOT NULL AND n.received_at IS NOT NULL AND e.event_type='PRIMARY_OPEN'"):
                    event = json.loads(raw)
                    if event.get("acceptance_run_id") != run_id or event.get("config_hash") != config.hash or event.get("source_sha256") != config.source_sha256:
                        continue
                    ms = (utc_timestamp(sent)-utc_timestamp(received)).total_seconds()*1000
                    if ms >= 0:
                        latencies["entry_receive_to_telegram_ack"].append(ms)
                        if event.get("timeframe") in per_frame:
                            per_frame[event["timeframe"]]["entry_receive_to_telegram_ack"].append(ms)
                    else:
                        incidents["invalid_timing"] += 1
        result["latency_ms"] = {k: distribution(v) for k, v in latencies.items()}
        result["latency_by_timeframe_ms"] = {tf: {k: distribution(v) for k,v in metrics.items()} for tf,metrics in per_frame.items()}
        result["incidents"] = incidents
        if result["ready_market_hours"] < 48:
            result["blockers"].append("Fewer than 48 verified ready market hours")
        if result["degraded_samples"] or result["uncovered_seconds"]:
            result["blockers"].append("Degraded periods or health collection gaps require investigation")
        if len(sessions) != 3 or not result["rollover"]:
            result["blockers"].append("Asia/Europe/US and rollover coverage incomplete")
        for key, limit in (("decision_after_receive", 1000), ("tick_entry_exit_after_receive", 500), ("entry_receive_to_telegram_ack", 3000)):
            stats = result["latency_ms"][key]
            if stats["count"] < 100 or stats["p95"] > limit:
                result["blockers"].append(key+": missing observations or p95 above target")
        if incidents["invalid_timing"]:
            result["blockers"].append("Invalid timestamps in latency evidence")
        result["ok"] = not result["blockers"]
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--tick-size", type=float, default=.01)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = report(args.database, PineConfig(tick_size=args.tick_size), args.run_id)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
