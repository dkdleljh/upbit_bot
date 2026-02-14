"""
Comprehensive test framework for Upbit trading bot.
Provides unit tests, integration tests, and test utilities.
"""
import pytest
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List
from unittest.mock import Mock, patch

from src.backtest_engine_simple import BacktestEngine, BacktestConfig, BacktestResult
from src.data_manager import DataManager, HistoricalDataCollector
from src.execution import ExecutionEngine, ExecutionResult
from src.indicators import atr, ema, highest_high, breakout_pct
from src.portfolio import Portfolio, Position
from src.risk import RiskManager, TradeStats
from src.signal_engine import build_signal, Signal, score_execution, score_notional, score_momentum
from src.storage import Storage
from src.universe import select_universe


class TestFixtures:
    """Test fixtures and utilities for testing."""
    
    @staticmethod
    def create_test_config() -> Dict:
        """Create test configuration."""
        return {
            "base_ccy": "KRW",
            "mode": "paper",
            "timezone": "Asia/Seoul",
            "universe": {
                "top30": 30,
                "tradable_top10": 10,
                "refresh_seconds": 300
            },
            "gates": {
                "spread_max": 0.0012,
                "depth_ratio_min": 5.0,
                "entry_slippage_cap": 0.0025,
                "exit_slippage_cap": 0.0030,
                "cooldown_minutes": 30
            },
            "signal": {
                "timeframe_main": "1m",
                "timeframe_filter": "5m",
                "breakout_lookback_minutes": 20,
                "notional_ratio_min": 3.0
            },
            "risk": {
                "max_positions": 10,
                "total_exposure_cap": 0.70,
                "per_coin_exposure_cap": 0.12,
                "risk_per_trade_start": 0.001,
                "risk_per_trade_target": 0.002,
                "promote_requirements": {
                    "min_trades": 200,
                    "max_order_error_rate": 0.005,
                    "max_avg_entry_slippage": 0.0025
                },
                "daily_stop_loss_pct": -0.02
            },
            "stops": {
                "atr_period": 14,
                "atr_timeframe": "1m",
                "atr_multiplier": 1.6,
                "stop_pct_min": 0.009,
                "stop_pct_max": 0.018,
                "tp_net_pnl_pct": 0.05,
                "tp1_ratio": 0.5,
                "trailing_stop_pct": 0.015
            },
            "time_rules": {
                "min_hold_seconds": 90,
                "soft_cut_minutes": 8,
                "soft_cut_progress": 0.01,
                "hard_exit_minutes": 20
            },
            "min_notional_krw": 50000,
            "runtime": {
                "loop_interval_seconds": 3,
                "paper_initial_equity_krw": 10000000,
                "backtest_days": 30,
                "backtest_default_slippage": 0.002,
                "safe_mode_error_threshold": 5
            },
            "fees": {
                "KRW": 0.0005,
                "BTC": 0.0010,
                "USDT": 0.0010
            }
        }
    
    @staticmethod
    def create_test_candles(count: int = 50, base_price: float = 50000) -> List[Dict]:
        """Create test candle data."""
        candles = []
        base_time = datetime.now() - timedelta(minutes=count)
        
        for i in range(count):
            # Simple price progression with some volatility
            price_change = (i % 10 - 5) * 100  # ±500 KRW range
            close_price = base_price + price_change + (i * 10)  # Trend
            high_price = close_price + abs(price_change) + 200
            low_price = close_price - abs(price_change) - 200
            open_price = close_price - (price_change // 2)
            
            candles.append({
                'timestamp': (base_time + timedelta(minutes=i)).timestamp(),
                'open': float(open_price),
                'high': float(high_price),
                'low': float(low_price),
                'close': float(close_price),
                'volume': 100.0 + (i % 50) * 2,
                'value': float(close_price) * (100.0 + (i % 50) * 2)
            })
        
        return candles
    
    @staticmethod
    def create_test_positions() -> Dict[str, Position]:
        """Create test positions."""
        now_ms = int(datetime.now().timestamp() * 1000)
        return {
            "KRW-BTC": Position(
                market="KRW-BTC",
                base_coin="BTC",
                qty=0.1,
                entry_price=50000,
                stop_price=49000,
                peak_price=51000,
                entry_ts_ms=now_ms - 300000,
                score=75.0
            ),
            "KRW-ETH": Position(
                market="KRW-ETH",
                base_coin="ETH", 
                qty=10.0,
                entry_price=3000,
                stop_price=2940,
                peak_price=3100,
                entry_ts_ms=now_ms - 600000,
                score=65.0
            )
        }
    
    @staticmethod
    def create_temp_storage():
        """Create temporary storage for testing."""
        import tempfile
        import os
        temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp_path = temp_db.name
        storage = Storage(temp_path)
        return storage, temp_path


class TestSignalEngine:
    """Test cases for SignalEngine."""
    
    def test_score_execution_perfect_conditions(self):
        """Test scoring with perfect execution conditions."""
        score, tradable = score_execution(0.0005, 10.0)
        
        assert score == 40
        assert tradable is True
    
    def test_score_execution_just_above_threshold(self):
        """Test scoring just above thresholds."""
        score, tradable = score_execution(0.0010, 5.1)
        
        assert score == 22  # 14 + 8
        assert tradable is True
    
    def test_score_execution_below_threshold(self):
        """Test scoring below minimum thresholds."""
        score, tradable = score_execution(0.0015, 4.9)
        
        assert score == 0
        assert tradable is False
    
    def test_score_notional_all_levels(self):
        """Test notional scoring at all levels."""
        assert score_notional(4.5) == 30
        assert score_notional(3.5) == 22
        assert score_notional(2.5) == 12
        assert score_notional(1.9) == 0
    
    def test_score_momentum_all_levels(self):
        """Test momentum scoring at all levels."""
        assert score_momentum(0.015) == 10
        assert score_momentum(0.008) == 6
        assert score_momentum(0.002) == 2
        assert score_momentum(-0.002) == 0
    
    def test_build_signal_tradable(self):
        """Test signal building with tradable conditions."""
        candles = TestFixtures.create_test_candles(30)
        
        signal = build_signal(
            market="KRW-BTC",
            candles=candles,
            notional_ratio=4.0,
            spread_pct=0.0008,
            depth_ratio=8.0,
            btc_regime_ok=True,
            notional_ratio_min=3.0
        )
        
        assert signal.market == "KRW-BTC"
        assert signal.tradable is True
        assert signal.score > 0
        assert signal.breakout is True
        assert signal.note == "PASS"
    
    def test_build_signal_not_tradable(self):
        """Test signal building with non-tradable conditions."""
        candles = TestFixtures.create_test_candles(10)  # Not enough candles
        
        signal = build_signal(
            market="KRW-BTC",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.0020,  # Too high spread
            depth_ratio=2.0,      # Too low depth
            btc_regime_ok=False,
            notional_ratio_min=3.0
        )
        
        assert signal.tradable is False
        assert signal.note == "candles 부족"
    
    def test_signal_edge_cases(self):
        """Test signal edge cases."""
        # Empty candles
        signal = build_signal(
            market="KRW-BTC",
            candles=[],
            notional_ratio=4.0,
            spread_pct=0.0005,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=3.0
        )
        
        assert signal.tradable is False
        assert "candles 부족" in signal.note


class TestIndicators:
    """Test cases for technical indicators."""
    
    def test_atr_basic(self):
        """Test ATR calculation with basic data."""
        candles = TestFixtures.create_test_candles(20, 50000)
        
        atr_value = atr(candles, 14)
        
        assert atr_value > 0
        assert atr_value < 10000  # Should be reasonable
    
    def test_atr_edge_cases(self):
        """Test ATR edge cases."""
        # Empty candles
        assert atr([], 14) == 0
        
        # Single candle
        single_candle = TestFixtures.create_test_candles(1)
        assert atr(single_candle, 14) == 0
    
    def test_ema_basic(self):
        """Test EMA calculation with basic data."""
        prices = [50000, 50100, 50200, 50300, 50400, 50500]
        
        ema_3 = ema(prices, 3)
        
        assert ema_3 > 50000
        assert ema_3 < 50500
    
    def test_ema_edge_cases(self):
        """Test EMA edge cases."""
        # Empty list
        assert ema([], 5) is None
        
        # Single value
        assert ema([50000], 5) == 50000
        
        # Period longer than data
        short_data = [50000, 50100]
        # EMA with 2 values and period 5
        alpha = 2 / (5 + 1)  # 0.333...
        expected = alpha * 50100 + (1 - alpha) * 50000  # 50033.33
        assert abs(ema(short_data, 5) - expected) < 0.01


class TestPortfolio:
    """Test cases for Portfolio management."""
    
    def test_portfolio_initialization(self):
        """Test portfolio initialization."""
        cfg = TestFixtures.create_test_config()
        portfolio = Portfolio(cfg)
        
        assert portfolio.count() == 0
        assert portfolio.positions == {}
    
    def test_add_position(self):
        """Test adding a position."""
        cfg = TestFixtures.create_test_config()
        portfolio = Portfolio(cfg)
        
        portfolio.add(
            market="KRW-BTC",
            qty=0.1,
            entry_price=50000,
            stop_price=49000,
            score=75.0
        )
        
        assert portfolio.count() == 1
        assert "KRW-BTC" in portfolio.positions
        
        position = portfolio.positions["KRW-BTC"]
        assert position.qty == 0.1
        assert position.entry_price == 50000
        assert position.stop_price == 49000
    
    def test_remove_position(self):
        """Test removing a position."""
        cfg = TestFixtures.create_test_config()
        portfolio = Portfolio(cfg)
        
        portfolio.add(
            market="KRW-BTC",
            qty=0.1,
            entry_price=50000,
            stop_price=49000,
            score=75.0
        )
        
        portfolio.remove("KRW-BTC")
        
        assert portfolio.count() == 0
        assert "KRW-BTC" not in portfolio.positions
    
    def test_total_exposure_calculation(self):
        """Test total exposure calculation."""
        cfg = TestFixtures.create_test_config()
        portfolio = Portfolio(cfg)
        
        # Add multiple positions
        portfolio.add("KRW-BTC", 0.1, 50000, 49000, 75.0)
        portfolio.add("KRW-ETH", 10.0, 3000, 2940, 65.0)
        
        equity = 10000000
        last_prices = {"KRW-BTC": 51000.0, "KRW-ETH": 3100.0}
        
        exposure = portfolio.total_exposure_ratio(equity, last_prices)
        
        btc_value = 0.1 * 51000
        eth_value = 10.0 * 3100
        expected = (btc_value + eth_value) / equity
        
        assert abs(exposure - expected) < 0.01
    
    def test_coin_exposure_calculation(self):
        """Test coin-specific exposure calculation."""
        cfg = TestFixtures.create_test_config()
        portfolio = Portfolio(cfg)
        
        # Add multiple ETH positions
        portfolio.add("KRW-ETH", 5.0, 3000, 2940, 65.0)
        portfolio.add("KRW-ETH2", 3.0, 3100, 3038, 60.0)
        
        equity = 10000000
        last_prices = {"KRW-ETH": 3100.0, "KRW-ETH2": 3200.0}
        
        exposure = portfolio.coin_exposure_ratio("ETH", equity, last_prices)
        
        eth_value = 5.0 * 3100
        expected = eth_value / equity
        
        assert abs(exposure - expected) < 0.01


class TestRiskManager:
    """Test cases for RiskManager."""
    
    def test_risk_manager_initialization(self):
        """Test risk manager initialization."""
        cfg = TestFixtures.create_test_config()
        initial_equity = 10000000
        risk_manager = RiskManager(cfg, initial_equity)
        
        assert risk_manager.initial_equity == initial_equity
        assert risk_manager.equity == initial_equity
        assert risk_manager.realized_pnl == 0.0
        assert risk_manager.daily_realized_pct == 0.0
    
    def test_current_r_calculation(self):
        """Test current R calculation."""
        cfg = TestFixtures.create_test_config()
        risk_manager = RiskManager(cfg, 10000000)
        
        # With no trades (should be starting risk)
        current_r = risk_manager.current_r()
        assert current_r == cfg["risk"]["risk_per_trade_start"]
        
        # Simulate good stats
        risk_manager.stats.total_trades = 250
        risk_manager.stats.order_errors = 1
        risk_manager.stats.avg_entry_slippage = 0.002
        
        current_r_promoted = risk_manager.current_r()
        assert current_r_promoted == cfg["risk"]["risk_per_trade_target"]
    
    def test_update_realized_pnl(self):
        """Test realized P&L updates."""
        cfg = TestFixtures.create_test_config()
        risk_manager = RiskManager(cfg, 10000000)
        
        # Add profit
        risk_manager.update_realized(100000)
        assert risk_manager.realized_pnl == 100000
        assert risk_manager.equity == 10100000
        assert risk_manager.daily_realized_pct == 0.01
        
        # Add loss
        risk_manager.update_realized(-50000)
        assert risk_manager.realized_pnl == 50000
        assert risk_manager.equity == 10050000
        assert risk_manager.daily_realized_pct == 0.005
    
    def test_can_open_new_entry(self):
        """Test new entry permission logic."""
        cfg = TestFixtures.create_test_config()
        risk_manager = RiskManager(cfg, 10000000)
        
        # Normal conditions
        can_open = risk_manager.can_open_new_entry(5, 0.6)
        assert can_open is True
        
        # Too many positions
        can_open = risk_manager.can_open_new_entry(10, 0.6)
        assert can_open is False
        
        # Too much exposure
        can_open = risk_manager.can_open_new_entry(5, 0.8)
        assert can_open is False
        
        # Daily stop loss triggered
        risk_manager.daily_realized_pct = -0.03
        can_open = risk_manager.can_open_new_entry(5, 0.6)
        assert can_open is False
    
    def test_compute_position_value(self):
        """Test position value computation."""
        cfg = TestFixtures.create_test_config()
        risk_manager = RiskManager(cfg, 10000000)
        
        position_value = risk_manager.compute_position_value(
            stop_pct=0.012,
            k_signals=5,
            coin_exposure_now=0.03
        )
        
        assert position_value >= cfg["min_notional_krw"]


class TestExecutionEngine:
    """Test cases for ExecutionEngine."""
    
    def test_execution_result_creation(self):
        """Test ExecutionResult creation."""
        result = ExecutionResult(
            ok=True,
            market="KRW-BTC",
            side="BUY",
            qty=0.1,
            fill_price=50000,
            fee=25,
            slippage_pct=0.001,
            reason="success"
        )
        
        assert result.ok is True
        assert result.market == "KRW-BTC"
        assert result.side == "BUY"
        assert result.qty == 0.1
        assert result.fill_price == 50000
        assert result.fee == 25
        assert result.slippage_pct == 0.001
        assert result.reason == "success"


class TestIntegration:
    """Integration tests for component interactions."""
    
    def test_backtest_engine_initialization(self):
        """Test BacktestEngine initialization."""
        storage, temp_path = TestFixtures.create_temp_storage()
        
        try:
            cfg = TestFixtures.create_test_config()
            from src.backtest_engine_simple import HistoricalDataLoader
            
            data_loader = HistoricalDataLoader(storage)
            engine = BacktestEngine(cfg, storage, data_loader)
            
            assert engine.cfg == cfg
            assert engine.storage == storage
            assert engine.portfolio.count() == 0
            assert engine.backtest_config.initial_equity == 10000000
            
        finally:
            import os
            os.unlink(temp_path)
    
    def test_data_manager_initialization(self):
        """Test DataManager initialization."""
        storage, temp_path = TestFixtures.create_temp_storage()
        
        try:
            data_manager = DataManager(storage)
            
            assert data_manager.storage == storage
            assert hasattr(data_manager, 'collector')
            assert hasattr(data_manager, 'collector')
            
        finally:
            import os
            os.unlink(temp_path)
    
    def test_end_to_end_signal_flow(self):
        """Test end-to-end signal generation flow."""
        storage, temp_path = TestFixtures.create_temp_storage()
        
        try:
            # Setup test data
            candles = TestFixtures.create_test_candles(30)
            
            # Insert test candles
            for candle in candles:
                storage.execute(
                    "INSERT INTO candles (market, timestamp, open, high, low, close, volume, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("KRW-BTC", candle['timestamp'], candle['open'], candle['high'], 
                     candle['low'], candle['close'], candle['volume'], candle['value'])
                )
            
            # Test signal generation
            signal = build_signal(
                market="KRW-BTC",
                candles=candles,
                notional_ratio=4.0,
                spread_pct=0.0008,
                depth_ratio=8.0,
                btc_regime_ok=True,
                notional_ratio_min=3.0
            )
            
            assert signal.tradable is True
            assert signal.score > 0
            
        finally:
            import os
            os.unlink(temp_path)


# Test runner utilities
class TestRunner:
    """Utilities for running tests."""
    
    @staticmethod
    def run_all_tests():
        """Run all test suites."""
        test_classes = [
            TestSignalEngine,
            TestIndicators, 
            TestPortfolio,
            TestRiskManager,
            TestExecutionEngine,
            TestIntegration
        ]
        
        for test_class in test_classes:
            pytest.main([f"tests/test_{test_class.__name__.lower()}.py", "-v"])
    
    @staticmethod
    def run_specific_test(test_name: str):
        """Run specific test."""
        pytest.main([f"tests/::{test_name}", "-v"])
    
    @staticmethod
    def generate_test_report():
        """Generate comprehensive test report."""
        # This would integrate with pytest coverage and reporting
        pass


if __name__ == "__main__":
    # Run tests when executed directly
    TestRunner.run_all_tests()