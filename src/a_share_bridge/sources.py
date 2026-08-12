from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _float(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    result = _float(value)
    return None if result is None else int(result)


def _iso_from_epoch(value: Any) -> str | None:
    epoch = _int(value)
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, SHANGHAI).isoformat(timespec="seconds")


def _iso_from_compact(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Instrument:
    name: str
    symbol: str
    kind: str
    eastmoney: str
    tencent: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.tencent}"


class QuoteProvider(Protocol):
    name: str
    available: bool

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]: ...

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any]: ...


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.35,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32))
    session.mount("http://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32))
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session


class FallbackChain:
    def __init__(self, providers: Iterable[QuoteProvider]):
        self.providers = list(providers)
        self.errors: list[str] = []
        self.reports: list[dict[str, Any]] = []

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        remaining = {item.key: item for item in instruments}
        merged: dict[str, dict[str, Any]] = {}
        for provider in self.providers:
            if not remaining or not provider.available:
                continue
            try:
                result = provider.fetch_quotes(list(remaining.values()))
                accepted = 0
                for key, quote in result.items():
                    if key in remaining and quote.get("last") is not None:
                        merged[key] = quote
                        remaining.pop(key)
                        accepted += 1
                self.reports.append({"source": provider.name, "operation": "quotes", "ok": True, "records": accepted})
            except Exception as exc:  # provider isolation is intentional
                provider.available = False
                message = f"{provider.name}实时行情失败: {type(exc).__name__}: {exc}"
                self.errors.append(message)
                self.reports.append({"source": provider.name, "operation": "quotes", "ok": False, "error": str(exc)})
        for item in remaining.values():
            self.errors.append(f"实时行情缺失: {item.name}({item.symbol})")
        return merged

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any] | None:
        for provider in self.providers:
            if not provider.available:
                continue
            try:
                result = provider.fetch_intraday(instrument)
                if result.get("points"):
                    self.reports.append(
                        {"source": provider.name, "operation": f"intraday:{instrument.tencent}", "ok": True, "records": len(result["points"])}
                    )
                    return result
            except Exception as exc:  # provider isolation is intentional
                message = f"{provider.name}分时失败 {instrument.name}: {type(exc).__name__}: {exc}"
                self.errors.append(message)
                self.reports.append(
                    {"source": provider.name, "operation": f"intraday:{instrument.tencent}", "ok": False, "error": str(exc)}
                )
        self.errors.append(f"分时数据缺失: {instrument.name}({instrument.symbol})")
        return None


