from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from a_share_bridge.publisher import publish_outputs
from a_share_bridge.trading_calendar import is_a_share_trading_day

SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
DEFAULT_LATEST = ROOT / "data" / "latest.json"
DEFAULT_INTRADAY = ROOT / "data" / "latest_intraday.json"
DEFAULT_PROMPT = ROOT / "config" / "report_prompt.md"
DEFAULT_REPORTS = ROOT / "reports"


@dataclass(frozen=True)
class ReportSlot:
    code: str
    title: str
    purpose: str


SLOTS = {
    "0905": ReportSlot("0905", "开盘前分析", "建立开盘基线、验证条件与失效条件"),
    "1105": ReportSlot("1105", "盘中验证", "验证上午风险偏好、风格和分时路径"),
    "1405": ReportSlot("1405", "美股预判", "把A股信号映射到美股相关资产并明确证据边界"),
}


def _float(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: Iterable[Any]) -> float | None:
    valid = [_float(value) for value in values]
    numbers = [value for value in valid if value is not None]
    return statistics.fmean(numbers) if numbers else None


def _pct(value: Any) -> str:
    number = _float(value)
    return "—" if number is None else f"{number:+.2f}%"


def _num(value: Any, digits: int = 2) -> str:
    number = _float(value)
    return "—" if number is None else f"{number:.{digits}f}"


def _amount(value: Any) -> str:
    number = _float(value)
    return "—" if number is None else f"{number / 100_000_000:.2f}亿元"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"输入文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"输入文件不是有效JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"输入文件根节点必须是对象: {path}")
    return payload


def load_inputs(latest_path: Path, intraday_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = _load_json(latest_path)
    intraday = _load_json(intraday_path)
    latest_required = {"generated_at", "market_time", "indices", "market_breadth", "industries", "watchlist"}
    intraday_required = {"generated_at", "market_time", "instruments"}
    missing_latest = sorted(latest_required - latest.keys())
    missing_intraday = sorted(intraday_required - intraday.keys())
    if missing_latest:
        raise ValueError(f"latest.json缺少字段: {', '.join(missing_latest)}")
    if missing_intraday:
        raise ValueError(f"latest_intraday.json缺少字段: {', '.join(missing_intraday)}")
    latest_generated = _parse_datetime(str(latest.get("generated_at") or ""))
    intraday_generated = _parse_datetime(str(intraday.get("generated_at") or ""))
    if not latest_generated or not intraday_generated:
        raise ValueError("输入缺少可解析的generated_at")
    if latest_generated.date() != intraday_generated.date():
        raise ValueError("latest.json与latest_intraday.json不是同一生成日期")
    return latest, intraday


def resolve_slot(value: str, now: datetime) -> ReportSlot:
    if value != "auto":
        try:
            return SLOTS[value]
        except KeyError as exc:
            raise ValueError(f"不支持的报告时段: {value}") from exc
    current = now.timetz().replace(tzinfo=None)
    if current < time(10, 0):
        return SLOTS["0905"]
    if current < time(13, 0):
        return SLOTS["1105"]
    return SLOTS["1405"]


def _instrument_metrics(intraday: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("kind")), str(item.get("symbol"))): item
        for item in intraday.get("instruments") or []
    }


def _index_by_name(latest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("name")): item for item in latest.get("indices") or []}


