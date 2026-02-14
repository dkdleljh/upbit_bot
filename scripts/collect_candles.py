"""Upbit candle collector (rate-limit friendly).

- Collects 1m candles into data/trading_bot.db (candles table)
- Uses UpbitRestClient with built-in throttling/backoff

Usage:
  source venv/bin/activate
  python scripts/collect_candles.py --days 7 --markets KRW-BTC,KRW-ETH
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# allow running as a script: python scripts/collect_candles.py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.storage import Storage
from src.upbit_rest import UpbitRestClient
from src.utils import load_config, load_env_file


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--days", type=int, default=7)
    p.add_argument(
        "--markets",
        default="KRW-BTC,KRW-ETH,KRW-XRP,KRW-SOL,KRW-USDT,KRW-BERA,KRW-VANA,KRW-AXS",
        help="comma separated",
    )
    return p.parse_args()


def to_utc_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_candle_time_utc(s: str) -> datetime:
    # Upbit returns e.g. 2026-02-10T06:00:00
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc)


async def collect_market(rest: UpbitRestClient, storage: Storage, market: str, days: int):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    to = end
    inserted = 0

    while to > start:
        data = await rest.get_candles_minutes(market, unit=1, count=200, to=to_utc_z(to))
        if not data:
            break

        rows = []
        oldest = None
        for c in data:
            t_utc = parse_candle_time_utc(c["candle_date_time_utc"])
            if t_utc < start:
                continue
            ts = t_utc.timestamp()
            rows.append(
                (
                    market,
                    ts,
                    float(c["opening_price"]),
                    float(c["high_price"]),
                    float(c["low_price"]),
                    float(c["trade_price"]),
                    float(c["candle_acc_trade_volume"]),
                    float(c["candle_acc_trade_price"]),
                )
            )
            oldest = t_utc if oldest is None else min(oldest, t_utc)

        if rows:
            storage.execute_many(
                """
                INSERT OR IGNORE INTO candles
                (market, timestamp, open, high, low, close, volume, value)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """.strip(),
                rows,
            )
            inserted += len(rows)

        if oldest is None:
            break

        # 다음 페이지: 가장 오래된 캔들보다 살짝 이전으로
        to = oldest - timedelta(seconds=1)

    return inserted


async def main():
    args = parse_args()

    load_env_file(".env", override=False)
    cfg = load_config(args.config)

    rest = UpbitRestClient()
    storage = Storage("data/trading_bot.db")

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]

    total = 0
    for m in markets:
        n = await collect_market(rest, storage, m, args.days)
        total += n
        print(f"{m}: inserted~{n}")
        await asyncio.sleep(1.0)  # market 간 쿨다운

    await rest.aclose()
    storage.close()
    print(f"done. inserted~{total}")


if __name__ == "__main__":
    asyncio.run(main())
