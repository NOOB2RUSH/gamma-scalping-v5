from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from gamma_scalping.export_format import format_for_csv


DETAIL_COLUMNS = [
    "trading_date",
    "episode_id",
    "instrument_id",
    "instrument_type",
    "quantity_prev",
    "multiplier",
    "prev_mark_price",
    "curr_mark_price",
    "mark_pnl",
    "delta_pnl",
    "gamma_pnl",
    "theta_pnl",
    "vega_pnl",
    "greeks_explained_pnl",
    "model_repricing_pnl",
    "market_model_basis_pnl",
    "taylor_residual_pnl",
    "mark_residual_pnl",
    "trade_cash_pnl",
    "fee_pnl",
    "execution_vs_mark_pnl",
    "missing_flags",
]

DAILY_COLUMNS = [
    "trading_date",
    "mark_pnl",
    "greeks_explained_pnl",
    "model_repricing_pnl",
    "market_model_basis_pnl",
    "taylor_residual_pnl",
    "mark_residual_pnl",
    "trade_cash_pnl",
    "fee_pnl",
    "execution_vs_mark_pnl",
    "detail_count",
    "missing_flags",
]


@dataclass(frozen=True)
class PricingReconciliationResult:
    detail: pd.DataFrame
    daily: pd.DataFrame

    def export_csv(self, output_dir: Path | str) -> dict[str, Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "pricing_reconciliation": output_dir / "pricing_reconciliation.csv",
            "pricing_reconciliation_daily": output_dir / "pricing_reconciliation_daily.csv",
        }
        format_for_csv(self.detail).to_csv(paths["pricing_reconciliation"], index=False)
        format_for_csv(self.daily).to_csv(paths["pricing_reconciliation_daily"], index=False)
        return paths


