import argparse
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path

import fcntl

from .backtest_engine_simple import BacktestEngine, BacktestResult
from .backtest_analyzer import BacktestReporter
from .data_manager import DataManager
from .notifier import TelegramNotifier, get_notifier, init_notifier
from .reporter import Reporter
from .risk import RiskManager
from .state_machine import TradingStateMachine, run_backtest_30d
from .storage import Storage
from .upbit_rest import UpbitRestClient
from .upbit_ws import MarketCache
from .utils import env_or_none, load_config, load_env_file, setup_logging

LOGGER = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Upbit Multi-Market Scalping Bot MVP")
    p.add_argument("--mode", choices=["live", "paper", "backtest"], default="paper")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--health", action="store_true", help="헬스체크 실행 후 종료(0/1)")
    return p.parse_args()


async def run_trading(mode: str, cfg: dict, storage: Storage):
    cache = MarketCache()
    access_env = cfg.get("env", {}).get("upbit_access_key", "UPBIT_ACCESS_KEY")
    secret_env = cfg.get("env", {}).get("upbit_secret_key", "UPBIT_SECRET_KEY")
    rest = UpbitRestClient(
        access_key=env_or_none(access_env),
        secret_key=env_or_none(secret_env),
    )
    reporter = Reporter(cfg, storage, "reports")
    
    notifier = get_notifier()
    if notifier.enabled:
        initial_equity = float(cfg["runtime"]["paper_initial_equity_krw"])
        await notifier.notify_startup(mode, initial_equity)

    # live 모드에서는 실제 계좌 KRW 잔고를 기준으로 포지션 사이징
    if mode == "live" and rest.is_live_ready:
        accounts = await rest.get_accounts()
        krw_avail = None
        if isinstance(accounts, list):
            for a in accounts:
                if str(a.get("currency")) == "KRW":
                    try:
                        bal = float(a.get("balance") or 0.0)
                        locked = float(a.get("locked") or 0.0)
                        krw_avail = max(0.0, bal - locked)
                    except Exception:
                        krw_avail = None
                    break
        initial_equity = float(krw_avail) if krw_avail is not None else float(cfg["runtime"]["paper_initial_equity_krw"])
    else:
        initial_equity = float(cfg["runtime"]["paper_initial_equity_krw"])

    risk = RiskManager(cfg, initial_equity)

    sm = TradingStateMachine(cfg, rest, cache, storage, risk, reporter, mode)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop():
        if not stop_event.is_set():
            LOGGER.info("shutdown signal received")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    try:
        await sm.run(stop_event=stop_event)
    finally:
        await rest.aclose()


def run_backtest(cfg: dict, storage: Storage):
    LOGGER.info("Starting historical backtest...")
    
    data_manager = DataManager(storage)
    from .backtest_engine_simple import HistoricalDataLoader
    data_loader = HistoricalDataLoader(storage)
    backtest_engine = BacktestEngine(cfg, storage, data_loader)
    
    backtest_days = cfg["runtime"]["backtest_days"]
    end_date = datetime.now()
    start_date = end_date - timedelta(days=backtest_days)
    
    test_markets = [
        "KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-ADA", "KRW-DOT",
        "KRW-LTC", "KRW-BCH", "KRW-ETC", "KRW-SOL", "KRW-MATIC"
    ]
    
    data_status = asyncio.run(
        data_manager.ensure_data_availability(test_markets, start_date, end_date, fill_gaps=True)
    )
    
    available_markets = [m for m, available in data_status.items() if available]

    if not available_markets:
        # 상태 플래그가 False여도 DB에 실제 캔들이 있으면 백테스트에서 사용 가능
        for market in test_markets:
            row = storage.query_one(
                """
                SELECT COUNT(*) AS n
                FROM candles
                WHERE market = ? AND timestamp BETWEEN ? AND ?
                """,
                (market, start_date.timestamp(), end_date.timestamp()),
            )
            if row and int(row["n"]) >= 200:
                available_markets.append(market)

    if not available_markets:
        LOGGER.warning("No available markets after gap-fill. Attempting fresh download for test markets...")
        try:
            collected = asyncio.run(data_manager.collector.collect_universe_data(test_markets, start_date, end_date))
            available_markets = [m for m, ok in collected.items() if ok]
        except Exception as e:
            LOGGER.warning("Fresh data download attempt failed: %s", e)
    
    if not available_markets:
        LOGGER.error("No historical data available for backtest (markets=%s, range=%s~%s)", test_markets, start_date, end_date)
        m = run_backtest_30d(cfg)
        _store_legacy_backtest_result(cfg, storage, m)
        return
    
    LOGGER.info(f"Running backtest with {len(available_markets)} markets")
    
    try:
        result = asyncio.run(backtest_engine.run_backtest())
        _store_historical_backtest_result(cfg, storage, result)
        LOGGER.info("Historical backtest completed successfully")
        LOGGER.info(f"Results: {result.total_trades} trades, {result.win_rate:.1%} win rate, {result.net_pnl_pct:.1%} net P&L")
        
    except Exception as e:
        LOGGER.error(f"Historical backtest failed: {e}")
        m = run_backtest_30d(cfg)
        _store_legacy_backtest_result(cfg, storage, m)


