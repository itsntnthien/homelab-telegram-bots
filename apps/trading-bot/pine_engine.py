"""Bounded incremental calculation of the frozen user Pine's signal decisions."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import math

from market_quality import utc_timestamp
from pine_config import PineConfig, SECONDS, ENGINE_VERSION
from pine_execution import PineExecution, context_authorized
from signal_engine import (Bar, CloudFrame, CloudMachine, DisplayFilter, PolarityDisplay,
                           TIMEFRAME_CONTEXT, _score, _grade)


def clamp(x, low=0.0, high=1.0):
    return max(low, min(high, x))


class PineEngine:
    def __init__(self, timeframe, config=None, state=None):
        self.tf = timeframe.lower()
        self.config = config or PineConfig()
        if state and (state.get("config_hash") != self.config.hash or state.get("engine") != ENGINE_VERSION):
            raise ValueError("incompatible engine state; explicit migration required")
        self.s = deepcopy(state) if state else {
            "engine": ENGINE_VERSION, "config_hash": self.config.hash,
            "bars": [], "count": 0, "last_close": None,
            "fast": None, "slow": None, "atr": None, "previous_close": None,
            "regime": 1, "regime_age": 0, "break_direction": 0,
            "high_pivot": None, "low_pivot": None, "high_used": False, "low_used": False,
            "trend": 0, "latest_event": 0, "sequence": 0,
            "liquidity": [], "gaps": [], "blocks": [], "candidates": {},
            "bias": 0, "pending_bias": 0, "bias_count": 0, "buckets": {},
            "ribbon_fast": None, "ribbon_slow": None, "slow_history": [],
            "sigmas": [], "sigma_baseline": None,
            "core_history": [], "micro": 0, "zones": None,
            "last_context": None,
        }
        self.execution = PineExecution(self.tf, self.config, self.s.pop("execution", None))
        self.cloud = CloudMachine()
        self.polarity = PolarityDisplay()
        self.upper = DisplayFilter()
        self.lower = DisplayFilter()
        for name in ("cloud", "polarity", "upper", "lower"):
            getattr(self, name).__dict__.update(self.s.pop(name, {}))

    def snapshot(self):
        state = deepcopy(self.s)
        state["execution"] = self.execution.snapshot()
        for name in ("cloud", "polarity", "upper", "lower"):
            state[name] = deepcopy(getattr(self, name).__dict__)
        return state

    def _htf(self, bar):
        s = self.s
        stamp = int(utc_timestamp(bar.timestamp).timestamp())
        source, confirmation, anchor = TIMEFRAME_CONTEXT[self.tf]
        committed = False
        for name, minutes in (("confirmation", confirmation), ("anchor", anchor)):
            width = minutes * 60
            start = stamp - stamp % width
            bucket = s["buckets"].get(name)
            if bucket is not None and start > bucket["start"]:
                complete = (bucket["valid"] and bucket["count"] == minutes // source and
                            bucket["last"] == bucket["start"] + width - source * 60)
                if complete:
                    s[name + "_bar"] = dict(bucket)
                    if name == "confirmation":
                        committed = True
            if bucket is None or start > bucket["start"]:
                s["buckets"][name] = dict(start=start, count=1, last=stamp,
                                         valid=stamp == start, open=bar.open, close=bar.close)
            else:
                bucket["valid"] &= stamp - bucket["last"] == source * 60 and start == bucket["start"]
                bucket["count"] += 1
                bucket["last"], bucket["close"] = stamp, bar.close
        next_bias = s["bias"]
        if committed:
            c, a = s["confirmation_bar"], s.get("anchor_bar")
            score = (1 if c["close"] > c["open"] else -1) + (0 if a is None else 1 if a["close"] > a["open"] else -1)
            target = 1 if score > .1 else -1 if score < -.1 else 0
            s["bias_count"] = s["bias_count"] + 1 if target == s["pending_bias"] else 1
            s["pending_bias"] = target
            if s["bias_count"] >= 2:
                next_bias = target
        return next_bias

    def _market(self, bar):
        s, tick = self.s, self.config.ticks
        previous = s["previous_close"]
        tr = bar.high - bar.low if previous is None else max(bar.high-bar.low, abs(bar.high-previous), abs(bar.low-previous))
        s["fast"] = bar.close if s["fast"] is None else s["fast"] + 2 / 9 * (bar.close-s["fast"])
        s["slow"] = bar.close if s["slow"] is None else s["slow"] + 2 / 22 * (bar.close-s["slow"])
        s["atr"] = max(tr if s["atr"] is None else s["atr"] + (tr-s["atr"]) / 14, 1e-12)
        history = s["bars"]
        prior_high = max(b["high"] for b in history[-20:]) if history else bar.high
        prior_low = min(b["low"] for b in history[-20:]) if history else bar.low
        history.append(asdict(bar))
        del history[:-52]
        hi, lo = max(b["high"] for b in history[-20:]), min(b["low"] for b in history[-20:])
        s["previous_close"] = bar.close
        s["count"] += 1
        s["regime_age"] += 1
        separation = (s["fast"]-s["slow"]) / s["atr"]
        pressure = (bar.close-lo) / max(hi-lo, s["atr"])
        regime = requested = s["regime"]
        if regime == 1 and s["regime_age"] >= 3:
            requested = 2
        elif regime in (2, 3, 4):
            if bar.close > prior_high and separation >= .32:
                s["break_direction"], requested = 1, 6
            elif bar.close < prior_low and separation <= -.32:
                s["break_direction"], requested = -1, 6
            else:
                potential = 3 if pressure >= .8 else 4 if pressure <= .2 else 2
                if regime == 2 or regime == 3 and potential != 4 or regime == 4 and potential != 3:
                    requested = potential
        elif regime == 6:
            if s["break_direction"] == 1 and separation >= .32:
                requested = 7
            elif s["break_direction"] == -1 and separation <= -.32:
                requested = 8
            elif abs(separation) < .32-.12:
                requested = 2
        elif regime == 7 and separation < .32-.12 or regime == 8 and separation > -(.32-.12):
            requested = 5
        elif regime == 5 and s["regime_age"] >= 3:
            requested = 1
        if regime != requested:
            s["regime"], s["regime_age"] = requested, 0
        closes = [b["close"] for b in history[-20:]]
        basis = sum(closes) / 20 if len(closes) == 20 else None
        sigma = math.sqrt(sum((v-basis)**2 for v in closes)/20) if basis is not None else None
        return dict(high=hi, low=lo, basis=basis, sigma=sigma,
                    upper=basis+2*sigma if basis is not None else None,
                    lower=basis-2*sigma if basis is not None else None,
                    separation=separation, pressure=pressure,
                    atr=tick(s["atr"]), warm=s["count"] >= 21,
                    core=(s["fast"]+s["slow"])/2, width=s["atr"]*.5)

    def _structure(self, bar):
        s, tick = self.s, self.config.ticks
        high, low, close = tick(bar.high), tick(bar.low), tick(bar.close)
        index, bars = s["count"], s["bars"]
        prior_high, prior_low = s["high_pivot"], s["low_pivot"]
        for key, age in (("liquidity", 500), ("gaps", 250), ("blocks", 250)):
            s[key] = [x for x in s[key] if not x.get("terminal") and
                      (index-x["bar"] <= age or key != "liquidity" and x.get("mitigated"))]
        for pool in s["liquidity"]:
            upper = pool["side"] == -1
            if close > pool["level"] if upper else close < pool["level"]:
                pool["terminal"] = True
            elif not pool["swept"] and (high > pool["level"] if upper else low < pool["level"]):
                pool["swept"] = True
        for key in ("gaps", "blocks"):
            for item in s[key]:
                if close < item["bottom"] if item["side"] == 1 else close > item["top"]:
                    item["terminal"] = True
                elif key == "gaps" and (low <= item["bottom"] if item["side"] == 1 else high >= item["top"]):
                    item["terminal"] = True
                elif high >= item["bottom"] and low <= item["top"]:
                    item["mitigated"] = True
        gap_side = 0
        if len(bars) >= 3:
            older, previous = bars[-3], bars[-2]
            if low > tick(older["high"]) and tick(previous["high"]) > tick(older["high"]):
                gap_side, top, bottom = 1, low, tick(older["high"])
            elif high < tick(older["low"]) and tick(previous["low"]) < tick(older["low"]):
                gap_side, top, bottom = -1, tick(older["low"]), high
            if gap_side:
                s["sequence"] += 1
                s["gaps"].append(dict(side=gap_side, id=s["sequence"], bar=index, top=top,
                                      bottom=bottom, mitigated=False, terminal=False))
        new_high = new_low = False
        if len(bars) >= 11:
            pivot, older, newer = bars[-6], bars[-11:-6], bars[-5:]
            ph, pl = tick(pivot["high"]), tick(pivot["low"])
            new_high = all(ph >= tick(b["high"]) for b in older) and all(ph > tick(b["high"]) for b in newer)
            new_low = all(pl <= tick(b["low"]) for b in older) and all(pl < tick(b["low"]) for b in newer)
            if new_high:
                s["high_pivot"] = ph
            if new_low:
                s["low_pivot"] = pl
        for side, exists, level in ((1, new_low, s["low_pivot"]), (-1, new_high, s["high_pivot"])):
            if exists:
                match = next((p for p in s["liquidity"] if not p["terminal"] and
                              p["side"] == side and abs(level-p["level"]) <= 1), None)
                if match:
                    match["level"] = max(level, match["level"]) if side == -1 else min(level, match["level"])
                else:
                    s["sequence"] += 1
                    s["liquidity"].append(dict(side=side, id=s["sequence"], bar=index,
                                               level=level, swept=False, terminal=False))
        bull = prior_high is not None and not s["high_used"] and close > prior_high
        bear = prior_low is not None and not s["low_used"] and close < prior_low
        event, broken = 0, None
        if bull != bear:
            if bull:
                event, broken = (2 if s["trend"] == -1 else 1), prior_high
                s["trend"], s["high_used"] = 1, True
            else:
                event, broken = (-2 if s["trend"] == 1 else -1), prior_low
                s["trend"], s["low_used"] = -1, True
        if new_high:
            s["high_used"] = False
        if new_low:
            s["low_used"] = False
        if event:
            s["latest_event"] = event
        if event and gap_side == (1 if event > 0 else -1):
            for source in reversed(bars[-21:-1]):
                if source["close"] < source["open"] if gap_side == 1 else source["close"] > source["open"]:
                    s["sequence"] += 1
                    s["blocks"].append(dict(side=gap_side, id=s["sequence"], bar=index,
                                            top=tick(source["high"]), bottom=tick(source["low"]),
                                            mitigated=False, terminal=False))
                    break
        for key, capacity in (("liquidity", 8), ("gaps", 5), ("blocks", 5)):
            s[key] = [p for p in s[key] if not p["terminal"]][-capacity:]
        return event, broken

    def _evidence(self, bar, m):
        s, bars = self.s, self.s["bars"]
        for name, period in (("ribbon_fast", 9), ("ribbon_slow", 21)):
            if s[name] is None and len(bars) >= period:
                s[name] = sum(b["close"] for b in bars[-period:]) / period
            elif s[name] is not None:
                alpha = 2/(period+1)
                s[name] = alpha*bar.close+(1-alpha)*s[name]
        s["slow_history"].append(s["ribbon_slow"])
        del s["slow_history"][:-4]
        if m["sigma"] is not None:
            s["sigmas"].append(m["sigma"])
            del s["sigmas"][:-20]
            if s["sigma_baseline"] is None and len(s["sigmas"]) == 20:
                s["sigma_baseline"] = sum(s["sigmas"])/20
            elif s["sigma_baseline"] is not None:
                s["sigma_baseline"] = 2/21*m["sigma"]+19/21*s["sigma_baseline"]
        slow = s["slow_history"]
        valid = len(slow) == 4 and all(v is not None for v in slow) and s["sigma_baseline"] is not None
        if not valid:
            return False, None, 0., 0., 0., 0.
        norm = max(m["sigma"], .25*m["width"], self.config.tick_size)
        sep = clamp((s["ribbon_fast"]-s["ribbon_slow"])/norm, -1, 1)
        slope = clamp(abs(slow[-1]-slow[0])/(3*norm))
        bp = sum(b > a for a, b in zip(slow, slow[1:]))/3
        sp = sum(b < a for a, b in zip(slow, slow[1:]))/3
        quality = clamp((m["sigma"]/max(s["sigma_baseline"], self.config.tick_size)-.75)/.75)
        mid = lambda n: (max(b["high"] for b in bars[-n:])+min(b["low"] for b in bars[-n:]))/2
        tenkan, kijun, spanb = mid(9), mid(26), mid(52)
        spana = (tenkan+kijun)/2
        bi = ((tenkan > kijun)+(bar.close > kijun)+(spana > spanb))/3
        si = ((tenkan < kijun)+(bar.close < kijun)+(spana < spanb))/3
        bull, bear = max(0., sep), max(0., -sep)
        bc = clamp(.30*bull+.25*(.60*slope+.40*bp)+.20*quality+.25*bi)
        sc = clamp(.30*bear+.25*(.60*slope+.40*sp)+.20*quality+.25*si)
        return True, quality, .40*bull+.30*bp+.30*bi, .40*bear+.30*sp+.30*si, bc, sc

    def _strength(self, bar, m):
        s = self.s
        if not m["warm"]:
            return 0
        core, width, regime, micro = m["core"], m["width"], s["regime"], s["micro"]
        direction = 1 if regime in (7, 3) else -1 if regime in (8, 4) else micro
        cores = s["core_history"]
        deltas = [core-cores[-1], cores[-1]-cores[-2], cores[-2]-cores[-3]] if len(cores) == 3 else []
        aligned = sum(d > 0 if direction == 1 else d < 0 for d in deltas) if direction else 0
        slope_points = (0, 8, 17, 25)[aligned]
        side = bar.close > core if direction == 1 else bar.close < core if direction == -1 else False
        displacement = abs(bar.close-core)/width
        dp = (20 if displacement >= 1 else 15 if displacement >= .75 else 10 if displacement >= .5 else 5 if displacement >= .25 else 0) if side else 0
        trend = regime in (7, 8)
        regime_points = 20 if trend else 14 if regime in (3, 4) else 7 if regime == 2 and direction and direction == micro else 0
        aligned_structure = s["latest_event"] in ((1, 2) if direction == 1 else (-1, -2)) if direction else False
        conflict = s["latest_event"] in ((-1, -2) if direction == 1 else (1, 2)) if direction else False
        micro_aligned = direction != 0 and direction == micro
        chop = regime in (2, 3, 4) and micro == 0 and m["low"]+(m["high"]-m["low"])*.35 < bar.close < m["high"]-(m["high"]-m["low"])*.35
        clean = 0 if conflict or chop else 20 if aligned_structure and micro_aligned else 10 if aligned_structure or micro_aligned else 0
        selected = regime not in (1, 5, 6)
        overlap = selected and bar.high >= core-width and bar.low <= core+width
        confirmation = overlap and (bar.close > core+width if direction == 1 else bar.close < core-width if direction == -1 else False)
        veto = conflict or chop or (micro == -direction if direction else True) or self.cloud.state in (3, 4)
        interaction = 0 if not selected or veto else 15 if overlap and confirmation else 10 if overlap and side else 5 if not overlap else 0
        strength = int(clamp(slope_points+dp+regime_points+clean+interaction, 0, 100))
        return strength

    def _cloud(self, bar, m):
        s = self.s
        valid, quality, bull, bear, bc, sc = self._evidence(bar, m)
        if not m["warm"]:
            self.polarity.step(None, 0, 0, 0, 0, 0, 0, s["regime"], 0, False)
            return 0, None
        core, width, regime, micro = m["core"], m["width"], s["regime"], s["micro"]
        strength = self._strength(bar, m)
        trend, selected, cores = regime in (7, 8), regime not in (1, 5, 6), s["core_history"]
        if selected:
            cores.append(core)
            del cores[:-3]
        previous_zones = s["zones"]
        width_na = s.get("polarity_width_na", False) or quality is None
        # The supplied Pine propagates na width from the unseeded sigma baseline.
        # Only its reversal pinch resets that width; do not silently repair it here.
        if self.polarity.center is not None and self.polarity.half_width is None:
            self.polarity.half_width = self.polarity.width_velocity = 0.
        polarity = self.polarity.step(core, width, quality or 0., bull, bear, bc, sc, regime, micro, valid)
        if self.polarity.recorded:
            width_na = False
        s["polarity_width_na"] = width_na
        if width_na:
            self.polarity.half_width = self.polarity.width_velocity = None
            polarity = None
        expansion = 1.25*strength/100*width if trend else 0.
        up_shift = max(0., core+width-(m["high"]-width)) if selected else 0.
        low_shift = max(0., m["low"]+width-(core-width)) if selected else 0.
        upper = self.upper.step(m["high"]-width+up_shift+expansion, m["high"]+width+up_shift+expansion, width)
        lower = self.lower.step(m["low"]-width-low_shift-expansion, m["low"]+width-low_shift-expansion, width)
        upper_move = max(0., max(bar.high+.1*width, polarity[1]+.1*width)-upper[0]) if polarity else 0.
        lower_move = max(0., lower[1]-min(bar.low-.1*width, polarity[0]-.1*width)) if polarity else 0.
        upper = tuple(v+upper_move for v in upper)
        lower = tuple(v-lower_move for v in lower)
        if upper_move:
            self.upper.center, self.upper.center_velocity = sum(upper)/2, 0.
        if lower_move:
            self.lower.center, self.lower.center_velocity = sum(lower)/2, 0.
        polarity_slope = self.polarity.center-s["previous_polarity_center"] if polarity and s.get("previous_polarity_center") is not None else 0.
        s["micro"] = 1 if polarity and bar.close > polarity[1] and polarity_slope > 0 else -1 if polarity and bar.close < polarity[0] and polarity_slope < 0 else 0
        s["previous_polarity_center"] = self.polarity.center if polarity else None
        outcome = None
        if previous_zones:
            p, u, l, previous_close = previous_zones
            epoch = self.cloud.epoch
            result = self.cloud.step(CloudFrame(bar.timestamp, regime, bar.high, bar.low, bar.close,
                                                previous_close, width, (m["high"]+m["low"])/2,
                                                s["micro"], polarity_slope, p[0], p[1], u[0], u[1],
                                                l[0], l[1], s["latest_event"],
                                                polarity[0] if polarity else None, polarity[1] if polarity else None))
            if result.epoch != epoch:
                outcome = result
        s["zones"] = [polarity, upper, lower, bar.close] if polarity else None
        return strength, outcome

    def _candidates(self, bar, m, event, broken):
        s, tick = self.s, self.config.ticks
        basis, upper, lower, regime, trend = m["basis"], m["upper"], m["lower"], s["regime"], s["trend"]
        if basis is None:
            return []
        bulltrend = regime in (7, 3) or trend == 1 or m["separation"] > 0
        beartrend = regime in (8, 4) or trend == -1 or m["separation"] < 0
        pullback = self.s["atr"]*.5
        bullcont = 102 if bulltrend and bar.low < basis < bar.close and bar.close > bar.open else 101 if bulltrend and bar.low <= basis+pullback and bar.close >= basis and bar.close >= bar.open else None
        bearcont = 102 if beartrend and bar.high > basis > bar.close and bar.close < bar.open else 101 if beartrend and bar.high >= basis-pullback and bar.close <= basis and bar.close <= bar.open else None
        bullbreak = 201 if event in (1, 2) else 202 if regime == 6 and s["break_direction"] == 1 or bar.close > upper else None
        bearbreak = 201 if event in (-1, -2) else 202 if regime == 6 and s["break_direction"] == -1 or bar.close < lower else None
        bullrev = 301 if event == 2 else 302 if s["low_pivot"] is not None and tick(bar.low) < s["low_pivot"] and bar.close > bar.open else None
        bearrev = 301 if event == -2 else 302 if s["high_pivot"] is not None and tick(bar.high) > s["high_pivot"] and bar.close < bar.open else None
        entry, op, hi, lo, atr = tick(bar.close), tick(bar.open), tick(bar.high), tick(bar.low), m["atr"]
        candidates = []
        for slot, detected in enumerate((bullcont, bearcont, bullbreak, bearbreak, bullrev, bearrev)):
            key, family, direction = str(slot), slot//2+1, 1 if slot%2 == 0 else -1
            c = s["candidates"].get(key)
            if detected is not None:
                anchor = broken if detected == 201 else tick(upper if direction == 1 else lower) if detected == 202 else None
                c = dict(subtype=detected, anchor=anchor, state="DETECTED")
            elif c:
                if c["state"] in ("CONFIRMED", "ARBITRATED"):
                    continue
                if c["state"] in ("DETECTED", "ELIGIBLE"):
                    opposed = regime in ((8, 4) if direction == 1 else (7, 3))
                    if family != 1 or opposed:
                        s["candidates"].pop(key, None)
                        continue
            if c is None:
                continue
            subtype = c["subtype"]
            if c["anchor"] is None:
                c["anchor"] = (lo if direction == 1 else hi) if subtype == 101 else tick(basis) if subtype == 102 else (s["low_pivot"] if direction == 1 else s["high_pivot"]) if subtype == 302 else broken
            anchor = c["anchor"]
            band = tick(upper if direction == 1 else lower) if family == 2 else None
            missing = anchor is None or atr <= 0
            numeric = not missing and hi > lo
            structural = numeric and (entry < anchor if direction == 1 else entry > anchor) if subtype in (101, 102, 302) else numeric and (entry <= anchor if direction == 1 else entry >= anchor)
            conflict = s["bias"] in (-direction, 2) or trend == -direction
            cell = (2 if regime == 6 else 1 if regime in (1, 2, 5) else 0) if family == 1 else (2 if regime == 1 else 1 if regime in (2, 7, 8) else 0) if family == 2 else (1 if regime in (3, 4, 6, 7, 8) else 0)
            veto = int(missing)+2*int(conflict)+4*int(cell == 2)+8*int(structural)+16*int(not missing and not numeric)
            ema_direction = 1 if m["separation"] > 0 else -1 if m["separation"] < 0 else 0
            trigger = (2 if trend == direction or ema_direction == direction else 1) if subtype in (101, 102) else (2 if event == direction else 1) if subtype == 201 else (2 if regime == 6 else 1) if subtype == 202 else (2 if event == direction*2 else 1) if subtype == 301 else 2
            confluence = int(subtype == 302)+int(any(g["side"] == direction for g in s["gaps"]))+int(any(o["side"] == direction for o in s["blocks"]))
            setup = _score(30*(100 if s["bias"] == direction else 50 if s["bias"] == 0 else 0),
                           25*(100 if trend == direction else 50 if trend == 0 else 0),
                           20*(100 if cell == 0 else 50 if cell == 1 else 0),
                           15*(100 if trigger == 2 else 50), 10*(100 if confluence >= 2 else 50 if confluence == 1 else 0),
                           effect=-10 if cell == 1 else 0)
            location = tick(basis) if family == 1 else band if family == 2 else anchor
            position = ((entry-lo if direction == 1 else hi-entry)*10000+(hi-lo)//2)//(hi-lo) if numeric else 0
            entry_quality = _score(35*(50 if entry == op else 100 if (entry > op) == (direction == 1) else 0),
                                   35*(50 if entry == location else 100 if location is not None and (entry > location) == (direction == 1) else 0),
                                   30*(100 if position >= 6667 else 50 if position >= 3333 else 0))
            total = max(0, min(100, (setup*60+entry_quality*40+50)//100))
            grade = _grade(setup, entry_quality, total, veto)
            c["state"] = "VETOED" if veto else "ELIGIBLE" if grade in (2, 3) else "GRADE_REJECTED"
            s["candidates"][key] = c
            if veto or grade not in (2, 3):
                continue
            sl = anchor if anchor is not None and (entry-anchor)*direction > 0 else entry-direction*atr
            risk = (entry-sl)*direction
            tp = entry+direction*risk*2
            pools = [p for p in s["liquidity"] if not p["swept"] and p["bar"] < s["count"] and p["side"] == -direction and 0 < (p["level"]-entry)*direction <= risk*2]
            selected = min(pools, key=lambda p: ((p["level"]-entry)*direction, p["id"])) if pools else None
            tp = selected["level"] if selected else tp
            rr = (tp-entry)*direction*1000//risk if risk > 0 else 0
            if rr < 1200:
                continue
            candidates.append(dict(slot=slot, direction=direction, family=family, subtype=subtype,
                                   quality=total, grade=grade, setup_quality=setup, entry_quality=entry_quality,
                                   entry=entry, sl=sl, tp=tp, atr=atr, rr=rr, regime=regime,
                                   target_id=selected["id"] if selected else None,
                                   key=f"XAUUSD:{self.tf}:{self.config.hash}:{bar.timestamp}:{slot}"))
        return candidates

    def close_bar(self, bar, historical=False):
        at = utc_timestamp(bar.timestamp)
        close_at = (at+timedelta(seconds=SECONDS[self.tf])).isoformat()
        if self.s["last_close"] and utc_timestamp(close_at) <= utc_timestamp(self.s["last_close"]):
            return [], None
        if not all(math.isfinite(v) for v in (bar.open, bar.high, bar.low, bar.close)) or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("invalid closed OHLC")
        if int(at.timestamp()) % SECONDS[self.tf]:
            raise ValueError("unaligned closed bar")
        events = []
        if historical:
            events += self.execution.on_bar_open(at.isoformat(), self.config.ticks(bar.open))
            deferred = self.execution.state["deferred"]
            if deferred:
                touched = bar.low <= self.config.price(deferred["entry"]) if deferred["direction"] == 1 else bar.high >= self.config.price(deferred["entry"])
                if touched:
                    rr, valid = self.execution._rr(deferred, deferred["entry"])
                    self.execution.state["deferred"] = None
                    if valid:
                        events += self.execution._open(deferred, deferred["entry"], rr, close_at, deferred=True)
            events += self.execution.observe_range(close_at, self.config.ticks(bar.high), self.config.ticks(bar.low))
        events += self.execution.expire_bar(close_at)
        next_bias = self._htf(bar)
        m = self._market(bar)
        event, broken = self._structure(bar)
        candidates = self._candidates(bar, m, event, broken)
        strength, outcome = self._cloud(bar, m)
        context = dict(regime=self.s["regime"], strength=strength,
                       structure_event=self.s["latest_event"], break_direction=self.s["break_direction"])
        authorized = [c for c in candidates if context_authorized(self.tf, c, context)]
        winner = max(authorized, key=lambda c: (c["grade"], c["quality"], -c["family"], -c["slot"]), default=None)
        for c in candidates:
            self.s["candidates"][str(c["slot"])]["state"] = "CONFIRMED" if c is winner else "ARBITRATED"
        if m["warm"]:
            self.execution.arm(winner, context, close_at)
        else:
            self.execution.state["context"] = context
        events += self.execution.cloud_event(outcome, close_at, self.config.ticks(bar.close))
        self.s["last_close"], self.s["bias"] = close_at, next_bias
        self.s["last_context"] = context
        diagnostics = dict(bar_time=at.isoformat(), close_time=close_at, regime=self.s["regime"],
                           bias=next_bias, strength=strength, winner=winner,
                           cloud=asdict(outcome) if outcome else None)
        return events, diagnostics

    def _intrabar_context(self, timestamp, bid):
        s = self.s
        if not s["count"] or s["fast"] is None:
            raise ValueError("intrabar context requires committed features")
        width = SECONDS[self.tf]
        start = int(utc_timestamp(timestamp).timestamp()) // width * width
        live = s.get("live_bar")
        if live is None or live["bucket"] != start:
            live = dict(bucket=start, open=bid, high=bid, low=bid, close=bid)
            s["live_bar"] = live
        else:
            live.update(high=max(live["high"], bid), low=min(live["low"], bid), close=bid)
        prior = s["bars"][-19:]
        m = dict(core=(s["fast"]+s["slow"])/2, width=s["atr"]*.5, warm=s["count"] >= 21,
                 high=max([b["high"] for b in prior]+[live["high"]]),
                 low=min([b["low"] for b in prior]+[live["low"]]))
        bar = Bar(timestamp, live["open"], live["high"], live["low"], live["close"], 0)
        return dict(regime=s["regime"], strength=self._strength(bar, m),
                    structure_event=s["latest_event"], break_direction=s["break_direction"])

    def tick(self, timestamp, bid, ask):
        at = utc_timestamp(timestamp)
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
            raise ValueError("invalid bid/ask")
        last = self.execution.state["last_tick"]
        if last and at < utc_timestamp(last):
            raise ValueError("tick time moved backwards")
        seconds = SECONDS[self.tf]
        start = int(at.timestamp()) // seconds * seconds
        bar_time = at.fromtimestamp(start, at.tzinfo).isoformat()
        if not self.s["last_close"] or utc_timestamp(self.s["last_close"]) < utc_timestamp(bar_time):
            raise ValueError("closed-bar context not yet available")
        self.execution.state["context"] = self._intrabar_context(timestamp, bid)
        events = self.execution.on_bar_open(bar_time, self.config.ticks(bid))
        for event in events:
            if event["event_type"] == "PRIMARY_OPEN":
                event["event_time"] = at.isoformat()
                event["eligible_at"] = at.isoformat()
        events += self.execution.on_tick(at.isoformat(), self.config.ticks(bid), self.config.ticks(ask))
        return events