class EastMoneySource:
    name = "eastmoney"

    def __init__(self, timeout: float = 12):
        self.timeout = timeout
        self.available = True
        self.session = build_session()
        self.session.headers["Referer"] = "https://quote.eastmoney.com/"

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        response = self.session.get(
            url,
            params={
                "fltt": 2,
                "secids": ",".join(item.eastmoney for item in instruments),
                "fields": "f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f124",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        rows = (payload.get("data") or {}).get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        by_secid = {f"{row.get('f13')}.{row.get('f12')}": row for row in rows}
        result: dict[str, dict[str, Any]] = {}
        for item in instruments:
            row = by_secid.get(item.eastmoney)
            if not row:
                continue
            result[item.key] = {
                "name": row.get("f14") or item.name,
                "symbol": item.symbol,
                "kind": item.kind,
                "last": _float(row.get("f2")),
                "change_percent": _float(row.get("f3")),
                "change": _float(row.get("f4")),
                "volume": _int(row.get("f5")),
                "amount": _float(row.get("f6")),
                "high": _float(row.get("f15")),
                "low": _float(row.get("f16")),
                "open": _float(row.get("f17")),
                "previous_close": _float(row.get("f18")),
                "market_time": _iso_from_epoch(row.get("f124")),
                "source": self.name,
                "is_stale": None,
                "errors": [],
                "warnings": [],
            }
        if not result:
            raise RuntimeError("接口返回空行情")
        return result

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any]:
        response = self.session.get(
            "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
            params={
                "secid": instrument.eastmoney,
                "ndays": 1,
                "iscr": 0,
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = (response.json().get("data") or {})
        raw_points = data.get("trends") or []
        points: list[dict[str, Any]] = []
        for raw in raw_points:
            parts = raw.split(",")
            if len(parts) < 7:
                continue
            points.append(
                {
                    "time": parts[0],
                    "price": _float(parts[1]),
                    "high": _float(parts[2]),
                    "low": _float(parts[3]),
                    "volume": _float(parts[4]),
                    "amount": _float(parts[5]),
                    "average_price": _float(parts[6]),
                }
            )
        if not points:
            raise RuntimeError("接口返回空分时")
        return {
            "source": self.name,
            "market_date": points[-1]["time"][:10],
            "market_time": points[-1]["time"].replace(" ", "T") + ":00+08:00",
            "points": points,
            "errors": [],
            "warnings": [],
        }

    def fetch_all_market(self) -> list[dict[str, Any]]:
        response = self.session.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1,
                "pz": 6000,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f14,f2,f3,f6,f124",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = ((response.json().get("data") or {}).get("diff") or [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        result = [
            {
                "symbol": str(row.get("f12", "")),
                "name": row.get("f14"),
                "last": _float(row.get("f2")),
                "change_percent": _float(row.get("f3")),
                "amount": _float(row.get("f6")),
                "market_time": _iso_from_epoch(row.get("f124")),
                "source": self.name,
            }
            for row in rows
        ]
        if not result:
            raise RuntimeError("接口返回空全市场行情")
        return result

    def fetch_industries(self) -> list[dict[str, Any]]:
        response = self.session.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1,
                "pz": 500,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:90+t:2+f:!50",
                "fields": "f12,f14,f3,f6,f8,f62",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = ((response.json().get("data") or {}).get("diff") or [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        return [
            {
                "code": str(row.get("f12", "")),
                "name": row.get("f14"),
                "change_percent": _float(row.get("f3")),
                "amount": _float(row.get("f6")),
                "turnover_rate": _float(row.get("f8")),
                "net_inflow": _float(row.get("f62")),
                "source": self.name,
            }
            for row in rows
            if _float(row.get("f3")) is not None
        ]


class TencentSource:
    name = "tencent"

    def __init__(self, timeout: float = 12):
        self.timeout = timeout
        self.available = True
        self.session = build_session()
        self.session.headers["Referer"] = "https://gu.qq.com/"

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        response = self.session.get(
            "https://qt.gtimg.cn/q=" + ",".join(item.tencent for item in instruments),
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        rows = dict(re.findall(r'v_([^=]+)="([^"]*)"', response.text))
        result: dict[str, dict[str, Any]] = {}
        for item in instruments:
            raw = rows.get(item.tencent)
            if not raw:
                continue
            fields = raw.split("~")
            if len(fields) < 38:
                continue
            amount_wan = _float(fields[37])
            result[item.key] = {
                "name": fields[1] or item.name,
                "symbol": item.symbol,
                "kind": item.kind,
                "last": _float(fields[3]),
                "previous_close": _float(fields[4]),
                "open": _float(fields[5]),
                "volume": _int(fields[6]),
                "market_time": _iso_from_compact(fields[30]),
                "change": _float(fields[31]),
                "change_percent": _float(fields[32]),
                "high": _float(fields[33]),
                "low": _float(fields[34]),
                "amount": amount_wan * 10000 if amount_wan is not None else None,
                "source": self.name,
                "is_stale": None,
                "errors": [],
                "warnings": [],
            }
        if not result:
            raise RuntimeError("接口返回空行情")
        return result

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any]:
        response = self.session.get(
            "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
            params={"code": instrument.tencent},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        block = (payload.get("data") or {}).get(instrument.tencent) or {}
        data = block.get("data") or {}
        market_date = str(data.get("date") or "")
        raw_points = data.get("data") or []
        points: list[dict[str, Any]] = []
        previous_volume = 0.0
        previous_amount = 0.0
        for raw in raw_points:
            parts = raw.split()
            if len(parts) < 4:
                continue
            cumulative_volume = _float(parts[2]) or 0.0
            cumulative_amount = _float(parts[3]) or 0.0
            minute_volume = max(cumulative_volume - previous_volume, 0.0)
            minute_amount = max(cumulative_amount - previous_amount, 0.0)
            previous_volume = cumulative_volume
            previous_amount = cumulative_amount
            hhmm = parts[0]
            minute_of_day = int(hhmm[:2]) * 60 + int(hhmm[2:])
            if not (570 <= minute_of_day <= 690 or 780 <= minute_of_day <= 900):
                continue
            timestamp = f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:8]} {hhmm[:2]}:{hhmm[2:]}"
            points.append(
                {
                    "time": timestamp,
                    "price": _float(parts[1]),
                    "volume": minute_volume,
                    "amount": minute_amount,
                    "volume_cumulative": cumulative_volume,
                    "amount_cumulative": cumulative_amount,
                }
            )
        if not points:
            raise RuntimeError("接口返回空分时")
        return {
            "source": self.name,
            "market_date": f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:8]}",
            "market_time": points[-1]["time"].replace(" ", "T") + ":00+08:00",
            "points": points,
            "errors": [],
            "warnings": [],
        }


class SinaSource:
    name = "sina"

    def __init__(self, timeout: float = 12, max_pages: int = 65):
        self.timeout = timeout
        self.max_pages = max_pages
        self.available = True
        self.session = build_session()
        self.session.headers["Referer"] = "https://finance.sina.com.cn/"
        self.warnings: list[str] = []

    def _market_page(self, page: int) -> list[dict[str, Any]]:
        response = self.session.get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            params={
                "page": page,
                "num": 100,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def fetch_all_market(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._market_page, page): page for page in range(1, self.max_pages + 1)}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    failures.append(f"page {page}: {exc}")
        if failures:
            self.warnings.append(f"新浪全市场有{len(failures)}页失败；统计可能不完整")
        if not rows:
            raise RuntimeError("接口返回空全市场行情")
        today = datetime.now(SHANGHAI).date().isoformat()
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("code") or row.get("symbol") or "").removeprefix("sh").removeprefix("sz").removeprefix("bj")
            tick = str(row.get("ticktime") or "15:00:00")
            unique[symbol] = {
                "symbol": symbol,
                "name": row.get("name"),
                "last": _float(row.get("trade")),
                "change_percent": _float(row.get("changepercent")),
                "amount": _float(row.get("amount")),
                "market_time": f"{today}T{tick}+08:00",
                "source": self.name,
            }
        return list(unique.values())

    def fetch_industries(self) -> list[dict[str, Any]]:
        response = self.session.get(
            "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        match = re.search(r"=\s*(\{.*\})\s*;?\s*$", response.text, re.S)
        if not match:
            raise RuntimeError("无法解析行业响应")
        payload = json.loads(match.group(1))
        result: list[dict[str, Any]] = []
        for raw in payload.values():
            parts = str(raw).split(",")
            if len(parts) < 8 or _float(parts[5]) is None:
                continue
            result.append(
                {
                    "code": parts[0],
                    "name": parts[1],
                    "constituent_count": _int(parts[2]),
                    "change_percent": _float(parts[5]),
                    "volume": _float(parts[6]),
                    "amount": _float(parts[7]),
                    "turnover_rate": None,
                    "net_inflow": None,
                    "leader_symbol": parts[8] if len(parts) > 8 else None,
                    "leader_name": parts[12] if len(parts) > 12 else None,
                    "source": self.name,
                }
            )
        if not result:
            raise RuntimeError("接口返回空行业行情")
        return result

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any]:
        response = self.session.get(
            "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": instrument.tencent, "scale": 1, "ma": "no", "datalen": 1023},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise RuntimeError("接口返回空分时")
        latest_date = max(str(row.get("day", ""))[:10] for row in payload)
        points: list[dict[str, Any]] = []
        cumulative_volume = 0.0
        cumulative_amount = 0.0
        for row in payload:
            timestamp = str(row.get("day") or "")
            if not timestamp.startswith(latest_date):
                continue
            hhmm = timestamp[11:16]
            try:
                minute_of_day = int(hhmm[:2]) * 60 + int(hhmm[3:])
            except ValueError:
                continue
            if not (570 <= minute_of_day <= 690 or 780 <= minute_of_day <= 900):
                continue
            volume = _float(row.get("volume")) or 0.0
            amount = _float(row.get("amount")) or 0.0
            cumulative_volume += volume
            cumulative_amount += amount
            points.append(
                {
                    "time": timestamp[:16],
                    "price": _float(row.get("close")),
                    "open": _float(row.get("open")),
                    "high": _float(row.get("high")),
                    "low": _float(row.get("low")),
                    "volume": volume,
                    "amount": amount,
                    "volume_cumulative": cumulative_volume,
                    "amount_cumulative": cumulative_amount,
                }
            )
        if not points:
            raise RuntimeError("接口未返回最新交易日分时")
        warnings = []
        if points[0]["time"][11:16] != "09:30":
            warnings.append(f"新浪分时首条为{points[0]['time'][11:16]}，接口未提供09:30分钟点")
        if len(points) < 240:
            warnings.append(f"新浪分时仅返回{len(points)}个连续竞价分钟点，未对缺失分钟进行插值")
        return {
            "source": self.name,
            "market_date": latest_date,
            "market_time": points[-1]["time"].replace(" ", "T") + ":00+08:00",
            "points": points,
            "errors": [],
            "warnings": warnings,
        }
