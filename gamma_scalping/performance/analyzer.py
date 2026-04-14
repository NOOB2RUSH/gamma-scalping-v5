from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PerformanceConfig:
    annual_trading_days: int = 252
    risk_free_rate: float = 0.0
    var_level: float = 0.05


@dataclass(frozen=True)
class PerformanceMetrics:
    summary: dict[str, float]
    daily_returns: pd.DataFrame
    monthly_returns: pd.DataFrame


@dataclass(frozen=True)
class PerformanceReport:
    metrics: PerformanceMetrics
    paths: dict[str, Path]


class PerformanceAnalyzer:
    def __init__(self, config: PerformanceConfig | None = None) -> None:
        self.config = config or PerformanceConfig()

    def compute_metrics(
        self,
        result: Any,
        *,
        attribution: Any | None = None,
        volatility: Any | None = None,
    ) -> PerformanceMetrics:
        equity_curve = _frame_from(result, "equity_curve")
        equity_curve = _normalize_trading_date(equity_curve)
        if equity_curve.empty:
            return PerformanceMetrics(summary=_empty_summary(), daily_returns=_empty_returns(), monthly_returns=_empty_returns())
        if "equity" not in equity_curve.columns:
            raise ValueError("equity_curve must contain an 'equity' column")

        equity_curve = equity_curve.sort_values("trading_date").reset_index(drop=True)
        equity = pd.to_numeric(equity_curve["equity"], errors="coerce")
        returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
        daily_returns = pd.DataFrame({"trading_date": equity_curve["trading_date"], "return": returns})
        monthly_returns = _monthly_returns(daily_returns)
        summary = self._core_summary(equity, returns)
        summary.update(_trade_summary(_frame_from(result, "trade_records", optional=True)))

        attribution_daily = _optional_attr_frame(attribution, "daily")
        if attribution_daily is not None and not attribution_daily.empty:
            summary.update(_attribution_summary(attribution_daily))

        volatility_frame = _optional_attr_frame(volatility, "frame")
        if volatility_frame is not None and not volatility_frame.empty:
            summary.update(_volatility_summary(volatility_frame))

        return PerformanceMetrics(
            summary=_sanitize_summary(summary),
            daily_returns=daily_returns,
            monthly_returns=monthly_returns,
        )

    def build_report(
        self,
        result: Any,
        output_dir: Path | str,
        *,
        attribution: Any | None = None,
        volatility: Any | None = None,
    ) -> PerformanceReport:
        from gamma_scalping.performance.visualizer import Visualizer

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        metrics = self.compute_metrics(result, attribution=attribution, volatility=volatility)
        paths = {
            "metrics": output_dir / "performance_metrics.csv",
            "daily_returns": output_dir / "daily_returns.csv",
            "monthly_returns": output_dir / "monthly_returns.csv",
            "report": output_dir / "performance_report.html",
        }
        pd.DataFrame([metrics.summary]).to_csv(paths["metrics"], index=False)
        metrics.daily_returns.to_csv(paths["daily_returns"], index=False)
        metrics.monthly_returns.to_csv(paths["monthly_returns"], index=False)

        visualizer = Visualizer()
        figure_paths: dict[str, Path] = {}
        equity_curve = _frame_from(result, "equity_curve")
        if not equity_curve.empty:
            figure_paths["equity_curve"] = visualizer.save(visualizer.plot_equity_curve(equity_curve), output_dir / "equity_curve.png")
            figure_paths["drawdown"] = visualizer.save(visualizer.plot_drawdown(equity_curve), output_dir / "drawdown.png")
        if volatility is not None:
            volatility_frame = _optional_attr_frame(volatility, "frame")
            if volatility_frame is not None and not volatility_frame.empty:
                figure_paths["volatility"] = visualizer.save(
                    visualizer.plot_volatility_series(volatility),
                    output_dir / "volatility_series.png",
                )
        if attribution is not None:
            attribution_daily = _optional_attr_frame(attribution, "daily")
            attribution_cumulative = _optional_attr_frame(attribution, "cumulative")
            if attribution_daily is not None and not attribution_daily.empty:
                figure_paths["greeks_attribution_daily"] = visualizer.save(
                    visualizer.plot_greeks_attribution(attribution),
                    output_dir / "greeks_attribution_daily.png",
                )
            if attribution_cumulative is not None and not attribution_cumulative.empty:
                figure_paths["greeks_attribution_cumulative"] = visualizer.save(
                    visualizer.plot_greeks_attribution_cumulative(attribution),
                    output_dir / "greeks_attribution_cumulative.png",
                )
        paths.update(figure_paths)
        paths["report"].write_text(_html_report(metrics, figure_paths), encoding="utf-8")
        return PerformanceReport(metrics=metrics, paths=paths)

    def _core_summary(self, equity: pd.Series, returns: pd.Series) -> dict[str, float]:
        initial_equity = float(equity.iloc[0])
        final_equity = float(equity.iloc[-1])
        cumulative_return = final_equity / initial_equity - 1.0 if initial_equity else 0.0
        periods = max(len(equity) - 1, 1)
        annual_return = (1.0 + cumulative_return) ** (self.config.annual_trading_days / periods) - 1.0
        daily_risk_free = self.config.risk_free_rate / self.config.annual_trading_days
        excess_returns = returns - daily_risk_free
        volatility = float(returns.std(ddof=0) * np.sqrt(self.config.annual_trading_days))
        downside = returns.where(returns < 0, 0.0)
        downside_dev = float(np.sqrt((downside**2).mean()))
        drawdown = equity / equity.cummax() - 1.0
        max_drawdown = float(drawdown.min())
        sharpe = float(excess_returns.mean() / returns.std(ddof=0) * np.sqrt(self.config.annual_trading_days)) if returns.std(ddof=0) else 0.0
        sortino = float(excess_returns.mean() / downside_dev * np.sqrt(self.config.annual_trading_days)) if downside_dev else 0.0
        calmar = annual_return / abs(max_drawdown) if max_drawdown else 0.0
        var = float(returns.quantile(self.config.var_level))
        tail = returns[returns <= var]
        cvar = float(tail.mean()) if not tail.empty else var
        return {
            "initial_equity": initial_equity,
            "final_equity": final_equity,
            "cumulative_return": cumulative_return,
            "annual_return": annual_return,
            "annual_volatility": volatility,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "var": var,
            "cvar": cvar,
            "observation_count": float(len(equity)),
        }


