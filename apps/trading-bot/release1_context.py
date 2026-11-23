"""Deterministic context helpers ported from XAUUSD Scalp Release 1."""
from __future__ import annotations

from dataclasses import dataclass, replace


DOW_TREND_UNAVAILABLE = -1
DOW_TREND_BEAR = -2
DOW_TREND_NEUTRAL = 0
DOW_TREND_BULL = 2
DOW_ALIGNMENT_UNAVAILABLE = -1
DOW_ALIGNMENT_NEUTRAL = 0
DOW_ALIGNMENT_FULL_BEAR = 1
DOW_ALIGNMENT_BEAR_CORRECTIVE = 2
DOW_ALIGNMENT_CONFLICTING = 3
DOW_ALIGNMENT_BULL_CORRECTIVE = 4
DOW_ALIGNMENT_FULL_BULL = 5

WYCKOFF_LEANING_UNAVAILABLE = -1
WYCKOFF_LEANING_NEUTRAL = 0
WYCKOFF_LEANING_ACCUMULATION = 1
WYCKOFF_LEANING_DISTRIBUTION = 2

CONFLICT_STRONG = -2
CONFLICT = -1
NEUTRAL = 0
SUPPORT = 1
SUPPORT_STRONG = 2

AR_CONTEXT_UNAVAILABLE = -1
AR_CONTEXT_NEUTRAL = 0
AR_CONTEXT_BULL_ACCEPT = 1
AR_CONTEXT_BEAR_ACCEPT = -2
AR_CONTEXT_BULL_REJECT = 2
AR_CONTEXT_BEAR_REJECT = -3
AR_IDLE = 0
AR_PENDING = 1
AR_ACCEPTED = 2
AR_REJECTED = 3
AR_FAILED = 4
AR_TERMINAL = 5
AR_ACCEPT_NONE = 0
AR_ACCEPT_DIRECT = 1
AR_ACCEPT_RETEST = 2
AR_REJECT_NONE = 0
AR_REJECT_EXCURSION = 1
AR_REJECT_PENDING_FAILURE = 2
AR_REJECT_FAILED_ACCEPTANCE = 3


@dataclass(frozen=True)
class DowFrame:
    primary: int
    secondary: int
    execution: int
    alignment: int
    buy_severity: int
    buy_effect: int
    sell_severity: int
    sell_effect: int
    valid: bool


def _dow_alignment(primary: int, secondary: int, execution: int) -> int:
    values = (primary, secondary, execution)
    if DOW_TREND_UNAVAILABLE in values:
        return DOW_ALIGNMENT_UNAVAILABLE
    if values == (DOW_TREND_BEAR,) * 3:
        return DOW_ALIGNMENT_FULL_BEAR
    if values == (DOW_TREND_BULL,) * 3:
        return DOW_ALIGNMENT_FULL_BULL
    if primary == DOW_TREND_BEAR and secondary != DOW_TREND_BEAR and execution in (DOW_TREND_BEAR, DOW_TREND_NEUTRAL):
        return DOW_ALIGNMENT_BEAR_CORRECTIVE
    if primary == DOW_TREND_BULL and secondary != DOW_TREND_BULL and execution in (DOW_TREND_BULL, DOW_TREND_NEUTRAL):
        return DOW_ALIGNMENT_BULL_CORRECTIVE
    if DOW_TREND_BULL in values and DOW_TREND_BEAR in values:
        return DOW_ALIGNMENT_CONFLICTING
    return DOW_ALIGNMENT_NEUTRAL


def _dow_effect(direction: int, primary: int, secondary: int, execution: int) -> tuple[int, int]:
    if direction not in (-1, 1) or DOW_TREND_UNAVAILABLE in (primary, secondary, execution):
        return 0, NEUTRAL
    support = DOW_TREND_BULL if direction == 1 else DOW_TREND_BEAR
    opposite = DOW_TREND_BEAR if direction == 1 else DOW_TREND_BULL
    lower_opposes = secondary == opposite or execution == opposite
    severity = 3 if primary == opposite else 2 if primary == DOW_TREND_NEUTRAL and lower_opposes else 1 if lower_opposes else 0
    effect = (CONFLICT_STRONG if primary == opposite else CONFLICT if lower_opposes else
              SUPPORT_STRONG if (primary, secondary, execution) == (support,) * 3 else
              SUPPORT if support in (primary, secondary, execution) else NEUTRAL)
    return severity, effect


