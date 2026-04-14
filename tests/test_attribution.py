from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from gamma_scalping.attribution import AttributionConfig, GreeksPnLAttribution


def _base_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    equity_curve = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 9)],
            "equity": [1000.0, 1068.0],
        }
    )
    trade_records = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 9)],
            "instrument_id": ["CALL1"],
            "instrument_type": ["option"],
            "side": ["buy"],
            "quantity": [1.0],
            "price": [1.0],
            "trade_amount": [100.0],
            "fee": [2.0],
            "reason": ["test"],
        }
    )
    position_records = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 8)],
            "instrument_id": ["CALL1", "510050.XSHG"],
            "instrument_type": ["option", "etf"],
            "quantity": [2.0, -50.0],
            "avg_price": [1.0, 100.0],
            "multiplier": [100.0, 1.0],
            "liquidation_price": [1.0, 100.0],
            "theoretical_unrealized_pnl": [0.0, 0.0],
            "role": ["call_leg", "delta_hedge"],
        }
    )
    greeks_history = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 9)],
            "contract_id": ["CALL1", "CALL1"],
            "delta": [0.5, 0.6],
            "gamma": [0.01, 0.02],
            "theta": [-0.2, -0.3],
            "vega": [3.0, 4.0],
            "greeks_status": ["ok", "ok"],
        }
    )
    iv_history = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 9)],
            "contract_id": ["CALL1", "CALL1"],
            "iv": [0.20, 0.21],
            "iv_status": ["ok", "ok"],
        }
    )
    underlying_history = pd.DataFrame(
        {
            "trading_date": [date(2024, 4, 8), date(2024, 4, 9)],
            "underlying": ["510050.XSHG", "510050.XSHG"],
            "close": [100.0, 102.0],
        }
    )
    return equity_curve, trade_records, position_records, greeks_history, iv_history, underlying_history


def test_daily_attribution_uses_previous_exposure_absolute_dspot_and_contract_iv() -> None:
    result = GreeksPnLAttribution().attribute_daily(*_base_inputs())

    row = result.daily[result.daily["trading_date"].eq(date(2024, 4, 9))].iloc[0]

    assert row["option_delta_exposure"] == pytest.approx(0.5 * 2 * 100)
    assert row["option_gamma_exposure"] == pytest.approx(0.01 * 2 * 100)
    assert row["option_theta_exposure"] == pytest.approx(-0.2 * 2 * 100)
    assert row["option_vega_exposure"] == pytest.approx(3.0 * 2 * 100)
    assert row["delta_pnl"] == pytest.approx(100.0 * 2.0)
    assert row["gamma_pnl"] == pytest.approx(0.5 * 2.0 * 2.0**2)
    assert row["theta_pnl"] == pytest.approx(-40.0)
    assert row["vega_pnl"] == pytest.approx(600.0 * 0.01)
    assert row["hedge_pnl"] == pytest.approx(-50.0 * 2.0)
    assert row["cost_pnl"] == pytest.approx(-2.0)
    assert row["explained_pnl"] == pytest.approx(68.0)
    assert row["actual_pnl"] == pytest.approx(68.0)
    assert row["residual_pnl"] == pytest.approx(0.0)
    assert row["weighted_position_iv"] == pytest.approx(0.20)
    assert row["weighted_position_iv_change"] == pytest.approx(0.01)


def test_cumulative_attribution_is_daily_component_cumsum_for_visualization() -> None:
    result = GreeksPnLAttribution().attribute_daily(*_base_inputs())

    cumulative = result.cumulative.iloc[-1]

    assert cumulative["cum_delta_pnl"] == pytest.approx(result.daily["delta_pnl"].sum())
    assert cumulative["cum_gamma_pnl"] == pytest.approx(result.daily["gamma_pnl"].sum())
    assert cumulative["cum_theta_pnl"] == pytest.approx(result.daily["theta_pnl"].sum())
    assert cumulative["cum_vega_pnl"] == pytest.approx(result.daily["vega_pnl"].sum())
    assert cumulative["cum_gamma_theta_pnl"] == pytest.approx(result.daily["gamma_theta_pnl"].sum())


def test_missing_iv_is_zeroed_and_reported_in_quality_table() -> None:
    inputs = list(_base_inputs())
    inputs[4] = inputs[4][inputs[4]["trading_date"].eq(date(2024, 4, 8))]

    result = GreeksPnLAttribution().attribute_daily(*inputs)
    row = result.daily[result.daily["trading_date"].eq(date(2024, 4, 9))].iloc[0]
    quality = result.quality[result.quality["trading_date"].eq(date(2024, 4, 9))].iloc[0]

    assert row["vega_pnl"] == pytest.approx(0.0)
    assert "missing_iv" in row["quality_flags"]
    assert quality["missing_iv_count"] == 1


def test_missing_greeks_are_zeroed_and_reported_in_quality_table() -> None:
    inputs = list(_base_inputs())
    inputs[3].loc[0, "gamma"] = pd.NA

    result = GreeksPnLAttribution().attribute_daily(*inputs)
    row = result.daily[result.daily["trading_date"].eq(date(2024, 4, 9))].iloc[0]
    quality = result.quality[result.quality["trading_date"].eq(date(2024, 4, 9))].iloc[0]

    assert row["option_delta_exposure"] == pytest.approx(100.0)
    assert row["option_gamma_exposure"] == pytest.approx(0.0)
    assert row["gamma_pnl"] == pytest.approx(0.0)
    assert "missing_greeks" in row["quality_flags"]
    assert quality["missing_greeks_count"] == 1


