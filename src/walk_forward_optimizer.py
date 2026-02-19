"""
Walk-Forward Research Loop for Upbit Trading Bot.

This module implements walk-forward optimization to prevent overfitting and ensure
the trading strategy generalizes well to unseen data.

Key concepts:
- In-Sample (IS): Training period where parameters are optimized
- Out-of-Sample (OOS): Testing period where optimized parameters are validated
- Walk-Forward: Rolling window that moves forward in time
"""

import asyncio
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Callable

from .backtest_engine import BacktestEngine, HistoricalDataLoader, BacktestResult
from .storage import Storage

LOGGER = logging.getLogger(__name__)


@dataclass
class ParameterSpace:
    """Defines the parameter space for optimization."""

    # Signal parameters
    score_cutoff_min: Tuple[float, float] = (60, 85)
    score_cutoff_max: Tuple[float, float] = (70, 95)
    ema_weight: Tuple[float, float] = (15, 45)
    volume_weight: Tuple[float, float] = (10, 30)
    breakout_weight: Tuple[float, float] = (10, 30)

    # Risk parameters
    risk_per_trade: Tuple[float, float] = (0.001, 0.003)
    max_positions: Tuple[int, int] = (5, 15)
    stop_pct_min: Tuple[float, float] = (0.005, 0.015)
    stop_pct_max: Tuple[float, float] = (0.015, 0.030)

    # Regime parameters
    adx_threshold: Tuple[float, float] = (15, 30)
    volatility_low: Tuple[float, float] = (0.4, 0.8)
    volatility_high: Tuple[float, float] = (1.2, 2.0)


@dataclass
class WalkForwardResult:
    """Results from a single walk-forward iteration."""

    iteration: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime

    # Optimized parameters (from IS)
    best_params: Dict

    # IS performance
    is_total_trades: int
    is_win_rate: float
    is_net_pnl_pct: float
    is_sharpe_ratio: float
    is_max_drawdown: float

    # OOS performance
    oos_total_trades: int
    oos_win_rate: float
    oos_net_pnl_pct: float
    oos_sharpe_ratio: float
    oos_max_drawdown: float

    # Quality metrics
    is_oos_ratio: float  # Ratio of IS to OOS performance
    degradation_pct: float  # How much OOS degraded from IS


@dataclass
class WalkForwardReport:
    """Comprehensive walk-forward optimization report."""

    total_iterations: int

    # Aggregated metrics
    avg_is_win_rate: float
    avg_oos_win_rate: float
    avg_is_pnl: float
    avg_oos_pnl: float
    avg_degradation: float

    # Consistency metrics
    oos_positive_count: int  # How many OOS periods were profitable
    consistency_ratio: float  # oos_positive_count / total_iterations

    # Best/worst iterations
    best_iteration: Optional[WalkForwardResult]
    worst_iteration: Optional[WalkForwardResult]

    # Parameter stability
    param_stability: Dict[str, float]  # Coefficient of variation for each param

    # Recommendation
    recommended_params: Dict
    confidence: str  # "high", "medium", "low"


