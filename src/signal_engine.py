from dataclasses import dataclass
from .indicators import rsi, sma, ema, macd, bollinger_bands, atr

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
    # 동적 컷오프 정보 추가
    dynamic_cutoff: int = 80 

def build_signal(
    market: str,
    candles: list[dict],
    notional_ratio: float,
    spread_pct: float,
    depth_ratio: float,
    btc_regime_ok: bool,
    notional_ratio_min: float,
    mtf_trend_ok: bool = True,
) -> Signal:
    if len(candles) < 60:
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

    # 3. [Power] Volume Explosion - 25점
    # 현재 캔들 거래량이 지난 20개 평균 거래량의 2배 이상
    vol_ma20 = sma(volumes[:-1], 20) # 현재 봉 제외 평균
    if vol_ma20 > 0:
        vol_ratio = volumes[-1] / vol_ma20
        if vol_ratio >= 2.0:
            score += 25
            reasons.append(f"VOL_2x({vol_ratio:.1f})")
        elif vol_ratio >= 1.5:
            score += 15
            reasons.append(f"VOL_1.5x")

    # 4. [Confirmation] MACD Bullish - 10점
    # MACD 히스토그램이 양수이면서 증가 중일 것 (상승 가속도)
    macd_line, macd_sig, macd_hist = macd(closes)
    # 직전 히스토그램
    _, _, prev_hist = macd(closes[:-1])
    
    if macd_hist > 0 and macd_hist > prev_hist:
        score += 10
        reasons.append("MACD_ACCEL")
    elif macd_hist > 0:
        score += 5

    # 5. [Safety] RSI Filter (과열 방지) - 10점
    # RSI가 50 이상이되(상승세), 75는 넘지 말 것(단기 고점 위험)
    rsi_val = rsi(closes, 14)
    if 50 <= rsi_val <= 70:
        score += 10
        reasons.append(f"RSI_OK({rsi_val:.0f})")
    elif rsi_val > 70:
        # 점수 추가 없음 (과열)
        reasons.append(f"RSI_HOT({rsi_val:.0f})")
    elif rsi_val < 40:
        score -= 20 # 하락세 감점
        reasons.append("RSI_WEAK")

    # ---------------------------------------------------------
    # [NEW] Dynamic Threshold (시장 상황별 컷오프 조절)
    # ---------------------------------------------------------
    # ATR 기반 변동성 측정 (최근 14개 봉)
    atr_val = atr(candles, 14)
    atr_pct = atr_val / last_price if last_price > 0 else 0
    
    # 기본 컷오프 80점
    dynamic_cutoff = 80
    
    # 변동성이 매우 낮으면(0.5% 미만, 횡보장) -> 컷오프 70점으로 완화 (적극 진입)
    if atr_pct < 0.005:
        dynamic_cutoff = 70
        reasons.append(f"LOW_VOL(Cut{dynamic_cutoff})")
    # 변동성이 매우 크면(2.0% 이상, 급등락장) -> 컷오프 85점으로 강화 (신중 진입)
    elif atr_pct > 0.020:
        dynamic_cutoff = 85
        reasons.append(f"HIGH_VOL(Cut{dynamic_cutoff})")

    # ---------------------------------------------------------
    # Final Veto (절대 불가 조건)
    # ---------------------------------------------------------
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

    # E. 추격매수 Veto
    if "REJECT_CHASE" in reasons:
        strict_pass = False

    # F. 호가창 품질 (스프레드)
    # spread_pct, depth_ratio는 외부에서 계산됨
    exec_score = 0
    if spread_pct <= 0.002: exec_score = 10
    
    final_score = score + exec_score
    
    # 진입 기준점: 동적 컷오프 적용
    is_buy = strict_pass and (final_score >= dynamic_cutoff)

    note = "/".join(reasons)
    
    return Signal(
        market=market,
        score=final_score,
        breakout=(final_score >= dynamic_cutoff), # 호환성
        breakout_pct=0.0,
        notional_ratio=notional_ratio,
        spread_pct=spread_pct,
        depth_ratio=depth_ratio,
        momentum_3m=0.0,
        btc_regime_ok=btc_regime_ok,
        tradable=is_buy,
        note=f"SCORE={final_score:.0f} [{note}]",
        dynamic_cutoff=dynamic_cutoff, # 추가된 필드
    )
