from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gamma_scalping.export_format import format_for_csv


EPISODE_CAPTURE_COLUMNS = [
    "episode_id",
    "start_date",
    "end_date",
    "holding_days",
    "entry_atm_iv",
    "realized_vol_holding",
    "net_gamma_scalping_pnl",
    "theoretical_vol_edge_pnl",
    "iv_hv_capture_rate",
    "gamma_theta_pnl",
    "hedge_pnl",
    "cost_pnl",
    "vega_pnl",
    "valid",
    "invalid_reason",
]


@dataclass(frozen=True)
class IvHvCaptureConfig:
    annual_trading_days: int = 252
    denominator_eps: float = 1e-8
    min_return_observations: int = 2


@dataclass(frozen=True)
class IvHvCaptureResult:
    episodes: pd.DataFrame
    summary: dict[str, float]

    def export_csv(self, output_dir: Path | str) -> dict[str, Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "iv_hv_capture_episodes": output_dir / "iv_hv_capture_episodes.csv",
        }
        format_for_csv(self.episodes).to_csv(paths["iv_hv_capture_episodes"], index=False)
        return paths


class IvHvCaptureAnalyzer:
    def __init__(self, config: IvHvCaptureConfig | None = None) -> None:
        self.config = config or IvHvCaptureConfig()

    def compute(
        self,
        *,
        episode_records: pd.DataFrame,
        attribution: Any,
        underlying_history: pd.DataFrame,
    ) -> IvHvCaptureResult:
        episode_records = _normalize_dates(episode_records)
        attribution_by_episode = _attribution_by_episode(attribution)
        attribution_by_episode = _normalize_dates(attribution_by_episode)
        underlying_history = _prepare_underlying(underlying_history)
        if episode_records.empty:
            episodes = pd.DataFrame(columns=EPISODE_CAPTURE_COLUMNS)
            return IvHvCaptureResult(episodes=episodes, summary=_capture_summary(episodes, self.config.denominator_eps))

        rows = []
        for episode in episode_records.itertuples(index=False):
            rows.append(self._episode_row(episode, attribution_by_episode, underlying_history))
        episodes = pd.DataFrame(rows, columns=EPISODE_CAPTURE_COLUMNS)
        return IvHvCaptureResult(episodes=episodes, summary=_capture_summary(episodes, self.config.denominator_eps))

    def _episode_row(
        self,
        episode: Any,
        attribution_by_episode: pd.DataFrame,
        underlying_history: pd.DataFrame,
    ) -> dict[str, object]:
        episode_id = str(getattr(episode, "episode_id", ""))
        start_date = getattr(episode, "opened_at", "")
        end_date = getattr(episode, "closed_at", "") or ""
        entry_atm_iv = _to_float(getattr(episode, "entry_atm_iv", np.nan))
        invalid_reasons: list[str] = []
        if not episode_id:
            invalid_reasons.append("missing_episode_id")
        if pd.isna(entry_atm_iv) or entry_atm_iv <= 0:
            invalid_reasons.append("missing_entry_atm_iv")

        rows = attribution_by_episode[attribution_by_episode["episode_id"].astype(str).eq(episode_id)].copy()
        if rows.empty:
            invalid_reasons.append("missing_episode_attribution")

        realized_returns = []
        theoretical_values = []
        rows = rows.sort_values("trading_date") if not rows.empty else rows
        for row in rows.itertuples(index=False):
            trading_date = getattr(row, "trading_date")
            spot_prev, spot_t = _spot_pair(underlying_history, trading_date)
            if pd.isna(spot_prev) or pd.isna(spot_t) or spot_prev <= 0 or spot_t <= 0:
                invalid_reasons.append("missing_underlying")
                continue
            # BSM continuous-time approximation: use log return intentionally.
            # This differs from simple return for large one-day moves.
            log_return = float(np.log(spot_t / spot_prev))
            realized_returns.append(log_return)
            gamma_exposure = _to_float(getattr(row, "option_gamma_exposure", np.nan))
            if pd.isna(gamma_exposure) or pd.isna(entry_atm_iv):
                continue
            dt = 1.0 / self.config.annual_trading_days
            theoretical_values.append(0.5 * gamma_exposure * spot_prev * spot_prev * (log_return * log_return - entry_atm_iv * entry_atm_iv * dt))

        if len(realized_returns) < self.config.min_return_observations:
            invalid_reasons.append("insufficient_holding_days")

        realized_vol_holding = (
            float(pd.Series(realized_returns).std(ddof=0) * np.sqrt(self.config.annual_trading_days))
            if realized_returns
            else np.nan
        )
        theoretical_vol_edge_pnl = float(np.nansum(theoretical_values)) if theoretical_values else 0.0
        if abs(theoretical_vol_edge_pnl) <= self.config.denominator_eps:
            invalid_reasons.append("near_zero_theoretical_edge")

        gamma_theta_pnl = _sum(rows, "gamma_theta_pnl")
        hedge_pnl = _sum(rows, "hedge_pnl")
        cost_pnl = _sum(rows, "cost_pnl")
        vega_pnl = _sum(rows, "vega_pnl")
        net_gamma_scalping_pnl = gamma_theta_pnl + hedge_pnl + cost_pnl
        valid = not invalid_reasons
        capture_rate = net_gamma_scalping_pnl / theoretical_vol_edge_pnl if valid else np.nan
        return {
            "episode_id": episode_id,
            "start_date": start_date,
            "end_date": end_date,
            "holding_days": float(len(rows["trading_date"].unique())) if not rows.empty else 0.0,
            "entry_atm_iv": entry_atm_iv,
            "realized_vol_holding": realized_vol_holding,
            "net_gamma_scalping_pnl": net_gamma_scalping_pnl,
            "theoretical_vol_edge_pnl": theoretical_vol_edge_pnl,
            "iv_hv_capture_rate": capture_rate,
            "gamma_theta_pnl": gamma_theta_pnl,
            "hedge_pnl": hedge_pnl,
            "cost_pnl": cost_pnl,
            "vega_pnl": vega_pnl,
            "valid": valid,
            "invalid_reason": ",".join(dict.fromkeys(invalid_reasons)),
        }


