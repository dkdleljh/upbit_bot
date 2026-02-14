"""Generate live performance report from trading_bot.db.

- Reads `trades` table (mode='live')
- Computes realized PnL (SELL value - BUY cost - fees) using FIFO matching per market
- Reads latest `equity_snapshots` (mode='live') to include unrealized PnL + current equity
- Outputs a text report + per-day summary CSV under reports/

Usage:
  source venv/bin/activate
  python scripts/live_performance_report.py
  python scripts/live_performance_report.py --days 7

Note:
- Realized PnL is DB-based FIFO accounting.
- Unrealized PnL / equity are snapshot-based (mark-to-market).
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "trading_bot.db"
REPORT_DIR = ROOT / "reports"

KST = timezone(timedelta(hours=9))


@dataclass
class Lot:
    qty: float
    price: float
    fee: float
    ts_ms: int


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=0, help="최근 N일만(0이면 전체)")
    return p.parse_args()


def kst_date_str(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(KST)
    return dt.strftime("%Y-%m-%d")


def main():
    args = parse_args()
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    where = "mode='live'"
    params: tuple = ()
    if args.days and args.days > 0:
        start = datetime.now(tz=KST).date() - timedelta(days=args.days)
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=KST)
        start_ms = int(start_dt.astimezone(timezone.utc).timestamp() * 1000)
        where += " AND ts_ms >= ?"
        params = (start_ms,)

    rows = conn.execute(
        f"SELECT ts_ms, market, side, qty, fill_price, fee, status, reason FROM trades WHERE {where} ORDER BY ts_ms ASC",
        params,
    ).fetchall()

    snap = conn.execute(
        "SELECT ts_ms, equity_krw, krw_balance, krw_locked, asset_value_krw, cost_basis_krw, unrealized_pnl_krw, note "
        "FROM equity_snapshots WHERE mode='live' ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()

    # equity curve (daily close equity)
    snap_where = "mode='live'"
    snap_params: tuple = ()
    if args.days and args.days > 0:
        snap_where += " AND ts_ms >= ?"
        snap_params = params  # same start_ms

    snap_rows = conn.execute(
        f"SELECT ts_ms, equity_krw FROM equity_snapshots WHERE {snap_where} ORDER BY ts_ms ASC",
        snap_params,
    ).fetchall()

    daily_equity: dict[str, tuple[int, float]] = {}  # day -> (ts_ms, equity)
    for sr in snap_rows:
        ts_ms = int(sr["ts_ms"])
        day = kst_date_str(ts_ms)
        eq = float(sr["equity_krw"])
        prev = daily_equity.get(day)
        if prev is None or ts_ms >= prev[0]:
            daily_equity[day] = (ts_ms, eq)

    curve_days = sorted(daily_equity.keys())
    equity_curve = [(d, daily_equity[d][1]) for d in curve_days]

    # max drawdown on daily equity
    peak = None
    max_dd = 0.0
    for _, eq in equity_curve:
        if peak is None or eq > peak:
            peak = eq
        if peak and peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    # FIFO lots per market
    lots: dict[str, deque[Lot]] = defaultdict(deque)

    realized_by_day = defaultdict(float)
    trades_by_day = defaultdict(int)
    wins_by_day = defaultdict(int)
    losses_by_day = defaultdict(int)

    # market contribution
    realized_by_market = defaultdict(float)
    gross_by_market = defaultdict(float)
    trades_by_market = defaultdict(int)

    total_realized = 0.0
    total_gross = 0.0
    total_fees = 0.0
    total_trades = 0
    wins = 0
    losses = 0

    for r in rows:
        if (r["status"] or "").startswith("filled") is False:
            continue
        ts_ms = int(r["ts_ms"])
        day = kst_date_str(ts_ms)
        m = r["market"]
        side = r["side"]
        qty = float(r["qty"])
        px = float(r["fill_price"])
        fee = float(r["fee"])
        total_fees += fee

        if side == "BUY":
            lots[m].append(Lot(qty=qty, price=px, fee=fee, ts_ms=ts_ms))
        elif side == "SELL":
            # match against lots
            sell_qty = qty
            sell_value = sell_qty * px
            sell_fee = fee
            cost = 0.0
            buy_fee = 0.0

            while sell_qty > 1e-12 and lots[m]:
                lot = lots[m][0]
                take = min(sell_qty, lot.qty)
                cost += take * lot.price
                # allocate buy fee pro-rata
                if lot.qty > 0:
                    buy_fee += lot.fee * (take / lot.qty)
                lot.qty -= take
                sell_qty -= take
                if lot.qty <= 1e-12:
                    lots[m].popleft()

            # if we had no lots (manual holdings), treat cost=0 for safety
            gross = sell_value - cost
            pnl = (sell_value - sell_fee) - (cost + buy_fee)

            realized_by_day[day] += pnl
            total_realized += pnl
            total_gross += gross

            realized_by_market[m] += pnl
            gross_by_market[m] += gross
            trades_by_market[m] += 1

            trades_by_day[day] += 1
            total_trades += 1
            if pnl >= 0:
                wins_by_day[day] += 1
                wins += 1
            else:
                losses_by_day[day] += 1
                losses += 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # write per-day csv (realized)
    out_csv = REPORT_DIR / "live_performance_daily.csv"
    days = sorted(trades_by_day.keys())
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date_kst", "trades", "wins", "losses", "win_rate", "realized_pnl_krw"])
        for d in days:
            t = trades_by_day[d]
            w.writerow(
                [
                    d,
                    t,
                    wins_by_day[d],
                    losses_by_day[d],
                    (wins_by_day[d] / t) if t else 0.0,
                    round(realized_by_day[d], 2),
                ]
            )

    # write equity curve csv (unrealized included in equity)
    out_curve = REPORT_DIR / "equity_curve_daily.csv"
    with open(out_curve, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date_kst", "equity_krw"])
        for d, eq in equity_curve:
            w.writerow([d, round(eq, 2)])

    # write text report
    out_txt = REPORT_DIR / f"live_report_{datetime.now(tz=KST).strftime('%Y%m%d_%H%M%S')}.txt"
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("LIVE PERFORMANCE REPORT (realized + unrealized)\n")
        f.write("=" * 60 + "\n")
        f.write(f"generated_kst: {datetime.now(tz=KST).strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"rows_scanned: {len(rows)}\n")
        f.write(f"total_sell_trades: {total_trades}\n")
        f.write(f"win_rate: {(wins/total_trades) if total_trades else 0.0:.1%}\n")
        f.write(f"realized_pnl_krw (net): {total_realized:,.2f}\n")
        f.write(f"realized_pnl_krw (gross, excl fees): {total_gross:,.2f}\n")
        f.write(f"fees_total_krw: {total_fees:,.2f}\n")

        if equity_curve:
            start_eq = equity_curve[0][1]
            end_eq = equity_curve[-1][1]
            total_ret = ((end_eq - start_eq) / start_eq) if start_eq else 0.0
            f.write("\n")
            f.write("equity_curve_daily (includes unrealized):\n")
            f.write(f"  days: {len(equity_curve)}\n")
            f.write(f"  start_equity: {start_eq:,.2f}\n")
            f.write(f"  end_equity: {end_eq:,.2f}\n")
            f.write(f"  total_return: {total_ret:.2%}\n")
            f.write(f"  max_drawdown: {max_dd:.2%}\n")

        if snap:
            snap_ts = datetime.fromtimestamp(int(snap["ts_ms"]) / 1000, tz=timezone.utc).astimezone(KST)
            f.write("\n")
            f.write("latest_equity_snapshot:\n")
            f.write(f"  ts_kst: {snap_ts.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  equity_krw: {float(snap['equity_krw']):,.2f}\n")
            f.write(f"  krw_balance: {float(snap['krw_balance']):,.2f} (locked {float(snap['krw_locked']):,.2f})\n")
            f.write(f"  asset_value_krw: {float(snap['asset_value_krw']):,.2f}\n")
            f.write(f"  cost_basis_krw: {float(snap['cost_basis_krw']):,.2f}\n")
            f.write(f"  unrealized_pnl_krw: {float(snap['unrealized_pnl_krw']):,.2f}\n")
            if snap["note"]:
                f.write(f"  note: {snap['note']}\n")

        # market contribution
        if trades_by_market:
            f.write("\n")
            f.write("by-market (realized net):\n")
            ranked = sorted(realized_by_market.items(), key=lambda x: x[1], reverse=True)
            for m, pnl in ranked[:10]:
                f.write(
                    f"  - {m}: trades={trades_by_market[m]}, pnl_net={pnl:,.2f} KRW, pnl_gross={gross_by_market[m]:,.2f} KRW\n"
                )

        # open positions (latest snapshot in positions table)
        try:
            pos_ts_row = conn.execute("SELECT MAX(ts_ms) AS mx FROM positions WHERE mode='live'").fetchone()
            pos_ts = int(pos_ts_row["mx"]) if pos_ts_row and pos_ts_row["mx"] else 0
            if pos_ts:
                pos_rows = conn.execute(
                    "SELECT market, qty, entry_price, last_price, net_pnl_pct, hold_seconds, score "
                    "FROM positions WHERE mode='live' AND ts_ms=? ORDER BY net_pnl_pct DESC",
                    (pos_ts,),
                ).fetchall()
            else:
                pos_rows = []
        except Exception:
            pos_rows = []

        if pos_rows:
            f.write("\n")
            f.write("open_positions (latest bot snapshot):\n")
            for pr in pos_rows[:20]:
                m = pr["market"]
                qty = float(pr["qty"])
                ep = float(pr["entry_price"])
                lp = float(pr["last_price"])
                val = qty * lp
                f.write(
                    f"  - {m}: value≈{val:,.0f}krw qty={qty:.12f} entry={ep:,.0f} last={lp:,.0f} pnl={float(pr['net_pnl_pct']):.2%} hold={int(pr['hold_seconds'])}s score={float(pr['score']):.1f}\n"
                )

        f.write("\n")
        f.write("per-day (realized net):\n")
        for d in days[-14:]:
            t = trades_by_day[d]
            f.write(
                f"  - {d}: trades={t}, win_rate={(wins_by_day[d]/t) if t else 0.0:.1%}, pnl={realized_by_day[d]:,.2f} KRW\n"
            )

    print(f"wrote: {out_txt}")
    print(f"wrote: {out_csv}")
    print(f"wrote: {out_curve}")


if __name__ == "__main__":
    main()