class PricingReconciliation:
    def reconcile(
        self,
        *,
        equity_curve: pd.DataFrame,
        trade_records: pd.DataFrame,
        position_records: pd.DataFrame,
        greeks_history: pd.DataFrame,
        iv_history: pd.DataFrame,
        underlying_history: pd.DataFrame,
    ) -> PricingReconciliationResult:
        equity_curve = _normalize_trading_date(equity_curve).sort_values("trading_date")
        if equity_curve.empty:
            return PricingReconciliationResult(
                detail=pd.DataFrame(columns=DETAIL_COLUMNS),
                daily=pd.DataFrame(columns=DAILY_COLUMNS),
            )

        trade_records = _normalize_optional(trade_records)
        position_records = _normalize_trading_date(position_records)
        greeks_history = _normalize_trading_date(greeks_history)
        iv_history = _normalize_trading_date(iv_history)
        underlying_history = _normalize_trading_date(underlying_history)

        dates = equity_curve["trading_date"].tolist()
        positions_by_date = _active_positions(position_records)
        trades_by_date = _active_trades(trade_records)
        greeks_by_key = greeks_history.set_index(["trading_date", "contract_id"])
        iv_by_key = iv_history.set_index(["trading_date", "contract_id"])
        underlying_by_date = underlying_history.set_index("trading_date")

        detail_rows: list[dict[str, object]] = []
        daily_rows: list[dict[str, object]] = []
        for idx in range(1, len(dates)):
            trading_date = dates[idx]
            prev_date = dates[idx - 1]
            prev_positions = positions_by_date.get(prev_date, pd.DataFrame())
            curr_positions = positions_by_date.get(trading_date, pd.DataFrame())
            trades = trades_by_date.get(trading_date, pd.DataFrame())
            detail = self._detail_rows(
                trading_date=trading_date,
                prev_date=prev_date,
                prev_positions=prev_positions,
                curr_positions=curr_positions,
                trades=trades,
                greeks_by_key=greeks_by_key,
                iv_by_key=iv_by_key,
                underlying_by_date=underlying_by_date,
            )
            detail_rows.extend(detail)
            daily_rows.append(_daily_row(trading_date, detail, trades))

        detail_frame = pd.DataFrame(detail_rows, columns=DETAIL_COLUMNS)
        daily_frame = pd.DataFrame(daily_rows, columns=DAILY_COLUMNS)
        return PricingReconciliationResult(detail=detail_frame, daily=daily_frame)

    def _detail_rows(
        self,
        *,
        trading_date: object,
        prev_date: object,
        prev_positions: pd.DataFrame,
        curr_positions: pd.DataFrame,
        trades: pd.DataFrame,
        greeks_by_key: pd.DataFrame,
        iv_by_key: pd.DataFrame,
        underlying_by_date: pd.DataFrame,
    ) -> list[dict[str, object]]:
        rows = []
        for _, position in prev_positions.iterrows():
            instrument_type = str(position["instrument_type"]).lower()
            instrument_id = str(position["instrument_id"])
            episode_id = str(position.get("episode_id", "") or "")
            quantity = _to_float(position.get("quantity", 0.0))
            multiplier = _to_float(position.get("multiplier", 1.0)) or 1.0
            flags: list[str] = []
            prev_mark = _to_float(position.get("mark_price"))
            curr_mark = _current_mark_price(curr_positions, instrument_id, episode_id)
            if pd.isna(curr_mark):
                curr_mark = _exit_trade_price(trades, instrument_id=instrument_id, episode_id=episode_id)
            if pd.isna(prev_mark) or pd.isna(curr_mark):
                flags.append("missing_mark_price")
                mark_pnl = 0.0
            else:
                mark_pnl = quantity * multiplier * (curr_mark - prev_mark)

            if instrument_type == "option":
                greeks = _greeks_pnl(
                    instrument_id=instrument_id,
                    prev_date=prev_date,
                    trading_date=trading_date,
                    quantity=quantity,
                    multiplier=multiplier,
                    greeks_by_key=greeks_by_key,
                    iv_by_key=iv_by_key,
                    underlying_by_date=underlying_by_date,
                )
                flags.extend(greeks.pop("flags"))
                model_repricing = _model_repricing_pnl(
                    instrument_id=instrument_id,
                    prev_date=prev_date,
                    trading_date=trading_date,
                    quantity=quantity,
                    multiplier=multiplier,
                    greeks_by_key=greeks_by_key,
                    flags=flags,
                )
            elif instrument_type == "etf":
                greeks = _etf_pnl(
                    prev_date=prev_date,
                    trading_date=trading_date,
                    quantity=quantity,
                    underlying_by_date=underlying_by_date,
                )
                flags.extend(greeks.pop("flags"))
                model_repricing = greeks["delta_pnl"]
            else:
                greeks = _zero_greeks()
                flags.append("unsupported_instrument_type")
                model_repricing = 0.0

            greeks_explained = sum(greeks.values())
            market_model_basis = mark_pnl - model_repricing
            taylor_residual = model_repricing - greeks_explained
            trade_cash_pnl, fee_pnl, execution_vs_mark = _trade_components(
                trades,
                instrument_id=instrument_id,
                episode_id=episode_id,
                curr_mark=curr_mark,
            )
            rows.append(
                {
                    "trading_date": trading_date,
                    "episode_id": episode_id,
                    "instrument_id": instrument_id,
                    "instrument_type": instrument_type,
                    "quantity_prev": quantity,
                    "multiplier": multiplier,
                    "prev_mark_price": prev_mark,
                    "curr_mark_price": curr_mark,
                    "mark_pnl": mark_pnl,
                    "delta_pnl": greeks["delta_pnl"],
                    "gamma_pnl": greeks["gamma_pnl"],
                    "theta_pnl": greeks["theta_pnl"],
                    "vega_pnl": greeks["vega_pnl"],
                    "greeks_explained_pnl": greeks_explained,
                    "model_repricing_pnl": model_repricing,
                    "market_model_basis_pnl": market_model_basis,
                    "taylor_residual_pnl": taylor_residual,
                    "mark_residual_pnl": mark_pnl - greeks_explained,
                    "trade_cash_pnl": trade_cash_pnl,
                    "fee_pnl": fee_pnl,
                    "execution_vs_mark_pnl": execution_vs_mark,
                    "missing_flags": ",".join(sorted(set(flags))),
                }
            )
        return rows


def _greeks_pnl(
    *,
    instrument_id: str,
    prev_date: object,
    trading_date: object,
    quantity: float,
    multiplier: float,
    greeks_by_key: pd.DataFrame,
    iv_by_key: pd.DataFrame,
    underlying_by_date: pd.DataFrame,
) -> dict[str, object]:
    flags: list[str] = []
    if (prev_date, instrument_id) not in greeks_by_key.index:
        return {**_zero_greeks(), "flags": ["missing_greeks"]}
    greek = greeks_by_key.loc[(prev_date, instrument_id)]
    spot_prev = _spot(underlying_by_date, prev_date)
    spot = _spot(underlying_by_date, trading_date)
    if pd.isna(spot_prev) or pd.isna(spot):
        d_spot = 0.0
        flags.append("missing_underlying")
    else:
        d_spot = spot - spot_prev

    scale = quantity * multiplier
    delta = _to_float(greek.get("delta"))
    gamma = _to_float(greek.get("gamma"))
    theta = _to_float(greek.get("theta"))
    vega = _to_float(greek.get("vega"))
    if any(pd.isna(value) for value in [delta, gamma, theta, vega]):
        flags.append("missing_greeks")
    iv_change = _iv_change(iv_by_key, prev_date, trading_date, instrument_id, flags)
    return {
        "delta_pnl": 0.0 if pd.isna(delta) else delta * scale * d_spot,
        "gamma_pnl": 0.0 if pd.isna(gamma) else 0.5 * gamma * scale * d_spot * d_spot,
        "theta_pnl": 0.0 if pd.isna(theta) else theta * scale,
        "vega_pnl": 0.0 if pd.isna(vega) or pd.isna(iv_change) else vega * scale * iv_change,
        "flags": flags,
    }


