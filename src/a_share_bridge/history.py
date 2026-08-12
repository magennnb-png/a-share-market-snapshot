from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

from .sources import EastMoneySource, Instrument, SinaSource, _float, build_session
from .trading_calendar import is_a_share_trading_day

INDEX_FIELDS = ["date", "symbol", "name", "open", "high", "low", "close", "change_percent", "volume", "amount", "source"]
BREADTH_FIELDS = ["date", "up", "down", "flat", "limit_up", "limit_down", "turnover_cny", "listed_with_quotes", "source", "method"]
INDUSTRY_FIELDS = ["date", "code", "name", "change_percent", "amount", "activity", "rank", "turnover_rate", "net_inflow", "source"]
ROTATION_FIELDS = ["date", "leader_1", "leader_2", "leader_3", "laggard_1", "laggard_2", "laggard_3", "industry_count", "source"]


class HistoryError(RuntimeError):
    pass


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _merge(rows: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(field) or "") for field in keys)
        if all(key):
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


def fetch_daily_kline(source: EastMoneySource, secid: str, begin: date, end: date) -> list[dict[str, Any]]:
    response = source.session.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": secid, "klt": 101, "fqt": 0,
            "beg": begin.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
            "lmt": 10000, "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        timeout=source.timeout,
    )
    response.raise_for_status()
    data = response.json().get("data") or {}
    result: list[dict[str, Any]] = []
    for raw in data.get("klines") or []:
        parts = raw.split(",")
        if len(parts) < 9:
            continue
        result.append({
            "date": parts[0], "open": _float(parts[1]), "close": _float(parts[2]),
            "high": _float(parts[3]), "low": _float(parts[4]), "volume": _float(parts[5]),
            "amount": _float(parts[6]), "change_percent": _float(parts[8]), "source": source.name,
        })
    return result


