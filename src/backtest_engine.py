"""
Historical backtesting engine for Upbit trading bot.
Replaces random simulation with real historical data simulation.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .execution import ExecutionResult
from .portfolio import Portfolio, Position
from .risk import RiskManager
from .signal_engine import build_signal
from .storage import Storage
from .universe import select_universe
from .utils import now_ms, parse_market

LOGGER = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    start_date: datetime
    end_date: datetime
    initial_equity: float
    commission_rates: Dict[str, float]
    slippage_model: str
    default_slippage: float


@dataclass 
class BacktestResult:
    total_trades: int
    win_rate: float
    net_pnl_pct: float
    max_drawdown_pct: float
    avg_hold_seconds: float
    avg_slippage_pct: float
    sharpe_ratio: float
    profit_factor: float
    per_market_stats: Dict[str, Dict]
    equity_curve: List[float]
    daily_returns: List[float]


class HistoricalDataLoader:
    """Load and manage historical candle data for backtesting."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
        self.candles_cache: Dict[str, List[Dict]] = {}
        
    def load_candles(self, market: str, start: datetime, end: datetime) -> List[Dict]:
        """Load 1-minute candles for a specific market."""
        cache_key = f"{market}_{start.date()}_{end.date()}"
        
        if cache_key not in self.candles_cache:
            candles = self._load_from_storage(market, start, end)
            self.candles_cache[cache_key] = candles
            
        return self.candles_cache[cache_key]
    
    def _load_from_storage(self, market: str, start: datetime, end: datetime) -> List[Dict]:
        """Load candles from local storage."""
        try:
            query = """
                SELECT * FROM candles 
                WHERE market = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp
            """
            rows = self.storage.query(query, (market, start.timestamp(), end.timestamp()))
            return [dict(row) for row in rows]
        except Exception as e:
            LOGGER.warning(f"Failed to load candles from storage: {e}")
            return []


class SlippageSimulator:
    """Simulate realistic slippage based on market conditions."""
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        
    def calculate_slippage(self, market: str, side: str, amount: float, 
                         orderbook: Dict, notional_ratio: float) -> float:
        """Calculate realistic slippage based on market conditions."""
        if self.config.slippage_model == "fixed":
            return self.config.default_slippage
            
        elif self.config.slippage_model == "percentage":
            base_slippage = self.config.default_slippage
            volume_multiplier = min(1.5, 1.0 + notional_ratio * 0.1)
            side_multiplier = 1.2 if side == "SELL" else 1.0
            return base_slippage * volume_multiplier * side_multiplier
            
        elif self.config.slippage_model == "volume_based":
            return self._calculate_volume_based_slippage(orderbook, amount, side)
        
        return self.config.default_slippage
    
    def _calculate_volume_based_slippage(self, orderbook: Dict, amount: float, side: str) -> float:
        """Calculate slippage based on orderbook depth."""
        units = orderbook.get("orderbook_units", [])
        if not units:
            return self.config.default_slippage
            
        filled_amount = 0
        total_value = 0
        
        if side == "BUY":
            for unit in units[:5]:
                ask_price = float(unit.get("ask_price", 0))
                ask_size = float(unit.get("ask_size", 0))
                
                needed = amount - filled_amount
                fill_at_level = min(needed, ask_size)
                
                total_value += fill_at_level * ask_price
                filled_amount += fill_at_level
                
                if filled_amount >= amount:
                    break
        else:
            for unit in units[:5]:
                bid_price = float(unit.get("bid_price", 0))
                bid_size = float(unit.get("bid_size", 0))
                
                needed = amount - filled_amount
                fill_at_level = min(needed, bid_size)
                
                total_value += fill_at_level * bid_price
                filled_amount += fill_at_level
                
                if filled_amount >= amount:
                    break
        
        if filled_amount > 0:
            avg_fill_price = total_value / filled_amount
            mid_price = (float(units[0].get("bid_price", 0)) + 
                        float(units[0].get("ask_price", 0))) / 2
            
            slippage = abs(avg_fill_price - mid_price) / mid_price
            return min(slippage, 0.01)
        
        return self.config.default_slippage