def _etf_pnl(
    *,
    prev_date: object,
    trading_date: object,
    quantity: float,
    underlying_by_date: pd.DataFrame,
) -> dict[str, object]:
    flags: list[str] = []
    spot_prev = _spot(underlying_by_date, prev_date)
    spot = _spot(underlying_by_date, trading_date)
    if pd.isna(spot_prev) or pd.isna(spot):
        flags.append("missing_underlying")
        delta_pnl = 0.0
    else:
        delta_pnl = quantity * (spot - spot_prev)
    return {
        "delta_pnl": delta_pnl,
        "gamma_pnl": 0.0,
        "theta_pnl": 0.0,
        "vega_pnl": 0.0,
        "flags": flags,
    }


def _model_repricing_pnl(
    *,
    instrument_id: str,
    prev_date: object,
    trading_date: object,
    quantity: float,
    multiplier: float,
    greeks_by_key: pd.DataFrame,
    flags: list[str],
) -> float:
    if (prev_date, instrument_id) not in greeks_by_key.index or (trading_date, instrument_id) not in greeks_by_key.index:
        flags.append("missing_model_price")
        return 0.0
    prev_price = _to_float(greeks_by_key.loc[(prev_date, instrument_id)].get("theoretical_price"))
    price = _to_float(greeks_by_key.loc[(trading_date, instrument_id)].get("theoretical_price"))
    if pd.isna(prev_price) or pd.isna(price):
        flags.append("missing_model_price")
        return 0.0
    return quantity * multiplier * (price - prev_price)


def _trade_components(
    trades: pd.DataFrame,
    *,
    instrument_id: str,
    episode_id: str,
    curr_mark: float,
) -> tuple[float, float, float]:
    if trades.empty:
        return 0.0, 0.0, 0.0
    matched = trades[
        trades["instrument_id"].fillna("").astype(str).eq(instrument_id)
        & trades["episode_id"].fillna("").astype(str).eq(episode_id)
    ].copy()
    if matched.empty:
        return 0.0, 0.0, 0.0
    quantity = pd.to_numeric(matched["quantity"], errors="coerce").fillna(0.0)
    price = pd.to_numeric(matched["price"], errors="coerce").fillna(0.0)
    if "multiplier" in matched.columns:
        multiplier = pd.to_numeric(matched["multiplier"], errors="coerce").fillna(1.0)
    else:
        multiplier = pd.Series(1.0, index=matched.index)
    sign = matched["side"].map({"buy": 1.0, "sell": -1.0}).fillna(0.0)
    signed_quantity = sign * quantity
    trade_cash_pnl = float((-signed_quantity * price * multiplier).sum())
    fee_pnl = -float(pd.to_numeric(matched.get("fee", 0.0), errors="coerce").fillna(0.0).sum())
    if pd.isna(curr_mark):
        execution_vs_mark = 0.0
    else:
        execution_vs_mark = float((signed_quantity * multiplier * (curr_mark - price)).sum())
    return trade_cash_pnl, fee_pnl, execution_vs_mark


