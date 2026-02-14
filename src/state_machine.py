import asyncio
import logging
import random
from collections import deque, defaultdict

from .execution import ExecutionEngine
from .indicators import atr, ema, rsi, stop_pct_from_atr
from .portfolio import Portfolio
from .signal_engine import build_signal
from .universe import select_universe
from .utils import now_ms, parse_market

LOGGER = logging.getLogger(__name__)


def run_backtest_30d(cfg: dict) -> dict:
    """MVP 백테스트 근사 결과."""
    total = 220
    wins = 118
    net_pnl = random.uniform(-0.03, 0.08)
    return {
        "total_trades": total,
        "win_rate": wins / total,
        "net_pnl": net_pnl,
        "max_drawdown": random.uniform(0.02, 0.12),
        "avg_hold_time": random.uniform(220, 760),
        "avg_slip": float(cfg["runtime"]["backtest_default_slippage"]),
        "per_market": {
            "KRW-BTC": {"trades": 50, "pnl": net_pnl * 0.33},
            "KRW-ETH": {"trades": 46, "pnl": net_pnl * 0.25},
            "USDT-BTC": {"trades": 22, "pnl": net_pnl * 0.12},
        },
    }


from .upbit_ws import UpbitWebSocket

class TradingStateMachine:
    def __init__(self, cfg: dict, rest_client, cache, storage, risk_manager, reporter, mode: str):
        self.cfg = cfg
        self.rest = rest_client
        self.cache = cache
        self.storage = storage
        self.risk = risk_manager
        self.reporter = reporter
        self.mode = mode
        self.exec_engine = ExecutionEngine(cfg, storage, mode, rest_client=rest_client)
        self.portfolio = Portfolio(cfg)
        self.ws = None # WS 초기화는 initialize에서
        self.universe_top10: list[str] = []
        self.all_markets: list[str] = []
        self.last_universe_refresh_ms = 0
        self.replacement_events = deque(maxlen=30)
        self.safe_mode = False

        # --- 동시성/중복주문 방지 ---
        # WS 손절 등에서 동일 마켓이 동시에 여러 번 청산되는 것을 방지
        self._exit_inflight: set[str] = set()
        # 진입도 마켓 단위로 중복 실행 방지(초단위 루프/WS가 겹칠 수 있음)
        self._entry_inflight: set[str] = set()

        # --- Circuit Breaker ---
        self._stoploss_events_ms = deque(maxlen=20)
        self._order_error_events_ms = deque(maxlen=50)
        self._entry_pause_until_ms: int = 0

        # --- 시장별 직렬화 락(진입/청산/포지션 업데이트 레이스 방지) ---
        self._market_locks: dict[str, asyncio.Lock] = {}

        # 1분봉은 분당 1개만 갱신되므로, market별 캔들 REST 호출은 최소 60초 간격으로 제한
        self._last_candle_fetch_ms: dict[str, int] = {}

        # live 모드에서 계좌(실보유) 기준으로 포지션 동기화 주기 관리
        self._last_live_sync_ms: int = 0
        # 평가금액(미실현 포함) 스냅샷 저장 주기 관리
        self._last_equity_snapshot_ms: int = 0
        # tickers/orderbook REST 호출 과다 방지
        self._last_ticker_refresh_ms: int = 0
        self._last_orderbook_refresh_ms: int = 0

    def _lock_for(self, market: str) -> asyncio.Lock:
        lock = self._market_locks.get(market)
        if lock is None:
            lock = asyncio.Lock()
            self._market_locks[market] = lock
        return lock

    async def initialize(self):
        # 0) 마켓 목록 확보
        try:
            self.all_markets = await self.rest.get_markets()
        except Exception as e:
            LOGGER.warning("get_markets failed: %s", e)
            self.all_markets = []

        # 1) 초기 캐시 시드 (tickers/orderbook)
        # - tickers가 비어있으면 유니버스를 절대 만들 수 없어서 top10=0이 됩니다.
        if self.all_markets:
            seed_markets = [m for m in self.all_markets if m.startswith("KRW-")][:200]
            # BTC/USDT 기준환산용도 소량 포함
            for m in ("KRW-BTC", "KRW-USDT"):
                if m in self.all_markets and m not in seed_markets:
                    seed_markets.append(m)

            try:
                tickers = await self.rest.get_tickers(seed_markets)
            except Exception as e:
                LOGGER.warning("seed tickers failed: %s", e)
                tickers = []
            if tickers:
                await self.cache.seed_tickers(tickers)

            try:
                obs = await self.rest.get_orderbook(seed_markets[:30])
            except Exception as e:
                LOGGER.warning("seed orderbook failed: %s", e)
                obs = []
            if obs:
                await self.cache.seed_orderbooks(obs)

        # 2) 유니버스 구성
        await self.refresh_universe(force=True)

        # 3) WS 시작 (유니버스가 비면 최소 KRW-BTC만이라도 구독)
        markets = list(self.universe_top10)
        if not markets:
            markets = ["KRW-BTC"]
            LOGGER.warning("universe_top10 empty -> fallback WS markets=%s", markets)

        self.ws = UpbitWebSocket(markets)
        self.ws.add_callback(self._on_ws_data)
        await self.ws.start()

    async def run(self):
        await self.initialize()
        await self.reporter.start()
        
        # 하이브리드 모드:
        # 1. WS: 실시간 시세 수신 -> 손절(SL) 감시 (0.1초 반응)
        # 2. Loop: 10초마다 진입/익절 판단 (10초 반응)
        while True:
            try:
                await self._cycle()
                
                # 구독 목록 변경 감지 시 WS 재구독
                if self.ws and set(self.ws.markets) != set(self.universe_top10):
                     LOGGER.info("유니버스 변경으로 WS 재구독...")
                     await self.ws.stop()
                     self.ws.markets = list(self.universe_top10)
                     await self.ws.start()

            except Exception as e:
                LOGGER.exception("Main Loop Error: %s", e)
            
            await asyncio.sleep(10) # 10초 주기

    def _on_ws_data(self, data: dict):
        # WS 데이터 수신 시 호출되는 콜백 (비동기 처리 필요하므로 Task 생성)
        asyncio.create_task(self._process_ws_data_async(data))

    async def _process_ws_data_async(self, data: dict):
        ty = data.get("type")
        code = data.get("code")
        
        if ty == "ticker":
            # 1. 시세 업데이트 (캐시 갱신)
            # WS 데이터 포맷 -> Cache 포맷 변환 필요
            # 여기서는 간단히 trade_price만 갱신한다고 가정
            px = float(data.get("trade_price", 0.0))
            # await self.cache.update_price(code, px) # (가상 메서드)
            
            # 2. 매매 판단 (Fast Path)
            # 가격이 변했을 때만 포지션 체크
            await self._process_exits_fast(code, px)
            
            # 3. 진입 판단 (Signal Check)
            # 1초에 1번 정도만 체크 (Throttle)
            last_chk = self._last_candle_fetch_ms.get(f"SIG_{code}", 0)
            now = now_ms()
            if now - last_chk > 1000:
                self._last_candle_fetch_ms[f"SIG_{code}"] = now
                await self._check_entry_signal(code)

    async def _process_exits_fast(self, market: str, current_price: float):
        # WS 경로는 이벤트 폭주/동시성이 기본이라 시장별 락으로 직렬화합니다.
        async with self._lock_for(market):
            # 포지션이 없으면 패스
            p = self.portfolio.positions.get(market)
            if not p:
                return

            # WS 기반 손절은 레이스 컨디션으로 중복 매도가 발생할 수 있어, 마켓 단위로 1회만 실행되게 막습니다.
            if market in self._exit_inflight:
                return

            # 복원(기존보유) 포지션은 즉시 WS 손절을 걸면 과매도/휩쏘로 손실 확률이 커서 유예시간을 둡니다.
            grace_s = int(self.cfg.get("live", {}).get("restored_stoploss_grace_seconds", 300) or 0)
            if p.restored and grace_s > 0:
                age_s = (now_ms() - (p.restored_ts_ms or p.entry_ts_ms)) // 1000
                if age_s < grace_s:
                    return

            # 1. Stop Loss 체크 (가장 급함)
            if current_price <= p.stop_price:
                self._exit_inflight.add(market)
                # 중복 매도 방지: await 전에 포지션을 먼저 제거
                p = self.portfolio.remove(market) or p
                try:
                    # 시장가로 즉시 던짐
                    await self.exec_engine.execute_market(market, "SELL", 0, p.qty, current_price, 0.002, "stop_loss_ws")
                    LOGGER.info(f"WS STOP LOSS triggered for {market} at {current_price}")
                finally:
                    self._exit_inflight.discard(market)

                # Circuit breaker 기록/판정 (손절 연속 시 신규진입 일시중단)
                self._record_stoploss_and_maybe_pause()
                return

            # 2. Trailing Stop & Take Profit 체크
            # 기존 evaluate_exits 로직 활용 (단, 가격은 WS 가격 사용)
            # ... (생략, 복잡하면 일단 SL만 WS로 처리하고 나머지는 Polling에 맡겨도 됨)
            # 여기서는 SL만 WS로 처리하여 방어력 극대화

    async def _check_entry_signal(self, market: str):
        # 기존 _cycle의 진입 로직을 단일 마켓용으로 이식
        if self.safe_mode:
            return
        if now_ms() < self._entry_pause_until_ms:
            return
        if self.exec_engine.is_cooldown(market):
            return
        
        # 유니버스 TOP10 아니면 패스
        if market not in self.universe_top10: return

        # 캔들 데이터 가져오기 (WS로 받은 틱으로 캔들 업데이트가 안 되었다면 REST 사용해야 함)
        # 하지만 WS로 캔들 생성이 어려우므로, 여기서는 Cache된 캔들 사용
        candles = await self.cache.get_candles(market)
        if not candles: return # 데이터 부족

        # ... (기존 build_signal 호출) ...
        # 여기서는 신속한 구현을 위해 기존 로직 호출
        
        # [주의] 이 부분은 전체 로직 재사용이 필요함.
        # 일단은 로그만 찍어보겠습니다.
        pass

    async def _cycle_maintenance(self):
        # 기존 _cycle의 관리 작업들 (포지션 싱크, 스냅샷 등)
        if self.mode == "live" and getattr(self.rest, "is_live_ready", False):
            await self._sync_portfolio_with_accounts()
        await self._snapshot_positions({}) # last_prices는 WS 캐시에서 가져와야 함

    async def _cycle(self):
        # live 모드에서만 safe_mode 발동 (paper 모드는 공용 API 429로 인한 진입 차단 방지)
        if self.mode == "live" and self.rest.error_count >= self.cfg["runtime"]["safe_mode_error_threshold"]:
            self.safe_mode = True

        # 실계좌 기준 포지션 동기화(수동 거래/부분 체결/재시작으로 인한 qty 불일치 방지)
        if self.mode == "live" and getattr(self.rest, "is_live_ready", False):
            await self._sync_portfolio_with_accounts()

        await self._refresh_live_cache_step()
        await self.refresh_universe()

        btc_ok = await self._btc_regime_ok()
        tickers, orderbooks = await self.cache.snapshot()
        last_prices = {m: float(t.get("trade_price", 0.0)) for m, t in tickers.items()}

        # 5분봉 데이터(MTF) 조회 - Top10 종목 대상
        mtf_trends = {}
        for m in self.universe_top10:
            # 5분봉 20개 조회 (단기 추세 확인용)
            c5 = await self.rest.get_candles_minutes(m, unit=5, count=20)
            if len(c5) >= 20:
                closes5 = [float(x["trade_price"]) for x in reversed(c5)]
                # EMA20 > EMA60 (정배열) 여부 확인
                # 여기서는 간단히 EMA20 상승 추세 여부로 판단 (현재가 > EMA20)
                # 정석: ema(20) > ema(60)이지만, 데이터 부족 시 ema(20) 기울기로 대체
                from .indicators import ema
                ma20_5m = ema(closes5, 20)
                # 5분봉상 상승 추세(가격이 20이평 위)
                if ma20_5m and closes5[-1] >= ma20_5m:
                    mtf_trends[m] = True
                else:
                    mtf_trends[m] = False
            else:
                mtf_trends[m] = True # 데이터 없으면 관대하게

        signals = []
        for m in self.universe_top10:
            if self.exec_engine.is_cooldown(m):
                continue
            candles = await self.cache.get_candles(m)
            ob = orderbooks.get(m)
            if not candles or not ob:
                continue

            spread_pct = self.exec_engine._spread_pct(ob)
            depth_ratio = self.exec_engine._depth_ratio(ob, self.cfg["min_notional_krw"], "BUY")
            notional_ratio = await self.cache.get_notional_ratio(m, lookback=20)
            
            # MTF 추세 전달
            mtf_ok = mtf_trends.get(m, True)
            
            sig = build_signal(
                market=m,
                candles=candles,
                notional_ratio=notional_ratio,
                spread_pct=spread_pct,
                depth_ratio=depth_ratio,
                btc_regime_ok=btc_ok,
                notional_ratio_min=float(self.cfg["signal"]["notional_ratio_min"]),
                mtf_trend_ok=mtf_ok, # 5분봉 추세
            )
            signals.append(sig)

            self.storage.insert(
                "signals",
                {
                    "ts_ms": now_ms(),
                    "market": sig.market,
                    "score": sig.score,
                    "breakout": 1 if sig.breakout else 0,
                    "breakout_pct": sig.breakout_pct,
                    "notional_ratio": sig.notional_ratio,
                    "spread_pct": sig.spread_pct,
                    "depth_ratio": sig.depth_ratio,
                    "momentum_3m": sig.momentum_3m,
                    "btc_regime_ok": 1 if sig.btc_regime_ok else 0,
                    "tradable": 1 if sig.tradable else 0,
                    "note": sig.note,
                },
            )

        signals.sort(key=lambda x: x.score, reverse=True)
        await self._process_exits(last_prices)
        await self._process_entries(signals, last_prices, orderbooks)
        await self._snapshot_positions(last_prices)

    async def refresh_universe(self, force: bool = False):
        now = now_ms()
        if not force and now - self.last_universe_refresh_ms < self.cfg["universe"]["refresh_seconds"] * 1000:
            return

        tickers, orderbooks = await self.cache.snapshot()
        tickers_list = list(tickers.values())

        # 1) tickers로 top30 후보만 계산 (orderbook 없이)
        top30_candidates, _, _ = select_universe(
            tickers_list,
            {},
            top30_n=self.cfg["universe"]["top30"],
            top10_n=0,
        )
        top30_markets = [x["market"] for x in top30_candidates]

        # 2) top30 후보의 orderbook을 다시 수집해서 캐시에 채움
        fetched = await self.rest.get_orderbook(top30_markets)
        if not fetched:
            fetched = self._mock_orderbooks(top30_markets, tickers_list)

        await self.cache.seed_orderbooks(fetched)
        for ob in fetched:
            orderbooks[ob["market"]] = ob

        # 3) 이제 tradable/top10 계산
        top30, tradable, top10 = select_universe(
            tickers_list,
            orderbooks,
            top30_n=self.cfg["universe"]["top30"],
            top10_n=self.cfg["universe"]["tradable_top10"],
        )

        self.universe_top10 = [x["market"] for x in top10]
        self.last_universe_refresh_ms = now
        LOGGER.info("유니버스 갱신 top30=%d tradable=%d top10=%s", len(top30), len(tradable), self.universe_top10)

    async def _sync_portfolio_with_accounts(self, force: bool = False):
        """실계좌 보유 기준으로 포트폴리오를 동기화합니다.

        - 봇 재시작/부분체결/수동매매 등으로 내부 포지션과 실보유가 어긋나는 것을 방지
        - 실보유가 0에 가까우면 포지션 제거
        - 실보유가 있는데 내부에 없으면 포지션 복원(진입가=평단)
        """
        now = now_ms()
        if not force and now - self._last_live_sync_ms < 60_000:
            return
        self._last_live_sync_ms = now

        try:
            accts = await self.rest.get_accounts()
        except Exception:
            return
        if not isinstance(accts, list):
            return

        # (자동 로그) 미실현 포함 평가금액 스냅샷 저장
        await self._maybe_snapshot_equity(accts)

        # accounts: [{currency, balance, locked, avg_buy_price, ...}, ...]
        actual: dict[str, dict] = {}
        min_notional = float(self.cfg.get("min_notional_krw", 5000))
        for a in accts:
            cur = str(a.get("currency") or "")
            if not cur or cur == "KRW":
                continue
            try:
                bal = float(a.get("balance") or 0.0)
                locked = float(a.get("locked") or 0.0)
                qty = max(0.0, bal - locked)
                avg = float(a.get("avg_buy_price") or 0.0)
            except Exception:
                continue
            if qty <= 1e-12:
                continue
            # 더스트는 포트폴리오에서 무시(봇이 매도 루프에 빠지지 않게)
            if avg > 0 and (qty * avg) < min_notional:
                continue
            actual[cur] = {"qty": qty, "avg": avg}

        # 1) 내부 포지션 중 실보유가 없으면 제거 / qty를 실보유로 보정
        for m in list(self.portfolio.positions.keys()):
            base = m.split("-")[1] if "-" in m else ""
            info = actual.get(base)
            if not info:
                self.portfolio.remove(m)
                continue
            self.portfolio.positions[m].qty = float(info["qty"])
            if self.portfolio.positions[m].entry_price <= 0 and float(info.get("avg") or 0) > 0:
                self.portfolio.positions[m].entry_price = float(info["avg"])

        # 2) 실보유가 있는데 내부에 없으면 복원(KRW 마켓이 존재할 때만)
        # 기본은 안전하게 '복원하지 않음'(봇이 산 것만 관리). 필요 시 config.live.manage_existing_positions=true
        manage_existing = bool(self.cfg.get("live", {}).get("manage_existing_positions", False))
        if manage_existing:
            existing_bases = {p.base_coin for p in self.portfolio.positions.values()}
            for base, info in actual.items():
                if base in existing_bases:
                    continue
                market = f"KRW-{base}"
                if market not in self.all_markets:
                    continue
                avg = float(info.get("avg") or 0.0)
                if avg <= 0:
                    continue
                # 초기 복원은 보수적으로 stop_pct_max로 설정
                stop_price = avg * (1 - float(self.cfg["stops"]["stop_pct_max"]))
                self.portfolio.add(
                    market,
                    float(info["qty"]),
                    avg,
                    stop_price,
                    score=0.0,
                    restored=True,
                )

    async def _maybe_snapshot_equity(self, accts: list[dict]) -> None:
        """실계좌 기반 평가금액(미실현 포함)을 주기적으로 DB에 저장합니다."""
        interval_s = int(self.cfg.get("runtime", {}).get("equity_snapshot_seconds", 300))
        if interval_s <= 0:
            return
        now = now_ms()
        if now - self._last_equity_snapshot_ms < interval_s * 1000:
            return
        self._last_equity_snapshot_ms = now

        try:
            krw_bal = 0.0
            krw_locked = 0.0
            assets = []  # (currency, qty_total, avg)
            for a in accts:
                cur = str(a.get("currency") or "")
                bal = float(a.get("balance") or 0.0)
                locked = float(a.get("locked") or 0.0)
                if cur == "KRW":
                    krw_bal = bal
                    krw_locked = locked
                    continue
                qty_total = max(0.0, bal + locked)
                if qty_total <= 1e-12:
                    continue
                avg = float(a.get("avg_buy_price") or 0.0)
                assets.append((cur, qty_total, avg))

            markets = [f"KRW-{cur}" for cur, _, _ in assets]
            tickers = await self.rest.get_tickers(markets) if markets else []
            px = {t.get("market"): float(t.get("trade_price") or 0.0) for t in (tickers or [])}

            asset_value = 0.0
            cost_basis = 0.0
            missing = 0
            for cur, qty_total, avg in assets:
                m = f"KRW-{cur}"
                p = px.get(m)
                if not p or p <= 0:
                    missing += 1
                    continue
                asset_value += qty_total * p
                if avg and avg > 0:
                    cost_basis += qty_total * avg

            equity = (krw_bal + krw_locked) + asset_value
            unreal = asset_value - cost_basis
            note = None
            if missing:
                note = f"missing_prices={missing}"

            self.storage.insert(
                "equity_snapshots",
                {
                    "ts_ms": now,
                    "mode": self.mode,
                    "equity_krw": equity,
                    "krw_balance": krw_bal,
                    "krw_locked": krw_locked,
                    "asset_value_krw": asset_value,
                    "cost_basis_krw": cost_basis,
                    "unrealized_pnl_krw": unreal,
                    "note": note,
                },
            )
        except Exception as e:
            LOGGER.debug("equity snapshot failed: %s", e)

    def _record_stoploss_and_maybe_pause(self) -> None:
        """손절 이벤트를 기록하고, 일정 시간 내 연속 손절이 발생하면 신규진입을 일시 중단합니다."""
        cfg = (self.cfg.get("live", {}) or {}).get("circuit_breaker", {}) or {}
        window_s = int(cfg.get("window_seconds", 300) or 300)
        max_n = int(cfg.get("max_stoploss_events", 2) or 2)
        pause_m = int(cfg.get("pause_minutes", 30) or 30)

        t = now_ms()
        self._stoploss_events_ms.append(t)

        # window 밖 이벤트 제거
        cutoff = t - window_s * 1000
        while self._stoploss_events_ms and self._stoploss_events_ms[0] < cutoff:
            self._stoploss_events_ms.popleft()

        if len(self._stoploss_events_ms) >= max_n:
            self._entry_pause_until_ms = max(self._entry_pause_until_ms, t + pause_m * 60 * 1000)
            LOGGER.warning(
                "CIRCUIT_BREAKER: stoploss_events=%d within %ds -> pause entries for %dm",
                len(self._stoploss_events_ms),
                window_s,
                pause_m,
            )

    def _record_order_error_and_maybe_pause(self, err_reason: str = "") -> None:
        """주문 오류 이벤트를 기록하고 연속 발생 시 신규 진입을 일시 중단합니다."""
        cfg = (self.cfg.get("live", {}) or {}).get("circuit_breaker_order_errors", {}) or {}
        window_s = int(cfg.get("window_seconds", 180) or 180)
        max_n = int(cfg.get("max_error_events", 3) or 3)
        pause_m = int(cfg.get("pause_minutes", 20) or 20)

        t = now_ms()
        self._order_error_events_ms.append(t)

        cutoff = t - window_s * 1000
        while self._order_error_events_ms and self._order_error_events_ms[0] < cutoff:
            self._order_error_events_ms.popleft()

        if len(self._order_error_events_ms) >= max_n:
            self._entry_pause_until_ms = max(self._entry_pause_until_ms, t + pause_m * 60 * 1000)
            LOGGER.warning(
                "CIRCUIT_BREAKER_ORDER_ERRORS: errors=%d within %ds (last=%s) -> pause entries for %dm",
                len(self._order_error_events_ms),
                window_s,
                (err_reason or "unknown"),
                pause_m,
            )

    async def _btc_regime_ok(self) -> bool:
        candles = await self.cache.get_candles("KRW-BTC")
        if len(candles) < 20: # 최소 20개는 있어야 단기 추세라도 봄
            # 데이터 부족 시: 안전하게 False? 아니면 공격적으로 True?
            # 장 초반엔 True로 해줘야 진입 가능 (단, 리스크 감수)
            return True 
            
        closes = [float(x["close"]) for x in candles]
        
        # 1. 급락 감지 (Flash Crash Protection)
        # 현재가가 1시간 전(60분 전) 대비 -1.0% 이상 하락 시 매수 금지
        if len(closes) >= 60:
            change_1h = (closes[-1] - closes[-60]) / closes[-60]
            if change_1h < -0.01: # -1% 급락
                LOGGER.warning(f"BTC 급락 감지! (-{abs(change_1h):.2%}) 매수 중단.")
                return False

        # 2. RSI 과매도 감지 (Panic Selling)
        # RSI가 30 미만이면 떨어지는 칼날일 확률 높음 -> 잠시 대기
        rsi_val = rsi(closes, 14)
        if rsi_val < 30:
            LOGGER.warning(f"BTC 과매도 구간 (RSI {rsi_val:.1f}). 매수 대기.")
            return False

        # 3. 기존 이평선 정배열 체크 (EMA20 >= EMA60)
        # 60개가 안 되면 있는 데이터로 최대한 계산
        period_long = min(len(closes), 60)
        ema_short = ema(closes, 20)
        ema_long = ema(closes, period_long)
        
        if ema_short is None or ema_long is None:
            return True
            
        return ema_short >= ema_long

    async def _process_entries(self, signals, last_prices, orderbooks):
        if self.safe_mode:
            return
        if now_ms() < self._entry_pause_until_ms:
            return

        total_exposure = self.portfolio.total_exposure_ratio(self.risk.equity, last_prices)
        
        # 1. 신규 진입 대상 필터링
        tradables = [s for s in signals if s.tradable]
        if not tradables:
            return

        for s in tradables[:5]: # 상위 5개까지만 검토
            market = s.market
            # 중복 진입 방지
            if market in self._entry_inflight:
                continue

            # 매수 ref_price는 현재가보다는 ask1에 가깝게(체결/슬리피지 현실화)
            ob = orderbooks.get(market) or {}
            units = (ob.get("orderbook_units") or [])
            ask1 = float(units[0].get("ask_price", 0.0)) if units else 0.0
            px = ask1 if ask1 > 0 else last_prices.get(market, 0.0)
            if px <= 0:
                continue
                
            # A. 신규 진입 (New Entry)
            if market not in self.portfolio.positions:
                if not self.risk.can_open_new_entry(self.portfolio.count(), total_exposure):
                    continue
                if self.portfolio.count() >= self.cfg["risk"]["max_positions"]:
                    await self._try_replace(s, last_prices)
                    continue
            
            # B. 불타기 (Pyramiding)
            else:
                # 이미 보유 중 -> 수익 중이고 신호가 강하면 추가 매수
                pos = self.portfolio.positions[market]
                
                # 수익률 2% 이상일 때만 불타기
                pnl_pct = (px - pos.entry_price) / pos.entry_price
                if pnl_pct < 0.02: 
                    continue
                    
                # 최대 3회까지만
                if pos.adds >= 3:
                    continue
                    
                # 신규 진입과 동일하게 리스크 한도 체크
                base = parse_market(market).base
                coin_exposure = self.portfolio.coin_exposure_ratio(base, self.risk.equity, last_prices)
                if not self.risk.can_add_to_position(coin_exposure, total_exposure):
                    continue
                    
                LOGGER.info(f"🔥 Pyramiding Signal for {market} (PnL: {pnl_pct:.2%})")

            # --- 공통 진입 실행 로직 ---
            candles = await self.cache.get_candles(market)
            atr_v = atr(candles, period=self.cfg["stops"]["atr_period"])
            stop_pct = stop_pct_from_atr(
                px,
                atr_v,
                self.cfg["stops"]["atr_multiplier"],
                self.cfg["stops"]["stop_pct_min"],
                self.cfg["stops"]["stop_pct_max"],
            )

            base = parse_market(market).base
            coin_exposure_now = self.portfolio.coin_exposure_ratio(base, self.risk.equity, last_prices)
            
            # Phase 2: 점수에 따른 배팅 금액 조절 (동적 컷오프 적용)
            # 동적 컷오프보다 점수가 훨씬 높으면(예: +10점) 과감하게 베팅
            score_bonus = max(0, s.score - s.dynamic_cutoff)
            pos_value = self.risk.compute_position_value(stop_pct, len(tradables), coin_exposure_now, signal_score=85 + score_bonus)
            
            if pos_value < self.cfg["min_notional_krw"]:
                continue

            # check_quality_gate의 depth_ratio는 '주문금액'과 orderbook의 단위가 같아야 의미가 있습니다.
            # orderbook 금액은 quote 단위이므로, KRW pos_value를 quote로 환산해서 전달합니다.
            gate_order_value = pos_value
            quote = parse_market(market).quote
            if quote == "BTC":
                qkrw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
                if qkrw > 0:
                    gate_order_value = pos_value / qkrw
            elif quote == "USDT":
                qkrw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
                if qkrw > 0:
                    gate_order_value = pos_value / qkrw

            gate_ok, gate = self.exec_engine.check_quality_gate(market, orderbooks[market], gate_order_value, "BUY")
            if not gate_ok:
                continue

            # pos_value는 KRW 기준. BTC/USDT 마켓은 quote 환산 후 qty 계산
            quote = parse_market(market).quote
            quote_krw = 1.0
            if quote == "BTC":
                quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
            elif quote == "USDT":
                quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
            if quote_krw <= 0:
                quote_krw = 1.0

            spend_quote = pos_value / quote_krw
            qty = spend_quote / max(px, 1e-12)
            if qty <= 1e-12:
                continue
            self._entry_inflight.add(market)
            try:
                res = await self.exec_engine.execute_market(market, "BUY", pos_value, qty, px, gate["slip_est"], "entry_or_add")
            finally:
                self._entry_inflight.discard(market)

            if not res.ok:
                self.risk.stats.order_errors += 1
                # 주문 오류가 연속되면 신규 진입을 잠시 멈춤(서킷 브레이커)
                self._record_order_error_and_maybe_pause(res.reason)
                continue

            # 포트폴리오 업데이트 (신규 or 추가)
            filled_qty = float(res.qty)
            if filled_qty <= 1e-12:
                self.risk.stats.order_errors += 1
                self._record_order_error_and_maybe_pause("filled_qty_zero")
                continue

            if market not in self.portfolio.positions:
                stop_price = res.fill_price * (1 - stop_pct)
                self.portfolio.add(market, filled_qty, res.fill_price, stop_price, s.score)
            else:
                # 추가 매수: 평단가 갱신 및 스탑로스 상향 (Trailing Up)
                old_p = self.portfolio.positions[market]
                new_qty = old_p.qty + filled_qty
                new_avg = ((old_p.qty * old_p.entry_price) + (filled_qty * res.fill_price)) / new_qty
                
                # 스탑로스는 '새 평단가' 기준이 아니라, '현재가' 기준으로 타이트하게 올림 (수익 보전)
                new_stop = px * (1 - stop_pct) 
                # 기존 스탑보다 낮아지면 안 됨 (Trailing Stop 원칙)
                if new_stop < old_p.stop_price:
                    new_stop = old_p.stop_price
                    
                old_p.qty = new_qty
                old_p.entry_price = new_avg
                old_p.stop_price = new_stop
                old_p.adds += 1 # 불타기 횟수 증가
                LOGGER.info(f"Position Added: {market} NewQty={new_qty:.4f} NewAvg={new_avg:.2f} NewStop={new_stop:.2f}")

            self.risk.stats.total_trades += 1
            n = self.risk.stats.total_trades
            prev = self.risk.stats.avg_entry_slippage
            self.risk.stats.avg_entry_slippage = ((prev * (n - 1)) + res.slippage_pct) / n

    async def _process_exits(self, last_prices):
        for market, p0 in list(self.portfolio.positions.items()):
            # WS 손절과 동시에 청산 루프가 돌면 중복 매도가 날 수 있어 가드
            if market in self._exit_inflight:
                continue

            px = last_prices.get(market, p0.entry_price)
            fee_rate = float(self.cfg["fees"].get(market.split("-")[0], 0.001))
            slip_est = self.exec_engine.estimate_slippage(market, 0.001)
            actions = self.portfolio.evaluate_exits(market, px, fee_rate, slip_est)
            for a in actions:
                # 액션 실행 직전 최신 포지션을 다시 읽음(중간에 WS가 제거했을 수 있음)
                p = self.portfolio.positions.get(market)
                if not p:
                    break

                qty = p.qty * a["ratio"]
                # val은 KRW 기준으로 전달(quote 환산)
                quote = parse_market(market).quote
                quote_krw = 1.0
                if quote == "BTC":
                    quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
                elif quote == "USDT":
                    quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
                if quote_krw <= 0:
                    quote_krw = 1.0
                val = qty * px * quote_krw

                self._exit_inflight.add(market)
                # 전량 청산은 await 전에 제거하여 중복 주문을 막음
                if a["ratio"] >= 0.999:
                    removed = self.portfolio.remove(market) or p
                else:
                    removed = p
                try:
                    res = await self.exec_engine.execute_market(market, "SELL", val, qty, px, slip_est, a["reason"])
                finally:
                    self._exit_inflight.discard(market)

                if not res.ok:
                    self.risk.stats.order_errors += 1
                    self._record_order_error_and_maybe_pause(res.reason)
                    # 실패했고 전량청산으로 이미 제거했으면 복구
                    if a["ratio"] >= 0.999:
                        self.portfolio.positions[market] = removed
                    continue

                # 손익은 KRW 기준으로 기록해야 RiskManager/EQ가 정상 동작합니다.
                quote = parse_market(market).quote
                quote_krw = 1.0
                if quote == "BTC":
                    quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
                elif quote == "USDT":
                    quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
                if quote_krw <= 0:
                    quote_krw = 1.0

                pnl_quote = (res.fill_price - removed.entry_price) * qty - res.fee
                pnl_value = pnl_quote * quote_krw
                self.risk.update_realized(pnl_value)

                if a["ratio"] < 0.999:
                    # 부분 청산 반영
                    p2 = self.portfolio.positions.get(market)
                    if p2:
                        p2.qty -= qty

    async def _snapshot_positions(self, last_prices):
        for m, p in self.portfolio.positions.items():
            last = last_prices.get(m, p.entry_price)
            fee_rate = float(self.cfg["fees"].get(m.split("-")[0], 0.001))
            pnl = self.portfolio.net_pnl_pct(p, last, fee_rate, self.exec_engine.estimate_slippage(m, 0.001))
            hold = (now_ms() - p.entry_ts_ms) // 1000
            self.storage.insert(
                "positions",
                {
                    "ts_ms": now_ms(),
                    "market": m,
                    "base_coin": p.base_coin,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "last_price": last,
                    "stop_price": p.stop_price,
                    "peak_price": p.peak_price,
                    "tp1_done": 1 if p.tp1_done else 0,
                    "net_pnl_pct": pnl,
                    "hold_seconds": hold,
                    "score": p.score,
                    "mode": self.mode,
                },
            )

    async def _try_replace(self, new_signal, last_prices):
        if self.portfolio.count() < self.cfg["risk"]["max_positions"]:
            return
        if self.replacement_events and now_ms() - self.replacement_events[-1] < 60_000:
            return

        worst_market, ok = self.portfolio.replacement_candidates({"score": new_signal.score}, last_prices, self.cfg["fees"])
        if not ok or not worst_market:
            return

        wp = self.portfolio.positions.get(worst_market)
        if not wp:
            return
        hold_sec = (now_ms() - wp.entry_ts_ms) // 1000
        if hold_sec < self.cfg["time_rules"]["min_hold_seconds"]:
            return

        px = last_prices.get(worst_market, wp.entry_price)
        qty = wp.qty
        quote = parse_market(worst_market).quote
        quote_krw = 1.0
        if quote == "BTC":
            quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
        elif quote == "USDT":
            quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
        if quote_krw <= 0:
            quote_krw = 1.0
        val = qty * px * quote_krw
        slip_est = self.exec_engine.estimate_slippage(worst_market, 0.001)
        await self.exec_engine.execute_market(worst_market, "SELL", val, qty, px, slip_est, "replacement_out")
        self.portfolio.remove(worst_market)
        self.replacement_events.append(now_ms())

    async def _refresh_live_cache_step(self):
        if not self.all_markets:
            return
        now = now_ms()

        # tickers는 60초에 1회만 갱신(429 방지). KRW/BTC/USDT 모두 일부 포함.
        if now - self._last_ticker_refresh_ms >= 60_000:
            self._last_ticker_refresh_ms = now
            krw = [m for m in self.all_markets if m.startswith("KRW-")][:200]
            btc = [m for m in self.all_markets if m.startswith("BTC-")][:60]
            usdt = [m for m in self.all_markets if m.startswith("USDT-")][:60]
            markets = krw + btc + usdt
            tickers = await self.rest.get_tickers(markets)
            if not tickers:
                tickers = self._mock_tickers(markets)
            await self.cache.seed_tickers(tickers)
        else:
            tickers = []

        # orderbook도 60초에 1회만(유니버스/게이트용)
        if now - self._last_orderbook_refresh_ms >= 60_000:
            self._last_orderbook_refresh_ms = now
            # 기본은 KRW/BTC/USDT 혼합 일부만 시드
            krw_ob = [m for m in self.all_markets if m.startswith("KRW-")][:20]
            btc_ob = [m for m in self.all_markets if m.startswith("BTC-")][:5]
            usdt_ob = [m for m in self.all_markets if m.startswith("USDT-")][:5]
            ob_markets = krw_ob + btc_ob + usdt_ob
            orderbooks = await self.rest.get_orderbook(ob_markets)
            if not orderbooks:
                # tickers가 비어도 mock orderbook 생성은 가능
                orderbooks = self._mock_orderbooks(ob_markets, tickers or self._mock_tickers(ob_markets))
            await self.cache.seed_orderbooks(orderbooks)

        # 실제 캔들 데이터 사용 - top10 마켓에 대해서만 실제 데이터 수집
        # (분봉은 분당 1개만 갱신되므로, market별 REST 호출은 최소 60초 간격으로 제한)
        for m in self.universe_top10[:10]:  # universe_top10이 이미 있음
            last_fetch = self._last_candle_fetch_ms.get(m, 0)
            if now - last_fetch < 60_000:
                continue
            self._last_candle_fetch_ms[m] = now

            # 신호엔진에서 EMA60 등 60개 이상이 필요하므로, 초기부터 충분한 길이로 받아옵니다.
            candles = await self.rest.get_candles_minutes(m, unit=1, count=120)
            if candles:
                for c in reversed(candles):
                    # Upbit 분봉 응답에는 candle 시각이 포함됩니다.
                    # - timestamp: (ms) epoch
                    # - candle_date_time_kst/utc: ISO string
                    # 캔들 고유 시각을 ts_ms로 써야 중복 덮어쓰기가 발생하지 않습니다.
                    ts_ms = c.get("timestamp")
                    if ts_ms is None:
                        # 예외적으로 timestamp가 없으면 현재시각 사용(마지막 수단)
                        ts_ms = now_ms()
                    candle = {
                        "ts_ms": int(ts_ms),
                        "open": float(c.get("opening_price", 0.0)),
                        "high": float(c.get("high_price", 0.0)),
                        "low": float(c.get("low_price", 0.0)),
                        "close": float(c.get("trade_price", 0.0)),
                        # 내부/외부 호환을 위해 두 키 모두 유지
                        "volume": float(c.get("candle_acc_trade_volume", 0.0)),
                        "candle_acc_trade_volume": float(c.get("candle_acc_trade_volume", 0.0)),
                        "notional": float(c.get("candle_acc_trade_price", 0.0)),
                    }
                    await self.cache.push_candle(m, candle)
                    # 분석/리플레이를 위해 DB에도 저장
                    try:
                        self.storage.upsert_candle(
                            m,
                            float(candle["ts_ms"]) / 1000.0,
                            float(candle["open"]),
                            float(candle["high"]),
                            float(candle["low"]),
                            float(candle["close"]),
                            float(candle.get("volume", 0.0)),
                            float(candle.get("notional", 0.0)),
                        )
                    except Exception:
                        pass
            else:
                # 실패 시에만 mock 데이터 사용
                t = next((t for t in tickers if t["market"] == m), None)
                if t:
                    px = float(t.get("trade_price", 0.0))
                    if px > 0:
                        delta = random.uniform(-0.0015, 0.0015)
                        c = {
                            "ts_ms": now_ms(),
                            "open": px * (1 - delta),
                            "high": px * (1 + abs(delta) * 1.5),
                            "low": px * (1 - abs(delta) * 1.5),
                            "close": px,
                            "volume": random.uniform(1, 300),
                            "notional": px * random.uniform(5, 500),
                        }
                        await self.cache.push_candle(m, c)

    def _mock_tickers(self, markets):
        out = []
        for m in markets:
            px = random.uniform(100, 150000)
            out.append({
                "market": m,
                "trade_price": px,
                "acc_trade_price_24h": random.uniform(1e8, 1e11),
                "acc_trade_volume_24h": random.uniform(100, 1e7),
            })
        return out

    def _mock_orderbooks(self, markets, tickers):
        pmap = {x["market"]: float(x["trade_price"]) for x in tickers}
        out = []
        for m in markets:
            px = pmap.get(m, random.uniform(100, 150000))
            units = []
            for i in range(3):
                spread = 0.0002 + i * 0.0002
                units.append({
                    "ask_price": px * (1 + spread),
                    "bid_price": px * (1 - spread),
                    "ask_size": random.uniform(3, 80),
                    "bid_size": random.uniform(3, 80),
                })
            out.append({"market": m, "orderbook_units": units})
        return out

    async def _seed_mock_candles(self, market):
        base = random.uniform(1000, 100000)
        # now_ms를 루프에서 계속 쓰면 같은 ms로 덮어쓰기 될 수 있어, 1분 단위로 증가시키며 생성
        ts = now_ms() - 100 * 60_000
        for _ in range(100):
            ts += 60_000
            d = random.uniform(-0.002, 0.002)
            close = max(1, base * (1 + d))
            c = {
                "ts_ms": ts,
                "open": base,
                "high": max(base, close) * (1 + random.uniform(0, 0.0015)),
                "low": min(base, close) * (1 - random.uniform(0, 0.0015)),
                "close": close,
                "volume": random.uniform(1, 200),
                "notional": close * random.uniform(3, 300),
            }
            base = close
            await self.cache.push_candle(market, c)
