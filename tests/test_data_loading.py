from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from gamma_scalping.data import MarketDataConfig, MarketDataLoader, TradingCalendar


def _write_fixture_files(root, trading_date: str, *, invalid_bid_ask: bool = False) -> None:
    underlying = "510050.XSHG"
    etf_dir = root / "etf"
    opt_dir = root / "opt"
    etf_dir.mkdir(parents=True, exist_ok=True)
    opt_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        {
            "open": [2.7],
            "close": [2.8],
            "high": [2.85],
            "low": [2.65],
            "volume": [1000.0],
            "money": [2800.0],
        },
        index=pd.Index([pd.Timestamp(trading_date)], name="date"),
    ).to_parquet(etf_dir / f"{underlying}_{trading_date}_price.parquet")

    bid = 0.12
    ask = 0.10 if invalid_bid_ask else 0.14
    pd.DataFrame(
        {
            "order_book_id": ["10000001.XSHG"],
            "strike_price": [2.8],
            "maturity_date": ["2024-04-10"],
            "option_type": ["C"],
            "bid": [bid],
            "ask": [ask],
            "volume": [10],
            "open_interest": [20],
            "contract_multiplier": [10000],
            "close": [0.11],
        }
    ).to_parquet(opt_dir / f"{underlying}_{trading_date}_chain.parquet")


def test_calendar_skips_weekends_and_mainland_china_holidays() -> None:
    calendar = TradingCalendar()

    assert not calendar.is_session("2024-04-04")
    assert not calendar.is_session("2024-04-06")
    assert calendar.is_session("2024-04-08")
    assert calendar.trading_days_between("2024-02-08", "2024-02-19") == 1


def test_loader_standardizes_snapshot_and_ttm(tmp_path) -> None:
    _write_fixture_files(tmp_path, "2024-04-08")
    loader = MarketDataLoader(MarketDataConfig(data_root=tmp_path, start_date="2024-04-08", end_date="2024-04-08"))

    dates = loader.list_trading_dates()
    snapshot = loader.load_snapshot(dates[0])
    chain = snapshot.option_chain.frame

    assert dates == [date(2024, 4, 8)]
    assert snapshot.etf_bar.turnover == 2800.0
    assert chain.loc[0, "contract_id"] == "10000001.XSHG"
    assert chain.loc[0, "mid"] == pytest.approx(0.13)
    assert chain.loc[0, "buy_price"] == pytest.approx(0.14)
    assert chain.loc[0, "sell_price"] == pytest.approx(0.12)
    assert chain.loc[0, "mark_price"] == pytest.approx(0.13)
    assert chain.loc[0, "ttm_trading_days"] == 2


def test_loader_falls_back_to_close_for_invalid_bid_ask(tmp_path) -> None:
    _write_fixture_files(tmp_path, "2024-04-08", invalid_bid_ask=True)
    loader = MarketDataLoader(MarketDataConfig(data_root=tmp_path, start_date="2024-04-08", end_date="2024-04-08"))

    chain = loader.load_option_chain(date(2024, 4, 8))

    assert chain.frame.loc[0, "mid"] == pytest.approx(0.11)
    assert chain.frame.loc[0, "buy_price"] == pytest.approx(0.11)
    assert chain.frame.loc[0, "sell_price"] == pytest.approx(0.11)
    assert chain.frame.loc[0, "price_quality"] == "close_fallback"
    assert chain.quality_issues == ("invalid_bid_ask_rows=1",)


def test_loader_supports_bid_ask_conservative_policy(tmp_path) -> None:
    _write_fixture_files(tmp_path, "2024-04-08")
    loader = MarketDataLoader(
        MarketDataConfig(
            data_root=tmp_path,
            start_date="2024-04-08",
            end_date="2024-04-08",
            price_policy="bid_ask_conservative",
        )
    )

    chain = loader.load_option_chain(date(2024, 4, 8))

    assert chain.frame.loc[0, "mid"] == pytest.approx(0.13)
    assert chain.frame.loc[0, "mark_price"] == pytest.approx(0.14)
    assert chain.frame.loc[0, "buy_price"] == pytest.approx(0.14)
    assert chain.frame.loc[0, "sell_price"] == pytest.approx(0.12)
    assert chain.frame.loc[0, "price_quality"] == "bid_ask_conservative"


def test_loader_missing_data_policy_error(tmp_path) -> None:
    _write_fixture_files(tmp_path, "2024-04-08")
    loader = MarketDataLoader(
        MarketDataConfig(
            data_root=tmp_path,
            start_date="2024-04-08",
            end_date="2024-04-09",
            missing_data_policy="error",
        )
    )

    with pytest.raises(FileNotFoundError):
        loader.list_trading_dates()
