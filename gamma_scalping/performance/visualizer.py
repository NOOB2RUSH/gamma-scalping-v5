from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


class Visualizer:
    def __init__(self, matplotlib_config_dir: Path | str | None = None) -> None:
        self.matplotlib_config_dir = None if matplotlib_config_dir is None else str(matplotlib_config_dir)

    def plot_equity_curve(self, result: Any, underlying_history: pd.DataFrame | None = None):
        frame = _as_frame(result, "equity_curve")
        frame = _date_sorted(frame)
        if "equity" not in frame.columns:
            raise ValueError("equity curve frame must contain an 'equity' column")
        equity = pd.to_numeric(frame["equity"], errors="coerce")
        strategy_return = equity / equity.dropna().iloc[0] - 1.0
        plt = _pyplot(self.matplotlib_config_dir)
        percent_formatter = _percent_formatter(plt)
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.plot(frame["trading_date"], strategy_return, label="strategy")
        underlying = _normalized_underlying_return(underlying_history)
        if underlying is not None:
            ax.plot(underlying["trading_date"], underlying["return"], label="50ETF")
        ax.set_title("Strategy vs 50ETF")
        ax.set_xlabel("Trading Date")
        ax.set_ylabel("Cumulative Return")
        ax.yaxis.set_major_formatter(percent_formatter)
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    def plot_drawdown(self, result: Any):
        frame = _as_frame(result, "equity_curve")
        frame = _date_sorted(frame)
        if "equity" not in frame.columns:
            raise ValueError("equity curve frame must contain an 'equity' column")
        equity = pd.to_numeric(frame["equity"], errors="coerce")
        drawdown = equity / equity.cummax() - 1.0
        plt = _pyplot(self.matplotlib_config_dir)
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.fill_between(frame["trading_date"], drawdown, 0.0, alpha=0.35)
        ax.set_title("Drawdown")
        ax.set_xlabel("Trading Date")
        ax.set_ylabel("Drawdown")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    def plot_volatility_series(self, volatility: Any):
        frame = _as_frame(volatility, "frame")
        frame = _date_sorted(frame)
        columns = [column for column in ["atm_iv", "hv_10", "hv_20", "hv_60", "iv_hv_spread", "hv_iv_edge"] if column in frame]
        if not columns:
            raise ValueError("volatility frame must contain at least one supported volatility column")
        plt = _pyplot(self.matplotlib_config_dir)
        fig, ax = plt.subplots(figsize=(10, 5))
        for column in columns:
            ax.plot(frame["trading_date"], pd.to_numeric(frame[column], errors="coerce"), label=column)
        ax.set_title("Volatility Series")
        ax.set_xlabel("Trading Date")
        ax.set_ylabel("Volatility")
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    def plot_greeks_attribution(self, attribution: Any):
        frame = _as_frame(attribution, "daily")
        frame = _date_sorted(frame)
        columns = [
            column
            for column in ["delta_pnl", "gamma_pnl", "theta_pnl", "vega_pnl", "hedge_pnl", "cost_pnl", "residual_pnl"]
            if column in frame
        ]
        if not columns:
            raise ValueError("attribution daily frame must contain PnL component columns")
        plt = _pyplot(self.matplotlib_config_dir)
        fig, ax = plt.subplots(figsize=(10, 5))
        bottom_pos = np.zeros(len(frame))
        bottom_neg = np.zeros(len(frame))
        for column in columns:
            values = pd.to_numeric(frame[column], errors="coerce").fillna(0.0).to_numpy()
            bottom = np.where(values >= 0, bottom_pos, bottom_neg)
            ax.bar(frame["trading_date"], values, bottom=bottom, label=column, width=0.8)
            bottom_pos += np.where(values >= 0, values, 0.0)
            bottom_neg += np.where(values < 0, values, 0.0)
        ax.set_title("Greeks Attribution")
        ax.set_xlabel("Trading Date")
        ax.set_ylabel("PnL")
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend()
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    def plot_greeks_attribution_cumulative(self, attribution: Any):
        frame = _as_frame(attribution, "cumulative")
        frame = _date_sorted(frame)
        columns = [column for column in ["cum_delta_pnl", "cum_gamma_pnl", "cum_theta_pnl", "cum_vega_pnl"] if column in frame]
        if not columns:
            raise ValueError("attribution cumulative frame must contain cumulative Greeks PnL columns")
        plt = _pyplot(self.matplotlib_config_dir)
        fig, ax = plt.subplots(figsize=(10, 5))
        for column in columns:
            ax.plot(frame["trading_date"], pd.to_numeric(frame[column], errors="coerce"), label=column)
        ax.set_title("Cumulative Greeks Attribution")
        ax.set_xlabel("Trading Date")
        ax.set_ylabel("Cumulative PnL")
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.autofmt_xdate()
        fig.tight_layout()
        return fig

    def save(self, figure: Any, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path, dpi=150)
        _pyplot(self.matplotlib_config_dir).close(figure)
        return path


def _pyplot(matplotlib_config_dir: str | None = None):
    if matplotlib_config_dir:
        os.environ.setdefault("MPLCONFIGDIR", matplotlib_config_dir)
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


def _as_frame(source: Any, attr: str) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    frame = getattr(source, attr, None)
    if frame is None:
        raise ValueError(f"Missing required frame: {attr}")
    return frame.copy()


def _date_sorted(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if "trading_date" not in frame.columns and frame.index.name:
        frame = frame.reset_index().rename(columns={frame.index.name: "trading_date"})
    if "trading_date" not in frame.columns:
        raise ValueError("frame must contain a 'trading_date' column")
    frame["trading_date"] = pd.to_datetime(frame["trading_date"])
    return frame.sort_values("trading_date").reset_index(drop=True)


def _normalized_underlying_return(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None or frame.empty or "close" not in frame.columns:
        return None
    normalized = _date_sorted(frame)
    close = pd.to_numeric(normalized["close"], errors="coerce")
    valid = close.dropna()
    if valid.empty:
        return None
    normalized["return"] = close / valid.iloc[0] - 1.0
    return normalized[["trading_date", "return"]]


def _percent_formatter(plt: Any):
    import matplotlib.ticker as mtick

    return mtick.PercentFormatter(xmax=1.0)
