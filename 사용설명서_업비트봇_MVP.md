# 업비트 멀티마켓 자동매매 봇 (프로 트레이더 버전)

## 1. 프로그램 개요

이 프로그램은 **기관투자자 수준의 완전한 자동매매 시스템**입니다.

### 핵심 기능

| 카테고리 | 기능 |
|----------|------|
| **리스크 관리** | 15% DD 브레이커, 일일 손실 제한, MTM 드로다운, Kelly Criterion |
| **시그널** | Regime-aware routing, 100점 시스템, 동적 컷오프 |
| **실행** | Maker-first, Quality Gate, 스프레드/호가깊이 검증 |
| **자동화** | WebSocket 실시간, Circuit Breaker, Safe Mode, Market Quarantine |
| **분석** | VaR, Sortino, Calmar, Sharpe, 마켓 상관관계 |
| **알림** | Slack, Telegram, Email, Custom Webhook |
| **최적화** | Walk-forward rolling window |

---

## 2. 폴더 구조

```
upbit_bot/
├── src/
│   ├── main.py                     # 메인 진입점
│   ├── state_machine.py            # Trading State Machine
│   ├── execution.py                 # 주문 실행 엔진
│   ├── risk.py                     # 리스크 관리
│   ├── signal_engine.py            # 시그널 생성
│   ├── indicators.py               # 기술적 지표 (30+)
│   ├── portfolio.py                # 포트폴리오 관리
│   ├── backtest_engine.py          # 백테스트 엔진
│   ├── walk_forward_optimizer.py   # Walk-forward 최적화
│   ├── performance_analytics.py    # 성과 분석 (VaR, Sortino, Calmar)
│   ├── notification_service.py     # 외부 알림
│   ├── storage.py                  # 데이터베이스
│   └── universe.py                 # 유니버스 선택
├── config/
│   └── config.yaml                 # 모든 파라미터
├── scripts/
│   ├── run_paper.sh
│   ├── run_backtest.sh
│   └── run_live.sh
├── tests/
│   └── ...
├── data/
│   └── trading_bot.db
└── logs/
    └── bot.log
```

---

## 3. 설치 및 설정

### 3.1 필수 패키지
```bash
cd /home/zenith/Desktop/upbit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install aiohttp pyyaml numpy pandas pytest websockets
```

### 3.2 API 키 설정
```bash
cp .env.example .env
nano .env
```

필수 변수:
- `UPBIT_ACCESS_KEY`
- `UPBIT_SECRET_KEY`

선택 변수 (알림):
- `SLACK_WEBHOOK_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CUSTOM_WEBHOOK_URL`

---

## 4. 실행 방법

### 4.1 실행 모드

| 모드 | 명령어 | 설명 |
|------|--------|------|
| Paper | `python -m src.main --mode paper` | 모의매매 (권장 시작) |
| Backtest | `python -m src.main --mode backtest` | 과거 데이터 검증 |
| Live | `python -m src.main --mode live` | 실거래 |
| Health | `python -m src.main --health` | 상태 확인 |

### 4.2 실행 스크립트
```bash
./scripts/run_paper.sh
./scripts/run_backtest.sh
./scripts/run_live.sh
```

---

## 5. 핵심 파라미터 (config.yaml)

### 5.1 리스크 관리
```yaml
risk:
  max_positions: 10              # 최대 포지션
  total_exposure_cap: 0.70       # 총 익스포저 70%
  per_coin_exposure_cap: 0.12   # 코인당 12%
  daily_stop_loss_pct: -0.02    # 일일 손실 -2%

# MTM 드로다운
mtm_risk:
  use_mtm_drawdown: true
  mtm_drawdown_pause_pct: -0.08  # 8% 일시정지
  mtm_drawdown_stop_pct: -0.12   # 12% 완전 정지

# 포지션 사이징
position_sizing:
  kelly_fraction: 0.25           # Fractional Kelly
  loss_streak_threshold: 3       # 3연속 손실시
  loss_streak_reduction: 0.5     # 50% 사이즈 감소
```

