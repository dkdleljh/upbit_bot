import argparse
import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

import fcntl

from .backtest_engine_simple import BacktestEngine, BacktestResult
from .backtest_analyzer import BacktestReporter
from .data_manager import DataManager
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
    await sm.run()


def run_backtest(cfg: dict, storage: Storage):
    LOGGER.info("Starting historical backtest...")
    
    data_manager = DataManager(storage)
    from .backtest_engine import HistoricalDataLoader
    data_loader = HistoricalDataLoader(storage)
    backtest_engine = BacktestEngine(cfg, storage, data_loader)
    
    backtest_days = cfg["runtime"]["backtest_days"]
    end_date = datetime.now()
    start_date = end_date - timedelta(days=backtest_days)
    
    test_markets = [
        "KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-ADA", "KRW-DOT",
        "KRW-LTC", "KRW-BCH", "KRW-ETC", "KRW-SOL", "KRW-MATIC"
    ]
    
    data_status = asyncio.run(data_manager.ensure_data_availability(
        test_markets, start_date, end_date, fill_gaps=False
    ))
    
    available_markets = [m for m, available in data_status.items() if available]
    
    if not available_markets:
        LOGGER.error("No historical data available for backtest")
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


def _acquire_singleton_lock() -> object:
    """중복 실행 방지(특히 live 모드에서 치명적)."""
    lock_path = os.path.join(os.getcwd(), ".upbit_bot.lock")
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
    return f


def main():
    args = parse_args()
    cfg = load_config(args.config)

    # 중복 실행 방지 (backtest는 제외)
    lock_f = None
    if args.mode != "backtest":
        lock_f = _acquire_singleton_lock()

    # .env에서 UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY를 로드하여 live 모드에서도 자동 인식
    if os.path.exists(".env"):
        LOGGER.info("Loading env file: .env")
    else:
        LOGGER.info("Env file not found: .env")
    if os.path.exists(os.path.expanduser("~/.upbit_bot.env")):
        LOGGER.info("(hint) Also supported: ~/.upbit_bot.env (loaded by scripts/run_live.sh)")

    load_env_file(".env", override=False)

    setup_logging("logs/bot.log")

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
                    LOGGER.warning("live 모드이지만 API 키가 없어 모의체결로 동작합니다. env: %s, %s", access_env, secret_env)
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
