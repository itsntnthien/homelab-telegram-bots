"""Stateful Release 1 execution lifecycle, independent from Telegram and SQLite."""
from __future__ import annotations

from dataclasses import asdict, dataclass


WAIT_RETEST = "WAIT_RETEST"
TRIGGERED = "TRIGGERED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
CONFIRMED_NEXT_BAR = "CONFIRMED_NEXT_BAR"


@dataclass
class SetupState:
    event_key: str
    direction: int
    created_bar: str
    expires_after_bars: int
    entry_ticks: int
    sl_ticks: int
    tp2_ticks: int
    family: int
    subtype: int
    grade: int
    quality: int
    state: str = WAIT_RETEST
    bars_waited: int = 0


@dataclass
class CampaignState:
    campaign_key: str
    direction: int
    entry_ticks: int
    initial_sl_ticks: int
    current_sl_ticks: int
    tp2_ticks: int
    risk_ticks: int
    family: int
    subtype: int
    grade: int
    quality: int
    best_ticks: int
    tp1_ticks: int | None = None
    tp1_hit: bool = False
    dca_used: bool = False
    active: bool = True


class Release1Execution:
    """Deterministic Pine-style setup/campaign state machine using integer ticks."""

    def __init__(self, *, two_stage_exit=False, trend_dca=False):
        self.two_stage_exit = bool(two_stage_exit)
        self.trend_dca = bool(trend_dca)
        self.setup: SetupState | None = None
        self.campaign: CampaignState | None = None

    def snapshot(self) -> dict:
        return {
            "two_stage_exit": self.two_stage_exit,
            "trend_dca": self.trend_dca,
            "setup": asdict(self.setup) if self.setup else None,
            "campaign": asdict(self.campaign) if self.campaign else None,
        }

    @classmethod
    def restore(cls, value: dict | None) -> "Release1Execution":
        value = value or {}
        engine = cls(two_stage_exit=value.get("two_stage_exit", False),
                     trend_dca=value.get("trend_dca", False))
        if value.get("setup"):
            engine.setup = SetupState(**value["setup"])
        if value.get("campaign"):
            engine.campaign = CampaignState(**value["campaign"])
        return engine

    def arm(self, setup: SetupState) -> bool:
        if self.campaign and self.campaign.active and self._same_thesis(setup):
            return False
        if self.setup and self.setup.state == WAIT_RETEST:
            current_score = self.setup.quality * 100 + self.setup.grade * 500
            new_score = setup.quality * 100 + setup.grade * 500
            if new_score < current_score + 500:
                return False
        self.setup = setup
        return True

    def on_closed_bar(self, *, timestamp: str, open_ticks: int, high_ticks: int,
                      low_ticks: int, maximum_chase_ticks: int) -> list[dict]:
        events = self._update_campaign(timestamp, high_ticks, low_ticks)
        setup = self.setup
        if setup is None:
            return events
        setup.bars_waited += 1
        if setup.bars_waited > setup.expires_after_bars:
            setup.state = EXPIRED
            events.append(self._setup_event("SETUP_CANCELLED", timestamp, "EXPIRED"))
            self.setup = None
            return events
        reached_at_open = open_ticks <= setup.entry_ticks if setup.direction == 1 else open_ticks >= setup.entry_ticks
        entry_ticks = open_ticks if reached_at_open else setup.entry_ticks
        touched = reached_at_open or (low_ticks <= entry_ticks if setup.direction == 1 else high_ticks >= entry_ticks)
        invalid = low_ticks <= setup.sl_ticks if setup.direction == 1 else high_ticks >= setup.sl_ticks
        if invalid and not touched:
            setup.state = CANCELLED
            events.append(self._setup_event("SETUP_CANCELLED", timestamp, "INVALIDATED"))
            self.setup = None
            return events
        chase_ticks = max(0, (open_ticks - setup.entry_ticks) * setup.direction)
        if not touched:
            return events
        if chase_ticks > maximum_chase_ticks:
            setup.state = CANCELLED
            events.append(self._setup_event("SETUP_CANCELLED", timestamp, "CHASE"))
            self.setup = None
            return events
        risk = (entry_ticks - setup.sl_ticks) * setup.direction
        reward = (setup.tp2_ticks - entry_ticks) * setup.direction
        if risk <= 0 or reward * 1000 < risk * 1250:
            setup.state = CANCELLED
            events.append(self._setup_event("SETUP_CANCELLED", timestamp, "RR"))
            self.setup = None
            return events
        setup.state = CONFIRMED_NEXT_BAR
        tp1 = entry_ticks + setup.direction * risk if self.two_stage_exit else None
        campaign_key = f"campaign:{setup.event_key}"
        self.campaign = CampaignState(
            campaign_key, setup.direction, entry_ticks, setup.sl_ticks, setup.sl_ticks,
            setup.tp2_ticks, risk, setup.family, setup.subtype, setup.grade,
            setup.quality, entry_ticks, tp1,
        )
        events.append({
            "event_key": setup.event_key,
            "event_type": "PRIMARY_OPEN",
            "event_time": timestamp,
            "campaign_key": campaign_key,
            "entry_ticks": entry_ticks,
            "sl_ticks": setup.sl_ticks,
            "tp1_ticks": tp1,
            "tp2_ticks": setup.tp2_ticks,
            "family": setup.family,
            "subtype": setup.subtype,
            "grade": setup.grade,
            "quality": setup.quality,
        })
        self.setup = None
        events.extend(self._update_campaign(timestamp, high_ticks, low_ticks))
        return events

    def on_tick(self, *, timestamp: str, bid_ticks: int, ask_ticks: int) -> list[dict]:
        campaign = self.campaign
        if not campaign or not campaign.active:
            return []
        price = bid_ticks if campaign.direction == 1 else ask_ticks
        return self._update_campaign(timestamp, price, price)

    def consider_dca(self, *, timestamp: str, timeframe: str, candidate: SetupState,
                     trend_strength: int) -> list[dict]:
        """Return the single Pine add-on event when every campaign guard passes."""
        campaign = self.campaign
        if not self.trend_dca or not campaign or not campaign.active:
            return []
        if timeframe.lower() not in {"m5", "m15"} or campaign.dca_used:
            return []
        if candidate.direction != campaign.direction or candidate.family != 1:
            return []
        favorable = (campaign.best_ticks - campaign.entry_ticks) * campaign.direction
        open_profit = (candidate.entry_ticks - campaign.entry_ticks) * campaign.direction
        pullback = (campaign.best_ticks - candidate.entry_ticks) * campaign.direction
        protected = favorable >= campaign.risk_ticks and open_profit * 2 >= campaign.risk_ticks
        stop_not_wider = ((candidate.sl_ticks >= campaign.initial_sl_ticks)
                          if campaign.direction == 1 else
                          (candidate.sl_ticks <= campaign.initial_sl_ticks))
        score_valid = candidate.quality >= 70 and candidate.grade >= campaign.grade
        if not (protected and pullback * 2 >= campaign.risk_ticks and stop_not_wider and
                trend_strength >= 70 and score_valid):
            return []
        campaign.dca_used = True
        return [{
            "event_key": f"{campaign.campaign_key}:dca",
            "event_type": "DCA_OPEN",
            "event_time": timestamp,
            "campaign_key": campaign.campaign_key,
            "entry_ticks": candidate.entry_ticks,
            "sl_ticks": candidate.sl_ticks,
            "tp2_ticks": candidate.tp2_ticks,
            "risk_share_milli": 300,
            "family": candidate.family,
            "subtype": candidate.subtype,
            "grade": candidate.grade,
            "quality": candidate.quality,
        }]

    def _update_campaign(self, timestamp: str, high_ticks: int, low_ticks: int) -> list[dict]:
        campaign = self.campaign
        if not campaign or not campaign.active:
            return []
        if campaign.direction == 1:
            campaign.best_ticks = max(campaign.best_ticks, high_ticks)
            stop_hit = low_ticks <= campaign.current_sl_ticks
            target_hit = high_ticks >= campaign.tp2_ticks
            tp1_hit = campaign.tp1_ticks is not None and high_ticks >= campaign.tp1_ticks
        else:
            campaign.best_ticks = min(campaign.best_ticks, low_ticks)
            stop_hit = high_ticks >= campaign.current_sl_ticks
            target_hit = low_ticks <= campaign.tp2_ticks
            tp1_hit = campaign.tp1_ticks is not None and low_ticks <= campaign.tp1_ticks
        # Historical OHLC has no intrabar ordering. Pine resolves ambiguity stop-first.
        if stop_hit:
            campaign.active = False
            kind = "BE" if campaign.tp1_hit else "SL"
            return [self._campaign_event(kind, timestamp, campaign.current_sl_ticks,
                                         ambiguous=bool(target_hit))]
        events = []
        if tp1_hit and not campaign.tp1_hit:
            campaign.tp1_hit = True
            campaign.current_sl_ticks = campaign.entry_ticks
            events.append(self._campaign_event("TP1", timestamp, campaign.tp1_ticks))
            events.append(self._campaign_event("STOP_PROTECTED", timestamp, campaign.current_sl_ticks))
        if target_hit:
            campaign.active = False
            events.append(self._campaign_event("TP2", timestamp, campaign.tp2_ticks))
        return events

    def _same_thesis(self, setup: SetupState) -> bool:
        campaign = self.campaign
        return bool(campaign and campaign.direction == setup.direction and
                    campaign.family == setup.family and campaign.subtype == setup.subtype and
                    campaign.initial_sl_ticks == setup.sl_ticks and campaign.tp2_ticks == setup.tp2_ticks)

    def _setup_event(self, event_type: str, timestamp: str, reason: str) -> dict:
        return {"event_key": f"{self.setup.event_key}:{event_type.lower()}",
                "event_type": event_type, "event_time": timestamp, "reason": reason}

    def _campaign_event(self, event_type: str, timestamp: str, price: int,
                        ambiguous: bool = False) -> dict:
        campaign = self.campaign
        return {"event_key": f"{campaign.campaign_key}:{event_type.lower()}:{timestamp}",
                "event_type": event_type, "event_time": timestamp,
                "campaign_key": campaign.campaign_key, "price_ticks": price,
                "ambiguous": ambiguous}