def _daily_row(trading_date: object, detail: list[dict[str, object]], trades: pd.DataFrame) -> dict[str, object]:
    frame = pd.DataFrame(detail)
    if frame.empty:
        sums = {column: 0.0 for column in DAILY_COLUMNS if column.endswith("_pnl")}
        flags = ""
        count = 0
    else:
        sums = {column: float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum()) for column in DAILY_COLUMNS if column.endswith("_pnl")}
        flags = ",".join(sorted({flag for value in frame["missing_flags"].fillna("") for flag in str(value).split(",") if flag}))
        count = int(len(frame))
    trade_fee = -float(pd.to_numeric(trades.get("fee", 0.0), errors="coerce").fillna(0.0).sum()) if not trades.empty else 0.0
    sums["fee_pnl"] = trade_fee
    return {
        "trading_date": trading_date,
        "mark_pnl": sums["mark_pnl"],
        "greeks_explained_pnl": sums["greeks_explained_pnl"],
        "model_repricing_pnl": sums["model_repricing_pnl"],
        "market_model_basis_pnl": sums["market_model_basis_pnl"],
        "taylor_residual_pnl": sums["taylor_residual_pnl"],
        "mark_residual_pnl": sums["mark_residual_pnl"],
        "trade_cash_pnl": sums["trade_cash_pnl"],
        "fee_pnl": sums["fee_pnl"],
        "execution_vs_mark_pnl": sums["execution_vs_mark_pnl"],
        "detail_count": count,
        "missing_flags": flags,
    }


def _active_positions(position_records: pd.DataFrame) -> dict[object, pd.DataFrame]:
    records = position_records.copy()
    records["instrument_id"] = records["instrument_id"].fillna("").astype(str)
    records["quantity"] = pd.to_numeric(records["quantity"], errors="coerce").fillna(0.0)
    records["multiplier"] = pd.to_numeric(records["multiplier"], errors="coerce").fillna(1.0)
    if "episode_id" not in records.columns:
        records["episode_id"] = ""
    records["episode_id"] = records["episode_id"].fillna("").astype(str)
    return {
        trading_date: group.copy()
        for trading_date, group in records[(records["instrument_id"] != "") & (records["quantity"] != 0.0)].groupby("trading_date", sort=False)
    }


def _active_trades(trade_records: pd.DataFrame) -> dict[object, pd.DataFrame]:
    if trade_records.empty:
        return {}
    records = trade_records.copy()
    records["instrument_id"] = records["instrument_id"].fillna("").astype(str)
    records = records[records["instrument_id"] != ""]
    if "episode_id" not in records.columns:
        records["episode_id"] = ""
    records["episode_id"] = records["episode_id"].fillna("").astype(str)
    if "fee" not in records.columns:
        records["fee"] = 0.0
    return {trading_date: group.copy() for trading_date, group in records.groupby("trading_date", sort=False)}


def _current_mark_price(curr_positions: pd.DataFrame, instrument_id: str, episode_id: str) -> float:
    if curr_positions.empty:
        return float("nan")
    matched = curr_positions[
        curr_positions["instrument_id"].fillna("").astype(str).eq(instrument_id)
        & curr_positions["episode_id"].fillna("").astype(str).eq(episode_id)
    ]
    if matched.empty:
        return float("nan")
    return _to_float(matched.iloc[0].get("mark_price"))


def _exit_trade_price(trades: pd.DataFrame, *, instrument_id: str, episode_id: str) -> float:
    if trades.empty:
        return float("nan")
    matched = trades[
        trades["instrument_id"].fillna("").astype(str).eq(instrument_id)
        & trades["episode_id"].fillna("").astype(str).eq(episode_id)
    ].copy()
    if matched.empty:
        return float("nan")
    quantity = pd.to_numeric(matched["quantity"], errors="coerce").fillna(0.0)
    price = pd.to_numeric(matched["price"], errors="coerce").fillna(0.0)
    total_quantity = float(quantity.sum())
    if total_quantity <= 0:
        return float("nan")
    return float((quantity * price).sum() / total_quantity)


def _iv_change(iv_by_key: pd.DataFrame, prev_date: object, trading_date: object, contract_id: str, flags: list[str]) -> float:
    if (prev_date, contract_id) not in iv_by_key.index or (trading_date, contract_id) not in iv_by_key.index:
        flags.append("missing_iv")
        return float("nan")
    prev_iv = _to_float(iv_by_key.loc[(prev_date, contract_id)].get("iv"))
    iv = _to_float(iv_by_key.loc[(trading_date, contract_id)].get("iv"))
    if pd.isna(prev_iv) or pd.isna(iv):
        flags.append("missing_iv")
        return float("nan")
    return iv - prev_iv


def _spot(underlying_by_date: pd.DataFrame, trading_date: object) -> float:
    if trading_date not in underlying_by_date.index:
        return float("nan")
    return _to_float(underlying_by_date.loc[trading_date].get("close"))


def _zero_greeks() -> dict[str, float]:
    return {"delta_pnl": 0.0, "gamma_pnl": 0.0, "theta_pnl": 0.0, "vega_pnl": 0.0}


def _normalize_optional(frame: pd.DataFrame) -> pd.DataFrame:
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


def _to_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")
