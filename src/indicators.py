from .utils import clip


def ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = alpha * v + (1 - alpha) * out
    return out


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def vwap(candles: list[dict]) -> float | None:
    """Volume Weighted Average Price - Day trader 필수 지표"""
    if not candles or len(candles) < 1:
        return None

    typical_prices = []
    volumes = []
    for c in candles:
        h = float(c.get("high", c.get("trade_price", 0)))
        l = float(c.get("low", c.get("trade_price", 0)))
        c_ = float(c.get("close", c.get("trade_price", 0)))
        v = float(c.get("volume", c.get("candle_acc_trade_volume", 0)) or 0)

        tp = (h + l + c_) / 3
        typical_prices.append(tp * v)
        volumes.append(v)

    if sum(volumes) == 0:
        return None
    return sum(typical_prices) / sum(volumes)


def adx(candles: list[dict], period: int = 14) -> tuple[float, float, float]:
    """Average Directional Index - 추세 강도 측정 (프로 트레이더 필수)

    Returns: (adx, plus_di, minus_di)
    - ADX > 25: 강한 추세
    - ADX < 20: 약세/횡보장
    - +DI > -DI: 상승 추세
    - -DI > +DI: 하락 추세
    """
    if len(candles) < period + 1:
        return 0.0, 0.0, 0.0

    high = [float(c["high"]) for c in candles]
    low = [float(c["low"]) for c in candles]
    close = [float(c["close"]) for c in candles]

    plus_dm = []
    minus_dm = []
    tr = []

    for i in range(1, len(candles)):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]

        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)

        tr.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )

    if len(tr) < period:
        return 0.0, 0.0, 0.0

    atr_val = sum(tr[-period:]) / period
    if atr_val == 0:
        return 0.0, 0.0, 0.0

    plus_di = (sum(plus_dm[-period:]) / period) / atr_val * 100
    minus_di = (sum(minus_dm[-period:]) / period) / atr_val * 100

    dx = (
        abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        if (plus_di + minus_di) > 0
        else 0
    )

    adx = dx
    return adx, plus_di, minus_di


def stochastic_rsi(
    closes: list[float], rsi_period: int = 14, k_period: int = 3, d_period: int = 3
) -> tuple[float, float]:
    """Stochastic RSI - RSI의 과매수/과매도 구간精确把握

    Returns: (k, d)
    - K > 80: 과매수 (매도 신호)
    - K < 20: 과매도 (매수 신호)
    """
    if len(closes) < rsi_period + 1:
        return 50.0, 50.0

    rsi_vals = []
    for i in range(rsi_period - 1, len(closes)):
        segment = closes[: i + 1]
        r = rsi(segment, rsi_period)
        rsi_vals.append(r)

    if len(rsi_vals) < k_period:
        return 50.0, 50.0

    lowest = min(rsi_vals[-k_period:])
    highest = max(rsi_vals[-k_period:])
    range_ = highest - lowest

    if range_ == 0:
        k = 50.0
    else:
        k = (rsi_vals[-1] - lowest) / range_ * 100

    d = k

    return k, d


def support_resistance(
    candles: list[dict], lookback: int = 20
) -> tuple[list[float], list[float]]:
    """Support and Resistance Levels - 핵심 가격 구간 탐지

    Returns: (support_levels, resistance_levels)
    """
    if len(candles) < lookback:
        return [], []

    highs = [float(c["high"]) for c in candles[-lookback:]]
    lows = [float(c["low"]) for c in candles[-lookback:]]

    resistance = []
    support = []

    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            resistance.append(highs[i])
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            support.append(lows[i])

    support.sort()
    resistance.sort()

    return support, resistance


def atr_percent(candles: list[dict], period: int = 14) -> float:
    """ATR Percentage - 변동성 Relative Measure

    Returns: ATR as percentage of current price
    """
    if len(candles) < period + 1:
        return 0.0

    atr_val = atr(candles, period)
    current_price = float(candles[-1]["close"])

    if current_price == 0:
        return 0.0

    return (atr_val / current_price) * 100


