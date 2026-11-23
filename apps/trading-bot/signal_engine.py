import math
import sqlite3
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


REGIME_RANGE_FORMING = 1
REGIME_RANGE_ACTIVE = 2
REGIME_RANGE_PRESSURE_UP = 3
REGIME_RANGE_PRESSURE_DOWN = 4
REGIME_TRANSITION = 5
REGIME_BREAKOUT_CANDIDATE = 6
REGIME_UPTREND = 7
REGIME_DOWNTREND = 8
BIAS_BEARISH = -1
BIAS_NEUTRAL = 0
BIAS_BULLISH = 1
DIR_BEARISH = -1
DIR_NEUTRAL = 0
DIR_BULLISH = 1
SE_CHOCH_BEARISH = -2
SE_BOS_BEARISH = -1
SE_NONE = 0
SE_BOS_BULLISH = 1
SE_CHOCH_BULLISH = 2
MF_CONTINUATION = 1
MF_BREAKOUT = 2
MF_REVERSAL = 3
MST_CONT_PULLBACK = 101
MST_CONT_RECLAIM = 102
MST_BREAK_STRUCTURE = 201
MST_BREAK_RANGE = 202
MST_REV_STRUCTURE = 301
MST_REV_EXHAUSTION = 302
CLOUD_INTERACTION_ARMED = 0
CLOUD_INTERACTION_LOCKED = 4
CLOUD_SETUP_NONE = 0
CLOUD_SETUP_CONTINUATION = 1
CLOUD_SETUP_RANGE_REJECTION = 2
CLOUD_SETUP_BREAKOUT = 3
CLOUD_FAMILY_POLARITY = 1
CLOUD_FAMILY_UPPER = 2
CLOUD_FAMILY_LOWER = 3
POLARITY_NEUTRAL = 0
POLARITY_BULL = 1
POLARITY_BEAR = -1
POLARITY_TRANSITION = 2

TIMEFRAME_CONTEXT = {
    "m1": (1, 5, 15),
    "m3": (3, 15, 60),
    "m5": (5, 15, 60),
    "m15": (15, 60, 240),
    "m30": (30, 240, 1440),
    "h1": (60, 240, 1440),
}

