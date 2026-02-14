from dataclasses import dataclass

from .utils import now_ms, parse_market


@dataclass
class Position:
    market: str
    base_coin: str
    qty: float
    entry_price: float
    stop_price: float
    peak_price: float
    entry_ts_ms: int
    score: float
    tp1_done: bool = False
    adds: int = 0  # 피라미딩(불타기) 횟수
    # 봇 외부(수동/기존보유)에서 유입된 포지션인지 표시
    restored: bool = False
    restored_ts_ms: int = 0


class Portfolio:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.positions: dict[str, Position] = {}

    def count(self) -> int:
        return len(self.positions)

    def total_exposure_ratio(self, equity: float, last_prices: dict[str, float]) -> float:
        if equity <= 0:
            return 0.0
        value_krw = 0.0
        for p in self.positions.values():
            px = last_prices.get(p.market, p.entry_price)
            quote = parse_market(p.market).quote
            quote_krw = 1.0
            if quote == "BTC":
                quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
            elif quote == "USDT":
                quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
            if quote_krw <= 0:
                quote_krw = 1.0
            value_krw += p.qty * px * quote_krw
        return value_krw / equity

    def coin_exposure_ratio(self, base_coin: str, equity: float, last_prices: dict[str, float]) -> float:
        if equity <= 0:
            return 0.0
        value_krw = 0.0
        for p in self.positions.values():
            if p.base_coin == base_coin:
                px = last_prices.get(p.market, p.entry_price)
                quote = parse_market(p.market).quote
                quote_krw = 1.0
                if quote == "BTC":
                    quote_krw = float(last_prices.get("KRW-BTC", 0.0) or 0.0)
                elif quote == "USDT":
                    quote_krw = float(last_prices.get("KRW-USDT", 0.0) or 0.0)
                if quote_krw <= 0:
                    quote_krw = 1.0
                value_krw += p.qty * px * quote_krw
        return value_krw / equity

    def add(
        self,
        market: str,
        qty: float,
        entry_price: float,
        stop_price: float,
        score: float,
        *,
        entry_ts_ms: int | None = None,
        restored: bool = False,
    ) -> None:
        base = parse_market(market).base
        ts = entry_ts_ms if entry_ts_ms is not None else now_ms()
        self.positions[market] = Position(
            market=market,
            base_coin=base,
            qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            peak_price=entry_price,
            entry_ts_ms=ts,
            score=score,
            adds=0,
            restored=restored,
            restored_ts_ms=ts if restored else 0,
        )

    def remove(self, market: str) -> Position | None:
        return self.positions.pop(market, None)

    def mark_peak(self, market: str, price: float) -> None:
        p = self.positions.get(market)
        if not p:
            return
        p.peak_price = max(p.peak_price, price)

    def net_pnl_pct(self, p: Position, last_price: float, fee_rate: float, slip_exit: float) -> float:
        gross = (last_price - p.entry_price) / p.entry_price
        # 왕복 수수료(진입+청산) 반영
        fee_total = fee_rate * 2
        # 슬리피지 추정치 반영
        return gross - fee_total - slip_exit

    def evaluate_exits(self, market: str, last_price: float, fee_rate: float, slip_exit: float) -> list[dict]:
        out: list[dict] = []
        p = self.positions.get(market)
        if not p:
            return out

        self.mark_peak(market, last_price)
        hold_sec = (now_ms() - p.entry_ts_ms) // 1000
        pnl = self.net_pnl_pct(p, last_price, fee_rate, slip_exit)

        # Breakeven Stop: 순수익률(수수료 차감 후) 1.2% 도달 시 손절가를 본전(진입가 + 왕복수수료)으로 상향
        # 즉, 떨어져도 수수료는 건지고 나오겠다는 전략
        breakeven_trigger = 0.012
        if pnl >= breakeven_trigger:
            # 최소한 수수료(0.1%)와 슬리피지(0.1%)를 커버할 수 있는 가격을 새로운 스탑으로 설정
            safe_margin = fee_rate * 2 + 0.001 
            new_stop = p.entry_price * (1 + safe_margin)
            
            if p.stop_price < new_stop:
                p.stop_price = new_stop

        # 피라미딩한 포지션은 스탑로스가 진입가보다 높을 수 있음 -> 가격이 스탑 밑으로 가면 무조건 청산
        if last_price <= p.stop_price:
            out.append({"type": "STOP", "ratio": 1.0, "reason": "hard_stop"})
            return out

        if pnl >= self.cfg["stops"]["tp_net_pnl_pct"] and not p.tp1_done:
            out.append({"type": "TP1", "ratio": self.cfg["stops"]["tp1_ratio"], "reason": "tp1"})
            p.tp1_done = True

        if p.tp1_done:
            dd = (p.peak_price - last_price) / max(1e-9, p.peak_price)
            if dd >= self.cfg["stops"]["trailing_stop_pct"]:
                out.append({"type": "TRAIL", "ratio": 1.0, "reason": "trailing"})
                return out

        if hold_sec >= self.cfg["time_rules"]["hard_exit_minutes"] * 60:
            out.append({"type": "TIME_HARD", "ratio": 1.0, "reason": "hard_exit"})
            return out

        if hold_sec >= self.cfg["time_rules"]["soft_cut_minutes"] * 60 and pnl < self.cfg["time_rules"]["soft_cut_progress"]:
            out.append({"type": "TIME_SOFT", "ratio": 1.0, "reason": "soft_cut"})
            return out

        return out

    def replacement_candidates(self, new_signal: dict, last_prices: dict[str, float], fee_rate_map: dict[str, float]) -> tuple[str | None, bool]:
        if not self.positions:
            return None, False

        def pos_score(market: str, p: Position) -> float:
            px = last_prices.get(market, p.entry_price)
            fee = fee_rate_map.get(market.split("-")[0], 0.001)
            pnl = self.net_pnl_pct(p, px, fee, 0.001)
            return 0.7 * p.score + 0.3 * (pnl * 100)

        worst_market = None
        worst_val = 10**9
        for m, p in self.positions.items():
            val = pos_score(m, p)
            if val < worst_val:
                worst_val = val
                worst_market = m

        if not worst_market:
            return None, False

        gap = new_signal["score"] - worst_val
        return worst_market, gap >= self.cfg["replace"]["score_gap_min"]
