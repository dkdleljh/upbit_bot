import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.error
from .market_policy import MarketPolicy
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


class UpbitAPIError(RuntimeError):
    def __init__(self, status, payload=None, retry_after=None):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}
        self.name = (self.payload.get("error") or {}).get("name", "unknown_error")
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {self.name}")


class UpbitRestClient:
    BASE = "https://api.upbit.com"

    def __init__(self, access_key: str | None = None, secret_key: str | None = None, timeout_seconds: int = 7):
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds) if aiohttp is not None else None
        self.error_count = 0
        self.last_error_at = 0.0
        self.policy = MarketPolicy()
        self.storage = None
        self.entry_guard = None
        self._submit_lock = asyncio.Lock()

        # 동시 요청 수를 제한하여 429 에러 방지
        self.sem = asyncio.Semaphore(2)

        # 429(backoff) 상태 관리
        self._backoff_until_monotonic = 0.0
        self._backoff_seconds = 0.5

        # aiohttp 세션 재사용 (요청마다 새 세션을 만들면 연결/레이트리밋에 불리)
        self._sess: aiohttp.ClientSession | None = None if aiohttp is not None else None

    def configure_safety(self, cfg, storage):
        self.policy = MarketPolicy(cfg)
        self.storage = storage

    def recover_error_count(self, stable_seconds):
        # A single unrelated successful GET must not erase repeated order failures.
        if self.last_error_at and time.time() - self.last_error_at >= stable_seconds:
            if self.policy.catalog_ok and self.policy.catalog_at is not None and (
                0 <= self.policy.clock() - self.policy.catalog_at <= self.policy.catalog_ttl
            ):
                self.error_count = 0

    def order_block_reason(self, market, side):
        reason = self.policy.reason(market, side, require_data=True)
        if reason:
            return reason
        if self.storage is None:
            return "order_journal_unavailable"
        if self.storage.pending_orders(market):
            return "unresolved_order"
        if side in ("bid", "BUY") and self.entry_guard and not self.entry_guard():
            return "entries_paused"
        return None

    async def _submit_order(self, body):
        async with self._submit_lock:
            if os.getenv("UPBIT_LIVE_CONFIRM", "").strip().upper() != "YES":
                return {"error": {"name": "live_confirm_missing"}}
            if os.getenv("UPBIT_KILL_SWITCH", "0").strip() == "1":
                return {"error": {"name": "kill_switch_on"}}
            reason = self.order_block_reason(body["market"], body["side"])
            if reason:
                return {"error": {"name": reason}}
            body = dict(body, identifier="ub-" + uuid.uuid4().hex)
            self.storage.prepare_order(body)  # durable intent BEFORE network I/O
            try:
                data = await self._request("POST", "/v1/orders", body=body, auth=True)
            except Exception as exc:
                definitive = (isinstance(exc, UpbitAPIError) and
                              exc.status in {400, 401, 403, 404, 422} and
                              "identifier" not in exc.name)
                data = {"error": {"name": getattr(exc, "name", type(exc).__name__)},
                        "http_status": getattr(exc, "status", None)}
                self.storage.update_order(body["identifier"], data,
                                          "rejected" if definitive else "unknown", definitive)
                self.storage.log_event("ERROR", "ORDER_REJECTED" if definitive else "ORDER_UNKNOWN",
                                       body["market"], body["identifier"])
                return data
            if not isinstance(data, dict) or not data.get("uuid"):
                data = {"error": {"name": "invalid_order_response"}}
                self.storage.update_order(body["identifier"], data, "unknown")
                return data
            self.storage.update_order(body["identifier"], data)
            return data

    async def reconcile_pending_orders(self):
        """Read only. Never resend, cancel, or silently book a recovered fill."""
        if self.storage is None:
            return
        for row in self.storage.pending_orders():
            try:
                detail = await self.get_order(row["uuid"], identifier=row["identifier"])
                if detail.get("state") in {"done", "cancel"}:
                    if float(detail.get("executed_volume", "nan")) == 0:
                        self.storage.acknowledge_order(detail["uuid"])
                    else:
                        self.storage.log_event("WARN", "ORDER_RECONCILIATION_REQUIRED", row["market"],
                                               row["identifier"])
            except Exception:
                # Includes not-found: absence on one GET does not prove POST was not accepted.
                self.storage.log_event("WARN", "ORDER_STILL_UNKNOWN", row["market"], row["identifier"])

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
            wait = float(retry_after_seconds)
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
        async with self.sem:
            url = f"{self.BASE}{path}"
            attempts = 3 if method == "GET" else 1
            for attempt in range(attempts):
                await self._respect_backoff()
                await asyncio.sleep(0.35)
                # Recheck at the actual submission boundary after rate-limit waits.
                if method == "POST" and path == "/v1/orders":
                    reason = self.policy.reason(body["market"], body["side"], require_data=True)
                    if (not reason and body["side"] == "bid" and self.entry_guard
                            and not self.entry_guard()):
                        reason = "entries_paused"
                    if os.getenv("UPBIT_KILL_SWITCH", "0").strip() == "1":
                        reason = "kill_switch_on"
                    if os.getenv("UPBIT_LIVE_CONFIRM", "").strip().upper() != "YES":
                        reason = "live_confirm_missing"
                    if reason:
                        raise UpbitAPIError(400, {"error": {"name": reason}})
                try:
                    if aiohttp is not None:
                        data = await self._request_aiohttp(method, url, params, body, auth)
                    else:
                        data = await self._request_urllib(method, url, params, body, auth)
                    self._reset_backoff()
                    return data
                except Exception as exc:
                    self.error_count += 1
                    self.last_error_at = time.time()
                    status = getattr(exc, "status", None)
                    LOGGER.warning("REST failure %s %s attempt=%s status=%s", method, path, attempt + 1, status)
                    if status in {418, 429}:
                        self._bump_backoff(getattr(exc, "retry_after", None))
                    permanent = status is not None and 400 <= status < 500 and status != 429
                    if attempt + 1 == attempts or permanent:
                        raise
                    if status != 429:
                        await asyncio.sleep(0.8)

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
        sign_src = body if method == "POST" else params
        if auth:
            headers["Authorization"] = f"Bearer {self._build_jwt(sign_src)}"

        sess = await self._get_session()
        async with sess.request(method, url, params=params, json=body, headers=headers) as resp:
            if resp.status >= 400:
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = {}
                try:
                    retry_after = float(resp.headers.get("Retry-After", "0")) or None
                except ValueError:
                    retry_after = None
                raise UpbitAPIError(resp.status, payload, retry_after)
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
            if method in {"GET", "DELETE"} and params:
                qs = urllib.parse.urlencode(params, doseq=True)
                target = f"{url}?{qs}"
            else:
                target = url

            if method == "POST" and body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"

            sign_src = body if method == "POST" else params
            if auth:
                headers["Authorization"] = f"Bearer {self._build_jwt(sign_src)}"

            req = urllib.request.Request(target, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    payload = {}
                try:
                    retry_after = float(exc.headers.get("Retry-After", "0")) or None
                except (ValueError, AttributeError):
                    retry_after = None
                raise UpbitAPIError(exc.code, payload, retry_after) from exc

        return await asyncio.to_thread(_fetch)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params, auth=False)

    @staticmethod
    def _rows(data, required):
        if not isinstance(data, list) or any(not isinstance(r, dict) or
                any(k not in r for k in required) for r in data):
            raise ValueError("invalid API list response")
        return data

    async def get_markets(self) -> list[str]:
        self.policy.catalog_ok = False
        rows = self._rows(await self._get("/v1/market/all", {"is_details": "true"}), ["market"])
        rows = [r for r in rows if r["market"].split("-")[0] in {"KRW", "BTC", "USDT"}]
        if not rows:
            raise ValueError("empty market catalog")
        self.policy.update_catalog(rows)
        return list(self.policy.markets)

    async def get_tickers(self, markets: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(markets), 100):
            chunk = markets[i:i + 100]
            rows = self._rows(await self._get("/v1/ticker", {"markets": ",".join(chunk)}),
                              ["market", "trade_price", "timestamp"])
            for row in rows:
                self.policy.record_data(row["market"], "ticker", row)
            out.extend(rows)
        return out

    async def get_orderbook(self, markets: list[str]) -> list[dict]:
        if not markets:
            return []
        rows = self._rows(await self._get("/v1/orderbook", {"markets": ",".join(markets[:30])}),
                          ["market", "orderbook_units", "timestamp"])
        for row in rows:
            self.policy.record_data(row["market"], "orderbook", row)
        return rows

    async def get_candles_minutes(self, market: str, unit: int = 1, count: int = 200, to: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"market": market, "count": count}
        if to:
            params["to"] = to
        data = await self._get(
            f"/v1/candles/minutes/{unit}",
            params=params,
        )
        return self._rows(data, ["trade_price"])

    async def get_candles_days(self, market: str, count: int = 30) -> list[dict]:
        data = await self._get("/v1/candles/days", params={"market": market, "count": count})
        return self._rows(data, ["trade_price"])

    async def get_accounts(self) -> list[dict]:
        data = await self._request("GET", "/v1/accounts", auth=True)
        return self._rows(data, ["currency", "balance", "locked"])

    async def place_limit_buy(self, market: str, volume: float, price: float) -> dict | None:
        """지정가 매수 주문"""
        body = {
            "market": market,
            "side": "bid",
            "ord_type": "limit",
            "volume": str(volume),
            "price": str(price),
        }
        data = await self._submit_order(body)
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
        data = await self._submit_order(body)
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
        data = await self._submit_order(body)
        return data if isinstance(data, dict) else None

    async def place_market_buy_amount(self, market: str, quote_amount: float) -> dict | None:
        # All quotes use bid + price; price is the amount of KRW/BTC/USDT to spend.
        return await self._submit_order({"market": market, "side": "bid", "ord_type": "price",
                                         "price": format(quote_amount, ".12f")})

    async def place_market_buy_volume(self, market: str, volume: float) -> dict | None:
        raise ValueError("Use place_market_buy_amount with quote-currency budget")

    async def place_market_sell(self, market: str, volume: float) -> dict | None:
        body = {
            "market": market,
            "side": "ask",
            "ord_type": "market",
            "volume": f"{volume:.12f}",
        }
        data = await self._submit_order(body)
        return data if isinstance(data, dict) else None

    async def get_order(self, order_uuid: str | None = None, *, identifier=None) -> dict:
        params = {"uuid": order_uuid} if order_uuid else {"identifier": identifier}
        if not order_uuid and not identifier:
            raise ValueError("uuid or identifier required")
        data = await self._request("GET", "/v1/order", params=params, auth=True)
        if not isinstance(data, dict) or not data.get("uuid") or "state" not in data:
            raise ValueError("invalid order response")
        if self.storage:
            rows = self.storage.query("SELECT identifier FROM order_intents WHERE uuid=? OR identifier=?",
                                      (data["uuid"], identifier or data.get("identifier")))
            for row in rows:
                self.storage.update_order(row["identifier"], data)
        return data

    async def cancel_order(self, order_uuid: str) -> dict | None:
        # An ambiguous cancellation is never automatically retried.
        data = await self._request("DELETE", "/v1/order", params={"uuid": order_uuid}, auth=True)
        return data if isinstance(data, dict) else None
