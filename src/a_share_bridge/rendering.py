from __future__ import annotations

import json
from pathlib import Path
from typing import Any


OUTPUT_NAMES = ("latest.json", "latest.md", "latest_intraday.json", "latest_intraday.md")


def _display(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _amount(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) / 100_000_000:.2f} 亿元"


def render_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        "# A股实时行情快照",
        "",
        f"- 行情时间：{snapshot.get('market_time') or '—'}",
        f"- 生成时间：{snapshot.get('generated_at')}",
        f"- 数据来源：{', '.join(snapshot.get('sources', [])) or '—'}",
        f"- 是否过期：{'是' if snapshot.get('is_stale') else '否'}",
        "",
        "## 主要指数",
        "",
        "| 名称 | 代码 | 最新 | 涨跌幅 | 开盘 | 最高 | 最低 | 成交额 | 来源 | 过期 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for item in snapshot.get("indices", []):
        lines.append(
            f"| {item['name']} | {item['symbol']} | {_display(item.get('last'))} | "
            f"{_display(item.get('change_percent'))}% | {_display(item.get('open'))} | "
            f"{_display(item.get('high'))} | {_display(item.get('low'))} | {_amount(item.get('amount'))} | "
            f"{item.get('source')} | {'是' if item.get('is_stale') else '否'} |"
        )

    breadth = snapshot.get("market_breadth") or {}
    lines.extend(
        [
            "",
            "## 全市场",
            "",
            f"- 有效行情数：{breadth.get('listed_with_quotes', '—')}",
            f"- 上涨 / 下跌 / 平盘：{breadth.get('up', '—')} / {breadth.get('down', '—')} / {breadth.get('flat', '—')}",
            f"- 涨停 / 跌停：{breadth.get('limit_up', '—')} / {breadth.get('limit_down', '—')}",
            f"- 全市场成交额：{_amount(breadth.get('turnover_cny'))}",
            f"- 统计来源：{breadth.get('source', '—')}",
            "",
            "## 行业涨跌榜",
            "",
            "### 前十",
            "",
            "| 行业 | 涨跌幅 | 成交额 | 来源 |",
            "|---|---:|---:|---|",
        ]
    )
    for item in (snapshot.get("industries") or {}).get("top10", []):
        lines.append(f"| {item['name']} | {_display(item.get('change_percent'))}% | {_amount(item.get('amount'))} | {item.get('source')} |")
    lines.extend(["", "### 后十", "", "| 行业 | 涨跌幅 | 成交额 | 来源 |", "|---|---:|---:|---|"])
    for item in (snapshot.get("industries") or {}).get("bottom10", []):
        lines.append(f"| {item['name']} | {_display(item.get('change_percent'))}% | {_amount(item.get('amount'))} | {item.get('source')} |")

    lines.extend(
        [
            "",
            "## Watchlist",
            "",
            "| 类型 | 名称 | 代码 | 最新 | 涨跌幅 | 成交额 | 来源 | 过期 |",
            "|---|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for item in snapshot.get("watchlist", []):
        lines.append(
            f"| {item['kind']} | {item['name']} | {item['symbol']} | {_display(item.get('last'))} | "
            f"{_display(item.get('change_percent'))}% | {_amount(item.get('amount'))} | {item.get('source')} | "
            f"{'是' if item.get('is_stale') else '否'} |"
        )

    lines.extend(["", "## 错误和警告", ""])
    errors = snapshot.get("errors") or []
    warnings = snapshot.get("warnings") or []
    if not errors and not warnings:
        lines.append("无。")
    for message in errors:
        lines.append(f"- 错误：{message}")
    for message in warnings:
        lines.append(f"- 警告：{message}")
    lines.extend(["", "> 本项目只记录公开行情，不包含券商账户、身份信息、持仓、Token 或密码。"])
    return "\n".join(lines) + "\n"


def render_intraday_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        "# A股完整 1 分钟分时",
        "",
        f"- 行情时间：{snapshot.get('market_time') or '—'}",
        f"- 生成时间：{snapshot.get('generated_at')}",
        f"- 数据来源：{', '.join(snapshot.get('sources', [])) or '—'}",
        f"- 是否过期：{'是' if snapshot.get('is_stale') else '否'}",
        "",
        "## 指标摘要",
        "",
        "| 类型 | 名称 | 代码 | 分钟数 | 开盘 | 最新 | VWAP | 最大回撤 | 日内位置 | 高点回落 | 低点修复 | 形态 | 来源 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for item in snapshot.get("instruments", []):
        metrics = item.get("metrics") or {}
        lines.append(
            f"| {item['kind']} | {item['name']} | {item['symbol']} | {len(item.get('points', []))} | "
            f"{_display(metrics.get('open'))} | {_display(metrics.get('last'))} | {_display(metrics.get('vwap'))} | "
            f"{_display(metrics.get('max_drawdown_percent'))}% | {_display(metrics.get('intraday_position'), 4)} | "
            f"{_display(metrics.get('pullback_from_high_percent'))}% | {_display(metrics.get('recovery_from_low_percent'))}% | "
            f"{', '.join(metrics.get('patterns', [])) or '—'} | {item.get('source')} |"
        )

    for item in snapshot.get("instruments", []):
        lines.extend(
            [
                "",
                f"## {item['name']}（{item['symbol']}）",
                "",
                f"来源：{item.get('source')}；行情时间：{item.get('market_time')}；过期：{'是' if item.get('is_stale') else '否'}",
                "",
                "| 时间 | 价格 | 分钟成交量 | 分钟成交额 | 累计成交量 | 累计成交额 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for point in item.get("points", []):
            lines.append(
                f"| {point.get('time')} | {_display(point.get('price'), 4)} | {_display(point.get('volume'), 0)} | "
                f"{_display(point.get('amount'), 2)} | {_display(point.get('volume_cumulative'), 0)} | "
                f"{_display(point.get('amount_cumulative'), 2)} |"
            )

    lines.extend(["", "## 错误和警告", ""])
    for message in snapshot.get("errors") or []:
        lines.append(f"- 错误：{message}")
    for message in snapshot.get("warnings") or []:
        lines.append(f"- 警告：{message}")
    if not snapshot.get("errors") and not snapshot.get("warnings"):
        lines.append("无。")
    return "\n".join(lines) + "\n"


def write_outputs(snapshot: dict[str, Any], intraday: dict[str, Any], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "latest.json": json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        "latest.md": render_snapshot_markdown(snapshot),
        "latest_intraday.json": json.dumps(intraday, ensure_ascii=False, indent=2) + "\n",
        "latest_intraday.md": render_intraday_markdown(intraday),
    }
    written: list[Path] = []
    for name, content in paths.items():
        target = output_dir / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
        written.append(target)
    return written