def dow_frame(official_bias: int, regime: int, smc_trend: int) -> DowFrame:
    primary = DOW_TREND_BULL if official_bias == 1 else DOW_TREND_BEAR if official_bias == -1 else DOW_TREND_NEUTRAL if official_bias in (0, 2) else DOW_TREND_UNAVAILABLE
    secondary = DOW_TREND_BULL if regime in (3, 7) else DOW_TREND_BEAR if regime in (4, 8) else DOW_TREND_NEUTRAL if 1 <= regime <= 6 else DOW_TREND_UNAVAILABLE
    execution = DOW_TREND_BULL if smc_trend == 1 else DOW_TREND_BEAR if smc_trend == -1 else DOW_TREND_NEUTRAL if smc_trend == 0 else DOW_TREND_UNAVAILABLE
    buy_severity, buy_effect = _dow_effect(1, primary, secondary, execution)
    sell_severity, sell_effect = _dow_effect(-1, primary, secondary, execution)
    return DowFrame(primary, secondary, execution, _dow_alignment(primary, secondary, execution),
                    buy_severity, buy_effect, sell_severity, sell_effect,
                    DOW_TREND_UNAVAILABLE not in (primary, secondary, execution))


@dataclass(frozen=True)
class WyckoffFrame:
    leaning: int
    spring_fresh: bool
    upthrust_fresh: bool
    buy_effect: int
    sell_effect: int
    valid: bool


def wyckoff_frame(*, regime_eligible: bool, latest_spring_confirm_bar: int = -1,
                  latest_upthrust_confirm_bar: int = -1, spring_event: bool = False,
                  upthrust_event: bool = False) -> WyckoffFrame:
    if not regime_eligible:
        return WyckoffFrame(WYCKOFF_LEANING_NEUTRAL, False, False, NEUTRAL, NEUTRAL, True)
    leaning = (WYCKOFF_LEANING_ACCUMULATION if latest_spring_confirm_bar > latest_upthrust_confirm_bar else
               WYCKOFF_LEANING_DISTRIBUTION if latest_upthrust_confirm_bar > latest_spring_confirm_bar else
               WYCKOFF_LEANING_NEUTRAL)
    if spring_event and upthrust_event:
        buy_effect = sell_effect = NEUTRAL
    else:
        buy_effect = SUPPORT if spring_event or leaning == WYCKOFF_LEANING_ACCUMULATION else CONFLICT if upthrust_event or leaning == WYCKOFF_LEANING_DISTRIBUTION else NEUTRAL
        sell_effect = SUPPORT if upthrust_event or leaning == WYCKOFF_LEANING_DISTRIBUTION else CONFLICT if spring_event or leaning == WYCKOFF_LEANING_ACCUMULATION else NEUTRAL
    return WyckoffFrame(leaning, spring_event, upthrust_event, buy_effect, sell_effect, True)


@dataclass(frozen=True)
class AcceptanceState:
    fsm: int = AR_IDLE
    context: int = AR_CONTEXT_UNAVAILABLE
    reference_id: str = "-1"
    direction: int = 0
    level_ticks: int | None = None
    break_source_bar: int = -1
    confirm_bar: int = -1
    pending_failure_emitted: bool = False
    excursion_emitted: bool = False


@dataclass(frozen=True)
class AcceptanceFrame:
    state: AcceptanceState
    acceptance_event: int = AR_ACCEPT_NONE
    rejection_event: int = AR_REJECT_NONE
    event_source_bar: int = -1
    event_confirm_bar: int = -1