def market_structure(candles: list[dict], lookback: int = 10) -> str:
    """Market Structure Detection - 추세 구조 파악

    Returns: 'uptrend', 'downtrend', 'ranging'
    """
    if len(candles) < lookback:
        return "ranging"

    highs = [float(c["high"]) for c in candles[-lookback:]]
    lows = [float(c["low"]) for c in candles[-lookback:]]

    hh = max(highs)
    ll = min(lows)
    current = float(candles[-1]["close"])

    range_pct = (hh - ll) / hh * 100

    if range_pct < 2:
        closes_slice = [float(c["close"]) for c in candles[-lookback:]]
        ema_long = ema(closes_slice, min(lookback, 20))
        ema_short = ema(closes_slice, 5)
        if ema_long and current > ema_long:
            return "uptrend"
        elif ema_short and current > ema_short:
            return "uptrend"
        else:
            return "downtrend"

    if current > hh * 0.98:
        return "uptrend"
    elif current < ll * 1.02:
        return "downtrend"

    return "ranging"


def volume_profile(
    candles: list[dict], bins: int = 20
) -> tuple[float, float, list[float]]:
    """Volume Profile - 거래량 집중 구간 파악

    Returns: (poc_price, vwap, volume_levels)
    """
    if len(candles) < bins:
        return 0.0, 0.0, []

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [
        float(c.get("volume", c.get("candle_acc_trade_volume", 0)) or 0)
        for c in candles
    ]

    price_range = max(highs) - min(lows)
    bin_size = price_range / bins

    if bin_size == 0:
        return 0.0, 0.0, []

    min_price = min(lows)
    vol_bins = [0.0] * bins

    for i, c in enumerate(candles):
        p = closes[i]
        v = volumes[i]
        bin_idx = int((p - min_price) / bin_size)
        if 0 <= bin_idx < bins:
            vol_bins[bin_idx] += v

    max_vol_bin = max(vol_bins) if vol_bins else 0
    poc_idx = vol_bins.index(max_vol_bin) if max_vol_bin > 0 else bins // 2
    poc_price = min_price + (poc_idx + 0.5) * bin_size

    vwap_val = vwap(candles) or 0.0

    return poc_price, vwap_val, vol_bins


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float]:
    """MACD, Signal, Histogram 반환."""
    if len(values) < slow + signal:
        return 0.0, 0.0, 0.0

    # 1. Calculate Fast & Slow EMA series
    ema_fast = []
    alpha_f = 2 / (fast + 1)
    val = sum(values[:fast]) / fast  # First value is SMA
    ema_fast.append(val)
    for v in values[fast:]:
        val = alpha_f * v + (1 - alpha_f) * val
        ema_fast.append(val)

    ema_slow = []
    alpha_s = 2 / (slow + 1)
    val = sum(values[:slow]) / slow
    ema_slow.append(val)
    for v in values[slow:]:
        val = alpha_s * v + (1 - alpha_s) * val
        ema_slow.append(val)

    # 2. Calculate MACD Line (Fast - Slow)
    # ema_slow의 길이에 맞춰 ema_fast를 뒤에서 자름
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]

    # 3. Calculate Signal Line (EMA of MACD Line)
    if len(macd_line) < signal:
        return 0.0, 0.0, 0.0

    alpha_sig = 2 / (signal + 1)
    sig_val = sum(macd_line[:signal]) / signal  # SMA initialization

    # Calculate last signal value
    for m in macd_line[signal:]:
        sig_val = alpha_sig * m + (1 - alpha_sig) * sig_val

    current_macd = macd_line[-1]
    current_signal = sig_val
    current_hist = current_macd - current_signal

    return current_macd, current_signal, current_hist


def bollinger_bands(
    values: list[float], period: int = 20, multiplier: float = 2.0
) -> tuple[float, float, float]:
    """Upper, Middle, Lower 밴드 반환."""
    if len(values) < period:
        return 0.0, 0.0, 0.0

    # SMA
    mid = sum(values[-period:]) / period

    # StdDev
    variance = sum((x - mid) ** 2 for x in values[-period:]) / period
    std_dev = variance**0.5

    upper = mid + (std_dev * multiplier)
    lower = mid - (std_dev * multiplier)

    return upper, mid, lower


