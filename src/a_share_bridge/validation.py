from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from .trading_calendar import is_a_share_trading_day

CORE_INDICES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
    "000300": "沪深300",
    "000852": "中证1000",
}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def validate_snapshot(snapshot: dict[str, Any], intraday: dict[str, Any], now: datetime) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    market_time = _parse(snapshot.get("market_time"))
    if market_time is None:
        errors.append("market_time 缺失或格式无效")
    else:
        current = now.timetz().replace(tzinfo=None)
        trading_day, calendar_warning = is_a_share_trading_day(now.date())
        if calendar_warning:
            warnings.append(calendar_warning)
        in_or_after_session = time(9, 30) <= current <= time(15, 10)
        if trading_day and in_or_after_session and market_time.date() != now.date():
            errors.append(f"行情数据过期：交易时段应为 {now.date()}，实际为 {market_time.date()}")
        elif (now.date() - market_time.date()) > timedelta(days=4):
            errors.append(f"行情数据过期：market_time 为 {market_time.date()}")
        in_session = time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)
        if trading_day and in_session and (now - market_time).total_seconds() > 600:
            errors.append(f"行情延迟超过10分钟：{market_time.isoformat()}")

    by_symbol = {str(row.get("symbol")): row for row in snapshot.get("indices") or []}
    for symbol, name in CORE_INDICES.items():
        row = by_symbol.get(symbol)
        if not row:
            errors.append(f"核心指数缺失：{name}({symbol})")
        elif row.get("last") in (None, 0):
            errors.append(f"核心指数价格无效：{name}({symbol})")
    if "899050" not in by_symbol:
        warnings.append("北证50(899050)缺失")

    breadth = snapshot.get("market_breadth") or {}
    if int(breadth.get("listed_with_quotes") or 0) < 1000:
        errors.append(f"市场宽度样本不足：{breadth.get('listed_with_quotes') or 0}")
    for field in ("up", "down", "flat", "limit_up", "limit_down", "turnover_cny"):
        if breadth.get(field) is None:
            errors.append(f"市场宽度字段缺失：{field}")
    if float(breadth.get("turnover_cny") or 0) <= 0:
        errors.append("全市场成交额无效")

    industries = (snapshot.get("industries") or {}).get("all") or []
    if len(industries) < 20:
        errors.append(f"行业数据不足：仅 {len(industries)} 条")
    intraday_symbols = {str(row.get("symbol")) for row in intraday.get("instruments") or [] if row.get("points")}
    missing_intraday = [symbol for symbol in CORE_INDICES if symbol not in intraday_symbols]
    if missing_intraday:
        errors.append("核心指数分时缺失：" + ", ".join(missing_intraday))
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
