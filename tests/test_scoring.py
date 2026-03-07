import src.signal_engine as signal_engine
from src.signal_engine import build_signal, _trend_value


def _make_candle(ts_ms: int, o: float, h: float, l: float, c: float, v: float):
    return {
        "ts_ms": ts_ms,
        "timestamp": ts_ms / 1000,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "value": c * v,
    }


def test_build_signal_requires_60_candles():
    candles = [_make_candle(0 + i * 60_000, 100, 101, 99, 100, 10) for i in range(59)]
    s = build_signal(
        market="KRW-AAA",
        candles=candles,
        notional_ratio=10.0,
        spread_pct=0.0005,
        depth_ratio=10.0,
        btc_regime_ok=True,
        notional_ratio_min=0.9,
    )
    assert s.tradable is False
    assert s.score == 0
    assert "candles < 60" in (s.note or "")


def test_build_signal_rsi_hot_veto():
    # 강한 우상향이면 RSI가 과열로 나올 확률이 높고, 그 경우 tradable은 False가 되어야 합니다.
    candles = []
    px = 100.0
    for i in range(120):
        px *= 1.002  # 꾸준히 상승
        candles.append(
            _make_candle(0 + i * 60_000, px * 0.999, px * 1.001, px * 0.998, px, 100.0)
        )

    s = build_signal(
        market="KRW-HOT",
        candles=candles,
        notional_ratio=10.0,
        spread_pct=0.0005,
        depth_ratio=10.0,
        btc_regime_ok=True,
        notional_ratio_min=0.9,
    )

    assert s.tradable is False
    assert "REJECT_RSI_HOT" in (s.note or "")


def test_build_signal_notional_ratio_veto():
    candles = []
    px = 100.0
    for i in range(120):
        px *= 1.0005
        candles.append(
            _make_candle(0 + i * 60_000, px * 0.999, px * 1.001, px * 0.998, px, 50.0)
        )

    s = build_signal(
        market="KRW-LOWVOL",
        candles=candles,
        notional_ratio=0.1,
        spread_pct=0.0005,
        depth_ratio=10.0,
        btc_regime_ok=True,
        notional_ratio_min=0.9,
    )

    assert s.tradable is False
    assert "REJECT_VOL" in (s.note or "")


def test_build_signal_mtf_veto():
    candles = []
    px = 100.0
    for i in range(120):
        px *= 1.0008
        candles.append(
            _make_candle(0 + i * 60_000, px * 0.999, px * 1.001, px * 0.998, px, 120.0)
        )

    s = build_signal(
        market="KRW-MTF",
        candles=candles,
        notional_ratio=10.0,
        spread_pct=0.0005,
        depth_ratio=10.0,
        btc_regime_ok=True,
        notional_ratio_min=0.9,
        mtf_trend_ok=False,
    )

    assert s.tradable is False
    assert "REJECT_MTF" in (s.note or "")


def test_trend_value_falls_back_to_sma():
    values = [100.0 + i * 0.1 for i in range(5)]
    trend, source = _trend_value(values, period=10)

    assert trend is not None
    assert source == "SMA"


def test_adaptive_cutoff_changes_for_extremes(monkeypatch):
    candles = [_make_candle(0 + i * 60_000, 100, 101, 99, 100, 10) for i in range(120)]

    monkeypatch.setattr(
        signal_engine, "volatility_regime", lambda *_args, **_kwargs: "low"
    )

    cutoff_low, reasons_low = signal_engine._compute_adaptive_cutoff(
        base_cutoff=75,
        candles=candles,
        atr_pct=0.2,
    )
    assert cutoff_low == 65
    assert any("LOW_VOL_REGIME" in reason for reason in reasons_low)
    assert any("LOW_ATR" in reason for reason in reasons_low)

    monkeypatch.setattr(
        signal_engine, "volatility_regime", lambda *_args, **_kwargs: "high"
    )

    cutoff_high, reasons_high = signal_engine._compute_adaptive_cutoff(
        base_cutoff=75,
        candles=candles,
        atr_pct=3.5,
    )
    assert cutoff_high == 80
    assert any("HIGH_ATR" in reason for reason in reasons_high)