# Pine C16C-01 compatibility defaults. Keep the source fingerprint visible so
# replay reports can distinguish this release from older signal calculations.
PINE_CORE_VERSION = 1603001
PINE_CONFIG_FINGERPRINT = 8211403
PINE_SPREAD_PRICE = 0.0
PINE_MINIMUM_RR_MILLI = 1250
PINE_MAXIMUM_CHASE_ATR = 0.25
PINE_EMA200_LENGTH = 200
PINE_RSI_LENGTH = 14
RELEASE_ENGINE_VERSION = "xau-scalp-r1"
RELEASE_FEATURES = {
    "acceptance_rejection": True,
    "cluster_throttle": True,
    "dow": True,
    "execute_confirmed_next_bar": True,
    "live_shadow": False,
    "trend_dca": False,
    "two_stage_exit": False,
    "wyckoff": True,
}
RELEASE_CONFIG_HASH = hashlib.sha256(json.dumps({
    "core": PINE_CORE_VERSION,
    "fingerprint": PINE_CONFIG_FINGERPRINT,
    "spread_price": PINE_SPREAD_PRICE,
    "minimum_rr_milli": PINE_MINIMUM_RR_MILLI,
    "maximum_chase_atr": PINE_MAXIMUM_CHASE_ATR,
    "features": RELEASE_FEATURES,
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pine_timeframe_family_authorized(timeframe, family):
    timeframe = str(timeframe).lower()
    if timeframe in ("m1", "m5"):
        return family != MF_REVERSAL
    if timeframe == "m15":
        return family == MF_REVERSAL
    return timeframe in ("m3", "m30", "h1") and family in (MF_CONTINUATION, MF_BREAKOUT, MF_REVERSAL)


@dataclass(frozen=True)
class PolicyAdjustment:
    adjustment: int
    ema_adjustment: int
    rsi_adjustment: int
    veto: bool


def wilder_rsi(values, period=14):
    """Return Pine-style Wilder RSI values, with None during warmup."""
    out = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains = [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value():
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        if avg_gain == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    out[period] = value()
    for i in range(period + 1, len(values)):
        gain, loss = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = value()
    return out


def ema_rsi_policy(direction, close, ema200, atr_value, ema_slope_atr,
                   rsi_value, rsi_slope, family, ema_policy="Soft", rsi_policy="Soft"):
    """Apply the Pine default Soft EMA200/RSI policy to a candidate."""
    ema_adjustment = rsi_adjustment = 0
    veto = False
    valid_ema = all(value is not None for value in (close, ema200, atr_value, ema_slope_atr)) and atr_value > 0
    distance = (close - ema200) / atr_value if valid_ema else None
    ema_aligned = valid_ema and ((direction == DIR_BULLISH and distance >= 0.15 and ema_slope_atr >= 0.05) or
                                 (direction == DIR_BEARISH and distance <= -0.15 and ema_slope_atr <= -0.05))
    ema_opposed = valid_ema and ((direction == DIR_BULLISH and distance <= -0.15) or
                                 (direction == DIR_BEARISH and distance >= 0.15))
    if family != MF_REVERSAL and valid_ema:
        ema_adjustment = 8 if ema_aligned else -8 if ema_opposed else 0
        if ema_policy == "Hard" and ema_opposed:
            veto = True
    valid_rsi = rsi_value is not None and rsi_slope is not None
    momentum_aligned = valid_rsi and ((direction == DIR_BULLISH and rsi_value >= 52 and rsi_slope >= 0) or
                                      (direction == DIR_BEARISH and rsi_value <= 48 and rsi_slope <= 0))
    momentum_opposed = valid_rsi and ((direction == DIR_BULLISH and rsi_value <= 48 and rsi_slope < 0) or
                                      (direction == DIR_BEARISH and rsi_value >= 52 and rsi_slope > 0))
    reversal_aligned = valid_rsi and ((direction == DIR_BULLISH and rsi_value <= 30) or
                                      (direction == DIR_BEARISH and rsi_value >= 70))
    if family == MF_REVERSAL:
        rsi_adjustment = 6 if reversal_aligned else 0
    else:
        rsi_adjustment = 6 if momentum_aligned else -6 if momentum_opposed else 0
    if rsi_policy == "Hard" and momentum_opposed and family != MF_REVERSAL:
        veto = True
    return PolicyAdjustment(ema_adjustment + rsi_adjustment, ema_adjustment, rsi_adjustment, veto)


def vsa_adjustment(direction, vw_direction, valid_volume, low_volume,
                   breakout, continuation, reversal, fresh_event):
    """Bounded VSA + Wyckoff score adjustment from the Pine release."""
    if not valid_volume:
        return 0
    vsa = max(-10, min(10, 10 * direction * vw_direction - (5 if breakout and low_volume else 0)))
    wyckoff = 0 if continuation else direction * vw_direction * (10 if reversal and fresh_event else 5)
    return max(-15, min(15, vsa + wyckoff))


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: str
    ema_fast: float
    ema_slow: float
    atr: float
    range_high: float
    range_low: float
    normalized_separation: float
    regime: int
    break_direction: int
    warmup_valid: bool
    cloud_core: float | None
    cloud_lower: float | None
    cloud_upper: float | None
    swing_high_ticks: int | None
    swing_high_source_timestamp: str | None
    swing_high_confirmation_timestamp: str | None
    swing_low_ticks: int | None
    swing_low_source_timestamp: str | None
    swing_low_confirmation_timestamp: str | None
    bollinger_basis: float | None
    bollinger_upper: float | None
    bollinger_lower: float | None
    range_pressure: float
    ema200: float | None = None
    ema200_slope_atr: float | None = None
    rsi: float | None = None
    rsi_slope: float | None = None
    vsa_valid_volume: bool = False
    vsa_direction: int = DIR_NEUTRAL
    vsa_low_volume: bool = False


@dataclass(frozen=True)
class BiasSnapshot:
    timestamp: str
    official_bias: int
    confirmation_committed: bool
    confirmation: Bar | None
    anchor: Bar | None


@dataclass
class Liquidity:
    side: int
    identity: int
    creation_bar: int
    level_ticks: int
    swept: bool = False
    terminal: bool = False


@dataclass
class Gap:
    side: int
    identity: int
    creation_bar: int
    top_ticks: int
    bottom_ticks: int
    mitigated: bool = False
    filled: bool = False
    terminal: bool = False


@dataclass
class OrderBlock:
    side: int
    identity: int
    creation_bar: int
    top_ticks: int
    bottom_ticks: int
    mitigated: bool = False
    terminal: bool = False


@dataclass(frozen=True)
class StructureSnapshot:
    timestamp: str
    trend: int
    event_type: int
    broken_level_ticks: int | None
    liquidity: tuple[Liquidity, ...]
    gaps: tuple[Gap, ...]
    order_blocks: tuple[OrderBlock, ...]


@dataclass(frozen=True)
class TradePlan:
    entry_ready: bool
    entry_ticks: int
    sl_ticks: int | None
    risk_ticks: int | None
    desired_tp_ticks: int | None
    effective_tp_ticks: int | None
    effective_rr_milli: int | None
    selected_liquidity_id: int | None


@dataclass(frozen=True)
class MainCandidate:
    slot: int
    family: int
    subtype: int
    direction: int
    source_bar: int
    anchor_ticks: int | None
    setup_quality: int
    entry_quality: int
    total_quality: int
    grade: int
    veto_mask: int
    plan: TradePlan
    policy_quality: int | None = None
    vsa_adjustment: int = 0
    ema200_adjustment: int = 0
    rsi_adjustment: int = 0
    context_authorized: bool = True
    compatibility_mode: str = "C16C08V1"


@dataclass(frozen=True)
class SignalEvent:
    event_key: str
    symbol: str
    timeframe: str
    signal: str
    confirmation_time: str
    execution_time: str
    entry_ticks: int | None
    sl_ticks: int | None
    tp_ticks: int | None
    source: str
    reference_ticks: int | None = None
    event_kind: str = "PRIMARY_OPEN"
    family: int | None = None
    subtype: int | None = None
    grade: int | None = None
    quality: int | None = None
    engine_version: str = "LEGACY"
    config_hash: str | None = None
    campaign_key: str | None = None
    tp1_ticks: int | None = None


# Cloud risk presets: distances are integer ticks (0.01).
CLOUD_RISK = {
    "M1": (400, 800, 400, 200),
    "M3": (500, 1000, 500, 250),
    "M5": (600, 900, 600, 300),
    "M15": (1000, 2000, 1000, 750),
}
USTEC_CLOUD_RISK = {
    "M1": (944, 1573, 786, 472),
    "M3": (1709, 2849, 1424, 855),
    "M5": (2106, 3510, 1755, 1053),
    "M15": (4763, 7939, 3969, 2382),
}


def cloud_plan(timeframe: str, direction: int, entry_ticks: int, symbol: str = "XAUUSD") -> tuple[int, int, int, int]:
    """Return (SL, TP, trail_activation, trail_distance) in integer ticks."""
    presets = USTEC_CLOUD_RISK if symbol.upper() == "USTEC" else CLOUD_RISK
    sl, tp, activation, trail = presets[timeframe.upper()]
    return ((entry_ticks - sl, entry_ticks + tp, activation, trail)
            if direction == DIR_BULLISH else
            (entry_ticks + sl, entry_ticks - tp, activation, trail))


@dataclass(frozen=True)
class CloudFrame:
    timestamp: str
    regime: int
    high: float
    low: float
    close: float
    previous_close: float
    cloud_width: float
    range_midpoint: float
    micro_bias: int
    polarity_slope: float
    polarity_lower: float
    polarity_upper: float
    upper_inner: float
    upper_outer: float
    lower_outer: float
    lower_inner: float
    structure_event: int
    current_polarity_lower: float | None = None
    current_polarity_upper: float | None = None


@dataclass(frozen=True)
class CloudOutcome:
    qualified: bool
    direction: int
    epoch: int
    score: int
    reason: str


class CloudMachine:
    def __init__(self):
        self.state = CLOUD_INTERACTION_ARMED
        self.epoch = 0
        self.regime = self.setup = self.family = 0
        self.direction = DIR_NEUTRAL

    def step(self, frame):
        trend = frame.regime in (REGIME_UPTREND, REGIME_DOWNTREND)
        range_active = frame.regime == REGIME_RANGE_ACTIVE
        pressure = frame.regime in (REGIME_RANGE_PRESSURE_UP, REGIME_RANGE_PRESSURE_DOWN)
        polarity_overlap = frame.high >= frame.polarity_lower and frame.low <= frame.polarity_upper
        lower_overlap = frame.high >= frame.lower_outer and frame.low <= frame.lower_inner
        upper_overlap = frame.high >= frame.upper_inner and frame.low <= frame.upper_outer
        buy = (frame.regime == REGIME_UPTREND and polarity_overlap and frame.close > frame.polarity_upper) or \
              (range_active and lower_overlap and frame.close > frame.lower_inner) or \
              (frame.regime == REGIME_RANGE_PRESSURE_UP and upper_overlap and frame.previous_close > frame.upper_outer and frame.close > frame.upper_outer)
        sell = (frame.regime == REGIME_DOWNTREND and polarity_overlap and frame.close < frame.polarity_lower) or \
               (range_active and upper_overlap and frame.close < frame.upper_inner) or \
               (frame.regime == REGIME_RANGE_PRESSURE_DOWN and lower_overlap and frame.previous_close < frame.lower_outer and frame.close < frame.lower_outer)
        direction = DIR_BULLISH if buy else DIR_BEARISH if sell else DIR_NEUTRAL
        setup = CLOUD_SETUP_CONTINUATION if trend else CLOUD_SETUP_RANGE_REJECTION if range_active else CLOUD_SETUP_BREAKOUT if pressure else CLOUD_SETUP_NONE
        family = CLOUD_FAMILY_POLARITY if setup == 1 else CLOUD_FAMILY_LOWER if setup == 2 and direction == 1 else CLOUD_FAMILY_UPPER if setup == 2 else CLOUD_FAMILY_UPPER if frame.regime == REGIME_RANGE_PRESSURE_UP else CLOUD_FAMILY_LOWER if setup == 3 else 0
        if (setup, family, direction, frame.regime) != (self.setup, self.family, self.direction, self.regime):
            self.state = CLOUD_INTERACTION_ARMED
        lower, upper = ((frame.polarity_lower, frame.polarity_upper) if family == 1 else
                        (frame.upper_inner, frame.upper_outer) if family == 2 else
                        (frame.lower_outer, frame.lower_inner))
        valid = frame.cloud_width > 0 and lower < upper
        overlap = valid and frame.high >= lower and frame.low <= upper
        confirmation = overlap and (frame.close > upper if direction == 1 else frame.close < lower if direction == -1 else False)
        breakout_retest = overlap and ((frame.previous_close > upper and frame.close > upper) if direction == 1 else
                                        (frame.previous_close < lower and frame.close < lower) if direction == -1 else False)
        candle_quality = breakout_retest if setup == 3 else confirmation
        mid = frame.range_midpoint - 0.5 * frame.cloud_width <= frame.close <= frame.range_midpoint + 0.5 * frame.cloud_width
        structure_aligned = frame.structure_event in ((SE_BOS_BULLISH, SE_CHOCH_BULLISH) if direction == 1 else (SE_BOS_BEARISH, SE_CHOCH_BEARISH))
        structure_conflict = frame.structure_event in ((SE_BOS_BEARISH, SE_CHOCH_BEARISH) if direction == 1 else (SE_BOS_BULLISH, SE_CHOCH_BULLISH))
        polarity_aligned = frame.micro_bias == direction
        regime_fit = (setup == 1 and ((frame.regime == 7 and direction == 1) or (frame.regime == 8 and direction == -1))) or setup == 2 and range_active or setup == 3 and ((frame.regime == 3 and direction == 1) or (frame.regime == 4 and direction == -1))
        location = setup == 1 or setup == 2 and not mid or setup == 3 and ((direction == 1 and family == 2) or (direction == -1 and family == 3))
        score = 25 * regime_fit + 20 * location + 20 * candle_quality + 15 * polarity_aligned + 10 * structure_aligned + 10 * (self.state == 0 and overlap)
        reason = "NONE"
        qualified = False
        if direction and overlap and self.state == CLOUD_INTERACTION_ARMED:
            self.epoch += 1
            wrong_side = frame.close < lower if direction == 1 else frame.close > upper
            current_lower = frame.polarity_lower if frame.current_polarity_lower is None else frame.current_polarity_lower
            current_upper = frame.polarity_upper if frame.current_polarity_upper is None else frame.current_polarity_upper
            opposite = frame.micro_bias == -direction or (frame.close < current_lower and frame.polarity_slope < 0 if direction == 1 else frame.close > current_upper and frame.polarity_slope > 0)
            reason = ("CLOUD_REJECT_INVALID_GEOMETRY" if not valid else "CLOUD_REJECT_WRONG_SIDE" if wrong_side else
                      "CLOUD_REJECT_NO_CONFIRMATION" if not candle_quality else "CLOUD_REJECT_OPPOSITE_POLARITY" if opposite else
                      "CLOUD_REJECT_STRUCTURE_CONFLICT" if structure_conflict else "CLOUD_REJECT_MID_RANGE" if setup == 2 and mid else
                      "CLOUD_REJECT_CHOP" if (range_active or pressure) and frame.micro_bias == 0 and mid else
                      "CLOUD_REJECT_SCORE_BELOW_THRESHOLD" if score < 70 else "NONE")
            qualified = reason == "NONE"
            self.state = CLOUD_INTERACTION_LOCKED
        elif self.state == CLOUD_INTERACTION_LOCKED and valid:
            rearmed = frame.low > upper + 0.5 * frame.cloud_width if direction == 1 else frame.high < lower - 0.5 * frame.cloud_width
            if rearmed and not overlap:
                self.state = CLOUD_INTERACTION_ARMED
        self.regime, self.setup, self.family, self.direction = frame.regime, setup, family, direction
        return CloudOutcome(qualified, direction, self.epoch, score, reason)


class DisplayFilter:
    def __init__(self):
        self.center = self.half_width = self.center_velocity = self.width_velocity = None

    def step(self, lower, upper, cloud_width):
        if lower is None or upper is None or lower >= upper or cloud_width <= 0:
            self.center = self.half_width = self.center_velocity = self.width_velocity = None
            return None
        target_center, target_half = (lower + upper) / 2, (upper - lower) / 2
        if self.center is None:
            self.center, self.half_width = target_center, target_half
            self.center_velocity = self.width_velocity = 0.0
        else:
            center_error = target_center - self.center
            desired = max(-0.5 * cloud_width, min(0.5 * cloud_width, center_error))
            self.center_velocity += max(-0.25 * cloud_width, min(0.25 * cloud_width, desired - self.center_velocity))
            candidate = self.center + self.center_velocity
            if (center_error >= 0 and candidate >= target_center) or (center_error <= 0 and candidate <= target_center):
                candidate, self.center_velocity = target_center, 0.0
            self.center = candidate
            width_error = target_half - self.half_width
            desired = max(-0.25 * cloud_width, min(0.25 * cloud_width, width_error))
            self.width_velocity += max(-0.125 * cloud_width, min(0.125 * cloud_width, desired - self.width_velocity))
            candidate = self.half_width + self.width_velocity
            if (width_error >= 0 and candidate >= target_half) or (width_error <= 0 and candidate <= target_half):
                candidate, self.width_velocity = target_half, 0.0
            minimum = min(target_half, 0.05 * cloud_width)
            self.half_width = max(minimum, candidate)
        return self.center - self.half_width, self.center + self.half_width


class PolarityDisplay:
    def __init__(self):
        self.state = POLARITY_NEUTRAL
        self.incumbent = self.recorded = self.establish_direction = DIR_NEUTRAL
        self.establish_count = self.reversal_count = self.nonqualifying_count = 0
        self.center = self.half_width = self.center_velocity = self.width_velocity = None

    def step(self, core, width, bb_quality, bull_evidence, bear_evidence,
             bull_composite, bear_composite, regime, micro_direction, evidence_valid):
        if core is None or width is None or width <= 0:
            self.__init__()
            return None
        if self.center is None:
            self.center, self.half_width = core, width * (0.15 + 0.15 * bb_quality)
            self.center_velocity = self.width_velocity = 0.0
        pinch = False
        if self.recorded:
            self.state = self.incumbent = self.recorded
            self.recorded = DIR_NEUTRAL
            self.reversal_count = self.nonqualifying_count = 0
        elif not evidence_valid:
            self.state = POLARITY_NEUTRAL
            self.incumbent = self.establish_direction = DIR_NEUTRAL
            self.establish_count = self.reversal_count = self.nonqualifying_count = 0
        elif self.state == POLARITY_NEUTRAL:
            tie = (DIR_BULLISH if regime in (REGIME_UPTREND, REGIME_RANGE_PRESSURE_UP) else
                   DIR_BEARISH if regime in (REGIME_DOWNTREND, REGIME_RANGE_PRESSURE_DOWN) else micro_direction)
            candidate = (DIR_BULLISH if bull_evidence >= 0.60 and bull_evidence - bear_evidence >= 0.10 else
                         DIR_BEARISH if bear_evidence >= 0.60 and bear_evidence - bull_evidence >= 0.10 else
                         tie if bull_evidence == bear_evidence else DIR_NEUTRAL)
            strength = bull_evidence if candidate == DIR_BULLISH else bear_evidence if candidate == DIR_BEARISH else 0.0
            if candidate and strength >= 0.60:
                self.establish_count = min(2, self.establish_count + 1) if self.establish_direction == candidate else 1
                self.establish_direction = candidate
                if self.establish_count == 2:
                    self.state = self.incumbent = candidate
                    self.establish_direction = DIR_NEUTRAL
                    self.establish_count = 0
            else:
                self.establish_direction = DIR_NEUTRAL
                self.establish_count = 0
        elif self.state in (POLARITY_BULL, POLARITY_BEAR):
            self.incumbent = self.state
            incumbent = bull_evidence if self.incumbent == DIR_BULLISH else bear_evidence
            opposite = bear_evidence if self.incumbent == DIR_BULLISH else bull_evidence
            if incumbent < 0.45 and opposite >= 0.60 and opposite - incumbent >= 0.10:
                self.state = POLARITY_TRANSITION
                self.reversal_count = self.nonqualifying_count = 0
        else:
            incumbent = bull_evidence if self.incumbent == DIR_BULLISH else bear_evidence
            opposite = bear_evidence if self.incumbent == DIR_BULLISH else bull_evidence
            if opposite >= 0.70 and opposite - incumbent >= 0.10:
                self.reversal_count = min(2, self.reversal_count + 1)
                self.nonqualifying_count = 0
                if self.reversal_count == 2:
                    self.recorded = -self.incumbent
                    pinch = True
            elif incumbent >= 0.50 and incumbent - opposite >= 0.10:
                self.state = self.incumbent
                self.reversal_count = self.nonqualifying_count = 0
            else:
                self.reversal_count = 0
                self.nonqualifying_count = min(2, self.nonqualifying_count + 1)
                if self.nonqualifying_count == 2:
                    self.state = self.incumbent if incumbent >= opposite else POLARITY_NEUTRAL
                    if self.state == POLARITY_NEUTRAL:
                        self.incumbent = DIR_NEUTRAL
                    self.nonqualifying_count = 0
        visible = self.incumbent if self.state == POLARITY_TRANSITION else self.state if self.state in (-1, 1) else 0
        composite = bull_composite if visible == 1 else bear_composite if visible == -1 else 0.0
        volatility = max(0.75, min(1.25, 0.75 + 0.50 * bb_quality))
        directional = max(0.10 * width, min(1.75 * width, width * volatility * (0.15 + 1.35 * composite ** 2)))
        target = width * (0.15 + 0.15 * bb_quality) if self.state == 0 or self.state in (-1, 1) and bull_evidence < 0.45 and bear_evidence < 0.45 else directional
        if self.state == POLARITY_TRANSITION:
            target = min(target, max(0.075 * width, 0.50 * self.half_width))
        center_error = core - self.center
        desired = max(-0.50 * width, min(0.50 * width, center_error))
        self.center_velocity += max(-0.25 * width, min(0.25 * width, desired - self.center_velocity))
        candidate = self.center + self.center_velocity
        if center_error >= 0 and candidate >= core or center_error <= 0 and candidate <= core:
            self.center, self.center_velocity = core, 0.0
        else:
            self.center = candidate
        width_error = target - self.half_width
        expanding = width_error >= 0
        velocity_cap = (0.30 if expanding else 0.50 if self.state == POLARITY_TRANSITION else 0.12) * width
        acceleration_cap = (0.15 if expanding else 0.25 if self.state == POLARITY_TRANSITION else 0.06) * width
        desired = max(-velocity_cap, min(velocity_cap, width_error))
        next_velocity = self.width_velocity + max(-acceleration_cap, min(acceleration_cap, desired - self.width_velocity))
        if self.width_velocity and desired * self.width_velocity < 0 and next_velocity * self.width_velocity < 0:
            next_velocity = 0.0
        candidate = self.half_width + next_velocity
        if width_error >= 0 and candidate >= target or width_error <= 0 and candidate <= target:
            self.half_width, self.width_velocity = target, 0.0
        else:
            self.half_width, self.width_velocity = candidate, next_velocity
        if pinch:
            self.half_width, self.width_velocity = 0.075 * width, 0.0
        return self.center - self.half_width, self.center + self.half_width


def polarity_evidence(bars, markets):
    closes = [bar.close for bar in bars]
    fast, slow, sigma = ema(closes, 9), ema(closes, 21), population_stdev(closes, 20)
    sigma_baseline = _ema_optional(sigma, 20)
    highs, lows = [bar.high for bar in bars], [bar.low for bar in bars]
    high9, low9 = rolling_extreme(highs, 9), rolling_extreme(lows, 9, False)
    high26, low26 = rolling_extreme(highs, 26), rolling_extreme(lows, 26, False)
    high52, low52 = rolling_extreme(highs, 52), rolling_extreme(lows, 52, False)
    output = []
    for i, bar in enumerate(bars):
        valid = i >= 52 and all(value is not None for value in (slow[i], slow[i - 3], sigma[i], sigma_baseline[i], high52[i]))
        if not valid:
            output.append((False, 0.0, 0.0, 0.0, 0.0, 0.0))
            continue
        width = markets[i].atr * 0.5
        norm = max(sigma[i], 0.25 * width, 0.01)
        separation = max(-1.0, min(1.0, (fast[i] - slow[i]) / norm))
        slope = max(0.0, min(1.0, abs(slow[i] - slow[i - 3]) / (3.0 * norm)))
        bull_persistence = sum(slow[j] > slow[j - 1] for j in range(i - 2, i + 1)) / 3.0
        bear_persistence = sum(slow[j] < slow[j - 1] for j in range(i - 2, i + 1)) / 3.0
        quality = max(0.0, min(1.0, (sigma[i] / max(sigma_baseline[i], 0.01) - 0.75) / 0.75))
        tenkan, kijun = (high9[i] + low9[i]) / 2, (high26[i] + low26[i]) / 2
        span_a, span_b = (tenkan + kijun) / 2, (high52[i] + low52[i]) / 2
        bull_ichi = ((tenkan > kijun) + (bar.close > kijun) + (span_a > span_b)) / 3.0
        bear_ichi = ((tenkan < kijun) + (bar.close < kijun) + (span_a < span_b)) / 3.0
        bull_sep, bear_sep = max(0.0, separation), max(0.0, -separation)
        bull_composite = min(1.0, 0.30 * bull_sep + 0.25 * (0.60 * slope + 0.40 * bull_persistence) + 0.20 * quality + 0.25 * bull_ichi)
        bear_composite = min(1.0, 0.30 * bear_sep + 0.25 * (0.60 * slope + 0.40 * bear_persistence) + 0.20 * quality + 0.25 * bear_ichi)
        output.append((True, quality, 0.40 * bull_sep + 0.30 * bull_persistence + 0.30 * bull_ichi,
                       0.40 * bear_sep + 0.30 * bear_persistence + 0.30 * bear_ichi,
                       bull_composite, bear_composite))
    return output


def _score(*parts, effect=0):
    return max(0, min(100, math.floor((sum(parts) + 50) / 100) + effect))


def _grade(setup, entry, total, veto):
    if veto:
        return 0
    if setup >= 90 and entry >= 85 and total >= 90:
        return 3
    if setup >= 75 and entry >= 70 and total >= 75:
        return 2
    return 1 if setup >= 45 and entry >= 40 else 0


def _regime_cell(regime, family):
    if family == MF_CONTINUATION:
        return 2 if regime == 6 else 1 if regime in (1, 2, 5) else 0
    if family == MF_BREAKOUT:
        return 2 if regime == 1 else 1 if regime in (2, 7, 8) else 0
    return 1 if regime in (3, 4, 6, 7, 8) else 0


def candidates_for_bar(bar, index, market, bias, structure, timeframe=None):
    if not market.warmup_valid:
        return []
    basis, upper, lower = market.bollinger_basis, market.bollinger_upper, market.bollinger_lower
    pullback = market.atr * 0.5
    bull_trend = market.regime in (REGIME_UPTREND, REGIME_RANGE_PRESSURE_UP) or structure.trend == DIR_BULLISH or market.normalized_separation > 0
    bear_trend = market.regime in (REGIME_DOWNTREND, REGIME_RANGE_PRESSURE_DOWN) or structure.trend == DIR_BEARISH or market.normalized_separation < 0
    cont_bull = MST_CONT_RECLAIM if bull_trend and basis is not None and bar.low < basis and bar.close > basis and bar.close > bar.open else \
        MST_CONT_PULLBACK if bull_trend and basis is not None and bar.low <= basis + pullback and bar.close >= basis and bar.close >= bar.open else None
    cont_bear = MST_CONT_RECLAIM if bear_trend and basis is not None and bar.high > basis and bar.close < basis and bar.close < bar.open else \
        MST_CONT_PULLBACK if bear_trend and basis is not None and bar.high >= basis - pullback and bar.close <= basis and bar.close <= bar.open else None
    break_bull = MST_BREAK_STRUCTURE if structure.event_type in (SE_BOS_BULLISH, SE_CHOCH_BULLISH) else \
        MST_BREAK_RANGE if market.regime == REGIME_BREAKOUT_CANDIDATE and market.break_direction == DIR_BULLISH or upper is not None and bar.close > upper else None
    break_bear = MST_BREAK_STRUCTURE if structure.event_type in (SE_BOS_BEARISH, SE_CHOCH_BEARISH) else \
        MST_BREAK_RANGE if market.regime == REGIME_BREAKOUT_CANDIDATE and market.break_direction == DIR_BEARISH or lower is not None and bar.close < lower else None
    rev_bull = MST_REV_STRUCTURE if structure.event_type == SE_CHOCH_BULLISH else \
        MST_REV_EXHAUSTION if market.swing_low_ticks is not None and to_ticks(bar.low) < market.swing_low_ticks and bar.close > bar.open else None
    rev_bear = MST_REV_STRUCTURE if structure.event_type == SE_CHOCH_BEARISH else \
        MST_REV_EXHAUSTION if market.swing_high_ticks is not None and to_ticks(bar.high) > market.swing_high_ticks and bar.close < bar.open else None
    output = []
    for slot, (family, direction, subtype) in enumerate(((1, 1, cont_bull), (1, -1, cont_bear),
                                                         (2, 1, break_bull), (2, -1, break_bear),
                                                         (3, 1, rev_bull), (3, -1, rev_bear))):
        if subtype is None:
            continue
        if timeframe is not None and not pine_timeframe_family_authorized(timeframe, family):
            continue
        entry = to_ticks(bar.close)
        atr_ticks = to_ticks(market.atr)
        basis_ticks = to_ticks(basis) if basis is not None else None
        band = to_ticks(upper if direction == 1 else lower) if upper is not None else None
        anchor = (to_ticks(bar.low if direction == 1 else bar.high) if subtype == MST_CONT_PULLBACK else
                  basis_ticks if subtype == MST_CONT_RECLAIM else
                  market.swing_low_ticks if subtype == MST_REV_EXHAUSTION and direction == 1 else
                  market.swing_high_ticks if subtype == MST_REV_EXHAUSTION else
                  structure.broken_level_ticks if subtype == MST_BREAK_STRUCTURE else band)
        ema_direction = 1 if market.normalized_separation > 0 else -1 if market.normalized_separation < 0 else 0
        cell = _regime_cell(market.regime, family)
        missing = anchor is None or atr_ticks <= 0 or basis_ticks is None and family == MF_CONTINUATION or band is None and family == MF_BREAKOUT
        structural = not missing and (entry < anchor if direction == 1 else entry > anchor) if subtype in (101, 102, 302) else \
            not missing and (entry <= anchor if direction == 1 else entry >= anchor)
        conflict = bias.official_bias in (-direction, 2) or structure.trend == -direction
        veto = (1 if missing else 0) + (2 if conflict else 0) + (4 if cell == 2 else 0) + (8 if structural else 0)
        htf = 100 if bias.official_bias == direction else 50 if bias.official_bias == 0 else 0
        struct_score = 100 if structure.trend == direction else 50 if structure.trend == 0 else 0
        liquidity_evidence = subtype == MST_REV_EXHAUSTION
        fvg = any(item.side == direction for item in structure.gaps)
        ob = any(item.side == direction for item in structure.order_blocks)
        trigger = (2 if structure.trend == direction or ema_direction == direction else 1) if subtype in (101, 102) else \
            (2 if structure.event_type == direction else 1) if subtype == 201 else \
            (2 if market.regime == 6 else 1) if subtype == 202 else \
            (2 if structure.event_type == direction * 2 else 1) if subtype == 301 else 2
        confluence = sum((liquidity_evidence, fvg, ob))
        setup = _score(30 * htf, 25 * struct_score, 20 * (100 if cell == 0 else 50 if cell == 1 else 0),
                       15 * (100 if trigger == 2 else 50), 10 * (100 if confluence >= 2 else 50 if confluence == 1 else 0),
                       effect=-10 if cell == 1 else 0)
        candle = 50 if entry == to_ticks(bar.open) else 100 if (entry > to_ticks(bar.open)) == (direction == 1) else 0
        location = basis_ticks if family == 1 else band if family == 2 else anchor
        location_score = 50 if entry == location else 100 if (entry > location) == (direction == 1) else 0
        span = to_ticks(bar.high) - to_ticks(bar.low)
        numerator = entry - to_ticks(bar.low) if direction == 1 else to_ticks(bar.high) - entry
        body_position = max(0, min(10000, math.floor((numerator * 10000 + span // 2) / span))) if span > 0 else 0
        base_entry_quality = _score(35 * candle, 35 * location_score, 30 * (100 if body_position >= 6667 else 50 if body_position >= 3333 else 0), 0, 0)
        vsa = vsa_adjustment(direction, market.vsa_direction, market.vsa_valid_volume,
                             market.vsa_low_volume, family == MF_BREAKOUT,
                             family == MF_CONTINUATION, family == MF_REVERSAL,
                             structure.event_type in (direction, direction * 2))
        entry_quality = max(0, min(100, base_entry_quality + vsa))
        total = max(0, min(100, math.floor((setup * 60 + entry_quality * 40 + 50) / 100)))
        policy = ema_rsi_policy(direction, bar.close, market.ema200, market.atr,
                                 market.ema200_slope_atr, market.rsi, market.rsi_slope, family)
        policy_quality = max(0, min(100, total + policy.adjustment))
        veto |= 16 if policy.veto else 0
        grade = _grade(setup, entry_quality, policy_quality, veto)
        plan = plan_trade(direction, entry, atr_ticks, anchor, structure.liquidity, index, grade, veto)
        output.append(MainCandidate(slot, family, subtype, direction, index, anchor, setup, entry_quality,
                                    policy_quality, grade, veto, plan, policy_quality, vsa,
                                    policy.ema_adjustment, policy.rsi_adjustment,
                                    True if timeframe is None else pine_timeframe_family_authorized(timeframe, family)))
    return output


def winning_candidate(candidates):
    eligible = [item for item in candidates if item.plan.entry_ready]
    return max(eligible, key=lambda item: (item.grade, item.total_quality, -item.family, -item.slot), default=None)


def replay_main_candidates(bars, timeframe):
    markets = market_snapshots(bars)
    biases = official_bias_snapshots(bars, timeframe)
    structures = structure_snapshots(bars, markets)
    return [winning_candidate(candidates_for_bar(bar, index, market, bias, structure, timeframe))
            for index, (bar, market, bias, structure) in enumerate(zip(bars, markets, biases, structures))]


def replay_main_events(bars, timeframe, symbol="XAUUSD"):
    winners = replay_main_candidates(bars, timeframe)
    markets = market_snapshots(bars)
    events, active_thesis = [], None
    for index in range(1, len(bars)):
        pending, bar = winners[index - 1], bars[index]
        if active_thesis:
            direction, sl, tp = active_thesis[0], active_thesis[-2], active_thesis[-1]
            if (direction == 1 and (bar.low <= sl or bar.high >= tp)) or (direction == -1 and (bar.high >= sl or bar.low <= tp)):
                active_thesis = None
        if not pending or not pending.plan.entry_ready:
            continue
        plan, direction = pending.plan, pending.direction
        thesis = (direction, pending.family, pending.subtype, plan.sl_ticks, plan.effective_tp_ticks)
        if thesis == active_thesis:
            continue
        open_ticks = to_ticks(bar.open)
        # Release 1 override: no synthetic spread is applied to the plan.
        spread_ticks = to_ticks(PINE_SPREAD_PRICE if symbol.upper() == "XAUUSD" else 0.3)
        maximum_chase = math.floor(to_ticks(markets[index - 1].atr) * PINE_MAXIMUM_CHASE_ATR)
        chase = max(0, (open_ticks - plan.entry_ticks) * direction)
        reached = open_ticks <= plan.entry_ticks if direction == 1 else open_ticks >= plan.entry_ticks
        entry = open_ticks if reached else plan.entry_ticks
        touched = reached or (to_ticks(bar.low) <= entry if direction == 1 else to_ticks(bar.high) >= entry)
        gross_risk = (entry - plan.sl_ticks) * direction
        gross_reward = (plan.effective_tp_ticks - entry) * direction
        net_risk, net_reward = gross_risk + spread_ticks, gross_reward - spread_ticks
        rr = net_reward * 1000 // net_risk if net_risk > 0 and net_reward > 0 else 0
        cost_limit = 1000 if timeframe == "m5" else 250 if timeframe in ("m1", "m15") else 200
        cost_ok = timeframe == "m5" or spread_ticks * 1000 <= gross_risk * cost_limit
        if not touched or chase > maximum_chase or rr < PINE_MINIMUM_RR_MILLI or not cost_ok:
            continue
        signal = "MAIN BUY" if direction == 1 else "MAIN SELL"
        confirmation = bars[index - 1].timestamp
        key = f"{symbol}:{timeframe}:{confirmation}:{signal.replace(' ', '-').lower()}"
        events.append(SignalEvent(key, symbol, timeframe.upper(), signal, confirmation, bar.timestamp,
                                  entry, plan.sl_ticks, plan.effective_tp_ticks, "MAIN", None,
                                  "PRIMARY_OPEN", pending.family, pending.subtype,
                                  pending.grade, pending.total_quality))
        active_thesis = thesis
    return events


def replay_cloud_events(bars, timeframe, symbol="XAUUSD", info_only=None):
    info_only = symbol.upper() == "XAUUSD" if info_only is None else info_only
    snapshots = cloud_snapshots(bars)
    events = []
    for bar, outcome in zip(bars, snapshots):
        if outcome and outcome.qualified:
            signal = "CLOUD BUY" if outcome.direction == DIR_BULLISH else "CLOUD SELL"
            key = f"{symbol}:{timeframe}:cloud:{outcome.epoch}:{outcome.direction}"
            entry = to_ticks(bar.close)
            if info_only:
                events.append(SignalEvent(key, symbol, timeframe.upper(), signal, bar.timestamp, bar.timestamp,
                                          None, None, None, "CLOUD", entry, "CLOUD_INFO"))
            else:
                sl, tp, _, _ = cloud_plan(timeframe, outcome.direction, entry, symbol)
                events.append(SignalEvent(key, symbol, timeframe.upper(), signal, bar.timestamp, bar.timestamp,
                                          entry, sl, tp, "CLOUD", entry))
    return events


def _contain_cloud_zones(bar, width, polarity, upper, lower):
    if not all((polarity, upper, lower)):
        return None
    clearance = 0.10 * width
    upper_shift = max(0.0, max(bar.high + clearance, polarity[1] + clearance) - upper[0])
    lower_shift = max(0.0, lower[1] - min(bar.low - clearance, polarity[0] - clearance))
    upper = upper[0] + upper_shift, upper[1] + upper_shift
    lower = lower[0] - lower_shift, lower[1] - lower_shift
    valid = (bar.high + clearance <= upper[0] and bar.low - clearance >= lower[1] and
             polarity[1] + clearance <= upper[0] and polarity[0] - clearance >= lower[1])
    return (polarity, upper, lower) if valid else None


def cloud_snapshots(bars):
    markets = market_snapshots(bars)
    structures = structure_snapshots(bars, markets)
    evidence = polarity_evidence(bars, markets)
    machine, outcomes = CloudMachine(), [None]
    polarity_filter, upper_filter, lower_filter = PolarityDisplay(), DisplayFilter(), DisplayFilter()
    previous_zones = None
    committed_micro = DIR_NEUTRAL
    for index in range(1, len(bars)):
        bar, current, previous = bars[index], markets[index], markets[index - 1]
        if not current.warmup_valid or not previous.warmup_valid:
            outcomes.append(None)
            continue
        width = current.atr * 0.5
        current_core = (current.ema_fast + current.ema_slow) / 2
        previous_core = (previous.ema_fast + previous.ema_slow) / 2
        slope = current_core - previous_core
        prior_micro = DIR_BULLISH if slope > 0 else DIR_BEARISH if slope < 0 else DIR_NEUTRAL
        valid, quality, bull, bear, bull_composite, bear_composite = evidence[index]
        polarity = polarity_filter.step(current_core, width, quality, bull, bear, bull_composite,
                                         bear_composite, current.regime, prior_micro, valid)
        polarity_slope = 0.0 if previous_zones is None or polarity is None or previous_zones[0] is None else (sum(polarity) - sum(previous_zones[0])) / 2
        micro = DIR_BULLISH if polarity and bar.close > polarity[1] and polarity_slope > 0 else DIR_BEARISH if polarity and bar.close < polarity[0] and polarity_slope < 0 else DIR_NEUTRAL
        trend_valid = current.regime in (REGIME_UPTREND, REGIME_DOWNTREND)
        direction = (DIR_BULLISH if current.regime in (REGIME_UPTREND, REGIME_RANGE_PRESSURE_UP) else
                     DIR_BEARISH if current.regime in (REGIME_DOWNTREND, REGIME_RANGE_PRESSURE_DOWN) else committed_micro)
        cores = [markets[j].cloud_core for j in range(max(0, index - 3), index + 1)]
        deltas = [b - a for a, b in zip(cores, cores[1:]) if a is not None and b is not None]
        aligned = sum(delta > 0 if direction == DIR_BULLISH else delta < 0 for delta in deltas[-3:]) if direction else 0
        slope_points = 25 if aligned == 3 else 17 if aligned == 2 else 8 if aligned == 1 else 0
        directional_side = bar.close > current_core if direction == 1 else bar.close < current_core if direction == -1 else False
        displacement = abs(bar.close - current_core) / width
        displacement_points = (20 if displacement >= 1 else 15 if displacement >= 0.75 else 10 if displacement >= 0.5 else 5 if displacement >= 0.25 else 0) if directional_side else 0
        regime_points = 20 if trend_valid else 14 if current.regime in (REGIME_RANGE_PRESSURE_UP, REGIME_RANGE_PRESSURE_DOWN) else 7 if current.regime == REGIME_RANGE_ACTIVE and direction == committed_micro and direction else 0
        event = structures[index].event_type
        structure_aligned = bool(direction and event in ((SE_BOS_BULLISH, SE_CHOCH_BULLISH) if direction == 1 else (SE_BOS_BEARISH, SE_CHOCH_BEARISH)))
        structure_conflict = bool(direction and event in ((SE_BOS_BEARISH, SE_CHOCH_BEARISH) if direction == 1 else (SE_BOS_BULLISH, SE_CHOCH_BULLISH)))
        micro_aligned = bool(direction and direction == committed_micro)
        chop = current.regime in (REGIME_RANGE_ACTIVE, REGIME_RANGE_PRESSURE_UP, REGIME_RANGE_PRESSURE_DOWN) and not committed_micro and current.range_low + (current.range_high - current.range_low) * 0.35 < bar.close < current.range_high - (current.range_high - current.range_low) * 0.35
        cleanliness = 0 if structure_conflict or chop else 20 if structure_aligned and micro_aligned else 10 if structure_aligned or micro_aligned else 0
        selected_valid = current.regime not in (REGIME_TRANSITION, REGIME_RANGE_FORMING, REGIME_BREAKOUT_CANDIDATE)
        overlap = selected_valid and bar.high >= current.cloud_lower and bar.low <= current.cloud_upper
        confirmation = overlap and (bar.close > current.cloud_upper if direction == 1 else bar.close < current.cloud_lower if direction == -1 else False)
        pre_veto = structure_conflict or chop or (committed_micro == -direction if direction else True) or machine.state == CLOUD_INTERACTION_LOCKED
        interaction = 0 if not selected_valid or pre_veto else 15 if overlap and confirmation else 10 if overlap and directional_side else 5 if not overlap else 0
        trend_strength = max(0, min(100, slope_points + displacement_points + regime_points + cleanliness + interaction)) / 100.0
        upper_displacement = max(0.0, current.cloud_upper - (current.range_high - width)) if trend_valid else 0.0
        lower_displacement = max(0.0, current.range_low + width - current.cloud_lower) if trend_valid else 0.0
        expansion = 1.25 * trend_strength * width if trend_valid else 0.0
        upper = upper_filter.step(current.range_high - width + upper_displacement + expansion,
                                  current.range_high + width + upper_displacement + expansion, width)
        lower = lower_filter.step(current.range_low - width - lower_displacement - expansion,
                                  current.range_low + width - lower_displacement - expansion, width)
        contained = _contain_cloud_zones(bar, width, polarity, upper, lower)
        if contained:
            polarity, upper, lower = contained
            upper_filter.center, upper_filter.center_velocity = sum(upper) / 2, 0.0
            lower_filter.center, lower_filter.center_velocity = sum(lower) / 2, 0.0
        else:
            polarity = upper = lower = None
        committed_micro = micro
        if previous_zones is None:
            previous_zones = (polarity, upper, lower)
            outcomes.append(None)
            continue
        visible_polarity, visible_upper, visible_lower = previous_zones
        previous_zones = (polarity, upper, lower)
        if not all((visible_polarity, visible_upper, visible_lower)):
            outcomes.append(None)
            continue
        frame = CloudFrame(
            bar.timestamp, current.regime, bar.high, bar.low, bar.close, bars[index - 1].close,
            width, (current.range_high + current.range_low) / 2, micro, polarity_slope,
            visible_polarity[0], visible_polarity[1],
            visible_upper[0], visible_upper[1], visible_lower[0], visible_lower[1],
            structures[index].event_type,
        )
        outcome = machine.step(frame)
        outcomes.append(outcome)
    return outcomes


def replay_events(bars, timeframe, symbol="XAUUSD"):
    return sorted(replay_main_events(bars, timeframe, symbol) + replay_cloud_events(bars, timeframe, symbol),
                  key=lambda event: (event.execution_time, event.source, event.event_key))


def replay_diagnostics(bars, timeframe):
    markets = market_snapshots(bars)
    biases = official_bias_snapshots(bars, timeframe)
    structures = structure_snapshots(bars, markets)
    clouds = cloud_snapshots(bars)
    rows = []
    for index, (bar, market, bias, structure, cloud) in enumerate(zip(bars, markets, biases, structures, clouds)):
        winner = winning_candidate(candidates_for_bar(bar, index, market, bias, structure, timeframe))
        rows.append({
            "timestamp": bar.timestamp,
            "pine_core_version": PINE_CORE_VERSION,
            "pine_config_fingerprint": PINE_CONFIG_FINGERPRINT,
            "regime": market.regime,
            "break_direction": market.break_direction,
            "bias": bias.official_bias,
            "swing_high_ticks": market.swing_high_ticks,
            "swing_low_ticks": market.swing_low_ticks,
            "structure_trend": structure.trend,
            "structure_event": structure.event_type,
            "broken_level_ticks": structure.broken_level_ticks,
            "main_slot": winner.slot if winner else None,
            "main_family": winner.family if winner else None,
            "main_subtype": winner.subtype if winner else None,
            "main_direction": winner.direction if winner else None,
            "main_grade": winner.grade if winner else None,
            "main_quality": winner.total_quality if winner else None,
            "policy_quality": winner.policy_quality if winner else None,
            "vsa_adjustment": winner.vsa_adjustment if winner else None,
            "ema200_adjustment": winner.ema200_adjustment if winner else None,
            "rsi_adjustment": winner.rsi_adjustment if winner else None,
            "context_authorized": winner.context_authorized if winner else None,
            "compatibility_mode": winner.compatibility_mode if winner else "C16C08V1",
            "main_entry_ticks": winner.plan.entry_ticks if winner else None,
            "main_sl_ticks": winner.plan.sl_ticks if winner else None,
            "main_tp_ticks": winner.plan.effective_tp_ticks if winner else None,
            "cloud_direction": cloud.direction if cloud else None,
            "cloud_epoch": cloud.epoch if cloud else None,
            "cloud_score": cloud.score if cloud else None,
            "cloud_reason": cloud.reason if cloud else None,
            "cloud_qualified": cloud.qualified if cloud else False,
        })
    return rows


def plan_trade(direction, entry_ticks, atr_ticks, anchor_ticks, liquidity, signal_bar, grade, veto_mask=0):
    anchor_valid = anchor_ticks is not None and (
        anchor_ticks < entry_ticks if direction == DIR_BULLISH else anchor_ticks > entry_ticks)
    sl = anchor_ticks if anchor_valid else entry_ticks - direction * atr_ticks if atr_ticks > 0 else None
    geometry = sl is not None and (sl < entry_ticks if direction == DIR_BULLISH else entry_ticks < sl)
    risk = abs(entry_ticks - sl) if geometry else None
    desired_tp = entry_ticks + direction * (risk * 2000 // 1000) if risk else None
    selected = None
    if desired_tp is not None:
        required_side = DIR_BEARISH if direction == DIR_BULLISH else DIR_BULLISH
        eligible = [item for item in liquidity if not item.terminal and not item.swept and
                    item.creation_bar < signal_bar and item.side == required_side and
                    (entry_ticks < item.level_ticks <= desired_tp if direction == DIR_BULLISH
                     else desired_tp <= item.level_ticks < entry_ticks)]
        if eligible:
            selected = min(eligible, key=lambda item: (item.level_ticks, item.identity)) if direction == DIR_BULLISH \
                else max(eligible, key=lambda item: (item.level_ticks, -item.identity))
    effective_tp = selected.level_ticks if selected else desired_tp
    valid = geometry and effective_tp is not None and (
        sl < entry_ticks < effective_tp if direction == DIR_BULLISH else effective_tp < entry_ticks < sl)
    reward = abs(effective_tp - entry_ticks) if valid else None
    rr = reward * 1000 // risk if reward and risk else None
    ready = veto_mask == 0 and grade in (2, 3) and rr is not None and rr >= PINE_MINIMUM_RR_MILLI
    return TradePlan(ready, entry_ticks, sl, risk, desired_tp, effective_tp, rr,
                     selected.identity if selected else None)


def structure_snapshots(bars, snapshots=None):
    snapshots = snapshots or market_snapshots(bars)
    liquidity, gaps, blocks, output = [], [], [], []
    trend = DIR_NEUTRAL
    high_consumed = low_consumed = False
    sequence = 0
    for index, (bar, snapshot) in enumerate(zip(bars, snapshots)):
        high, low, close = to_ticks(bar.high), to_ticks(bar.low), to_ticks(bar.close)
        prior = snapshots[index - 1] if index else None
        prior_high = prior.swing_high_ticks if prior else None
        prior_low = prior.swing_low_ticks if prior else None

        liquidity = [item for item in liquidity if index - item.creation_bar <= 500 and not item.terminal]
        gaps = [item for item in gaps if index - item.creation_bar <= 250 and not item.terminal]
        blocks = [item for item in blocks if index - item.creation_bar <= 250 and not item.terminal]
        for item in liquidity:
            is_high = item.side == DIR_BEARISH
            if (is_high and close > item.level_ticks) or (not is_high and close < item.level_ticks):
                item.terminal = True
            elif not item.swept and ((is_high and high > item.level_ticks and close <= item.level_ticks) or
                                     (not is_high and low < item.level_ticks and close >= item.level_ticks)):
                item.swept = True
        for item in gaps:
            if (item.side == DIR_BULLISH and close < item.bottom_ticks) or (item.side == DIR_BEARISH and close > item.top_ticks):
                item.terminal = True
            elif not item.filled and ((item.side == DIR_BULLISH and low <= item.bottom_ticks) or
                                      (item.side == DIR_BEARISH and high >= item.top_ticks)):
                item.filled = item.terminal = True
            elif not item.mitigated and high >= item.bottom_ticks and low <= item.top_ticks:
                item.mitigated = True
        for item in blocks:
            if (item.side == DIR_BULLISH and close < item.bottom_ticks) or (item.side == DIR_BEARISH and close > item.top_ticks):
                item.terminal = True
            elif not item.mitigated and high >= item.bottom_ticks and low <= item.top_ticks:
                item.mitigated = True

        new_gap_side = DIR_NEUTRAL
        if index >= 2:
            prior_two_high, prior_two_low = to_ticks(bars[index - 2].high), to_ticks(bars[index - 2].low)
            prior_high_bar, prior_low_bar = to_ticks(bars[index - 1].high), to_ticks(bars[index - 1].low)
            if low > prior_two_high and prior_high_bar > prior_two_high:
                sequence += 1
                gaps.append(Gap(DIR_BULLISH, sequence, index, low, prior_two_high))
                new_gap_side = DIR_BULLISH
            elif high < prior_two_low and prior_low_bar < prior_two_low:
                sequence += 1
                gaps.append(Gap(DIR_BEARISH, sequence, index, prior_two_low, high))
                new_gap_side = DIR_BEARISH

        new_high = snapshot.swing_high_confirmation_timestamp == bar.timestamp
        new_low = snapshot.swing_low_confirmation_timestamp == bar.timestamp
        for side, exists, level in ((DIR_BULLISH, new_low, snapshot.swing_low_ticks),
                                    (DIR_BEARISH, new_high, snapshot.swing_high_ticks)):
            if exists:
                match = next((item for item in liquidity if not item.terminal and item.side == side and
                              abs(level - item.level_ticks) <= 1), None)
                if match:
                    match.level_ticks = max(match.level_ticks, level) if side == DIR_BEARISH else min(match.level_ticks, level)
                else:
                    sequence += 1
                    liquidity.append(Liquidity(side, sequence, index, level))

        bull_break = prior_high is not None and not high_consumed and close > prior_high
        bear_break = prior_low is not None and not low_consumed and close < prior_low
        event, broken = SE_NONE, None
        if not (bull_break and bear_break):
            if bull_break:
                event, broken = (SE_CHOCH_BULLISH if trend == DIR_BEARISH else SE_BOS_BULLISH), prior_high
                trend, high_consumed = DIR_BULLISH, True
            elif bear_break:
                event, broken = (SE_CHOCH_BEARISH if trend == DIR_BULLISH else SE_BOS_BEARISH), prior_low
                trend, low_consumed = DIR_BEARISH, True
        if new_high:
            high_consumed = False
        if new_low:
            low_consumed = False

        if event != SE_NONE and new_gap_side == (DIR_BULLISH if event > 0 else DIR_BEARISH):
            side = DIR_BULLISH if event > 0 else DIR_BEARISH
            for offset in range(1, min(20, index) + 1):
                source = bars[index - offset]
                if (side == DIR_BULLISH and source.close < source.open) or (side == DIR_BEARISH and source.close > source.open):
                    sequence += 1
                    blocks.append(OrderBlock(side, sequence, index, to_ticks(source.high), to_ticks(source.low)))
                    break

        liquidity = [item for item in liquidity if not item.terminal][-8:]
        gaps = [item for item in gaps if not item.terminal][-5:]
        blocks = [item for item in blocks if not item.terminal][-5:]
        output.append(StructureSnapshot(bar.timestamp, trend, event, broken,
                                        tuple(liquidity), tuple(gaps), tuple(blocks)))
    return output


def _epoch_ms(timestamp):
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def _bucket_commits(bars, source_minutes, target_minutes):
    source_ms, target_ms = source_minutes * 60_000, target_minutes * 60_000
    expected = target_minutes // source_minutes
    current_start = None
    current = []
    commits = {}
    for index, bar in enumerate(bars):
        timestamp = _epoch_ms(bar.timestamp)
        bucket_start = timestamp - timestamp % target_ms
        if current_start is None or bucket_start == current_start:
            current_start = bucket_start
            current.append(bar)
            continue
        if bucket_start > current_start:
            times = [_epoch_ms(item.timestamp) for item in current]
            complete = (len(current) == expected and times[0] == current_start and
                        times[-1] == current_start + target_ms - source_ms and
                        all(right - left == source_ms for left, right in zip(times, times[1:])))
            if complete:
                commits[index] = Bar(
                    datetime.fromtimestamp(current_start / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                    current[0].open, max(item.high for item in current), min(item.low for item in current),
                    current[-1].close, sum(item.volume for item in current))
            current_start, current = bucket_start, [bar]
        else:
            current_start, current = None, []
    return commits


def official_bias_snapshots(bars, timeframe):
    source_minutes, confirmation_minutes, anchor_minutes = TIMEFRAME_CONTEXT[timeframe]
    confirmation_commits = _bucket_commits(bars, source_minutes, confirmation_minutes)
    anchor_commits = _bucket_commits(bars, source_minutes, anchor_minutes)
    official = pending = BIAS_NEUTRAL
    pending_count = 0
    confirmation = anchor = None
    snapshots = []
    for index, bar in enumerate(bars):
        if index in anchor_commits:
            anchor = anchor_commits[index]
        committed = index in confirmation_commits
        if committed:
            confirmation = confirmation_commits[index]
            confirmation_direction = 1 if confirmation.close > confirmation.open else -1
            anchor_direction = 0 if anchor is None else 1 if anchor.close > anchor.open else -1
            score = confirmation_direction + anchor_direction
            target = BIAS_BULLISH if score > 0.1 else BIAS_BEARISH if score < -0.1 else BIAS_NEUTRAL
            if target != pending:
                pending, pending_count = target, 1
            else:
                pending_count += 1
            if target != official and pending_count >= 2:
                official = target
        snapshots.append(BiasSnapshot(bar.timestamp, official, committed, confirmation, anchor))
    return snapshots


def market_snapshots(bars):
    ema_fast = ema_slow = average_true_range = previous_close = None
    highs, lows, snapshots = [], [], []
    closes = [item.close for item in bars]
    volumes = [item.volume for item in bars]
    ranges = [item.high - item.low for item in bars]
    ema200_values = ema(closes, PINE_EMA200_LENGTH)
    rsi_values = wilder_rsi(closes, PINE_RSI_LENGTH)
    regime, regime_age, break_direction = REGIME_RANGE_FORMING, 0, 0
    swing_high = swing_low = None
    swing_high_source = swing_high_confirmation = None
    swing_low_source = swing_low_confirmation = None
    for index, bar in enumerate(bars):
        prior_high = max(highs) if highs else bar.high
        prior_low = min(lows) if lows else bar.low
        true_range = bar.high - bar.low if previous_close is None else max(
            bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close))
        ema_fast = bar.close if ema_fast is None else ema_fast + (2.0 / 9.0) * (bar.close - ema_fast)
        ema_slow = bar.close if ema_slow is None else ema_slow + (2.0 / 22.0) * (bar.close - ema_slow)
        average_true_range = max(true_range if average_true_range is None else average_true_range + (1.0 / 14.0) * (true_range - average_true_range), 1e-12)
        previous_close = bar.close
        highs = (highs + [bar.high])[-20:]
        lows = (lows + [bar.low])[-20:]
        range_high, range_low = max(highs), min(lows)

        regime_age += 1
        separation = (ema_fast - ema_slow) / max(average_true_range, 1e-12)
        requested = regime
        if regime == REGIME_RANGE_FORMING:
            if regime_age >= 3:
                requested = REGIME_RANGE_ACTIVE
        elif regime in (REGIME_RANGE_ACTIVE, REGIME_RANGE_PRESSURE_UP, REGIME_RANGE_PRESSURE_DOWN):
            if bar.close > prior_high and separation >= 0.32:
                break_direction, requested = 1, REGIME_BREAKOUT_CANDIDATE
            elif bar.close < prior_low and separation <= -0.32:
                break_direction, requested = -1, REGIME_BREAKOUT_CANDIDATE
            else:
                pressure = (bar.close - range_low) / max(range_high - range_low, average_true_range)
                potential = REGIME_RANGE_PRESSURE_UP if pressure >= 0.8 else REGIME_RANGE_PRESSURE_DOWN if pressure <= 0.2 else REGIME_RANGE_ACTIVE
                if regime == REGIME_RANGE_ACTIVE or regime == REGIME_RANGE_PRESSURE_UP and potential != REGIME_RANGE_PRESSURE_DOWN or regime == REGIME_RANGE_PRESSURE_DOWN and potential != REGIME_RANGE_PRESSURE_UP:
                    requested = potential
        elif regime == REGIME_BREAKOUT_CANDIDATE:
            if break_direction == 1 and separation >= 0.32:
                requested = REGIME_UPTREND
            elif break_direction == -1 and separation <= -0.32:
                requested = REGIME_DOWNTREND
            elif abs(separation) < 0.20:
                requested = REGIME_RANGE_ACTIVE
        elif regime == REGIME_UPTREND and separation < 0.20:
            requested = REGIME_TRANSITION
        elif regime == REGIME_DOWNTREND and separation > -0.20:
            requested = REGIME_TRANSITION
        elif regime == REGIME_TRANSITION and regime_age >= 3:
            requested = REGIME_RANGE_FORMING
        if requested != regime:
            regime, regime_age = requested, 0

        warmup = len(snapshots) + 1 >= 21 and average_true_range > 0
        core = (ema_fast + ema_slow) / 2.0 if warmup else None
        width = average_true_range * 0.5 if warmup else None
        closes = [item.close for item in bars[max(0, index - 19):index + 1]]
        basis = sum(closes) / 20 if len(closes) == 20 else None
        deviation = math.sqrt(sum((value - basis) ** 2 for value in closes) / 20) if basis is not None else None
        if index >= 10:
            pivot = bars[index - 5]
            older = bars[index - 10:index - 5]
            newer = bars[index - 4:index + 1]
            if all(pivot.high >= item.high for item in older) and all(pivot.high > item.high for item in newer):
                swing_high = to_ticks(pivot.high)
                swing_high_source, swing_high_confirmation = pivot.timestamp, bar.timestamp
            if all(pivot.low <= item.low for item in older) and all(pivot.low < item.low for item in newer):
                swing_low = to_ticks(pivot.low)
                swing_low_source, swing_low_confirmation = pivot.timestamp, bar.timestamp
        ema200_value = ema200_values[index]
        ema200_slope = ((ema200_value - ema200_values[index - 5]) / average_true_range
                        if index >= 5 and ema200_value is not None and ema200_values[index - 5] is not None else None)
        rsi_value = rsi_values[index]
        rsi_slope = rsi_value - rsi_values[index - 1] if index and rsi_value is not None and rsi_values[index - 1] is not None else None
        volume_mean = sum(volumes[index - 20:index]) / 20.0 if index >= 20 else None
        range_mean = sum(ranges[index - 20:index]) / 20.0 if index >= 20 else None
        valid_volume = volume_mean is not None and volume_mean > 0 and math.isfinite(bar.volume)
        relative_volume = bar.volume / volume_mean if valid_volume else None
        vsa_direction = DIR_BULLISH if bar.close > bar.open else DIR_BEARISH if bar.close < bar.open else DIR_NEUTRAL
        snapshots.append(MarketSnapshot(
            bar.timestamp, ema_fast, ema_slow, average_true_range,
            range_high, range_low, separation, regime, break_direction, warmup,
            core, core - width if warmup else None, core + width if warmup else None,
            swing_high, swing_high_source, swing_high_confirmation,
            swing_low, swing_low_source, swing_low_confirmation,
            basis, basis + 2 * deviation if basis is not None else None,
            basis - 2 * deviation if basis is not None else None,
            (bar.close - range_low) / max(range_high - range_low, average_true_range, 1e-12),
            ema200_value, ema200_slope, rsi_value, rsi_slope, valid_volume,
            vsa_direction, relative_volume is not None and relative_volume <= 0.7))
    return snapshots


def load_bars(db_path, symbol, timeframe, limit=2000):
    with sqlite3.connect(db_path) as db:
        rows = db.execute("""SELECT timestamp, open, high, low, close, volume
            FROM bars WHERE symbol=? AND timeframe=?
            ORDER BY timestamp DESC LIMIT ?""", (symbol, timeframe, limit)).fetchall()
    bars = [Bar(*row) for row in reversed(rows)]
    if len(bars) < limit:
        raise ValueError(f"{timeframe}: need {limit} closed bars, found {len(bars)}")
    if any(b.high < max(b.open, b.close) or b.low > min(b.open, b.close) or b.low > b.high for b in bars):
        raise ValueError(f"{timeframe}: invalid OHLC")
    if any(a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:])):
        raise ValueError(f"{timeframe}: timestamps are not strictly increasing")
    return bars


def sma(values, period):
    out = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values, period):
    seed = sma(values, period)
    out = [None] * len(values)
    alpha = 2.0 / (period + 1.0)
    prior = None
    for i, value in enumerate(values):
        if prior is None:
            prior = seed[i]
        elif value is not None:
            prior = alpha * value + (1.0 - alpha) * prior
        out[i] = prior
    return out


def _ema_optional(values, period):
    out = [None] * len(values)
    alpha, prior = 2.0 / (period + 1.0), None
    for i, value in enumerate(values):
        if value is None:
            continue
        if prior is None:
            window = [item for item in values[:i + 1] if item is not None]
            if len(window) >= period:
                prior = sum(window[-period:]) / period
        else:
            prior = alpha * value + (1.0 - alpha) * prior
        out[i] = prior
    return out


def rma(values, period):
    seed = sma(values, period)
    out = [None] * len(values)
    prior = None
    for i, value in enumerate(values):
        if prior is None:
            prior = seed[i]
        else:
            prior = (prior * (period - 1) + value) / period
        out[i] = prior
    return out


def atr(bars, period=14):
    ranges = []
    for i, bar in enumerate(bars):
        previous_close = bars[i - 1].close if i else bar.close
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
    return rma(ranges, period)


def population_stdev(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        out[i] = math.sqrt(sum((value - mean) ** 2 for value in window) / period)
    return out


def rolling_extreme(values, period, highest=True):
    out = [None] * len(values)
    select = max if highest else min
    for i in range(period - 1, len(values)):
        out[i] = select(values[i - period + 1:i + 1])
    return out


def to_ticks(price, tick_size=0.01):
    return int((Decimal(str(price)) / Decimal(str(tick_size))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_price(ticks, tick_size=0.01):
    return ticks * tick_size
