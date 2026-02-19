"""
Institutional-Grade Performance Analytics Module.

This module provides comprehensive risk-adjusted performance metrics including:
- VaR (Value at Risk) calculation
- Sortino ratio
- Calmar ratio
- Advanced portfolio analytics

Designed for professional institutional investor requirements.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


@dataclass
class PerformanceMetrics:
    """Comprehensive performance metrics for institutional investors."""

    # Basic metrics
    total_trades: int = 0
    win_rate: float = 0.0
    net_pnl_pct: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0

    # Risk metrics
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: int = 0

    # Institutional metrics
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    # VaR metrics
    var_95: float = 0.0
    var_99: float = 0.0
    cvar_95: float = 0.0
    cvar_99: float = 0.0

    # Advanced metrics
    skewness: float = 0.0
    kurtosis: float = 0.0
    win_loss_avg_ratio: float = 0.0

    # Time-based metrics
    avg_hold_time_seconds: float = 0.0
    avg_trades_per_day: float = 0.0

    # Efficiency metrics
    expectancy: float = 0.0
    edge: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "net_pnl_pct": self.net_pnl_pct,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "profit_factor": self.profit_factor,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration_days": self.max_drawdown_duration_days,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "var_95": self.var_95,
            "var_99": self.var_99,
            "cvar_95": self.cvar_95,
            "cvar_99": self.cvar_99,
            "skewness": self.skewness,
            "kurtosis": self.kurtosis,
            "win_loss_avg_ratio": self.win_loss_avg_ratio,
            "avg_hold_time_seconds": self.avg_hold_time_seconds,
            "avg_trades_per_day": self.avg_trades_per_day,
            "expectancy": self.expectancy,
            "edge": self.edge,
        }


class PerformanceAnalyzer:
    """Institutional-grade performance analyzer."""

    def __init__(self, initial_equity: float = 10_000_000):
        self.initial_equity = initial_equity
        self.equity_curve: List[float] = [initial_equity]
        self.daily_returns: List[float] = []
        self.trade_returns: List[float] = []
        self.trade_dates: List[datetime] = []

    def add_trade(
        self, pnl_pct: float, entry_time: datetime, exit_time: datetime
    ) -> None:
        self.trade_returns.append(pnl_pct)
        self.trade_dates.append(entry_time)

        current_equity = self.equity_curve[-1] * (1 + pnl_pct)
        self.equity_curve.append(current_equity)

        if len(self.trade_dates) >= 2:
            days = (self.trade_dates[-1] - self.trade_dates[0]).days
            if days > 0:
                self.avg_trades_per_day = len(self.trade_dates) / days

    def add_daily_return(self, daily_return: float) -> None:
        self.daily_returns.append(daily_return)

    def calculate_all_metrics(self) -> PerformanceMetrics:
        if not self.trade_returns:
            return PerformanceMetrics()

        metrics = PerformanceMetrics()

        metrics.total_trades = len(self.trade_returns)

        wins = [r for r in self.trade_returns if r > 0]
        losses = [r for r in self.trade_returns if r <= 0]

        metrics.win_rate = (
            len(wins) / len(self.trade_returns) if self.trade_returns else 0
        )
        metrics.gross_profit = sum(wins) if wins else 0
        metrics.gross_loss = abs(sum(losses)) if losses else 0
        metrics.profit_factor = (
            metrics.gross_profit / metrics.gross_loss
            if metrics.gross_loss > 0
            else float("inf")
        )
        metrics.net_pnl_pct = sum(self.trade_returns)

        metrics.max_drawdown_pct, metrics.max_drawdown_duration_days = (
            self._calculate_max_drawdown()
        )

        metrics.sharpe_ratio = self._calculate_sharpe_ratio()
        metrics.sortino_ratio = self._calculate_sortino_ratio()
        metrics.calmar_ratio = self._calculate_calmar_ratio()

        metrics.var_95, metrics.cvar_95 = self._calculate_var_cvar(0.95)
        metrics.var_99, metrics.cvar_99 = self._calculate_var_cvar(0.99)

        metrics.skewness = self._calculate_skewness()
        metrics.kurtosis = self._calculate_kurtosis()

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        metrics.win_loss_avg_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        metrics.expectancy = (metrics.win_rate * avg_win) - (
            (1 - metrics.win_rate) * avg_loss
        )
        metrics.edge = metrics.expectancy * metrics.total_trades

        return metrics

    def _calculate_max_drawdown(self) -> Tuple[float, int]:
        if not self.equity_curve:
            return 0.0, 0

        peak = self.equity_curve[0]
        max_dd = 0.0
        max_dd_days = 0

        peak_date = self.trade_dates[0] if self.trade_dates else datetime.now()
        current_dd_start = peak_date

        for i, equity in enumerate(self.equity_curve):
            if equity > peak:
                peak = equity
                if max_dd_days > 0:
                    trade_date = (
                        self.trade_dates[i] if i < len(self.trade_dates) else peak_date
                    )
                    dd_days = (trade_date - current_dd_start).days
                    max_dd_days = max(max_dd_days, dd_days)
                current_dd_start = (
                    self.trade_dates[i] if i < len(self.trade_dates) else peak_date
                )

            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        return max_dd, max_dd_days

    def _calculate_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        if len(self.daily_returns) < 2:
            if not self.trade_returns:
                return 0.0

            mean_return = sum(self.trade_returns) / len(self.trade_returns)
            variance = sum((r - mean_return) ** 2 for r in self.trade_returns) / len(
                self.trade_returns
            )
            std_dev = math.sqrt(variance) if variance > 0 else 0

            if std_dev == 0:
                return 0.0

            annual_return = mean_return * 252
            annual_std = std_dev * math.sqrt(252)

            return (
                (annual_return - risk_free_rate) / annual_std if annual_std > 0 else 0
            )

        mean_return = sum(self.daily_returns) / len(self.daily_returns)
        variance = sum((r - mean_return) ** 2 for r in self.daily_returns) / len(
            self.daily_returns
        )
        std_dev = math.sqrt(variance) if variance > 0 else 0

        if std_dev == 0:
            return 0.0

        annual_return = mean_return * 252
        annual_std = std_dev * math.sqrt(252)

        return (annual_return - risk_free_rate) / annual_std if annual_std > 0 else 0

    def _calculate_sortino_ratio(self, target_return: float = 0.0) -> float:
        if not self.trade_returns:
            return 0.0

        mean_return = sum(self.trade_returns) / len(self.trade_returns)

        downside_returns = [
            r - target_return for r in self.trade_returns if r < target_return
        ]

        if not downside_returns:
            return float("inf") if mean_return > target_return else 0.0

        downside_variance = sum(r**2 for r in downside_returns) / len(downside_returns)
        downside_std = math.sqrt(downside_variance)

        if downside_std == 0:
            return 0.0

        annual_return = mean_return * 252
        annual_downside = downside_std * math.sqrt(252)

        return (
            (annual_return - target_return) / annual_downside
            if annual_downside > 0
            else 0
        )

    def _calculate_calmar_ratio(self) -> float:
        if not self.trade_returns:
            return 0.0

        mean_return = sum(self.trade_returns) / len(self.trade_returns)
        annual_return = mean_return * 252

        max_dd, _ = self._calculate_max_drawdown()

        if max_dd == 0:
            return 0.0

        return annual_return / max_dd

    def _calculate_var_cvar(self, confidence_level: float) -> Tuple[float, float]:
        if not self.trade_returns:
            return 0.0, 0.0

        sorted_returns = sorted(self.trade_returns)
        index = int((1 - confidence_level) * len(sorted_returns))
        index = min(index, len(sorted_returns) - 1)

        var = abs(sorted_returns[index])

        cvar_returns = sorted_returns[: index + 1]
        cvar = abs(sum(cvar_returns) / len(cvar_returns)) if cvar_returns else var

        return var, cvar

    def _calculate_skewness(self) -> float:
        if len(self.trade_returns) < 3:
            return 0.0

        n = len(self.trade_returns)
        mean = sum(self.trade_returns) / n

        std_dev = math.sqrt(sum((r - mean) ** 2 for r in self.trade_returns) / n)
        if std_dev == 0:
            return 0.0

        skewness = sum((r - mean) ** 3 for r in self.trade_returns) / (n * std_dev**3)

        return skewness

    def _calculate_kurtosis(self) -> float:
        if len(self.trade_returns) < 4:
            return 0.0

        n = len(self.trade_returns)
        mean = sum(self.trade_returns) / n

        std_dev = math.sqrt(sum((r - mean) ** 2 for r in self.trade_returns) / n)
        if std_dev == 0:
            return 0.0

        kurtosis = (
            sum((r - mean) ** 4 for r in self.trade_returns) / (n * std_dev**4) - 3
        )

        return kurtosis


class PortfolioAnalytics:
    """Advanced portfolio-level analytics for multi-position trading."""

    def __init__(self, initial_equity: float = 10_000_000):
        self.initial_equity = initial_equity
        self.per_market_returns: Dict[str, List[float]] = {}
        self.correlation_matrix: Dict[str, Dict[str, float]] = {}

    def add_market_trade(self, market: str, pnl_pct: float) -> None:
        if market not in self.per_market_returns:
            self.per_market_returns[market] = []
        self.per_market_returns[market].append(pnl_pct)

    def calculate_correlation_matrix(self) -> Dict[str, Dict[str, float]]:
        markets = list(self.per_market_returns.keys())

        for m1 in markets:
            self.correlation_matrix[m1] = {}
            for m2 in markets:
                if m1 == m2:
                    self.correlation_matrix[m1][m1] = 1.0
                elif m2 in self.correlation_matrix:
                    self.correlation_matrix[m1][m2] = self.correlation_matrix.get(
                        m2, {}
                    ).get(m1, 0.0)
                else:
                    corr = self._calculate_correlation(
                        self.per_market_returns[m1], self.per_market_returns[m2]
                    )
                    self.correlation_matrix[m1][m2] = corr

        return self.correlation_matrix

    def _calculate_correlation(
        self, returns1: List[float], returns2: List[float]
    ) -> float:
        if len(returns1) < 2 or len(returns2) < 2:
            return 0.0

        min_len = min(len(returns1), len(returns2))
        r1 = returns1[:min_len]
        r2 = returns2[:min_len]

        mean1 = sum(r1) / len(r1)
        mean2 = sum(r2) / len(r2)

        cov = sum((r1[i] - mean1) * (r2[i] - mean2) for i in range(min_len)) / min_len

        std1 = math.sqrt(sum((r - mean1) ** 2 for r in r1) / min_len)
        std2 = math.sqrt(sum((r - mean2) ** 2 for r in r2) / min_len)

        if std1 == 0 or std2 == 0:
            return 0.0

        return cov / (std1 * std2)

    def calculate_diversification_ratio(self) -> float:
        if not self.per_market_returns:
            return 0.0

        weighted_volatility = 0.0
        portfolio_volatility = 0.0

        total_trades = sum(len(returns) for returns in self.per_market_returns.values())

        for market, returns in self.per_market_returns.items():
            if not returns:
                continue

            weight = len(returns) / total_trades
            mean = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / len(returns)
            vol = math.sqrt(variance)

            weighted_volatility += weight * vol

        all_returns = []
        for returns in self.per_market_returns.values():
            all_returns.extend(returns)

        if all_returns:
            mean = sum(all_returns) / len(all_returns)
            variance = sum((r - mean) ** 2 for r in all_returns) / len(all_returns)
            portfolio_volatility = math.sqrt(variance)

        if portfolio_volatility == 0:
            return 0.0

        return weighted_volatility / portfolio_volatility

    def get_performance_by_market(self) -> Dict[str, Dict]:
        result = {}

        for market, returns in self.per_market_returns.items():
            if not returns:
                continue

            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]

            result[market] = {
                "trades": len(returns),
                "win_rate": len(wins) / len(returns),
                "avg_pnl": sum(returns) / len(returns),
                "gross_profit": sum(wins),
                "gross_loss": abs(sum(losses)),
                "best_trade": max(returns),
                "worst_trade": min(returns),
                "avg_win": sum(wins) / len(wins) if wins else 0,
                "avg_loss": abs(sum(losses) / len(losses)) if losses else 0,
            }

        return result


def calculate_risk_adjusted_metrics(
    trade_returns: List[float], initial_equity: float = 10_000_000
) -> PerformanceMetrics:
    """Convenience function to calculate all risk-adjusted metrics."""
    analyzer = PerformanceAnalyzer(initial_equity)

    for ret in trade_returns:
        analyzer.trade_returns.append(ret)

    return analyzer.calculate_all_metrics()