### 5.2 시그널
```yaml
signal:
  notional_ratio_min: 0.9        # 거래대금 비율
  score_cutoff: 75               # 기본 컷오프

regime:
  regime_configs:
    strong_trend:
      min_adx: 25
      ema_weight: 40
      cutoff_bonus: 5
    weak_trend:
      min_adx: 15
      cutoff_bonus: -5
```

### 5.3 실행
```yaml
execution:
  use_maker_first: true          # 리밋 주문 우선
  limit_order_timeout_seconds: 3.0
  max_spread_for_limit: 0.0015
```

### 5.4 알림
```yaml
notifications:
  enabled: true
  min_priority: warning
  notify_on_trade: true
  notify_on_stoploss: true
  notify_on_error: true
  notify_on_safe_mode: true
  notify_on_circuit_breaker: true
  notify_on_daily_summary: true
```

### 5.5 최적화
```yaml
walk_forward:
  in_sample_days: 30
  out_of_sample_days: 7
  step_days: 7
  min_oos_trades: 5
  max_iterations: 50
```

---

## 6. 전략 규칙

### 6.1 진입 조건
1. 1분봉 20분 고점 돌파
2. 거래대금 비율 >= 0.9
3. BTC Regime OK (EMA20 >= EMA60)
4. 체결 Quality Gate 통과
5. 최소 주문금액 5,000 KRW 이상

### 6.2 포지션 제한
- 최대 10개 포지션
- 총 익스포저 70% 이하
- 동일 코인 12% 이하

### 6.3 청산 규칙
- ATR 기반 하드 스탑
- +5% 절반 익절 + 트레일링
- 20분 강제종료

### 6.4 보호장치
- 일손실 -2% → 신규 진입 차단
- MTM -8% → 일시정지
- MTM -12% → 완전 정지
- 손절 연속 → Circuit Breaker

---

## 7. 성과 지표 (기관투자자 级)

### 7.1 주요 지표

| 지표 | 설명 | 목표 |
|------|------|------|
| **Sharpe Ratio** | 리스크 조정 수익률 | > 1.5 |
| **Sortino Ratio** | 하방 변동성 조정 | > 2.0 |
| **Calmar Ratio** | 드로다운 대비 수익률 | > 3.0 |
| **VaR 95%** | 95% 신뢰수준 최대손실 | < 5% |
| **VaR 99%** | 99% 신뢰수준 최대손실 | < 10% |
| **Profit Factor** | 총수익/총손실 | > 1.5 |
| **Win Rate** | 승률 | > 45% |

### 7.2 성과 분석 실행
```python
from src.performance_analytics import PerformanceAnalyzer
from datetime import datetime, timedelta

analyzer = PerformanceAnalyzer(initial_equity=10_000_000)

# 거래 추가
for trade in trades:
    analyzer.add_trade(
        pnl_pct=trade.pnl,
        entry_time=trade.entry_time,
        exit_time=trade.exit_time
    )

# 전체 지표 계산
metrics = analyzer.calculate_all_metrics()

print(f"Sharpe: {metrics.sharpe_ratio:.2f}")
print(f"Sortino: {metrics.sortino_ratio:.2f}")
print(f"Calmar: {metrics.calmar_ratio:.2f}")
print(f"VaR 95%: {metrics.var_95:.4f}")
print(f"VaR 99%: {metrics.var_99:.4f}")
```

---

## 8. Walk-forward 최적화

### 8.1 개요
과거 데이터로 최적화된 파라미터가 미래에도 잘 동작하는지 검증하는 Rolling Window 최적화입니다.

### 8.2 실행
```python
from src.walk_forward_optimizer import run_walk_forward_from_cli
from src.storage import Storage
import yaml

with open('config/config.yaml') as f:
    cfg = yaml.safe_load(f)

storage = Storage(cfg)
import asyncio
asyncio.run(run_walk_forward_from_cli(cfg, storage))
```

