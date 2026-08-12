from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from a_share_bridge.history import update_history, validate_history
from a_share_bridge.main import collect
from a_share_bridge.rendering import write_outputs
from a_share_bridge.validation import validate_snapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")


class UpdateError(RuntimeError):
    pass


def _strip_volatile_times(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_volatile_times(item)
            for key, item in value.items()
            if key not in {"generated_at", "market_time", "source_status", "errors", "warnings"}
        }
    if isinstance(value, list):
        return [_strip_volatile_times(item) for item in value]
    return value


def _canonical_json(path: Path) -> Any:
    payload = _strip_volatile_times(json.loads(path.read_bytes()))
    if path.name == "latest_intraday.json" or path.parent.name == "intraday":
        # Point timestamps can differ by a few seconds between equivalent
        # source polls. Compare the actual price/volume path instead.
        for instrument in (payload.get("instruments") or []) if isinstance(payload, dict) else []:
            for point in instrument.get("points") or []:
                point.pop("time", None)
    return payload


def _normalized_content(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".json":
        try:
            payload = _canonical_json(path)
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if path.suffix == ".md" and path.name.startswith("latest"):
        text = data.decode("utf-8")
        # Markdown repeats poll timestamps already present in JSON. Normalize
        # all ISO datetimes so seconds-only source polling drift is not treated
        # as a material market-data change.
        text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)", "<DATETIME>", text)
        return text.encode("utf-8")
    return data


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(_normalized_content(path))
    return digest.hexdigest()


def _materially_changed(staged: Path, current: Path) -> bool:
    # Human-readable Markdown mirrors the JSON payload and should never create
    # a commit by itself. History README is documentation, not market data.
    tracked = {
        "latest.json", "latest_intraday.json", "latest_rotation.json",
        "history/indices_daily.csv", "history/watchlist_daily.csv",
        "history/market_breadth_daily.csv", "history/industries_daily.csv",
        "history/rotation_daily.csv",
    }
    relative = {
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*") if path.is_file()
    } | {
        path.relative_to(current).as_posix()
        for path in current.rglob("*") if path.is_file()
    } if current.exists() else {
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*") if path.is_file()
    }
    tracked.update(name for name in relative if name.startswith("history/intraday/") and name.endswith(".json"))
    for name in tracked:
        left, right = staged / name, current / name
        if left.exists() != right.exists():
            return True
        if left.exists() and _normalized_content(left) != _normalized_content(right):
            return True
    return False


def _install(staged: Path, target: Path) -> None:
    for source in sorted(item for item in staged.rglob("*") if item.is_file()):
        relative = source.relative_to(staged)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".new")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    staged_files = {path.relative_to(staged) for path in staged.rglob("*") if path.is_file()}
    for old in sorted((path for path in target.rglob("*") if path.is_file()), reverse=True):
        if old.relative_to(target) not in staged_files:
            old.unlink()


def run(config_path: Path, data_dir: Path, validate_only: bool = False) -> dict[str, Any]:
    now = datetime.now(SHANGHAI)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    settings = config.get("settings") or {}
    with tempfile.TemporaryDirectory(prefix="a-share-update-") as temporary:
        staged = Path(temporary) / "data"
        if data_dir.exists():
            shutil.copytree(data_dir, staged)
        else:
            staged.mkdir(parents=True)
        print("[1/4] 获取实时行情（东方财富 → 腾讯，宽度/行业 → 新浪回退）...", flush=True)
        snapshot, intraday, _ = collect(config_path, None)
        errors, warnings = validate_snapshot(snapshot, intraday, now)
        if errors:
            raise UpdateError("实时行情校验失败：\n- " + "\n- ".join(errors))
        snapshot["warnings"] = list(dict.fromkeys([*(snapshot.get("warnings") or []), *warnings]))
        intraday["warnings"] = list(dict.fromkeys([*(intraday.get("warnings") or []), *warnings]))
        write_outputs(snapshot, intraday, staged)

        print("[2/4] 增量更新指数、宽度、行业和轮动历史...", flush=True)
        history = update_history(config_path, staged, snapshot, intraday)
        required_index = min(250, int(settings.get("history_index_days", 320)))
        # Breadth history is observed-only; unlike daily bars it cannot be
        # truthfully reconstructed after a missed day.
        required_breadth = 1
        required_industry = min(120, int(settings.get("history_industry_days", 120)))
        history_errors = validate_history(history, required_index, required_breadth, required_industry)
        if history_errors:
            raise UpdateError("历史行情校验失败：\n- " + "\n- ".join(history_errors))
        print("[3/4] 数据完整性校验通过。", flush=True)

        changed = _materially_changed(staged, data_dir)
        if changed and not validate_only:
            _install(staged, data_dir)
        print("[4/4] 数据文件已原子更新。" if changed and not validate_only else "[4/4] 行情数据没有变化，无需改写。", flush=True)
        return {
            "ok": True, "changed": changed and not validate_only,
            "market_time": snapshot["market_time"], "generated_at": snapshot["generated_at"],
            "sources": snapshot.get("sources") or [], "indices": snapshot.get("indices") or [],
            "market_breadth": snapshot.get("market_breadth") or {},
            "files": ["latest.json", "latest_intraday.json", "latest_rotation.json", "latest_rotation.md"],
            "history_counts": {key: len(history[key]) for key in ("indices", "watchlist", "breadth", "industries", "rotation")},
            "warnings": history.get("warnings") or [],
        }


def _print_summary(result: dict[str, Any]) -> None:
    by_symbol = {row["symbol"]: row for row in result["indices"]}
    breadth = result["market_breadth"]
    print("\n========================================")
    print("A股行情快照更新完成")
    print("========================================\n")
    print(f"行情时间：{result['market_time']}")
    print(f"生成时间：{result['generated_at']}")
    print(f"上证指数：{by_symbol.get('000001', {}).get('last', '—')}")
    print(f"创业板指：{by_symbol.get('399006', {}).get('last', '—')}")
    print(f"上涨家数：{breadth.get('up', '—')}")
    print(f"下跌家数：{breadth.get('down', '—')}")
    print(f"两市成交额：{float(breadth.get('turnover_cny') or 0) / 100_000_000:.2f} 亿元")
    print(f"数据源：{' / '.join(result['sources'])}")
    print("数据变化：" + ("YES" if result["changed"] else "NO（无需提交）"))
    print("RESULT_JSON=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地一键更新A股实时与历史行情")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "watchlist.yaml")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.config.resolve(), args.data_dir.resolve(), args.validate_only)
        _print_summary(result)
        return 0
    except Exception as exc:
        print(f"\n更新失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
