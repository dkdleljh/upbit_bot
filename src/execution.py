import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass

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

    def _depth_ratio(self, orderbook: dict, order_value_krw: float, side: str = "BUY") -> float:
        units = orderbook.get("orderbook_units", [])[:3]
        if not units or order_value_krw <= 0:
            return 0.0
        if side == "BUY":
            total = sum(float(u.get("ask_price", 0.0)) * float(u.get("ask_size", 0.0)) for u in units)
        else:
            total = sum(float(u.get("bid_price", 0.0)) * float(u.get("bid_size", 0.0)) for u in units)
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

    def check_quality_gate(self, market: str, orderbook: dict, order_value_krw: float, side: str) -> tuple[bool, dict]:
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
        reason = "PASS" if ok else f"spread_ok={spread_ok},depth_ok={depth_ok},slip_ok={slip_ok}"

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
        return ok, {"spread_pct": sp, "depth_ratio": dr, "slip_est": se, "reason": reason}

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

    def _maybe_cooldown_by_slippage(self, market: str, side: str, slippage_pct: float) -> None:
        cap = self.cfg["gates"]["entry_slippage_cap"] if side == "BUY" else self.cfg["gates"]["exit_slippage_cap"]
        if slippage_pct > cap:
            self.cooldown_until_ms[market] = now_ms() + self.cfg["gates"]["cooldown_minutes"] * 60 * 1000

    def _parse_live_fill(self, order: dict, fallback_price: float, fallback_fee: float) -> tuple[float, float, float, str]:
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
            fill_price = total / executed_volume if executed_volume > 0 else fallback_price
        else:
            price = order.get("price")
            fill_price = float(price) if price else fallback_price

        status = str(order.get("state") or "unknown")
        return fill_price, paid_fee, executed_volume, status

    async def _poll_order(self, order_uuid: str, retry: int = 6, wait_s: float = 0.5) -> dict | None:
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
            bucket = now_s // int(self.cfg.get("live", {}).get("order_dedup_stop_seconds", 2) or 2)
        elif side == "BUY":
            bucket = now_s // int(self.cfg.get("live", {}).get("order_dedup_entry_seconds", 10) or 10)
        else:
            bucket = now_s // int(self.cfg.get("live", {}).get("order_dedup_exit_seconds", 5) or 5)

        # ref_price까지 포함하면 너무 세밀해져 중복 방지 효과가 떨어져서, 0.1% 단위로 라운딩해서 넣습니다.
        px_bucket = int(round(ref_price / max(ref_price * 0.001, 1e-9))) if ref_price > 0 else 0
        return f"{market}|{side}|{reason}|{bucket}|{px_bucket}"

    def _try_dedup(self, market: str, side: str, reason: str, qty: float, ref_price: float) -> bool:
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
            return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_not_ready")

        # 주문 멱등성(중복 제출 방지)
        if not self._try_dedup(market, side, reason or "", qty, ref_price):
            try:
                self.storage.log_event("WARN", "ORDER_DEDUP_BLOCK", market, f"side={side} reason={reason} qty={qty} ref={ref_price}")
            except Exception:
                pass
            return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "dedup_block")

        quote = market.split("-")[0] if "-" in market else "KRW"

        # 잔고 부족 주문을 API로 던지지 않도록 사전 체크(버퍼 2%)
        if side == "BUY":
            if quote == "KRW":
                avail = await self._krw_available()
                if avail is not None:
                    max_value = max(0.0, avail * 0.98)
                    if order_value_krw > max_value:
                        order_value_krw = max_value
                    if order_value_krw < max(float(self.cfg.get("min_notional_krw", 0)), 1000.0):
                        return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_insufficient_krw")
            else:
                # BTC/USDT 마켓: quote 잔고 기준으로 매수 수량(qty)을 clamp
                avail_q = await self._quote_available(quote)
                if avail_q is not None:
                    max_qty = (avail_q * 0.98) / max(ref_price, 1e-12)
                    if qty > max_qty:
                        qty = max(0.0, max_qty)

                min_notional = float(self.cfg.get("min_notional_krw", 5000))
                if order_value_krw < min_notional:
                    return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_under_min_notional")

        if side == "SELL":
            # 가용수량 기준으로만 제한하고, 가능한 전량 매도 시도
            avail_qty = await self._base_available(market)
            if avail_qty is not None:
                qty = min(qty, max(0.0, avail_qty))

            if qty < 1e-12:
                return ExecutionResult(False, market, side, 0.0, ref_price, 0.0, 0.0, "live_insufficient_asset")

            # 업비트 최소 주문금액(KRW 기준) 미만이면(더스트) 매도 시도 자체를 하지 않음
            # 단, 'topup_before_sell' 옵션이 켜져 있으면 "부족분 매수 -> 합산 매도" 시도
            min_notional = float(self.cfg.get("min_notional_krw", 5000))
            
            if order_value_krw < min_notional:
                # (승률/안전 보강) 손절 상황에서 더스트 탑업 매수는 '손절을 위해 추가매수'가 되어
                # 가격/수량이 꼬이고 과매도/주문부족 에러를 유발할 수 있으므로 금지합니다.
                if "stop" in reason.lower():
                    return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "stoploss_under_min_notional")

                can_topup = self.cfg.get("dust", {}).get("topup_before_sell", False)
                # KRW 마켓이고, Top-up 설정이 켜져 있을 때만 시도
                if can_topup and quote == "KRW":
                    # 1. 필요 금액 계산 (최소금액 + 버퍼만큼 확보)
                    buffer = float(self.cfg["dust"].get("topup_buffer_krw", 2000))
                    target_amt = 5000 + buffer # 최소 5,000원은 넘겨야 함
                    buy_needed = target_amt - order_value_krw
                    
                    # 배보다 배꼽이 너무 크면(설정 한도 초과) 포기
                    max_topup = float(self.cfg["dust"].get("topup_max_krw", 20000))
                    
                    # 더스트 탑업 쿨다운(루프/연속 매수 방지)
                    cd_until = self._dust_cooldown_until_ms.get(market, 0)
                    if now_ms() < cd_until:
                        return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "dust_topup_cooldown")

                    if 0 < buy_needed <= max_topup:
                        LOGGER.warning(f"DUST_TOPUP: Buying {buy_needed:.0f} KRW to exit {market} (Current Value: {order_value_krw:.0f} KRW)")
                        
                        # 2. 시장가 매수 시도
                        buy_res = await self.rest.place_market_buy(market, buy_needed)
                        if buy_res and buy_res.get("uuid"):
                            # 체결 대기 (1초)
                            await asyncio.sleep(1.0)
                            # 잔고 재조회 (매수된 수량 합산)
                            new_qty = await self._base_available(market)
                            if new_qty is not None and new_qty > qty:
                                LOGGER.info(f"DUST_TOPUP: Success. Qty updated {qty} -> {new_qty}")
                                qty = new_qty # 수량 업데이트
                                # 더스트 탑업은 연속 실행되면 위험하므로 쿨다운
                                self._dust_cooldown_until_ms[market] = now_ms() + int(self.cfg.get("dust", {}).get("topup_cooldown_seconds", 1800)) * 1000
                                # 이제 아래 매도 로직으로 진행 (수량이 늘어났으므로 5000원 넘음)
                            else:
                                return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "dust_buy_failed_balance_check")
                        else:
                             return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "dust_buy_failed_api_error")
                    else:
                         return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "dust_too_large_or_invalid")
                else:
                    return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_under_min_notional")

        fallback_fee = order_value_krw * self._fee_rate(market)

        # [지정가 주문으로 변경]
        # 시장가 주문은 슬리피지가 크고, 급등/급락 시 불리한 가격에 체결될 위험이 있음.
        # 따라서 최우선 호가(1호가)에 지정가 주문을 내고, 미체결 시 취소하는 전략 사용.
        
        placed = None
        limit_price = ref_price # 기본값 (호가 정보가 없을 경우)

        # 호가 정보 조회 (없으면 ref_price 사용)
        try:
            # 현재가와 호가 차이가 크지 않다고 가정하고, ref_price를 기준으로 함.
            # 더 정밀하게 하려면 orderbook을 인자로 받아야 하지만, 여기서는 ref_price 사용.
            # 지정가 주문은 가격 단위(Tick Size)를 맞춰야 하므로, 단순히 ref_price를 쓰면 에러날 수 있음.
            # 하지만 Upbit API는 유효하지 않은 가격이면 400 에러를 줌.
            # 여기서는 편의상 시장가 주문을 유지하되, IOC(Immediate or Cancel) 옵션이 없으므로
            # "지정가 주문 후 5초 대기 -> 미체결 시 취소" 로직으로 구현.
            
            # NOTE: 호가 단위를 맞추는 로직(get_tick_size)이 없어서, 
            # 일단은 시장가 주문(market/price)을 유지하되 
            # 매수 시에는 "현재가보다 높게 잡히지 않도록" 감시하는 로직이 필요함.
            # 하지만 시장가 주문은 가격을 지정할 수 없음.
            
            # 결론: 지정가 주문을 하려면 '호가 단위 계산'이 필수임.
            # 현재 코드베이스엔 호가 단위 계산 로직이 없으므로, 
            # **시장가 주문을 쓰되, 진입 조건을 더 까다롭게(RSI/윗꼬리) 한 것**으로 만족해야 함.
            # 무리하게 지정가를 쓰다가 '호가 단위 오류'로 주문 거부당하면 더 손해임.
            
            # 따라서 여기서는 지정가 로직 대신, **기존 시장가 주문을 유지**하되
            # 위에서 적용한 '알고리즘 필터(RSI, 윗꼬리)'가 잘 작동하기를 기대하는 것이 안전함.
            
            # 다만, 매도(SELL) 시에는 '시장가 매도(던지기)'가 너무 위험하므로,
            # 매도만이라도 **"호가창을 보고 지정가 매도"**를 시도하는 게 좋으나,
            # 역시 호가 단위 문제 때문에 복잡함.
            
            # --> **전략 수정: 지정가 전환은 호가 단위 모듈 없이는 위험함.**
            # 대신 **"슬리피지 체크 강화"**로 우회 방어.
            pass

        except Exception:
            pass

        # [주문 실행 라우팅 (Order Routing) - Smart Chasing]
        # 1차 시도: 지정가(Limit) -> 미체결 시 취소 -> 2차 시도: 시장가(Market)
        
        placed = None
        
        # A. 매수 주문 (Limit Chasing)
        if side == "BUY":
            min_krw = 5500
            if quote == "KRW" and order_value_krw < min_krw:
                 return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_value_too_small")

            # 1. 최우선 매도호가(Ask 1)로 지정가 주문 (Taker지만 지정가라 안전)
            target_price = adjust_price_to_tick(ref_price) # ref_price는 현재가(Ask1 근처)
            
            # KRW 마켓은 가격 필수, BTC/USDT는 수량 필수
            if quote == "KRW":
                placed = await self.rest.place_limit_buy(market, order_value_krw / max(target_price, 1), target_price)
            else:
                placed = await self.rest.place_limit_buy(market, qty, target_price)

            # 체결 확인 및 추격 (Chasing)
            if placed and placed.get("uuid"):
                # 3초 대기 (체결 기회 부여)
                await asyncio.sleep(3.0)
                detail = await self.rest.get_order(placed["uuid"])
                
                # 아직 대기 중('wait')이면 -> 취소 후 시장가(Taker)로 전환
                if detail and detail.get("state") == "wait":
                     # 부분 체결량 확인
                     executed_vol = float(detail.get("executed_volume", 0.0))
                     remaining_vol = float(detail.get("remaining_volume", 0.0))
                     
                     # 90% 이상 체결됐으면 그냥 둠
                     if remaining_vol > 0 and (executed_vol / (executed_vol + remaining_vol) < 0.9):
                         await self.rest.cancel_order(placed["uuid"])
                         await asyncio.sleep(0.5)
                         # 남은 금액만큼 시장가 재주문
                         rem_val = remaining_vol * target_price
                         if quote == "KRW":
                             placed = await self.rest.place_market_buy(market, rem_val)
                         else:
                             placed = await self.rest.place_market_buy_volume(market, remaining_vol)

        # B. 매도 주문 (상황별 분기)
        else:
            # 1) 급한 상황: 손절(stop_loss) -> 즉시 시장가
            if "stop" in reason.lower():
                q = qty
                # 시장가 매도 재시도 로직 (최대 3회)
                for i in range(3):
                    placed_try = await self.rest.place_market_sell(market, q)
                    # 성공하면 break
                    if isinstance(placed_try, dict) and placed_try.get("uuid"):
                        placed = placed_try
                        break
                    # 에러 처리 (잔고 부족 시 미세 조정)
                    err = (placed_try or {}).get("error", {}).get("name", "")
                    if "insufficient_funds" in str(placed_try):
                        q = q * 0.995 # 0.5% 줄여서 재시도
                        await asyncio.sleep(0.2)
                        continue
                    break
            
            # 2) 여유 있는 상황: 익절(take_profit), 시간만료 -> 지정가 시도 후 시장가
            else:
                # 지정가 매도: 현재가 기준 호가 보정
                limit_price = adjust_price_to_tick(ref_price)
                
                # 주문 시도
                placed = await self.rest.place_limit_sell(market, qty, limit_price)
                
                if placed and placed.get("uuid"):
                    # 지정가 체결 대기 (5초)
                    await asyncio.sleep(5.0)
                    detail = await self.rest.get_order(placed["uuid"])
                    
                    # 5초 뒤에도 'wait' 상태면 취소하고 시장가로 전환
                    if detail and detail.get("state") == "wait":
                        remaining_vol = float(detail.get("remaining_volume", 0.0))
                        # 잔량이 유의미하면 취소 후 시장가
                        if remaining_vol * limit_price > 1000:
                            await self.rest.cancel_order(placed["uuid"])
                            await asyncio.sleep(0.5)
                            # 남은 수량만큼 시장가 매도 (Fallback)
                            placed = await self.rest.place_market_sell(market, remaining_vol)

        if not placed or not placed.get("uuid"):
            return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_place_failed")

        detail = await self._poll_order(placed["uuid"])
        if not detail:
            return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_poll_failed")

        fill_price, fee, filled_qty, status = self._parse_live_fill(detail, ref_price, fallback_fee)
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
                placed2 = await self.rest.place_market_buy(market, remaining_value)
            else:
                placed2 = await self.rest.place_market_sell(market, remaining_qty)
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
            self.cooldown_until_ms[market] = now_ms() + self.cfg["gates"]["cooldown_minutes"] * 60 * 1000
            return ExecutionResult(False, market, side, qty, ref_price, 0.0, 0.0, "live_not_filled")

        if side == "BUY":
            slip = max(0.0, (fill_price - ref_price) / max(ref_price, 1e-9))
        else:
            slip = max(0.0, (ref_price - fill_price) / max(ref_price, 1e-9))

        self.slip_hist[market].append(slip)
        self._maybe_cooldown_by_slippage(market, side, slip)

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
            self.cooldown_until_ms[market] = now_ms() + self.cfg["gates"]["cooldown_minutes"] * 60 * 1000

        return ExecutionResult(True, market, side, filled_qty, fill_price, fee, slip, reason)

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
            return await self._execute_live(market, side, order_value_krw, qty, ref_price, reason)

        # paper/backtest 또는 live 키 미설정 시 모의체결
        if side == "BUY":
            fill = ref_price * (1 + slip_est)
        else:
            fill = ref_price * (1 - slip_est)
        fee = order_value_krw * self._fee_rate(market)
        result = ExecutionResult(True, market, side, qty, fill, fee, slip_est, reason)
        self.slip_hist[market].append(slip_est)
        self._maybe_cooldown_by_slippage(market, side, slip_est)
        self._save_trade(market, side, qty, order_value_krw, ref_price, fill, fee, slip_est, reason, "filled")
        return result

    def is_cooldown(self, market: str) -> bool:
        return now_ms() < self.cooldown_until_ms.get(market, 0)
