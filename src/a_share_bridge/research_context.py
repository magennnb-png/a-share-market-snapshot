from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import yaml

from .trading_calendar import is_a_share_trading_day


ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "1.1"
PROVIDER_PRIORITY = {"csindex": 50, "eastmoney": 40, "tencent": 30, "sohu": 20, "sina": 10}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass
class SeriesResult:
    rows: list[dict[str, Any]]
    backend: str
    fallback_used: bool = False


class HistoryProvider(Protocol):
    backend: str

    def load_series(self, item: dict[str, Any]) -> list[dict[str, Any]]: ...


class SQLiteHistoryProvider:
    backend = "sqlite"

    def __init__(self, data_dir: Path):
        self.path = data_dir / "history" / "market_history.db"

    @property
    def available(self) -> bool:
        return self.path.exists()

    def load_series(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.available:
            return []
        symbol = str(item.get("tencent") or item.get("symbol") or "")
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=20)
            connection.row_factory = sqlite3.Row
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(market_daily)")}
            if not columns:
                return []
            amount_source = "amount_source" if "amount_source" in columns else "NULL AS amount_source"
            amount_unit = "amount_unit" if "amount_unit" in columns else "NULL AS amount_unit"
            rows = connection.execute(
                f"""
                SELECT provider, symbol, name, instrument_type, trade_date, open, high, low, close,
                       pre_close, pct_change, volume, amount, adjustment, {amount_source}, {amount_unit}
                FROM market_daily WHERE symbol=? ORDER BY trade_date, provider, adjustment
                """,
                (symbol,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            if "connection" in locals():
                connection.close()

        expected_adjustment = "none" if str(item.get("kind")) == "index" else "qfq"
        selected: dict[str, tuple[int, dict[str, Any]]] = {}
        for raw in rows:
            row = dict(raw)
            score = PROVIDER_PRIORITY.get(str(row.get("provider")), 0)
            if row.get("adjustment") == expected_adjustment:
                score += 100
            day = str(row.get("trade_date") or "")
            if not day:
                continue
            current = selected.get(day)
            if current is None or score > current[0]:
                selected[day] = (
                    score,
                    {
                        "date": day,
                        "open": _number(row.get("open")),
                        "high": _number(row.get("high")),
                        "low": _number(row.get("low")),
                        "close": _number(row.get("close")),
                        "previous_close": _number(row.get("pre_close")),
                        "change_percent": _number(row.get("pct_change")),
                        "volume": _number(row.get("volume")),
                        "amount": _number(row.get("amount")),
                        "amount_source": row.get("amount_source"),
                        "amount_unit": row.get("amount_unit"),
                        "source": row.get("provider"),
                        "adjustment": row.get("adjustment"),
                    },
                )
        return [selected[day][1] for day in sorted(selected)]


class CSVHistoryProvider:
    backend = "csv"

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.indices_by_symbol = self._group(_read_csv(data_dir / "history" / "indices_daily.csv"))
        self.watchlist_by_symbol = self._group(_read_csv(data_dir / "history" / "watchlist_daily.csv"))

    @staticmethod
    def _group(rows: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in rows:
            amount = _number(raw.get("amount"))
            by_symbol[str(raw.get("symbol") or "")].append(
                {
                    "date": raw.get("date"),
                    "open": _number(raw.get("open")),
                    "high": _number(raw.get("high")),
                    "low": _number(raw.get("low")),
                    "close": _number(raw.get("close")),
                    "previous_close": None,
                    "change_percent": _number(raw.get("change_percent")),
                    "volume": _number(raw.get("volume")),
                    "amount": amount,
                    "amount_source": raw.get("amount_source") or (raw.get("source") if amount is not None else None),
                    "amount_unit": raw.get("amount_unit") or ("CNY" if amount is not None else None),
                    "source": raw.get("source"),
                    "adjustment": "none",
                }
            )
        for series in by_symbol.values():
            series.sort(key=lambda row: str(row.get("date")))
            for index, row in enumerate(series):
                if row.get("previous_close") is None and index:
                    row["previous_close"] = series[index - 1].get("close")
        return by_symbol

    def load_series(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        symbol = str(item.get("symbol") or "")
        # A raw six-digit code is not globally unique across files: 000001 is
        # both the SSE Composite and Ping An Bank. Core index definitions use
        # indices_daily; other watchlist instruments use watchlist_daily.
        if str(item.get("kind")) == "index" and symbol in self.indices_by_symbol:
            series = self.indices_by_symbol[symbol]
        else:
            series = self.watchlist_by_symbol.get(symbol, [])
        return [dict(row) for row in series]


class UnifiedHistoryProvider:
    """Prefer a current, usable local SQLite series; otherwise use tracked CSV."""

    def __init__(self, data_dir: Path):
        self.sqlite = SQLiteHistoryProvider(data_dir)
        self.csv = CSVHistoryProvider(data_dir)

    def load_series(self, item: dict[str, Any]) -> SeriesResult:
        sqlite_rows = self.sqlite.load_series(item)
        csv_rows = self.csv.load_series(item)
        sqlite_latest = str(sqlite_rows[-1].get("date")) if sqlite_rows else ""
        csv_latest = str(csv_rows[-1].get("date")) if csv_rows else ""
        if len(sqlite_rows) >= 20 and sqlite_latest >= csv_latest:
            return SeriesResult(sqlite_rows, "sqlite", False)
        return SeriesResult(csv_rows, "csv", bool(sqlite_rows))


def _window_return(rows: list[dict[str, Any]], sessions: int) -> float | None:
    if len(rows) <= sessions:
        return None
    current = _number(rows[-1].get("close"))
    prior = _number(rows[-sessions - 1].get("close"))
    if current is None or prior in (None, 0):
        return None
    return _round((current / prior - 1) * 100)


def _moving_average(rows: list[dict[str, Any]], sessions: int) -> float | None:
    if len(rows) < sessions:
        return None
    values = [_number(row.get("close")) for row in rows[-sessions:]]
    if any(value is None for value in values):
        return None
    return _round(statistics.fmean(value for value in values if value is not None))


def _extreme(rows: list[dict[str, Any]], sessions: int, field: str, high: bool) -> float | None:
    if len(rows) < sessions:
        return None
    values = [_number(row.get(field)) for row in rows[-sessions:]]
    if any(value is None for value in values):
        return None
    clean = [value for value in values if value is not None]
    return _round(max(clean) if high else min(clean))


def _distance(current: float | None, reference: float | None) -> float | None:
    if current is None or reference in (None, 0):
        return None
    return _round((current / reference - 1) * 100)


def _technical_structure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = _number(rows[-1].get("close")) if rows else None
    highs = {days: _extreme(rows, days, "high", True) for days in (20, 60, 120)}
    lows = {days: _extreme(rows, days, "low", False) for days in (20, 60, 120)}
    result: dict[str, Any] = {}
    for days in (5, 10, 20, 60):
        result[f"ma{days}"] = _moving_average(rows, days)
        result[f"return_{days}d_pct"] = _window_return(rows, days)
    for days in (20, 60, 120):
        result[f"high_{days}d"] = highs[days]
        result[f"low_{days}d"] = lows[days]
        result[f"distance_to_{days}d_high_pct"] = _distance(current, highs[days])
        result[f"distance_to_{days}d_low_pct"] = _distance(current, lows[days])
    result["amplitude_1m_pct"] = _distance(highs[20], lows[20])
    result["amplitude_3m_pct"] = _distance(highs[60], lows[60])
    return result


def _week_is_partial(last_day: str | None, generated_at: datetime) -> bool | None:
    if not last_day:
        return None
    try:
        observed = date.fromisoformat(last_day)
    except ValueError:
        return None
    if observed.isocalendar()[:2] != generated_at.date().isocalendar()[:2]:
        return False
    if observed == generated_at.date() and generated_at.hour < 15:
        return True
    cursor = observed + timedelta(days=1)
    while cursor.isocalendar()[:2] == observed.isocalendar()[:2]:
        if is_a_share_trading_day(cursor)[0]:
            return True
        cursor += timedelta(days=1)
    return False


def _aggregate_weekly(
    rows: list[dict[str, Any]], generated_at: datetime | None = None, limit: int = 52
) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            trading_day = date.fromisoformat(str(row["date"]))
        except (KeyError, ValueError):
            continue
        year, week, _ = trading_day.isocalendar()
        grouped[(year, week)].append(row)
    weekly: list[dict[str, Any]] = []
    previous_close: float | None = None
    for (year, week), items in sorted(grouped.items()):
        items.sort(key=lambda row: str(row.get("date")))
        close = _number(items[-1].get("close"))
        volumes = [_number(row.get("volume")) for row in items]
        amounts = [_number(row.get("amount")) for row in items]
        units = {row.get("amount_unit") for row in items if row.get("amount_unit")}
        complete_amount = all(value is not None for value in amounts) and units <= {"CNY"}
        weekly.append(
            {
                "week": f"{year}-W{week:02d}",
                "week_start": items[0].get("date"),
                "week_end": items[-1].get("date"),
                "open": _number(items[0].get("open")),
                "high": _round(max(value for value in (_number(row.get("high")) for row in items) if value is not None), 6),
                "low": _round(min(value for value in (_number(row.get("low")) for row in items) if value is not None), 6),
                "close": close,
                "change_percent": _round((close / previous_close - 1) * 100) if close is not None and previous_close else None,
                "volume": _round(sum(value for value in volumes if value is not None), 2) if all(value is not None for value in volumes) else None,
                "amount": _round(sum(value for value in amounts if value is not None), 2) if complete_amount else None,
                "amount_unit": "CNY" if complete_amount else None,
                "sources": sorted({str(row.get("source")) for row in items if row.get("source")}),
                "is_partial": _week_is_partial(str(items[-1].get("date")), generated_at) if generated_at else None,
            }
        )
        previous_close = close
    return weekly[-limit:]


def _daily_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "date", "open", "high", "low", "close", "previous_close", "change_percent",
            "volume", "amount", "amount_source", "amount_unit", "source", "adjustment",
        )
    }


def _instrument_context(
    provider: UnifiedHistoryProvider,
    item: dict[str, Any],
    generated_at: datetime,
    include_weekly: bool,
) -> dict[str, Any]:
    result = provider.load_series(item)
    rows = result.rows
    recent = rows[-120:]
    weekly = _aggregate_weekly(rows, generated_at) if include_weekly and rows else []
    amount_count = sum(_number(row.get("amount")) is not None and row.get("amount_unit") == "CNY" for row in recent)
    ratio = _round(amount_count / len(recent), 4) if recent else 0.0
    limitations: list[str] = []
    if len(rows) < 120:
        limitations.append(f"仅有{len(rows)}个可比交易日，120日位置指标不完整。")
    if include_weekly and len(weekly) < 52:
        limitations.append(f"仅有{len(weekly)}根周K，52周结构不完整。")
    if recent and amount_count < len(recent):
        limitations.append(f"最近日K成交额覆盖{amount_count}/{len(recent)}；缺失值和不完整周保持null。")
    if not rows:
        limitations.append("SQLite与CSV均无该标的日K。")
    sources = sorted({str(row.get("source")) for row in rows if row.get("source")})
    grade = "red" if len(rows) < 20 else "yellow" if len(rows) < 120 or ratio < 1 else "green"
    payload = {
        "code": str(item.get("symbol") or ""),
        "name": item.get("name"),
        "kind": item.get("kind"),
        "backend": result.backend,
        "backend_fallback_used": result.fallback_used,
        "data_as_of": rows[-1].get("date") if rows else None,
        "history_start": rows[0].get("date") if rows else None,
        "daily_history_count": len(rows),
        "weekly_history_count": len(weekly) if include_weekly else None,
        "previous_close": rows[-1].get("previous_close") if rows else None,
        "current_week_is_partial": weekly[-1].get("is_partial") if weekly else None,
        "source": sources[0] if len(sources) == 1 else sources,
        "sources": sources,
        "amount_coverage_ratio": ratio,
        "quality": {"grade": grade, "limitations": limitations},
        "daily": [_daily_public(row) for row in recent],
        "technical": _technical_structure(rows),
    }
    if include_weekly:
        payload["weekly"] = weekly
    return payload


def build_market_technical(
    root: Path, data_dir: Path, generated_at: datetime, provider: UnifiedHistoryProvider | None = None
) -> dict[str, Any]:
    config = yaml.safe_load((root / "config" / "watchlist.yaml").read_text(encoding="utf-8")) or {}
    history = provider or UnifiedHistoryProvider(data_dir)
    indices = [_instrument_context(history, item, generated_at, True) for item in config.get("indices") or []]
    dates = [item.get("data_as_of") for item in indices if item.get("data_as_of")]
    grades = [str((item.get("quality") or {}).get("grade")) for item in indices]
    backends = sorted({str(item.get("backend")) for item in indices if item.get("backend")})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "data_as_of": max(dates, default=None),
        "backend": backends[0] if len(backends) == 1 else "mixed",
        "sources": sorted({source for item in indices for source in item.get("sources") or []}),
        "quality": {
            "grade": "red" if "red" in grades else "yellow" if "yellow" in grades else "green",
            "instrument_count": len(indices),
            "sqlite_available": history.sqlite.available,
        },
        "known_limitations": [
            "周K由日K按ISO周聚合；current_week_is_partial标记未结束交易周。",
            "周成交额仅在该周所有日K成交额均有效且单位为CNY时输出，否则为null。",
            "SQLite可用且不落后时优先；否则自动使用GitHub可读CSV，JSON字段保持一致。",
        ],
        "indices": indices,
    }


def _trading_minute(timestamp: str | None) -> int | None:
    try:
        moment = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    minute = moment.hour * 60 + moment.minute
    if 570 <= minute <= 690:
        return minute - 570
    if 780 <= minute <= 900:
        return 121 + minute - 780
    return None


def _past_row(rows: list[dict[str, Any]], current: dict[str, Any], minutes: int) -> dict[str, Any] | None:
    current_index = _trading_minute(current.get("bucket_time"))
    if current_index is None:
        return None
    target = current_index - minutes
    candidates = [row for row in rows if (_trading_minute(row.get("bucket_time")) or 0) <= target]
    return candidates[-1] if candidates else None


def _relative_window(current: dict[str, Any], past: dict[str, Any] | None) -> float | None:
    if not past:
        return None
    values = (
        _number(current.get("change_percent")),
        _number(past.get("change_percent")),
        _number(((current.get("benchmarks") or {}).get("shanghai") or {}).get("change_percent")),
        _number(((past.get("benchmarks") or {}).get("shanghai") or {}).get("change_percent")),
    )
    if any(value is None for value in values):
        return None
    current_return, past_return, current_benchmark, past_benchmark = values
    return _round((current_return - past_return) - (current_benchmark - past_benchmark))


def _delta(current: dict[str, Any], past: dict[str, Any] | None, field: str) -> float | None:
    if not past:
        return None
    new, old = _number(current.get(field)), _number(past.get(field))
    return _round(new - old) if new is not None and old is not None else None


def _period_relative(rows: list[dict[str, Any]], days: int) -> float | None:
    last_by_date = {str(row.get("market_date")): row for row in rows}
    daily = list(last_by_date.values())[-days:]
    if len(daily) < days:
        return None
    sector_factor = benchmark_factor = 1.0
    for row in daily:
        sector = _number(row.get("change_percent"))
        benchmark = _number(((row.get("benchmarks") or {}).get("shanghai") or {}).get("change_percent"))
        if sector is None or benchmark is None:
            return None
        sector_factor *= 1 + sector / 100
        benchmark_factor *= 1 + benchmark / 100
    return _round((sector_factor / benchmark_factor - 1) * 100)


def _rotation_score(row: dict[str, Any]) -> float:
    raw = (
        (_number(row.get("rs_15m")) or 0) * 18
        + (_number(row.get("rs_30m")) or 0) * 14
        + (_number(row.get("rs_60m")) or 0) * 7
        + (_number(row.get("breadth_change_30m")) or 0) * 45
        + (_number(row.get("amount_share_change_30m")) or 0) * 250
        + (_number(row.get("median_return_change_30m")) or 0) * 12
        + ((_number(row.get("intraday_position")) or 0.5) - 0.5) * 10
    )
    return _round(max(-100.0, min(100.0, raw))) or 0.0


def _rotation_state(row: dict[str, Any]) -> str:
    rs15, rs30, rs60 = (_number(row.get(key)) for key in ("rs_15m", "rs_30m", "rs_60m"))
    breadth = _number(row.get("breadth_change_30m"))
    turnover = _number(row.get("amount_share_change_30m"))
    position = _number(row.get("intraday_position"))
    pullback = _number(row.get("high_point_pullback_pct"))
    recovery = _number(row.get("low_point_recovery_pct"))
    score = _number(row.get("rotation_score")) or 0
    if position is not None and position <= 0.4 and (rs15 or 0) >= 0.25 and (breadth or 0) >= 0.08 and (turnover or 0) > 0:
        return "低位启动候选"
    if (rs15 or 0) >= 0.3 and (rs30 or 0) >= 0.45 and (breadth or 0) >= 0.08 and (turnover or 0) > 0:
        return "加速走强"
    if (rs60 or 0) > 0.2 and (rs15 or 0) <= -0.25 and (breadth or 0) < 0:
        return "由强转弱"
    if (rs15 or 0) <= -0.3 and (rs30 or 0) <= -0.45 and (breadth or 0) <= -0.08:
        return "加速走弱"
    if (pullback or 0) >= 1 and (position if position is not None else 1) < 0.5 and (rs15 or 0) < -0.15:
        return "冲高回落"
    if position is not None and position <= 0.35 and (rs15 or 0) >= 0 and (recovery or 0) >= 0.5:
        return "低位企稳"
    if score >= 15 and (rs30 or 0) > 0:
        return "温和走强"
    return "暂无明确信号"


def _load_rotation_sqlite(path: Path, limit_days: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=20)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT s.bucket_time, s.market_date, s.industry_code, s.industry_name, s.payload_json,
                   m.market_time, m.generated_at, m.source, m.benchmarks_json
            FROM rotation_snapshots s JOIN rotation_snapshot_meta m USING(bucket_time)
            WHERE s.market_date IN (
                SELECT DISTINCT market_date FROM rotation_snapshots ORDER BY market_date DESC LIMIT ?
            ) ORDER BY s.bucket_time, s.industry_code
            """,
            (limit_days,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if "connection" in locals():
            connection.close()
    result: list[dict[str, Any]] = []
    for raw in rows:
        try:
            payload = json.loads(raw["payload_json"])
            payload.update(
                bucket_time=raw["bucket_time"], market_date=raw["market_date"],
                snapshot_market_time=raw["market_time"], snapshot_generated_at=raw["generated_at"],
                snapshot_source=raw["source"], benchmarks=json.loads(raw["benchmarks_json"]),
            )
            result.append(payload)
        except (TypeError, json.JSONDecodeError):
            continue
    return result


def _enrich_rotation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_bucket = max((str(row.get("bucket_time") or "") for row in rows), default="")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("industry_code") or row.get("industry_name"))].append(row)
    enriched: list[dict[str, Any]] = []
    for history in grouped.values():
        history.sort(key=lambda row: str(row.get("bucket_time")))
        current = dict(history[-1])
        if str(current.get("bucket_time")) != latest_bucket:
            continue
        same_day = [row for row in history if row.get("market_date") == current.get("market_date")]
        current["rs_15m"] = _relative_window(current, _past_row(same_day, current, 15))
        current["rs_30m"] = _relative_window(current, _past_row(same_day, current, 30))
        current["rs_60m"] = _relative_window(current, _past_row(same_day, current, 60))
        current["rs_since_open"] = _relative_window(current, same_day[0]) if len(same_day) > 1 else 0.0
        current["rs_1d"] = current.get("relative_return_shanghai")
        for days in (5, 10, 20):
            current[f"rs_{days}d"] = _period_relative(history, days)
        past30 = _past_row(same_day, current, 30)
        current["breadth_change_30m"] = _delta(current, past30, "up_ratio")
        current["amount_share_change_30m"] = _delta(current, past30, "turnover_share")
        current["median_return_change_30m"] = _delta(current, past30, "constituent_median_return")
        recent = same_day[-12:]
        leader = current.get("leader_symbol")
        current["leader_persistence"] = _round(sum(row.get("leader_symbol") == leader for row in recent) / len(recent)) if recent and leader else None
        current["rotation_score"] = _rotation_score(current)
        current["rotation_state"] = _rotation_state(current)
        enriched.append(current)
    return sorted(enriched, key=lambda row: _number(row.get("rotation_score")) or -999, reverse=True)


ROTATION_FIELDS = {
    "board_code": "industry_code", "board_name": "industry_name", "timestamp": "bucket_time",
    "pct_change": "change_percent", "relative_to_sse": "relative_return_shanghai",
    "relative_to_csi300": "relative_return_hs300", "relative_to_market_median": "relative_return_market_median",
    "amount": "amount", "amount_share": "turnover_share", "advance_count": "up_count",
    "decline_count": "down_count", "advance_ratio": "up_ratio",
    "constituent_median_return": "constituent_median_return", "leader_name": "leader_name",
    "leader_code": "leader_symbol", "leader_return": "leader_return", "leader_persistence": "leader_persistence",
    "rs_15m": "rs_15m", "rs_30m": "rs_30m", "rs_60m": "rs_60m", "rs_since_open": "rs_since_open",
    "rs_1d": "rs_1d", "rs_5d": "rs_5d", "rs_10d": "rs_10d", "rs_20d": "rs_20d",
    "breadth_change_30m": "breadth_change_30m", "amount_share_change_30m": "amount_share_change_30m",
    "median_return_change_30m": "median_return_change_30m", "intraday_position": "intraday_position",
    "high_point_pullback_pct": "pullback_from_high", "low_point_recovery_pct": "recovery_from_low",
    "rotation_score": "rotation_score", "rotation_state": "rotation_state",
}


def _rotation_public(row: dict[str, Any], rank: int, taxonomy: str, comparable: bool) -> dict[str, Any]:
    result = {target: row.get(source) for target, source in ROTATION_FIELDS.items()}
    result.update(current_rank=rank, taxonomy=taxonomy, comparable=comparable, canonical_ths_comparable=taxonomy == "ths")
    result["null_reasons"] = {
        field: (
            "同口径盘中快照不足。" if field in {"rs_15m", "rs_30m", "rs_60m", "breadth_change_30m", "amount_share_change_30m", "median_return_change_30m"}
            else "同口径交易日历史不足。" if field in {"rs_5d", "rs_10d", "rs_20d"}
            else "当前数据源未提供可靠输入。"
        )
        for field, value in result.items() if field in ROTATION_FIELDS and value is None
    }
    return result


def _rotation_rank_context(current: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    ranks: dict[str, list[tuple[str, int, float | None]]] = defaultdict(list)
    for day in history:
        for item in day.get("sectors") or []:
            ranks[str(item.get("board_code"))].append((str(day.get("date")), int(item.get("current_rank") or 0), _number(item.get("rotation_score"))))
    for item in current:
        observations = ranks.get(str(item.get("board_code")), [])
        current_rank = int(item.get("current_rank") or 0)
        item["rank_change_1d"] = observations[-2][1] - current_rank if len(observations) >= 2 else None
        item["rank_change_5d"] = observations[-6][1] - current_rank if len(observations) >= 6 else None
        streak = 0
        for _, _, score in reversed(observations):
            if score is None or score <= 0:
                break
            streak += 1
        item["positive_score_streak_days"] = streak if observations else None


def _compact_rotation_history(sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "board_code", "board_name", "current_rank", "pct_change", "rotation_score", "rotation_state",
        "rs_1d", "rs_5d", "rs_10d", "rs_20d", "advance_ratio", "amount_share",
    )
    return [{field: item.get(field) for field in fields} for item in sectors]


def _build_sqlite_rotation(data_dir: Path, generated_at: datetime) -> dict[str, Any] | None:
    rows = _load_rotation_sqlite(data_dir / "history" / "rotation.db")
    if not rows:
        return None
    latest = rows[-1]
    taxonomy = str(latest.get("taxonomy") or "unknown_fallback")
    source = str(latest.get("snapshot_source") or latest.get("source") or "unknown")
    comparable_rows = [row for row in rows if str(row.get("taxonomy") or "unknown_fallback") == taxonomy and str(row.get("snapshot_source") or row.get("source") or "unknown") == source]
    dates = sorted({str(row.get("market_date")) for row in comparable_rows})
    full_history: list[dict[str, Any]] = []
    for day in dates[-10:]:
        usable = [row for row in comparable_rows if str(row.get("market_date")) <= day]
        sectors = [_rotation_public(row, rank, taxonomy, True) for rank, row in enumerate(_enrich_rotation(usable), 1)]
        full_history.append({"date": day, "sectors": sectors})
    current = full_history[-1]["sectors"] if full_history else []
    compact_history = [{"date": day["date"], "sectors": _compact_rotation_history(day["sectors"])} for day in full_history]
    _rotation_rank_context(current, compact_history)
    limitations = ["轮动描述来自价格、成交和扩散度，不代表真实资金净流入或机构行为。"]
    if taxonomy != "ths":
        limitations.append(f"当前口径为{taxonomy}，不能与同花顺分类历史直接混算。")
    if len(dates) < 20:
        limitations.append(f"同口径历史仅{len(dates)}个交易日，10/20日指标可能为空。")
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_time": latest.get("snapshot_market_time") or latest.get("bucket_time"), "data_as_of": dates[-1] if dates else None,
        "backend": "sqlite", "sources": [source], "taxonomy": taxonomy, "comparable": True,
        "quality": {"grade": "green" if len(dates) >= 10 else "yellow", "sector_count": len(current), "same_taxonomy_days": len(dates), "snapshot_count": len({row.get('bucket_time') for row in comparable_rows})},
        "known_limitations": limitations, "sectors": current, "recent_daily_history": compact_history,
    }


def _compound_relative(industry: list[dict[str, Any]], benchmark: dict[str, float], days: int, source: str) -> float | None:
    usable = [row for row in industry if str(row.get("source")) == source and row.get("date") in benchmark][-days:]
    if len(usable) < days:
        return None
    sector_factor = benchmark_factor = 1.0
    for row in usable:
        sector, market = _number(row.get("change_percent")), benchmark.get(str(row.get("date")))
        if sector is None or market is None:
            return None
        sector_factor *= 1 + sector / 100
        benchmark_factor *= 1 + market / 100
    return _round((sector_factor / benchmark_factor - 1) * 100)


def _build_csv_rotation(data_dir: Path, generated_at: datetime) -> dict[str, Any]:
    rows = _read_csv(data_dir / "history" / "industries_daily.csv")
    dates = sorted({str(row.get("date")) for row in rows if row.get("date")})
    index_rows = _read_csv(data_dir / "history" / "indices_daily.csv")
    sse = {str(row.get("date")): _number(row.get("change_percent")) for row in index_rows if row.get("symbol") == "000001"}
    csi300 = {str(row.get("date")): _number(row.get("change_percent")) for row in index_rows if row.get("symbol") == "000300"}
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_code[str(row.get("code"))].append(row)
    full_history: list[dict[str, Any]] = []
    for day in dates[-10:]:
        daily = [row for row in rows if row.get("date") == day]
        daily.sort(key=lambda row: int(float(row.get("rank") or 9999)))
        total_amount = sum(_number(row.get("amount")) or 0 for row in daily)
        sectors = []
        for rank, row in enumerate(daily, 1):
            change = _number(row.get("change_percent"))
            source = str(row.get("source") or "unknown")
            taxonomy = "sina_legacy" if "sina" in source else "eastmoney_industry" if "eastmoney" in source else "unknown_fallback"
            raw = {
                "industry_code": row.get("code"), "industry_name": row.get("name"), "bucket_time": day,
                "change_percent": change, "relative_return_shanghai": _round(change - sse[day]) if change is not None and sse.get(day) is not None else None,
                "relative_return_hs300": _round(change - csi300[day]) if change is not None and csi300.get(day) is not None else None,
                "amount": _number(row.get("amount")), "turnover_share": _round((_number(row.get("amount")) or 0) / total_amount, 8) if total_amount else None,
                "rs_1d": _round(change - sse[day]) if change is not None and sse.get(day) is not None else None,
                "rs_5d": _compound_relative(by_code[str(row.get("code"))], sse, 5, source),
                "rs_10d": _compound_relative(by_code[str(row.get("code"))], sse, 10, source),
                "rs_20d": _compound_relative(by_code[str(row.get("code"))], sse, 20, source),
            }
            sectors.append(_rotation_public(raw, rank, taxonomy, len(dates) >= 2))
        full_history.append({"date": day, "sectors": sectors})
    current = full_history[-1]["sectors"] if full_history else []
    compact_history = [{"date": day["date"], "sectors": _compact_rotation_history(day["sectors"])} for day in full_history]
    _rotation_rank_context(current, compact_history)
    sources = sorted({str(row.get("source")) for row in rows if row.get("source")})
    taxonomy = current[0].get("taxonomy") if current else None
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_time": dates[-1] if dates else None, "data_as_of": dates[-1] if dates else None,
        "backend": "csv", "sources": sources, "taxonomy": taxonomy, "comparable": len(dates) >= 2,
        "quality": {"grade": "yellow" if current else "red", "sector_count": len(current), "daily_history_days": len(dates)},
        "known_limitations": [
            "CSV降级仅有日频行业涨跌、成交额和排名；15/30/60分钟、扩散度、领涨股持续性及rotation_score/state保持null。",
            "跨日相对强弱只在板块source完全一致且交易日输入充足时计算。",
            "轮动描述来自价格和成交，不代表真实资金净流入或机构行为。",
        ],
        "sectors": current, "recent_daily_history": compact_history,
    }


def build_rotation_context(root: Path, data_dir: Path, generated_at: datetime) -> dict[str, Any]:
    return _build_sqlite_rotation(data_dir, generated_at) or _build_csv_rotation(data_dir, generated_at)


def _breadth_row(raw: dict[str, Any], current: bool = False) -> dict[str, Any]:
    return {
        "date": raw.get("date") or str(raw.get("market_time") or "")[:10] or None,
        "timestamp": raw.get("market_time") if current else raw.get("date"),
        "advance": int(float(raw.get("up") or 0)), "decline": int(float(raw.get("down") or 0)),
        "flat": int(float(raw.get("flat") or 0)), "approx_limit_up": int(float(raw.get("limit_up") or 0)),
        "approx_limit_down": int(float(raw.get("limit_down") or 0)),
        "total_amount": _number(raw.get("turnover_cny")),
        "market_median_return": _number(raw.get("median_change_percent")),
        "listed_with_quotes": int(float(raw.get("listed_with_quotes") or 0)),
        "source": raw.get("source"), "method": raw.get("method") or ("realtime_snapshot" if current else None),
    }


def build_market_breadth_context(data_dir: Path, generated_at: datetime, market: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = market or _read_json(data_dir / "latest.json")
    raw_current = snapshot.get("market_breadth") or {}
    current = _breadth_row(raw_current, True) if raw_current else None
    rows = [_breadth_row(row) for row in _read_csv(data_dir / "history" / "market_breadth_daily.csv")][-10:]
    if current and (not rows or rows[-1].get("date") != current.get("date")):
        rows.append(current)
    sources = sorted({str(row.get("source")) for row in rows if row.get("source")})
    dates = sorted({str(row.get("date")) for row in rows if row.get("date")})
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_time": current.get("timestamp") if current else None, "data_as_of": dates[-1] if dates else None,
        "backend": "csv", "sources": sources,
        "quality": {"grade": "yellow" if current else "red", "current_available": current is not None, "historical_trading_days": len(dates)},
        "known_limitations": [
            "市场宽度历史只保存程序实际观察到的快照，漏跑日期不使用当前股票池伪造回填。",
            "涨跌停家数按ST、主板、创业板/科创板和北交所阈值近似统计。",
        ],
        "historical_coverage_limited": True, "current": current, "recent_history": rows,
    }


def build_watchlist_context(
    root: Path, data_dir: Path, generated_at: datetime, market: dict[str, Any] | None = None,
    intraday: dict[str, Any] | None = None, provider: UnifiedHistoryProvider | None = None,
) -> dict[str, Any]:
    config = yaml.safe_load((root / "config" / "watchlist.yaml").read_text(encoding="utf-8")) or {}
    items = config.get("watchlist") or []
    snapshot = market or _read_json(data_dir / "latest.json")
    minute = intraday or _read_json(data_dir / "latest_intraday.json")
    quotes = {(str(row.get("kind")), str(row.get("symbol"))): row for row in snapshot.get("watchlist") or []}
    intraday_items = {(str(row.get("kind")), str(row.get("symbol"))): row for row in minute.get("instruments") or []}
    history = provider or UnifiedHistoryProvider(data_dir)
    instruments = []
    for item in items:
        key = (str(item.get("kind")), str(item.get("symbol")))
        technical = _instrument_context(history, item, generated_at, False)
        instruments.append({
            **technical,
            "current_quote": quotes.get(key),
            "intraday_summary": (intraday_items.get(key) or {}).get("metrics"),
            "intraday_source": (intraday_items.get(key) or {}).get("source"),
        })
    missing_quotes = [item.get("name") for item in instruments if item.get("current_quote") is None]
    missing_intraday = [item.get("name") for item in instruments if item.get("intraday_summary") is None]
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at.isoformat(timespec="seconds"),
        "market_time": snapshot.get("market_time") or minute.get("market_time"),
        "data_as_of": max((str(item.get("data_as_of") or "") for item in instruments), default="") or None,
        "backend": "mixed" if len({item.get("backend") for item in instruments}) > 1 else (instruments[0].get("backend") if instruments else None),
        "sources": sorted({source for item in instruments for source in item.get("sources") or []} | {str(source) for source in snapshot.get("sources") or []}),
        "quality": {"grade": "yellow" if instruments and not missing_quotes else "red", "instrument_count": len(instruments), "missing_current_quotes": missing_quotes, "missing_intraday_summaries": missing_intraday},
        "known_limitations": [
            "当前行情和分时来自公共近实时接口，不是交易所授权行情。",
            "技术结构只使用可靠日K；成交额缺失时保持null，不以成交量乘价格估算。",
        ],
        "instruments": instruments,
    }


def generate_research_context(
    root: Path = ROOT, data_dir: Path | None = None, generated_at: datetime | None = None,
    market: dict[str, Any] | None = None, intraday: dict[str, Any] | None = None,
) -> tuple[list[Path], list[str]]:
    generated_at = generated_at or datetime.now(SHANGHAI)
    data_dir = data_dir or root / "data"
    provider = UnifiedHistoryProvider(data_dir)
    builders = {
        "market_technical.json": lambda: build_market_technical(root, data_dir, generated_at, provider),
        "rotation_context.json": lambda: build_rotation_context(root, data_dir, generated_at),
        "market_breadth_context.json": lambda: build_market_breadth_context(data_dir, generated_at, market),
        "watchlist_context.json": lambda: build_watchlist_context(root, data_dir, generated_at, market, intraday, provider),
    }
    written: list[Path] = []
    errors: list[str] = []
    for filename, builder in builders.items():
        try:
            path = data_dir / "research_context" / filename
            _atomic_json(path, builder())
            written.append(path)
        except Exception as exc:
            errors.append(f"{filename}: {type(exc).__name__}: {exc}")
    return written, errors


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出供ChatGPT读取的轻量A股研究上下文")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    paths, errors = generate_research_context(args.root.resolve())
    for path in paths:
        print(path)
    for error in errors:
        print(f"WARNING: research_context生成失败: {error}", file=sys.stderr)
    return 0 if paths else 2


if __name__ == "__main__":
    raise SystemExit(cli())
