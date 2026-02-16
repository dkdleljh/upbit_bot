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
    tp2_done: bool = False
    adds: int = 0
    restored: bool = False
    restored_ts_ms: int = 0
    trailing_activated: bool = False
    volatility_regime: str = "normal"


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
        volatility_regime: str = "normal",
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
            volatility_regime=volatility_regime,
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
        fee_total = fee_rate * 2
        return gross - fee_total - slip_exit

    def evaluate_exits(self, market: str, last_price: float, fee_rate: float, slip_exit: float, atr_pct: float = 0.0, volatility_regime: str = "normal") -> list[dict]:
        out: list[dict] = []
        p = self.positions.get(market)
        if not p:
            return out

        self.mark_peak(market, last_price)
        hold_sec = (now_ms() - p.entry_ts_ms) // 1000
        pnl = self.net_pnl_pct(p, last_price, fee_rate, slip_exit)

        tp_multiplier = 1.0
        trailing_multiplier = 1.0
        if volatility_regime == 'high':
            tp_multiplier = 1.3
            trailing_multiplier = 1.2
        elif volatility_regime == 'low':
            tp_multiplier = 0.8
            trailing_multiplier = 0.9

        base_tp = self.cfg["stops"]["tp_net_pnl_pct"]
        adjusted_tp = base_tp * tp_multiplier
        
        base_trailing = self.cfg["stops"]["trailing_stop_pct"]
        adjusted_trailing = base_trailing * trailing_multiplier

        breakeven_trigger = 0.012
        if pnl >= breakeven_trigger:
            safe_margin = fee_rate * 2 + 0.001 
            new_stop = p.entry_price * (1 + safe_margin)
            
            if p.stop_price < new_stop:
                p.stop_price = new_stop

        if last_price <= p.stop_price:
            out.append({"type": "STOP", "ratio": 1.0, "reason": "hard_stop"})
            return out

        if pnl >= adjusted_tp and not p.tp1_done:
            out.append({"type": "TP1", "ratio": self.cfg["stops"]["tp1_ratio"], "reason": "tp1"})
            p.tp1_done = True
            p.trailing_activated = True

        tp2_threshold = adjusted_tp * 2
        if p.tp1_done and pnl >= tp2_threshold and not p.tp2_done:
            tp2_ratio = self.cfg["stops"].get("tp2_ratio", 0.3)
            out.append({"type": "TP2", "ratio": tp2_ratio, "reason": "tp2"})
            p.tp2_done = True

        if p.tp1_done:
            dd = (p.peak_price - last_price) / max(1e-9, p.peak_price)
            if dd >= adjusted_trailing:
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
