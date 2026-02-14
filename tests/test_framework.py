from src.portfolio import Portfolio


def test_portfolio_total_exposure_ratio_krw_normalized_for_usdt():
    cfg = {
        "stops": {"tp_net_pnl_pct": 0.02, "tp1_ratio": 0.5, "trailing_stop_pct": 0.02},
        "time_rules": {"hard_exit_minutes": 999, "soft_cut_minutes": 999, "soft_cut_progress": 0.0},
        "replace": {"score_gap_min": 999},
    }
    pf = Portfolio(cfg)
    pf.add("USDT-ABC", qty=1.0, entry_price=1000.0, stop_price=900.0, score=80)

    equity = 1_000_000.0
    last_prices = {
        "USDT-ABC": 1000.0,
        "KRW-USDT": 1300.0,
    }

    r = pf.total_exposure_ratio(equity, last_prices)
    assert abs(r - 1.3) < 1e-9


def test_portfolio_total_exposure_ratio_krw_normalized_for_btc():
    cfg = {
        "stops": {"tp_net_pnl_pct": 0.02, "tp1_ratio": 0.5, "trailing_stop_pct": 0.02},
        "time_rules": {"hard_exit_minutes": 999, "soft_cut_minutes": 999, "soft_cut_progress": 0.0},
        "replace": {"score_gap_min": 999},
    }
    pf = Portfolio(cfg)
    pf.add("BTC-ABC", qty=0.01, entry_price=0.05, stop_price=0.04, score=80)

    equity = 1_000_000.0
    last_prices = {
        "BTC-ABC": 0.05,
        "KRW-BTC": 100_000_000.0,
    }

    # value_krw = 0.01 * 0.05 BTC * 1e8 KRW/BTC = 50,000 KRW -> ratio 0.05
    r = pf.total_exposure_ratio(equity, last_prices)
    assert abs(r - 0.05) < 1e-9