def test_missing_underlying_sets_dspot_to_zero_and_reports_flag() -> None:
    inputs = list(_base_inputs())
    inputs[5] = inputs[5][inputs[5]["trading_date"].eq(date(2024, 4, 8))]

    result = GreeksPnLAttribution().attribute_daily(*inputs)
    row = result.daily[result.daily["trading_date"].eq(date(2024, 4, 9))].iloc[0]

    assert row["delta_pnl"] == pytest.approx(0.0)
    assert row["gamma_pnl"] == pytest.approx(0.0)
    assert row["hedge_pnl"] == pytest.approx(0.0)
    assert "missing_underlying" in row["quality_flags"]


def test_empty_equity_curve_returns_empty_output_frames() -> None:
    inputs = list(_base_inputs())
    inputs[0] = inputs[0].iloc[0:0]

    result = GreeksPnLAttribution().attribute_daily(*inputs)

    assert result.daily.empty
    assert result.cumulative.empty
    assert result.quality.empty
    assert list(result.daily.columns) == [
        "trading_date",
        "underlying",
        "actual_pnl",
        "delta_pnl",
        "gamma_pnl",
        "theta_pnl",
        "vega_pnl",
        "hedge_pnl",
        "cost_pnl",
        "explained_pnl",
        "residual_pnl",
        "residual_ratio",
        "gamma_theta_pnl",
        "option_delta_exposure",
        "option_gamma_exposure",
        "option_theta_exposure",
        "option_vega_exposure",
        "weighted_position_iv",
        "weighted_position_iv_change",
        "exposure_mode",
        "quality_flags",
    ]


def test_multi_contract_positions_use_abs_vega_weighted_iv() -> None:
    inputs = list(_base_inputs())
    inputs[2] = pd.concat(
        [
            inputs[2],
            pd.DataFrame(
                {
                    "trading_date": [date(2024, 4, 8)],
                    "instrument_id": ["PUT1"],
                    "instrument_type": ["option"],
                    "quantity": [-1.0],
                    "avg_price": [1.0],
                    "multiplier": [100.0],
                    "liquidation_price": [1.0],
                    "theoretical_unrealized_pnl": [0.0],
                    "role": ["put_leg"],
                }
            ),
        ],
        ignore_index=True,
    )
    inputs[3] = pd.concat(
        [
            inputs[3],
            pd.DataFrame(
                {
                    "trading_date": [date(2024, 4, 8), date(2024, 4, 9)],
                    "contract_id": ["PUT1", "PUT1"],
                    "delta": [-0.4, -0.5],
                    "gamma": [0.02, 0.03],
                    "theta": [-0.1, -0.2],
                    "vega": [2.0, 2.5],
                    "greeks_status": ["ok", "ok"],
                }
            ),
        ],
        ignore_index=True,
    )
    inputs[4] = pd.concat(
        [
            inputs[4],
            pd.DataFrame(
                {
                    "trading_date": [date(2024, 4, 8), date(2024, 4, 9)],
                    "contract_id": ["PUT1", "PUT1"],
                    "iv": [0.30, 0.27],
                    "iv_status": ["ok", "ok"],
                }
            ),
        ],
        ignore_index=True,
    )

    result = GreeksPnLAttribution().attribute_daily(*inputs)
    row = result.daily[result.daily["trading_date"].eq(date(2024, 4, 9))].iloc[0]

    assert row["vega_pnl"] == pytest.approx(600.0 * 0.01 + (-200.0) * -0.03)
    assert row["weighted_position_iv"] == pytest.approx((600.0 * 0.20 + 200.0 * 0.30) / 800.0)
    assert row["weighted_position_iv_change"] == pytest.approx((600.0 * 0.01 + 200.0 * -0.03) / 800.0)


def test_first_row_has_zero_pnl_components_and_first_row_flag() -> None:
    result = GreeksPnLAttribution().attribute_daily(*_base_inputs())

    row = result.daily.iloc[0]

    for column in [
        "actual_pnl",
        "delta_pnl",
        "gamma_pnl",
        "theta_pnl",
        "vega_pnl",
        "hedge_pnl",
        "cost_pnl",
        "explained_pnl",
        "residual_pnl",
        "gamma_theta_pnl",
    ]:
        assert row[column] == pytest.approx(0.0)
    assert row["quality_flags"] == "first_row"


def test_attribution_result_exports_csv(tmp_path) -> None:
    result = GreeksPnLAttribution().attribute_daily(*_base_inputs())

    paths = result.export_csv(tmp_path)

    assert paths["greeks_attribution"].exists()
    assert paths["greeks_attribution_cumulative"].exists()
    assert paths["attribution_quality"].exists()


def test_unsupported_exposure_mode_fails_fast() -> None:
    with pytest.raises(ValueError, match="previous_close"):
        GreeksPnLAttribution(AttributionConfig(exposure_mode="average"))
