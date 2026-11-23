"""Small, deterministic helpers for tick freshness and signal slippage."""
from datetime import datetime, timezone

from market_quality import utc_timestamp


def merge_quote(previous, *, bid=None, ask=None, provider_timestamp=None):
    """Merge partial spot updates and ignore provider timestamps that move backwards."""
    state = dict(previous or {})
    incoming = utc_timestamp(provider_timestamp) if provider_timestamp else None
    current = utc_timestamp(state["provider_timestamp"]) if state.get("provider_timestamp") else None
    if incoming and current and incoming < current:
        return state
    if bid is not None:
        state["bid"] = float(bid)
    if ask is not None:
        state["ask"] = float(ask)
    if incoming:
        state["provider_timestamp"] = incoming.isoformat()
    return state


def slippage_observation(signal, planned_entry, bid, ask, spread, atr, tick_time=None):
    signal = signal.upper()
    current = float(ask if signal.endswith("BUY") else bid)
    delta = current - float(planned_entry)
    threshold = max(float(spread) * 2.0, float(atr) * 0.15)
    now = datetime.now(timezone.utc)
    observed = utc_timestamp(tick_time) if tick_time else now
    age = max(0.0, (now - observed).total_seconds())
    return {"current_price": current, "delta": round(delta, 8),
            "threshold": round(threshold, 8), "warning": abs(delta) > threshold,
            "spread": float(spread), "tick_age": round(age, 3)}
