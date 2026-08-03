from typing import Any

from a_share_bridge.sources import FallbackChain, Instrument


ITEM = Instrument("上证指数", "000001", "index", "1.000001", "sh000001")


class FailingProvider:
    name = "primary"
    available = True

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        raise ConnectionError("simulated outage")

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any]:
        raise ConnectionError("simulated outage")


class WorkingProvider:
    name = "secondary"
    available = True

    def fetch_quotes(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        return {
            item.key: {
                "name": item.name,
                "symbol": item.symbol,
                "kind": item.kind,
                "last": 3800.0,
                "source": self.name,
            }
            for item in instruments
        }

    def fetch_intraday(self, instrument: Instrument) -> dict[str, Any]:
        return {"source": self.name, "points": [{"time": "09:30", "price": 3800.0, "volume": 1}]}


def test_quote_fallback_survives_primary_failure() -> None:
    primary = FailingProvider()
    chain = FallbackChain([primary, WorkingProvider()])
    result = chain.fetch_quotes([ITEM])
    assert result[ITEM.key]["source"] == "secondary"
    assert result[ITEM.key]["last"] == 3800.0
    assert not primary.available
    assert any("primary实时行情失败" in error for error in chain.errors)
    assert any(report["source"] == "primary" and not report["ok"] for report in chain.reports)


def test_intraday_fallback_skips_unavailable_provider() -> None:
    primary = FailingProvider()
    primary.available = False
    chain = FallbackChain([primary, WorkingProvider()])
    result = chain.fetch_intraday(ITEM)
    assert result is not None
    assert result["source"] == "secondary"
    assert len(result["points"]) == 1
