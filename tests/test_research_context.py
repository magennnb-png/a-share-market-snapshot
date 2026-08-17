from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from a_share_bridge.research_context import (
    UnifiedHistoryProvider,
    _aggregate_weekly,
    _technical_structure,
    build_rotation_context,
    generate_research_context,
)


TZ = ZoneInfo("Asia/Shanghai")


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _daily_rows(symbol: str, name: str, count: int = 130) -> list[dict]:
    rows = []
    day = date(2026, 1, 1)
    while len(rows) < count:
        if day.weekday() < 5:
            close = 100 + len(rows)
            rows.append({
                "date": day.isoformat(), "symbol": symbol, "name": name,
                "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
                "change_percent": 1, "volume": 1000, "amount": 100000,
                "amount_source": "eastmoney", "amount_unit": "CNY", "source": "eastmoney",
            })
        day += timedelta(days=1)
    return rows


def _fixture_root(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    data = tmp_path / "data"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "watchlist.yaml").write_text(
        """
indices:
  - {name: 上证指数, symbol: '000001', kind: index, eastmoney: '1.000001', tencent: sh000001}
watchlist:
  - {name: 芯片ETF国泰, symbol: '512760', kind: etf, eastmoney: '1.512760', tencent: sh512760}
""".strip() + "\n",
        encoding="utf-8",
    )
    fields = ["date", "symbol", "name", "open", "high", "low", "close", "change_percent", "volume", "amount", "amount_source", "amount_unit", "source"]
    _write_csv(data / "history" / "indices_daily.csv", fields, _daily_rows("000001", "上证指数"))
    _write_csv(data / "history" / "watchlist_daily.csv", fields, _daily_rows("512760", "芯片ETF国泰"))
    _write_csv(
        data / "history" / "market_breadth_daily.csv",
        ["date", "up", "down", "flat", "limit_up", "limit_down", "turnover_cny", "median_change_percent", "listed_with_quotes", "source", "method"],
        [{"date": "2026-07-01", "up": 3000, "down": 1800, "flat": 100, "limit_up": 60, "limit_down": 5, "turnover_cny": 1e12, "median_change_percent": 0.5, "listed_with_quotes": 4900, "source": "sina", "method": "realtime_snapshot"}],
    )
    _write_csv(
        data / "history" / "industries_daily.csv",
        ["date", "code", "name", "change_percent", "amount", "activity", "rank", "turnover_rate", "net_inflow", "source"],
        [
            {"date": "2026-07-01", "code": "new_it", "name": "电子行业", "change_percent": 1.2, "amount": 1e10, "activity": 80, "rank": 1, "turnover_rate": "", "net_inflow": "", "source": "sina"},
            {"date": "2026-07-01", "code": "new_bank", "name": "银行", "change_percent": -0.2, "amount": 5e9, "activity": 20, "rank": 2, "turnover_rate": "", "net_inflow": "", "source": "sina"},
        ],
    )
    market = {
        "market_time": "2026-07-01T14:05:00+08:00", "sources": ["tencent", "sina"],
        "watchlist": [{"kind": "etf", "symbol": "512760", "last": 1.2, "source": "tencent"}],
        "market_breadth": {"market_time": "2026-07-01T14:05:00+08:00", "up": 3000, "down": 1800, "flat": 100, "limit_up": 60, "limit_down": 5, "turnover_cny": 1e12, "median_change_percent": 0.5, "listed_with_quotes": 4900, "source": "sina"},
    }
    intraday = {"market_time": "2026-07-01T14:05:00+08:00", "instruments": [{"kind": "etf", "symbol": "512760", "source": "tencent", "metrics": {"vwap": 1.18, "max_drawdown_percent": 1.1}}]}
    return tmp_path, data, market, intraday


