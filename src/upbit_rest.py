import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
import urllib.parse
import urllib.request
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

LOGGER = logging.getLogger(__name__)


class UpbitRestClient:
    BASE = "https://api.upbit.com"

    def __init__(self, access_key: str | None = None, secret_key: str | None = None, timeout_seconds: int = 7):
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds) if aiohttp is not None else None
        self.error_count = 0

        # 동시 요청 수를 제한하여 429 에러 방지
        self.sem = asyncio.Semaphore(2)

        # 429(backoff) 상태 관리
        self._backoff_until_monotonic = 0.0
        self._backoff_seconds = 0.5

        # aiohttp 세션 재사용 (요청마다 새 세션을 만들면 연결/레이트리밋에 불리)
        self._sess: aiohttp.ClientSession | None = None if aiohttp is not None else None

    @property
    def is_live_ready(self) -> bool:
        return bool(self.access_key and self.secret_key)

    async def aclose(self):
        if self._sess is not None and not self._sess.closed:
            await self._sess.close()

    async def _get_session(self) -> "aiohttp.ClientSession":
        assert aiohttp is not None
        if self._sess is None or self._sess.closed:
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            self._sess = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self._sess

    def _b64url(self, b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")

    def _build_jwt(self, params: dict[str, Any] | None = None) -> str:
        if not self.is_live_ready:
            raise RuntimeError("UPBIT API 키가 설정되지 않았습니다")

        payload: dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            payload["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"

        header = {"alg": "HS256", "typ": "JWT"}
        h = self._b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        p = self._b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing = f"{h}.{p}".encode("utf-8")
        sig = hmac.new(self.secret_key.encode("utf-8"), signing, hashlib.sha256).digest()
        s = self._b64url(sig)
        return f"{h}.{p}.{s}"

    async def _respect_backoff(self):
        now = time.monotonic()
        if now < self._backoff_until_monotonic:
            await asyncio.sleep(self._backoff_until_monotonic - now)

    def _bump_backoff(self, retry_after_seconds: float | None = None):
        if retry_after_seconds is not None and retry_after_seconds > 0:
            wait = min(float(retry_after_seconds), 15.0)
        else:
            wait = min(self._backoff_seconds, 15.0)
            self._backoff_seconds = min(self._backoff_seconds * 2.0, 8.0)
        self._backoff_until_monotonic = time.monotonic() + wait
        return wait

    def _reset_backoff(self):
        self._backoff_seconds = 0.5

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        # 세마포어로 동시 요청 수 제한 + 백오프(429) 존중
        async with self.sem:
            await self._respect_backoff()
            url = f"{self.BASE}{path}"

            # 요청 간 최소 간격(공용 API 보호)
            await asyncio.sleep(0.35)

            for attempt in range(1, 4):
                try:
                    if aiohttp is not None:
                        data = await self._request_aiohttp(method, url, params, body, auth)
                    else:
                        data = await self._request_urllib(method, url, params, body, auth)
                    self._reset_backoff()
                    return data
                except Exception as e:
                    msg = str(e)
                    is_429 = "HTTP 429" in msg
                    is_4xx = any(x in msg for x in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404"))

                    self.error_count += 1
                    LOGGER.warning("REST 요청 실패 %s %s %s (attempt=%d): %s", method, path, params or body, attempt, e)

                    # 429는 즉시 재시도하지 말고 지수 백오프
                    if is_429:
                        wait = self._bump_backoff()
                        await asyncio.sleep(wait)
                        continue

                    # 4xx(특히 잔고부족 등)는 재시도해도 해결되지 않는 경우가 대부분이므로 즉시 중단
                    # 호출부에서 원인 분기할 수 있도록 에러 메시지를 dict로 반환
                    if is_4xx:
                        return {"__http_error__": msg}

                    # 기타 오류는 짧게 쉬고 재시도
                    await asyncio.sleep(0.8)

            return None

    async def _request_aiohttp(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        auth: bool,
    ) -> Any:
        assert aiohttp is not None
        headers = {"Accept": "application/json"}
        sign_src = body if method in {"POST", "DELETE"} else params
        if auth:
            headers["Authorization"] = f"Bearer {self._build_jwt(sign_src)}"

        sess = await self._get_session()
        async with sess.request(method, url, params=params, json=body, headers=headers) as resp:
            if resp.status == 429:
                # Upbit는 Remaining-Req 헤더를 주는 경우가 있어 참고할 수 있음
                retry_after = resp.headers.get("Retry-After")
                ra_s = float(retry_after) if retry_after and retry_after.isdigit() else None
                text = await resp.text()
                wait = self._bump_backoff(ra_s)
                raise RuntimeError(f"HTTP 429: {text} (backoff {wait:.2f}s)")

            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text}")
            return await resp.json()

    async def _request_urllib(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        auth: bool,
    ) -> Any:
        def _fetch() -> Any:
            headers = {"Accept": "application/json"}
            data = None
            if method == "GET" and params:
                qs = urllib.parse.urlencode(params, doseq=True)
                target = f"{url}?{qs}"
            else:
                target = url

            if method in {"POST", "DELETE"} and body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"

            sign_src = body if method in {"POST", "DELETE"} else params
            if auth:
                headers["Authorization"] = f"Bearer {self._build_jwt(sign_src)}"

            req = urllib.request.Request(target, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                # urllib은 429를 예외로 던지므로 여기서는 정상 케이스만
                return json.loads(resp.read().decode("utf-8"))

        return await asyncio.to_thread(_fetch)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params, auth=False)

    async def get_markets(self) -> list[str]:
        data = await self._get("/v1/market/all", params={"isDetails": "false"})
        if not data:
            return []
        return [x["market"] for x in data if x["market"].split("-")[0] in {"KRW", "BTC", "USDT"}]

    async def get_tickers(self, markets: list[str]) -> list[dict]:
        if not markets:
            return []
        chunks = [markets[i : i + 100] for i in range(0, len(markets), 100)]
        out: list[dict] = []
        for chunk in chunks:
            data = await self._get("/v1/ticker", params={"markets": ",".join(chunk)})
            if data:
                out.extend(data)
        return out

    async def get_orderbook(self, markets: list[str]) -> list[dict]:
        if not markets:
            return []
        data = await self._get("/v1/orderbook", params={"markets": ",".join(markets[:30])})
        return data or []

    async def get_candles_minutes(self, market: str, unit: int = 1, count: int = 200, to: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"market": market, "count": count}
        if to:
            params["to"] = to
        data = await self._get(
            f"/v1/candles/minutes/{unit}",
            params=params,
        )
        return data or []

    async def get_candles_days(self, market: str, count: int = 30) -> list[dict]:
        data = await self._get("/v1/candles/days", params={"market": market, "count": count})
        return data or []

    async def get_accounts(self) -> list[dict]:
        data = await self._request("GET", "/v1/accounts", auth=True)
        return data or []

    async def place_limit_buy(self, market: str, volume: float, price: float) -> dict | None:
        """지정가 매수 주문"""
        body = {
            "market": market,
            "side": "bid",
            "ord_type": "limit",
            "volume": str(volume),
            "price": str(price),
        }
        data = await self._request("POST", "/v1/orders", body=body, auth=True)
        return data if isinstance(data, dict) else None

    async def place_limit_sell(self, market: str, volume: float, price: float) -> dict | None:
        """지정가 매도 주문"""
        body = {
            "market": market,
            "side": "ask",
            "ord_type": "limit",
            "volume": str(volume),
            "price": str(price),
        }
        data = await self._request("POST", "/v1/orders", body=body, auth=True)
        return data if isinstance(data, dict) else None

    async def place_market_buy(self, market: str, price_krw: float) -> dict | None:
        """KRW 마켓 시장가 매수(price 주문).

        Upbit API 특성상 KRW 마켓은 ord_type=price, price(원화)로 시장가 매수를 합니다.
        """
        body = {
            "market": market,
            "side": "bid",
            "ord_type": "price",
            "price": str(int(price_krw)),
        }
        data = await self._request("POST", "/v1/orders", body=body, auth=True)
        return data if isinstance(data, dict) else None

    async def place_market_buy_volume(self, market: str, volume: float) -> dict | None:
        """BTC/USDT 마켓 시장가 매수(market 주문).

        KRW 이외(예: BTC-ETH, USDT-BTC)는 ord_type=market + volume(매수할 base 수량)으로 시장가 매수를 합니다.
        """
        body = {
            "market": market,
            "side": "bid",
            "ord_type": "market",
            "volume": f"{volume:.12f}",
        }
        data = await self._request("POST", "/v1/orders", body=body, auth=True)
        return data if isinstance(data, dict) else None

    async def place_market_sell(self, market: str, volume: float) -> dict | None:
        body = {
            "market": market,
            "side": "ask",
            "ord_type": "market",
            "volume": f"{volume:.12f}",
        }
        data = await self._request("POST", "/v1/orders", body=body, auth=True)
        return data if isinstance(data, dict) else None

    async def get_order(self, order_uuid: str) -> dict | None:
        data = await self._request("GET", "/v1/order", params={"uuid": order_uuid}, auth=True)
        return data if isinstance(data, dict) else None

    async def cancel_order(self, order_uuid: str) -> dict | None:
        data = await self._request("DELETE", "/v1/order", body={"uuid": order_uuid}, auth=True)
        return data if isinstance(data, dict) else None
