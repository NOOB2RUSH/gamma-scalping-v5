from __future__ import annotations

from datetime import date
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from gamma_scalping.attribution import AttributionResult
from gamma_scalping.performance import IvHvCaptureAnalyzer, PerformanceAnalyzer, PerformanceConfig, Visualizer
from gamma_scalping.volatility import VolatilityTimeSeries


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        equity_curve=pd.DataFrame(
            {
                "trading_date": [date(2024, 4, 8), date(2024, 4, 9), date(2024, 4, 10), date(2024, 4, 11)],
                "equity": [100.0, 110.0, 105.0, 126.0],
            }
        ),
        trade_records=pd.DataFrame(
            {
                "trading_date": [date(2024, 4, 9), date(2024, 4, 10), date(2024, 4, 11)],
                "instrument_id": ["CALL1", "", "510050.XSHG"],
                "instrument_type": ["option", "", "etf"],
                "trade_amount": [1000.0, 0.0, 500.0],
                "fee": [2.0, 0.0, 1.0],
            }
        ),
        episode_records=pd.DataFrame(
            {
                "episode_id": ["episode_1"],
                "opened_at": [date(2024, 4, 8)],
                "closed_at": [date(2024, 4, 11)],
                "entry_atm_iv": [0.20],
                "entry_hv_20": [0.18],
                "entry_spot": [100.0],
            }
        ),
    )


def _attribution() -> AttributionResult:
    daily = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 9), date(2024, 4, 10)],
            "delta_pnl": [0.0, 10.0, -2.0],
            "gamma_pnl": [0.0, 4.0, 5.0],
            "theta_pnl": [0.0, -1.0, -1.0],
            "vega_pnl": [0.0, 3.0, -2.0],
            "hedge_pnl": [0.0, -1.0, 1.0],
            "cost_pnl": [0.0, -0.5, -0.5],
            "residual_pnl": [0.0, 0.5, -0.5],
            "gamma_theta_pnl": [0.0, 3.0, 4.0],
            "option_delta_exposure": [0.0, 10.0, 20.0],
            "option_gamma_exposure": [0.0, 2.0, 3.0],
            "option_theta_exposure": [0.0, -1.0, -1.0],
            "option_vega_exposure": [0.0, 4.0, 5.0],
            "residual_ratio": [0.0, 0.02, 0.03],
        }
    )
    cumulative = pd.DataFrame(
        {
            "trading_date": daily["trading_date"],
            "cum_actual_pnl": [0.0, 16.0, 16.0],
            "cum_delta_pnl": [0.0, 10.0, 8.0],
            "cum_gamma_pnl": [0.0, 4.0, 9.0],
            "cum_theta_pnl": [0.0, -1.0, -2.0],
            "cum_vega_pnl": [0.0, 3.0, 1.0],
            "cum_hedge_pnl": [0.0, -1.0, 0.0],
            "cum_cost_pnl": [0.0, -0.5, -1.0],
            "cum_explained_pnl": [0.0, 14.5, 15.0],
            "cum_residual_pnl": [0.0, 0.5, 0.0],
            "cum_gamma_theta_pnl": [0.0, 3.0, 7.0],
        }
    )
    quality = pd.DataFrame({"trading_date": daily["trading_date"], "residual_ratio": [0.0, 0.02, 0.03]})
    by_episode = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 9), date(2024, 4, 10), date(2024, 4, 11)],
            "episode_id": ["episode_1", "episode_1", "episode_1"],
            "delta_pnl": [0.0, 0.0, 0.0],
            "gamma_pnl": [2.0, 3.0, 4.0],
            "theta_pnl": [-1.0, -1.0, -1.0],
            "vega_pnl": [0.5, -0.2, 0.1],
            "hedge_pnl": [-0.1, 0.2, -0.1],
            "cost_pnl": [-0.05, -0.05, -0.05],
            "explained_pnl": [1.35, 2.15, 2.85],
            "gamma_theta_pnl": [1.0, 2.0, 3.0],
            "option_gamma_exposure": [100.0, 100.0, 100.0],
        }
    )
    return AttributionResult(daily=daily, cumulative=cumulative, quality=quality, by_episode=by_episode)


