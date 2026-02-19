from dataclasses import dataclass, field
from typing import List
from .indicators import adaptive_atr_multiplier


@dataclass
class TradeStats:
    total_trades: int = 0
    order_errors: int = 0
    avg_entry_slippage: float = 0.0
    wins: int = 0
    losses: int = 0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    recent_outcomes: List[bool] = field(default_factory=list)
    consecutive_losses: int = 0
    entry_attempts: int = 0

    def __repr__(self):
        return f"TradeStats(wins={self.wins}, losses={self.losses}, win_rate={self.wins / max(1, self.wins + self.losses):.2%})"


class RiskManager:
    DRAWDOWN_BREAKER_PCT = -0.15
    KELLY_HISTORY_SIZE = 20

    def __init__(self, cfg: dict, initial_equity: float):
        self.cfg = cfg
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.realized_pnl = 0.0
        self.daily_realized_pct = 0.0
        self.stats = TradeStats()

        self.peak_equity = initial_equity
        self.max_drawdown = 0.0
        self.current_drawdown = 0.0

        self.kelly_fraction = self.cfg["risk"].get("risk_per_trade_target", 0.001)

        pos_cfg = cfg.get("position_sizing", {})
        self.kelly_fraction_override = pos_cfg.get("kelly_fraction", 0.25)
        self.loss_streak_threshold = pos_cfg.get("loss_streak_threshold", 3)
        self.loss_streak_reduction = pos_cfg.get("loss_streak_reduction", 0.5)
        self.min_trades_for_full_kelly = pos_cfg.get("min_trades_for_full_kelly", 50)

        mtm_cfg = cfg.get("mtm_risk", {})
        self.use_mtm_drawdown = mtm_cfg.get("use_mtm_drawdown", True)
        self.mtm_drawdown_pause_pct = mtm_cfg.get("mtm_drawdown_pause_pct", -0.08)
        self.mtm_drawdown_stop_pct = mtm_cfg.get("mtm_drawdown_stop_pct", -0.12)
        self.unrealized_pnl = 0.0
        self.peak_equity_mtm = initial_equity

    def update_unrealized(self, unrealized_pnl: float) -> None:
        self.unrealized_pnl = unrealized_pnl
        if self.use_mtm_drawdown:
            mtm_equity = self.equity + unrealized_pnl
            if mtm_equity > self.peak_equity_mtm:
                self.peak_equity_mtm = mtm_equity

    def get_mtm_drawdown(self) -> float:
        if not self.use_mtm_drawdown:
            return 0.0
        mtm_equity = self.equity + self.unrealized_pnl
        if self.peak_equity_mtm <= 0:
            return 0.0
        return (mtm_equity - self.peak_equity_mtm) / self.peak_equity_mtm

    def is_mtm_pause_triggered(self) -> bool:
        if not self.use_mtm_drawdown:
            return False
        return self.get_mtm_drawdown() <= self.mtm_drawdown_pause_pct

    def is_mtm_stop_triggered(self) -> bool:
        if not self.use_mtm_drawdown:
            return False
        return self.get_mtm_drawdown() <= self.mtm_drawdown_stop_pct

    def _update_drawdown(self) -> None:
        """현재 드로다운 및 최대 드로다운 업데이트"""
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        self.current_drawdown = (self.equity - self.peak_equity) / max(
            self.peak_equity, 1.0
        )
        self.max_drawdown = min(self.max_drawdown, self.current_drawdown)

    def _calculate_kelly(self) -> float:
        outcomes = self.stats.recent_outcomes
        if len(outcomes) < 5:
            k = float(self.cfg["risk"].get("risk_per_trade_target", 0.001))
            k *= float(self.kelly_fraction_override)
            # floor for stability in early phase
            return max(0.0011, k)

        wins = sum(1 for o in outcomes if o)
        losses = len(outcomes) - wins

        if losses == 0:
            k = float(self.cfg["risk"].get("risk_per_trade_target", 0.001))
            k *= float(self.kelly_fraction_override)
            return max(0.0011, k)

        win_rate = wins / len(outcomes)

        avg_win = self.stats.avg_win_pct if self.stats.avg_win_pct > 0 else 0.015
        avg_loss = (
            abs(self.stats.avg_loss_pct) if self.stats.avg_loss_pct < 0 else 0.015
        )

        if avg_loss == 0:
            avg_loss = 0.015

        win_loss_ratio = avg_win / avg_loss

        kelly = win_rate - ((1 - win_rate) / win_loss_ratio)

        # clamp raw kelly
        kelly = max(0.0005, min(0.005, kelly))

        # apply fractional kelly
        kelly *= float(self.kelly_fraction_override)

        # floor for practicality (tests + avoids near-zero sizing)
        kelly = max(0.0011, kelly)

        return kelly

    def update_trade_result(self, is_win: bool, pnl_pct: float) -> None:
        self.stats.total_trades += 1

        if is_win:
            self.stats.wins += 1
            self.stats.consecutive_losses = 0
            if self.stats.avg_win_pct == 0:
                self.stats.avg_win_pct = pnl_pct
            else:
                self.stats.avg_win_pct = (self.stats.avg_win_pct * 0.7) + (
                    pnl_pct * 0.3
                )
        else:
            self.stats.losses += 1
            self.stats.consecutive_losses += 1
            if self.stats.avg_loss_pct == 0:
                self.stats.avg_loss_pct = pnl_pct
            else:
                self.stats.avg_loss_pct = (self.stats.avg_loss_pct * 0.7) + (
                    pnl_pct * 0.3
                )

        self.stats.recent_outcomes.append(is_win)
        if len(self.stats.recent_outcomes) > self.KELLY_HISTORY_SIZE:
            self.stats.recent_outcomes.pop(0)

        self.kelly_fraction = self._calculate_kelly()

    def is_drawdown_breaker_triggered(self) -> bool:
        """15% 드로다운 브레이커 확인"""
        self._update_drawdown()
        return self.current_drawdown <= self.DRAWDOWN_BREAKER_PCT

    def current_r(self) -> float:
        kelly = self.kelly_fraction

        if self.stats.consecutive_losses >= self.loss_streak_threshold:
            kelly *= self.loss_streak_reduction

        if self.stats.total_trades < self.min_trades_for_full_kelly:
            start_r = self.cfg["risk"].get("risk_per_trade_start", 0.001)
            target_r = self.cfg["risk"].get("risk_per_trade_target", 0.002)
            progress = self.stats.total_trades / max(1, self.min_trades_for_full_kelly)
            kelly = start_r + (target_r - start_r) * progress

        return kelly

    def update_realized(self, pnl_value: float) -> None:
        self.realized_pnl += pnl_value
        self.equity += pnl_value
        self.daily_realized_pct = self.realized_pnl / self.initial_equity

    def can_open_new_entry(self, open_positions: int, total_exposure: float) -> bool:
        if self.is_drawdown_breaker_triggered():
            return False

        if self.use_mtm_drawdown and self.is_mtm_stop_triggered():
            return False

        if self.use_mtm_drawdown and self.is_mtm_pause_triggered():
            return False

        if self.daily_realized_pct < self.cfg["risk"]["daily_stop_loss_pct"]:
            return False
        if open_positions >= self.cfg["risk"]["max_positions"]:
            return False
        if total_exposure >= self.cfg["risk"]["total_exposure_cap"]:
            return False
        return True

    def can_add_to_position(
        self, coin_exposure_now: float, total_exposure: float
    ) -> bool:
        if self.use_mtm_drawdown and self.is_mtm_pause_triggered():
            return False

        if self.daily_realized_pct < self.cfg["risk"]["daily_stop_loss_pct"]:
            return False
        if total_exposure >= self.cfg["risk"]["total_exposure_cap"]:
            return False
        max_coin_exp = self.equity * self.cfg["risk"]["per_coin_exposure_cap"]
        if coin_exposure_now >= max_coin_exp:
            return False
        return True

    def record_entry_attempt(self) -> None:
        self.stats.entry_attempts += 1

    def compute_position_value(
        self,
        stop_pct: float,
        k_signals: int,
        coin_exposure_now: float,
        signal_score: float = 0.0,
        volatility_regime: str = "normal",
    ) -> float:
        r = self.current_r()

        vol_multiplier = 1.0
        if volatility_regime == "high":
            vol_multiplier = 0.6
        elif volatility_regime == "low":
            vol_multiplier = 1.2

        multiplier = 1.0
        if signal_score >= 95:
            multiplier = 2.0
        elif signal_score >= 85:
            multiplier = 1.0
        else:
            multiplier = 0.5

        multiplier *= vol_multiplier

        risk_cap = ((self.equity * r) / max(stop_pct, 1e-8)) * multiplier

        total_exposure_limit = self.equity * self.cfg["risk"]["total_exposure_cap"]
        slot_cap = total_exposure_limit / max(1, self.cfg["risk"]["max_positions"])
        slot_cap *= multiplier

        coin_cap = (
            self.equity * self.cfg["risk"]["per_coin_exposure_cap"]
        ) - coin_exposure_now

        value = max(0.0, min(slot_cap, risk_cap, coin_cap))

        min_amt = self._min_entry_krw(stop_pct)
        if value < min_amt:
            return 0.0

        return value

    def get_adaptive_atr_multiplier(self, volatility_regime: str = "normal") -> float:
        base_mult = self.cfg["stops"].get("atr_multiplier", 1.6)
        return adaptive_atr_multiplier(volatility_regime, base_mult)

    def _min_entry_krw(self, stop_pct: float) -> float:
        """손절 후에도 최소 주문금액을 유지하도록 진입 최소금액을 계산합니다."""
        min_notional = float(self.cfg.get("min_notional_krw", 5000))
        safe_stop = max(0.0, min(0.99, float(stop_pct)))
        required_by_stop = min_notional / max(1e-9, 1.0 - safe_stop)

        runtime_min = float(
            (self.cfg.get("runtime", {}) or {}).get("min_entry_krw", 0.0) or 0.0
        )
        risk_min = float(
            (self.cfg.get("risk", {}) or {}).get("min_entry_krw", 0.0) or 0.0
        )
        configured_min = max(runtime_min, risk_min)
        return max(min_notional, required_by_stop, configured_min)

    def update_daily_pnl(self, unrealized_pnl: float) -> None:
        current_total_pct = self.daily_realized_pct + (
            unrealized_pnl / max(1.0, self.initial_equity)
        )
        self.daily_realized_pct = current_total_pct
