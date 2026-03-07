import asyncio
import logging
import random
from collections import deque
from typing import Any

from .execution import ExecutionEngine
from .indicators import atr, ema, rsi, stop_pct_from_atr
from .portfolio import Portfolio
from .signal_engine import build_signal, build_signal_simple
from .universe import select_universe
from .utils import now_ms, parse_market

LOGGER = logging.getLogger(__name__)


def run_backtest_30d(cfg: dict[str, Any]) -> dict[str, Any]:
    """Legacy 백테스트 (단순 시뮬레이션 - 실제 데이터 기반 아님).

    경고: 이 함수는 실제 백테스트 결과가 아닌 근사값을 반환합니다.
    정확한 백테스트를 원하시면 config의 backtest_days 설정을 사용하세요.
    """
    import logging

    LOGGER = logging.getLogger(__name__)
    LOGGER.warning(
        "LEGACY BACKTEST: Using estimated results (not actual data-driven). Use config backtest_days for accurate results."
    )

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
    @staticmethod
    def _should_auto_recover_safe_mode(
        *,
        safe_mode: bool,
        error_count: int,
        now_ms: int,
        stable_since_ms: int,
        entry_pause_until_ms: int,
        recover_seconds: int,
        auto_recover: bool,
    ) -> bool:
        if not safe_mode:
            return False
        if not auto_recover:
            return False
        if stable_since_ms <= 0:
            return False
        if error_count != 0:
            return False
        if now_ms < entry_pause_until_ms:
            return False
        return (now_ms - stable_since_ms) >= int(recover_seconds) * 1000

    def __init__(
        self,
        cfg: dict[str, Any],
        rest_client,
        cache,
        storage,
        risk_manager,
        reporter,
        mode: str,
    ):
        self.cfg = cfg
        self.rest = rest_client
        self.cache = cache
        self.storage = storage
        self.risk = risk_manager
        self.reporter = reporter
        self.mode = mode
        self.exec_engine = ExecutionEngine(cfg, storage, mode, rest_client=rest_client)
        self.portfolio = Portfolio(cfg)
        self.ws = None  # WS 초기화는 initialize에서

        # WS 이벤트 폭주 방지: 콜백에서 태스크를 만들지 않고 큐로 넘겨 단일 소비자가 처리
        self._ws_in_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self._ws_consumer_task: asyncio.Task[None] | None = None
        self._ws_drop_count: int = 0
        self._last_ws_drop_event_ms: int = 0

        # 운영 heartbeat(전략 루프 생존/지연 관측)
        self._last_heartbeat_event_ms: int = 0
        self.universe_top10: list[str] = []
        self.all_markets: list[str] = []
        self.last_universe_refresh_ms = 0
        self.replacement_events = deque(maxlen=30)
        self.safe_mode = False
        self._safe_mode_since_ms: int = 0
        self._last_order_error_ms: int = 0

        # --- 동시성/중복주문 방지 ---
        # WS 손절 등에서 동일 마켓이 동시에 여러 번 청산되는 것을 방지
        self._exit_inflight: set[str] = set()
        # 진입도 마켓 단위로 중복 실행 방지(초단위 루프/WS가 겹칠 수 있음)
        self._entry_inflight: set[str] = set()

        # --- Circuit Breaker ---
        self._stoploss_events_ms = deque(maxlen=20)
        self._order_error_events_ms = deque(maxlen=50)
        self._entry_pause_until_ms: int = 0
        self._entry_pause_logged: bool = False

        # --- Market quarantine (per-market) ---
        # WS 손절/주문오류가 반복되는 코인을 일정 시간 격리하여 루프를 끊습니다.
        self._market_stoploss_events_ms: dict[str, deque[int]] = {}

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

        # 운영 상태 추적 (헬스체크/관측용)
        self._last_ws_tick_ms: int = 0
        self._last_ws_tick_market: str | None = None
        self._last_ws_tick_event_ms: int = 0
        self._last_candle_success_ms: dict[str, int] = {}

        # 종료 제어
        self._stop_event: asyncio.Event | None = None

    def _lock_for(self, market: str) -> asyncio.Lock:
        lock = self._market_locks.get(market)
        if lock is None:
            lock = asyncio.Lock()
            self._market_locks[market] = lock
        return lock

    async def initialize(self):
        # 0) 마켓 목록 확보
        try:
            self.all_markets = (
                await self._call_with_retry(
                    lambda: self.rest.get_markets(), name="get_markets_init"
                )
                or []
            )
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
                tickers = (
                    await self._call_with_retry(
                        lambda: self.rest.get_tickers(seed_markets), name="seed_tickers"
                    )
                    or []
                )
            except Exception as e:
                LOGGER.warning("seed tickers failed: %s", e)
                tickers = []
            if tickers:
                await self.cache.seed_tickers(tickers)

            try:
                obs = (
                    await self._call_with_retry(
                        lambda: self.rest.get_orderbook(seed_markets[:30]),
                        name="seed_orderbook",
                    )
                    or []
                )
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
        try:
            self.storage.log_event(
                "INFO", "WS_STARTED", None, f"markets={len(markets)}"
            )
        except Exception:
            pass

        # WS 소비자 시작(중복 시작 방지)
        if self._ws_consumer_task is None or self._ws_consumer_task.done():
            self._ws_consumer_task = asyncio.create_task(self._ws_consumer_loop())

    async def run(self, stop_event: asyncio.Event | None = None):
        self._stop_event = stop_event
        await self.initialize()
        await self.reporter.start()

        # 하이브리드 모드:
        # 1. WS: 실시간 시세 수신 -> 손절(SL) 감시 (0.1초 반응)
        # 2. Loop: 10초마다 진입/익절 판단 (10초 반응)
        loop_interval = max(
            1.0, float(self.cfg.get("runtime", {}).get("loop_interval_seconds", 10))
        )
        while not (self._stop_event and self._stop_event.is_set()):
            try:
                await self._cycle()

                # 구독 목록 변경 감지 시 WS 재구독
                if self.ws and set(self.ws.markets) != set(self.universe_top10):
                    LOGGER.info("유니버스 변경으로 WS 재구독...")
                    await self.ws.stop()
                    try:
                        self.storage.log_event(
                            "INFO", "WS_STOPPED", None, "universe_resubscribe"
                        )
                    except Exception:
                        pass
                    self.ws.markets = list(self.universe_top10)
                    await self.ws.start()
                    try:
                        self.storage.log_event(
                            "INFO",
                            "WS_STARTED",
                            None,
                            f"markets={len(self.ws.markets)} universe_resubscribe",
                        )
                    except Exception:
                        pass

                    # 소비자는 유지되지만, 혹시 죽어있으면 재기동
                    if self._ws_consumer_task is None or self._ws_consumer_task.done():
                        self._ws_consumer_task = asyncio.create_task(
                            self._ws_consumer_loop()
                        )

            except Exception as e:
                LOGGER.exception("Main Loop Error: %s", e)

            # 10초 고정 슬립 대신 stop_event를 기다려 빠른 종료를 지원
            try:
                if self._stop_event is not None:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=loop_interval
                    )
                else:
                    await asyncio.sleep(loop_interval)
            except asyncio.TimeoutError:
                pass

        await self.shutdown()

    async def shutdown(self):
        try:
            if self._ws_consumer_task is not None:
                self._ws_consumer_task.cancel()
                try:
                    await self._ws_consumer_task
                except asyncio.CancelledError:
                    pass
            if self.ws is not None:
                await self.ws.stop()
        except Exception as e:
            LOGGER.warning("state machine shutdown warning: %s", e)

    def _on_ws_data(self, data: dict[str, Any]):
        # WS 콜백에서 create_task를 무한히 만들면 폭주/지연이 생길 수 있어 큐로 넘깁니다.
        try:
            if not self._ws_in_q.full():
                self._ws_in_q.put_nowait(data)
                return

            # 큐가 꽉 찼으면 드랍(백프레셔) + 이벤트로 관측
            self._ws_drop_count += 1
            now = now_ms()
            # 10초에 한 번만 이벤트로 남김(스팸 방지)
            if now - self._last_ws_drop_event_ms >= 10_000:
                self._last_ws_drop_event_ms = now
                try:
                    self.storage.log_event(
                        "WARN",
                        "WS_QUEUE_DROP",
                        None,
                        f"drops={self._ws_drop_count} qsize={self._ws_in_q.qsize()} max={self._ws_in_q.maxsize}",
                    )
                except Exception:
                    pass
        except Exception:
            # 큐 오류는 WS를 죽일 정도는 아니므로 무시
            return

    async def _ws_consumer_loop(self):
        # 단일 소비자 루프: WS 메시지를 순서대로 처리
        while True:
            try:
                data = await self._ws_in_q.get()
                await self._process_ws_data_async(data)
            except asyncio.CancelledError:
                return
            except Exception as e:
                LOGGER.exception("WS consumer error: %s", e)
                await asyncio.sleep(0.2)

    async def _process_ws_data_async(self, data: dict[str, Any]):
        ty = data.get("type")
        code = data.get("code")
        if not code:
            return

        if ty == "ticker":
            # 1. 시세 업데이트 (캐시 갱신)
            # WS 데이터 포맷 -> Cache 포맷 변환 필요
            # 여기서는 간단히 trade_price만 갱신한다고 가정
            px = float(data.get("trade_price", 0.0))
            now = now_ms()
            self._last_ws_tick_ms = now
            self._last_ws_tick_market = code
            if now - self._last_ws_tick_event_ms >= 60_000:
                self._last_ws_tick_event_ms = now
                try:
                    self.storage.log_event(
                        "INFO", "WS_TICK_OK", code, f"trade_price={px}"
                    )
                except Exception:
                    pass
            # await self.cache.update_price(code, px) # (가상 메서드)

            # 2. 매매 판단 (Fast Path)
            # 가격이 변했을 때만 포지션 체크
            await self._process_exits_fast(code, px)

            # 3. 진입 판단 (Signal Check)
            # 1초에 1번 정도만 체크 (Throttle)
            # 과거 인스턴스/이상 상태에서 속성이 누락돼도 죽지 않도록 방어
            if not isinstance(getattr(self, "_last_candle_fetch_ms", None), dict):
                self._last_candle_fetch_ms = {}
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
            grace_s = int(
                self.cfg.get("live", {}).get("restored_stoploss_grace_seconds", 300)
                or 0
            )
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
                    try:
                        self.storage.log_event(
                            "INFO",
                            "SELL_ATTEMPT",
                            market,
                            f"reason=stop_loss_ws ratio=1.000 qty={p.qty:.8f} px={current_price:.4f} val_krw=0",
                        )
                    except Exception:
                        pass

                    # SELL의 최소주문금액(더스트) 판단을 위해 대략적인 주문금액을 넘깁니다.
                    order_value_krw = float(p.qty) * float(current_price)
                    res = await self.exec_engine.execute_market(
                        market,
                        "SELL",
                        order_value_krw,
                        p.qty,
                        current_price,
                        0.002,
                        "stop_loss_ws",
                    )
                    if not res.ok:
                        try:
                            self.storage.log_event(
                                "WARN",
                                "SELL_FAIL",
                                market,
                                f"reason={res.reason} action=stop_loss_ws",
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            self.storage.log_event(
                                "INFO",
                                "SELL_OK",
                                market,
                                f"fill_px={res.fill_price:.4f} qty={float(res.qty):.8f} action=stop_loss_ws",
                            )
                        except Exception:
                            pass

                    LOGGER.info(
                        f"WS STOP LOSS triggered for {market} at {current_price}"
                    )
                finally:
                    self._exit_inflight.discard(market)

                # Circuit breaker 기록/판정 (손절 연속 시 신규진입 일시중단)
                self._record_stoploss_and_maybe_pause()

                # market 단위 격리(추천값): WS 손절이 반복되는 코인은 단계적으로 격리 시간을 늘려 루프를 차단
                try:
                    live_cfg = self.cfg.get("live", {}) or {}
                    base_min = int(
                        live_cfg.get("market_quarantine_on_stoploss_minutes", 30) or 30
                    )
                    max_min = int(
                        live_cfg.get("market_quarantine_max_minutes", 360) or 360
                    )
                    window_s = int(
                        live_cfg.get("market_quarantine_stoploss_window_seconds", 3600)
                        or 3600
                    )

                    dq = self._market_stoploss_events_ms.get(market)
                    if dq is None:
                        dq = deque(maxlen=10)
                        self._market_stoploss_events_ms[market] = dq
                    nowv = now_ms()
                    dq.append(nowv)
                    # prune window
                    while dq and (nowv - dq[0] > window_s * 1000):
                        dq.popleft()

                    hits = len(dq)
                    # 1회: base, 2회: 2x, 3회: 4x ... (cap)
                    q_min = min(max_min, base_min * (2 ** max(0, hits - 1)))
                    self.exec_engine.cooldown_until_ms[market] = nowv + q_min * 60_000

                    try:
                        self.storage.log_event(
                            "WARN",
                            "MARKET_QUARANTINE",
                            market,
                            f"reason=stoploss_ws hits={hits} window_s={window_s} minutes={q_min}",
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

                try:
                    self.storage.log_event(
                        "WARN", "STOPLOSS_WS", market, f"px={current_price}"
                    )
                except Exception:
                    pass
                return

            # 2. Trailing Stop & Take Profit 체크
            # 기존 evaluate_exits 로직 활용 (단, 가격은 WS 가격 사용)
            # ... (생략, 복잡하면 일단 SL만 WS로 처리하고 나머지는 Polling에 맡겨도 됨)
            # 여기서는 SL만 WS로 처리하여 방어력 극대화

    async def _check_entry_signal(self, market: str):
        pass

    async def _cycle_maintenance(self):
        # 기존 _cycle의 관리 작업들 (포지션 싱크, 스냅샷 등)
        if self.mode == "live" and getattr(self.rest, "is_live_ready", False):
            await self._sync_portfolio_with_accounts()
        await self._snapshot_positions({})  # last_prices는 WS 캐시에서 가져와야 함

    async def _cycle(self):
        now = now_ms()

        self.risk.roll_daily()

        # live 모드에서만 safe_mode 발동 (paper 모드는 공용 API 429로 인한 진입 차단 방지)
        if (
            self.mode == "live"
            and self.rest.error_count
            >= self.cfg["runtime"]["safe_mode_error_threshold"]
        ):
            if not self.safe_mode:
                self._safe_mode_since_ms = now
                try:
                    self.storage.log_event(
                        "ERROR",
                        "SAFE_MODE_ON",
                        None,
                        f"error_count={self.rest.error_count}",
                    )
                except Exception:
                    pass
            self.safe_mode = True

        # safe_mode 자동 복구(조건 충족 시)
        if self.mode == "live" and self.safe_mode:
            live_cfg = self.cfg.get("live", {}) or {}
            recover_s = int(live_cfg.get("safe_mode_recover_seconds", 900) or 900)
            stable_since = max(
                self._safe_mode_since_ms or 0, self._last_order_error_ms or 0
            )
            if self._should_auto_recover_safe_mode(
                safe_mode=self.safe_mode,
                error_count=int(getattr(self.rest, "error_count", 0) or 0),
                now_ms=now,
                stable_since_ms=int(stable_since or 0),
                entry_pause_until_ms=int(self._entry_pause_until_ms or 0),
                recover_seconds=recover_s,
                auto_recover=bool(live_cfg.get("safe_mode_auto_recover", True)),
            ):
                self.safe_mode = False
                try:
                    self.storage.log_event(
                        "INFO", "SAFE_MODE_OFF", None, f"stable_s>={recover_s}"
                    )
                except Exception:
                    pass

        # entry pause 상태 변화를 이벤트로 남김
        if now < self._entry_pause_until_ms:
            if not self._entry_pause_logged:
                self._entry_pause_logged = True
                try:
                    left_s = int((self._entry_pause_until_ms - now) / 1000)
                    self.storage.log_event(
                        "WARN", "ENTRY_PAUSED", None, f"left_s={left_s}"
                    )
                except Exception:
                    pass
        else:
            if self._entry_pause_logged:
                self._entry_pause_logged = False
                try:
                    self.storage.log_event(
                        "INFO", "ENTRY_RESUMED", None, "pause_expired"
                    )
                except Exception:
                    pass

        # 전략 루프 heartbeat (1분에 1회) — '신호가 없다/멈췄다'를 즉시 구분 가능
        if now - self._last_heartbeat_event_ms >= 60_000:
            self._last_heartbeat_event_ms = now
            try:
                paused_left = max(0, int((self._entry_pause_until_ms - now) / 1000))
                last_ws_age_s = (
                    -1
                    if self._last_ws_tick_ms <= 0
                    else int((now - self._last_ws_tick_ms) / 1000)
                )
                if self._last_candle_success_ms:
                    last_candle_ms = max(self._last_candle_success_ms.values())
                    last_candle_age_s = int((now - last_candle_ms) / 1000)
                else:
                    last_candle_age_s = -1
                self.storage.log_event(
                    "INFO",
                    "STRATEGY_HEARTBEAT",
                    None,
                    f"mode={self.mode} safe_mode={self.safe_mode} err_count={getattr(self.rest, 'error_count', None)} "
                    f"positions={len(self.portfolio.positions)} paused_left_s={paused_left} ws_q={self._ws_in_q.qsize()} "
                    f"drops={self._ws_drop_count} last_ws_tick_age_s={last_ws_age_s} "
                    f"last_ws_market={self._last_ws_tick_market or '-'} last_candle_age_s={last_candle_age_s}",
                )
            except Exception:
                pass

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
            c5 = (
                await self._call_with_retry(
                    lambda: self.rest.get_candles_minutes(m, unit=5, count=20),
                    name=f"get_candles_5m:{m}",
                )
                or []
            )
            if len(c5) >= 20:
                closes5 = [float(x["trade_price"]) for x in reversed(c5)]
                ma20_5m = ema(closes5, 20)
                if ma20_5m and closes5[-1] >= ma20_5m:
                    mtf_trends[m] = True
                else:
                    mtf_trends[m] = False
            else:
                mtf_trends[m] = False

        signals = []
        for m in self.universe_top10:
            if self.exec_engine.is_cooldown(m):
                continue
            candles = await self.cache.get_candles(m)
            ob = orderbooks.get(m)
            if not candles or not ob:
                continue

            spread_pct = self.exec_engine._spread_pct(ob)
            depth_ratio = self.exec_engine._depth_ratio(
                ob, self.cfg["min_notional_krw"], "BUY"
            )
            notional_ratio = await self.cache.get_notional_ratio(m, lookback=20)

            # MTF 추세 전달
            mtf_ok = mtf_trends.get(m, True)
            ob = orderbooks.get(m)

            profile = str(
                (self.cfg.get("signal", {}) or {}).get("profile", "full")
            ).lower()
            builder = (
                build_signal_simple if profile in {"simple", "lite"} else build_signal
            )

            sig = builder(
                market=m,
                candles=candles,
                notional_ratio=notional_ratio,
                spread_pct=spread_pct,
                depth_ratio=depth_ratio,
                btc_regime_ok=btc_ok,
                notional_ratio_min=float(self.cfg["signal"]["notional_ratio_min"]),
                mtf_trend_ok=mtf_ok,
                orderbook=ob,
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
        await self._process_exits(last_prices, orderbooks)
        await self._process_entries(signals, last_prices, orderbooks)
        await self._snapshot_positions(last_prices)

    async def refresh_universe(self, force: bool = False):
        now = now_ms()
        if (
            not force
            and now - self.last_universe_refresh_ms
            < self.cfg["universe"]["refresh_seconds"] * 1000
        ):
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
        fetched = (
            await self._call_with_retry(
                lambda: self.rest.get_orderbook(top30_markets),
                name="refresh_universe_orderbook",
            )
            or []
        )
        if not fetched:
            if self.mode == "live":
                try:
                    self.storage.log_event(
                        "WARN",
                        "UNIVERSE_ORDERBOOK_FETCH_FAIL",
                        None,
                        "skip_universe_refresh_in_live",
                    )
                except Exception:
                    pass
                return
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
        LOGGER.info(
            "유니버스 갱신 top30=%d tradable=%d top10=%s",
            len(top30),
            len(tradable),
            self.universe_top10,
        )

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
            accts = await self._call_with_retry(
                lambda: self.rest.get_accounts(), name="get_accounts_sync"
            )
        except Exception:
            return
        if not isinstance(accts, list):
            return

        # (자동 로그) 미실현 포함 평가금액 스냅샷 저장
        await self._maybe_snapshot_equity(accts)

        # accounts: [{currency, balance, locked, avg_buy_price, ...}, ...]
        actual: dict[str, dict[str, float]] = {}
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
            if (
                self.portfolio.positions[m].entry_price <= 0
                and float(info.get("avg") or 0) > 0
            ):
                self.portfolio.positions[m].entry_price = float(info["avg"])

        # 2) 실보유가 있는데 내부에 없으면 복원(KRW 마켓이 존재할 때만)
        # 기본은 안전하게 '복원하지 않음'(봇이 산 것만 관리). 필요 시 config.live.manage_existing_positions=true
        manage_existing = bool(
            self.cfg.get("live", {}).get("manage_existing_positions", False)
        )
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

    async def _maybe_snapshot_equity(self, accts: list[dict[str, Any]]) -> None:
        """실계좌 기반 평가금액(미실현 포함)을 주기적으로 DB에 저장합니다."""
        interval_s = int(
            self.cfg.get("runtime", {}).get("equity_snapshot_seconds", 300)
        )
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
            tickers = (
                await self._call_with_retry(
                    lambda: self.rest.get_tickers(markets), name="get_tickers_equity"
                )
                if markets
                else []
            )
            px = {
                t.get("market"): float(t.get("trade_price") or 0.0)
                for t in (tickers or [])
            }

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
            self._entry_pause_until_ms = max(
                self._entry_pause_until_ms, t + pause_m * 60 * 1000
            )
            LOGGER.warning(
                "CIRCUIT_BREAKER: stoploss_events=%d within %ds -> pause entries for %dm",
                len(self._stoploss_events_ms),
                window_s,
                pause_m,
            )
            try:
                self.storage.log_event(
                    "WARN",
                    "CIRCUIT_BREAKER_STOPLOSS",
                    None,
                    f"count={len(self._stoploss_events_ms)} window_s={window_s} pause_m={pause_m}",
                )
            except Exception:
                pass

    def _record_order_error_and_maybe_pause(self, err_reason: str = "") -> None:
        """주문 오류 이벤트를 기록하고 연속 발생 시 신규 진입을 일시 중단합니다."""
        cfg = (self.cfg.get("live", {}) or {}).get(
            "circuit_breaker_order_errors", {}
        ) or {}
        window_s = int(cfg.get("window_seconds", 180) or 180)
        max_n = int(cfg.get("max_error_events", 3) or 3)
        pause_m = int(cfg.get("pause_minutes", 20) or 20)

        t = now_ms()
        self._order_error_events_ms.append(t)

        self._last_order_error_ms = t
        try:
            self.storage.log_event("WARN", "ORDER_ERROR", None, err_reason or "unknown")
        except Exception:
            pass

        cutoff = t - window_s * 1000
        while self._order_error_events_ms and self._order_error_events_ms[0] < cutoff:
            self._order_error_events_ms.popleft()

        if len(self._order_error_events_ms) >= max_n:
            self._entry_pause_until_ms = max(
                self._entry_pause_until_ms, t + pause_m * 60 * 1000
            )
            LOGGER.warning(
                "CIRCUIT_BREAKER_ORDER_ERRORS: errors=%d within %ds (last=%s) -> pause entries for %dm",
                len(self._order_error_events_ms),
                window_s,
                (err_reason or "unknown"),
                pause_m,
            )
            try:
                self.storage.log_event(
                    "WARN",
                    "CIRCUIT_BREAKER_ORDER_ERRORS",
                    None,
                    f"count={len(self._order_error_events_ms)} window_s={window_s} pause_m={pause_m} last={err_reason}",
                )
            except Exception:
                pass

            # 옵션이 켜져 있으면 안전모드로 승격(잔고/레이트리밋/권한 문제 등으로 주문이 계속 실패하는 상황)
            if bool(
                (self.cfg.get("live", {}) or {}).get("safe_mode_on_order_errors", True)
            ):
                if not self.safe_mode:
                    self.safe_mode = True
                    self._safe_mode_since_ms = t
                    try:
                        self.storage.log_event(
                            "ERROR",
                            "SAFE_MODE_ON_ORDER_ERRORS",
                            None,
                            f"count={len(self._order_error_events_ms)} window_s={window_s} last={err_reason}",
                        )
                    except Exception:
                        pass

    async def _btc_regime_ok(self) -> bool:
        candles = await self.cache.get_candles("KRW-BTC")
        if len(candles) < 20:  # 최소 20개는 있어야 단기 추세라도 봄
            # 데이터 부족 시: 안전하게 False? 아니면 공격적으로 True?
            # 장 초반엔 True로 해줘야 진입 가능 (단, 리스크 감수)
            return True

        closes = [float(x["close"]) for x in candles]

        # 1. 급락 감지 (Flash Crash Protection)
        # 현재가가 1시간 전(60분 전) 대비 -1.0% 이상 하락 시 매수 금지
        if len(closes) >= 60:
            change_1h = (closes[-1] - closes[-60]) / closes[-60]
            if change_1h < -0.01:  # -1% 급락
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

        total_exposure = self.portfolio.total_exposure_ratio(
            self.risk.equity, last_prices
        )

        # 1. 신규 진입 대상 필터링
        tradables = [s for s in signals if s.tradable]
        if not tradables:
            return

        for s in tradables[:5]:  # 상위 5개까지만 검토
            market = s.market
            # 마켓 격리/쿨다운(WS 손절 등) 중이면 진입 금지
            if self.exec_engine.is_cooldown(market):
                continue
            # 중복 진입 방지
            if market in self._entry_inflight:
                continue

            # 매수 ref_price는 현재가보다는 ask1에 가깝게(체결/슬리피지 현실화)
            ob = orderbooks.get(market) or {}
            units = ob.get("orderbook_units") or []
            ask1 = float(units[0].get("ask_price", 0.0)) if units else 0.0
            px = ask1 if ask1 > 0 else last_prices.get(market, 0.0)
            if px <= 0:
                continue

            # A. 신규 진입 (New Entry)
            if market not in self.portfolio.positions:
                if not self.risk.can_open_new_entry(
                    self.portfolio.count(), total_exposure
                ):
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
                coin_exposure = self.portfolio.coin_exposure_ratio(
                    base, self.risk.equity, last_prices
                )
                if not self.risk.can_add_to_position(coin_exposure, total_exposure):
                    continue

                LOGGER.info(f"🔥 Pyramiding Signal for {market} (PnL: {pnl_pct:.2%})")

            # --- 공통 진입 실행 로직 ---
            candles = await self.cache.get_candles(market)
            atr_v = atr(candles, period=self.cfg["stops"]["atr_period"])
            vol_regime = s.volatility_regime
            atr_mult = self.risk.get_adaptive_atr_multiplier(vol_regime)
            stop_pct = stop_pct_from_atr(
                px,
                atr_v,
                atr_mult,
                self.cfg["stops"]["stop_pct_min"],
                self.cfg["stops"]["stop_pct_max"],
            )

            base = parse_market(market).base
            coin_exposure_now = self.portfolio.coin_exposure_ratio(
                base, self.risk.equity, last_prices
            )

            # Phase 2: 점수에 따른 배팅 금액 조절 (동적 컷오프 적용)
            # 동적 컷오프보다 점수가 훨씬 높으면(예: +10점) 과감하게 베팅
            score_bonus = max(0, s.score - s.dynamic_cutoff)
            pos_value = self.risk.compute_position_value(
                stop_pct,
                len(tradables),
                coin_exposure_now,
                signal_score=85 + score_bonus,
                volatility_regime=vol_regime,
            )

            if pos_value < self.cfg["min_notional_krw"]:
                continue

            # check_quality_gate의 depth_ratio는 '주문금액'과 orderbook의 단위가 같아야 의미가 있습니다.
            # orderbook 금액은 quote 단위이므로, KRW pos_value를 quote로 환산해서 전달합니다.
            gate_order_value = pos_value
            quote = parse_market(market).quote
            if quote == "BTC":
                qkrw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
                if qkrw <= 0:
                    continue
                gate_order_value = pos_value / qkrw
            elif quote == "USDT":
                qkrw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
                if qkrw <= 0:
                    continue
                gate_order_value = pos_value / qkrw

            gate_ok, gate = self.exec_engine.check_quality_gate(
                market, orderbooks[market], gate_order_value, "BUY"
            )
            if not gate_ok:
                continue

            # pos_value는 KRW 기준. BTC/USDT 마켓은 quote 환산 후 qty 계산
            quote = parse_market(market).quote
            quote_krw = 1.0
            if quote == "BTC":
                quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
            elif quote == "USDT":
                quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
            if quote != "KRW" and quote_krw <= 0:
                continue

            spend_quote = pos_value / quote_krw
            qty = spend_quote / max(px, 1e-12)
            if qty <= 1e-12:
                continue
            self._entry_inflight.add(market)
            try:
                try:
                    self.storage.log_event(
                        "INFO",
                        "BUY_ATTEMPT",
                        market,
                        f"pos_value_krw={pos_value:.0f} qty={qty:.8f} px={px:.4f} score={s.score:.0f} cutoff={s.dynamic_cutoff}",
                    )
                except Exception:
                    pass

                res = await self.exec_engine.execute_market(
                    market, "BUY", pos_value, qty, px, gate["slip_est"], "entry_or_add"
                )
            finally:
                self._entry_inflight.discard(market)

            if not res.ok:
                r = str(res.reason or "")

                # 정책적/기술적 스킵은 '오류'로 누적하지 않음 (전략 평가 왜곡 방지)
                is_skip = r in {
                    "dedup_block",
                    "daily_trade_limit",
                    "live_insufficient_krw",
                    "live_confirm_missing",
                    "kill_switch_on",
                }

                # dedup_block은 짧은 쿨다운을 걸어 루프를 끊습니다.
                if r == "dedup_block":
                    try:
                        self.exec_engine.cooldown_until_ms[market] = now_ms() + 60_000
                    except Exception:
                        pass

                if not is_skip:
                    self.risk.stats.order_errors += 1

                try:
                    self.storage.log_event(
                        "WARN", "BUY_FAIL", market, f"reason={res.reason}"
                    )
                except Exception:
                    pass

                if not is_skip:
                    # 주문 오류가 연속되면 신규 진입을 잠시 멈춤(서킷 브레이커)
                    self._record_order_error_and_maybe_pause(res.reason)
                else:
                    try:
                        self.storage.log_event(
                            "WARN", "BUY_SKIP", market, f"reason={res.reason}"
                        )
                    except Exception:
                        pass
                continue

            # 포트폴리오 업데이트 (신규 or 추가)
            filled_qty = float(res.qty)
            if filled_qty <= 1e-12:
                self.risk.stats.order_errors += 1
                try:
                    self.storage.log_event(
                        "WARN", "BUY_FAIL", market, "filled_qty_zero"
                    )
                except Exception:
                    pass
                self._record_order_error_and_maybe_pause("filled_qty_zero")
                continue

            if market not in self.portfolio.positions:
                stop_price = res.fill_price * (1 - stop_pct)
                self.portfolio.add(
                    market,
                    filled_qty,
                    res.fill_price,
                    stop_price,
                    s.score,
                    volatility_regime=s.volatility_regime,
                )
                try:
                    self.storage.log_event(
                        "INFO",
                        "BUY_OK",
                        market,
                        f"fill_px={res.fill_price:.4f} qty={filled_qty:.8f}",
                    )
                except Exception:
                    pass
            else:
                # 추가 매수: 평단가 갱신 및 스탑로스 상향 (Trailing Up)
                old_p = self.portfolio.positions[market]
                new_qty = old_p.qty + filled_qty
                new_avg = (
                    (old_p.qty * old_p.entry_price) + (filled_qty * res.fill_price)
                ) / new_qty

                # 스탑로스는 '새 평단가' 기준이 아니라, '현재가' 기준으로 타이트하게 올림 (수익 보전)
                new_stop = px * (1 - stop_pct)
                # 기존 스탑보다 낮아지면 안 됨 (Trailing Stop 원칙)
                if new_stop < old_p.stop_price:
                    new_stop = old_p.stop_price

                old_p.qty = new_qty
                old_p.entry_price = new_avg
                old_p.stop_price = new_stop
                old_p.adds += 1  # 불타기 횟수 증가
                LOGGER.info(
                    f"Position Added: {market} NewQty={new_qty:.4f} NewAvg={new_avg:.2f} NewStop={new_stop:.2f}"
                )
                try:
                    self.storage.log_event(
                        "INFO",
                        "BUY_OK",
                        market,
                        f"add fill_px={res.fill_price:.4f} qty={filled_qty:.8f} new_avg={new_avg:.4f}",
                    )
                except Exception:
                    pass

            self.risk.stats.total_trades += 1
            n = self.risk.stats.total_trades
            prev = self.risk.stats.avg_entry_slippage
            self.risk.stats.avg_entry_slippage = (
                (prev * (n - 1)) + res.slippage_pct
            ) / n

    async def _process_exits(self, last_prices, orderbooks=None):
        """청산 로직.

        개선(추천값): SELL 주문의 ref_price를 ticker(trade_price) 대신 orderbook bid1로 잡아
        불필요한 '미체결 -> 시장가 던지기'를 줄여 저가 체결 위험을 낮춥니다.
        """
        orderbooks = orderbooks or {}
        for market, p0 in list(self.portfolio.positions.items()):
            # WS 손절과 동시에 청산 루프가 돌면 중복 매도가 날 수 있어 가드
            if market in self._exit_inflight:
                continue

            # 기본은 ticker 기반 현재가
            px_ticker = last_prices.get(market, p0.entry_price)

            # SELL은 bid1 기준으로 지정가 체결 유도 (없으면 ticker fallback)
            ob = orderbooks.get(market) or {}
            units = ob.get("orderbook_units") or []
            bid1 = float(units[0].get("bid_price", 0.0)) if units else 0.0
            px = bid1 if bid1 > 0 else px_ticker

            fee_rate = float(self.cfg["fees"].get(market.split("-")[0], 0.001))
            slip_est = self.exec_engine.estimate_slippage(market, 0.001)
            vol_regime = p0.volatility_regime
            actions = self.portfolio.evaluate_exits(
                market, px, fee_rate, slip_est, volatility_regime=vol_regime
            )
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
                    try:
                        self.storage.log_event(
                            "INFO",
                            "SELL_ATTEMPT",
                            market,
                            f"reason={a['reason']} ratio={a['ratio']:.3f} qty={qty:.8f} px={px:.4f} val_krw={val:.0f}",
                        )
                    except Exception:
                        pass

                    res = await self.exec_engine.execute_market(
                        market, "SELL", val, qty, px, slip_est, a["reason"]
                    )
                finally:
                    self._exit_inflight.discard(market)

                if not res.ok:
                    r = str(res.reason or "")
                    # 손절 더스트(최소주문금액 미만)는 반복 오류 루프를 만들 수 있어
                    # 주문오류 카운트/서킷브레이커 집계에서 제외합니다.
                    is_stoploss_dust = r.startswith("stoploss_under_min_notional")

                    # 더스트 처리 쿨다운/최소주문금액 미만 등은 '오류'라기보다 정책적 스킵이므로
                    # order_errors/서킷브레이커에 반영하지 않습니다.
                    is_dust_skip = r in {
                        "dust_topup_cooldown",
                        "live_under_min_notional",
                    }

                    if not (is_stoploss_dust or is_dust_skip):
                        self.risk.stats.order_errors += 1

                    try:
                        self.storage.log_event(
                            "WARN",
                            "SELL_FAIL",
                            market,
                            f"reason={res.reason} action={a['reason']}",
                        )
                    except Exception:
                        pass

                    if not (is_stoploss_dust or is_dust_skip):
                        self._record_order_error_and_maybe_pause(res.reason)
                    else:
                        try:
                            self.storage.log_event(
                                "WARN",
                                "SELL_SKIP_DUST",
                                market,
                                f"action={a['reason']} reason={res.reason}",
                            )
                        except Exception:
                            pass

                    # 실패했고 전량청산으로 이미 제거했으면 복구
                    # 단, 손절 더스트는 반복 루프 방지를 위해 복구하지 않습니다.
                    if a["ratio"] >= 0.999 and not is_stoploss_dust:
                        self.portfolio.positions[market] = removed
                    continue

                try:
                    self.storage.log_event(
                        "INFO",
                        "SELL_OK",
                        market,
                        f"fill_px={res.fill_price:.4f} qty={float(res.qty):.8f} action={a['reason']}",
                    )
                except Exception:
                    pass

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

                pnl_pct = (
                    (res.fill_price - removed.entry_price) / removed.entry_price
                    if removed.entry_price > 0
                    else 0
                )
                is_win = pnl_pct > 0
                self.risk.update_trade_result(is_win, pnl_pct)

                if a["ratio"] < 0.999:
                    # 부분 청산 반영
                    p2 = self.portfolio.positions.get(market)
                    if p2:
                        p2.qty -= qty

    async def _snapshot_positions(self, last_prices):
        for m, p in self.portfolio.positions.items():
            last = last_prices.get(m, p.entry_price)
            fee_rate = float(self.cfg["fees"].get(m.split("-")[0], 0.001))
            pnl = self.portfolio.net_pnl_pct(
                p, last, fee_rate, self.exec_engine.estimate_slippage(m, 0.001)
            )
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

        worst_market, ok = self.portfolio.replacement_candidates(
            {"score": new_signal.score}, last_prices, self.cfg["fees"]
        )
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
        try:
            self.storage.log_event(
                "INFO",
                "SELL_ATTEMPT",
                worst_market,
                f"reason=replacement_out ratio=1.000 qty={qty:.8f} px={px:.4f} val_krw={val:.0f}",
            )
        except Exception:
            pass

        res = await self.exec_engine.execute_market(
            worst_market, "SELL", val, qty, px, slip_est, "replacement_out"
        )
        if not res.ok:
            try:
                self.storage.log_event(
                    "WARN",
                    "SELL_FAIL",
                    worst_market,
                    f"reason={res.reason} action=replacement_out",
                )
            except Exception:
                pass
            self._record_order_error_and_maybe_pause(res.reason)
            return

        try:
            self.storage.log_event(
                "INFO",
                "SELL_OK",
                worst_market,
                f"fill_px={res.fill_price:.4f} qty={float(res.qty):.8f} action=replacement_out",
            )
        except Exception:
            pass

        self.portfolio.remove(worst_market)
        self.replacement_events.append(now_ms())

    async def _refresh_live_cache_step(self):
        if not self.all_markets:
            return
        now = now_ms()
        if not isinstance(getattr(self, "_last_candle_fetch_ms", None), dict):
            self._last_candle_fetch_ms = {}

        # tickers는 60초에 1회만 갱신(429 방지). KRW/BTC/USDT 모두 일부 포함.
        if now - self._last_ticker_refresh_ms >= 60_000:
            self._last_ticker_refresh_ms = now
            krw = [m for m in self.all_markets if m.startswith("KRW-")][:200]
            btc = [m for m in self.all_markets if m.startswith("BTC-")][:60]
            usdt = [m for m in self.all_markets if m.startswith("USDT-")][:60]
            markets = krw + btc + usdt
            tickers = (
                await self._call_with_retry(
                    lambda: self.rest.get_tickers(markets), name="get_tickers"
                )
                or []
            )
            if not tickers:
                if self.mode == "live":
                    try:
                        self.storage.log_event(
                            "WARN",
                            "TICKER_FETCH_FAIL",
                            None,
                            "skip_mock_tickers_in_live",
                        )
                    except Exception:
                        pass
                    return
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
            orderbooks = (
                await self._call_with_retry(
                    lambda: self.rest.get_orderbook(ob_markets), name="get_orderbook"
                )
                or []
            )
            if not orderbooks:
                if self.mode == "live":
                    try:
                        self.storage.log_event(
                            "WARN",
                            "ORDERBOOK_FETCH_FAIL",
                            None,
                            "skip_mock_orderbook_in_live",
                        )
                    except Exception:
                        pass
                    return
                # tickers가 비어도 mock orderbook 생성은 가능
                orderbooks = self._mock_orderbooks(
                    ob_markets, tickers or self._mock_tickers(ob_markets)
                )
            await self.cache.seed_orderbooks(orderbooks)

        # 실제 캔들 데이터 사용 - top10 마켓에 대해서만 실제 데이터 수집
        # (분봉은 분당 1개만 갱신되므로, market별 REST 호출은 최소 60초 간격으로 제한)
        for m in self.universe_top10[:10]:  # universe_top10이 이미 있음
            last_fetch = self._last_candle_fetch_ms.get(m, 0)
            if now - last_fetch < 60_000:
                continue
            self._last_candle_fetch_ms[m] = now

            # 신호엔진에서 EMA60 등 60개 이상이 필요하므로, 초기부터 충분한 길이로 받아옵니다.
            candles = (
                await self._call_with_retry(
                    lambda: self.rest.get_candles_minutes(m, unit=1, count=120),
                    name=f"get_candles_minutes:{m}",
                )
                or []
            )
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
                        "candle_acc_trade_volume": float(
                            c.get("candle_acc_trade_volume", 0.0)
                        ),
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
                self._last_candle_success_ms[m] = now_ms()
                try:
                    self.storage.log_event(
                        "INFO", "CANDLE_FETCH_OK", m, f"candles={len(candles)}"
                    )
                except Exception:
                    pass
            else:
                if self.mode == "live":
                    try:
                        self.storage.log_event(
                            "WARN",
                            "CANDLE_FETCH_FAIL",
                            m,
                            "skip_mock_candle_in_live",
                        )
                    except Exception:
                        pass
                    continue
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

    async def _call_with_retry(
        self, fn, *, name: str, retries: int = 3, base_delay_s: float = 0.4
    ):
        for attempt in range(1, retries + 1):
            try:
                return await fn()
            except Exception as e:
                if attempt >= retries:
                    LOGGER.warning("call failed after retries: %s err=%s", name, e)
                    return None
                wait = min(5.0, base_delay_s * (2 ** (attempt - 1)))
                LOGGER.warning(
                    "call failed: %s attempt=%d wait=%.2fs err=%s",
                    name,
                    attempt,
                    wait,
                    e,
                )
                await asyncio.sleep(wait)

    def _mock_tickers(self, markets):
        out = []
        for m in markets:
            px = random.uniform(100, 150000)
            out.append(
                {
                    "market": m,
                    "trade_price": px,
                    "acc_trade_price_24h": random.uniform(1e8, 1e11),
                    "acc_trade_volume_24h": random.uniform(100, 1e7),
                }
            )
        return out

    def _mock_orderbooks(self, markets, tickers):
        pmap = {x["market"]: float(x["trade_price"]) for x in tickers}
        out = []
        for m in markets:
            px = pmap.get(m, random.uniform(100, 150000))
            units = []
            for i in range(3):
                spread = 0.0002 + i * 0.0002
                units.append(
                    {
                        "ask_price": px * (1 + spread),
                        "bid_price": px * (1 - spread),
                        "ask_size": random.uniform(3, 80),
                        "bid_size": random.uniform(3, 80),
                    }
                )
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
