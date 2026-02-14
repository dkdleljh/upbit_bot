import logging
import os
from datetime import datetime

LOGGER = logging.getLogger(__name__)


class Reporter:
    """운영용 간단 리포터.

    중요:
    - 리포터는 **절대 봇을 죽이면 안 됨** (실패해도 로그만 남기고 종료)
    - DB 스키마와 반드시 일치해야 함
    """

    def __init__(self, cfg, storage, report_dir):
        self.cfg = cfg
        self.storage = storage
        self.report_dir = report_dir
        os.makedirs(report_dir, exist_ok=True)

    async def start(self):
        LOGGER.info("Reporter started (Markdown Mode).")

    def export_today(self):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            report_path = os.path.join(self.report_dir, f"Daily_Report_{today}.md")

            # 오늘 00:00 KST 기준(로컬타임 가정)
            start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000

            trades = self.storage.query(
                "SELECT ts_ms, market, side, qty, order_value_krw, request_price, fill_price, fee, slippage_pct, reason, status "
                "FROM trades WHERE ts_ms >= ? ORDER BY ts_ms ASC",
                (start_of_day,),
            )

            events = self.storage.query(
                "SELECT ts_ms, level, event, market, details FROM runtime_events WHERE ts_ms >= ? ORDER BY ts_ms ASC",
                (start_of_day,),
            )

            buy_count = sum(1 for t in trades if t["side"] == "BUY")
            sell_count = sum(1 for t in trades if t["side"] == "SELL")

            # 간단 FIFO 기반 실현손익(대략치)
            # - 마켓별로 "보유수량"과 "평단"을 추적
            inv = {}  # market -> {qty, avg}
            realized = 0.0
            wins = 0
            sell_trades = 0

            for t in trades:
                m = t["market"]
                side = t["side"]
                qty = float(t["qty"])
                fp = float(t["fill_price"])
                fee = float(t["fee"])

                if side == "BUY":
                    pos = inv.get(m, {"qty": 0.0, "avg": 0.0})
                    old_q = pos["qty"]
                    old_avg = pos["avg"]
                    new_q = old_q + qty
                    new_avg = ((old_q * old_avg) + (qty * fp) + fee) / max(new_q, 1e-12)
                    inv[m] = {"qty": new_q, "avg": new_avg}

                elif side == "SELL":
                    pos = inv.get(m, {"qty": 0.0, "avg": 0.0})
                    use_q = min(pos["qty"], qty)
                    if use_q > 0:
                        pnl = (fp - pos["avg"]) * use_q - fee
                        realized += pnl
                        sell_trades += 1
                        if pnl > 0:
                            wins += 1
                        pos["qty"] = max(0.0, pos["qty"] - use_q)
                        inv[m] = pos

            win_rate = (wins / sell_trades * 100.0) if sell_trades else 0.0

            last_eq = self.storage.query_one(
                "SELECT equity_krw, unrealized_pnl_krw, krw_balance, krw_locked, asset_value_krw, note "
                "FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1"
            )
            equity = float(last_eq["equity_krw"]) if last_eq else 0.0
            unreal = float(last_eq["unrealized_pnl_krw"]) if last_eq else 0.0

            with open(report_path, "w", encoding="utf-8") as f:
                f.write(f"# Upbit Bot Daily Report ({today})\n\n")
                f.write("## Asset\n")
                f.write(f"- Equity: `{equity:,.0f} KRW`\n")
                f.write(f"- Unrealized: `{unreal:,.0f} KRW`\n\n")

                f.write("## Trades\n")
                f.write(f"- Total: {len(trades)} (BUY {buy_count} / SELL {sell_count})\n")
                f.write(f"- Realized PnL (FIFO approx): `{realized:,.0f} KRW`\n")
                f.write(f"- Win rate (SELL only): {win_rate:.1f}%\n\n")

                f.write("## Runtime Events\n")
                if not events:
                    f.write("- (none)\n\n")
                else:
                    # 이벤트별 카운트 요약
                    counts = {}
                    for ev in events:
                        k = str(ev["event"])
                        counts[k] = counts.get(k, 0) + 1
                    top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
                    f.write("- Counts:\n")
                    for k, n in top:
                        f.write(f"  - {k}: {n}\n")
                    f.write("\n")

                    # 주문 오류 Top3(원인)
                    order_err = {}
                    for ev in events:
                        if str(ev["event"]) == "ORDER_ERROR":
                            det = (ev["details"] or "").strip() or "unknown"
                            order_err[det] = order_err.get(det, 0) + 1
                    if order_err:
                        f.write("- ORDER_ERROR Top3:\n")
                        for det, n in sorted(order_err.items(), key=lambda x: (-x[1], x[0]))[:3]:
                            f.write(f"  - {det}: {n}\n")
                        f.write("\n")

                    # 최근 이벤트(최대 30개)
                    f.write("### Recent (last 30)\n")
                    f.write("| time | level | event | market | details |\n")
                    f.write("|---|---|---|---|---|\n")
                    for ev in events[-30:]:
                        dt = datetime.fromtimestamp(ev["ts_ms"] / 1000).strftime("%H:%M:%S")
                        mk = ev["market"] or ""
                        det = (ev["details"] or "").replace("\n", " ")
                        if len(det) > 120:
                            det = det[:120] + "..."
                        f.write(f"| {dt} | {ev['level']} | {ev['event']} | {mk} | {det} |\n")
                    f.write("\n")

                f.write("## Details\n")
                f.write("| time | market | side | req | fill | qty | fee | reason | status |\n")
                f.write("|---|---|---:|---:|---:|---:|---:|---|---|\n")
                for t in trades:
                    dt = datetime.fromtimestamp(t["ts_ms"] / 1000).strftime("%H:%M:%S")
                    f.write(
                        f"| {dt} | {t['market']} | {t['side']} | {t['request_price']:.2f} | {t['fill_price']:.2f} | {t['qty']:.8f} | {t['fee']:.2f} | {t['reason'] or ''} | {t['status']} |\n"
                    )

            LOGGER.info("Report generated: %s", report_path)

        except Exception as e:
            LOGGER.error("Report generation failed: %s", e)
            return