def arm_acceptance(state: AcceptanceState, *, reference_id: str, direction: int,
                   level_ticks: int, bar_index: int, regime: int) -> AcceptanceState:
    if direction not in (-1, 1) or not 1 <= regime <= 6 or reference_id == state.reference_id:
        return state
    return AcceptanceState(AR_PENDING, AR_CONTEXT_NEUTRAL, reference_id, direction,
                           level_ticks, bar_index)


def acceptance_step(state: AcceptanceState, *, bar_index: int, close_ticks: int,
                    high_ticks: int, low_ticks: int, valid: bool = True,
                    enabled: bool = True) -> AcceptanceFrame:
    if not enabled:
        return AcceptanceFrame(AcceptanceState(AR_IDLE, AR_CONTEXT_NEUTRAL))
    if not valid:
        terminal = AR_TERMINAL if state.fsm in (AR_PENDING, AR_REJECTED) else state.fsm
        return AcceptanceFrame(replace(state, fsm=terminal, context=AR_CONTEXT_UNAVAILABLE))
    if state.level_ticks is None or state.direction not in (-1, 1):
        return AcceptanceFrame(state)
    if state.fsm == AR_ACCEPTED:
        lost = close_ticks <= state.level_ticks if state.direction == 1 else close_ticks >= state.level_ticks
        if not lost:
            return AcceptanceFrame(state)
        context = AR_CONTEXT_BULL_REJECT if state.direction == 1 else AR_CONTEXT_BEAR_REJECT
        failed = replace(state, fsm=AR_FAILED, context=context)
        return AcceptanceFrame(failed, rejection_event=AR_REJECT_FAILED_ACCEPTANCE,
                               event_source_bar=state.confirm_bar, event_confirm_bar=bar_index)
    offset = bar_index - state.break_source_bar
    if state.fsm not in (AR_PENDING, AR_REJECTED) or not 1 <= offset <= 3:
        return AcceptanceFrame(state)
    strict_beyond = close_ticks > state.level_ticks if state.direction == 1 else close_ticks < state.level_ticks
    retest_touch = low_ticks <= state.level_ticks if state.direction == 1 else high_ticks >= state.level_ticks
    acceptance = AR_ACCEPT_RETEST if retest_touch and strict_beyond else AR_ACCEPT_DIRECT if offset == 1 and state.fsm == AR_PENDING and strict_beyond else AR_ACCEPT_NONE
    if acceptance:
        context = AR_CONTEXT_BULL_ACCEPT if state.direction == 1 else AR_CONTEXT_BEAR_ACCEPT
        accepted = replace(state, fsm=AR_ACCEPTED, context=context, confirm_bar=bar_index)
        return AcceptanceFrame(accepted, acceptance_event=acceptance,
                               event_source_bar=state.break_source_bar, event_confirm_bar=bar_index)
    at_or_inside = close_ticks <= state.level_ticks if state.direction == 1 else close_ticks >= state.level_ticks
    excursion = (high_ticks > state.level_ticks if state.direction == 1 else low_ticks < state.level_ticks) and at_or_inside
    rejection = AR_REJECT_NONE
    pending_emitted = state.pending_failure_emitted
    excursion_emitted = state.excursion_emitted
    if offset == 1 and state.fsm == AR_PENDING and at_or_inside and not pending_emitted:
        rejection, pending_emitted = AR_REJECT_PENDING_FAILURE, True
    elif excursion and not excursion_emitted:
        rejection, excursion_emitted = AR_REJECT_EXCURSION, True
    if rejection:
        context = AR_CONTEXT_BULL_REJECT if state.direction == 1 else AR_CONTEXT_BEAR_REJECT
        rejected = replace(state, fsm=AR_REJECTED, context=context,
                           pending_failure_emitted=pending_emitted,
                           excursion_emitted=excursion_emitted)
        if offset == 3:
            rejected = replace(rejected, fsm=AR_TERMINAL)
        return AcceptanceFrame(rejected, rejection_event=rejection,
                               event_source_bar=state.break_source_bar, event_confirm_bar=bar_index)
    if offset == 3:
        return AcceptanceFrame(replace(state, fsm=AR_TERMINAL))
    return AcceptanceFrame(state)
