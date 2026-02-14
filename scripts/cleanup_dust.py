"""Clean up Upbit dust positions by topping up to >= min_notional then selling.

⚠️ This script places REAL orders when API keys are present.

Flow:
- Load accounts
- For each non-KRW asset with KRW market available:
  - Estimate KRW value (balance * last_price)
  - If 0 < value < min_notional:
      - Market-buy (min_notional + buffer - value), capped by topup_max
      - Then market-sell ~98% of available base

Usage:
  source venv/bin/activate
  python scripts/cleanup_dust.py
  python scripts/cleanup_dust.py --dry-run
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

# allow running as a script
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.upbit_rest import UpbitRestClient
from src.utils import load_config, load_env_file


@dataclass
class DustItem:
    currency: str
    market: str
    qty: float
    price: float
    value_krw: float


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


async def main():
    args = parse_args()
    load_env_file(".env", override=False)
    cfg = load_config(args.config)

    rest = UpbitRestClient(
        access_key=os.getenv(cfg.get("env", {}).get("upbit_access_key", "UPBIT_ACCESS_KEY")),
        secret_key=os.getenv(cfg.get("env", {}).get("upbit_secret_key", "UPBIT_SECRET_KEY")),
    )

    if not rest.is_live_ready:
        raise SystemExit("UPBIT API 키가 없어 더스트 정리를 실행할 수 없습니다.")

    min_notional = float(cfg.get("min_notional_krw", 5000))
    dust_cfg = cfg.get("dust", {}) or {}
    buffer_krw = float(dust_cfg.get("topup_buffer_krw", 500))
    topup_max = float(dust_cfg.get("topup_max_krw", 20000))

    accounts = await rest.get_accounts()
    if not isinstance(accounts, list):
        raise SystemExit("계좌 정보를 가져오지 못했습니다")

    # Build markets list for tickers
    markets = []
    qty_map = {}
    for a in accounts:
        cur = str(a.get("currency") or "")
        if not cur or cur == "KRW":
            continue
        bal = float(a.get("balance") or 0.0)
        locked = float(a.get("locked") or 0.0)
        qty = max(0.0, bal - locked)
        if qty <= 0:
            continue
        m = f"KRW-{cur}"
        markets.append(m)
        qty_map[cur] = qty

    tickers = await rest.get_tickers(markets)
    px_by_market = {t["market"]: float(t.get("trade_price") or 0.0) for t in tickers}

    dust_items: list[DustItem] = []
    for cur, qty in qty_map.items():
        m = f"KRW-{cur}"
        px = px_by_market.get(m, 0.0)
        if px <= 0:
            continue
        val = qty * px
        if 0 < val < min_notional:
            dust_items.append(DustItem(cur, m, qty, px, val))

    dust_items.sort(key=lambda x: x.value_krw)

    if not dust_items:
        print("dust: none")
        await rest.aclose()
        return

    print(f"dust_candidates={len(dust_items)} min_notional={min_notional}")
    for d in dust_items:
        need = max(0.0, (min_notional + buffer_krw) - d.value_krw)
        need = min(need, topup_max)
        print(f"- {d.market}: value≈{d.value_krw:.0f}krw qty={d.qty:.12f} px={d.price:.0f} need≈{need:.0f}krw")

    if args.dry_run:
        print("dry-run: no orders placed")
        await rest.aclose()
        return

    # Execute cleanup sequentially
    for d in dust_items:
        need = max(0.0, (min_notional + buffer_krw) - d.value_krw)
        # 업비트 시장가 매수 최소 주문금액(5,000원) 보장
        if 0 < need < min_notional:
            need = min_notional
        need = max(0.0, min(need, topup_max))
        if need < 1000:
            continue

        print(f"topup BUY {d.market} krw={need:.0f}")
        buy = await rest.place_market_buy(d.market, need)
        if not buy or not buy.get("uuid"):
            print(f"  buy_failed {d.market}")
            continue

        # Wait for completion
        await asyncio.sleep(0.8)
        for _ in range(10):
            od = await rest.get_order(buy["uuid"])
            if od and od.get("state") in {"done", "cancel"}:
                break
            await asyncio.sleep(0.8)

        # Refresh available qty and sell
        accounts2 = await rest.get_accounts()
        sell_qty = None
        if isinstance(accounts2, list):
            for a in accounts2:
                if str(a.get("currency") or "") == d.currency:
                    bal = float(a.get("balance") or 0.0)
                    locked = float(a.get("locked") or 0.0)
                    sell_qty = max(0.0, bal - locked)
                    break

        if not sell_qty or sell_qty <= 0:
            print(f"  skip_sell qty=0 {d.market}")
            continue

        # 더스트 제거 목적이므로 가능한 한 전량 매도 시도(가용수량 기준)
        print(f"SELL {d.market} qty≈{sell_qty:.12f}")
        sell = await rest.place_market_sell(d.market, sell_qty)
        if not sell or not sell.get("uuid"):
            print(f"  sell_failed {d.market}")
            continue
        await asyncio.sleep(0.8)

    await rest.aclose()


if __name__ == "__main__":
    asyncio.run(main())
