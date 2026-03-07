import json
import time

from src import main
from src.storage import Storage


def test_run_healthcheck_returns_zero_when_runtime_is_fresh(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "_read_lock_pid", lambda: 12345)
    monkeypatch.setattr(main, "_pid_alive", lambda _pid: True)

    st = Storage("data/trading_bot.db")
    now_ms = int(time.time() * 1000)
    st.insert(
        "runtime_events",
        {
            "ts_ms": now_ms,
            "level": "INFO",
            "event": "WS_TICK_OK",
            "market": None,
            "details": None,
        },
    )
    st.insert(
        "runtime_events",
        {
            "ts_ms": now_ms,
            "level": "INFO",
            "event": "CANDLE_FETCH_OK",
            "market": None,
            "details": None,
        },
    )
    st.close()

    rc = main.run_healthcheck({})
    out = capsys.readouterr().out.strip().splitlines()
    status = json.loads(out[-1])

    assert rc == 0
    assert status["ok"] is True


def test_run_healthcheck_returns_one_when_runtime_is_not_healthy(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "_read_lock_pid", lambda: None)

    rc = main.run_healthcheck({})
    out = capsys.readouterr().out.strip().splitlines()
    status = json.loads(out[-1])

    assert rc == 1
    assert status["ok"] is False