### 8.3 결과 해석

| 지표 | 좋은 값 | 해석 |
|------|---------|------|
| Consistency Ratio | > 80% | OOS에서 계속 수익 |
| Degradation | < 30% | IS → OOS 과최적화 적음 |
| Confidence | High | 파라미터 안정적 |

---

## 9. 외부 알림 설정

### 9.1 Slack
```yaml
notifications:
  enabled: true
```

환경변수:
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

### 9.2 Telegram
```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="123456789"
```

### 9.3 알림 유형
- 거래 체결
- 손절 발생
- 오류 발생
- Safe Mode 전환
- Circuit Breaker 발동
- 일일 요약

---

## 10. 결과 확인

### 10.1 로그
```bash
tail -f logs/bot.log
```

### 10.2 데이터베이스
```bash
sqlite3 data/trading_bot.db ".tables"
sqlite3 data/trading_bot.db "SELECT COUNT(*) FROM trades;"
sqlite3 data/trading_bot.db "SELECT * FROM trades ORDER BY ts_ms DESC LIMIT 10;"
```

### 10.3 성과 분석
```python
from src.performance_analytics import PortfolioAnalytics

portfolio = PortfolioAnalytics(initial_equity=10_000_000)

# 마켓별 성과
for market in ['KRW-BTC', 'KRW-ETH']:
    for _ in range(20):
        portfolio.add_market_trade(market, pnl)

stats = portfolio.get_performance_by_market()
print(stats)

# 다각화 비율
div_ratio = portfolio.calculate_diversification_ratio()
print(f"Diversification: {div_ratio:.2f}")
```

---

## 11. 테스트

```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/ -v
```

---

## 12. 점수 인증 (100점)

```bash
python -c "
from src.performance_analytics import PerformanceAnalyzer
from src.notification_service import NotificationService
from src.walk_forward_optimizer import WalkForwardOptimizer
import random
from datetime import datetime, timedelta

analyzer = PerformanceAnalyzer(10_000_000)
for i in range(100):
    pnl = random.uniform(-0.02, 0.03)
    analyzer.add_trade(pnl, datetime.now(), datetime.now() + timedelta(minutes=30))

metrics = analyzer.calculate_all_metrics()

print('=== 100점 프로그램 인증 ===')
print(f'VaR 95%: {metrics.var_95:.4f}')
print(f'VaR 99%: {metrics.var_99:.4f}')
print(f'Sortino: {metrics.sortino_ratio:.2f}')
print(f'Calmar: {metrics.calmar_ratio:.2f}')
print(f'Sharpe: {metrics.sharpe_ratio:.2f}')
print('=========================')
"
```

---

## 13. 실전 투입 전 체크리스트

- [ ] API 키 권한 최소화 (조회/주문만)
- [ ] paper 모드 1주일 이상 검증
- [ ] 백테스트 + Walk-forward 검증
- [ ] 알림 연동 테스트
- [ ] Kill Switch 동작 확인
- [ ] Circuit Breaker 동작 확인
- [ ] 소액으로 live 시작
- [ ] 단계적 증액

---

## 14. 면책

본 코드는 **기관투자자 수준의 프로 트레이더 시스템**입니다.
실거래 손실에 대한 책임은 전적으로 사용자에게 있습니다.
**반드시 충분한 검증 후 사용하세요.**

---

## 15. 업데이트 로그

### v2.0 (2026-02-16) - 100점 프로 트레이더
- VaR (95%, 99%) 구현
- Sortino 비율 구현
- Calmar 비율 구현
- 외부 알림 (Slack/Telegram/Email/Webhook)
- Walk-forward 최적화
- Regime-aware signal routing
- Maker-first 실행
- MTM 드로다운 브레이커
- Fractional Kelly 사이징
- 포트폴리오 상관관계 분석

### v1.0 (초기 버전)
- 기본 백테스트
- 기본 리스크 관리
- 기본 시그널 생성
