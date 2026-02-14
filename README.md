# Upbit Multi-Market Scalping Bot MVP

업비트 현물(KRW/BTC/USDT) 멀티마켓 자동매매 MVP입니다.

## 1) 특징
- 유니버스: 5분마다 전 마켓 24h 거래대금(KRW 환산) Top30 -> 거래가능 필터 -> Top10
- 시그널: 1분봉 20분 고점 돌파 + 1분 거래대금 비율 + BTC 레짐 필터
- 집행 게이트: 스프레드/호가깊이/슬리피지 추정 하드컷
- 리스크: 최대 10포지션, 총 익스포저 70%, 코인당 12%
- 청산: ATR 기반 하드스탑, 순손익 +5% TP1(50%), 트레일링, 시간컷
- 교체: 포지션 10개 가득 찰 때 점수 기반 교체
- 저장: SQLite(WAL), 일일 CSV 스냅샷
- 모드: `live`, `paper`, `backtest`

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

## 8) 테스트
```bash
pytest -q
```

## 9) 주의
- 본 코드는 MVP입니다. 실계정 투입 전 소액으로 충분히 검증하세요.
