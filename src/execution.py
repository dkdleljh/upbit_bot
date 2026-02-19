import asyncio
import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

from .utils import now_ms, adjust_price_to_tick

LOGGER = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    ok: bool
    market: str
    side: str
    qty: float
    fill_price: float
    fee: float
    slippage_pct: float
    reason: str


class ExecutionEngine:
    def __init__(self, cfg: dict, storage, mode: str, rest_client=None):
        self.cfg = cfg
        self.storage = storage
        self.mode = mode
        self.rest = rest_client
        self.slip_hist = defaultdict(lambda: deque(maxlen=30))
        self.cooldown_until_ms: dict[str, int] = {}

        # 더스트(5,000원 미만) 처리용 쿨다운
        self._dust_cooldown_until_ms: dict[str, int] = {}
        # 손절 더스트(최소주문금액 미만) 반복 에러 루프 방지
        self._stoploss_under_notional_until_ms: dict[str, int] = {}

        # live 안전 기본값 (환경변수로 오버라이드)
        self.live_confirm_required = (
            os.getenv("UPBIT_LIVE_CONFIRM", "").strip().upper() == "YES"
        )
        self.kill_switch = os.getenv("UPBIT_KILL_SWITCH", "0").strip() == "1"
        self.max_order_krw = max(
            5000.0, float(os.getenv("UPBIT_MAX_ORDER_KRW", "100000"))
        )
        self.max_trades_per_day = max(
            1, int(os.getenv("UPBIT_MAX_TRADES_PER_DAY", "30"))
        )

    def _depth_ratio(
        self, orderbook: dict, order_value_krw: float, side: str = "BUY"
    ) -> float:
        units = orderbook.get("orderbook_units", [])[:3]
        if not units or order_value_krw <= 0:
            return 0.0
        if side == "BUY":
            total = sum(
                float(u.get("ask_price", 0.0)) * float(u.get("ask_size", 0.0))
                for u in units
            )
        else:
            total = sum(
                float(u.get("bid_price", 0.0)) * float(u.get("bid_size", 0.0))
                for u in units
            )
        return total / order_value_krw

    def _spread_pct(self, orderbook: dict) -> float:
        units = orderbook.get("orderbook_units", [])
        if not units:
            return 1.0
        bid = float(units[0].get("bid_price", 0.0))
        ask = float(units[0].get("ask_price", 0.0))
        mid = (bid + ask) / 2 if bid and ask else 0.0
        if mid <= 0:
            return 1.0
        return (ask - bid) / mid

    def estimate_slippage(self, market: str, spread_pct: float) -> float:
        hist = self.slip_hist[market]
        if hist:
            return sum(hist) / len(hist)
        return min(0.0025, spread_pct * 0.5 + 0.0008)

    def check_quality_gate(
        self, market: str, orderbook: dict, order_value_krw: float, side: str
    ) -> tuple[bool, dict]:
        sp = self._spread_pct(orderbook)
        dr = self._depth_ratio(orderbook, order_value_krw, side)
        se = self.estimate_slippage(market, sp)

        quote = market.split("-")[0] if "-" in market else "KRW"
        spread_max = self.cfg["gates"].get("spread_max", 0.0025)
        if quote == "BTC":
            spread_max = float(self.cfg["gates"].get("spread_max_btc", spread_max))
        elif quote == "USDT":
            spread_max = float(self.cfg["gates"].get("spread_max_usdt", spread_max))

        spread_ok = sp <= spread_max
        depth_ok = dr >= self.cfg["gates"]["depth_ratio_min"]
        slip_ok = se <= self.cfg["gates"]["entry_slippage_cap"]
        ok = spread_ok and depth_ok and slip_ok
        reason = (
            "PASS"
            if ok
            else f"spread_ok={spread_ok},depth_ok={depth_ok},slip_ok={slip_ok}"
        )

        self.storage.insert(
            "market_quality",
            {
                "ts_ms": now_ms(),
                "market": market,
                "side": side,
                "spread_pct": sp,
                "depth_ratio": dr,
                "slip_est": se,
                "pass": 1 if ok else 0,
                "reason": reason,
            },
        )
        return ok, {
            "spread_pct": sp,
            "depth_ratio": dr,
            "slip_est": se,
            "reason": reason,
        }

    def _fee_rate(self, market: str) -> float:
        quote = market.split("-")[0]
        return float(self.cfg["fees"].get(quote, 0.001))

    def _save_trade(
        self,
        market: str,
        side: str,
        qty: float,
        order_value_krw: float,
        ref_price: float,
        fill_price: float,
        fee: float,
        slip: float,
        reason: str,
        status: str,
    ) -> None:
        self.storage.insert(
            "trades",
            {
                "ts_ms": now_ms(),
                "mode": self.mode,
                "market": market,
                "side": side,
                "qty": qty,
                "order_value_krw": order_value_krw,
                "request_price": ref_price,
                "fill_price": fill_price,
                "fee": fee,
                "slippage_pct": slip,
                "reason": reason,
                "status": status,
            },
        )

    def _maybe_cooldown_by_slippage(
        self, market: str, side: str, slippage_pct: float
    ) -> None:
        cap = (
            self.cfg["gates"]["entry_slippage_cap"]
            if side == "BUY"
            else self.cfg["gates"]["exit_slippage_cap"]
        )
        if slippage_pct > cap:
            self.cooldown_until_ms[market] = (
                now_ms() + self.cfg["gates"]["cooldown_minutes"] * 60 * 1000
            )

    def _log_runtime_event(
        self, level: str, event: str, market: str | None, details: str
    ) -> None:
        try:
            self.storage.log_event(level, event, market, details)
        except Exception:
            pass

    def _parse_live_fill(
        self, order: dict, fallback_price: float, fallback_fee: float
    ) -> tuple[float, float, float, str]:
        executed_volume = float(order.get("executed_volume") or 0.0)
        paid_fee = float(order.get("paid_fee") or fallback_fee)

        trades = order.get("trades") or []
        if trades and executed_volume > 0:
            total = 0.0
            for t in trades:
                price = float(t.get("price") or 0.0)
                vol = float(t.get("volume") or 0.0)
                funds = t.get("funds")
                if funds is not None:
                    total += float(funds)
                else:
                    total += price * vol
            fill_price = (
                total / executed_volume if executed_volume > 0 else fallback_price
            )
        else:
            price = order.get("price")
            fill_price = float(price) if price else fallback_price

        status = str(order.get("state") or "unknown")
        return fill_price, paid_fee, executed_volume, status

    async def _poll_order(
        self, order_uuid: str, retry: int = 6, wait_s: float = 0.5
    ) -> dict | None:
        if not self.rest:
            return None
        for _ in range(retry):
            detail = await self.rest.get_order(order_uuid)
            if not detail:
                await asyncio.sleep(wait_s)
                continue
            if detail.get("state") in {"done", "cancel"}:
                return detail
            await asyncio.sleep(wait_s)
        return await self.rest.get_order(order_uuid)

    async def _krw_available(self) -> float | None:
        return await self._quote_available("KRW")

    async def _quote_available(self, quote: str) -> float | None:
        if not self.rest or not self.rest.is_live_ready:
            return None
        accts = await self.rest.get_accounts()
        if not isinstance(accts, list):
            return None
        for a in accts:
            if str(a.get("currency")) == quote:
                try:
                    bal = float(a.get("balance") or 0.0)
                    locked = float(a.get("locked") or 0.0)
                    return max(0.0, bal - locked)
                except Exception:
                    return None
        return None

    async def _base_available(self, market: str) -> float | None:
        """해당 마켓의 base 코인(예: KRW-BTC면 BTC) 가용 수량을 반환합니다."""
        if not self.rest or not self.rest.is_live_ready:
            return None
        try:
            _, base = market.split("-", 1)
        except ValueError:
            return None
        accts = await self.rest.get_accounts()
        if not isinstance(accts, list):
            return None
        for a in accts:
            if str(a.get("currency")) == base:
                try:
                    bal = float(a.get("balance") or 0.0)
                    locked = float(a.get("locked") or 0.0)
                    return max(0.0, bal - locked)
                except Exception:
                    return None
        return None

    def _dedup_key(self, market: str, side: str, reason: str, ref_price: float) -> str:
        # stop_loss 계열은 초단위로 과잉 중복을 막는 게 핵심, 엔트리는 조금 더 넉넉히 잡습니다.
        now_s = now_ms() // 1000
        if "stop" in (reason or "").lower():
            bucket = now_s // int(
                self.cfg.get("live", {}).get("order_dedup_stop_seconds", 2) or 2
            )
        elif side == "BUY":
            bucket = now_s // int(
                self.cfg.get("live", {}).get("order_dedup_entry_seconds", 10) or 10
            )
        else:
            bucket = now_s // int(
                self.cfg.get("live", {}).get("order_dedup_exit_seconds", 5) or 5
            )

        # ref_price까지 포함하면 너무 세밀해져 중복 방지 효과가 떨어져서, 0.1% 단위로 라운딩해서 넣습니다.
        px_bucket = (
            int(round(ref_price / max(ref_price * 0.001, 1e-9))) if ref_price > 0 else 0
        )
        return f"{market}|{side}|{reason}|{bucket}|{px_bucket}"

    def _try_dedup(
        self, market: str, side: str, reason: str, qty: float, ref_price: float
    ) -> bool:
        key = self._dedup_key(market, side, reason, ref_price)
        try:
            self.storage.execute(
                "INSERT INTO order_dedup(ts_ms,dedup_key,market,side,reason,qty,ref_price,note) VALUES(?,?,?,?,?,?,?,?)",
                (now_ms(), key, market, side, reason, float(qty), float(ref_price), ""),
            )
            return True
        except Exception:
            # unique conflict 포함. (정교한 예외 분기보다 '중복이면 막기'가 목적)
            return False

    async def _execute_live(
        self,
        market: str,
        side: str,
        order_value_krw: float,
        qty: float,
        ref_price: float,
        reason: str,
    ) -> ExecutionResult:
        if not self.rest or not self.rest.is_live_ready:
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "live_not_ready"
            )
        if not self.live_confirm_required:
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "live_confirm_missing"
            )
        if self.kill_switch:
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "kill_switch_on"
            )

        if side == "BUY" and order_value_krw > self.max_order_krw:
            order_value_krw = self.max_order_krw

        # 일일 거래 제한은 신규 진입(BUY)에만 적용. 청산(SELL)은 항상 허용.
        if side == "BUY" and (not self._within_daily_trade_limit()):
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "daily_trade_limit"
            )

        # 주문 멱등성(중복 제출 방지)
        if not self._try_dedup(market, side, reason or "", qty, ref_price):
            try:
                self.storage.log_event(
                    "WARN",
                    "ORDER_DEDUP_BLOCK",
                    market,
                    f"side={side} reason={reason} qty={qty} ref={ref_price}",
                )
            except Exception:
                pass
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "dedup_block"
            )

        quote = market.split("-")[0] if "-" in market else "KRW"

        # 잔고 부족 주문을 API로 던지지 않도록 사전 체크(버퍼 2%)
        if side == "BUY":
            if quote == "KRW":
                avail = await self._krw_available()
                if avail is not None:
                    max_value = max(0.0, avail * 0.98)
                    if order_value_krw > max_value:
                        order_value_krw = max_value
                    if order_value_krw < max(
                        float(self.cfg.get("min_notional_krw", 0)), 1000.0
                    ):
                        return ExecutionResult(
                            False,
                            market,
                            side,
                            qty,
                            ref_price,
                            0.0,
                            0.0,
                            "live_insufficient_krw",
                        )
            else:
                # BTC/USDT 마켓: quote 잔고 기준으로 매수 수량(qty)을 clamp
                avail_q = await self._quote_available(quote)
                if avail_q is not None:
                    max_qty = (avail_q * 0.98) / max(ref_price, 1e-12)
                    if qty > max_qty:
                        qty = max(0.0, max_qty)

                min_notional = float(self.cfg.get("min_notional_krw", 5000))
                if order_value_krw < min_notional:
                    return ExecutionResult(
                        False,
                        market,
                        side,
                        qty,
                        ref_price,
                        0.0,
                        0.0,
                        "live_under_min_notional",
                    )

        if side == "SELL":
            # 가용수량 기준으로만 제한하고, 가능한 전량 매도 시도
            avail_qty = await self._base_available(market)
            if avail_qty is not None:
                qty = min(qty, max(0.0, avail_qty))

            if qty < 1e-12:
                return ExecutionResult(
                    False,
                    market,
                    side,
                    0.0,
                    ref_price,
                    0.0,
                    0.0,
                    "live_insufficient_asset",
                )

            # 업비트 최소 주문금액(KRW 기준) 미만이면(더스트) 매도 시도 자체를 하지 않음
            # 단, 'topup_before_sell' 옵션이 켜져 있으면 "부족분 매수 -> 합산 매도" 시도
            min_notional = float(self.cfg.get("min_notional_krw", 5000))

            if order_value_krw < min_notional:
                # (승률/안전 보강) 손절 상황에서 더스트 탑업 매수는 '손절을 위해 추가매수'가 되어
                # 가격/수량이 꼬이고 과매도/주문부족 에러를 유발할 수 있으므로 금지합니다.
                if "stop" in reason.lower():
                    cd_until = int(
                        self._stoploss_under_notional_until_ms.get(market, 0)
                    )
                    now = now_ms()
                    if now < cd_until:
                        return ExecutionResult(
                            False,
                            market,
                            side,
                            qty,
                            ref_price,
                            0.0,
                            0.0,
                            "stoploss_under_min_notional_cooldown",
                        )
                    cooldown_s = int(
                        self.cfg.get("dust", {}).get(
                            "stoploss_under_min_notional_cooldown_seconds", 600
                        )
                        or 600
                    )
                    self._stoploss_under_notional_until_ms[market] = now + (
                        cooldown_s * 1000
                    )
                    self._log_runtime_event(
                        "WARN",
                        "STOPLOSS_UNDER_MIN_NOTIONAL",
                        market,
                        f"reason={reason} order_value_krw={order_value_krw:.0f} min_notional_krw={min_notional:.0f} qty={qty:.8f} ref_price={ref_price:.4f} cooldown_s={cooldown_s}",
                    )
                    return ExecutionResult(
                        False,
                        market,
                        side,
                        qty,
                        ref_price,
                        0.0,
                        0.0,
                        "stoploss_under_min_notional",
                    )

                can_topup = self.cfg.get("dust", {}).get("topup_before_sell", False)
                # KRW 마켓이고, Top-up 설정이 켜져 있을 때만 시도
                if can_topup and quote == "KRW":
                    # [개선] 시장가 매수 대신 지정가 매수 사용 (슬리피지 위험 감소)
                    # 지정가로 매수 후 시장가로 전환하는 하이브리드 방식
                    buffer = float(self.cfg["dust"].get("topup_buffer_krw", 2000))
                    target_amt = 5000 + buffer  # 최소 5,000원은 넘겨야 함
                    buy_needed = target_amt - order_value_krw

                    # 배보다 배꼽이 너무 크면(설정 한도 초과) 포기
                    max_topup = float(self.cfg["dust"].get("topup_max_krw", 20000))

                    # 더스트 탑업 쿨다운(루프/연속 매수 방지)
                    cd_until = self._dust_cooldown_until_ms.get(market, 0)
                    if now_ms() < cd_until:
                        return ExecutionResult(
                            False,
                            market,
                            side,
                            qty,
                            ref_price,
                            0.0,
                            0.0,
                            "dust_topup_cooldown",
                        )

                    buy_amt = max(float(buy_needed), float(min_notional))
                    if 0 < buy_amt <= max_topup:
                        # NOTE: Upbit enforces a minimum total for BUY orders too.
                        # A limit buy with a small KRW notional will fail with under_min_total_bid.
                        # For dust top-up we prioritize reliability: use a single market buy.
                        LOGGER.warning(
                            f"DUST_TOPUP: Buying {buy_amt:.0f} KRW to exit {market} (Current Value: {order_value_krw:.0f} KRW, needed={buy_needed:.0f})"
                        )

                        buy_res = await self._call_rest(
                            self.rest.place_market_buy, market, buy_amt
                        )
                        if buy_res and buy_res.get("uuid"):
                            await asyncio.sleep(1.0)
                            new_qty = await self._base_available(market)
                            if new_qty is not None and new_qty > qty:
                                LOGGER.warning(
                                    f"DUST_TOPUP: Success. Qty updated {qty} -> {new_qty}"
                                )
                                qty = new_qty
                                self._dust_cooldown_until_ms[market] = (
                                    now_ms()
                                    + int(
                                        self.cfg.get("dust", {}).get(
                                            "topup_cooldown_seconds", 1800
                                        )
                                    )
                                    * 1000
                                )
                            else:
                                return ExecutionResult(
                                    False,
                                    market,
                                    side,
                                    qty,
                                    ref_price,
                                    0.0,
                                    0.0,
                                    "dust_buy_failed_balance_check",
                                )
                        else:
                            return ExecutionResult(
                                False,
                                market,
                                side,
                                qty,
                                ref_price,
                                0.0,
                                0.0,
                                "dust_buy_failed_api_error",
                            )
                    else:
                        return ExecutionResult(
                            False,
                            market,
                            side,
                            qty,
                            ref_price,
                            0.0,
                            0.0,
                            "dust_too_large_or_invalid",
                        )
                else:
                    return ExecutionResult(
                        False,
                        market,
                        side,
                        qty,
                        ref_price,
                        0.0,
                        0.0,
                        "live_under_min_notional",
                    )

        fallback_fee = order_value_krw * self._fee_rate(market)
        placed = None

        # A. 매수는 취소 루프 없이 단일 시장가 주문으로 단순화(체결 실패/취소 churn 감소)
        if side == "BUY":
            if quote == "KRW":
                placed = await self._call_rest(
                    self.rest.place_market_buy, market, order_value_krw
                )
            else:
                placed = await self._call_rest(
                    self.rest.place_market_buy_volume, market, qty
                )

        # B. 매도 주문 (상황별 분기)
        else:
            # 1) 급한 상황: 손절(stop_loss) -> 즉시 시장가
            if "stop" in reason.lower():
                q = qty
                # 시장가 매도 재시도 로직 (최대 3회)
                for i in range(3):
                    placed_try = await self._call_rest(
                        self.rest.place_market_sell, market, q
                    )
                    # 성공하면 break
                    if isinstance(placed_try, dict) and placed_try.get("uuid"):
                        placed = placed_try
                        break
                    # 에러 처리 (잔고 부족 시 미세 조정)
                    err = (placed_try or {}).get("error", {}).get("name", "")
                    if "insufficient_funds" in str(placed_try):
                        q = q * 0.995  # 0.5% 줄여서 재시도
                        await asyncio.sleep(0.2)
                        continue
                    break

            # 2) 여유 있는 상황: 지정가 짧게 대기 후 시장가 fallback
            else:
                limit_price = adjust_price_to_tick(ref_price)
                placed = await self._call_rest(
                    self.rest.place_limit_sell, market, qty, limit_price
                )
                if placed and placed.get("uuid"):
                    wait_s = float(
                        self.cfg.get("live", {}).get("sell_limit_wait_seconds", 2.0)
                        or 2.0
                    )
                    await asyncio.sleep(max(0.5, wait_s))
                    detail = await self._call_rest(self.rest.get_order, placed["uuid"])
                    if detail and detail.get("state") == "wait":
                        remaining_vol = float(detail.get("remaining_volume", 0.0))
                        if remaining_vol * limit_price >= 1000.0:
                            await self._call_rest(
                                self.rest.cancel_order, placed["uuid"]
                            )
                            await asyncio.sleep(0.5)
                            placed = await self._call_rest(
                                self.rest.place_market_sell, market, remaining_vol
                            )

        if not placed or not placed.get("uuid"):
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "live_place_failed"
            )

        detail = await self._poll_order(placed["uuid"])
        if not detail:
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "live_poll_failed"
            )

        fill_price, fee, filled_qty, status = self._parse_live_fill(
            detail, ref_price, fallback_fee
        )
        fill_value = fill_price * max(filled_qty, 0.0)

        # 부분 체결 시 잔량 처리 (시장가 주문이라 거의 다 체결되지만 혹시 모를 상황 대비)
        if side == "BUY":
            remaining_value = max(0.0, order_value_krw - fill_value)
            remaining_qty = max(0.0, qty - filled_qty)
        else:
            remaining_qty = max(0.0, qty - filled_qty)
            remaining_value = remaining_qty * ref_price

        # 1회 재시도 정책
        if remaining_qty > 1e-10 and remaining_value >= 1000:
            if side == "BUY":
                placed2 = await self._call_rest(
                    self.rest.place_market_buy, market, remaining_value
                )
            else:
                placed2 = await self._call_rest(
                    self.rest.place_market_sell, market, remaining_qty
                )
            if placed2 and placed2.get("uuid"):
                detail2 = await self._poll_order(placed2["uuid"])
                if detail2:
                    fp2, fee2, fq2, _ = self._parse_live_fill(detail2, ref_price, 0.0)
                    total_qty = filled_qty + fq2
                    total_val = fill_value + (fp2 * fq2)
                    if total_qty > 0:
                        fill_price = total_val / total_qty
                    fee += fee2
                    filled_qty = total_qty

        if filled_qty <= 1e-12:
            self.cooldown_until_ms[market] = (
                now_ms() + self.cfg["gates"]["cooldown_minutes"] * 60 * 1000
            )
            return ExecutionResult(
                False, market, side, qty, ref_price, 0.0, 0.0, "live_not_filled"
            )

        if side == "BUY":
            slip = max(0.0, (fill_price - ref_price) / max(ref_price, 1e-9))
        else:
            slip = max(0.0, (ref_price - fill_price) / max(ref_price, 1e-9))

        self.slip_hist[market].append(slip)
        self._maybe_cooldown_by_slippage(market, side, slip)

        # Normalize status: canceled orders can be partially filled.
        if status == "cancel" and filled_qty > 1e-12:
            status = "partial_cancel"

        filled_value_effective = filled_qty * fill_price
        self._save_trade(
            market,
            side,
            filled_qty,
            filled_value_effective,
            ref_price,
            fill_price,
            fee,
            slip,
            reason,
            f"filled_live_{status}",
        )

        # 1회 재시도 후에도 부족 체결이면 비정상으로 판단
        if filled_qty < qty * 0.98:
            self.cooldown_until_ms[market] = (
                now_ms() + self.cfg["gates"]["cooldown_minutes"] * 60 * 1000
            )

        return ExecutionResult(
            True, market, side, filled_qty, fill_price, fee, slip, reason
        )

    async def execute_market(
        self,
        market: str,
        side: str,
        order_value_krw: float,
        qty: float,
        ref_price: float,
        slip_est: float,
        reason: str,
    ) -> ExecutionResult:
        if self.mode == "live" and self.rest and self.rest.is_live_ready:
            try:
                return await self._execute_live(
                    market, side, order_value_krw, qty, ref_price, reason
                )
            except Exception as e:
                LOGGER.exception("execute_market live failed: %s", e)
                return ExecutionResult(
                    False,
                    market,
                    side,
                    qty,
                    ref_price,
                    0.0,
                    0.0,
                    f"live_exception:{type(e).__name__}",
                )

        paper_result = self._execute_paper(
            side, order_value_krw, qty, ref_price, slip_est, reason
        )
        return paper_result

    def _execute_paper(
        self,
        side: str,
        order_value_krw: float,
        qty: float,
        ref_price: float,
        slip_est: float,
        reason: str,
    ) -> ExecutionResult:
        fill = (
            ref_price * (1 + slip_est) if side == "BUY" else ref_price * (1 - slip_est)
        )
        fee = order_value_krw * self._fee_rate("KRW")
        result = ExecutionResult(True, "PAPER", side, qty, fill, fee, slip_est, reason)
        self.slip_hist["PAPER"].append(slip_est)
        self._maybe_cooldown_by_slippage("PAPER", side, slip_est)
        self._save_trade(
            "PAPER",
            side,
            qty,
            order_value_krw,
            ref_price,
            fill,
            fee,
            slip_est,
            reason,
            "filled",
        )
        return result

    async def execute_with_maker_first(
        self,
        market: str,
        side: str,
        order_value_krw: float,
        qty: float,
        ref_price: float,
        slip_est: float,
        reason: str,
        orderbook: dict | None = None,
    ) -> ExecutionResult:
        cfg_exec = self.cfg.get("execution", {})
        use_maker_first = cfg_exec.get("use_maker_first", True)
        max_spread_for_limit = cfg_exec.get("max_spread_for_limit", 0.0015)
        limit_timeout = cfg_exec.get("limit_order_timeout_seconds", 3.0)

        if not use_maker_first or side != "BUY" or not orderbook:
            return await self.execute_market(
                market, side, order_value_krw, qty, ref_price, slip_est, reason
            )

        spread_pct = self._spread_pct(orderbook)
        if spread_pct > max_spread_for_limit:
            return await self.execute_market(
                market, side, order_value_krw, qty, ref_price, slip_est, reason
            )

        units = orderbook.get("orderbook_units", [])
        if not units:
            return await self.execute_market(
                market, side, order_value_krw, qty, ref_price, slip_est, reason
            )

        ask_price = float(units[0].get("ask_price", 0.0))
        ask_size = float(units[0].get("ask_size", 0.0))
        if ask_price <= 0 or ask_size <= 0:
            return await self.execute_market(
                market, side, order_value_krw, qty, ref_price, slip_est, reason
            )

        available_ask_value = ask_price * ask_size
        if available_ask_value < order_value_krw * 0.5:
            return await self.execute_market(
                market, side, order_value_krw, qty, ref_price, slip_est, reason
            )

        limit_price = adjust_price_to_tick(ask_price * 0.999)
        qty_to_buy = min(qty, ask_size * 1.02)
        quote = market.split("-")[0] if "-" in market else "KRW"

        try:
            if quote == "KRW":
                placed = await self._call_rest(
                    self.rest.place_limit_buy, market, order_value_krw, limit_price
                )
            else:
                placed = await self._call_rest(
                    self.rest.place_limit_buy_volume, market, qty_to_buy, limit_price
                )

            if placed and placed.get("uuid"):
                wait_time = max(0.5, limit_timeout)
                await asyncio.sleep(wait_time)
                detail = await self._call_rest(self.rest.get_order, placed["uuid"])

                if detail and detail.get("state") == "done":
                    fill_price = float(detail.get("price", limit_price))
                    fee = order_value_krw * self._fee_rate(market)
                    slip = max(0.0, (fill_price - ref_price) / max(ref_price, 1e-9))
                    self.slip_hist[market].append(slip)
                    self._save_trade(
                        market,
                        side,
                        qty_to_buy,
                        order_value_krw,
                        ref_price,
                        fill_price,
                        fee,
                        slip,
                        reason + "_maker",
                        "filled_maker",
                    )
                    return ExecutionResult(
                        True,
                        market,
                        side,
                        qty_to_buy,
                        fill_price,
                        fee,
                        slip,
                        reason + "_maker",
                    )

                if detail and detail.get("state") == "wait":
                    remaining = float(detail.get("remaining_volume", 0.0))
                    await self._call_rest(self.rest.cancel_order, placed["uuid"])
                    await asyncio.sleep(0.3)

        except Exception as e:
            LOGGER.debug("Maker order failed, falling back to market: %s", e)

        return await self.execute_market(
            market, side, order_value_krw, qty, ref_price, slip_est, reason
        )

    def is_cooldown(self, market: str) -> bool:
        return now_ms() < self.cooldown_until_ms.get(market, 0)

    async def _call_rest(self, fn, *args, retries: int = 3, base_delay_s: float = 0.5):
        for attempt in range(retries):
            try:
                return await fn(*args)
            except Exception as e:
                if attempt == retries - 1:
                    raise
                wait = base_delay_s * (2**attempt)
                LOGGER.warning(
                    "REST call failed (%s), retrying in %.2fs: %s", fn.__name__, wait, e
                )
                await asyncio.sleep(wait)

    def _within_daily_trade_limit(self) -> bool:
        """일일 거래횟수 제한 체크.

        기존 구현은 UTC 자정 기준이라(KST 기준 오전 9시에 리셋) 체감상 '하루'와 어긋나
        쉽게 daily_trade_limit에 걸립니다. 설정된 timezone(기본 Asia/Seoul) 자정 기준으로 계산합니다.
        """
        try:
            # cfg의 timezone 우선, 없으면 Asia/Seoul
            tz_name = str(self.cfg.get("timezone") or "Asia/Seoul")
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(tz_name)
            except Exception:
                tz = timezone.utc

            now_local = datetime.now(tz=tz)
            start_local = datetime.combine(
                now_local.date(), datetime.min.time(), tzinfo=tz
            )
            start_ms = int(start_local.astimezone(timezone.utc).timestamp() * 1000)

            rows = self.storage.query(
                "SELECT COUNT(*) AS n FROM trades WHERE mode='live' AND status LIKE 'filled_live_%' AND ts_ms >= ?",
                (start_ms,),
            )
            n = int(rows[0]["n"]) if rows else 0
            return n < self.max_trades_per_day
        except Exception:
            # 실패 시 보수적으로 허용 (실거래 차단으로 인한 장애 전파 방지)
            return True
