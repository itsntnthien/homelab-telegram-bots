"""Validation helpers for canonical, closed market bars."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo
from functools import lru_cache
import json
import os
from pathlib import Path


BAR_SECONDS = {"m1": 60, "m3": 180, "m5": 300, "m15": 900,
               "m30": 1800, "h1": 3600}


@dataclass(frozen=True)
class BarQuality:
    status: str
    age_sec: float | None
    gap_count: int = 0
    duplicate_count: int = 0
    ready: bool = False


def utc_timestamp(value: str | datetime) -> datetime:
    """Parse provider timestamps into an aware UTC datetime."""
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # .NET round-trip timestamps may have more than Python's six digits.
        if "." in text:
            head, tail = text.split(".", 1)
            zone = ""
            for marker in ("+", "-"):
                index = tail.find(marker)
                if index > 0:
                    zone, tail = tail[index:], tail[:index]
                    break
            text = f"{head}.{tail[:6]}{zone}"
        result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def inspect_bars(rows, timeframe: str, now: datetime, min_closed_bars: int = 2000,
                 max_age_sec: float | None = None) -> BarQuality:
    """Validate newest bars without mutating the database."""
    timeframe = timeframe.lower()
    expected = BAR_SECONDS[timeframe]
    now = utc_timestamp(now)
    if not rows:
        return BarQuality("NO_DATA", None, ready=False)
    parsed = []
    seen = set()
    duplicate_count = 0
    for row in rows:
        timestamp, op, hi, lo, close = row[:5]
        try:
            at = utc_timestamp(timestamp)
            values = tuple(float(value) for value in (op, hi, lo, close))
        except (TypeError, ValueError, OverflowError):
            return BarQuality("INVALID", None, ready=False)
        if at in seen:
            duplicate_count += 1
        seen.add(at)
        op, hi, lo, close = values
        if not all(isfinite(value) for value in values) or not (lo <= op <= hi and lo <= close <= hi):
            return BarQuality("INVALID", max(0.0, (now - at).total_seconds()), 0, duplicate_count, False)
        if at + timedelta(seconds=expected) > now:
            return BarQuality("INVALID", max(0.0, (now - at).total_seconds()), 0, duplicate_count, False)
        parsed.append(at)
    parsed.sort(reverse=True)
    newest = parsed[0]
    # Provider timestamps identify the bar start; freshness is measured after its close.
    close_at = newest + timedelta(seconds=expected)
    age = max(0.0, (now - close_at).total_seconds())
    if duplicate_count:
        return BarQuality("DUPLICATE", age, 0, duplicate_count, False)
    def in_session_gap(left, right):
        return (left-right).total_seconds() > expected and not interval_is_closed(
            right+timedelta(seconds=expected), left)

    gap_count = sum(1 for left, right in zip(parsed, parsed[1:]) if in_session_gap(left, right))
    if gap_count:
        return BarQuality("GAP", age, gap_count, 0, False)
    if max_age_sec is not None and age > max_age_sec:
        return BarQuality("STALE", age, 0, 0, False)
    return BarQuality("OK", age, 0, 0, len(parsed) >= min_closed_bars)


@lru_cache(maxsize=4)
def broker_schedule(path):
    data = json.loads(Path(path).read_text())
    zone = ZoneInfo(data["scheduleTimeZone"])
    intervals = tuple((int(item["startSecond"]), int(item["endSecond"])) for item in data["schedule"])
    if not intervals or any(not 0 <= start < end <= 604800 for start, end in intervals):
        raise ValueError("invalid broker weekly schedule")
    holidays = []
    for item in data.get("holiday", []):
        day = (datetime(1970, 1, 1) + timedelta(days=int(item["holidayDate"]))).date()
        start, end = int(item.get("startSecond", 0)), int(item.get("endSecond", 86399))
        if not 0 <= start <= end <= 86399:
            raise ValueError("invalid broker holiday interval")
        holidays.append((ZoneInfo(item["scheduleTimeZone"]), day, bool(item["isRecurring"]), start, end))
    return zone, intervals, holidays


def market_is_open(at):
    path = os.environ.get("MARKET_SCHEDULE_FILE")
    if path:
        zone, intervals, holidays = broker_schedule(path)
        for holiday_zone, day, recurring, start, end in holidays:
            hlocal = utc_timestamp(at).astimezone(holiday_zone)
            matches = (hlocal.month, hlocal.day) == (day.month, day.day) if recurring else hlocal.date() == day
            if matches and start <= hlocal.hour*3600+hlocal.minute*60+hlocal.second <= end:
                return False
        local = utc_timestamp(at).astimezone(zone)
        # cTrader intervals are Sunday-based, inclusive start / exclusive end.
        second = ((local.weekday()+1) % 7)*86400 + local.hour*3600 + local.minute*60 + local.second
        return any(start <= second < end for start, end in intervals)
    local = utc_timestamp(at).astimezone(ZoneInfo("America/New_York"))
    return not (local.weekday() == 5 or local.weekday() == 6 and local.hour < 18
                or local.weekday() == 4 and local.hour >= 17 or local.hour == 17)


def interval_is_closed(start, end):
    # Baseline NY rollover/weekend calendar; broker holidays need explicit evidence.
    start, end = utc_timestamp(start), utc_timestamp(end)
    if end <= start or end-start > timedelta(days=4):
        return False
    cursor = start
    while cursor < end:
        if market_is_open(cursor):
            return False
        cursor += timedelta(minutes=1)
    return True