def fetch_sohu_daily(session: Any, symbol: str, begin: date, end: date) -> list[dict[str, Any]]:
    response = session.get(
        "https://q.stock.sohu.com/hisHq",
        params={"code": f"cn_{symbol}", "start": begin.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
                "stat": 1, "order": "D", "period": "d", "rt": "json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    block = payload[0] if isinstance(payload, list) and payload else {}
    rows = []
    for parts in block.get("hq") or []:
        if len(parts) < 9:
            continue
        rows.append({
            "date": parts[0], "open": _float(parts[1]), "close": _float(parts[2]),
            "change_percent": _float(str(parts[4]).rstrip("%")), "low": _float(parts[5]),
            "high": _float(parts[6]), "volume": _float(parts[7]),
            # Sohu reports amount in 10k CNY.
            "amount": (_float(parts[8]) or 0) * 10_000, "source": "sohu",
        })
    return rows


def fetch_sina_daily(session: Any, symbol: str, begin: date, end: date, length: int = 650) -> list[dict[str, Any]]:
    response = session.get(
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": length}, timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    rows = []
    previous = None
    for item in payload:
        day = str(item.get("day") or "")[:10]
        if not day or day < begin.isoformat() or day > end.isoformat():
            continue
        close = _float(item.get("close"))
        change = (close / previous - 1) * 100 if close is not None and previous else None
        rows.append({"date": day, "open": _float(item.get("open")), "close": close,
                     "high": _float(item.get("high")), "low": _float(item.get("low")),
                     "volume": _float(item.get("volume")), "amount": None,
                     "change_percent": round(change, 4) if change is not None else None, "source": "sina"})
        previous = close
    return rows


def fetch_tencent_daily(session: Any, symbol: str, begin: date, end: date, length: int = 650) -> list[dict[str, Any]]:
    response = session.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,{begin.isoformat()},{end.isoformat()},{length},qfq"}, timeout=15,
    )
    response.raise_for_status()
    block = (response.json().get("data") or {}).get(symbol) or {}
    payload = block.get("qfqday") or block.get("day") or []
    rows = []
    previous = None
    for parts in payload:
        if len(parts) < 6 or not isinstance(parts, list):
            continue
        close = _float(parts[2])
        change = (close / previous - 1) * 100 if close is not None and previous else None
        rows.append({"date": parts[0], "open": _float(parts[1]), "close": close,
                     "high": _float(parts[3]), "low": _float(parts[4]), "volume": _float(parts[5]),
                     "amount": _float(parts[6]) if len(parts) > 6 else None,
                     "change_percent": round(change, 4) if change is not None else None, "source": "tencent"})
        previous = close
    return rows


def fetch_tencent_extended(session: Any, symbol: str, begin: date, end: date, length: int = 650) -> list[dict[str, Any]]:
    response = session.get(
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
        params={"_var": "kline_dayqfq", "param": f"{symbol},day,{begin.isoformat()},{end.isoformat()},{length},qfq", "r": "0.82"},
        timeout=15,
    )
    response.raise_for_status()
    text = response.text
    payload = json.loads(text[text.find("={") + 1 :]) if "={" in text else response.json()
    block = (payload.get("data") or {}).get(symbol) or {}
    points = block.get("qfqday") or block.get("day") or []
    rows = []
    previous = None
    for parts in points:
        if len(parts) < 6:
            continue
        if parts[0] < begin.isoformat() or parts[0] > end.isoformat():
            continue
        close = _float(parts[2])
        change = (close / previous - 1) * 100 if close is not None and previous else None
        amount = _float(parts[8]) * 10_000 if len(parts) > 8 and _float(parts[8]) is not None else None
        rows.append({"date": parts[0], "open": _float(parts[1]), "close": close, "high": _float(parts[3]),
                     "low": _float(parts[4]), "volume": _float(parts[5]), "amount": amount,
                     "change_percent": round(change, 4) if change is not None else None, "source": "tencent"})
        previous = close
    return rows


def fetch_csindex_daily(session: Any, symbol: str, begin: date, end: date) -> list[dict[str, Any]]:
    response = session.get(
        "https://www.csindex.com.cn/csindex-home/perf/index-perf",
        params={"indexCode": symbol, "startDate": begin.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")},
        timeout=20,
    )
    response.raise_for_status()
    result = []
    for item in response.json().get("data") or []:
        day = str(item.get("tradeDate") or "")
        if len(day) != 8:
            continue
        parsed_day = date(int(day[:4]), int(day[4:6]), int(day[6:]))
        # CSIndex repeats the next trading day's value at a non-trading begin
        # boundary (for example Sunday). Never persist that synthetic row.
        if not is_a_share_trading_day(parsed_day)[0]:
            continue
        result.append({"date": parsed_day.isoformat(), "open": _float(item.get("open")),
                       "close": _float(item.get("close")), "high": _float(item.get("high")),
                       "low": _float(item.get("low")), "volume": _float(item.get("tradingVol")),
                       # The official API reports tradingValue in CNY 100m.
                       "amount": (_float(item.get("tradingValue")) or 0) * 100_000_000,
                       "change_percent": _float(item.get("changePct")), "source": "csindex"})
    return result


def _valid_ohlc(row: dict[str, Any]) -> bool:
    values = [_float(row.get(field)) for field in ("open", "high", "low", "close")]
    if any(value is None or value <= 0 for value in values):
        return False
    open_, high, low, close = values
    return bool(high >= max(open_, close) and low <= min(open_, close) and high >= low)


def _update_instrument_history(
    path: Path, instruments: list[Instrument], settings: dict[str, Any], source: EastMoneySource, end: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    existing = _read_csv(path)
    wanted = int(settings.get("history_index_days", 500))
    lookback = wanted * 2

    fallback_session = build_session()

    def fetch(item: Instrument) -> list[dict[str, Any]]:
        # Always refetch a rolling window. This repairs gaps automatically even
        # when the user has not run the tool for days or weeks.
        begin = end - timedelta(days=lookback)
        try:
            rows = fetch_csindex_daily(fallback_session, item.symbol, begin, end)
        except Exception:
            rows = []
        if not rows:
            try:
                rows = fetch_daily_kline(source, item.eastmoney, begin, end)
            except Exception:
                rows = []
        if not rows and item.tencent:
            try:
                length = min(650, max(10, (end - begin).days + 5))
                rows = fetch_tencent_extended(fallback_session, item.tencent, begin, end, length)
            except Exception:
                rows = []
        if not rows and item.tencent:
            rows = fetch_tencent_daily(fallback_session, item.tencent, begin, end)
        if not rows and item.tencent:
            rows = fetch_sina_daily(fallback_session, item.tencent, begin, end)
        if not rows and item.kind != "history_index":
            rows = fetch_sohu_daily(fallback_session, item.symbol, begin, end)
        for row in rows:
            row.update(symbol=item.symbol, name=item.name)
        return rows

    new: list[dict[str, Any]] = []
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(instruments)))) as executor:
        futures = {executor.submit(fetch, item): item for item in instruments}
        for future in as_completed(futures):
            item = futures[future]
            try:
                rows = future.result()
                if not rows:
                    warnings.append(f"历史行情为空: {item.name}({item.symbol})")
                new.extend(rows)
            except Exception as exc:
                warnings.append(f"历史行情失败: {item.name}({item.symbol}): {exc}")
    merged = _merge([*existing, *new], ("date", "symbol"))
    merged = [row for row in merged if is_a_share_trading_day(date.fromisoformat(str(row["date"])))[0]]
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in merged:
        by_symbol[str(row["symbol"])].append(row)
    merged = []
    for rows in by_symbol.values():
        merged.extend(sorted(rows, key=lambda row: str(row["date"]))[-wanted:])
    merged = _merge(merged, ("date", "symbol"))
    complete_symbols = {
        symbol for symbol, rows in by_symbol.items()
        if len(rows) >= min(250, wanted)
    }
    warnings = [
        warning for warning in warnings
        if not any(f"({symbol})" in warning for symbol in complete_symbols)
    ]
    _write_csv(path, INDEX_FIELDS, merged)
    return merged, warnings


def _fetch_stock_universe(source: EastMoneySource) -> list[dict[str, str]]:
    params = {"pn": 1, "pz": 6000, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
              "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12,f13,f14"}
    for host in ("push2.eastmoney.com", "17.push2.eastmoney.com", "82.push2.eastmoney.com"):
        try:
            response = source.session.get(f"https://{host}/api/qt/clist/get", params=params, timeout=source.timeout)
            response.raise_for_status()
            rows = (response.json().get("data") or {}).get("diff") or []
            if isinstance(rows, dict):
                rows = rows.values()
            universe = [{"symbol": str(row.get("f12")), "name": str(row.get("f14") or ""), "secid": f"{row.get('f13')}.{row.get('f12')}"} for row in rows]
            if len(universe) >= 1000:
                return universe
        except Exception:
            continue

    # The Sina listing endpoint is already an established fallback in the
    # realtime collector.  Derive EastMoney's market prefix from the A-share
    # code so history backfill can continue when the EM listing host is down.
    sina = SinaSource(source.timeout, max_pages=65)
    rows = sina.fetch_all_market()
    universe = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        market = "1" if symbol.startswith(("5", "6", "9")) else "0"
        universe.append({"symbol": symbol, "name": str(row.get("name") or ""), "secid": f"{market}.{symbol}"})
    if len(universe) < 1000:
        raise HistoryError(f"股票样本仅 {len(universe)} 只，无法回补市场宽度")
    return universe


def _limit_percent(symbol: str, name: str) -> float:
    if "ST" in name.upper().replace(" ", ""):
        return 5.0
    if symbol.startswith(("300", "301", "688", "689")):
        return 20.0
    if symbol.startswith(("4", "8", "92")):
        return 30.0
    return 10.0


def _backfill_breadth(source: EastMoneySource, begin: date, end: date, workers: int) -> list[dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = defaultdict(lambda: {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "turnover_cny": 0.0, "listed_with_quotes": 0})
    universe = _fetch_stock_universe(source)
    print(f"  首次市场宽度回补：{len(universe)} 只股票，{begin} 至 {end}...", flush=True)

    tencent = build_session()

    def fetch(item: dict[str, str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
        symbol = item["symbol"]
        prefix = "sh" if symbol.startswith(("5", "6", "9")) else "bj" if symbol.startswith(("4", "8")) else "sz"
        return item, fetch_tencent_extended(tencent, prefix + symbol, begin, end)

    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, item) for item in universe]
        completed = 0
        for future in as_completed(futures):
            completed += 1
            if completed % 500 == 0 or completed == len(futures):
                print(f"  市场宽度回补进度：{completed}/{len(futures)}", flush=True)
            try:
                item, rows = future.result()
            except Exception:
                failures += 1
                continue
            threshold = _limit_percent(item["symbol"], item["name"])
            for row in rows:
                change = _float(row.get("change_percent")) or 0.0
                target = aggregates[row["date"]]
                target["listed_with_quotes"] += 1
                target["turnover_cny"] += _float(row.get("amount")) or 0.0
                target["up" if change > 0 else "down" if change < 0 else "flat"] += 1
                target["limit_up"] += int(change >= threshold - 0.2)
                target["limit_down"] += int(change <= -(threshold - 0.2))
    if failures > len(universe) * 0.1:
        raise HistoryError(f"市场宽度回补失败股票 {failures}/{len(universe)}，超过10%阈值")
    return [{"date": day, **values, "turnover_cny": round(values["turnover_cny"], 2), "source": "eastmoney", "method": "current_universe_daily_kline"} for day, values in sorted(aggregates.items())]


def _update_breadth(path: Path, snapshot: dict[str, Any], settings: dict[str, Any], source: EastMoneySource, end: date) -> list[dict[str, Any]]:
    # Historical breadth cannot be reconstructed reliably from today's listed
    # universe (doing so introduces survivorship bias). Keep only snapshots
    # actually observed by this tool and append/replace the current day.
    existing = [row for row in _read_csv(path) if row.get("method") == "realtime_snapshot"]
    market_time = snapshot.get("market_time")
    breadth = snapshot.get("market_breadth") or {}
    if market_time and breadth.get("listed_with_quotes", 0) >= 1000:
        existing.append({"date": str(market_time)[:10], **breadth, "source": breadth.get("source"), "method": "realtime_snapshot"})
    merged = _merge(existing, ("date",))
    _write_csv(path, BREADTH_FIELDS, merged)
    return merged


def _has_daily_coverage(rows: list[dict[str, Any]], end: date, wanted: int) -> bool:
    counts = Counter(str(row.get("date")) for row in rows)
    complete = sorted(day for day, count in counts.items() if count >= 20)
    if len(complete) < wanted:
        return False
    last = date.fromisoformat(complete[-1])
    # During the trading day today's official industry daily bar may not exist;
    # yesterday (or the latest trading day before a holiday) is sufficient.
    return (end - last).days <= 4


def _update_industries(path: Path, snapshot: dict[str, Any], settings: dict[str, Any], source: EastMoneySource, end: date) -> list[dict[str, Any]]:
    existing = _read_csv(path)
    wanted = int(settings.get("history_industry_days", 120))
    begin = end - timedelta(days=wanted * 2)
    industries = (snapshot.get("industries") or {}).get("all") or []
    # Rebuild whenever the rolling window is incomplete or stale. Consecutive
    # runs on the same day reuse the already-complete window.
    needs_backfill = not _has_daily_coverage(existing, end, wanted)

    def fetch(item: dict[str, Any]) -> list[dict[str, Any]]:
        rows = fetch_daily_kline(source, f"90.{item['code']}", begin, end)
        for row in rows:
            row.update(code=item["code"], name=item["name"])
        return rows

    new: list[dict[str, Any]] = []
    if needs_backfill:
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(industries)))) as executor:
            futures = [executor.submit(fetch, item) for item in industries]
            for future in as_completed(futures):
                try:
                    new.extend(future.result())
                except Exception:
                    continue
    # EastMoney's history host is occasionally unavailable while its realtime
    # host still works.  A complete first run must not depend on that single
    # endpoint, so rebuild a transparent equal-weight industry series from
    # Sina's public industry membership and Tencent's stock daily bars.
    if needs_backfill and len({row.get("date") for row in new}) < wanted:
        new = _backfill_industries_from_stocks(begin, end, int(settings.get("history_workers", 16)))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in new:
        grouped[row["date"]].append(row)
    normalized: list[dict[str, Any]] = []
    for day, rows in grouped.items():
        rows.sort(key=lambda row: _float(row.get("change_percent")) or 0, reverse=True)
        amounts = sorted((_float(row.get("amount")) or 0 for row in rows))
        for rank, row in enumerate(rows, 1):
            amount = _float(row.get("amount")) or 0
            percentile = (amounts.index(amount) + 1) / len(amounts) * 100 if amounts else None
            normalized.append({**row, "rank": rank, "activity": round(percentile, 4) if percentile else None, "turnover_rate": None, "net_inflow": None})
    market_day = str(snapshot.get("market_time") or "")[:10]
    # Same-day reruns replace today's provisional industry rows with the
    # newest realtime snapshot. Historical dates remain immutable.
    if needs_backfill:
        existing = []
    else:
        existing = [row for row in existing if row.get("date") != market_day]
    if market_day not in grouped or not needs_backfill:
        for item in industries:
            normalized.append({"date": market_day, **item})
    merged = _merge([*existing, *normalized], ("date", "code"))
    counts = Counter(row["date"] for row in merged)
    complete_days = sorted(day for day, count in counts.items() if count >= 20)
    if len(complete_days) >= wanted:
        keep = set(complete_days[-max(wanted + 40, wanted):])
        merged = [row for row in merged if row["date"] in keep]
    _write_csv(path, INDUSTRY_FIELDS, merged)
    return merged


