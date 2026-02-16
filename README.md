# Upbit Multi-Market Scalping Bot

업비트 현물(KRW/BTC/USDT) 멀티마켓 자동매매 봇입니다.

## 1) 특징

### 핵심 기능
- **유니버스**: 5분마다 전 마켓 24h 거래대금(KRW 환산) Top30 -> 거래가능 필터 -> Top10
- **시그널**: 1분봉 20분 고점 돌파 + 1분 거래대금 비율 + BTC 레짐 필터
- **집행 게이트**: 스프레드/호가깊이/슬리피지 추정 하드컷
- **리스크**: 최대 10포지션, 총 익스포저 70%, 코인당 12%
- **청산**: ATR 기반 하드스탑, 순손익 +5% TP1(50%), 트레일링, 시간컷
- **교체**: 포지션 10개 가득 찰 때 점수 기반 교체

### 시그널 엔진 (15+ 기술 지표)
- EMA 정배열, RSI, MACD, Bollinger Bands
- ATR, VWAP, ADX, Stochastic RSI
- RSI Divergence, Candle Pattern Recognition
- Momentum Score, Order Flow Imbalance
- Liquidity Pressure Detection, Market Regime

### 안전장치
- Kelly Criterion 기반 적응형 리스크 관리
- -15% 드로다운 브레이커
- -2% 일일 손실 한도
- Circuit Breaker (연속 손절/주문 오류)
- Safe Mode 자동 복구
- Market Quarantine (반복 손절 코인 격리)
- 중복 주문 방지 (Dedup DB)

### 백테스트 & 분석
- Historical Data 기반 백테스트
- 샤프 비율, 칼마 비율, 소르티노 비율
- 최대 드로다운, 이익 계수, 기대값

## 2) 설치
```bash
cd /home/zenith/Desktop/upbit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install aiohttp pyyaml numpy pandas
```

## 3) 실행
```bash
cd /home/zenith/Desktop/upbit_bot
python -m src.main --mode paper
python -m src.main --mode backtest
python -m src.main --mode live
python -m src.main --health
```

## 4) API 키 설정 (보안 중요)

### 절대 .env 파일에 키를 직접 입력하지 마세요!

**올바른 설정 방법:**
```bash
# 1. 템플릿 복사
cp .env.example .env

# 2. .env 파일에 실제 키 입력 (터미널에서만)
nano .env

# 3. .env 파일이 .gitignore에 포함되었는지 확인
git status  # .env가 tracked 상태가 아니어야 함
```

**추가 보안 권장사항:**
- 실제 운영 키는 `/home/zenith/.upbit_bot.env`에 저장
- `.env` 파일은 개발용 더미 키만 사용
- 키 정기적 교체 (월 1회 이상)
- IP 화이트리스트 설정

### 라이브 모드 실행
```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
python -m src.main --mode live
```

live 모드에서는 Upbit JWT 서명 기반 주문을 사용하며, 키가 없으면 자동으로 paper 모드로 폴백됩니다.

실거래 주문은 안전 기본값으로 **명시적 확인이 있어야만** 활성화됩니다.
```bash
export UPBIT_LIVE_CONFIRM=YES     # 없으면 live 모드에서도 주문 차단
export UPBIT_KILL_SWITCH=0        # 1이면 신규 주문 전면 차단
export UPBIT_MAX_ORDER_KRW=100000 # 1회 최대 주문 금액(기본 100,000 KRW)
export UPBIT_MAX_TRADES_PER_DAY=30
```

## 5) 설정 파일
`config/config.yaml`에서 다음을 조정할 수 있습니다.
- 유니버스 개수 및 주기
- 스프레드/깊이/슬리피지 게이트
- 브레이크아웃/거래대금 조건
- 포지션/익스포저/리스크 퍼 트레이드
- ATR 스탑/TP/트레일링/시간컷
- 교체 정책
- 수수료 및 백테스트 슬리피지

## 6) 데이터 저장
- DB: `data/trading_bot.db`
- 로그: `logs/bot.log`
- 리포트: `reports/YYYY-MM-DD_*.csv`

## 7) 안전장치
- 일손실 -2% 이하 시 신규 진입 차단
- 슬리피지 초과 시 티커 30분 쿨다운
- REST 오류 급증 시 safe mode(신규 진입 차단)
- 키 하드코딩 금지(환경변수 사용)

## 8) 커밋 전 안전장치 (pre-commit)
이 레포는 실수로 API 키/토큰이 커밋되는 것을 막기 위해 **pre-commit hook**을 사용합니다.

설정(1회):
```bash
cd /home/zenith/Desktop/upbit_bot
git config core.hooksPath .githooks
```

동작:
- 커밋 시 staged diff에서 업비트 키/텔레그램 토큰 등 **시크릿 패턴**이 감지되면 커밋을 차단합니다.
- `scripts/smoke_check.sh`(pytest + healthcheck)를 실행합니다.

## 9) 테스트
```bash
# 전체 테스트
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/ -v

# 수익성 검증 테스트
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/test_profitability.py -v

# 시그널 테스트
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/test_scoring.py -v

# 리스크 테스트
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/test_sizing.py -v
```

## 9) 주의
- 본 코드는 MVP입니다. 실계정 투입 전 소액으로 충분히 검증하세요.
