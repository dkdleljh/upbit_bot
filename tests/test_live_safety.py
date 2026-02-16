import asyncio

from src.execution import ExecutionEngine
from src.storage import Storage


class _FakeRest:
    is_live_ready = True


def _cfg():
    return {
        "live": {"order_dedup_entry_seconds": 10, "order_dedup_stop_seconds": 2, "order_dedup_exit_seconds": 5},
        "gates": {"entry_slippage_cap": 0.003, "exit_slippage_cap": 0.003, "cooldown_minutes": 1},
        "fees": {"KRW": 0.0005},
        "min_notional_krw": 5000,
        "dust": {},
    }


def test_live_orders_blocked_without_confirm(monkeypatch):
    monkeypatch.delenv("UPBIT_LIVE_CONFIRM", raising=False)
    monkeypatch.delenv("UPBIT_KILL_SWITCH", raising=False)
    st = Storage(":memory:")
    eng = ExecutionEngine(_cfg(), st, mode="live", rest_client=_FakeRest())
    res = asyncio.run(eng.execute_market("KRW-BTC", "BUY", 10000, 0.001, 100000000, 0.001, "entry"))
    assert res.ok is False
    assert res.reason == "live_confirm_missing"


def test_live_orders_blocked_by_kill_switch(monkeypatch):
    monkeypatch.setenv("UPBIT_LIVE_CONFIRM", "YES")
    monkeypatch.setenv("UPBIT_KILL_SWITCH", "1")
    st = Storage(":memory:")
    eng = ExecutionEngine(_cfg(), st, mode="live", rest_client=_FakeRest())
    res = asyncio.run(eng.execute_market("KRW-BTC", "BUY", 10000, 0.001, 100000000, 0.001, "entry"))
    assert res.ok is False
    assert res.reason == "kill_switch_on"


def test_daily_trade_limit_applies_to_buy_only(monkeypatch):
    monkeypatch.setenv("UPBIT_LIVE_CONFIRM", "YES")
    monkeypatch.delenv("UPBIT_KILL_SWITCH", raising=False)
    st = Storage(":memory:")
    eng = ExecutionEngine(_cfg(), st, mode="live", rest_client=_FakeRest())
    eng._within_daily_trade_limit = lambda: False

    buy = asyncio.run(eng.execute_market("KRW-BTC", "BUY", 10000, 0.001, 100000000, 0.001, "entry"))
    assert buy.ok is False
    assert buy.reason == "daily_trade_limit"

    sell = asyncio.run(eng.execute_market("KRW-BTC", "SELL", 10000, 0.001, 100000000, 0.001, "stop_loss"))
    assert sell.reason != "daily_trade_limit"
