from dataclasses import dataclass


@dataclass
class TradeStats:
    total_trades: int = 0
    order_errors: int = 0
    avg_entry_slippage: float = 0.0


class RiskManager:
    def __init__(self, cfg: dict, initial_equity: float):
        self.cfg = cfg
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.realized_pnl = 0.0
        self.daily_realized_pct = 0.0
        self.stats = TradeStats()

    def current_r(self) -> float:
        promote = self.cfg["risk"]["promote_requirements"]
        if (
            self.stats.total_trades >= promote["min_trades"]
            and (self.stats.order_errors / max(1, self.stats.total_trades)) < promote["max_order_error_rate"]
            and self.stats.avg_entry_slippage <= promote["max_avg_entry_slippage"]
        ):
            return self.cfg["risk"]["risk_per_trade_target"]
        return self.cfg["risk"]["risk_per_trade_start"]

    def update_realized(self, pnl_value: float) -> None:
        self.realized_pnl += pnl_value
        self.equity += pnl_value
        self.daily_realized_pct = self.realized_pnl / self.initial_equity

    def can_open_new_entry(self, open_positions: int, total_exposure: float) -> bool:
        if self.daily_realized_pct <= self.cfg["risk"]["daily_stop_loss_pct"]:
            return False
        if open_positions >= self.cfg["risk"]["max_positions"]:
            return False
        if total_exposure >= self.cfg["risk"]["total_exposure_cap"]:
            return False
        return True

    def can_add_to_position(self, coin_exposure_now: float, total_exposure: float) -> bool:
        """피라미딩(추가 매수) 가능 여부 확인"""
        if self.daily_realized_pct <= self.cfg["risk"]["daily_stop_loss_pct"]:
            return False
        # 전체 노출 한도 체크
        if total_exposure >= self.cfg["risk"]["total_exposure_cap"]:
            return False
        # 개별 코인 한도 체크 (이미 많이 샀으면 그만)
        max_coin_exp = self.equity * self.cfg["risk"]["per_coin_exposure_cap"]
        if coin_exposure_now >= max_coin_exp:
            return False
        return True

    def compute_position_value(self, stop_pct: float, k_signals: int, coin_exposure_now: float, signal_score: float = 0.0) -> float:
        """
        Phase 2 Dynamic Sizing:
        - 신호 점수(Score)에 따라 베팅 비율 조절 (0.5배 ~ 2.0배)
        """
        r = self.current_r()
        
        # 1. 확신도에 따른 승수(Multiplier) 결정
        multiplier = 1.0
        if signal_score >= 95:
            multiplier = 2.0  # 확실하면 2배 (Max)
        elif signal_score >= 85:
            multiplier = 1.0  # 우수하면 정배 (Normal)
        else:
            multiplier = 0.5  # 긴가민가하면 절반 (Small)
            
        # 2. 리스크 기반 한도 계산 (Kelly Criterion 응용)
        # R%를 잃더라도 감내할 수 있는 금액 * 승수
        # 예: 자본금 100만원, R=0.1%(1000원), 손절폭=1% -> 10만원 베팅 * Multiplier
        risk_cap = ((self.equity * r) / max(stop_pct, 1e-8)) * multiplier
        
        # 3. 포트폴리오 슬롯 한도 (N빵)
        # 전체 시드의 70%를 최대 10개 종목에 분산 -> 슬롯당 7% 기본
        total_exposure_limit = self.equity * self.cfg["risk"]["total_exposure_cap"]
        slot_cap = total_exposure_limit / max(1, self.cfg["risk"]["max_positions"])
        # 여기서도 Multiplier 적용 (확신하면 한도를 좀 더 열어줌)
        slot_cap *= multiplier

        # 4. 개별 코인 최대 노출 한도 (몰빵 방지)
        coin_cap = (self.equity * self.cfg["risk"]["per_coin_exposure_cap"]) - coin_exposure_now
        
        # 5. 최종 진입 금액 (교집합)
        value = max(0.0, min(slot_cap, risk_cap, coin_cap))
        
        # 최소 주문 금액(10,000원) 미만이면 진입 포기
        min_amt = float(self.cfg.get("min_notional_krw", 10000))
        if value < min_amt:
            return 0.0
            
        return value
    
    def update_daily_pnl(self, unrealized_pnl: float) -> None:
        current_total_pct = self.daily_realized_pct + (unrealized_pnl / max(1.0, self.initial_equity))
        self.daily_realized_pct = current_total_pct
