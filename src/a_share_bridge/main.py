from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .analytics import calculate_intraday_metrics, calculate_market_breadth, is_quote_stale
from .publisher import publish_outputs
from .rendering import write_outputs
from .sources import EastMoneySource, FallbackChain, Instrument, SinaSource, TencentSource
from .trading_calendar import is_a_share_trading_day

SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[2]


def _unique(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(message for message in messages if message))


def load_config(path: Path) -> tuple[list[Instrument], list[Instrument], dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def convert(rows: list[dict[str, Any]]) -> list[Instrument]:
        return [Instrument(**row) for row in rows]

    indices = convert(payload.get("indices") or [])
    watchlist = convert(payload.get("watchlist") or [])
    if not indices:
        raise ValueError("config/watchlist.yaml 未配置主要指数")
    return indices, watchlist, payload.get("settings") or {}


def collect(config_path: Path, output_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    generated = datetime.now(SHANGHAI)
    indices, watchlist, settings = load_config(config_path)
    instruments = indices + watchlist
    timeout = float(settings.get("request_timeout_seconds", 12))
    eastmoney = EastMoneySource(timeout)
    tencent = TencentSource(timeout)
    chain = FallbackChain([eastmoney, tencent])

    quotes = chain.fetch_quotes(instruments)
    for quote in quotes.values():
        quote["is_stale"] = is_quote_stale(quote.get("market_time"), generated)

    errors = list(chain.errors)
    warnings: list[str] = []
    source_reports = list(chain.reports)

    all_market: list[dict[str, Any]] = []
    breadth_source = None
    if eastmoney.available:
        try:
            all_market = eastmoney.fetch_all_market()
            breadth_source = eastmoney.name
            source_reports.append({"source": eastmoney.name, "operation": "breadth", "ok": True, "records": len(all_market)})
        except Exception as exc:
            errors.append(f"eastmoney全市场行情失败: {type(exc).__name__}: {exc}")
            eastmoney.available = False

    sina = SinaSource(timeout, int(settings.get("max_sina_pages", 65)))
    if not all_market:
        try:
            all_market = sina.fetch_all_market()
            breadth_source = sina.name
            source_reports.append({"source": sina.name, "operation": "breadth", "ok": True, "records": len(all_market)})
            warnings.extend(sina.warnings)
        except Exception as exc:
            errors.append(f"sina全市场行情失败: {type(exc).__name__}: {exc}")
            source_reports.append({"source": sina.name, "operation": "breadth", "ok": False, "error": str(exc)})

    breadth = calculate_market_breadth(all_market)
    breadth["source"] = breadth_source
    breadth["market_time"] = max((row.get("market_time") or "" for row in all_market), default=None)
    breadth["is_stale"] = is_quote_stale(breadth.get("market_time"), generated)
    breadth["errors"] = [] if all_market else ["全市场行情不可用"]
    breadth["warnings"] = [breadth["limit_count_method"]]

    industries: list[dict[str, Any]] = []
    if eastmoney.available:
        try:
            industries = eastmoney.fetch_industries()
            source_reports.append({"source": eastmoney.name, "operation": "industries", "ok": True, "records": len(industries)})
        except Exception as exc:
            errors.append(f"eastmoney行业行情失败: {type(exc).__name__}: {exc}")
    if not industries:
        try:
            industries = sina.fetch_industries()
            source_reports.append({"source": sina.name, "operation": "industries", "ok": True, "records": len(industries)})
        except Exception as exc:
            errors.append(f"sina行业行情失败: {type(exc).__name__}: {exc}")
            source_reports.append({"source": sina.name, "operation": "industries", "ok": False, "error": str(exc)})
    industries.sort(key=lambda row: float(row.get("change_percent") or 0), reverse=True)

    intraday_items: list[dict[str, Any]] = []
    chain.providers.append(sina)
    intraday_report_start = len(chain.reports)

    def fetch_one(item: Instrument) -> tuple[Instrument, dict[str, Any] | None]:
        return item, chain.fetch_intraday(item)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_one, item) for item in instruments]
        for future in as_completed(futures):
            item, data = future.result()
            if not data:
                continue
            quote = quotes.get(item.key) or {}
            data.update(
                {
                    "name": item.name,
                    "symbol": item.symbol,
                    "kind": item.kind,
                    "is_stale": is_quote_stale(data.get("market_time"), generated),
                    "metrics": calculate_intraday_metrics(data["points"], quote.get("previous_close")),
                }
            )
            if item.kind == "index":
                data["warnings"].append("指数VWAP为按指数分钟成交量加权的指数点位，不是ETF价格、IOPV或基金净值")
            intraday_items.append(data)
    order = {item.key: index for index, item in enumerate(instruments)}
    instrument_by_tuple = {(item.kind, item.symbol): item.key for item in instruments}
    intraday_items.sort(key=lambda item: order[instrument_by_tuple[(item["kind"], item["symbol"])]])

    for item in intraday_items:
        for message in item.get("warnings") or []:
            warnings.append(f"{item['name']}: {message}")

    errors.extend(chain.errors)
    source_reports.extend(chain.reports[intraday_report_start:])
    errors = _unique(errors)
    warnings = _unique(warnings + [breadth["limit_count_method"]])

    index_quotes = [quotes[item.key] for item in indices if item.key in quotes]
    watch_quotes = [quotes[item.key] for item in watchlist if item.key in quotes]
    market_times = [quote.get("market_time") for quote in quotes.values() if quote.get("market_time")]
    market_times.extend(item.get("market_time") for item in intraday_items if item.get("market_time"))
    market_time = max(market_times, default=None)
    sources = sorted(
        {quote.get("source") for quote in quotes.values() if quote.get("source")}
        | {item.get("source") for item in intraday_items if item.get("source")}
        | ({breadth_source} if breadth_source else set())
        | {row.get("source") for row in industries if row.get("source")}
    )

    snapshot = {
        "schema_version": "1.0",
        "market_time": market_time,
        "generated_at": generated.isoformat(timespec="seconds"),
        "sources": sources,
        "is_stale": all(quote.get("is_stale", True) for quote in quotes.values()) if quotes else True,
        "errors": errors,
        "warnings": warnings,
        "source_status": source_reports,
        "indices": index_quotes,
        "market_breadth": breadth,
        "industries": {"top10": industries[:10], "bottom10": list(reversed(industries[-10:]))},
        "watchlist": watch_quotes,
    }
    intraday = {
        "schema_version": "1.0",
        "market_time": max((item.get("market_time") or "" for item in intraday_items), default=None),
        "generated_at": generated.isoformat(timespec="seconds"),
        "sources": sorted({item.get("source") for item in intraday_items if item.get("source")}),
        "is_stale": all(item.get("is_stale", True) for item in intraday_items) if intraday_items else True,
        "errors": errors,
        "warnings": warnings,
        "source_status": source_reports,
        "instruments": intraday_items,
    }
    paths = write_outputs(snapshot, intraday, output_dir)
    return snapshot, intraday, paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="获取A股实时行情和完整1分钟分时")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "watchlist.yaml")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--scheduled", action="store_true", help="启用A股交易日检查")
    publish = parser.add_mutually_exclusive_group()
    publish.add_argument("--publish", action="store_true", help="提交并推送四个生成文件")
    publish.add_argument("--no-publish", action="store_true", help="禁止自动发布")
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(SHANGHAI)
    settings = (yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}).get("settings") or {}
    if args.scheduled:
        trading_day, warning = is_a_share_trading_day(now.date())
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)
        if not trading_day:
            print(f"{now.date()} 不是A股交易日，跳过。")
            return 0

    snapshot, intraday, paths = collect(args.config, args.output_dir)
    print(f"实时行情: {len(snapshot['indices'])} 个指数，{len(snapshot['watchlist'])} 个watchlist标的")
    print(f"分时行情: {len(intraday['instruments'])} 个标的")
    for path in paths:
        print(path)

    should_publish = args.publish or (
        args.scheduled and bool(settings.get("publish_on_scheduled_run", True)) and not args.no_publish
    )
    if should_publish:
        result = publish_outputs(ROOT, paths, now)
        print(f"发布结果: {result}")
        if result["errors"]:
            return 3
    return 0 if snapshot["indices"] else 2


if __name__ == "__main__":
    raise SystemExit(cli())
