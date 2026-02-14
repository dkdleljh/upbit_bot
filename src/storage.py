import csv
import logging
import sqlite3
from pathlib import Path
from threading import Lock

LOGGER = logging.getLogger(__name__)


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._create_tables()

    def _create_tables(self) -> None:
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                timestamp REAL NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                value REAL NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_candles_market_ts
            ON candles(market, timestamp)
            """,
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                market TEXT NOT NULL,
                base_coin TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                last_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                peak_price REAL NOT NULL,
                tp1_done INTEGER NOT NULL,
                net_pnl_pct REAL NOT NULL,
                hold_seconds INTEGER NOT NULL,
                score REAL NOT NULL,
                mode TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                mode TEXT NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                order_value_krw REAL NOT NULL,
                request_price REAL NOT NULL,
                fill_price REAL NOT NULL,
                fee REAL NOT NULL,
                slippage_pct REAL NOT NULL,
                reason TEXT,
                status TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                market TEXT NOT NULL,
                score REAL NOT NULL,
                breakout INTEGER NOT NULL,
                breakout_pct REAL NOT NULL,
                notional_ratio REAL NOT NULL,
                spread_pct REAL NOT NULL,
                depth_ratio REAL NOT NULL,
                momentum_3m REAL NOT NULL,
                btc_regime_ok INTEGER NOT NULL,
                tradable INTEGER NOT NULL,
                note TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS market_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                spread_pct REAL NOT NULL,
                depth_ratio REAL NOT NULL,
                slip_est REAL NOT NULL,
                pass INTEGER NOT NULL,
                reason TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_kst TEXT NOT NULL,
                mode TEXT NOT NULL,
                equity REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                realized_pnl_pct REAL NOT NULL,
                trades INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                max_drawdown REAL NOT NULL,
                avg_hold_seconds REAL NOT NULL,
                avg_slippage REAL NOT NULL,
                sharpe_ratio REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                note TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                mode TEXT NOT NULL,
                equity_krw REAL NOT NULL,
                krw_balance REAL NOT NULL,
                krw_locked REAL NOT NULL,
                asset_value_krw REAL NOT NULL,
                cost_basis_krw REAL NOT NULL,
                unrealized_pnl_krw REAL NOT NULL,
                note TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_equity_snapshots_ts
            ON equity_snapshots(ts_ms)
            """,
        ]
        with self.lock, self.conn:
            for q in ddl:
                self.conn.execute(q)

            # lightweight migration for older DBs
            try:
                self.conn.execute("ALTER TABLE daily_summary ADD COLUMN sharpe_ratio REAL DEFAULT 0")
            except Exception:
                pass
            try:
                self.conn.execute("ALTER TABLE daily_summary ADD COLUMN profit_factor REAL DEFAULT 0")
            except Exception:
                pass

            # equity_snapshots는 신규 테이블이므로 별도 ALTER은 불필요(테이블 생성만)

    def insert(self, table: str, data: dict) -> None:
        cols = ",".join(data.keys())
        placeholders = ",".join(["?"] * len(data))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self.lock, self.conn:
            self.conn.execute(sql, tuple(data.values()))

    def upsert_candle(
        self,
        market: str,
        timestamp: float,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        value: float,
    ) -> None:
        """candles 테이블은 (market, timestamp) unique 입니다."""
        sql = (
            "INSERT OR REPLACE INTO candles(market,timestamp,open,high,low,close,volume,value) "
            "VALUES(?,?,?,?,?,?,?,?)"
        )
        with self.lock, self.conn:
            self.conn.execute(sql, (market, timestamp, open_, high, low, close, volume, value))

    def fetch_all(self, table: str, where: str = "", params: tuple = ()) -> list[sqlite3.Row]:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchall()

    def export_table_csv(self, table: str, out_path: str, where: str = "", params: tuple = ()) -> None:
        rows = self.fetch_all(table, where, params)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            if not rows:
                f.write("")
                return
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        LOGGER.info("CSV 내보내기 완료: %s", out_path)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchall()
    
    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchone()
    
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.lock, self.conn:
            self.conn.execute(sql, params)
    
    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        with self.lock, self.conn:
            self.conn.executemany(sql, params_list)
    
    def close(self) -> None:
        with self.lock:
            self.conn.close()