def _sina_industry_members() -> dict[str, dict[str, Any]]:
    session = build_session()
    session.headers.update({"Referer": "https://finance.sina.com.cn"})
    response = session.get("https://money.finance.sina.com.cn/q/view/newSinaHy.php", timeout=30)
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise HistoryError("新浪行业目录格式无效")
    catalog = json.loads(text[start : end + 1])
    result: dict[str, dict[str, Any]] = {}
    endpoint = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    for node, raw in catalog.items():
        parts = str(raw).split(",")
        name = parts[1] if len(parts) > 1 else node
        try:
            payload = session.get(
                endpoint,
                params={"page": 1, "num": 1000, "sort": "symbol", "asc": 1,
                        "node": node, "symbol": "", "_s_r_a": "page"},
                timeout=30,
            ).json()
        except Exception:
            continue
        members = {str(row.get("symbol") or "") for row in payload or [] if row.get("symbol")}
        if len(members) >= 3:
            result[node] = {"name": name, "members": members}
    if len(result) < 20:
        raise HistoryError(f"新浪行业分类仅取得 {len(result)} 个行业")
    return result


def _backfill_industries_from_stocks(begin: date, end: date, workers: int) -> list[dict[str, Any]]:
    catalog = _sina_industry_members()
    memberships: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for code, item in catalog.items():
        for symbol in item["members"]:
            memberships[symbol].append((code, item["name"]))
    print(f"  首次行业历史回补：{len(catalog)} 个行业，{len(memberships)} 只成分股...", flush=True)
    session = build_session()

    def fetch(symbol: str) -> tuple[str, list[dict[str, Any]]]:
        return symbol, fetch_tencent_extended(session, symbol, begin, end)

    daily: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"changes": [], "amount": 0.0, "quoted": 0}
    )
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, symbol) for symbol in memberships]
        completed = 0
        for future in as_completed(futures):
            completed += 1
            if completed % 500 == 0 or completed == len(futures):
                print(f"  行业历史回补进度：{completed}/{len(futures)}", flush=True)
            try:
                symbol, bars = future.result()
            except Exception:
                failures += 1
                continue
            for bar in bars:
                change = _float(bar.get("change_percent"))
                if change is None:
                    continue
                for code, name in memberships[symbol]:
                    bucket = daily[(bar["date"], code, name)]
                    bucket["changes"].append(change)
                    bucket["amount"] += _float(bar.get("amount")) or 0.0
                    bucket["quoted"] += 1
    if failures > len(memberships) * 0.1:
        raise HistoryError(f"行业历史回补失败股票 {failures}/{len(memberships)}，超过10%阈值")
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (day, code, name), values in daily.items():
        if values["quoted"] < 3:
            continue
        by_day[day].append({
            "date": day, "code": code, "name": name,
            "change_percent": round(sum(values["changes"]) / len(values["changes"]), 4),
            "amount": round(values["amount"], 2), "turnover_rate": None,
            "net_inflow": None, "source": "sina_membership+tencent_equal_weight",
        })
    result: list[dict[str, Any]] = []
    for day, rows in by_day.items():
        rows.sort(key=lambda row: _float(row.get("change_percent")) or 0, reverse=True)
        by_amount = sorted(rows, key=lambda row: _float(row.get("amount")) or 0)
        activity = {row["code"]: (rank + 1) / len(by_amount) * 100 for rank, row in enumerate(by_amount)}
        for rank, row in enumerate(rows, 1):
            result.append({**row, "rank": rank, "activity": round(activity[row["code"]], 4)})
    return result


