"""Release 1 replay facade used for deterministic shadow/parity runs."""
from __future__ import annotations

import math
from dataclasses import asdict

from release1_execution import Release1Execution, SetupState
from release1_context import (AcceptanceState, acceptance_step, arm_acceptance,
                              dow_frame, wyckoff_frame)
from signal_engine import (PINE_MAXIMUM_CHASE_ATR, RELEASE_CONFIG_HASH,
                           RELEASE_ENGINE_VERSION, SignalEvent, cloud_snapshots,
                           market_snapshots, official_bias_snapshots,
                           replay_main_candidates, structure_snapshots, to_ticks)


def replay_release1(bars, timeframe: str, symbol: str = "XAUUSD",
                    *, two_stage_exit: bool = False,
                    trend_dca: bool = False, initial_state: dict | None = None,
                    start_after: str | None = None) -> tuple[list[SignalEvent], list[dict], dict]:
    """Replay closed bars through the stateful Release 1 execution contract."""
    if not bars:
        return [], [], Release1Execution(two_stage_exit=two_stage_exit,
                                         trend_dca=trend_dca).snapshot()
    winners = replay_main_candidates(bars, timeframe)
    markets = market_snapshots(bars)
    biases = official_bias_snapshots(bars, timeframe)
    structures = structure_snapshots(bars, markets)
    execution = (Release1Execution.restore(initial_state) if initial_state else
                 Release1Execution(two_stage_exit=two_stage_exit, trend_dca=trend_dca))
    signals: list[SignalEvent] = []
    lifecycle: list[dict] = []
    runtime = (initial_state or {}).get("context_runtime", {})
    acceptance = AcceptanceState(**runtime.get("acceptance", {}))
    latest_spring = int(runtime.get("latest_spring", -1))
    latest_upthrust = int(runtime.get("latest_upthrust", -1))
    context_snapshot = (initial_state or {}).get("context")
    for index, bar in enumerate(bars):
        if start_after is not None and bar.timestamp <= start_after:
            continue
        structure = structures[index]
        if structure.event_type and structure.broken_level_ticks is not None:
            acceptance = arm_acceptance(
                acceptance, reference_id=f"{structure.event_type}:{structure.broken_level_ticks}:{index}",
                direction=1 if structure.event_type > 0 else -1,
                level_ticks=structure.broken_level_ticks, bar_index=index,
                regime=markets[index].regime)
        acceptance_frame = acceptance_step(
            acceptance, bar_index=index, close_ticks=to_ticks(bar.close),
            high_ticks=to_ticks(bar.high), low_ticks=to_ticks(bar.low))
        acceptance = acceptance_frame.state
        spring = bool(index and markets[index - 1].swing_low_ticks is not None and
                      to_ticks(bar.low) < markets[index - 1].swing_low_ticks and bar.close > bar.open)
        upthrust = bool(index and markets[index - 1].swing_high_ticks is not None and
                        to_ticks(bar.high) > markets[index - 1].swing_high_ticks and bar.close < bar.open)
        latest_spring = index if spring else latest_spring
        latest_upthrust = index if upthrust else latest_upthrust
        wyckoff = wyckoff_frame(
            regime_eligible=1 <= markets[index].regime <= 5,
            latest_spring_confirm_bar=latest_spring,
            latest_upthrust_confirm_bar=latest_upthrust,
            spring_event=spring, upthrust_event=upthrust)
        dow = dow_frame(biases[index].official_bias, markets[index].regime, structure.trend)
        context_snapshot = {"dow": asdict(dow), "wyckoff": asdict(wyckoff),
                            "acceptance_rejection": asdict(acceptance_frame)}
        if index:
            maximum_chase = math.floor(to_ticks(markets[index - 1].atr) * PINE_MAXIMUM_CHASE_ATR)
            for event in execution.on_closed_bar(
                    timestamp=bar.timestamp, open_ticks=to_ticks(bar.open),
                    high_ticks=to_ticks(bar.high), low_ticks=to_ticks(bar.low),
                    maximum_chase_ticks=maximum_chase):
                lifecycle.append(event)
                if event["event_type"] != "PRIMARY_OPEN":
                    continue
                direction = execution.campaign.direction
                signal = "MAIN BUY" if direction == 1 else "MAIN SELL"
                signals.append(SignalEvent(
                    event["event_key"], symbol, timeframe.upper(), signal,
                    bars[index - 1].timestamp, bar.timestamp,
                    event["entry_ticks"], event["sl_ticks"], event["tp2_ticks"],
                    "MAIN", None, "PRIMARY_OPEN", event["family"], event["subtype"],
                    event["grade"], event["quality"], RELEASE_ENGINE_VERSION,
                    RELEASE_CONFIG_HASH, event["campaign_key"], event.get("tp1_ticks"),
                ))
        winner = winners[index]
        if winner and winner.plan.entry_ready:
            candidate = SetupState(
                event_key=f"{symbol}:{timeframe}:{bar.timestamp}:main:{winner.direction}",
                direction=winner.direction, created_bar=bar.timestamp,
                expires_after_bars=3, entry_ticks=winner.plan.entry_ticks,
                sl_ticks=winner.plan.sl_ticks, tp2_ticks=winner.plan.effective_tp_ticks,
                family=winner.family, subtype=winner.subtype, grade=winner.grade,
                quality=winner.total_quality,
            )
            dca_events = execution.consider_dca(
                timestamp=bar.timestamp, timeframe=timeframe, candidate=candidate,
                trend_strength=winner.total_quality)
            lifecycle.extend(dca_events)
            for event in dca_events:
                direction = candidate.direction
                signals.append(SignalEvent(
                    event["event_key"], symbol, timeframe.upper(),
                    "MAIN BUY" if direction == 1 else "MAIN SELL",
                    bar.timestamp, bar.timestamp, event["entry_ticks"], event["sl_ticks"],
                    event["tp2_ticks"], "MAIN", None, "DCA_OPEN", event["family"],
                    event["subtype"], event["grade"], event["quality"],
                    RELEASE_ENGINE_VERSION, RELEASE_CONFIG_HASH, event["campaign_key"], None,
                ))
            if not dca_events:
                execution.arm(candidate)
    for bar, cloud in zip(bars, cloud_snapshots(bars)):
        if cloud and cloud.qualified:
            signal = "CLOUD BUY" if cloud.direction == 1 else "CLOUD SELL"
            signals.append(SignalEvent(
                f"{symbol}:{timeframe}:cloud:{cloud.epoch}:{cloud.direction}",
                symbol, timeframe.upper(), signal, bar.timestamp, bar.timestamp,
                None, None, None, "CLOUD", to_ticks(bar.close), "CLOUD_INFO",
                None, None, None, None, RELEASE_ENGINE_VERSION, RELEASE_CONFIG_HASH,
            ))
    signals.sort(key=lambda event: (event.execution_time, event.event_key))
    state = execution.snapshot()
    state["context"] = context_snapshot
    state["context_runtime"] = {"acceptance": asdict(acceptance),
                                "latest_spring": latest_spring,
                                "latest_upthrust": latest_upthrust}
    return signals, lifecycle, state
