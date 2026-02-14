import asyncio
import json
import logging
import uuid
from typing import Callable, Optional

import websockets

LOGGER = logging.getLogger(__name__)

class UpbitWebSocket:
    def __init__(self, markets: list[str], q_size: int = 1000):
        self.uri = "wss://api.upbit.com/websocket/v1"
        self.markets = markets
        self.q = asyncio.Queue(maxsize=q_size)
        self.connected = False
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.callbacks: list[Callable[[dict], None]] = []

    def add_callback(self, cb: Callable[[dict], None]):
        self.callbacks.append(cb)

    async def start(self):
        # 구독 마켓이 없으면 서버가 바로 끊는 경우가 있어, 연결 자체를 시작하지 않습니다.
        if not self.markets:
            LOGGER.warning("WebSocket start skipped: 0 markets")
            self.connected = False
            return
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        LOGGER.info(f"WebSocket started for {len(self.markets)} markets")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        LOGGER.info("WebSocket stopped")

    async def _connect_loop(self):
        while self._running:
            try:
                async with websockets.connect(self.uri, ping_interval=60, ping_timeout=20) as ws:
                    self.connected = True
                    LOGGER.info("WebSocket connected!")
                    
                    # 구독 메시지 전송
                    subscribe_fmt = [
                        {"ticket": str(uuid.uuid4())},
                        {"type": "ticker", "codes": self.markets, "isOnlyRealtime": True},
                        {"type": "orderbook", "codes": self.markets, "isOnlyRealtime": True},
                    ]
                    await ws.send(json.dumps(subscribe_fmt).encode("utf-8"))

                    while self._running:
                        try:
                            msg = await ws.recv()
                            data = json.loads(msg)
                            
                            # 큐에 넣거나 콜백 실행
                            if not self.q.full():
                                self.q.put_nowait(data)
                            
                            for cb in self.callbacks:
                                try:
                                    cb(data)
                                except Exception as e:
                                    LOGGER.error(f"WS Callback error: {e}")

                        except websockets.ConnectionClosed:
                            LOGGER.warning("WebSocket connection closed")
                            break
                        except Exception as e:
                            LOGGER.error(f"WebSocket recv error: {e}")
                            break
            
            except Exception as e:
                LOGGER.error(f"WebSocket connection failed: {e}. Retrying in 5s...")
                self.connected = False
            
            # 연결 종료/실패 후 반드시 대기 (무한 재접속 방지)
            await asyncio.sleep(5)


class MarketCache:
    def __init__(self):
        # {market: ticker_dict}
        self.tickers: dict[str, dict] = {}
        # {market: orderbook_dict}
        self.orderbooks: dict[str, dict] = {}
        # {market: deque[candle_dict]}
        self.candles: dict[str, object] = {} # deque는 async 처리 중 import 해서 사용

    async def seed_tickers(self, tickers: list[dict]):
        for t in tickers:
            self.tickers[t["market"]] = t

    async def seed_orderbooks(self, orderbooks: list[dict]):
        for ob in orderbooks:
            self.orderbooks[ob["market"]] = ob

    async def push_candle(self, market: str, candle: dict):
        from collections import deque
        if market not in self.candles:
            self.candles[market] = deque(maxlen=200)
        
        # 중복 방지 (같은 시간대면 덮어쓰기)
        dq = self.candles[market]
        if dq and dq[-1]["ts_ms"] == candle["ts_ms"]:
            dq[-1] = candle
        else:
            dq.append(candle)

    async def get_candles(self, market: str) -> list[dict]:
        dq = self.candles.get(market)
        if not dq:
            return []
        return list(dq)

    async def snapshot(self):
        return self.tickers.copy(), self.orderbooks.copy()

    async def get_notional_ratio(self, market: str, lookback: int = 20) -> float:
        """최근 N개 캔들 대비 현재 거래대금 비율"""
        candles = await self.get_candles(market)
        if len(candles) < 2:
            return 0.0
        
        current_vol = float(candles[-1].get("notional", 0.0)) # notional은 candle_acc_trade_price
        
        # 이전 N개 평균
        prev_candles = list(candles)[:-1]
        if not prev_candles:
            return 0.0
            
        cnt = min(len(prev_candles), lookback)
        sample = prev_candles[-cnt:]
        
        avg_vol = sum(float(c.get("notional", 0.0)) for c in sample) / cnt
        if avg_vol <= 0:
            return 0.0
            
        return current_vol / avg_vol
