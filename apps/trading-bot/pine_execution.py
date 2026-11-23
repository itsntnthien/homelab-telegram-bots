"""Next-bar/one-bar-retest execution for the frozen Pine contract.

OHLC replay resolves an ambiguous exit stop-first. Live observations preserve
tick order and carry their own event time; neither mode reads future prices.
"""
from copy import deepcopy
from datetime import timedelta
from market_quality import utc_timestamp
from pine_config import PineConfig, SECONDS


def context_authorized(tf, c, context):
    family, subtype, direction = c["family"], c["subtype"], c["direction"]
    regime, strength = context["regime"], context["strength"]
    if tf in ("m1", "m5") and family == 3 or tf == "m15" and family != 3:
        return False
    if (tf == "m3" and subtype == 101 and regime in (3, 4) or
            tf == "m5" and family == 1 and c["grade"] == 3 and regime in (7, 8) or
            tf == "m15" and subtype == 301):
        return False
    aligned = context["structure_event"] in ((1, 2) if direction == 1 else (-1, -2))
    strict = regime == (7 if direction == 1 else 8)
    pressure = regime == (3 if direction == 1 else 4)
    if family == 1:
        return (strict or pressure) and strength >= (80 if subtype == 102 else 40)
    if family == 2:
        return strength >= 55 and (aligned if subtype == 201 else
                                  regime == 6 and context["break_direction"] == direction)
    opposite = regime == (8 if direction == 1 else 7) and strength >= 70
    structural = subtype == 301 and aligned
    return c["quality"] >= 85 and (structural or subtype == 302) and (not opposite or structural)


