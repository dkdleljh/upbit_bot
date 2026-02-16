from dataclasses import dataclass
from .indicators import (
    rsi, sma, ema, macd, bollinger_bands, atr,
    vwap, adx, stochastic_rsi, atr_percent, market_regime, volatility_regime,
    rsi_divergence, candle_pattern, momentum_score,
    order_flow_imbalance, liquidity_pressure, is_optimal_trading_time
)
import logging

LOGGER = logging.getLogger(__name__)


@dataclass
class Signal:
    market: str
    score: float
    breakout: bool
    breakout_pct: float
    notional_ratio: float
    spread_pct: float
    depth_ratio: float
    momentum_3m: float
    btc_regime_ok: bool
    tradable: bool
    note: str = ""
    dynamic_cutoff: int = 80
    vwap_distance: float = 0.0
    adx: float = 0.0
    trend_strength: str = "weak"
    market_regime_signal: str = "neutral"
    volatility_regime: str = "normal"

def build_signal_simple(
    market: str,
    candles: list[dict],
    notional_ratio: float,
    spread_pct: float,
    depth_ratio: float,
    btc_regime_ok: bool,
    notional_ratio_min: float,
    mtf_trend_ok: bool = True,
    orderbook: dict | None = None,
) -> Signal:
    """Simplified, lower-overfit entry model.

    Goal: reduce degrees of freedom while keeping the main idea:
    - trend + volume confirmation
    - avoid obvious chase/overheat
    - hard vetoes: BTC regime / MTF trend / notional

    This is intentionally *not* a classic volatility breakout; it's a conservative
    momentum/trend filter designed for live robustness.
    """

    if len(candles) < 60:
        return Signal(market, 0, False, 0, notional_ratio, spread_pct, depth_ratio, 0, btc_regime_ok, False, "candles < 60", 80)

    closes = [float(c["close"]) for c in candles]
    volumes = [float(c.get("volume", c.get("candle_acc_trade_volume", 0.0)) or 0.0) for c in candles]
    last_price = closes[-1]

    score = 0.0
    reasons: list[str] = []

    # Trend: EMA alignment (core)
    ema5 = ema(closes, 5)
    ema20 = ema(closes, 20)
    ema60 = ema(closes, 60)
    if ema5 and ema20 and ema60 and (ema5 > ema20 > ema60):
        score += 50
        reasons.append("EMA_GOLDEN")
    elif ema5 and ema20 and ema5 > ema20:
        score += 25
        reasons.append("EMA_SHORT_UP")
    else:
        reasons.append("EMA_NOT_UP")

    # Volume confirmation
    vol_ma20 = sma(volumes[:-1], 20)
    if vol_ma20 > 0:
        vr = volumes[-1] / vol_ma20
        if vr >= 2.0:
            score += 25
            reasons.append(f"VOL_2x({vr:.1f})")
        elif vr >= 1.5:
            score += 10
            reasons.append(f"VOL_1.5x({vr:.1f})")
        else:
            reasons.append(f"VOL_LOW({vr:.1f})")

    # RSI: avoid extreme overheat
    rsi_val = rsi(closes, 14)
    if 45 <= rsi_val <= 70:
        score += 15
        reasons.append(f"RSI_OK({rsi_val:.0f})")
    elif rsi_val > 72:
        reasons.append(f"REJECT_RSI_HOT({rsi_val:.0f})")
    elif rsi_val < 40:
        score -= 15
        reasons.append(f"RSI_WEAK({rsi_val:.0f})")

    # Execution bonus: tight spread
    if spread_pct <= 0.002:
        score += 10
        reasons.append("SPREAD_OK")

    # Hard vetoes (strict)
    strict_pass = True
    if not btc_regime_ok:
        strict_pass = False
        reasons.append("REJECT_BTC")

    if not mtf_trend_ok:
        strict_pass = False
        reasons.append("REJECT_MTF")

    if notional_ratio < notional_ratio_min:
        strict_pass = False
        reasons.append("REJECT_VOL")

    # Simple anti-chase: reject if last candle upper wick is too large
    op = float(candles[-1]["open"])
    hi = float(candles[-1]["high"])
    cl = float(candles[-1]["close"])
    body = abs(cl - op)
    upper_wick = hi - max(cl, op)
    if body > 0 and upper_wick > body * 2.5:
        strict_pass = False
        reasons.append("REJECT_WICK")

    if "REJECT_RSI_HOT" in "/".join(reasons):
        strict_pass = False

    dynamic_cutoff = 60  # simpler fixed threshold
    tradable = strict_pass and (score >= dynamic_cutoff)

    return Signal(
        market=market,
        score=float(score),
        breakout=(score >= dynamic_cutoff),
        breakout_pct=0.0,
        notional_ratio=notional_ratio,
        spread_pct=spread_pct,
        depth_ratio=depth_ratio,
        momentum_3m=0.0,
        btc_regime_ok=btc_regime_ok,
        tradable=tradable,
        note=f"SIMPLE score={score:.0f} [{'/'.join(reasons)}]",
        dynamic_cutoff=dynamic_cutoff,
        volatility_regime=volatility_regime(candles),
    )