def _frame_from(source: Any, attr: str, *, optional: bool = False) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    frame = getattr(source, attr, None)
    if frame is None:
        if optional:
            return pd.DataFrame()
        raise ValueError(f"Missing required frame: {attr}")
    return frame.copy()


def _optional_attr_frame(source: Any | None, attr: str) -> pd.DataFrame | None:
    if source is None:
        return None
    if isinstance(source, pd.DataFrame):
        return source.copy()
    frame = getattr(source, attr, None)
    return None if frame is None else frame.copy()


def _normalize_trading_date(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if "trading_date" not in frame.columns and frame.index.name:
        frame = frame.reset_index().rename(columns={frame.index.name: "trading_date"})
    if "trading_date" in frame.columns:
        frame["trading_date"] = pd.to_datetime(frame["trading_date"])
    return frame


def _monthly_returns(daily_returns: pd.DataFrame) -> pd.DataFrame:
    if daily_returns.empty:
        return _empty_returns()
    frame = daily_returns.copy()
    frame["trading_date"] = pd.to_datetime(frame["trading_date"])
    monthly = (1.0 + frame.set_index("trading_date")["return"]).resample("ME").prod() - 1.0
    return monthly.reset_index().rename(columns={"return": "monthly_return"})


def _trade_summary(trade_records: pd.DataFrame) -> dict[str, float]:
    if trade_records.empty:
        return {"total_trade_amount": 0.0, "total_fee": 0.0, "trade_count": 0.0, "rebalance_count": 0.0}
    records = trade_records.copy()
    if "instrument_id" in records.columns:
        records = records[records["instrument_id"].fillna("").astype(str) != ""]
    if records.empty:
        return {"total_trade_amount": 0.0, "total_fee": 0.0, "trade_count": 0.0, "rebalance_count": 0.0}
    trade_amount = pd.to_numeric(records.get("trade_amount", 0.0), errors="coerce").fillna(0.0).abs()
    fee = pd.to_numeric(records.get("fee", 0.0), errors="coerce").fillna(0.0)
    return {
        "total_trade_amount": float(trade_amount.sum()),
        "total_fee": float(fee.sum()),
        "trade_count": float(len(records)),
        "rebalance_count": float(records["trading_date"].nunique()) if "trading_date" in records.columns else 0.0,
    }


def _attribution_summary(attribution_daily: pd.DataFrame) -> dict[str, float]:
    frame = attribution_daily.copy()
    summary: dict[str, float] = {}
    for column in ["delta", "gamma", "theta", "vega"]:
        exposure_column = f"option_{column}_exposure"
        if exposure_column in frame.columns:
            summary[f"avg_{column}_exposure"] = float(pd.to_numeric(frame[exposure_column], errors="coerce").mean())
    if "gamma_theta_pnl" in frame.columns:
        summary["total_gamma_theta_pnl"] = float(pd.to_numeric(frame["gamma_theta_pnl"], errors="coerce").fillna(0.0).sum())
    if "vega_pnl" in frame.columns:
        summary["total_vega_pnl"] = float(pd.to_numeric(frame["vega_pnl"], errors="coerce").fillna(0.0).sum())
    if "residual_ratio" in frame.columns:
        summary["avg_residual_ratio"] = float(pd.to_numeric(frame["residual_ratio"], errors="coerce").replace([np.inf, -np.inf], np.nan).mean())
    return summary


def _volatility_summary(volatility_frame: pd.DataFrame) -> dict[str, float]:
    frame = volatility_frame.copy()
    summary: dict[str, float] = {}
    for column in ["atm_iv", "hv_20", "iv_hv_spread", "hv_iv_edge"]:
        if column in frame.columns:
            summary[f"avg_{column}"] = float(pd.to_numeric(frame[column], errors="coerce").mean())
    if "iv_failed_count" in frame.columns:
        summary["total_iv_failed_count"] = float(pd.to_numeric(frame["iv_failed_count"], errors="coerce").fillna(0.0).sum())
    return summary


def _sanitize_summary(summary: dict[str, float]) -> dict[str, float]:
    sanitized = {}
    for key, value in summary.items():
        value = float(value) if value is not None else 0.0
        sanitized[key] = 0.0 if np.isnan(value) or np.isinf(value) else value
    return sanitized


def _empty_summary() -> dict[str, float]:
    return {
        "initial_equity": 0.0,
        "final_equity": 0.0,
        "cumulative_return": 0.0,
        "annual_return": 0.0,
        "annual_volatility": 0.0,
        "max_drawdown": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "calmar_ratio": 0.0,
        "var": 0.0,
        "cvar": 0.0,
        "observation_count": 0.0,
    }


def _empty_returns() -> pd.DataFrame:
    return pd.DataFrame(columns=["trading_date", "return"])


def _html_report(metrics: PerformanceMetrics, figure_paths: dict[str, Path]) -> str:
    rows = "\n".join(
        f"<tr><th>{key}</th><td>{value:.6g}</td></tr>" for key, value in sorted(metrics.summary.items())
    )
    figures = "\n".join(
        f'<section><h2>{name}</h2><img src="{path.name}" alt="{name}"></section>' for name, path in figure_paths.items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Performance Report</title>
  <style>
    body {{ font-family: sans-serif; margin: 24px; line-height: 1.5; }}
    table {{ border-collapse: collapse; margin-bottom: 24px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
    th {{ text-align: left; }}
    img {{ max-width: 100%; height: auto; display: block; margin: 12px 0 24px; }}
  </style>
</head>
<body>
  <h1>Performance Report</h1>
  <table>
    <tbody>
{rows}
    </tbody>
  </table>
{figures}
</body>
</html>
"""
