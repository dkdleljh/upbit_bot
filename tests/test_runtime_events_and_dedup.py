import pytest

from src.storage import Storage
from src.execution import ExecutionEngine


def test_runtime_events_table_and_log_event():
    st = Storage(":memory:")
    st.log_event("INFO", "TEST_EVENT", "KRW-BTC", "hello")
    rows = st.query("select level,event,market,details from runtime_events")
    assert len(rows) == 1
    assert rows[0]["event"] == "TEST_EVENT"


def test_order_dedup_unique_blocks_duplicates():
    st = Storage(":memory:")
    cfg = {"live": {"order_dedup_entry_seconds": 10, "order_dedup_stop_seconds": 2, "order_dedup_exit_seconds": 5}}
    eng = ExecutionEngine(cfg, st, mode="paper", rest_client=None)

    ok1 = eng._try_dedup("KRW-BTC", "BUY", "entry", 0.01, 100.0)
    ok2 = eng._try_dedup("KRW-BTC", "BUY", "entry", 0.01, 100.0)

    assert ok1 is True
    assert ok2 is False