def _underlying_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 9), date(2024, 4, 10), date(2024, 4, 11)],
            "close": [100.0, 101.0, 99.0, 102.0],
        }
    )


def _volatility() -> VolatilityTimeSeries:
    return VolatilityTimeSeries(
        underlying="510050.XSHG",
        frame=pd.DataFrame(
            {
                "trading_date": [date(2024, 4, 8), date(2024, 4, 9), date(2024, 4, 10)],
                "atm_iv": [0.20, 0.21, 0.19],
                "hv_10": [0.18, 0.19, 0.20],
                "hv_20": [0.17, 0.18, 0.19],
                "hv_60": [0.16, 0.17, 0.18],
                "iv_hv_spread": [0.03, 0.03, 0.00],
                "hv_iv_edge": [-0.03, -0.03, 0.00],
                "iv_failed_count": [0, 1, 0],
            }
        ),
    )


def test_performance_analyzer_computes_core_and_trade_metrics() -> None:
    metrics = PerformanceAnalyzer().compute_metrics(_result())

    assert metrics.summary["initial_equity"] == pytest.approx(100.0)
    assert metrics.summary["final_equity"] == pytest.approx(126.0)
    assert metrics.summary["cumulative_return"] == pytest.approx(0.26)
    assert metrics.summary["max_drawdown"] == pytest.approx(105.0 / 110.0 - 1.0)
    assert metrics.summary["total_trade_amount"] == pytest.approx(1500.0)
    assert metrics.summary["total_fee"] == pytest.approx(3.0)
    assert metrics.summary["trade_count"] == pytest.approx(2.0)
    assert len(metrics.daily_returns) == 4
    assert not metrics.monthly_returns.empty


def test_sortino_uses_downside_deviation_without_mean_adjustment() -> None:
    metrics = PerformanceAnalyzer().compute_metrics(_result())
    returns = metrics.daily_returns["return"]
    downside = returns.where(returns < 0, 0.0)
    downside_dev = math.sqrt(float((downside**2).mean()))
    expected_sortino = returns.mean() / downside_dev * math.sqrt(252)
    incorrect_std_sortino = returns.mean() / downside.std(ddof=0) * np.sqrt(252)

    assert metrics.summary["sortino_ratio"] == pytest.approx(expected_sortino)
    assert metrics.summary["sortino_ratio"] != pytest.approx(incorrect_std_sortino)


def test_performance_analyzer_consumes_attribution_and_volatility_outputs() -> None:
    metrics = PerformanceAnalyzer().compute_metrics(
        _result(),
        attribution=_attribution(),
        volatility=_volatility(),
        underlying_history=_underlying_history(),
    )

    assert metrics.summary["avg_delta_exposure"] == pytest.approx(10.0)
    assert metrics.summary["total_gamma_theta_pnl"] == pytest.approx(7.0)
    assert metrics.summary["total_vega_pnl"] == pytest.approx(1.0)
    assert metrics.summary["avg_atm_iv"] == pytest.approx(0.20)
    assert metrics.summary["total_iv_failed_count"] == pytest.approx(1.0)
    assert metrics.summary["iv_hv_capture_rate_valid_count"] == pytest.approx(1.0)


def test_performance_analyzer_passes_iv_hv_capture_config() -> None:
    metrics = PerformanceAnalyzer(
        PerformanceConfig(iv_hv_capture_min_return_observations=4)
    ).compute_metrics(
        _result(),
        attribution=_attribution(),
        volatility=_volatility(),
        underlying_history=_underlying_history(),
    )

    assert metrics.summary["iv_hv_capture_rate_valid_count"] == pytest.approx(0.0)


