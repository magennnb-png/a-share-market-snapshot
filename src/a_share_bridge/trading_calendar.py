from __future__ import annotations

from datetime import date


def is_a_share_trading_day(day: date) -> tuple[bool, str | None]:
    if day.weekday() >= 5:
        return False, None
    try:
        from chinese_calendar import is_holiday

        return (not is_holiday(day)), None
    except (ImportError, NotImplementedError, ValueError) as exc:
        return True, f"交易日历不可用，已回退为周一至周五判断: {exc}"
