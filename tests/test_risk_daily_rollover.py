from datetime import timedelta

from src.risk import RiskManager


def _cfg():
    return {
        "timezone": "Asia/Seoul",
        "risk": {
            "risk_per_trade_target": 0.002,
            "risk_per_trade_start": 0.001,
            "daily_stop_loss_pct": -0.02,
            "max_positions": 10,
            "total_exposure_cap": 0.7,
            "per_coin_exposure_cap": 0.12,
        },
        "position_sizing": {
            "kelly_fraction": 0.25,
            "loss_streak_threshold": 3,
            "loss_streak_reduction": 0.5,
            "min_trades_for_full_kelly": 50,
        },
        "stops": {"atr_multiplier": 1.5},
        "mtm_risk": {
            "use_mtm_drawdown": True,
            "mtm_drawdown_pause_pct": -0.08,
            "mtm_drawdown_stop_pct": -0.12,
        },
        "min_notional_krw": 5000,
        "runtime": {"min_entry_krw": 5500},
    }


def test_daily_realized_rolls_over_on_new_day():
    rm = RiskManager(_cfg(), initial_equity=1_000_000)

    rm.update_realized(-10_000)
    assert rm.daily_realized_pnl == -10_000
    assert rm.daily_realized_pct < 0

    rm._risk_day = rm._risk_day - timedelta(days=1)
    rm.roll_daily()

    assert rm.daily_realized_pnl == 0.0
    assert rm.daily_realized_pct == 0.0
    assert rm.realized_pnl == -10_000


def test_update_daily_pnl_uses_today_realized_only():
    rm = RiskManager(_cfg(), initial_equity=1_000_000)

    rm.update_realized(-5_000)
    rm.update_daily_pnl(unrealized_pnl=-2_000)
    assert abs(rm.daily_realized_pct - (-0.007)) < 1e-9

    rm._risk_day = rm._risk_day - timedelta(days=1)
    rm.update_daily_pnl(unrealized_pnl=-1_000)

    assert abs(rm.daily_realized_pct - (-0.001)) < 1e-9
