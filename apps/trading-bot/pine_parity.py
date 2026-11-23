"""Compare externally captured Pine telemetry, including every no-signal step.

This tool deliberately cannot generate an expected-output fixture.
"""
import argparse
import hashlib
import json
from pathlib import Path

from pine_config import PineConfig, SOURCE_SHA256, SECONDS
from pine_engine import PineEngine
from signal_engine import Bar


WINNER_FIELDS = ("slot", "direction", "family", "subtype", "quality", "grade",
                 "setup_quality", "entry_quality", "entry", "sl", "tp", "atr", "rr")
EVENT_FIELDS = ("event_type", "event_time", "direction", "family", "subtype", "grade",
                "quality", "entry_ticks", "sl_ticks", "tp_ticks", "rr_milli", "deferred",
                "reference_ticks", "price_ticks", "ambiguous")


def observation(engine, row):
    if row["kind"] == "bar":
        events, diag = engine.close_bar(Bar(**row["bar"]), historical=row.get("historical", False))
        if diag is None:
            raise ValueError("duplicate/out-of-order bar in parity input")
        winner = diag["winner"]
        state = {k: diag[k] for k in ("bar_time", "regime", "bias", "strength")}
        state["winner"] = {k: winner[k] for k in WINNER_FIELDS} if winner else None
    elif row["kind"] == "tick":
        events = engine.tick(row["timestamp"], row["bid"], row["ask"])
        state = None
    else:
        raise ValueError("parity input kind must be bar or tick")
    return {"state": state, "events": [{k: e[k] for k in EVENT_FIELDS if k in e} for e in events]}


def checked_jsonl(root, spec):
    path = (root/spec["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("fixture must stay inside its package")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise ValueError("fixture fingerprint mismatch")
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def report(manifest_path=None, config=None):
    config = config or PineConfig()
    result = dict(ok=False, source_sha256=SOURCE_SHA256, config_hash=config.hash,
                  reference_origin=None, timeframes={tf: dict(ok=False, status="BLOCKED",
                  reason="Independent Pine telemetry and identical input data not supplied") for tf in SECONDS})
    if not manifest_path:
        return result
    path = Path(manifest_path)
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if (manifest.get("reference_origin") != "tradingview-pine" or
            manifest.get("source_sha256") != SOURCE_SHA256 or
            manifest.get("config_hash") != config.hash or not manifest.get("provider_symbol") or
            manifest.get("input_mode") != "closed_bar_and_ordered_ticks" or
            set(manifest.get("timeframes", {})) != set(SECONDS)):
        raise ValueError("external Pine provenance, matching inputs and six timeframes are required")
    result.update(reference_origin=manifest["reference_origin"],
                  fixture_sha256=hashlib.sha256(raw).hexdigest(), provider_symbol=manifest["provider_symbol"])
    for tf, spec in manifest["timeframes"].items():
        inputs = checked_jsonl(path.parent, spec["inputs"])
        expected = checked_jsonl(path.parent, spec["expected"])
        if len(inputs) != len(expected) or not inputs:
            raise ValueError("one external expected record is required for every input step")
        engine = PineEngine(tf, config)
        mismatches, bar_count, tick_count, negatives, entries = [], 0, 0, 0, 0
        for index, (row, wanted) in enumerate(zip(inputs, expected)):
            actual = observation(engine, row)
            bar_count += row["kind"] == "bar"
            tick_count += row["kind"] == "tick"
            negatives += wanted.get("events") == [] and (wanted.get("state") or {}).get("winner") is None
            entries += sum(e.get("event_type") == "PRIMARY_OPEN" for e in wanted.get("events", []))
            if actual != wanted:
                mismatches.append(dict(step=index, expected=wanted, actual=actual))
        covered = bar_count > 0 and tick_count > 0 and negatives > 0 and entries > 0
        result["timeframes"][tf] = dict(ok=covered and not mismatches, status="PASS" if covered and not mismatches else "FAIL",
                                        bar_count=bar_count, tick_count=tick_count, negative_count=negatives, entry_count=entries,
                                        mismatch_count=len(mismatches), mismatches=mismatches[:20],
                                        input_sha256=spec["inputs"]["sha256"], expected_sha256=spec["expected"]["sha256"])
    result["ok"] = all(item["ok"] for item in result["timeframes"].values())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--tick-size", type=float, default=.01)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = report(args.manifest, PineConfig(tick_size=args.tick_size))
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