def build_signal(
    market: str,
    candles: list[dict],
    notional_ratio: float,
    spread_pct: float,
    depth_ratio: float,
    btc_regime_ok: bool,
    notional_ratio_min: float,
    mtf_trend_ok: bool = True,
    orderbook: dict | None = None,
) -> Signal:
    if len(candles) < 60:
        LOGGER.warning(f"Insufficient candles for {market}: {len(candles)} < 60, returning zero signal")
        return Signal(market, 0, False, 0, notional_ratio, spread_pct, depth_ratio, 0, btc_regime_ok, False, "candles < 60", 80)

    closes = [float(c["close"]) for c in candles]
    # 캐시 캔들은 volume 키로 저장됩니다(Upbit 원본 키도 같이 넣어둘 수 있음)
    volumes = [float(c.get("volume", c.get("candle_acc_trade_volume", 0.0)) or 0.0) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    last_price = closes[-1]
    
    # ---------------------------------------------------------
    # Phase 2: Composite Strategy (종합 점수제)
    # 총점 100점 만점
    # ---------------------------------------------------------
    score = 0
    reasons = []

    # 1. [Trend] EMA 정배열 (Golden Cross) - 30점
    # 5일선 > 20일선 > 60일선 (단기 급등 추세 확인)
    ema5 = ema(closes, 5)
    ema20 = ema(closes, 20)
    ema60 = ema(closes, 60)
    
    if ema5 and ema20 and ema60:
        if ema5 > ema20 > ema60:
            score += 30
            reasons.append("EMA_GOLDEN")
        elif ema5 > ema20: # 최소한 단기는 정배열이어야 함
            score += 15
            reasons.append("EMA_SHORT_UP")

    # 2. [Momentum] Bollinger Band Breakout - 25점
    # 가격이 상단 밴드를 뚫었거나, 상단 밴드 근처(1% 이내)에 붙어서 상승 중
    # (승률형 보강) 밴드를 '과도하게 찢는' 구간은 되돌림(휩쏘) 확률이 커서 진입을 피합니다.
    bb_up, bb_mid, bb_low = bollinger_bands(closes, 20, 2.0)
    if bb_up > 0:
        dist_to_upper = (last_price - bb_up) / bb_up
        # 밴드 근처(-0.5%~+0.2%)만 가점. +0.2% 초과는 추격매수로 간주(승률↓)
        if -0.005 <= dist_to_upper <= 0.002:
            score += 25
            reasons.append("BB_BREAKOUT")
        elif dist_to_upper > 0.002:
            reasons.append("REJECT_CHASE")

    # 3. [Power] Volume Explosion - 25점 (개선: 150%+ 도 가점)
    vol_ma20 = sma(volumes[:-1], 20)
    if vol_ma20 > 0:
        vol_ratio = volumes[-1] / vol_ma20
        if vol_ratio >= 2.0:
            score += 25
            reasons.append(f"VOL_2x({vol_ratio:.1f})")
        elif vol_ratio >= 1.5:
            score += 15
            reasons.append(f"VOL_1.5x")

    # 4. [Confirmation] MACD Bullish - 10점
    macd_line, macd_sig, macd_hist = macd(closes)
    _, _, prev_hist = macd(closes[:-1])
    
    if macd_hist > 0 and macd_hist > prev_hist:
        score += 10
        reasons.append("MACD_ACCEL")
    elif macd_hist > 0:
        score += 5

    # 5. [Safety] RSI Filter (과열 방지) - 10점 + Faster RSI
    rsi_val = rsi(closes, 14)
    rsi_fast = rsi(closes, 9)
    if 50 <= rsi_val <= 70:
        score += 10
        reasons.append(f"RSI_OK({rsi_val:.0f})")
    elif rsi_val > 70:
        reasons.append(f"RSI_HOT({rsi_val:.0f})")
    elif rsi_val < 40:
        score -= 20
        reasons.append("RSI_WEAK")
    
    if rsi_fast < 35:
        score += 5
        reasons.append(f"RSI_FAST_OS({rsi_fast:.0f})")

    # 6. [NEW] RSI Divergence Detection
    rsi_div = rsi_divergence(candles)
    if rsi_div == 'bullish':
        score += 10
        reasons.append("RSI_DIV_BULL")
    elif rsi_div == 'bearish':
        score -= 10
        reasons.append("RSI_DIV_BEAR")

    # 7. [NEW] Candle Pattern Recognition
    pattern = candle_pattern(candles)
    if pattern == 'hammer':
        score += 10
        reasons.append("HAMMER")
    elif pattern == 'bullish_engulfing':
        score += 10
        reasons.append("ENGULF_BULL")
    elif pattern == 'shooting_star':
        score -= 10
        reasons.append("SHOOTING_STAR")
    elif pattern == 'bearish_engulfing':
        score -= 10
        reasons.append("ENGULF_BEAR")

    # 8. [NEW] Momentum Score
    mom_score = momentum_score(candles)
    if mom_score > 20:
        score += 10
        reasons.append(f"MOM_STRONG({mom_score:.0f})")
    elif mom_score < -10:
        score -= 10
        reasons.append(f"MOM_WEAK({mom_score:.0f})")

    vwap_val = vwap(candles)
    vwap_distance = 0.0
    if vwap_val:
        vwap_distance = (last_price - vwap_val) / vwap_val
        if vwap_distance < -0.002:
            score += 15
            reasons.append(f"VWAP_ABOVE({vwap_distance*100:.1f}%)")
        elif vwap_distance > 0.002:
            score += 5

    adx_val, plus_di, minus_di = adx(candles, 14)
    trend_strength = "weak"
    if adx_val > 25:
        if plus_di > minus_di:
            score += 15
            trend_strength = "strong_up"
            reasons.append(f"ADX_STRONG({adx_val:.0f}+DI)")
        elif minus_di > plus_di:
            score -= 10
            trend_strength = "strong_down"
            reasons.append(f"ADX_STRONG({adx_val:.0f}-DI)")
    elif adx_val > 15:
        trend_strength = "moderate"

    stoch_k, stoch_d = stochastic_rsi(closes)
    if stoch_k < 20:
        score += 10
        reasons.append(f"STOCH_OS({stoch_k:.0f})")
    elif stoch_k > 80:
        score -= 10
        reasons.append(f"STOCH_OB({stoch_k:.0f})")

    atr_pct = atr_percent(candles, 14)
    
    regime = market_regime(candles)
    vol_regime = volatility_regime(candles)
    market_regime_signal = regime['signal']
    
    dynamic_cutoff = 75
    
    if atr_pct < 0.5:
        dynamic_cutoff = 65
        reasons.append(f"LOW_VOL(Cut{dynamic_cutoff})")
    elif atr_pct > 2.0:
        dynamic_cutoff = 80
        reasons.append(f"HIGH_VOL(Cut{dynamic_cutoff})")
    
    if market_regime_signal == 'favorable':
        score += 10
        reasons.append(f"REGIME_FAVORABLE({regime['trend']})")
    elif market_regime_signal == 'unfavorable':
        score -= 10
        reasons.append(f"REGIME_UNFAVORABLE({regime['volatility']})")
    
    if vol_regime == 'high':
        score -= 5
        reasons.append("HIGH_VOLATILITY")
    elif vol_regime == 'low':
        score += 5
        reasons.append("LOW_VOLATILITY")

    # 9. [NEW] Order Flow Imbalance
    if orderbook:
        of_imbalance = order_flow_imbalance(orderbook)
        if of_imbalance > 0.3:
            score += 10
            reasons.append(f"ORDERFLOW_BULL({of_imbalance:.2f})")
        elif of_imbalance < -0.3:
            score -= 10
            reasons.append(f"ORDERFLOW_BEAR({of_imbalance:.2f})")

    # 10. [NEW] Liquidity Pressure Detection
    liq_pressure = liquidity_pressure(candles)
    if liq_pressure == 'liquidity_grab_down':
        score += 10
        reasons.append("LIQ_GRAB_DOWN")
    elif liq_pressure == 'liquidity_grab_up':
        score -= 10
        reasons.append("LIQ_GRAB_UP")

    # 11. [NEW] Trading Session Filter
    if not is_optimal_trading_time():
        score -= 5
        reasons.append("OFF_HOURS")

    strict_pass = True
    
    # A. 윗꼬리 금지 (상승하다 처박는 중이면 절대 진입 금지)
    op = float(candles[-1]["open"])
    hi = float(candles[-1]["high"])
    lo = float(candles[-1]["low"])
    cl = float(candles[-1]["close"])
    body = abs(cl - op)
    upper_wick = hi - max(cl, op)
    
    # 윗꼬리가 몸통보다 2배 이상 길면 탈락 (매도세 출현)
    if body > 0 and upper_wick > body * 2.5:
        strict_pass = False
        reasons.append("REJECT_WICK")

    # B. RSI 과열 Veto (승률형: 고점추격 방지)
    if rsi_val >= 72:
        strict_pass = False
        reasons.append("REJECT_RSI_HOT")

    # C. 비트코인 하락장 Veto (전체 시장 분위기)
    if not btc_regime_ok:
        strict_pass = False
        reasons.append("REJECT_BTC")

    # D. 거래량 비율 최소 조건
    if notional_ratio < notional_ratio_min:
        strict_pass = False
        reasons.append("REJECT_VOL")

    # E. 상위 타임프레임(5m) 추세 필터
    if not mtf_trend_ok:
        score -= 25
        strict_pass = False
        reasons.append("REJECT_MTF")

    if "REJECT_CHASE" in reasons:
        strict_pass = False

    if trend_strength == "strong_down":
        strict_pass = False
        reasons.append("REJECT_TREND_DOWN")

    exec_score = 0
    if spread_pct <= 0.002: exec_score = 10
    
    final_score = score + exec_score
    
    # 진입 기준점: 동적 컷오프 적용
    is_buy = strict_pass and (final_score >= dynamic_cutoff)

    note = "/".join(reasons)
    
    return Signal(
        market=market,
        score=final_score,
        breakout=(final_score >= dynamic_cutoff),
        breakout_pct=0.0,
        notional_ratio=notional_ratio,
        spread_pct=spread_pct,
        depth_ratio=depth_ratio,
        momentum_3m=0.0,
        btc_regime_ok=btc_regime_ok,
        tradable=is_buy,
        note=f"SCORE={final_score:.0f} [{note}]",
        dynamic_cutoff=dynamic_cutoff,
        vwap_distance=vwap_distance,
        adx=adx_val,
        trend_strength=trend_strength,
        market_regime_signal=market_regime_signal,
        volatility_regime=vol_regime,
    )
