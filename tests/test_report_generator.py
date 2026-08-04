from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from report_generator import SLOTS, generate_report, load_inputs, render_report, resolve_slot


def _latest() -> dict:
    return {
        "generated_at": "2026-08-04T10:58:05+08:00",
        "market_time": "2026-08-04T10:59:00+08:00",
        "sources": ["sina", "tencent"],
        "is_stale": False,
        "errors": ["eastmoney实时行情失败，已回退"],
        "warnings": ["涨跌停为近似统计"],
        "indices": [
            {"name": "上证指数", "symbol": "000001", "last": 3808, "change_percent": -0.04},
            {"name": "深证成指", "symbol": "399001", "last": 13743, "change_percent": 2.19},
            {"name": "创业板指", "symbol": "399006", "last": 3430, "change_percent": 3.88},
            {"name": "科创50", "symbol": "000688", "last": 1585, "change_percent": 2.08},
            {"name": "沪深300", "symbol": "000300", "last": 4572, "change_percent": 0.64},
            {"name": "中证1000", "symbol": "000852", "last": 7225, "change_percent": 2.05},
            {"name": "北证50", "symbol": "899050", "last": 1089, "change_percent": 1.21},
        ],
        "market_breadth": {
            "up": 3660,
            "down": 1702,
            "flat": 167,
            "limit_up": 77,
            "limit_down": 2,
            "turnover_cny": 1_207_246_247_788,
            "limit_count_method": "按板块阈值近似统计",
        },
        "industries": {
            "top10": [
                {"name": "电子", "change_percent": 4.2},
                {"name": "软件", "change_percent": 3.8},
                {"name": "通信", "change_percent": 3.2},
            ],
            "bottom10": [
                {"name": "银行", "change_percent": -0.8},
                {"name": "煤炭", "change_percent": -0.5},
            ],
        },
        "watchlist": [
            {"kind": "etf", "name": "创业板ETF", "symbol": "159915", "change_percent": 3.7},
            {"kind": "stock", "name": "贵州茅台", "symbol": "600519", "change_percent": -0.4},
        ],
    }


def _intraday() -> dict:
    instruments = []
    latest = _latest()
    for item in [*latest["indices"], *latest["watchlist"]]:
        kind = "index" if item in latest["indices"] else item["kind"]
        instruments.append(
            {
                "kind": kind,
                "name": item["name"],
                "symbol": item["symbol"],
                "metrics": {
                    "intraday_position": 0.8,
                    "pullback_from_high_percent": 0.3,
                    "recovery_from_low_percent": 1.2,
                    "vwap": item.get("last", 1.0),
                    "patterns": ["探底回升"],
                },
            }
        )
    return {
        "generated_at": "2026-08-04T10:58:05+08:00",
        "market_time": "2026-08-04T10:59:00+08:00",
        "sources": ["tencent"],
        "is_stale": False,
        "errors": [],
        "warnings": [],
        "instruments": instruments,
    }


def test_resolve_slot_boundaries() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    assert resolve_slot("auto", datetime(2026, 8, 4, 9, 5, tzinfo=tz)).code == "0905"
    assert resolve_slot("auto", datetime(2026, 8, 4, 11, 5, tzinfo=tz)).code == "1105"
    assert resolve_slot("auto", datetime(2026, 8, 4, 14, 5, tzinfo=tz)).code == "1405"


def test_generate_report_uses_date_slot_and_required_structure(tmp_path: Path) -> None:
    latest_path = tmp_path / "latest.json"
    intraday_path = tmp_path / "latest_intraday.json"
    prompt_path = tmp_path / "prompt.md"
    latest_path.write_text(json.dumps(_latest(), ensure_ascii=False), encoding="utf-8")
    intraday_path.write_text(json.dumps(_intraday(), ensure_ascii=False), encoding="utf-8")
    prompt_path.write_text("# A股投资研究与交易观察系统\n\n只使用输入证据。", encoding="utf-8")

    output = generate_report(latest_path, intraday_path, prompt_path, tmp_path / "reports", SLOTS["1105"])
    content = output.read_text(encoding="utf-8")
    assert output.name == "2026-08-04_1105.md"
    assert "## Executive Summary（执行摘要）" in content
    assert "## 上午信号是否得到市场宽度确认" in content
    assert "## Further Questions（待补证据）" in content
    assert "## Caveats and Assumptions（风险与口径）" in content
    assert "12072.46亿元" in content
    assert "不构成投资建议" in content


def test_1405_report_discloses_missing_us_inputs() -> None:
    content = render_report(_latest(), _intraday(), SLOTS["1405"], "A股投资研究与交易观察系统")
    assert "低（仅A股映射，缺少美股直接输入）" in content
    assert "不能替代美股期指" in content
    assert "不给确定性涨跌预测" in content
    assert "东方财富实时行情失败；指数和watchlist已由腾讯回退" in content


def test_load_inputs_rejects_mismatched_dates(tmp_path: Path) -> None:
    latest = _latest()
    intraday = _intraday()
    intraday["generated_at"] = "2026-08-03T14:00:00+08:00"
    latest_path = tmp_path / "latest.json"
    intraday_path = tmp_path / "intraday.json"
    latest_path.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
    intraday_path.write_text(json.dumps(intraday, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="不是同一生成日期"):
        load_inputs(latest_path, intraday_path)
