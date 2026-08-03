from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from a_share_bridge.analytics import (
    calculate_intraday_metrics,
    calculate_market_breadth,
    is_quote_stale,
)


def test_intraday_metrics_and_v_shape() -> None:
    points = [
        {"time": "2026-08-03 09:30", "price": 100.0, "volume": 10},
        {"time": "2026-08-03 09:31", "price": 98.0, "volume": 20},
        {"time": "2026-08-03 09:32", "price": 101.0, "volume": 30},
        {"time": "2026-08-03 09:33", "price": 103.0, "volume": 40},
    ]
    result = calculate_intraday_metrics(points, previous_close=101.0)
    assert result["open"] == 100.0
    assert result["high"] == 103.0
    assert result["low"] == 98.0
    assert result["vwap"] == pytest.approx(101.1)
    assert result["max_drawdown_percent"] == pytest.approx(2.0)
    assert result["intraday_position"] == 1.0
    assert result["recovery_from_low_percent"] == pytest.approx(5.102, abs=0.001)
    assert "低开高走" in result["patterns"]
    assert "探底回升" in result["patterns"]
    assert "V形修复" in result["patterns"]


def test_max_drawdown_uses_running_peak() -> None:
    points = [
        {"time": "09:30", "price": 100.0, "volume": 1},
        {"time": "09:31", "price": 105.0, "volume": 1},
        {"time": "09:32", "price": 98.0, "volume": 1},
        {"time": "09:33", "price": 103.0, "volume": 1},
    ]
    result = calculate_intraday_metrics(points)
    assert result["max_drawdown_percent"] == pytest.approx(6.6667, abs=0.0001)
    assert result["pullback_from_high_percent"] == pytest.approx(1.9048, abs=0.0001)


def test_market_breadth_uses_board_specific_limits() -> None:
    rows = [
        {"symbol": "600001", "name": "主板", "last": 10, "change_percent": 9.9, "amount": 100},
        {"symbol": "300001", "name": "创业板", "last": 10, "change_percent": 19.9, "amount": 200},
        {"symbol": "800001", "name": "北交所", "last": 10, "change_percent": -29.9, "amount": 300},
        {"symbol": "600002", "name": "ST测试", "last": 10, "change_percent": -4.9, "amount": 400},
        {"symbol": "600003", "name": "平盘", "last": 10, "change_percent": 0, "amount": 500},
    ]
    result = calculate_market_breadth(rows)
    assert result["up"] == 2
    assert result["down"] == 2
    assert result["flat"] == 1
    assert result["limit_up"] == 2
    assert result["limit_down"] == 2
    assert result["turnover_cny"] == 1500


def test_staleness_during_session() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 3, 10, 58, tzinfo=tz)
    assert not is_quote_stale("2026-08-03T10:56:00+08:00", now)
    assert is_quote_stale("2026-08-03T10:40:00+08:00", now)
    after_close = datetime(2026, 8, 3, 16, 0, tzinfo=tz)
    assert not is_quote_stale("2026-08-03T15:00:00+08:00", after_close)