def test_weekly_aggregation_keeps_incomplete_amount_null() -> None:
    rows = _daily_rows("000001", "上证指数", 5)
    rows[-1]["amount"] = None
    rows[-1]["amount_source"] = None
    rows[-1]["amount_unit"] = None
    weekly = _aggregate_weekly(rows, datetime(2026, 1, 8, 10, tzinfo=TZ))
    assert weekly[-1]["amount"] is None
    assert weekly[-1]["is_partial"] is True


def test_technical_structure_calculates_requested_windows() -> None:
    rows = _daily_rows("000001", "上证指数", 130)
    technical = _technical_structure(rows)
    assert technical["ma5"] == 227
    assert technical["return_20d_pct"] is not None
    assert technical["high_120d"] == 230


def test_unified_provider_prefers_current_sqlite(tmp_path: Path) -> None:
    root, data, _, _ = _fixture_root(tmp_path)
    database = data / "history" / "market_history.db"
    connection = sqlite3.connect(database)
    connection.execute("""CREATE TABLE market_daily(provider TEXT,symbol TEXT,name TEXT,instrument_type TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,pre_close REAL,pct_change REAL,volume REAL,amount REAL,adjustment TEXT,amount_source TEXT,amount_unit TEXT)""")
    csv_rows = _daily_rows("000001", "上证指数")
    for row in csv_rows:
        connection.execute("INSERT INTO market_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("tencent", "sh000001", "上证指数", "index", row["date"], row["open"], row["high"], row["low"], row["close"], row["close"] - 1, row["change_percent"], row["volume"], None, "none", None, None))
    connection.commit()
    connection.close()
    result = UnifiedHistoryProvider(data).load_series({"symbol": "000001", "tencent": "sh000001", "kind": "index"})
    assert result.backend == "sqlite"
    assert len(result.rows) == 130


def test_csv_provider_does_not_mix_same_code_index_and_stock(tmp_path: Path) -> None:
    root, data, _, _ = _fixture_root(tmp_path)
    fields = ["date", "symbol", "name", "open", "high", "low", "close", "change_percent", "volume", "amount", "amount_source", "amount_unit", "source"]
    stock_rows = _daily_rows("000001", "平安银行")
    for row in stock_rows:
        row.update(open=10, high=11, low=9, close=10)
    existing = list(csv.DictReader((data / "history" / "watchlist_daily.csv").open(encoding="utf-8-sig")))
    _write_csv(data / "history" / "watchlist_daily.csv", fields, [*existing, *stock_rows])
    provider = UnifiedHistoryProvider(data)
    index = provider.load_series({"symbol": "000001", "tencent": "sh000001", "kind": "index"})
    stock = provider.load_series({"symbol": "000001", "tencent": "sz000001", "kind": "stock"})
    assert len(index.rows) == 130 and index.rows[-1]["close"] == 229
    assert len(stock.rows) == 130 and stock.rows[-1]["close"] == 10


def test_csv_rotation_keeps_intraday_fields_null(tmp_path: Path) -> None:
    root, data, _, _ = _fixture_root(tmp_path)
    context = build_rotation_context(root, data, datetime(2026, 7, 1, 14, 5, tzinfo=TZ))
    assert context["backend"] == "csv"
    assert context["sectors"][0]["rs_15m"] is None
    assert "rs_15m" in context["sectors"][0]["null_reasons"]


def test_generate_writes_four_same_schema_contexts(tmp_path: Path) -> None:
    root, data, market, intraday = _fixture_root(tmp_path)
    paths, errors = generate_research_context(root, data, datetime(2026, 7, 1, 14, 5, tzinfo=TZ), market, intraday)
    assert errors == []
    assert {path.name for path in paths} == {"market_technical.json", "rotation_context.json", "market_breadth_context.json", "watchlist_context.json"}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == "1.1"
        assert "backend" in payload
        assert "quality" in payload
        assert "known_limitations" in payload
    technical = json.loads((data / "research_context" / "market_technical.json").read_text(encoding="utf-8"))
    assert technical["indices"][0]["daily_history_count"] == 130
    assert technical["indices"][0]["amount_coverage_ratio"] == 1.0
