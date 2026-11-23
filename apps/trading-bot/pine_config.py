"""Immutable contract for the user-supplied 2026-11-12 Pine source."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import hashlib
import json
import math

SOURCE_SHA256 = "00c44c20b26617a744a34cb533b675cb234b5efbbe0b5d34d3df5b59aa198823"
ENGINE_VERSION = "xau-pine-20261112"
SECONDS = {"m1": 60, "m3": 180, "m5": 300, "m15": 900, "m30": 1800, "h1": 3600}


@dataclass(frozen=True)
class PineConfig:
    tick_size: float = 0.01
    spread_price: float = 0.3
    minimum_rr_milli: int = 1200
    maximum_chase_atr: float = 0.25
    source_sha256: str = SOURCE_SHA256

    def __post_init__(self):
        if not math.isfinite(self.tick_size) or self.tick_size <= 0:
            raise ValueError("positive finite mintick required")
        if (self.spread_price, self.minimum_rr_milli, self.maximum_chase_atr,
                self.source_sha256) != (0.3, 1200, 0.25, SOURCE_SHA256):
            raise ValueError("configuration differs from the frozen Pine contract")

    def ticks(self, price):
        if price is None:
            return None
        if not math.isfinite(price):
            raise ValueError("non-finite price")
        return int((Decimal(str(price)) / Decimal(str(self.tick_size))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))

    def price(self, ticks):
        return None if ticks is None else float(Decimal(ticks) * Decimal(str(self.tick_size)))

    @property
    def spread_ticks(self):
        return self.ticks(self.spread_price)

    @property
    def hash(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()


def verify_source():
    path = Path(__file__).parent / "reference" / "XAU-SCALP-R1-user-20261112.pine"
    if hashlib.sha256(path.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Pine source fingerprint mismatch")


def require_parity_report(path, config):
    """An environment flag alone cannot authorize an unverified new contract."""
    if not path:
        raise ValueError("independent Pine parity report is required")
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if (report.get("source_sha256") != SOURCE_SHA256 or
            report.get("config_hash") != config.hash or
            report.get("reference_origin") != "tradingview-pine" or
            report.get("ok") is not True or not report.get("fixture_sha256") or
            set(report.get("timeframes", {})) != set(SECONDS) or
            not all(item.get("ok") is True and item.get("bar_count", 0) > 0 and item.get("tick_count", 0) > 0
                    and item.get("entry_count", 0) > 0 and item.get("negative_count", 0) > 0 and item.get("mismatch_count") == 0
                    and item.get("input_sha256") and item.get("expected_sha256")
                    for item in report["timeframes"].values())):
        raise ValueError("parity report does not cover this source, configuration and six timeframes")


def require_acceptance_report(path, config):
    if not path:
        raise ValueError("48-hour shadow and measured latency report required")
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    from acceptance_run import release_hash
    import math
    limits = {"decision_after_receive": 1000, "tick_entry_exit_after_receive": 500,
              "entry_receive_to_telegram_ack": 3000}
    if (not report.get("acceptance_run_id") or report.get("release_sha256") != release_hash()
            or report.get("ok") is not True or report.get("source_sha256") != SOURCE_SHA256
            or report.get("config_hash") != config.hash
            or not math.isfinite(report.get("ready_market_hours", 0)) or report.get("ready_market_hours", 0) < 48
            or report.get("degraded_samples", 1) or report.get("uncovered_seconds", 1)
            or not report.get("rollover") or set(report.get("sessions", [])) != {"ASIA", "EUROPE", "US"}):
        raise ValueError("shadow coverage incomplete")
    for name, limit in limits.items():
        item = report.get("latency_ms", {}).get(name, {})
        if item.get("count", 0) < 100 or item.get("p95") is None or not math.isfinite(item["p95"]) or not 0 <= item["p95"] <= limit:
            raise ValueError("latency acceptance incomplete: "+name)
