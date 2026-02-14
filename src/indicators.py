from .utils import clip


def ema(values: list[float], period: int) -> float | None:
    if not values:
        return None  # 테스트 기대치에 맞춤
    alpha = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """MACD, Signal, Histogram 반환."""
    if len(values) < slow + signal:
        return 0.0, 0.0, 0.0

    # 1. Calculate Fast & Slow EMA series
    ema_fast = []
    alpha_f = 2 / (fast + 1)
    val = sum(values[:fast]) / fast # First value is SMA
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
    sig_val = sum(macd_line[:signal]) / signal # SMA initialization
    
    # Calculate last signal value
    for m in macd_line[signal:]:
        sig_val = alpha_sig * m + (1 - alpha_sig) * sig_val
    
    current_macd = macd_line[-1]
    current_signal = sig_val
    current_hist = current_macd - current_signal
    
    return current_macd, current_signal, current_hist


def bollinger_bands(values: list[float], period: int = 20, multiplier: float = 2.0) -> tuple[float, float, float]:
    """Upper, Middle, Lower 밴드 반환."""
    if len(values) < period:
        return 0.0, 0.0, 0.0
    
    # SMA
    mid = sum(values[-period:]) / period
    
    # StdDev
    variance = sum((x - mid) ** 2 for x in values[-period:]) / period
    std_dev = variance ** 0.5
    
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


def stop_pct_from_atr(entry_price: float, atr_value: float, atr_mult: float, low: float, high: float) -> float:
    if entry_price <= 0:
        return high
    return clip((atr_value * atr_mult) / entry_price, low, high)