def analyze_market(latest: dict[str, Any], intraday: dict[str, Any]) -> dict[str, Any]:
    indices = latest.get("indices") or []
    breadth = latest.get("market_breadth") or {}
    changes = [_float(item.get("change_percent")) for item in indices]
    valid_changes = [value for value in changes if value is not None]
    median_change = statistics.median(valid_changes) if valid_changes else None
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    advance_ratio = up / (up + down) if up + down else None

    if median_change is not None and advance_ratio is not None and median_change >= 0.5 and advance_ratio >= 0.58:
        tone = "风险偏好占优"
        tone_detail = "多数主要指数与市场宽度同向，强势并非只由少数权重股贡献。"
    elif median_change is not None and advance_ratio is not None and median_change <= -0.5 and advance_ratio <= 0.42:
        tone = "风险规避占优"
        tone_detail = "主要指数与市场宽度同步走弱，防守优先于追逐弹性。"
    else:
        tone = "结构分化"
        tone_detail = "指数方向与个股宽度没有形成一致强信号，需要按风格和分时位置拆解。"

    by_name = _index_by_name(latest)

    def style_score(names: list[str]) -> float | None:
        return _mean((by_name.get(name) or {}).get("change_percent") for name in names)

    style_scores = {
        "大盘权重": style_score(["上证指数", "沪深300"]),
        "成长科技": style_score(["创业板指", "科创50"]),
        "中小盘": style_score(["中证1000", "北证50"]),
    }
    ranked_styles = sorted(
        ((name, value) for name, value in style_scores.items() if value is not None),
        key=lambda pair: pair[1],
        reverse=True,
    )
    leader_style = ranked_styles[0][0] if ranked_styles else "暂无"
    laggard_style = ranked_styles[-1][0] if ranked_styles else "暂无"
    metrics_map = _instrument_metrics(intraday)

    return {
        "median_index_change": median_change,
        "advance_ratio": advance_ratio,
        "tone": tone,
        "tone_detail": tone_detail,
        "style_scores": style_scores,
        "leader_style": leader_style,
        "laggard_style": laggard_style,
        "metrics_map": metrics_map,
    }


def _data_quality(latest: dict[str, Any], intraday: dict[str, Any], slot: ReportSlot) -> dict[str, Any]:
    raw_errors = list(dict.fromkeys([*(latest.get("errors") or []), *(intraday.get("errors") or [])]))
    raw_warnings = list(dict.fromkeys([*(latest.get("warnings") or []), *(intraday.get("warnings") or [])]))
    errors: list[str] = []
    for raw in raw_errors:
        message = str(raw)
        if "eastmoney实时行情失败" in message:
            summary = "东方财富实时行情失败；指数和watchlist已由腾讯回退。"
        elif "tencent分时失败 北证50" in message:
            summary = "腾讯北证50分时为空；北证50分时已由新浪回退。"
        else:
            summary = message if len(message) <= 240 else message[:237] + "..."
        if summary not in errors:
            errors.append(summary)
    warnings: list[str] = []
    index_vwap_added = False
    for raw in raw_warnings:
        message = str(raw)
        if "指数VWAP为按指数分钟成交量加权" in message:
            if index_vwap_added:
                continue
            message = "指数VWAP为按指数分钟成交量加权的指数点位，不是ETF价格、IOPV或基金净值。"
            index_vwap_added = True
        if message not in warnings:
            warnings.append(message)
    stale = bool(latest.get("is_stale")) or bool(intraday.get("is_stale"))
    expected = len(latest.get("indices") or []) + len(latest.get("watchlist") or [])
    actual = len(intraday.get("instruments") or [])
    if stale or actual < expected:
        confidence = "低"
    elif errors:
        confidence = "中"
    else:
        confidence = "高"
    if slot.code == "1405":
        confidence = "低（仅A股映射，缺少美股直接输入）"
    return {"errors": errors, "warnings": warnings, "stale": stale, "confidence": confidence, "expected": expected, "actual": actual}


