from __future__ import annotations

from a_share_bridge.history import validate_history


def _bar(symbol: str) -> dict:
    return {
        "date": "2026-08-17", "symbol": symbol, "open": 10, "high": 11,
        "low": 9, "close": 10.5, "amount": None,
    }


def test_missing_amount_is_quality_limitation_not_history_failure() -> None:
    result = {
        "indices": [_bar(str(index)) for index in range(7)],
        "watchlist": [_bar(f"w{index}") for index in range(3)],
        "breadth": [{"date": "2026-08-17"}],
        "industries": [{"date": "2026-08-17", "code": str(index)} for index in range(20)],
        "rotation": [],
    }
    assert validate_history(result, required_index_days=1, required_breadth_days=1, required_industry_days=1) == []
