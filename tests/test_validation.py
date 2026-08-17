from datetime import datetime
from zoneinfo import ZoneInfo

from a_share_bridge.analytics import calculate_market_breadth
from a_share_bridge.validation import CORE_INDICES, validate_snapshot


def valid_payload(now: datetime):
    indices = [
        {"symbol": symbol, "name": name, "last": 100, "market_time": now.isoformat()}
        for symbol, name in CORE_INDICES.items()
    ]
    indices.append({"symbol": "899050", "name": "北证50", "last": 100, "market_time": now.isoformat()})
    snapshot = {
        "market_time": now.isoformat(), "indices": indices,
        "market_breadth": {"listed_with_quotes": 5000, "up": 3000, "down": 1900, "flat": 100, "limit_up": 60, "limit_down": 5, "turnover_cny": 1e12},
        "industries": {"all": [{"name": str(i)} for i in range(80)]},
    }
    intraday = {"instruments": [{"symbol": symbol, "points": [{}]} for symbol in CORE_INDICES]}
    return snapshot, intraday


def test_rejects_stale_market_during_session() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 12, 10, 30, tzinfo=tz)
    snapshot, intraday = valid_payload(now)
    snapshot["market_time"] = "2026-08-11T15:00:00+08:00"
    errors, _ = validate_snapshot(snapshot, intraday, now)
    assert any("行情数据过期" in error for error in errors)


def test_rejects_missing_core_index_and_bad_breadth() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 12, 10, 30, tzinfo=tz)
    snapshot, intraday = valid_payload(now)
    snapshot["indices"] = snapshot["indices"][1:]
    snapshot["market_breadth"]["listed_with_quotes"] = 10
    errors, _ = validate_snapshot(snapshot, intraday, now)
    assert any("上证指数" in error for error in errors)
    assert any("市场宽度样本不足" in error for error in errors)


def test_accepts_complete_current_payload() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 12, 10, 30, tzinfo=tz)
    snapshot, intraday = valid_payload(now)
    errors, _ = validate_snapshot(snapshot, intraday, now)
    assert errors == []


def test_market_breadth_includes_median_change() -> None:
    breadth = calculate_market_breadth([
        {"last": 10, "change_percent": -1, "amount": 100, "symbol": "600000", "name": "甲"},
        {"last": 10, "change_percent": 2, "amount": 200, "symbol": "600001", "name": "乙"},
        {"last": 10, "change_percent": 4, "amount": 300, "symbol": "600002", "name": "丙"},
    ])
    assert breadth["median_change_percent"] == 2.0