def rsi(series: list[float], period: int = 14) -> float:
    """Relative Strength Index (RSI) 계산."""
    if len(series) < period + 1:
        return 50.0

    deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
    gains = [x if x > 0 else 0 for x in deltas]
    losses = [-x if x < 0 else 0 for x in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100.0

    # Wilder's smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period


def highest_high(candles: list[dict], lookback: int) -> float:
    if len(candles) < lookback:
        return 0.0
    return max(float(x["high"]) for x in candles[-lookback:])


def breakout_pct(last_close: float, ref_high: float) -> float:
    if ref_high <= 0:
        return 0.0
    return max(0.0, (last_close - ref_high) / ref_high)


def stop_pct_from_atr(
    entry_price: float, atr_value: float, atr_mult: float, low: float, high: float
) -> float:
    if entry_price <= 0:
        return high
    return clip((atr_value * atr_mult) / entry_price, low, high)


def volatility_regime(candles: list[dict], period: int = 14) -> str:
    if len(candles) < period + 10:
        return "normal"

    current_atr_pct = atr_percent(candles, period)

    if current_atr_pct == 0:
        return "normal"

    hist_period = min(50, len(candles) - period)
    if hist_period < 20:
        return "normal"

    hist_atr_vals = []
    for i in range(period, len(candles) - period, period):
        segment = candles[max(0, i - period) : i]
        hist_atr_vals.append(atr_percent(segment, period))

    if not hist_atr_vals:
        return "normal"

    avg_hist_atr = sum(hist_atr_vals) / len(hist_atr_vals)

    if current_atr_pct > avg_hist_atr * 1.5:
        return "high"
    elif current_atr_pct < avg_hist_atr * 0.6:
        return "low"

    return "normal"


def market_regime(candles: list[dict], lookback: int = 20) -> dict:
    if len(candles) < lookback:
        return {
            "trend": "neutral",
            "volatility": "normal",
            "strength": 50,
            "signal": "neutral",
        }

    closes = [float(c["close"]) for c in candles[-lookback:]]

    ema_9 = ema(closes, 9)
    ema_21 = ema(closes, 21)
    ema_50 = ema(closes, min(50, len(closes))) if len(closes) >= 50 else None

    trend = "neutral"
    if ema_9 and ema_21:
        if ema_9 > ema_21:
            if ema_50 and ema_21 > ema_50:
                trend = "bull"
            else:
                trend = "bull"
        else:
            if ema_50 and ema_21 < ema_50:
                trend = "bear"
            else:
                trend = "bear"

    volatility = volatility_regime(candles)

    adx_val, plus_di, minus_di = adx(candles, 14)
    strength = min(100, adx_val * 4)

    signal = "neutral"
    if trend == "bull" and volatility != "high" and strength > 40:
        signal = "favorable"
    elif trend == "bear" and volatility != "high" and strength > 40:
        signal = "favorable"
    elif volatility == "high":
        signal = "unfavorable"

    return {
        "trend": trend,
        "volatility": volatility,
        "strength": strength,
        "signal": signal,
    }


def adaptive_atr_multiplier(regime: str, base_mult: float = 1.6) -> float:
    if regime == "high":
        return base_mult * 1.3
    elif regime == "low":
        return base_mult * 0.8
    return base_mult


def rsi_divergence(candles: list[dict], period: int = 14, lookback: int = 5) -> str:
    if len(candles) < period + lookback + 1:
        return "none"

    closes = [float(c["close"]) for c in candles]
    rsi_vals = []
    for i in range(period, len(closes)):
        rsi_vals.append(rsi(closes[: i + 1], period))

    if len(rsi_vals) < lookback + 1:
        return "none"

    recent_rsi = rsi_vals[-lookback:]
    recent_prices = closes[-lookback:]

    price_low = min(recent_prices)
    price_high = max(recent_prices)
    rsi_low = min(recent_rsi)
    rsi_high = max(recent_rsi)

    price_trend = recent_prices[-1] - recent_prices[0]
    rsi_trend = recent_rsi[-1] - recent_rsi[0]

    if price_trend < 0 and rsi_trend > 0:
        return "bullish"
    elif price_trend > 0 and rsi_trend < 0:
        return "bearish"

    return "none"


def candle_pattern(candles: list[dict]) -> str:
    if len(candles) < 2:
        return "none"

    curr = candles[-1]
    prev = candles[-2]

    o = float(curr["open"])
    h = float(curr["high"])
    l = float(curr["low"])
    c = float(curr["close"])
    po = float(prev["open"])
    pc = float(prev["close"])

    body = abs(c - o)
    upper_wick = h - max(c, o)
    lower_wick = min(c, o) - l

    if body == 0:
        return "none"

    total_range = h - l
    if total_range == 0:
        return "none"

    body_ratio = body / total_range
    upper_ratio = upper_wick / body if body > 0 else 0
    lower_ratio = lower_wick / body if body > 0 else 0

    if lower_wick > body * 2 and upper_wick < body:
        return "hammer"
    elif upper_wick > body * 2 and lower_wick < body:
        return "shooting_star"

    bullish_engulfing = c > po and o < pc and c > po and o < pc and c - o > pc - po
    bearish_engulfing = c < po and o > pc and o > pc and c < po and o - c > po - pc

    if bullish_engulfing:
        return "bullish_engulfing"
    elif bearish_engulfing:
        return "bearish_engulfing"

    if body_ratio > 0.7:
        if c > o and c > pc:
            return "strong_bullish"
        elif c < o and c < pc:
            return "strong_bearish"

    return "none"


def momentum_score(candles: list[dict], lookback: int = 10) -> float:
    if len(candles) < lookback:
        return 0.0

    closes = [float(c["close"]) for c in candles[-lookback:]]
    volumes = [
        float(c.get("volume", c.get("candle_acc_trade_volume", 0.0)) or 0.0)
        for c in candles[-lookback:]
    ]

    if len(closes) < 2:
        return 0.0

    price_change = (closes[-1] - closes[0]) / closes[0] * 100

    avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    vol_ratio = volumes[-1] / max(avg_vol, 1e-8)

    high = max(closes)
    low = min(closes)
    range_pct = (high - low) / low * 100 if low > 0 else 0

    ema_5 = ema(closes, 5)
    ema_10 = ema(closes, 10)
    ema_momentum = 0
    if ema_5 and ema_10:
        ema_momentum = ((ema_5 - ema_10) / ema_10) * 100

    score = (
        (price_change * 2) + (vol_ratio * 10) + (range_pct * 0.5) + (ema_momentum * 3)
    )

    return clip(score, -50, 50)


def order_flow_imbalance(orderbook: dict, levels: int = 5) -> float:
    if not orderbook:
        return 0.0

    units = orderbook.get("orderbook_units", [])[:levels]
    if not units:
        return 0.0

    bid_volume = 0.0
    ask_volume = 0.0

    for i, u in enumerate(units):
        bid_size = float(u.get("bid_size", 0.0))
        ask_size = float(u.get("ask_size", 0.0))

        bid_volume += bid_size * (levels - i)
        ask_volume += ask_size * (levels - i)

    total = bid_volume + ask_volume
    if total == 0:
        return 0.0

    imbalance = (bid_volume - ask_volume) / total

    return clip(imbalance, -1.0, 1.0)


def liquidity_pressure(candles: list[dict], lookback: int = 20) -> str:
    if len(candles) < lookback:
        return "normal"

    closes = [float(c["close"]) for c in candles[-lookback:]]
    volumes = [
        float(c.get("volume", c.get("candle_acc_trade_volume", 0.0)) or 0.0)
        for c in candles[-lookback:]
    ]

    current_price = closes[-1]
    current_vol = volumes[-1]

    avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)

    recent_high = max(closes[-5:])
    recent_low = min(closes[-5:])

    at_high = current_price >= recent_high * 0.99
    at_low = current_price <= recent_low * 1.01

    high_vol_at_high = current_vol > avg_vol * 1.5 and at_high
    high_vol_at_low = current_vol > avg_vol * 1.5 and at_low

    if high_vol_at_high:
        return "liquidity_grab_up"
    elif high_vol_at_low:
        return "liquidity_grab_down"

    return "normal"


def trading_session() -> str:
    from .utils import now_ms
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    hour = now.hour

    if 0 <= hour < 7:
        return "asian_low_vol"
    elif 7 <= hour < 9:
        return "asian_open"
    elif 9 <= hour < 13:
        return "european_session"
    elif 13 <= hour < 16:
        return "overlap"
    elif 16 <= hour < 21:
        return "us_session"
    elif 21 <= hour < 24:
        return "us_close"

    return "unknown"


def is_optimal_trading_time() -> bool:
    session = trading_session()
    return session in ["european_session", "overlap", "us_session"]