def _index_table(latest: dict[str, Any], metrics_map: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    lines = [
        "| 指数 | 涨跌幅 | 最新 | 日内位置 | 高点回落 | 低点修复 | 分时形态 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in latest.get("indices") or []:
        intraday = metrics_map.get(("index", str(item.get("symbol")))) or {}
        metrics = intraday.get("metrics") or {}
        patterns = "、".join(metrics.get("patterns") or []) or "—"
        lines.append(
            f"| {item.get('name')} | {_pct(item.get('change_percent'))} | {_num(item.get('last'))} | "
            f"{_num(metrics.get('intraday_position'), 3)} | {_pct(metrics.get('pullback_from_high_percent'))} | "
            f"{_pct(metrics.get('recovery_from_low_percent'))} | {patterns} |"
        )
    return lines


def _industry_table(latest: dict[str, Any]) -> list[str]:
    top = (latest.get("industries") or {}).get("top10") or []
    bottom = (latest.get("industries") or {}).get("bottom10") or []
    lines = [
        "| 强势行业 | 涨跌幅 | 弱势行业 | 涨跌幅 |",
        "|---|---:|---|---:|",
    ]
    for index in range(max(min(len(top), 5), min(len(bottom), 5))):
        strong = top[index] if index < len(top) and index < 5 else {}
        weak = bottom[index] if index < len(bottom) and index < 5 else {}
        lines.append(
            f"| {strong.get('name', '—')} | {_pct(strong.get('change_percent'))} | "
            f"{weak.get('name', '—')} | {_pct(weak.get('change_percent'))} |"
        )
    return lines


def _watchlist_table(latest: dict[str, Any], metrics_map: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    lines = [
        "| 类型 | 标的 | 涨跌幅 | 日内位置 | VWAP | 形态 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for item in latest.get("watchlist") or []:
        intraday = metrics_map.get((str(item.get("kind")), str(item.get("symbol")))) or {}
        metrics = intraday.get("metrics") or {}
        patterns = "、".join(metrics.get("patterns") or []) or "—"
        lines.append(
            f"| {item.get('kind')} | {item.get('name')}（{item.get('symbol')}） | {_pct(item.get('change_percent'))} | "
            f"{_num(metrics.get('intraday_position'), 3)} | {_num(metrics.get('vwap'), 4)} | {patterns} |"
        )
    return lines


def _slot_section(slot: ReportSlot, latest: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
    breadth = latest.get("market_breadth") or {}
    leader = analysis["leader_style"]
    laggard = analysis["laggard_style"]
    top = ((latest.get("industries") or {}).get("top10") or [{}])[0]
    bottom = ((latest.get("industries") or {}).get("bottom10") or [{}])[0]
    if slot.code == "0905":
        return [
            "## 开盘前先把方向写成可验证条件",
            "",
            "**当前数据用于建立基线，不把集合竞价或上一交易日分时直接当作开盘后趋势。** 开盘后应优先检查指数方向与上涨家数是否同步，再判断风格信号是否成立。",
            "",
            "| 情景 | 确认条件 | 失效条件 |",
            "|---|---|---|",
            f"| 风险偏好延续 | 主要指数中位涨跌幅维持在{_pct(analysis['median_index_change'])}附近或改善，且上涨占比高于58% | 指数上行但上涨占比跌破50% |",
            f"| {leader}继续占优 | 对应风格指数开盘后日内位置保持在0.65以上 | 风格指数跌至VWAP下方且日内位置低于0.35 |",
            f"| 行业扩散 | {top.get('name', '领先行业')}之外出现新的强势行业 | 领涨集中在单一行业且涨停扩散停滞 |",
            "",
            "**下一步：** 09:30后用第一小时分时验证，不因09:05时点缺少连续竞价成交而提高仓位判断置信度。",
        ]
    if slot.code == "1105":
        advance_ratio = analysis.get("advance_ratio")
        breadth_text = "—" if advance_ratio is None else f"{advance_ratio:.1%}"
        return [
            "## 上午信号是否得到市场宽度确认",
            "",
            f"**{analysis['tone']}。** 上涨家数占上涨与下跌家数之和的{breadth_text}，主要指数涨跌幅中位数为{_pct(analysis['median_index_change'])}。{analysis['tone_detail']}",
            "",
            "| 验证维度 | 当前证据 | 解释 |",
            "|---|---|---|",
            f"| 市场宽度 | 上涨{breadth.get('up', '—')}家、下跌{breadth.get('down', '—')}家 | {'扩散有效' if advance_ratio is not None and advance_ratio >= 0.58 else '尚未形成强扩散'} |",
            f"| 风格 | {leader}领先，{laggard}相对落后 | 午后观察领先风格能否维持VWAP上方 |",
            f"| 行业 | {top.get('name', '—')}居前，{bottom.get('name', '—')}居后 | 关注领涨是否从单一行业扩散 |",
            "",
            "**午后验证条件：** 若上涨占比、领先风格日内位置和行业扩散同时走弱，则上午结论降级为脉冲；若三者维持，则风险偏好信号继续有效。",
        ]
    return [
        "## A股信号对美股相关资产的映射",
        "",
        f"**当前映射为{analysis['tone']}，领先风格是{leader}。** 这只能用于观察美股中概、中国收入敞口和相近成长风格的情绪起点，不能替代美股期指、美元、利率和ADR盘前行情。",
        "",
        "| 美股情景 | A股映射证据 | 需要补充确认 |",
        "|---|---|---|",
        f"| 中概/中国敞口偏积极 | A股上涨宽度改善且{leader}领先 | 中概ADR盘前、离岸人民币、恒生科技走势同向 |",
        f"| 成长风格情绪外溢 | 创业板/科创或中小盘保持高日内位置 | 纳指期货、美国实际利率未反向压制 |",
        f"| 映射失效 | A股尾盘从高点显著回落，或{bottom.get('name', '弱势行业')}拖累扩散 | 美股盘前风险资产与A股信号背离 |",
        "",
        "**结论边界：** 在补齐美股直接输入前，报告只给方向映射和验证清单，不给确定性涨跌预测。",
    ]


def render_report(
    latest: dict[str, Any],
    intraday: dict[str, Any],
    slot: ReportSlot,
    prompt_text: str,
) -> str:
    generated = _parse_datetime(str(latest.get("generated_at")))
    if not generated:
        raise ValueError("latest.json的generated_at不可解析")
    report_date = generated.date().isoformat()
    analysis = analyze_market(latest, intraday)
    quality = _data_quality(latest, intraday, slot)
    breadth = latest.get("market_breadth") or {}
    advance_ratio = analysis.get("advance_ratio")
    breadth_text = "—" if advance_ratio is None else f"{advance_ratio:.1%}"
    prompt_digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]
    source_names = sorted(set(latest.get("sources") or []) | set(intraday.get("sources") or []))
    top_names = "、".join(str(item.get("name")) for item in ((latest.get("industries") or {}).get("top10") or [])[:3]) or "无"

    lines = [
        f"# {report_date} A股{slot.title}",
        "",
        f"> 报告时段：{slot.code[:2]}:{slot.code[2:]}｜行情时间：{latest.get('market_time') or '—'}｜生成时间：{latest.get('generated_at')}｜来源：{', '.join(source_names) or '—'}｜置信度：{quality['confidence']}",
        "",
        "## Executive Summary（执行摘要）",
        "",
        f"- **市场状态：{analysis['tone']}。** 主要指数涨跌幅中位数为{_pct(analysis['median_index_change'])}，上涨占比为{breadth_text}；{analysis['tone_detail']}",
        f"- **风格线索：{analysis['leader_style']}领先。** 相对落后的是{analysis['laggard_style']}，行业前三为{top_names}，需要用分时位置确认是否只是短时脉冲。",
        f"- **行动含义：{slot.purpose}。** 所有判断均以条件表达；数据质量置信度为{quality['confidence']}，不会把接口回退或缺失输入隐藏在结论中。",
        "",
        "## 指数与宽度给出的市场底色",
        "",
        f"**全市场成交额为{_amount(breadth.get('turnover_cny'))}，上涨{breadth.get('up', '—')}家、下跌{breadth.get('down', '—')}家、近似涨停{breadth.get('limit_up', '—')}家、跌停{breadth.get('limit_down', '—')}家。** 指数表用于核对方向是否由市场宽度和日内位置共同支持。",
        "",
        *_index_table(latest, analysis["metrics_map"]),
        "",
        "## 行业强弱是否形成扩散",
        "",
        "**行业榜只描述当前横截面，不把单次排名直接解释为持续主线。** 强弱两端并列展示，用于观察领涨扩散与拖累来源。",
        "",
        *_industry_table(latest),
        "",
        *_slot_section(slot, latest, analysis),
        "",
        "## Watchlist的路径证据",
        "",
        "**交易价格、VWAP和日内位置用于验证路径，不代表ETF IOPV或基金净值。** 日内位置接近1表示靠近当日高位，接近0表示靠近当日低位。",
        "",
        *_watchlist_table(latest, analysis["metrics_map"]),
        "",
        "## 下一观察点",
        "",
        f"1. 观察上涨占比能否维持在当前{breadth_text}附近；跌回50%以下意味着扩散信号明显减弱。",
        f"2. 观察{analysis['leader_style']}代表指数是否保持VWAP上方和0.65以上日内位置。",
        "3. 观察行业前三是否出现扩散，或仅由单一行业和少数高弹性标的支撑。",
        "4. 若主要指数从高点回落幅度扩大，同时上涨家数下降，应优先下调趋势延续置信度。",
        "",
        "## Further Questions（待补证据）",
        "",
        "- 北向/机构资金、指数期货基差和期权波动率是否支持当前风险偏好？现有输入未包含这些字段。",
        "- 行业强势是否有连续多个时点确认？当前报告只读取最新快照和当天分时。",
        "- 14:05美股映射需要补充纳指期货、美元、美国利率、离岸人民币和ADR盘前行情后才能提高置信度。",
        "",
        "## Caveats and Assumptions（风险与口径）",
        "",
        f"- 报告框架：A股投资研究与交易观察系统；配置指纹 `{prompt_digest}`。",
        f"- 分时覆盖：{quality['actual']}/{quality['expected']}个配置标的；整体过期标志：{'是' if quality['stale'] else '否'}。",
        f"- 涨跌停口径：{breadth.get('limit_count_method') or '以输入文件说明为准'}。",
    ]
    for message in quality["errors"]:
        lines.append(f"- 数据错误/回退：{message}")
    for message in quality["warnings"]:
        lines.append(f"- 数据警告：{message}")
    if not quality["errors"] and not quality["warnings"]:
        lines.append("- 输入未报告数据源错误或警告。")
    lines.extend(
        [
            "- 本报告只使用指定JSON输入，不包含新闻或未提供的外部行情。",
            "- 本报告用于研究和市场观察，不构成投资建议或收益承诺。",
        ]
    )
    return "\n".join(lines) + "\n"


def generate_report(
    latest_path: Path,
    intraday_path: Path,
    prompt_path: Path,
    reports_dir: Path,
    slot: ReportSlot,
) -> Path:
    latest, intraday = load_inputs(latest_path, intraday_path)
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"报告提示词不存在: {prompt_path}") from exc
    if "A股投资研究与交易观察系统" not in prompt_text:
        raise ValueError("报告提示词缺少框架名称“A股投资研究与交易观察系统”")
    generated = _parse_datetime(str(latest.get("generated_at")))
    if not generated:
        raise ValueError("latest.json的generated_at不可解析")
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"{generated.date().isoformat()}_{slot.code}.md"
    temporary = target.with_suffix(".md.tmp")
    temporary.write_text(render_report(latest, intraday, slot, prompt_text), encoding="utf-8")
    temporary.replace(target)
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从A股行情JSON生成交易观察报告")
    parser.add_argument("--slot", choices=["auto", *SLOTS], default="auto")
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    parser.add_argument("--intraday", type=Path, default=DEFAULT_INTRADAY)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument("--scheduled", action="store_true", help="检查交易日和输入生成日期")
    parser.add_argument("--publish", action="store_true", help="提交行情输出和本次报告并推送main")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(SHANGHAI)
    slot = resolve_slot(args.slot, now)
    if args.scheduled:
        trading_day, warning = is_a_share_trading_day(now.date())
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)
        if not trading_day:
            print(f"{now.date()}不是A股交易日，跳过报告。")
            return 0
        latest = _load_json(args.latest)
        generated = _parse_datetime(str(latest.get("generated_at") or ""))
        if not generated or generated.date() != now.date():
            print("行情输入不是当天生成，拒绝发布过期报告。", file=sys.stderr)
            return 2

    try:
        report_path = generate_report(args.latest, args.intraday, args.prompt, args.reports_dir, slot)
    except ValueError as exc:
        print(f"报告生成失败: {exc}", file=sys.stderr)
        return 2
    print(report_path)

    if args.publish:
        data_paths = [
            ROOT / "data" / "latest.json",
            ROOT / "data" / "latest.md",
            ROOT / "data" / "latest_intraday.json",
            ROOT / "data" / "latest_intraday.md",
            report_path,
        ]
        result = publish_outputs(
            ROOT,
            data_paths,
            now,
            commit_message=f"report: update A-share observation {now:%Y-%m-%d} {slot.code}",
        )
        print(f"发布结果: {result}")
        if result["errors"]:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