class WalkForwardOptimizer:
    """
    Walk-Forward Optimization Engine.

    Implements rolling window optimization to find robust parameters
    that perform well on unseen data.
    """

    def __init__(
        self,
        cfg: dict,
        storage: Storage,
        data_loader: HistoricalDataLoader,
        param_space: Optional[ParameterSpace] = None,
    ):
        self.cfg = cfg
        self.storage = storage
        self.data_loader = data_loader
        self.param_space = param_space or ParameterSpace()

        # Default walk-forward configuration
        self.is_days = cfg.get("walk_forward", {}).get("in_sample_days", 30)
        self.oos_days = cfg.get("walk_forward", {}).get("out_of_sample_days", 7)
        self.step_days = cfg.get("walk_forward", {}).get("step_days", 7)
        self.min_oos_trades = cfg.get("walk_forward", {}).get("min_oos_trades", 5)

        # Optimization settings
        self.max_iterations = cfg.get("walk_forward", {}).get("max_iterations", 50)
        self.n_jobs = cfg.get("walk_forward", {}).get("n_jobs", 4)

    def _sample_params(self) -> Dict:
        """Generate a random parameter set from the parameter space."""
        return {
            # Signal
            "score_cutoff_min": random.uniform(*self.param_space.score_cutoff_min),
            "score_cutoff_max": random.uniform(*self.param_space.score_cutoff_max),
            "ema_weight": random.uniform(*self.param_space.ema_weight),
            "volume_weight": random.uniform(*self.param_space.volume_weight),
            "breakout_weight": random.uniform(*self.param_space.breakout_weight),
            # Risk
            "risk_per_trade": random.uniform(*self.param_space.risk_per_trade),
            "max_positions": random.randint(*self.param_space.max_positions),
            "stop_pct_min": random.uniform(*self.param_space.stop_pct_min),
            "stop_pct_max": random.uniform(*self.param_space.stop_pct_max),
            # Regime
            "adx_threshold": random.uniform(*self.param_space.adx_threshold),
            "volatility_low": random.uniform(*self.param_space.volatility_low),
            "volatility_high": random.uniform(*self.param_space.volatility_high),
        }

    def _apply_params(self, params: Dict, cfg: dict) -> dict:
        """Apply parameters to config for backtesting."""
        cfg = cfg.copy()

        # Apply signal params
        if "signal" not in cfg:
            cfg["signal"] = {}
        cfg["signal"]["score_cutoff"] = params.get("score_cutoff_min", 75)

        # Apply risk params
        if "risk" not in cfg:
            cfg["risk"] = {}
        cfg["risk"]["risk_per_trade_target"] = params.get("risk_per_trade", 0.002)
        cfg["risk"]["max_positions"] = params.get("max_positions", 10)

        # Apply stop params
        if "stops" not in cfg:
            cfg["stops"] = {}
        cfg["stops"]["stop_pct_min"] = params.get("stop_pct_min", 0.008)
        cfg["stops"]["stop_pct_max"] = params.get("stop_pct_max", 0.020)

        # Apply regime params
        if "regime" not in cfg:
            cfg["regime"] = {}
        cfg["regime"]["regime_lookback_period"] = params.get("adx_threshold", 20)

        return cfg

    async def _run_backtest_for_params(
        self, params: Dict, start: datetime, end: datetime
    ) -> Optional[BacktestResult]:
        """Run backtest with given parameters."""
        try:
            test_cfg = self._apply_params(params, self.cfg.copy())
            engine = BacktestEngine(test_cfg, self.storage, self.data_loader)

            # Override dates for this specific test
            engine.backtest_config.start_date = start
            engine.backtest_config.end_date = end

            result = await engine.run_backtest()
            return result
        except Exception as e:
            LOGGER.warning(f"Backtest failed for params: {e}")
            return None

    def _evaluate_result(self, result: BacktestResult) -> float:
        """
        Calculate fitness score for a backtest result.

        Objective: Maximize Sharpe ratio while penalizing overfitting.
        """
        if result.total_trades < 5:
            return -999  # Penalize low trade count

        # Primary: Sharpe ratio (annualized)
        sharpe = result.sharpe_ratio if result.sharpe_ratio > 0 else 0

        # Secondary: Win rate consistency
        win_rate = result.win_rate

        # Penalty: Excessive drawdown
        dd_penalty = max(0, result.max_drawdown_pct - 0.15) * 10

        # Penalty: Too few trades (overfitting risk)
        trade_penalty = max(0, 10 - result.total_trades) * 0.1

        return sharpe * win_rate - dd_penalty - trade_penalty

    async def _optimize_is_period(
        self, is_start: datetime, is_end: datetime, n_iterations: Optional[int] = None
    ) -> Tuple[Optional[Dict], Optional[BacktestResult]]:
        """
        Optimize parameters on in-sample period.

        Uses random search for parameter optimization.
        """
        n_iter = n_iterations or self.max_iterations

        best_params: Optional[Dict] = None
        best_score = float("-inf")
        best_result: Optional[BacktestResult] = None

        for i in range(n_iter):
            params = self._sample_params()
            result = await self._run_backtest_for_params(params, is_start, is_end)

            if result is None:
                continue

            score = self._evaluate_result(result)

            if score > best_score:
                best_score = score
                best_params = params
                best_result = result

            # Early stopping if we found a really good result
            if score > 5.0 and i >= 10:
                break

        return best_params, best_result

    async def run_walk_forward(
        self,
        data_start: datetime,
        data_end: datetime,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> WalkForwardReport:
        """
        Run complete walk-forward optimization.

        Args:
            data_start: Start of available historical data
            data_end: End of available historical data
            progress_callback: Optional callback for progress updates

        Returns:
            WalkForwardReport with comprehensive analysis
        """
        LOGGER.info(
            f"Starting walk-forward optimization: {self.is_days} days IS, "
            f"{self.oos_days} days OOS, {self.step_days} day step"
        )

        results: List[WalkForwardResult] = []

        # Calculate walk-forward windows
        current_is_start = data_start
        iteration = 0

        while True:
            is_end = current_is_start + timedelta(days=self.is_days)
            oos_start = is_end
            oos_end = oos_start + timedelta(days=self.oos_days)

            # Check if we have enough data
            if oos_end > data_end:
                LOGGER.info(
                    f"OOS end ({oos_end}) exceeds data end ({data_end}), stopping"
                )
                break

            iteration += 1
            LOGGER.info(
                f"Iteration {iteration}: IS={current_is_start.date()} to {is_end.date()}, "
                f"OOS={oos_start.date()} to {oos_end.date()}"
            )

            # Progress callback
            if progress_callback:
                progress_callback(iteration, -1)  # -1 = running

            # Optimize on IS period
            best_params, is_result = await self._optimize_is_period(
                current_is_start, is_end
            )

            if best_params is None or is_result is None:
                LOGGER.warning(f"Iteration {iteration}: No valid IS result, skipping")
                current_is_start += timedelta(days=self.step_days)
                continue

            # Validate on OOS period
            oos_result = await self._run_backtest_for_params(
                best_params, oos_start, oos_end
            )

            if oos_result is None or oos_result.total_trades < self.min_oos_trades:
                LOGGER.warning(
                    f"Iteration {iteration}: OOS trades ({oos_result.total_trades if oos_result else 0}) "
                    f"below minimum ({self.min_oos_trades}), skipping"
                )
                current_is_start += timedelta(days=self.step_days)
                continue

            # Calculate degradation
            is_pnl = is_result.net_pnl_pct
            oos_pnl = oos_result.net_pnl_pct
            degradation = 0 if is_pnl == 0 else ((is_pnl - oos_pnl) / abs(is_pnl)) * 100

            wf_result = WalkForwardResult(
                iteration=iteration,
                is_start=current_is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
                best_params=best_params,
                is_total_trades=is_result.total_trades,
                is_win_rate=is_result.win_rate,
                is_net_pnl_pct=is_result.net_pnl_pct,
                is_sharpe_ratio=is_result.sharpe_ratio,
                is_max_drawdown=is_result.max_drawdown_pct,
                oos_total_trades=oos_result.total_trades,
                oos_win_rate=oos_result.win_rate,
                oos_net_pnl_pct=oos_result.net_pnl_pct,
                oos_sharpe_ratio=oos_result.sharpe_ratio,
                oos_max_drawdown=oos_result.max_drawdown_pct,
                is_oos_ratio=is_result.net_pnl_pct
                / max(oos_result.net_pnl_pct, 0.0001),
                degradation_pct=degradation,
            )

            results.append(wf_result)

            LOGGER.info(
                f"  IS: trades={is_result.total_trades}, win_rate={is_result.win_rate:.1%}, "
                f"pnl={is_result.net_pnl_pct:.2%} | "
                f"OOS: trades={oos_result.total_trades}, win_rate={oos_result.win_rate:.1%}, "
                f"pnl={oos_result.net_pnl_pct:.2%}"
            )

            # Move forward
            current_is_start += timedelta(days=self.step_days)

        # Generate comprehensive report
        report = self._generate_report(results)

        LOGGER.info(
            f"Walk-forward complete: {report.total_iterations} iterations, "
            f"consistency={report.consistency_ratio:.1%}, "
            f"confidence={report.confidence}"
        )

        return report

    def _generate_report(self, results: List[WalkForwardResult]) -> WalkForwardReport:
        """Generate comprehensive analysis report."""

        if not results:
            return WalkForwardReport(
                total_iterations=0,
                avg_is_win_rate=0,
                avg_oos_win_rate=0,
                avg_is_pnl=0,
                avg_oos_pnl=0,
                avg_degradation=0,
                oos_positive_count=0,
                consistency_ratio=0,
                best_iteration=None,
                worst_iteration=None,
                param_stability={},
                recommended_params={},
                confidence="low",
            )

        # Calculate aggregated metrics
        avg_is_win_rate = sum(r.is_win_rate for r in results) / len(results)
        avg_oos_win_rate = sum(r.oos_win_rate for r in results) / len(results)
        avg_is_pnl = sum(r.is_net_pnl_pct for r in results) / len(results)
        avg_oos_pnl = sum(r.oos_net_pnl_pct for r in results) / len(results)
        avg_degradation = sum(r.degradation_pct for r in results) / len(results)

        # Consistency
        oos_positive_count = sum(1 for r in results if r.oos_net_pnl_pct > 0)
        consistency_ratio = oos_positive_count / len(results)

        # Best/worst
        best_iteration = max(results, key=lambda r: r.oos_net_pnl_pct)
        worst_iteration = min(results, key=lambda r: r.oos_net_pnl_pct)

        # Parameter stability (coefficient of variation)
        param_values: Dict[str, List[float]] = {}
        for r in results:
            for k, v in r.best_params.items():
                if isinstance(v, (int, float)):
                    param_values.setdefault(k, []).append(v)

        param_stability = {}
        for k, vals in param_values.items():
            if len(vals) > 1:
                mean_val = sum(vals) / len(vals)
                if mean_val != 0:
                    variance = sum((x - mean_val) ** 2 for x in vals) / len(vals)
                    std_val = math.sqrt(variance)
                    cv = std_val / abs(mean_val)
                    param_stability[k] = cv

        # Recommended params (median of optimized values)
        recommended_params = {}
        for k, vals in param_values.items():
            sorted_vals = sorted(vals)
            n = len(sorted_vals)
            if n % 2 == 0:
                median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
            else:
                median = sorted_vals[n // 2]
            recommended_params[k] = float(median)

        # Confidence level
        if consistency_ratio >= 0.8 and avg_degradation < 30:
            confidence = "high"
        elif consistency_ratio >= 0.6 and avg_degradation < 50:
            confidence = "medium"
        else:
            confidence = "low"

        return WalkForwardReport(
            total_iterations=len(results),
            avg_is_win_rate=avg_is_win_rate,
            avg_oos_win_rate=avg_oos_win_rate,
            avg_is_pnl=avg_is_pnl,
            avg_oos_pnl=avg_oos_pnl,
            avg_degradation=avg_degradation,
            oos_positive_count=oos_positive_count,
            consistency_ratio=consistency_ratio,
            best_iteration=best_iteration,
            worst_iteration=worst_iteration,
            param_stability=param_stability,
            recommended_params=recommended_params,
            confidence=confidence,
        )

    def print_report(self, report: WalkForwardReport) -> None:
        """Print formatted walk-forward report."""
        print("\n" + "=" * 60)
        print("WALK-FORWARD OPTIMIZATION REPORT")
        print("=" * 60)

        print(f"\nIterations: {report.total_iterations}")
        print(f"Confidence: {report.confidence.upper()}")

        print("\n--- Performance Metrics ---")
        print(f"Avg IS Win Rate:     {report.avg_is_win_rate:.1%}")
        print(f"Avg OOS Win Rate:    {report.avg_oos_win_rate:.1%}")
        print(f"Avg IS PnL:          {report.avg_is_pnl:.2%}")
        print(f"Avg OOS PnL:         {report.avg_oos_pnl:.2%}")
        print(f"Avg Degradation:     {report.avg_degradation:.1f}%")

        print("\n--- Consistency ---")
        print(
            f"OOS Positive:        {report.oos_positive_count}/{report.total_iterations}"
        )
        print(f"Consistency Ratio:   {report.consistency_ratio:.1%}")

        if report.best_iteration:
            print("\n--- Best Iteration ---")
            print(f"  Iteration:         {report.best_iteration.iteration}")
            print(f"  OOS Win Rate:      {report.best_iteration.oos_win_rate:.1%}")
            print(f"  OOS PnL:           {report.best_iteration.oos_net_pnl_pct:.2%}")

        if report.worst_iteration:
            print("\n--- Worst Iteration ---")
            print(f"  Iteration:         {report.worst_iteration.iteration}")
            print(f"  OOS Win Rate:      {report.worst_iteration.oos_win_rate:.1%}")
            print(f"  OOS PnL:           {report.worst_iteration.oos_net_pnl_pct:.2%}")

        print("\n--- Recommended Parameters ---")
        for k, v in report.recommended_params.items():
            print(f"  {k}: {v}")

        print("\n--- Parameter Stability (CV) ---")
        for k, cv in sorted(report.param_stability.items(), key=lambda x: x[1]):
            stability = "stable" if cv < 0.3 else "moderate" if cv < 0.5 else "unstable"
            print(f"  {k}: {cv:.2f} ({stability})")

        print("\n" + "=" * 60)


async def run_walk_forward_from_cli(cfg: dict, storage: Storage):
    """Run walk-forward optimization from CLI."""
    from datetime import datetime, timedelta

    # Get historical data range
    backtest_days = cfg.get("runtime", {}).get("backtest_days", 30)
    data_end = datetime.now()
    data_start = data_end - timedelta(days=backtest_days * 2)  # Need 2x for IS + OOS

    # Initialize
    data_loader = HistoricalDataLoader(storage)
    optimizer = WalkForwardOptimizer(cfg, storage, data_loader)

    # Run optimization
    report = await optimizer.run_walk_forward(data_start, data_end)

    # Print results
    optimizer.print_report(report)

    return report
