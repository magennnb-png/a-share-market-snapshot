from __future__ import annotations

from datetime import datetime, time
from typing import Any, Iterable


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _pct(new: float, old: float) -> float | None:
    if not old:
        return None
    return (new / old - 1.0) * 100.0


def calculate_intraday_metrics(
    points: Iterable[dict[str, Any]],
    previous_close: float | None = None,
) -> dict[str, Any]:
    """Calculate path-aware metrics from ordered one-minute observations."""
    valid = [p for p in points if p.get("price") is not None]
    if not valid:
        return {
            "open": None,
            "last": None,
            "high": None,
            "low": None,
            "vwap": None,
            "max_drawdown_percent": None,
            "intraday_position": None,
            "pullback_from_high_percent": None,
            "recovery_from_low_percent": None,
            "patterns": [],
        }

    prices = [float(p["price"]) for p in valid]
    open_price = prices[0]
    last_price = prices[-1]
    high_price = max(prices)
    low_price = min(prices)
    high_index = prices.index(high_price)
    low_index = prices.index(low_price)

    running_high = prices[0]
    max_drawdown = 0.0
    for price in prices:
        running_high = max(running_high, price)
        if running_high:
            max_drawdown = max(max_drawdown, (running_high - price) / running_high * 100.0)

    weights = [max(float(p.get("volume") or 0.0), 0.0) for p in valid]
    total_weight = sum(weights)
    vwap = (
        sum(price * weight for price, weight in zip(prices, weights, strict=True)) / total_weight
        if total_weight
        else sum(prices) / len(prices)
    )

    position = (last_price - low_price) / (high_price - low_price) if high_price != low_price else 0.5
    pullback = (high_price - last_price) / high_price * 100.0 if high_price else None
    recovery = (last_price - low_price) / low_price * 100.0 if low_price else None
    open_to_last = _pct(last_price, open_price) or 0.0
    rise_from_open = _pct(high_price, open_price) or 0.0
    fall_from_open = -(_pct(low_price, open_price) or 0.0)
    gap = _pct(open_price, previous_close) if previous_close else None

    patterns: list[str] = []
    if gap is not None and gap >= 0.3 and open_to_last <= -0.3:
        patterns.append("高开低走")
    if gap is not None and gap <= -0.3 and open_to_last >= 0.3:
        patterns.append("低开高走")
    if rise_from_open >= 0.8 and (pullback or 0.0) >= 0.5 and high_index < len(prices) - 1:
        patterns.append("冲高回落")
    if fall_from_open >= 0.8 and (recovery or 0.0) >= 0.5 and low_index < len(prices) - 1:
        patterns.append("探底回升")
    if (
        0 < low_index < len(prices) - 1
        and fall_from_open >= 0.8
        and recovery is not None
        and recovery >= 0.8
        and last_price >= open_price * 0.995
    ):
        patterns.append("V形修复")

    return {
        "open": _round(open_price),
        "last": _round(last_price),
        "high": _round(high_price),
        "low": _round(low_price),
        "vwap": _round(vwap),
        "max_drawdown_percent": _round(max_drawdown),
        "intraday_position": _round(position),
        "pullback_from_high_percent": _round(pullback),
        "recovery_from_low_percent": _round(recovery),
        "open_to_last_percent": _round(open_to_last),
        "gap_at_open_percent": _round(gap),
        "high_time": valid[high_index].get("time"),
        "low_time": valid[low_index].get("time"),
        "patterns": patterns,
    }

def is_quote_stale(market_time: str | None, now: datetime) -> bool:
    if not market_time:
        return True
    try:
        observed = datetime.fromisoformat(market_time)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=now.tzinfo)
    except ValueError:
        return True
    if observed.date() != now.date():
        return True
    current = now.timetz().replace(tzinfo=None)
    in_session = time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)
    return in_session and (now - observed).total_seconds() > 300


def limit_percent_for(code: str, name: str) -> float:
    normalized = name.upper().replace(" ", "")
    if "ST" in normalized:
        return 5.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8", "92")):
        return 30.0
    return 10.0


def calculate_market_breadth(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("last") not in (None, 0)]
    up = sum(1 for row in valid if float(row.get("change_percent") or 0) > 0)
    down = sum(1 for row in valid if float(row.get("change_percent") or 0) < 0)
    flat = len(valid) - up - down
    limit_up = 0
    limit_down = 0
    for row in valid:
        threshold = limit_percent_for(str(row.get("symbol", "")), str(row.get("name", "")))
        change = float(row.get("change_percent") or 0)
        if change >= threshold - 0.2:
            limit_up += 1
        if change <= -(threshold - 0.2):
            limit_down += 1
    return {
        "listed_with_quotes": len(valid),
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "turnover_cny": round(sum(float(row.get("amount") or 0) for row in valid), 2),
        "limit_count_method": "按ST 5%、主板10%、创业板/科创板20%、北交所30%的价格涨跌幅近似统计（容差0.2个百分点）",
    }
