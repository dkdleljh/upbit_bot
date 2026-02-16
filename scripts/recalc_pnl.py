#!/usr/bin/env python3
"""Recalculate realized PnL from trades table (single-source-of-truth).

- Uses FIFO lot matching per market
- Includes fees as stored in trades.fee
- Ignores canceled/unfilled trades (status not like '%done%')

Output:
- Prints summary to stdout
- Writes markdown report to reports/recalc_pnl_<timestamp>.md

NOTE: This script is intentionally conservative and simple. If you trade non-KRW
quotes, make sure `fee` is in quote currency and `fill_price*qty` is also in
quote currency. For KRW markets this holds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Tuple


@dataclass
class Trade:
    ts_ms: int
    mode: str
    market: str
    side: str
    qty: float
    fill_price: float
    fee: float
    slippage_pct: float
    reason: str
    status: str


@dataclass
class Lot:
    qty: float
    price: float
    fee_per_unit: float  # fee allocated per unit qty
    ts_ms: int


def _now_tag() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def load_trades(db_path: str, mode: str | None = None, *, since_ts_ms: int | None = None, until_ts_ms: int | None = None) -> List[Trade]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # IMPORTANT: In Upbit, canceled orders can be partially filled.
    # This bot stores those as filled_live_cancel with non-zero qty.
    # For PnL, we must include ANY row with qty>0 and fill_price>0, regardless of status.
    q = (
        "SELECT ts_ms, mode, market, side, qty, fill_price, fee, slippage_pct, COALESCE(reason,''), status "
        "FROM trades "
        "WHERE qty > 0 AND fill_price > 0 "
    )
    params: list = []
    if mode:
        q += " AND mode = ? "
        params.append(mode)
    if since_ts_ms is not None:
        q += " AND ts_ms >= ? "
        params.append(int(since_ts_ms))
    if until_ts_ms is not None:
        q += " AND ts_ms < ? "
        params.append(int(until_ts_ms))

    q += " ORDER BY ts_ms ASC, id ASC "

    rows = cur.execute(q, tuple(params)).fetchall()
    con.close()

    out: List[Trade] = []
    for r in rows:
        out.append(
            Trade(
                ts_ms=int(r[0]),
                mode=str(r[1]),
                market=str(r[2]),
                side=str(r[3]).upper(),
                qty=float(r[4]),
                fill_price=float(r[5]),
                fee=float(r[6]),
                slippage_pct=float(r[7]),
                reason=str(r[8] or ""),
                status=str(r[9]),
            )
        )
    return out


def fifo_recalc(trades: List[Trade]) -> dict:
    # inventory lots per market
    lots: Dict[str, Deque[Lot]] = defaultdict(deque)

    realized_pnl = 0.0
    realized_pnl_by_market: Dict[str, float] = defaultdict(float)
    realized_qty_by_market: Dict[str, float] = defaultdict(float)

    # diagnostics
    sells_without_inventory: List[Trade] = []

    for t in trades:
        if t.qty <= 0 or t.fill_price <= 0:
            continue

        if t.side == "BUY":
            # allocate fee per unit
            fee_per_unit = (t.fee / t.qty) if t.qty > 0 else 0.0
            lots[t.market].append(Lot(qty=t.qty, price=t.fill_price, fee_per_unit=fee_per_unit, ts_ms=t.ts_ms))

        elif t.side == "SELL":
            remaining = t.qty
            sell_fee_per_unit = (t.fee / t.qty) if t.qty > 0 else 0.0

            if not lots[t.market]:
                sells_without_inventory.append(t)
                continue

            while remaining > 1e-12 and lots[t.market]:
                lot = lots[t.market][0]
                use = min(remaining, lot.qty)

                # cost basis includes buy fee allocated
                cost = use * lot.price + use * lot.fee_per_unit
                proceeds = use * t.fill_price - use * sell_fee_per_unit
                pnl = proceeds - cost

                realized_pnl += pnl
                realized_pnl_by_market[t.market] += pnl
                realized_qty_by_market[t.market] += use

                lot.qty -= use
                remaining -= use

                if lot.qty <= 1e-12:
                    lots[t.market].popleft()

            # If we couldn't match fully, keep a record
            if remaining > 1e-8:
                sells_without_inventory.append(
                    Trade(
                        ts_ms=t.ts_ms,
                        mode=t.mode,
                        market=t.market,
                        side=t.side,
                        qty=remaining,
                        fill_price=t.fill_price,
                        fee=t.fee * (remaining / max(t.qty, 1e-12)),
                        slippage_pct=t.slippage_pct,
                        reason=t.reason + "|UNMATCHED",
                        status=t.status,
                    )
                )

    # remaining inventory valuation is NOT included in realized pnl
    inventory_value = 0.0
    inventory_qty = 0.0
    for m, dq in lots.items():
        for lot in dq:
            inventory_qty += lot.qty
            inventory_value += lot.qty * lot.price

    return {
        "realized_pnl": realized_pnl,
        "realized_pnl_by_market": dict(realized_pnl_by_market),
        "realized_qty_by_market": dict(realized_qty_by_market),
        "open_inventory_qty": inventory_qty,
        "open_inventory_cost": inventory_value,
        "sells_without_inventory": sells_without_inventory,
    }


def slippage_stats(trades: List[Trade]) -> dict:
    by_market: Dict[str, List[float]] = defaultdict(list)
    by_market_buy: Dict[str, List[float]] = defaultdict(list)
    by_market_sell: Dict[str, List[float]] = defaultdict(list)

    all_vals: List[float] = []

    for t in trades:
        v = float(t.slippage_pct)
        all_vals.append(v)
        by_market[t.market].append(v)
        if t.side == "BUY":
            by_market_buy[t.market].append(v)
        elif t.side == "SELL":
            by_market_sell[t.market].append(v)

    def pct(xs: List[float], p: float) -> float:
        if not xs:
            return 0.0
        ys = sorted(xs)
        k = int(round((len(ys) - 1) * p))
        return float(ys[max(0, min(len(ys) - 1, k))])

    def agg(xs: List[float]) -> dict:
        if not xs:
            return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        return {
            "n": len(xs),
            "mean": sum(xs) / len(xs),
            "p50": pct(xs, 0.50),
            "p95": pct(xs, 0.95),
            "p99": pct(xs, 0.99),
            "max": max(xs),
        }

    per_market = {m: agg(vs) for m, vs in by_market.items()}
    per_market_buy = {m: agg(vs) for m, vs in by_market_buy.items()}
    per_market_sell = {m: agg(vs) for m, vs in by_market_sell.items()}

    return {
        "all": agg(all_vals),
        "per_market": per_market,
        "per_market_buy": per_market_buy,
        "per_market_sell": per_market_sell,
    }


def format_money(x: float) -> str:
    return f"{x:,.0f}"


def write_report(out_path: str, *, mode: str | None, pnl: dict, slip: dict) -> None:
    lines: List[str] = []
    lines.append(f"# Recalculated PnL Report ({_now_tag()})")
    lines.append("")
    lines.append(f"- db-source: trades(status like %done%) FIFO")
    lines.append(f"- mode filter: {mode or 'ALL'}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Realized PnL (quote): **{format_money(pnl['realized_pnl'])}**")
    lines.append(f"- Open inventory qty (units): {pnl['open_inventory_qty']:.8f}")
    lines.append(f"- Open inventory cost (quote): {format_money(pnl['open_inventory_cost'])}")
    lines.append("")

    lines.append("## Slippage (slippage_pct) overall")
    a = slip["all"]
    lines.append("")
    lines.append(f"- n={a['n']} mean={a['mean']*100:.3f}% p50={a['p50']*100:.3f}% p95={a['p95']*100:.3f}% p99={a['p99']*100:.3f}% max={a['max']*100:.3f}%")

    # Top tail markets by max
    lines.append("")
    lines.append("## Tail risk: top markets by max slippage")
    lines.append("")
    rows = sorted(((m, d["n"], d["max"]) for m, d in slip["per_market"].items()), key=lambda x: x[2], reverse=True)[:20]
    lines.append("| market | n | max_slip |")
    lines.append("|---|---:|---:|")
    for m, n, mx in rows:
        lines.append(f"| {m} | {n} | {mx*100:.3f}% |")

    lines.append("")
    lines.append("## Realized PnL by market (top absolute)")
    lines.append("")
    pm = pnl["realized_pnl_by_market"]
    rows2 = sorted(pm.items(), key=lambda kv: abs(kv[1]), reverse=True)[:30]
    lines.append("| market | realized_pnl |")
    lines.append("|---|---:|")
    for m, v in rows2:
        lines.append(f"| {m} | {format_money(v)} |")

    # Unmatched sells
    unmatched = pnl["sells_without_inventory"]
    if unmatched:
        lines.append("")
        lines.append("## WARNING: sells without inventory (needs investigation)")
        lines.append("")
        lines.append("| time | market | qty | fill | reason |")
        lines.append("|---|---|---:|---:|---|")
        for t in unmatched[:50]:
            ts = dt.datetime.fromtimestamp(t.ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"| {ts} | {t.market} | {t.qty:.8f} | {t.fill_price:.4f} | {t.reason} |")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trading_bot.db")
    ap.add_argument("--mode", default=None, help="Filter trades.mode (e.g., live/paper/backtest)")
    ap.add_argument("--since-hours", type=float, default=None, help="Only include trades in last N hours")
    ap.add_argument("--since-local", default=None, help="Local time 'YYYY-mm-dd HH:MM:SS' (Asia/Seoul) inclusive")
    ap.add_argument("--until-local", default=None, help="Local time 'YYYY-mm-dd HH:MM:SS' exclusive")
    args = ap.parse_args()

    since_ts_ms = None
    until_ts_ms = None

    if args.since_hours is not None:
        since_ts_ms = int((dt.datetime.now().timestamp() - float(args.since_hours) * 3600) * 1000)

    def parse_local(s: str | None) -> int | None:
        if not s:
            return None
        x = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        # treat as local time; convert to epoch
        return int(x.timestamp() * 1000)

    if args.since_local:
        since_ts_ms = parse_local(args.since_local)
    if args.until_local:
        until_ts_ms = parse_local(args.until_local)

    trades = load_trades(args.db, mode=args.mode, since_ts_ms=since_ts_ms, until_ts_ms=until_ts_ms)
    pnl = fifo_recalc(trades)
    slip = slippage_stats(trades)

    out_path = f"reports/recalc_pnl_{_now_tag()}.md"
    write_report(out_path, mode=args.mode, pnl=pnl, slip=slip)

    print("[recalc] trades(done):", len(trades))
    print("[recalc] realized_pnl:", pnl["realized_pnl"])
    print("[recalc] open_inventory_qty:", pnl["open_inventory_qty"])
    print("[recalc] open_inventory_cost:", pnl["open_inventory_cost"])
    print("[recalc] slippage overall:", slip["all"])
    print("[recalc] report:", out_path)
    if pnl["sells_without_inventory"]:
        print("[recalc][WARN] sells_without_inventory:", len(pnl["sells_without_inventory"]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
