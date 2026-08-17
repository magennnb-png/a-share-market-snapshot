import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import update_market
from update_market import UpdateError, _materially_changed, _tree_digest


def test_digest_ignores_generated_at_only(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "latest.json").write_text(json.dumps({"generated_at": "a", "market_time": "same", "value": 1}), encoding="utf-8")
    (right / "latest.json").write_text(json.dumps({"generated_at": "b", "market_time": "same", "value": 1}), encoding="utf-8")
    assert _tree_digest(left) == _tree_digest(right)


def test_digest_detects_real_market_change(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "latest.json").write_text(json.dumps({"generated_at": "a", "value": 1}), encoding="utf-8")
    (right / "latest.json").write_text(json.dumps({"generated_at": "b", "value": 2}), encoding="utf-8")
    assert _tree_digest(left) != _tree_digest(right)


def test_digest_ignores_archive_generated_at(tmp_path: Path) -> None:
    left = tmp_path / "left" / "history" / "intraday"
    right = tmp_path / "right" / "history" / "intraday"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "2026-08-12.json").write_text(json.dumps({"generated_at": "a", "value": 1}), encoding="utf-8")
    (right / "2026-08-12.json").write_text(json.dumps({"generated_at": "b", "value": 1}), encoding="utf-8")
    assert _tree_digest(left.parents[1]) == _tree_digest(right.parents[1])


def test_digest_ignores_nested_source_poll_times(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    one = {"market_time": "12:05:41", "indices": [{"last": 100, "market_time": "12:05:00"}]}
    two = {"market_time": "12:05:53", "indices": [{"last": 100, "market_time": "12:05:03"}]}
    (left / "latest.json").write_text(json.dumps(one), encoding="utf-8")
    (right / "latest.json").write_text(json.dumps(two), encoding="utf-8")
    assert _tree_digest(left) == _tree_digest(right)


def test_markdown_only_change_is_not_material(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "latest.md").write_text("generated a", encoding="utf-8")
    (right / "latest.md").write_text("generated b", encoding="utf-8")
    assert not _materially_changed(left, right)


def test_research_context_change_is_material(tmp_path: Path) -> None:
    left = tmp_path / "left" / "research_context"
    right = tmp_path / "right" / "research_context"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "market_technical.json").write_text(json.dumps({"data_as_of": "2026-08-17", "value": 1}), encoding="utf-8")
    (right / "market_technical.json").write_text(json.dumps({"data_as_of": "2026-08-17", "value": 2}), encoding="utf-8")
    assert _materially_changed(left.parent, right.parent)


def test_invalid_snapshot_never_replaces_existing_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    marker = data / "keep.txt"
    marker.write_text("original", encoding="utf-8")
    config = tmp_path / "watchlist.yaml"
    config.write_text("settings: {}\n", encoding="utf-8")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(update_market, "collect", lambda *_: (
        {"market_time": now.isoformat(), "indices": [], "market_breadth": {}, "industries": {"all": []}},
        {"instruments": []},
        [],
    ))
    with pytest.raises(UpdateError):
        update_market.run(config, data)
    assert marker.read_text(encoding="utf-8") == "original"
    assert list(data.iterdir()) == [marker]