def _attribution_by_episode(attribution: Any) -> pd.DataFrame:
    if isinstance(attribution, pd.DataFrame):
        return attribution.copy()
    frame = getattr(attribution, "by_episode", None)
    if frame is None:
        raise ValueError("attribution must provide a by_episode DataFrame")
    return frame.copy()


def _normalize_dates(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in ["trading_date", "opened_at", "closed_at", "start_date", "end_date"]:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.date
    return frame


def _prepare_underlying(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if "trading_date" not in frame.columns:
        index_name = frame.index.name or "index"
        frame = frame.reset_index().rename(columns={index_name: "trading_date"})
    if "close" not in frame.columns:
        raise ValueError("underlying_history must contain a 'close' column")
    frame["trading_date"] = pd.to_datetime(frame["trading_date"], errors="coerce").dt.date
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.sort_values("trading_date").reset_index(drop=True)
    frame["prev_close"] = frame["close"].shift(1)
    return frame.set_index("trading_date")


def _spot_pair(underlying_history: pd.DataFrame, trading_date: object) -> tuple[float, float]:
    if trading_date not in underlying_history.index:
        return np.nan, np.nan
    row = underlying_history.loc[trading_date]
    return _to_float(row.get("prev_close", np.nan)), _to_float(row.get("close", np.nan))


def _to_float(value: object) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    return float(numeric) if not pd.isna(numeric) else np.nan


def _sum(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())


def _capture_summary(episodes: pd.DataFrame, denominator_eps: float) -> dict[str, float]:
    if episodes.empty or "valid" not in episodes.columns:
        return {
            "iv_hv_capture_rate_mean": 0.0,
            "iv_hv_capture_rate_median": 0.0,
            "iv_hv_capture_rate_weighted": 0.0,
            "iv_hv_capture_rate_valid_count": 0.0,
            "iv_hv_signal_hit_rate": 0.0,
        }
    valid = episodes[episodes["valid"].astype(bool)].copy()
    if valid.empty:
        return {
            "iv_hv_capture_rate_mean": 0.0,
            "iv_hv_capture_rate_median": 0.0,
            "iv_hv_capture_rate_weighted": 0.0,
            "iv_hv_capture_rate_valid_count": 0.0,
            "iv_hv_signal_hit_rate": 0.0,
        }
    capture_rate = pd.to_numeric(valid["iv_hv_capture_rate"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    denominator = pd.to_numeric(valid["theoretical_vol_edge_pnl"], errors="coerce").fillna(0.0).sum()
    numerator = pd.to_numeric(valid["net_gamma_scalping_pnl"], errors="coerce").fillna(0.0).sum()
    weighted = numerator / denominator if abs(denominator) > denominator_eps else 0.0
    signal_match = np.sign(pd.to_numeric(valid["realized_vol_holding"], errors="coerce") - pd.to_numeric(valid["entry_atm_iv"], errors="coerce")) == np.sign(
        pd.to_numeric(valid["theoretical_vol_edge_pnl"], errors="coerce")
    )
    return {
        "iv_hv_capture_rate_mean": float(capture_rate.mean()),
        "iv_hv_capture_rate_median": float(capture_rate.median()),
        "iv_hv_capture_rate_weighted": float(weighted),
        "iv_hv_capture_rate_valid_count": float(len(valid)),
        "iv_hv_signal_hit_rate": float(signal_match.mean()),
    }
