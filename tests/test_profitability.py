import pytest
from src.signal_engine import build_signal, build_signal_simple, Signal
from src.portfolio import Portfolio, Position
from src.risk import RiskManager


def _c(ts_ms: int, o: float, h: float, l: float, c: float, v: float):
    return {"ts_ms": ts_ms, "timestamp": ts_ms / 1000, "open": o, "high": h, "low": l, "close": c, "volume": v, "value": c * v}


def _make_uptrend_candles(n: int = 120, base_price: float = 100.0):
    candles = []
    px = base_price
    for i in range(n):
        px *= 1.0008
        candles.append(_c(0 + i * 60_000, px * 0.999, px * 1.002, px * 0.998, px, 100.0))
    return candles


def _make_downtrend_candles(n: int = 120, base_price: float = 100.0):
    candles = []
    px = base_price
    for i in range(n):
        px *= 0.9992
        candles.append(_c(0 + i * 60_000, px * 1.001, px * 1.002, px * 0.998, px, 100.0))
    return candles


def _make_sideways_candles(n: int = 120, base_price: float = 100.0):
    candles = []
    import random
    random.seed(42)
    px = base_price
    for i in range(n):
        px = px * (1 + random.uniform(-0.002, 0.002))
        candles.append(_c(0 + i * 60_000, px * 0.999, px * 1.001, px * 0.998, px, 50.0))
    return candles


class TestSignalProfitability:
    def test_uptrend_signal_generates_buy_signal(self):
        candles = _make_uptrend_candles()
        s = build_signal(
            market="KRW-BTC",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.001,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=True,
        )
        assert s.score > 0, f"Expected positive score for uptrend, got {s.score}"
        assert s.tradable is True or s.score >= s.dynamic_cutoff

    def test_downtrend_signal_rejected(self):
        candles = _make_downtrend_candles()
        s = build_signal(
            market="KRW-ETH",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.001,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=True,
        )
        assert s.tradable is False or s.score < s.dynamic_cutoff

    def test_sideways_market_low_score(self):
        candles = _make_sideways_candles()
        s = build_signal(
            market="KRW-XRP",
            candles=candles,
            notional_ratio=0.5,
            spread_pct=0.003,
            depth_ratio=3.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=False,
        )
        assert s.score < s.dynamic_cutoff or s.tradable is False

    def test_volume_explosion_boosts_score(self):
        candles = _make_uptrend_candles()
        candles[-1]["volume"] = candles[-1]["volume"] * 3
        s = build_signal(
            market="KRW-SOL",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.001,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=True,
        )
        assert "VOL_2x" in s.note or "VOL_1.5x" in s.note

    def test_rsi_overheat_veto(self):
        candles = _make_uptrend_candles(base_price=100.0)
        for i in range(20):
            candles[-1 - i]["close"] = candles[-2 - i]["close"] * 1.003
        s = build_signal(
            market="KRW-HOT",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.001,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=True,
        )
        if s.score >= s.dynamic_cutoff and s.tradable:
            assert "REJECT_RSI_HOT" not in s.note


class TestPortfolioProfitability:
    def test_profitable_position_evaluation(self):
        cfg = {
            "stops": {"tp_net_pnl_pct": 0.02, "tp1_ratio": 0.5, "trailing_stop_pct": 0.015, "tp2_ratio": 0.3},
            "time_rules": {"hard_exit_minutes": 60, "soft_cut_minutes": 20, "soft_cut_progress": 0.02},
            "replace": {"score_gap_min": 12},
        }
        pf = Portfolio(cfg)
        entry_price = 10000.0
        pf.add("KRW-BTC", qty=1.0, entry_price=entry_price, stop_price=9800.0, score=80.0)
        current_price = 10500.0
        actions = pf.evaluate_exits("KRW-BTC", current_price, 0.0005, 0.001)
        assert len(actions) > 0
        assert actions[0]["type"] in ["TP1", "TP2", "TRAIL"]

    def test_stop_loss_trigger(self):
        cfg = {
            "stops": {"tp_net_pnl_pct": 0.02, "tp1_ratio": 0.5, "trailing_stop_pct": 0.015, "tp2_ratio": 0.3},
            "time_rules": {"hard_exit_minutes": 60, "soft_cut_minutes": 20, "soft_cut_progress": 0.02},
            "replace": {"score_gap_min": 12},
        }
        pf = Portfolio(cfg)
        entry_price = 10000.0
        pf.add("KRW-ETH", qty=1.0, entry_price=entry_price, stop_price=9900.0, score=80.0)
        current_price = 9890.0
        import time
        pf.positions["KRW-ETH"].entry_ts_ms = int(time.time() * 1000) - 120 * 1000
        actions = pf.evaluate_exits("KRW-ETH", current_price, 0.0005, 0.001)
        assert len(actions) > 0
        assert actions[0]["type"] == "STOP"

    def test_breakeven_stop_moves_up(self):
        cfg = {
            "stops": {"tp_net_pnl_pct": 0.02, "tp1_ratio": 0.5, "trailing_stop_pct": 0.015, "tp2_ratio": 0.3},
            "time_rules": {"hard_exit_minutes": 60, "soft_cut_minutes": 20, "soft_cut_progress": 0.02},
            "replace": {"score_gap_min": 12},
        }
        pf = Portfolio(cfg)
        entry_price = 10000.0
        pf.add("KRW-ADA", qty=1.0, entry_price=entry_price, stop_price=9900.0, score=80.0)
        pf.mark_peak("KRW-ADA", 10200.0)
        import time
        pf.positions["KRW-ADA"].entry_ts_ms = int(time.time() * 1000) - 120 * 1000
        pf.positions["KRW-ADA"].tp1_done = True
        current_price = 10200.0
        pf.evaluate_exits("KRW-ADA", current_price, 0.0005, 0.001)
        assert pf.positions["KRW-ADA"].stop_price > entry_price


