from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


DAILY_COLUMNS = [
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

CUMULATIVE_COLUMNS = [
    "trading_date",
    "cum_actual_pnl",
    "cum_delta_pnl",
    "cum_gamma_pnl",
    "cum_theta_pnl",
    "cum_vega_pnl",
    "cum_hedge_pnl",
    "cum_cost_pnl",
    "cum_explained_pnl",
    "cum_residual_pnl",
    "cum_gamma_theta_pnl",
]

QUALITY_COLUMNS = [
    "trading_date",
    "residual_ratio",
    "residual_warning",
    "missing_greeks_count",
    "missing_iv_count",
    "failed_iv_count",
    "option_position_count",
    "notes",
]


@dataclass(frozen=True)
class AttributionConfig:
    exposure_mode: str = "previous_close"
    residual_warning_threshold: float = 0.10
    eps: float = 1e-8


@dataclass(frozen=True)
class AttributionResult:
    daily: pd.DataFrame
    cumulative: pd.DataFrame
    quality: pd.DataFrame

    def export_csv(self, output_dir: Path | str) -> dict[str, Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "greeks_attribution": output_dir / "greeks_attribution.csv",
            "greeks_attribution_cumulative": output_dir / "greeks_attribution_cumulative.csv",
            "attribution_quality": output_dir / "attribution_quality.csv",
        }
        self.daily.to_csv(paths["greeks_attribution"], index=False)
        self.cumulative.to_csv(paths["greeks_attribution_cumulative"], index=False)
        self.quality.to_csv(paths["attribution_quality"], index=False)
        return paths


class GreeksPnLAttribution:
    def __init__(self, config: AttributionConfig | None = None) -> None:
        self.config = config or AttributionConfig()
        if self.config.exposure_mode != "previous_close":
            raise ValueError("Only exposure_mode='previous_close' is supported in V1")

    def attribute_daily(
        self,
        equity_curve: pd.DataFrame,
        trade_records: pd.DataFrame,
        position_records: pd.DataFrame,
        greeks_history: pd.DataFrame,
        iv_history: pd.DataFrame,
        underlying_history: pd.DataFrame,
    ) -> AttributionResult:
        equity_curve = _prepare_table(equity_curve, {"trading_date", "equity"}, "equity_curve")
        if equity_curve.empty:
            return AttributionResult(
                daily=pd.DataFrame(columns=DAILY_COLUMNS),
                cumulative=pd.DataFrame(columns=CUMULATIVE_COLUMNS),
                quality=pd.DataFrame(columns=QUALITY_COLUMNS),
            )

        trade_records = _prepare_optional_table(trade_records, "trade_records")
        position_records = _prepare_table(
            position_records,
            {"trading_date", "instrument_id", "instrument_type", "quantity", "multiplier"},
            "position_records",
        )
        greeks_history = _prepare_table(
            greeks_history,
            {"trading_date", "contract_id", "delta", "gamma", "theta", "vega"},
            "greeks_history",
        )
        iv_history = _prepare_table(iv_history, {"trading_date", "contract_id", "iv"}, "iv_history")
        underlying_history = _prepare_table(
            underlying_history,
            {"trading_date", "close"},
            "underlying_history",
        )

        equity_curve = equity_curve.sort_values("trading_date")
        dates = equity_curve["trading_date"].tolist()
        equity_by_date = equity_curve.set_index("trading_date")["equity"]
        underlying_by_date = underlying_history.set_index("trading_date")
        positions_by_date = _active_positions(position_records)
        trades_by_date = _active_trades(trade_records)
        greeks_by_key = greeks_history.set_index(["trading_date", "contract_id"])
        iv_by_key = iv_history.set_index(["trading_date", "contract_id"])

        daily_rows = []
        quality_rows = []
        for idx, trading_date in enumerate(dates):
            if idx == 0:
                row, quality = self._first_row(trading_date, underlying_by_date)
            else:
                prev_date = dates[idx - 1]
                row, quality = self._attribute_row(
                    trading_date=trading_date,
                    prev_date=prev_date,
                    equity_by_date=equity_by_date,
                    underlying_by_date=underlying_by_date,
                    positions_by_date=positions_by_date,
                    trades_by_date=trades_by_date,
                    greeks_by_key=greeks_by_key,
                    iv_by_key=iv_by_key,
                )
            daily_rows.append(row)
            quality_rows.append(quality)

        daily = pd.DataFrame(daily_rows, columns=DAILY_COLUMNS)
        cumulative = self._build_cumulative(daily)
        quality = pd.DataFrame(quality_rows, columns=QUALITY_COLUMNS)
        return AttributionResult(daily=daily, cumulative=cumulative, quality=quality)

    def _first_row(self, trading_date: object, underlying_by_date: pd.DataFrame) -> tuple[dict[str, object], dict[str, object]]:
        underlying = _underlying_for_date(underlying_by_date, trading_date)
        row = _empty_daily_row(trading_date, underlying, self.config.exposure_mode)
        row["quality_flags"] = "first_row"
        quality = _quality_row(trading_date, 0.0, False, 0, 0, 0, 0, "first_row")
        return row, quality

    def _attribute_row(
        self,
        *,
        trading_date: object,
        prev_date: object,
        equity_by_date: pd.Series,
        underlying_by_date: pd.DataFrame,
        positions_by_date: dict[object, pd.DataFrame],
        trades_by_date: dict[object, pd.DataFrame],
        greeks_by_key: pd.DataFrame,
        iv_by_key: pd.DataFrame,
    ) -> tuple[dict[str, object], dict[str, object]]:
        flags: list[str] = []
        spot_t, underlying_t = _spot_for_date(underlying_by_date, trading_date)
        spot_prev, _ = _spot_for_date(underlying_by_date, prev_date)
        if pd.isna(spot_t) or pd.isna(spot_prev):
            d_spot = 0.0
            flags.append("missing_underlying")
        else:
            d_spot = float(spot_t) - float(spot_prev)

        actual_pnl = float(equity_by_date.loc[trading_date]) - float(equity_by_date.loc[prev_date])
        prev_positions = positions_by_date.get(prev_date, pd.DataFrame())
        option_positions = _option_positions(prev_positions)
        etf_positions = _etf_positions(prev_positions)
        option_position_count = int(len(option_positions))

        exposures, missing_greeks_count = _option_exposures(option_positions, prev_date, greeks_by_key)
        if missing_greeks_count:
            flags.append("missing_greeks")

        iv_state = _vega_and_iv_change(option_positions, prev_date, trading_date, greeks_by_key, iv_by_key)
        if iv_state["missing_iv_count"]:
            flags.append("missing_iv")
        if iv_state["failed_iv_count"]:
            flags.append("failed_iv")

        delta_pnl = exposures["delta"] * d_spot
        gamma_pnl = 0.5 * exposures["gamma"] * d_spot * d_spot
        theta_pnl = exposures["theta"]
        vega_pnl = iv_state["vega_pnl"]
        hedge_pnl = _hedge_pnl(etf_positions, d_spot)
        cost_pnl = -_trade_fees(trades_by_date.get(trading_date, pd.DataFrame()))
        explained_pnl = delta_pnl + gamma_pnl + theta_pnl + vega_pnl + hedge_pnl + cost_pnl
        residual_pnl = actual_pnl - explained_pnl
        residual_ratio = abs(residual_pnl) / max(abs(actual_pnl), self.config.eps)
        residual_warning = residual_ratio > self.config.residual_warning_threshold
        if residual_warning:
            flags.append("residual_warning")

        row = {
            "trading_date": trading_date,
            "underlying": underlying_t,
            "actual_pnl": actual_pnl,
            "delta_pnl": delta_pnl,
            "gamma_pnl": gamma_pnl,
            "theta_pnl": theta_pnl,
            "vega_pnl": vega_pnl,
            "hedge_pnl": hedge_pnl,
            "cost_pnl": cost_pnl,
            "explained_pnl": explained_pnl,
            "residual_pnl": residual_pnl,
            "residual_ratio": residual_ratio,
            "gamma_theta_pnl": gamma_pnl + theta_pnl,
            "option_delta_exposure": exposures["delta"],
            "option_gamma_exposure": exposures["gamma"],
            "option_theta_exposure": exposures["theta"],
            "option_vega_exposure": exposures["vega"],
            "weighted_position_iv": iv_state["weighted_position_iv"],
            "weighted_position_iv_change": iv_state["weighted_position_iv_change"],
            "exposure_mode": self.config.exposure_mode,
            "quality_flags": ",".join(flags),
        }
        quality = _quality_row(
            trading_date,
            residual_ratio,
            residual_warning,
            missing_greeks_count,
            iv_state["missing_iv_count"],
            iv_state["failed_iv_count"],
            option_position_count,
            ",".join(flags),
        )
        return row, quality

    @staticmethod
    def _build_cumulative(daily: pd.DataFrame) -> pd.DataFrame:
        cumulative = pd.DataFrame({"trading_date": daily["trading_date"]})
        for source, target in [
            ("actual_pnl", "cum_actual_pnl"),
            ("delta_pnl", "cum_delta_pnl"),
            ("gamma_pnl", "cum_gamma_pnl"),
            ("theta_pnl", "cum_theta_pnl"),
            ("vega_pnl", "cum_vega_pnl"),
            ("hedge_pnl", "cum_hedge_pnl"),
            ("cost_pnl", "cum_cost_pnl"),
            ("explained_pnl", "cum_explained_pnl"),
            ("residual_pnl", "cum_residual_pnl"),
            ("gamma_theta_pnl", "cum_gamma_theta_pnl"),
        ]:
            cumulative[target] = daily[source].fillna(0.0).cumsum()
        return cumulative[CUMULATIVE_COLUMNS]


def _prepare_table(frame: pd.DataFrame, required: set[str], name: str) -> pd.DataFrame:
    frame = _normalize_trading_date(frame)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Missing columns for {name}: {missing}")
    return frame


def _prepare_optional_table(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if frame.empty and "trading_date" not in frame.columns:
        return pd.DataFrame(columns=["trading_date"])
    return _normalize_trading_date(frame)


def _normalize_trading_date(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if "trading_date" not in frame.columns:
        index_name = frame.index.name or "index"
        frame = frame.reset_index().rename(columns={index_name: "trading_date"})
    frame["trading_date"] = pd.to_datetime(frame["trading_date"]).dt.date
    return frame


def _active_positions(position_records: pd.DataFrame) -> dict[object, pd.DataFrame]:
    records = position_records.copy()
    records["instrument_id"] = records["instrument_id"].fillna("").astype(str)
    records["quantity"] = pd.to_numeric(records["quantity"], errors="coerce").fillna(0.0)
    records["multiplier"] = pd.to_numeric(records["multiplier"], errors="coerce").fillna(1.0)
    records = records[(records["instrument_id"] != "") & (records["quantity"] != 0.0)]
    return {trading_date: group.copy() for trading_date, group in records.groupby("trading_date", sort=False)}


def _active_trades(trade_records: pd.DataFrame) -> dict[object, pd.DataFrame]:
    if trade_records.empty:
        return {}
    records = trade_records.copy()
    if "instrument_id" in records.columns:
        records["instrument_id"] = records["instrument_id"].fillna("").astype(str)
        records = records[records["instrument_id"] != ""]
    if "fee" not in records.columns:
        records["fee"] = 0.0
    records["fee"] = pd.to_numeric(records["fee"], errors="coerce").fillna(0.0)
    return {trading_date: group.copy() for trading_date, group in records.groupby("trading_date", sort=False)}


def _option_positions(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return positions
    return positions[positions["instrument_type"].astype(str).str.lower().eq("option")].copy()


def _etf_positions(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return positions
    return positions[positions["instrument_type"].astype(str).str.lower().eq("etf")].copy()


def _option_exposures(
    option_positions: pd.DataFrame,
    prev_date: object,
    greeks_by_key: pd.DataFrame,
) -> tuple[dict[str, float], int]:
    exposures = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    missing_greeks_count = 0
    for _, position in option_positions.iterrows():
        contract_id = str(position["instrument_id"])
        if (prev_date, contract_id) not in greeks_by_key.index:
            missing_greeks_count += 1
            continue
        greek = greeks_by_key.loc[(prev_date, contract_id)]
        scale = float(position["quantity"]) * float(position["multiplier"])
        missing_required = False
        for key in exposures:
            value = pd.to_numeric(greek.get(key), errors="coerce")
            if pd.isna(value):
                missing_required = True
                continue
            exposures[key] += float(value) * scale
        if missing_required:
            missing_greeks_count += 1
    return exposures, missing_greeks_count


def _vega_and_iv_change(
    option_positions: pd.DataFrame,
    prev_date: object,
    trading_date: object,
    greeks_by_key: pd.DataFrame,
    iv_by_key: pd.DataFrame,
) -> dict[str, float | int]:
    vega_pnl = 0.0
    weighted_iv_sum = 0.0
    weighted_iv_change_sum = 0.0
    abs_vega_sum = 0.0
    missing_iv_count = 0
    failed_iv_count = 0
    for _, position in option_positions.iterrows():
        contract_id = str(position["instrument_id"])
        if (prev_date, contract_id) not in greeks_by_key.index:
            continue
        greek = greeks_by_key.loc[(prev_date, contract_id)]
        vega = pd.to_numeric(greek.get("vega"), errors="coerce")
        if pd.isna(vega):
            continue
        position_vega = float(vega) * float(position["quantity"]) * float(position["multiplier"])

        if (prev_date, contract_id) not in iv_by_key.index or (trading_date, contract_id) not in iv_by_key.index:
            missing_iv_count += 1
            continue
        prev_iv_row = iv_by_key.loc[(prev_date, contract_id)]
        iv_row = iv_by_key.loc[(trading_date, contract_id)]
        prev_iv = pd.to_numeric(prev_iv_row.get("iv"), errors="coerce")
        iv = pd.to_numeric(iv_row.get("iv"), errors="coerce")
        if pd.isna(prev_iv) or pd.isna(iv):
            missing_iv_count += 1
            continue
        if _iv_failed(prev_iv_row) or _iv_failed(iv_row):
            failed_iv_count += 1
        iv_change = float(iv) - float(prev_iv)
        abs_vega = abs(position_vega)
        vega_pnl += position_vega * iv_change
        weighted_iv_sum += abs_vega * float(prev_iv)
        weighted_iv_change_sum += abs_vega * iv_change
        abs_vega_sum += abs_vega
    return {
        "vega_pnl": vega_pnl,
        "weighted_position_iv": weighted_iv_sum / abs_vega_sum if abs_vega_sum else 0.0,
        "weighted_position_iv_change": weighted_iv_change_sum / abs_vega_sum if abs_vega_sum else 0.0,
        "missing_iv_count": missing_iv_count,
        "failed_iv_count": failed_iv_count,
    }


def _iv_failed(row: pd.Series) -> bool:
    status = str(row.get("iv_status", "ok")).lower()
    return status not in {"", "ok", "valid"}


def _hedge_pnl(etf_positions: pd.DataFrame, d_spot: float) -> float:
    if etf_positions.empty:
        return 0.0
    return float((etf_positions["quantity"].astype(float) * d_spot).sum())


def _trade_fees(trades: pd.DataFrame) -> float:
    if trades.empty or "fee" not in trades.columns:
        return 0.0
    return float(pd.to_numeric(trades["fee"], errors="coerce").fillna(0.0).sum())


def _spot_for_date(underlying_by_date: pd.DataFrame, trading_date: object) -> tuple[float, object]:
    if trading_date not in underlying_by_date.index:
        return float("nan"), ""
    row = underlying_by_date.loc[trading_date]
    close = pd.to_numeric(row.get("close"), errors="coerce")
    underlying = row.get("underlying", "")
    return float(close) if not pd.isna(close) else float("nan"), underlying


def _underlying_for_date(underlying_by_date: pd.DataFrame, trading_date: object) -> object:
    if trading_date not in underlying_by_date.index:
        return ""
    return underlying_by_date.loc[trading_date].get("underlying", "")


def _empty_daily_row(trading_date: object, underlying: object, exposure_mode: str) -> dict[str, object]:
    return {
        "trading_date": trading_date,
        "underlying": underlying,
        "actual_pnl": 0.0,
        "delta_pnl": 0.0,
        "gamma_pnl": 0.0,
        "theta_pnl": 0.0,
        "vega_pnl": 0.0,
        "hedge_pnl": 0.0,
        "cost_pnl": 0.0,
        "explained_pnl": 0.0,
        "residual_pnl": 0.0,
        "residual_ratio": 0.0,
        "gamma_theta_pnl": 0.0,
        "option_delta_exposure": 0.0,
        "option_gamma_exposure": 0.0,
        "option_theta_exposure": 0.0,
        "option_vega_exposure": 0.0,
        "weighted_position_iv": 0.0,
        "weighted_position_iv_change": 0.0,
        "exposure_mode": exposure_mode,
        "quality_flags": "",
    }


def _quality_row(
    trading_date: object,
    residual_ratio: float,
    residual_warning: bool,
    missing_greeks_count: int,
    missing_iv_count: int,
    failed_iv_count: int,
    option_position_count: int,
    notes: str,
) -> dict[str, object]:
    return {
        "trading_date": trading_date,
        "residual_ratio": residual_ratio,
        "residual_warning": residual_warning,
        "missing_greeks_count": missing_greeks_count,
        "missing_iv_count": missing_iv_count,
        "failed_iv_count": failed_iv_count,
        "option_position_count": option_position_count,
        "notes": notes,
    }
