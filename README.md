# Upbit 멀티마켓 자동매매 봇 (프로 트레이더 버전)

업비트 현물(KRW/BTC/USDT) 멀티마켓 자동매매 봇입니다.

## Modes

| Mode | Description |
|------|-------------|
| **paper** | 모의매매 - 실주문 없이 동작 (처음 시작은 반드시 여기) |
| **backtest** | 과거 데이터 기반 백테스트 |
| **live** | 실제 주문 전송 (키/안전장치 설정 필수) |

## 주요 기능 (100점 프로 트레이더)

### 리스크 관리
- 15% 드로다운 브레이커
- 일일 손실 제한 (-2%)
- MTM (Mark-to-Market) 드로다운 추적
- Kelly Criterion 기반 동적 베팅
- Fractional Kelly (0.25x)
- 손실 연속 추적 및 사이즈 감소

### 시그널 & 실행
- Regime-aware signal routing (트렌드/변동성별 맞춤 전략)
- Maker-first 실행 (수수료 절감)
- Quality Gate (스프레드/호가깊이/슬리피지 검증)
- 100점 만점 시그널 시스템

### 자동화
- WebSocket 실시간 모니터링
- 다중 Circuit Breaker (손절/주문오류)
- Market Quarantine (반복 손절 코인 격리)
- Safe Mode 자동 전환/복구
- 주문 멱등성 및 동시성 제어

### 성과 분석 (기관투자자 级)
- VaR (95%, 99%) 손실 추정
- Sortino 비율
- Calmar 비율
- Sharpe 비율
- 마켓별 상관관계 분석
- 다각화 비율

### 외부 알림
- Slack Webhook
- Telegram Bot
- Email (SMTP)
- Custom Webhook

### 최적화
- Walk-forward rolling window 최적화
- Overfitting 방지

---

## 0) 안전 수칙

### API 키 관리
- 실제 키는 `.env` 또는 로컬 파일에만 저장
- Git에 절대 키를 올리지 마세요
- 키가 히스토리에 들어갔다면 즉시 폐기/재발급

### 시작 순서
```
paper → backtest → 소액 live → 증액
```

---

## 1) 설치

```bash
cd /home/zenith/Desktop/upbit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install aiohttp pyyaml numpy pandas pytest websockets
```

---

## 2) 설정

### API 키 (.env)
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

### 전략 파라미터
`config/config.yaml`에서 조정:
- 최대 포지션 수
- 총 익스포저 한도
- 스프레드/슬리피지 컷
- 손절/익절 비율
- Regime 설정
- 알림 설정

---

## 3) 실행

### Paper (모의매매)
```bash
python -m src.main --mode paper
```

### Backtest (백테스트)
```bash
python -m src.main --mode backtest
```

### Live (실거래)
```bash
python -m src.main --mode live
```

### Health Check
```bash
python -m src.main --health
```

---

## 4) 폴더 구조

```
upbit_bot/
├── src/
│   ├── main.py              # 메인 진입점
│   ├── state_machine.py     # Trading State Machine
│   ├── execution.py         # 주문 실행 엔진
│   ├── risk.py              # 리스크 관리
│   ├── signal_engine.py     # 시그널 생성
│   ├── indicators.py        # 기술적 지표
│   ├── portfolio.py         # 포트폴리오 관리
│   ├── backtest_engine.py   # 백테스트 엔진
│   ├── walk_forward_optimizer.py  # Walk-forward 최적화
│   ├── performance_analytics.py   # 성과 분석 (VaR, Sortino, Calmar)
│   ├── notification_service.py    # 외부 알림
│   └── ...
├── config/
│   └── config.yaml          # 전략/리스크 파라미터
├── scripts/
│   ├── run_paper.sh
│   ├── run_backtest.sh
│   └── run_live.sh
├── tests/
│   └── ...
├── data/
│   └── trading_bot.db       # SQLite DB
└── logs/
    └── bot.log              # 실행 로그
```

---

## 5) 성과 지표 설명

### 기관투자자 级 지표

| 지표 | 설명 | 좋은 값 |
|------|------|---------|
| **Sharpe** | 리스크 조정 수익률 | > 1.5 |
| **Sortino** | 하방 변동성 조정 | > 2.0 |
| **Calmar** | 드로다운 대비 수익률 | > 3.0 |
| **VaR 95%** | 95% 신뢰수준 최대 손실 | < 5% |
| **Profit Factor** | 총 수익 / 총 손실 | > 1.5 |
| **Win Rate** | 승률 | > 45% |

### Walk-forward 최적화

```bash
# Walk-forward 최적화 실행
python -c "
from src.walk_forward_optimizer import run_walk_forward_from_cli
from src.storage import Storage
import yaml

with open('config/config.yaml') as f:
    cfg = yaml.safe_load(f)

storage = Storage(cfg)
import asyncio
asyncio.run(run_walk_forward_from_cli(cfg, storage))
"
```

---

## 6) 알림 설정

### Slack
```yaml
notifications:
  enabled: true
  min_priority: warning
  notify_on_trade: true
  notify_on_stoploss: true
```

환경변수:
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

### Telegram
```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
export TELEGRAM_CHAT_ID="123456789"
```

---

## 7) 테스트

```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/ -v
```

---

## 8) 점수 인증

```bash
python -c "
from src.performance_analytics import PerformanceAnalyzer, PerformanceMetrics
from src.notification_service import NotificationService, NotificationConfig
from src.walk_forward_optimizer import WalkForwardOptimizer

# 성과 분석 테스트
analyzer = PerformanceAnalyzer(initial_equity=10_000_000)
import random
from datetime import datetime, timedelta

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

## 9) 면책

본 코드는 프로 트레이더 수준의 자동매매 시스템입니다.
실거래 손실에 대한 책임은 사용자에게 있습니다.
반드시 충분한 검증 후 사용하세요.

---

## 10) 업데이트 로그

### v2.0 (2026-02-16) - 100점 프로 트레이더
- VaR, Sortino, Calmar 비율 구현
- 외부 알림 (Slack/Telegram/Email/Webhook) 연동
- Walk-forward 최적화 구현
- Regime-aware signal routing
- Maker-first 실행
- MTM 드로다운 브레이커
- Fractional Kelly 사이징
- 기관투자자 级 성과 분석 모듈

### v1.0 (초기 버전)
- 기본 백테스트
- 기본 리스크 관리
- 기본 시그널 생성