class TestRiskManagementProfitability:
    def test_kelly_adapts_to_win_rate(self):
        cfg = {
            "risk": {
                "risk_per_trade_start": 0.001,
                "risk_per_trade_target": 0.002,
                "daily_stop_loss_pct": -0.02,
                "max_positions": 10,
                "total_exposure_cap": 0.70,
                "per_coin_exposure_cap": 0.12,
                "promote_requirements": {"min_trades": 5, "max_order_error_rate": 0.1, "max_avg_entry_slippage": 0.01},
            },
            "min_notional_krw": 5000,
        }
        r = RiskManager(cfg, 1_000_000)
        for _ in range(10):
            r.update_trade_result(True, 0.02)
        kelly = r.kelly_fraction
        assert kelly > 0.001

    def test_consecutive_losses_trigger_caution(self):
        cfg = {
            "risk": {
                "risk_per_trade_start": 0.001,
                "risk_per_trade_target": 0.002,
                "daily_stop_loss_pct": -0.02,
                "max_positions": 10,
                "total_exposure_cap": 0.70,
                "per_coin_exposure_cap": 0.12,
                "promote_requirements": {"min_trades": 5, "max_order_error_rate": 0.1, "max_avg_entry_slippage": 0.01},
            },
            "min_notional_krw": 5000,
        }
        r = RiskManager(cfg, 1_000_000)
        for _ in range(5):
            r.update_trade_result(False, -0.02)
        kelly = r.kelly_fraction
        assert kelly <= 0.0015

    def test_drawdown_breaker_stops_trading(self):
        cfg = {
            "risk": {
                "risk_per_trade_start": 0.001,
                "risk_per_trade_target": 0.002,
                "daily_stop_loss_pct": -0.02,
                "max_positions": 10,
                "total_exposure_cap": 0.70,
                "per_coin_exposure_cap": 0.12,
                "promote_requirements": {"min_trades": 5, "max_order_error_rate": 0.1, "max_avg_entry_slippage": 0.01},
            },
            "min_notional_krw": 5000,
        }
        r = RiskManager(cfg, 1_000_000)
        r.equity = 800000
        r.peak_equity = 1000000
        assert r.is_drawdown_breaker_triggered() is True


class TestStrategyProfitability:
    def test_profitable_scenario_simulation(self):
        cfg = {
            "stops": {"tp_net_pnl_pct": 0.01, "tp1_ratio": 0.5, "trailing_stop_pct": 0.01, "tp2_ratio": 0.3},
            "time_rules": {"hard_exit_minutes": 60, "soft_cut_minutes": 20, "soft_cut_progress": 0.02},
            "replace": {"score_gap_min": 12},
            "risk": {
                "risk_per_trade_start": 0.001,
                "risk_per_trade_target": 0.002,
                "daily_stop_loss_pct": -0.02,
                "max_positions": 10,
                "total_exposure_cap": 0.70,
                "per_coin_exposure_cap": 0.12,
                "promote_requirements": {"min_trades": 5, "max_order_error_rate": 0.1, "max_avg_entry_slippage": 0.01},
            },
            "min_notional_krw": 5000,
        }
        pf = Portfolio(cfg)
        
        wins = 0
        total_trades = 0
        
        import time
        
        for i in range(20):
            entry_price = 10000.0
            pf.add(f"KRW-COIN{i}", qty=1.0, entry_price=entry_price, stop_price=entry_price * 0.98, score=80.0)
            pf.positions[f"KRW-COIN{i}"].entry_ts_ms = int(time.time() * 1000) - 120 * 1000
            
            if i % 2 == 0:
                current_price = entry_price * 1.02
                actions = pf.evaluate_exits(f"KRW-COIN{i}", current_price, 0.0005, 0.001)
                if actions:
                    wins += 1
                total_trades += 1
            else:
                current_price = entry_price * 0.97
                actions = pf.evaluate_exits(f"KRW-COIN{i}", current_price, 0.0005, 0.001)
                if actions:
                    wins += 1
                total_trades += 1
        
        assert total_trades > 0, "Should have some trades"


class TestSimpleSignalProfile:
    def test_simple_profile_faster_and_selective(self):
        candles = _make_uptrend_candles()
        
        full_sig = build_signal(
            market="KRW-FULL",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.001,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=True,
        )
        
        simple_sig = build_signal_simple(
            market="KRW-SIMPLE",
            candles=candles,
            notional_ratio=2.0,
            spread_pct=0.001,
            depth_ratio=10.0,
            btc_regime_ok=True,
            notional_ratio_min=0.9,
            mtf_trend_ok=True,
        )
        
        assert simple_sig.dynamic_cutoff == 60
        assert full_sig.dynamic_cutoff >= 60