class BacktestEngine:
    """Main backtesting engine with realistic simulation."""
    
    def __init__(self, cfg: dict, storage: Storage, data_loader: HistoricalDataLoader):
        self.cfg = cfg
        self.storage = storage
        self.data_loader = data_loader
        
        self.portfolio = Portfolio(cfg)
        initial_equity = float(cfg["runtime"]["paper_initial_equity_krw"])
        self.risk_manager = RiskManager(cfg, initial_equity)
        
        self.backtest_config = BacktestConfig(
            start_date=datetime.now() - timedelta(days=cfg["runtime"]["backtest_days"]),
            end_date=datetime.now(),
            initial_equity=initial_equity,
            commission_rates=cfg["fees"],
            slippage_model="volume_based",
            default_slippage=cfg["runtime"]["backtest_default_slippage"]
        )
        
        self.slippage_simulator = SlippageSimulator(self.backtest_config)
        self.equity_curve = [initial_equity]
        self.all_trades = []
        self.daily_pnl = []
        
    async def run_backtest(self) -> BacktestResult:
        """Run complete backtest simulation."""
        LOGGER.info(f"Starting backtest from {self.backtest_config.start_date} to {self.backtest_config.end_date}")
        
        current_time = self.backtest_config.start_date
        time_step = timedelta(minutes=1)
        
        while current_time <= self.backtest_config.end_date:
            await self._simulate_minute(current_time)
            current_time += time_step
            
            if current_time.minute == 0:
                current_equity = self._calculate_current_equity()
                self.equity_curve.append(current_equity)
        
        return self._generate_results()
    
    async def _simulate_minute(self, current_time: datetime):
        """Simulate one minute of trading."""
        await self._process_exits(current_time)
        universe = await self._select_universe(current_time)
        signals = await self._generate_signals(universe, current_time)
        await self._process_entries(signals, current_time)
        self.risk_manager.update_daily_pnl(self._calculate_unrealized_pnl())
    
    def _generate_results(self) -> BacktestResult:
        """Generate comprehensive backtest results."""
        if not self.all_trades:
            return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, {}, [], [])
        
        trades_df = pd.DataFrame(self.all_trades)
        
        total_trades = len(trades_df)
        winning_trades = len(trades_df[trades_df['pnl'] > 0])
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        total_pnl = trades_df['pnl'].sum()
        initial_equity = self.backtest_config.initial_equity
        net_pnl_pct = total_pnl / initial_equity
        
        avg_hold_seconds = trades_df['hold_seconds'].mean()
        avg_slippage_pct = trades_df['slippage'].mean()
        
        equity_curve = np.array(self.equity_curve)
        returns = np.diff(equity_curve) / equity_curve[:-1]
        max_drawdown = self._calculate_max_drawdown(equity_curve)
        
        sharpe_ratio = self._calculate_sharpe_ratio(returns)
        profit_factor = self._calculate_profit_factor(trades_df)
        
        per_market_stats = {}
        for market, group in trades_df.groupby('market'):
            per_market_stats[market] = {
                'trades': len(group),
                'pnl': group['pnl'].sum() / initial_equity,
                'win_rate': len(group[group['pnl'] > 0]) / len(group)
            }
        
        return BacktestResult(
            total_trades=total_trades,
            win_rate=win_rate,
            net_pnl_pct=net_pnl_pct,
            max_drawdown_pct=max_drawdown,
            avg_hold_seconds=avg_hold_seconds,
            avg_slippage_pct=avg_slippage_pct,
            sharpe_ratio=sharpe_ratio,
            profit_factor=profit_factor,
            per_market_stats=per_market_stats,
            equity_curve=self.equity_curve,
            daily_returns=returns.tolist()
        )
    
    def _calculate_max_drawdown(self, equity_curve: np.ndarray) -> float:
        peak = np.maximum.accumulate(equity_curve)
        drawdown = (peak - equity_curve) / peak
        return np.max(drawdown) if len(drawdown) > 0 else 0
    
    def _calculate_sharpe_ratio(self, returns: np.ndarray, risk_free_rate: float = 0.02) -> float:
        if len(returns) < 2:
            return 0
        excess_returns = returns - risk_free_rate / (365 * 24 * 60)
        return np.mean(excess_returns) / np.std(excess_returns) if np.std(excess_returns) > 0 else 0
    
    def _calculate_profit_factor(self, trades_df: pd.DataFrame) -> float:
        gross_profit = trades_df[trades_df['pnl'] > 0]['pnl'].sum()
        gross_loss = abs(trades_df[trades_df['pnl'] < 0]['pnl'].sum())
        return gross_profit / gross_loss if gross_loss > 0 else float('inf')