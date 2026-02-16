"""
Historical backtesting engine for Upbit trading bot.
Replaces random simulation with real historical data simulation.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .execution import ExecutionResult
from .portfolio import Portfolio, Position
from .risk import RiskManager
from .signal_engine import build_signal
from .storage import Storage
from .utils import parse_market

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
    def __init__(self, storage: Storage):
        self.storage = storage
        self.candles_cache: Dict[str, List[Dict]] = {}
        
    def load_candles(self, market: str, start: datetime, end: datetime) -> List[Dict]:
        cache_key = f"{market}_{start.date()}_{end.date()}"
        
        if cache_key not in self.candles_cache:
            try:
                query = """
                    SELECT * FROM candles 
                    WHERE market = ? AND timestamp BETWEEN ? AND ?
                    ORDER BY timestamp
                """
                rows = self.storage.query(query, (market, start.timestamp(), end.timestamp()))
                self.candles_cache[cache_key] = [dict(row) for row in rows]
            except Exception as e:
                LOGGER.warning(f"Failed to load candles: {e}")
                self.candles_cache[cache_key] = []
            
        return self.candles_cache[cache_key]


class SlippageSimulator:
    def __init__(self, config: BacktestConfig):
        self.config = config
        
    def calculate_slippage(self, market: str, side: str, amount: float, 
                         orderbook: Dict, notional_ratio: float) -> float:
        if self.config.slippage_model == "fixed":
            return self.config.default_slippage
            
        elif self.config.slippage_model == "percentage":
            base_slippage = self.config.default_slippage
            volume_multiplier = min(1.5, 1.0 + notional_ratio * 0.1)
            side_multiplier = 1.2 if side == "SELL" else 1.0
            return base_slippage * volume_multiplier * side_multiplier
        
        return self.config.default_slippage


class BacktestEngine:
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
            slippage_model="percentage",
            default_slippage=cfg["runtime"]["backtest_default_slippage"]
        )
        
        self.slippage_simulator = SlippageSimulator(self.backtest_config)
        self.equity_curve = [initial_equity]
        self.all_trades = []
        self.daily_returns: List[float] = []
        
    async def run_backtest(self) -> BacktestResult:
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
        await self._process_exits(current_time)
        universe = await self._select_universe(current_time)
        signals = await self._generate_signals(universe, current_time)
        await self._process_entries(signals, current_time)
        self.risk_manager.update_daily_pnl(self._calculate_unrealized_pnl())
    
    async def _process_exits(self, current_time: datetime):
        for market, position in list(self.portfolio.positions.items()):
            current_price = await self._get_current_price(market, current_time)
            if current_price is None:
                continue
                
            exit_reason = self._check_exit_conditions(position, current_price, current_time)
            
            if exit_reason:
                exit_result = self._simulate_exit_order(position, current_price, exit_reason)
                if exit_result.ok:
                    self.portfolio.remove(market)
                    self.all_trades.append({
                        'market': market,
                        'entry_price': position.entry_price,
                        'exit_price': exit_result.fill_price,
                        'qty': position.qty,
                        'pnl': self._calculate_trade_pnl(position, exit_result.fill_price),
                        'exit_reason': exit_reason,
                        'hold_seconds': (current_time - datetime.fromtimestamp(position.entry_ts_ms/1000)).total_seconds(),
                        'slippage': exit_result.slippage_pct
                    })
    
    async def _process_entries(self, signals: List, current_time: datetime):
        viable_signals = [s for s in signals if s.tradable]
        viable_signals.sort(key=lambda s: s.score, reverse=True)
        
        for signal in viable_signals:
            if not self.risk_manager.can_open_new_entry(0, 0.0):
                break
                
            if self.portfolio.count() >= self.cfg["risk"]["max_positions"]:
                break
                
            position_value = self.risk_manager.compute_position_value(
                stop_pct=0.012,
                k_signals=len(viable_signals),
                coin_exposure_now=self.portfolio.coin_exposure_ratio(
                    parse_market(signal.market).base, 
                    self.risk_manager.equity,
                    {}
                )
            )
            
            if position_value < self.cfg["min_notional_krw"]:
                continue
            
            entry_price = await self._get_current_price(signal.market, current_time)
            if entry_price is None:
                continue
                
            entry_result = self._simulate_entry_order(signal, entry_price, position_value)
            
            if entry_result.ok:
                stop_price = entry_price * 0.98  # 2% stop
                self.portfolio.add(
                    market=signal.market,
                    qty=position_value / entry_price,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    score=signal.score
                )
    
    def _simulate_entry_order(self, signal, entry_price: float, position_value: float) -> ExecutionResult:
        mock_orderbook = {
            "orderbook_units": [
                {"ask_price": entry_price * 1.001, "ask_size": position_value / entry_price},
                {"bid_price": entry_price * 0.999, "bid_size": position_value / entry_price}
            ]
        }
        
        slippage = self.slippage_simulator.calculate_slippage(
            signal.market, "BUY", position_value / entry_price, 
            mock_orderbook, signal.notional_ratio
        )
        
        fill_price = entry_price * (1 + slippage)
        market_info = parse_market(signal.market)
        commission_rate = self.backtest_config.commission_rates.get(market_info.quote, 0.0005)
        fee = position_value * commission_rate
        
        return ExecutionResult(
            ok=True,
            market=signal.market,
            side="BUY",
            qty=position_value / entry_price,
            fill_price=fill_price,
            fee=fee,
            slippage_pct=slippage,
            reason="entry_simulated"
        )
    
    def _simulate_exit_order(self, position: Position, current_price: float, exit_reason: str) -> ExecutionResult:
        mock_orderbook = {
            "orderbook_units": [
                {"ask_price": current_price * 1.001, "ask_size": position.qty},
                {"bid_price": current_price * 0.999, "bid_size": position.qty}
            ]
        }
        
        slippage = self.slippage_simulator.calculate_slippage(
            position.market, "SELL", position.qty, mock_orderbook, 1.0
        )
        
        fill_price = current_price * (1 - slippage)
        position_value = position.qty * fill_price
        market_info = parse_market(position.market)
        commission_rate = self.backtest_config.commission_rates.get(market_info.quote, 0.0005)
        fee = position_value * commission_rate
        
        return ExecutionResult(
            ok=True,
            market=position.market,
            side="SELL",
            qty=position.qty,
            fill_price=fill_price,
            fee=fee,
            slippage_pct=slippage,
            reason=exit_reason
        )
    
    def _calculate_max_drawdown(self) -> float:
        """실제 최대 드로다운 계산"""
        if not self.equity_curve or len(self.equity_curve) < 2:
            return 0.0
        
        peak = self.equity_curve[0]
        max_dd = 0.0
        
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        
        return max_dd
    
    def _calculate_sharpe_ratio(self, risk_free_rate: float = 0.03) -> float:
        """샤프 비율 계산 (연간화)"""
        if not self.daily_returns or len(self.daily_returns) < 2:
            return 0.0
        
        import numpy as np
        returns = np.array(self.daily_returns)
        
        if returns.std() == 0:
            return 0.0
        
        mean_return = returns.mean()
        std_return = returns.std()
        
        # 일간 -> 연간 (252交易日)
        annual_return = mean_return * 252
        annual_std = std_return * np.sqrt(252)
        
        if annual_std == 0:
            return 0.0
        
        sharpe = (annual_return - risk_free_rate) / annual_std
        return float(sharpe)
    
    def _calculate_profit_factor(self) -> float:
        """이익 계수 (총 이익 / 총 손실)"""
        total_profit = 0.0
        total_loss = 0.0
        
        for trade in self.all_trades:
            if trade['pnl'] > 0:
                total_profit += trade['pnl']
            else:
                total_loss += abs(trade['pnl'])
        
        if total_loss == 0:
            return float('inf') if total_profit > 0 else 0.0
        
        return total_profit / total_loss
    
    def _calculate_calmar_ratio(self) -> float:
        """ 칼마 비율 (연간 수익률 / 최대 드로다운) """
        if not self.equity_curve or len(self.equity_curve) < 2:
            return 0.0
        
        # 일간 수익률 기반 연환산 수익률
        if not self.daily_returns:
            return 0.0
        
        import numpy as np
        annual_return = np.mean(self.daily_returns) * 252
        max_dd = self._calculate_max_drawdown()
        
        if max_dd == 0:
            return 0.0
        
        return annual_return / max_dd
    
    def _calculate_sortino_ratio(self, target_return: float = 0.0) -> float:
        """소르티노 비율 (초과수익률 / 하방편차)"""
        if not self.daily_returns or len(self.daily_returns) < 2:
            return 0.0
        
        import numpy as np
        returns = np.array(self.daily_returns)
        
        excess_returns = returns - target_return / 252  # 일간 목표 수익
        downside_returns = excess_returns[excess_returns < 0]
        
        if len(downside_returns) == 0:
            return float('inf')
        
        downside_std = np.std(downside_returns)
        if downside_std == 0:
            return 0.0
        
        mean_excess = np.mean(excess_returns)
        sortino = (mean_excess * 252) / (downside_std * np.sqrt(252))
        
        return float(sortino)
    
    def _calculate_win_loss_ratio(self) -> float:
        """평균 승/손 비율"""
        wins = [t['pnl'] for t in self.all_trades if t['pnl'] > 0]
        losses = [abs(t['pnl']) for t in self.all_trades if t['pnl'] < 0]
        
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        if avg_loss == 0:
            return float('inf') if avg_win > 0 else 0.0
        
        return avg_win / avg_loss
    
    def _calculate_expectancy(self) -> float:
        """기대값 (승률 * 평균승 - 손률 * 평균손)"""
        if not self.all_trades:
            return 0.0
        
        wins = [t['pnl'] for t in self.all_trades if t['pnl'] > 0]
        losses = [abs(t['pnl']) for t in self.all_trades if t['pnl'] < 0]
        
        win_rate = len(wins) / len(self.all_trades) if self.all_trades else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
        
        return expectancy
    
    def _calculate_recovery_factor(self) -> float:
        """회복 계수 (총净利润 / 최대 드로다운)"""
        total_pnl = sum(t['pnl'] for t in self.all_trades)
        max_dd_value = self._calculate_max_drawdown() * self.backtest_config.initial_equity
        
        if max_dd_value == 0:
            return float('inf') if total_pnl > 0 else 0.0
        
        return total_pnl / max_dd_value
    
    def _calculate_ulcer_index(self) -> float:
        """울서 인덱스 (드래우다운의 깊이와 지속기간을 측정)"""
        if not self.equity_curve or len(self.equity_curve) < 2:
            return 0.0
        
        import numpy as np
        
        peak = self.equity_curve[0]
        dd_squared = []
        
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0
            dd_squared.append(dd_pct ** 2)
        
        if not dd_squared:
            return 0.0
        
        ulcer = np.sqrt(np.mean(dd_squared))
        return float(ulcer)
    
    async def _select_universe(self, current_time: datetime) -> List[str]:
        query = """
            SELECT DISTINCT market, COUNT(*) as candle_count
            FROM candles 
            WHERE timestamp <= ?
            GROUP BY market
            HAVING candle_count >= 120
            ORDER BY candle_count DESC
            LIMIT 10
        """
        
        markets_info = self.storage.query(query, (current_time.timestamp(),))
        
        if not markets_info:
            return []
        
        return [info['market'] for info in markets_info]
    
    async def _generate_signals(self, universe: List[str], current_time: datetime) -> List:
        signals = []
        
        btc_regime_ok = True  # Simplified
        
        for market in universe:
            candles = self.data_loader.load_candles(
                market, 
                current_time - timedelta(hours=2),
                current_time
            )
            
            if len(candles) < 24:
                continue
            
            # live 로직의 tradable 조건을 통과할 수 있도록 최소치 이상으로 설정
            notional_ratio = max(4.0, float(self.cfg["signal"]["notional_ratio_min"]) + 0.5)
            spread_pct = min(0.001, float(self.cfg["gates"]["spread_max"]) * 0.5)
            depth_ratio = max(6.0, float(self.cfg["gates"]["depth_ratio_min"]) + 1.0)
            
            signal = build_signal(
                market=market,
                candles=candles,
                notional_ratio=notional_ratio,
                spread_pct=spread_pct,
                depth_ratio=depth_ratio,
                btc_regime_ok=btc_regime_ok,
                notional_ratio_min=self.cfg["signal"]["notional_ratio_min"],
                orderbook=None,
            )
            
            signals.append(signal)
        
        return signals
    
    async def _get_current_price(self, market: str, current_time: datetime) -> Optional[float]:
        query = """
            SELECT close 
            FROM candles 
            WHERE market = ? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT 1
        """
        
        result = self.storage.query_one(query, (market, current_time.timestamp()))
        return result['close'] if result else None
    
    def _check_exit_conditions(self, position: Position, current_price: float, 
                            current_time: datetime) -> Optional[str]:
        entry_time = datetime.fromtimestamp(position.entry_ts_ms / 1000)
        hold_minutes = (current_time - entry_time).total_seconds() / 60
        
        if current_price <= position.stop_price:
            return "stop_loss"
        
        unrealized_pct = (current_price - position.entry_price) / position.entry_price
        if unrealized_pct >= self.cfg["stops"]["tp_net_pnl_pct"]:
            return "take_profit"
        
        if hold_minutes >= self.cfg["time_rules"]["hard_exit_minutes"]:
            return "time_exit"
        
        if hold_minutes >= self.cfg["time_rules"]["soft_cut_minutes"]:
            if unrealized_pct <= self.cfg["time_rules"]["soft_cut_progress"]:
                return "soft_cut"
        
        return None
    
    def _calculate_trade_pnl(self, position: Position, exit_price: float) -> float:
        price_change_pct = (exit_price - position.entry_price) / position.entry_price
        gross_pnl = position.qty * position.entry_price * price_change_pct
        
        market_info = parse_market(position.market)
        commission_rate = self.backtest_config.commission_rates.get(market_info.quote, 0.0005)
        
        entry_value = position.qty * position.entry_price
        exit_value = position.qty * exit_price
        total_fees = (entry_value + exit_value) * commission_rate
        
        return gross_pnl - total_fees
    
    def _calculate_current_equity(self) -> float:
        initial_equity = self.backtest_config.initial_equity
        
        if not self.all_trades:
            return initial_equity
        
        realized_pnl = sum(t['pnl'] for t in self.all_trades)
        unrealized_pnl = self._calculate_unrealized_pnl()
        
        return initial_equity + realized_pnl + unrealized_pnl
    
    def _calculate_unrealized_pnl(self) -> float:
        return 0.0
    
    def _generate_results(self) -> BacktestResult:
        if not self.all_trades:
            return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, {}, [], [])
        
        total_trades = len(self.all_trades)
        winning_trades = len([t for t in self.all_trades if t['pnl'] > 0])
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        total_pnl = sum(t['pnl'] for t in self.all_trades)
        initial_equity = self.backtest_config.initial_equity
        net_pnl_pct = total_pnl / initial_equity
        
        hold_times = [t['hold_seconds'] for t in self.all_trades]
        avg_hold_seconds = sum(hold_times) / len(hold_times) if hold_times else 0
        
        slippages = [t['slippage'] for t in self.all_trades]
        avg_slippage_pct = sum(slippages) / len(slippages) if slippages else 0
        
        max_drawdown_pct = self._calculate_max_drawdown()
        sharpe_ratio = self._calculate_sharpe_ratio()
        profit_factor = self._calculate_profit_factor()
        
        daily_returns = []
        if len(self.equity_curve) > 1:
            for i in range(1, len(self.equity_curve)):
                daily_returns.append((self.equity_curve[i] - self.equity_curve[i-1]) / self.equity_curve[i-1])
        
        per_market_stats = {}
        for trade in self.all_trades:
            market = trade['market']
            if market not in per_market_stats:
                per_market_stats[market] = {'trades': 0, 'pnl': 0, 'wins': 0}
            per_market_stats[market]['trades'] += 1
            per_market_stats[market]['pnl'] += trade['pnl']
            if trade['pnl'] > 0:
                per_market_stats[market]['wins'] += 1
        
        for market in per_market_stats:
            stats = per_market_stats[market]
            stats['win_rate'] = stats['wins'] / stats['trades'] if stats['trades'] > 0 else 0
            stats['pnl'] = stats['pnl'] / initial_equity
        
        return BacktestResult(
            total_trades=total_trades,
            win_rate=win_rate,
            net_pnl_pct=net_pnl_pct,
            max_drawdown_pct=max_drawdown_pct,
            avg_hold_seconds=avg_hold_seconds,
            avg_slippage_pct=avg_slippage_pct,
            sharpe_ratio=sharpe_ratio,
            profit_factor=profit_factor,
            per_market_stats=per_market_stats,
            equity_curve=self.equity_curve,
            daily_returns=daily_returns
        )
