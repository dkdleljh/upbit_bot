import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

KST = ZoneInfo("Asia/Seoul")


@dataclass
class MarketParts:
    quote: str
    base: str


def now_ms() -> int:
    return int(datetime.now(tz=ZoneInfo("UTC")).timestamp() * 1000)


def kst_now() -> datetime:
    return datetime.now(tz=KST)


def date_kst_str() -> str:
    return kst_now().strftime("%Y-%m-%d")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dirs(root: Path) -> None:
    for child in ["config", "data", "logs", "reports", "src", "tests"]:
        (root / child).mkdir(parents=True, exist_ok=True)


def parse_market(market: str) -> MarketParts:
    quote, base = market.split("-", 1)
    return MarketParts(quote=quote, base=base)


def clip(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def pct_change(cur: float, prev: float) -> float:
    if prev == 0:
        return 0.0
    return (cur - prev) / prev


def setup_logging(log_file: str) -> None:
    from logging.handlers import RotatingFileHandler

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=20 * 1024 * 1024,  # 20MB
        backupCount=10,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[file_handler, stream_handler],
    )


def seconds_until_kst(hour: int, minute: int) -> int:
    now = kst_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int((target - now).total_seconds())


def load_env_file(path: str = ".env", override: bool = False) -> None:
    """Very small .env loader (no external dependency).

    Supports lines like KEY=VALUE, ignores blanks/comments.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if not k:
                    continue
                if not override and os.getenv(k) is not None:
                    continue
                os.environ[k] = v
    except FileNotFoundError:
        return


def env_or_none(key: str) -> str | None:
    v = os.getenv(key)
    return v.strip() if v else None


def get_tick_size(price: float) -> float:
    """업비트 원화 마켓 호가 단위(Tick Size) 결정 규칙."""
    if price < 0.1: # 0.1원 미만 (예: 0.0012)
        return 0.0001
    if price < 1: # 1원 미만 (예: 0.123)
        return 0.001
    if price < 10: # 10원 미만 (예: 1.23)
        return 0.01
    if price < 100: # 100원 미만 (예: 12.3)
        return 0.1
    if price < 1000: # 1,000원 미만 (예: 123)
        return 1.0
    if price < 10000: # 1만원 미만 (예: 1230)
        return 5.0
    if price < 100000: # 10만원 미만 (예: 12300)
        return 10.0
    if price < 500000: # 50만원 미만 (예: 123000)
        return 50.0
    if price < 1000000: # 100만원 미만 (예: 523000)
        return 100.0
    if price < 2000000: # 200만원 미만 (예: 1523000)
        return 500.0
    # 200만원 이상 (예: BTC)
    return 1000.0


def adjust_price_to_tick(price: float) -> float:
    """가격을 호가 단위에 맞춰 내림(Floor) 처리.
    부동소수점 연산 오차 방지를 위해 decimal 사용 권장되나,
    여기서는 간단히 처리함.
    """
    if price <= 0:
        return 0.0
        
    tick = get_tick_size(price)
    
    # Python round()는 짝수 반올림(Banker's rounding)이라서 주의 필요.
    # 단순 버림(Floor)으로 처리하는 게 매수 시 더 안전함 (호가 이탈 방지)
    # 하지만 지정가 매도 시에는 '버림'하면 손해일 수 있으나, '주문 거부'보다는 나음.
    
    # 1. 정수 틱
    if tick >= 1.0:
        return float(int(price / tick) * int(tick))
    
    # 2. 소수점 틱 (부동소수점 오차 최소화)
    # 예: 12.34 -> 0.1단위 -> 12.3
    import math
    ratio = price / tick
    # 내림 처리 (매수 유리, 매도 시 약간 손해지만 체결 우선)
    adjusted = math.floor(ratio) * tick
    return float(f"{adjusted:.8f}")