def test_iv_hv_capture_uses_episode_attribution_and_entry_atm_iv() -> None:
    capture = IvHvCaptureAnalyzer().compute(
        episode_records=_result().episode_records,
        attribution=_attribution(),
        underlying_history=_underlying_history(),
    )

    row = capture.episodes.iloc[0]
    returns = [
        math.log(101.0 / 100.0),
        math.log(99.0 / 101.0),
        math.log(102.0 / 99.0),
    ]
    expected_theoretical = sum(
        0.5 * 100.0 * spot * spot * (ret * ret - 0.20**2 / 252)
        for spot, ret in zip([100.0, 101.0, 99.0], returns)
    )

    assert row["valid"]
    assert row["net_gamma_scalping_pnl"] == pytest.approx(6.0 - 0.0 - 0.15)
    assert row["theoretical_vol_edge_pnl"] == pytest.approx(expected_theoretical)
    assert row["iv_hv_capture_rate"] == pytest.approx(row["net_gamma_scalping_pnl"] / expected_theoretical)
    assert row["realized_vol_holding"] == pytest.approx(pd.Series(returns).std(ddof=0) * math.sqrt(252))
    assert capture.summary["iv_hv_capture_rate_weighted"] == pytest.approx(row["iv_hv_capture_rate"])


def test_iv_hv_capture_marks_missing_attribution_invalid() -> None:
    attribution = AttributionResult(
        daily=_attribution().daily,
        cumulative=_attribution().cumulative,
        quality=_attribution().quality,
    )

    capture = IvHvCaptureAnalyzer().compute(
        episode_records=_result().episode_records,
        attribution=attribution,
        underlying_history=_underlying_history(),
    )

    assert not capture.episodes.iloc[0]["valid"]
    assert "missing_episode_attribution" in capture.episodes.iloc[0]["invalid_reason"]


def test_visualizer_saves_equity_and_attribution_figures(tmp_path) -> None:
    visualizer = Visualizer()

    equity_path = visualizer.save(visualizer.plot_equity_curve(_result()), tmp_path / "equity.png")
    cumulative_path = visualizer.save(
        visualizer.plot_greeks_attribution_cumulative(_attribution()),
        tmp_path / "cumulative.png",
    )

    assert equity_path.exists()
    assert cumulative_path.exists()
    assert equity_path.stat().st_size > 0
    assert cumulative_path.stat().st_size > 0


def test_performance_report_exports_tables_html_and_figures(tmp_path) -> None:
    analyzer = PerformanceAnalyzer()
    report = analyzer.build_report(
        _result(),
        tmp_path,
        attribution=_attribution(),
        volatility=_volatility(),
        underlying_history=_underlying_history(),
    )

    assert report.paths["metrics"].exists()
    assert report.paths["daily_returns"].exists()
    assert report.paths["monthly_returns"].exists()
    assert report.paths["report"].exists()
    assert report.paths["equity_curve"].exists()
    assert report.paths["drawdown"].exists()
    assert report.paths["volatility"].exists()
    assert report.paths["greeks_attribution_daily"].exists()
    assert report.paths["greeks_attribution_cumulative"].exists()
    assert report.paths["iv_hv_capture_episodes"].exists()
    assert report.metrics.iv_hv_capture is not None


def test_performance_report_reuses_iv_hv_capture_result(tmp_path, monkeypatch) -> None:
    analyzer = PerformanceAnalyzer()
    calls = 0
    original = analyzer._compute_iv_hv_capture

    def wrapped_compute_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(analyzer, "_compute_iv_hv_capture", wrapped_compute_capture)

    analyzer.build_report(
        _result(),
        tmp_path,
        attribution=_attribution(),
        volatility=_volatility(),
        underlying_history=_underlying_history(),
    )

    assert calls == 1


def test_performance_analyzer_handles_empty_equity_curve() -> None:
    result = SimpleNamespace(equity_curve=pd.DataFrame(columns=["trading_date", "equity"]), trade_records=pd.DataFrame())

    metrics = PerformanceAnalyzer().compute_metrics(result)

    assert metrics.summary["observation_count"] == pytest.approx(0.0)
    assert metrics.daily_returns.empty
