from src.risk import RiskManager


def test_position_sizing_basic():
    cfg = {
        "risk": {
            "promote_requirements": {
                "min_trades": 200,
                "max_order_error_rate": 0.005,
                "max_avg_entry_slippage": 0.0025,
            },
            "risk_per_trade_start": 0.001,
            "risk_per_trade_target": 0.002,
            "daily_stop_loss_pct": -0.02,
            "max_positions": 10,
            "total_exposure_cap": 0.70,
            "per_coin_exposure_cap": 0.12,
        },
        "min_notional_krw": 50000,
    }
    r = RiskManager(cfg, 10_000_000)
    value = r.compute_position_value(stop_pct=0.012, k_signals=5, coin_exposure_now=0.03)
    assert value >= 50_000


def test_daily_stop_blocks_entry():
    cfg = {
        "risk": {
            "promote_requirements": {
                "min_trades": 200,
                "max_order_error_rate": 0.005,
                "max_avg_entry_slippage": 0.0025,
            },
            "risk_per_trade_start": 0.001,
            "risk_per_trade_target": 0.002,
            "daily_stop_loss_pct": -0.02,
            "max_positions": 10,
            "total_exposure_cap": 0.70,
            "per_coin_exposure_cap": 0.12,
        },
        "min_notional_krw": 50000,
    }
    r = RiskManager(cfg, 1_000_000)
    r.daily_realized_pct = -0.03
    assert r.can_open_new_entry(0, 0.0) is False