def _rotation(industry_rows: list[dict[str, Any]], output_dir: Path, generated_at: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in industry_rows:
        grouped[str(row.get("date"))].append(row)
    result = []
    for day, rows in sorted(grouped.items()):
        if len(rows) < 3:
            continue
        ranked = sorted(rows, key=lambda row: int(float(row.get("rank") or 9999)))
        laggards = sorted(rows, key=lambda row: int(float(row.get("rank") or 0)), reverse=True)
        result.append({"date": day, "leader_1": ranked[0]["name"], "leader_2": ranked[1]["name"], "leader_3": ranked[2]["name"], "laggard_1": laggards[0]["name"], "laggard_2": laggards[1]["name"], "laggard_3": laggards[2]["name"], "industry_count": len(rows), "source": ranked[0].get("source")})
    _write_csv(output_dir / "history" / "rotation_daily.csv", ROTATION_FIELDS, result)
    latest = result[-1] if result else {}
    payload = {"schema_version": "1.0", "market_time": latest.get("date"), "generated_at": generated_at, "rotation": latest, "history_file": "data/history/rotation_daily.csv"}
    (output_dir / "latest_rotation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = "# A股行业轮动\n\n" + f"- 行情日期：{latest.get('date', '—')}\n- 领涨：{latest.get('leader_1', '—')}、{latest.get('leader_2', '—')}、{latest.get('leader_3', '—')}\n- 领跌：{latest.get('laggard_1', '—')}、{latest.get('laggard_2', '—')}、{latest.get('laggard_3', '—')}\n- 历史：`data/history/rotation_daily.csv`\n"
    (output_dir / "latest_rotation.md").write_text(markdown, encoding="utf-8")
    return result


def archive_intraday(intraday: dict[str, Any], output_dir: Path, keep_days: int) -> None:
    market_day = str(intraday.get("market_time") or "")[:10]
    if not market_day:
        return
    archive = output_dir / "history" / "intraday"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"{market_day}.json"
    target.write_text(json.dumps(intraday, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    for stale in sorted(archive.glob("*.json"), reverse=True)[keep_days:]:
        stale.unlink()


def update_history(config_path: Path, output_dir: Path, snapshot: dict[str, Any], intraday: dict[str, Any]) -> dict[str, Any]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    settings = config.get("settings") or {}
    indices = [Instrument(**row) for row in config.get("indices") or []]
    watch_rows = list(config.get("history_watchlist") or [])
    known = {str(row["symbol"]) for row in watch_rows}
    for row in config.get("watchlist") or []:
        if row.get("kind") in {"index", "etf"} and str(row.get("symbol")) not in known:
            watch_rows.append(row)
            known.add(str(row["symbol"]))
    watch = [Instrument(name=row["name"], symbol=str(row["symbol"]), kind="history_index", eastmoney=row["eastmoney"], tencent=row.get("tencent", "")) for row in watch_rows]
    source = EastMoneySource(float(settings.get("request_timeout_seconds", 12)))
    end = datetime.fromisoformat(snapshot["market_time"]).date()
    history_dir = output_dir / "history"
    index_rows, index_warnings = _update_instrument_history(history_dir / "indices_daily.csv", indices, settings, source, end)
    watch_rows, watch_warnings = _update_instrument_history(history_dir / "watchlist_daily.csv", watch, settings, source, end)
    breadth_rows = _update_breadth(history_dir / "market_breadth_daily.csv", snapshot, settings, source, end)
    industry_rows = _update_industries(history_dir / "industries_daily.csv", snapshot, settings, source, end)
    rotation_rows = _rotation(industry_rows, output_dir, snapshot["generated_at"])
    archive_intraday(intraday, output_dir, int(settings.get("intraday_retention_days", 10)))
    return {"indices": index_rows, "watchlist": watch_rows, "breadth": breadth_rows, "industries": industry_rows, "rotation": rotation_rows, "warnings": [*index_warnings, *watch_warnings]}


def validate_history(result: dict[str, Any], required_index_days: int = 250, required_breadth_days: int = 120, required_industry_days: int = 120) -> list[str]:
    errors: list[str] = []
    for label in ("indices", "watchlist"):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in result[label]:
            grouped[str(row.get("symbol"))].append(row)
        for symbol, rows in grouped.items():
            days = {row.get("date") for row in rows}
            if len(days) < required_index_days:
                errors.append(f"{label} {symbol} 仅 {len(days)} 个交易日，要求至少 {required_index_days}")
            if any(not _valid_ohlc(row) for row in rows[-required_index_days:]):
                errors.append(f"{label} {symbol} 存在非法OHLC")
            missing_amount = sum(1 for row in rows[-required_index_days:] if _float(row.get("amount")) is None)
            if missing_amount:
                errors.append(f"{label} {symbol} 最近{required_index_days}日有 {missing_amount} 日成交额缺失")
        minimum_instruments = 7 if label == "indices" else 3
        if len(grouped) < minimum_instruments:
            errors.append(f"{label} 仅 {len(grouped)} 个标的，要求至少 {minimum_instruments}")
    breadth_days = {row.get("date") for row in result["breadth"]}
    if len(breadth_days) < required_breadth_days:
        errors.append(f"市场宽度仅 {len(breadth_days)} 个交易日，要求至少 {required_breadth_days}")
    industry_days = {row.get("date") for row in result["industries"]}
    if len(industry_days) < required_industry_days:
        errors.append(f"行业历史仅 {len(industry_days)} 个交易日，要求至少 {required_industry_days}")
    industry_counts = Counter(row.get("date") for row in result["industries"])
    sparse = [day for day, count in industry_counts.items() if count < 20]
    if sparse:
        errors.append(f"行业历史存在 {len(sparse)} 个不足20行业的稀疏日期")
    return errors