def _store_historical_backtest_result(cfg: dict, storage: Storage, result: BacktestResult):
    storage.insert(
        "daily_summary",
        {
            "date_kst": f"BACKTEST-{cfg['runtime']['backtest_days']}D",
            "mode": "backtest",
            "equity": cfg["runtime"]["paper_initial_equity_krw"] * (1 + result.net_pnl_pct),
            "realized_pnl": cfg["runtime"]["paper_initial_equity_krw"] * result.net_pnl_pct,
            "realized_pnl_pct": result.net_pnl_pct,
            "trades": result.total_trades,
            "win_rate": result.win_rate,
            "max_drawdown": result.max_drawdown_pct,
            "avg_hold_seconds": result.avg_hold_seconds,
            "avg_slippage": result.avg_slippage_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "profit_factor": result.profit_factor,
            "note": f"Historical backtest with {len(result.per_market_stats)} markets",
        },
    )
    
    reporter = BacktestReporter(storage)
    report = reporter.generate_report(result, f"reports/backtest_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    
    LOGGER.info("=== 백테스트 결과 요약 ===")
    LOGGER.info(f"총 거래: {result.total_trades}회")
    LOGGER.info(f"승률: {result.win_rate:.1%}")
    LOGGER.info(f"순수익률: {result.net_pnl_pct:.2%}")
    LOGGER.info(f"샤프 비율: {result.sharpe_ratio:.2f}")
    LOGGER.info(f"최대 손실: {result.max_drawdown_pct:.2%}")
    LOGGER.info("상세 보고서는 reports/ 폴더를 확인하세요")


def _store_legacy_backtest_result(cfg: dict, storage: Storage, m: dict):
    storage.insert(
        "daily_summary",
        {
            "date_kst": "BACKTEST-30D-SIMULATION",
            "mode": "backtest",
            "equity": cfg["runtime"]["paper_initial_equity_krw"] * (1 + m["net_pnl"]),
            "realized_pnl": cfg["runtime"]["paper_initial_equity_krw"] * m["net_pnl"],
            "realized_pnl_pct": m["net_pnl"],
            "trades": m["total_trades"],
            "win_rate": m["win_rate"],
            "max_drawdown": m["max_drawdown"],
            "avg_hold_seconds": m["avg_hold_time"],
            "avg_slippage": m["avg_slip"],
            "note": f"Legacy simulation: {str(m['per_market'])}",
        },
    )
    LOGGER.info("Legacy simulation backtest completed: %s", m)


def _lock_path() -> str:
    return os.path.join(os.getcwd(), ".upbit_bot.lock")


def _acquire_singleton_lock() -> object:
    """중복 실행 방지(특히 live 모드에서 치명적)."""
    lock_path = _lock_path()
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # 이미 실행 중
        f.seek(0)
        pid = (f.read() or "").strip()
        raise SystemExit(f"업비트 봇이 이미 실행 중입니다. pid={pid}")

    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    os.fsync(f.fileno())
    return f


def _read_lock_pid() -> int | None:
    try:
        with open(_lock_path(), "r", encoding="utf-8") as f:
            raw = (f.read() or "").strip()
        if not raw:
            return None
        return int(raw)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def run_healthcheck(cfg: dict) -> int:
    Path("data").mkdir(exist_ok=True)
    storage = Storage("data/trading_bot.db")
    status = {
        "ok": False,
        "pid": None,
        "pid_alive": False,
        "db_ok": False,
        "last_ws_tick_ms": None,
        "last_candle_fetch_ms": None,
    }
    try:
        status["db_ok"] = True
        pid = _read_lock_pid()
        status["pid"] = pid
        status["pid_alive"] = bool(pid and _pid_alive(pid))

        ws = storage.query_one("SELECT ts_ms FROM runtime_events WHERE event='WS_TICK_OK' ORDER BY ts_ms DESC LIMIT 1")
        cdl = storage.query_one("SELECT ts_ms FROM runtime_events WHERE event='CANDLE_FETCH_OK' ORDER BY ts_ms DESC LIMIT 1")
        status["last_ws_tick_ms"] = int(ws["ts_ms"]) if ws else None
        status["last_candle_fetch_ms"] = int(cdl["ts_ms"]) if cdl else None

        now_ms = int(datetime.now().timestamp() * 1000)
        ws_fresh = bool(status["last_ws_tick_ms"] and (now_ms - status["last_ws_tick_ms"] < 180_000))
        cdl_fresh = bool(status["last_candle_fetch_ms"] and (now_ms - status["last_candle_fetch_ms"] < 300_000))
        status["ok"] = bool(status["db_ok"] and status["pid_alive"] and ws_fresh and cdl_fresh)
    except Exception as e:
        status["error"] = str(e)
    finally:
        storage.close()

    print(json.dumps(status, ensure_ascii=True))
    return 0 if status.get("ok") else 1


def main():
    args = parse_args()
    setup_logging("logs/bot.log")
    cfg = load_config(args.config)

    # .env에서 UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY를 로드하여 live 모드에서도 자동 인식
    load_env_file(".env", override=False)

    if args.health:
        raise SystemExit(run_healthcheck(cfg))

    # 중복 실행 방지 (backtest/health는 제외)
    lock_f = None
    if args.mode != "backtest":
        lock_f = _acquire_singleton_lock()

    Path("data").mkdir(exist_ok=True)
    Path("reports").mkdir(exist_ok=True)
    storage = Storage("data/trading_bot.db")

    try:
        if args.mode == "backtest":
            run_backtest(cfg, storage)
        else:
            if args.mode == "live":
                access_env = cfg.get("env", {}).get("upbit_access_key", "UPBIT_ACCESS_KEY")
                secret_env = cfg.get("env", {}).get("upbit_secret_key", "UPBIT_SECRET_KEY")
                if not env_or_none(access_env) or not env_or_none(secret_env):
                    LOGGER.error("live 모드 실행 시 API 키(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)가 반드시 필요합니다.")
                    LOGGER.error("실제 거래를 원하시면 .env 파일에 API 키를 설정하거나 환경변수를export하세요.")
                    raise SystemExit(1)
                if os.getenv("UPBIT_LIVE_CONFIRM", "").strip().upper() != "YES":
                    LOGGER.error("UPBIT_LIVE_CONFIRM=YES 가 설정되지 않아 live 주문이 차단됩니다.")
                    LOGGER.error("실제 거래를 원하시면 UPBIT_LIVE_CONFIRM=YES 를 설정하세요.")
                    raise SystemExit(1)
            asyncio.run(run_trading(args.mode, cfg, storage))
    except KeyboardInterrupt:
        LOGGER.info("종료 신호 수신")
    finally:
        from .reporter import Reporter

        Reporter(cfg, storage, "reports").export_today()
        storage.close()

        # 락 해제
        if lock_f is not None:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