class PineExecution:
    def __init__(self, timeframe, config=None, state=None):
        if timeframe not in SECONDS:
            raise ValueError("unsupported execution timeframe")
        self.tf = timeframe
        self.config = config or PineConfig()
        self.state = deepcopy(state) if state is not None else {
            "pending": None, "deferred": None, "campaign": None,
            "accepted": [], "bar": None, "bar_number": 0, "last_tick": None,
            "context": None, "cloud_outcomes": [], "cloud_epoch": None,
        }

    def snapshot(self):
        return deepcopy(self.state)

    def _rr(self, c, entry):
        risk = (entry - c["sl"]) * c["direction"]
        reward = (c["tp"] - entry) * c["direction"]
        net_risk, net_reward = risk + self.config.spread_ticks, reward - self.config.spread_ticks
        rr = net_reward * 1000 // net_risk if net_risk > 0 and net_reward > 0 else 0
        limit = {"m1": 250, "m3": 200, "m15": 250}.get(self.tf, 1000)
        cost = self.tf == "m5" or risk > 0 and self.config.spread_ticks * 1000 <= risk * limit
        return rr, risk > 0 and reward > 0 and cost and rr >= self.config.minimum_rr_milli

    def _score(self, c, rr):
        return c["quality"] * 100 + c["grade"] * 500 + min(3000, rr)

    def _same_thesis(self, campaign, c):
        same_target = (campaign.get("target_id") == c.get("target_id")
                       if campaign.get("target_id") is not None and c.get("target_id") is not None
                       else campaign["tp"] == c["tp"])
        return (campaign["direction"] == c["direction"] and campaign["family"] == c["family"]
                and campaign["subtype"] == c["subtype"] and campaign["sl"] == c["sl"]
                and campaign["regime"] == c["regime"] and same_target)

    def _campaign_authorized(self, c, entry, rr):
        campaign = self.state["campaign"]
        if not campaign or not campaign["active"] or campaign["direction"] != c["direction"]:
            return True
        score = self._score(c, rr)
        same = self._same_thesis(campaign, c)
        if same:
            return score >= campaign["score"] + 500
        if self.tf not in ("m5", "m15"):
            return False
        direction, atr = c["direction"], c["atr"]
        favorable = (campaign["best"] - campaign["entry"]) * direction
        profit = (entry - campaign["entry"]) * direction
        pullback = (campaign["best"] - entry) * direction
        advance = (c["sl"] - campaign["sl"]) * direction
        context = self.state["context"]
        strict = context["regime"] == (7 if direction == 1 else 8)
        protected = favorable >= campaign["risk"] and profit * 2 >= campaign["risk"]
        reset = pullback * 2 >= campaign["risk"]
        material = atr > 0 and advance * 1000 >= atr * 250
        tolerant = score >= campaign["score"] - 1500
        continuation = c["family"] == 1 and c["subtype"] == 101 and strict
        protected_reset = (continuation and context["strength"] >= 70 and protected and reset
                           and advance >= 0 and score >= campaign["score"])
        structural_reset = (continuation and context["strength"] >= 80 and material and
                            pullback * 1000 >= atr * 500 and tolerant)
        breakout_reset = c["family"] == 2 and c["subtype"] == 201 and context["strength"] >= 55 and material and tolerant
        return rr >= self.config.minimum_rr_milli and (protected_reset or structural_reset or breakout_reset)

    def arm(self, candidate, context, confirmation_time):
        self.state["context"] = dict(context)
        if not candidate or not context_authorized(self.tf, candidate, context):
            return
        c = dict(candidate)
        rr, _ = self._rr(c, c["entry"])
        # Pine checks gross B04 RR while arming and net RR at actual execution.
        if not self._campaign_authorized(c, c["entry"], c["rr"]):
            return
        c["confirmed_at"] = confirmation_time
        c["execution_bar"] = utc_timestamp(confirmation_time).isoformat()
        self.state["pending"] = c

    def _density_ready(self, c, rr):
        number = self.state["bar_number"]
        window = {"m1": 10, "m3": 12, "m5": 8}.get(self.tf, 6)
        accepted = [e for e in self.state["accepted"] if e["bar"] >= number - window + 1]
        self.state["accepted"] = accepted
        if len(accepted) >= 3:
            return False
        if not accepted:
            return True
        last = accepted[-1]
        return (number - last["bar"] >= (3 if self.tf == "m1" else 2) or
                self._score(c, rr) >= last["score"] + 500 or last["direction"] != c["direction"])

    def _event(self, c, kind, timestamp, **extra):
        return dict(event_key=f"{c['key']}:{kind}:{timestamp}", event_type=kind,
                    event_time=timestamp, confirmation_time=c.get("confirmed_at"),
                    timeframe=self.tf, **extra)

    def _open(self, c, entry, rr, timestamp, deferred=False):
        previous = self.state["campaign"]
        events = []
        if previous and previous["active"]:
            previous["active"] = False
            events.append(self._event(previous, "SUPERSEDED", timestamp, campaign_key=previous["key"]))
        campaign = dict(c, entry=entry, best=entry, risk=abs(entry - c["sl"]),
                        score=self._score(c, rr), active=True)
        self.state["campaign"] = campaign
        self.state["accepted"].append({"bar": self.state["bar_number"],
                                       "score": campaign["score"], "direction": c["direction"]})
        events.append(self._event(c, "PRIMARY_OPEN", timestamp, entry_ticks=entry,
                                 sl_ticks=c["sl"], tp_ticks=c["tp"], rr_milli=rr,
                                 direction=c["direction"], family=c["family"], subtype=c["subtype"],
                                 grade=c["grade"], quality=c["quality"], campaign_key=c["key"],
                                 deferred=deferred))
        return events

    def on_bar_open(self, timestamp, open_ticks):
        at = utc_timestamp(timestamp).isoformat()
        if self.state["bar"] == at:
            return []
        if self.state["bar"] and utc_timestamp(at) <= utc_timestamp(self.state["bar"]):
            raise ValueError("bar time moved backwards")
        events = []
        old = self.state["deferred"]
        if old:
            events.append(self._event(old, "SETUP_EXPIRED", at))
        self.state["deferred"] = None
        self.state["bar"] = at
        self.state["bar_number"] += 1
        c = self.state["pending"]
        self.state["pending"] = None
        if not c:
            return events
        if utc_timestamp(c["execution_bar"]) != utc_timestamp(at):
            return events + [self._event(c, "SETUP_EXPIRED", at)]
        rr, price_valid = self._rr(c, open_ticks)
        reached = (open_ticks - c["entry"]) * c["direction"] <= 0
        chase = max(0, (open_ticks - c["entry"]) * c["direction"])
        campaign_ok = self._campaign_authorized(c, open_ticks, rr)
        density = self._density_ready(c, rr)
        if reached and price_valid and chase <= int(c["atr"] * self.config.maximum_chase_atr) and campaign_ok and density:
            return events + self._open(c, open_ticks, rr, at)
        boundary_rr, boundary_valid = self._rr(c, c["entry"])
        if (not reached and campaign_ok and density and boundary_valid and
                context_authorized(self.tf, c, self.state["context"])):
            self.state["deferred"] = c
        else:
            events.append(self._event(c, "SETUP_CANCELLED", at,
                                     reason="PRICE_COST_CONTEXT_CAMPAIGN_OR_DENSITY"))
        return events

    def on_tick(self, timestamp, bid_ticks, ask_ticks):
        at = utc_timestamp(timestamp)
        if bid_ticks > ask_ticks:
            raise ValueError("crossed quote")
        last = self.state["last_tick"]
        if last and at < utc_timestamp(last):
            raise ValueError("tick time moved backwards")
        self.state["last_tick"] = at.isoformat()
        events = self.observe_range(at.isoformat(), bid_ticks, bid_ticks, ask_ticks, ask_ticks,
                                    historical=False)
        c = self.state["deferred"]
        if c is None:
            return events
        boundary = utc_timestamp(self.state["bar"]) + timedelta(seconds=SECONDS[self.tf])
        if at >= boundary:
            self.state["deferred"] = None
            return events + [self._event(c, "SETUP_EXPIRED", at.isoformat())]
        if not context_authorized(self.tf, c, self.state["context"]):
            self.state["deferred"] = None
            return events + [self._event(c, "SETUP_CANCELLED", at.isoformat(), reason="CONTEXT")]
        # The contract uses chart prices for entry; executable ask/bid is observed separately.
        touched = bid_ticks <= c["entry"] if c["direction"] == 1 else bid_ticks >= c["entry"]
        if touched:
            rr, valid = self._rr(c, c["entry"])
            self.state["deferred"] = None
            if valid:
                events += self._open(c, c["entry"], rr, at.isoformat(), deferred=True)
                events += self.observe_range(at.isoformat(), bid_ticks, bid_ticks, ask_ticks, ask_ticks,
                                             historical=False)
        return events

    def observe_range(self, timestamp, bid_high, bid_low, ask_high=None, ask_low=None, historical=True):
        c = self.state["campaign"]
        if not c or not c["active"]:
            return []
        high, low = bid_high, bid_low
        c["best"] = max(c["best"], high) if c["direction"] == 1 else min(c["best"], low)
        stop = low <= c["sl"] if c["direction"] == 1 else high >= c["sl"]
        target = high >= c["tp"] if c["direction"] == 1 else low <= c["tp"]
        if not (stop or target):
            return []
        c["active"] = False
        return [self._event(c, "SL" if stop else "TP2", timestamp,
                           campaign_key=c["key"], price_ticks=c["sl"] if stop else c["tp"],
                           ambiguous=bool(historical and stop and target),
                           observation="ohlc-stop-first" if historical else "ordered-chart-bid")]

    def expire_bar(self, close_time):
        c = self.state["deferred"]
        self.state["deferred"] = None
        return [self._event(c, "SETUP_EXPIRED", close_time)] if c else []

    def cloud_event(self, outcome, timestamp, reference_ticks):
        if outcome is None:
            return []
        number = self.state["bar_number"]
        window = {"m1": 10, "m3": 12, "m5": 8}.get(self.tf, 6)
        spacing = {"m1": 4, "m3": 3}.get(self.tf, 2)
        emitted = [n for n in self.state["cloud_outcomes"] if n >= number - window + 1]
        self.state["cloud_outcomes"] = emitted
        # Rejected outcomes also consume the Pine transport throttle.
        if (outcome.epoch == self.state["cloud_epoch"] or len(emitted) >= 3 or
                emitted and number - emitted[-1] < spacing):
            return []
        self.state["cloud_epoch"] = outcome.epoch
        emitted.append(number)
        if not outcome.qualified:
            return []
        return [{"event_key": f"cloud:{self.tf}:{timestamp}:{outcome.epoch}:{outcome.direction}",
                 "event_type": "CLOUD_INFO", "event_time": timestamp, "timeframe": self.tf,
                 "direction": outcome.direction, "reference_ticks": reference_ticks}]
