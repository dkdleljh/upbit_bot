# 업비트 멀티마켓 스캘핑 봇 MVP 사용설명서

## 1. 프로그램 개요
이 프로그램은 업비트 현물시장에서 KRW/BTC/USDT 전체 마켓을 대상으로,
1분봉 브레이크아웃 기반의 단타 전략을 자동 실행하는 MVP입니다.

핵심 목적:
- 거래대금이 큰 종목 위주로 자동 선별
- 체결 품질(스프레드/호가깊이/슬리피지) 기준을 통과한 경우에만 진입
- 포지션 수/익스포저/손절/익절/시간컷 규칙으로 리스크 통제
- 매매/신호/품질 데이터를 DB와 CSV로 누적 기록

## 2. 폴더 구조
- `config/config.yaml`: 전체 전략 파라미터
- `data/trading_bot.db`: SQLite DB
- `logs/bot.log`: 실행 로그
- `reports/`: 일일 CSV 출력
- `src/`: 실행 코드
- `tests/`: 최소 단위 테스트

## 3. 실행 전 준비
1) 가상환경 생성 및 패키지 설치
```bash
cd /home/zenith/Desktop/upbit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install aiohttp pyyaml numpy pandas pytest
```

2) live 모드용 API 키(필수, paper/backtest는 불필요)

⚠️ **주의(중요)**
- 아래 값은 "예시"입니다. 문서/코드/레포 안에 실제 키를 적지 마세요.
- 키는 `.env`(Git에 커밋되지 않음) 또는 `~/.upbit_bot.env` 같은 로컬 전용 파일에만 보관하세요.

예시(환경변수로 주입):
```bash
export UPBIT_ACCESS_KEY="YOUR_UPBIT_ACCESS_KEY"
export UPBIT_SECRET_KEY="YOUR_UPBIT_SECRET_KEY"
```

권장(로컬 .env 사용):
```bash
cd /home/zenith/Desktop/upbit_bot
cp .env.example .env
nano .env
# git status에서 .env가 보이면(추적되면) 안 됩니다.
```

## 4. 실행 방법
### 4-0. 권장 실행 스크립트
```bash
cd /home/zenith/Desktop/upbit_bot
./scripts/run_paper.sh
./scripts/run_backtest.sh
./scripts/run_live.sh
```
- 스크립트는 `.venv`에 필수 모듈이 없으면 자동으로 시스템 Python으로 전환합니다.

### 4-1. Paper 모드(권장 시작)
```bash
python -m src.main --mode paper
```
- 실주문 없이 모의 체결합니다.
- 네트워크 실패 시에도 mock 데이터로 루프가 유지되도록 구성되어 있습니다.

### 4-2. Backtest 모드
```bash
python -m src.main --mode backtest
```
- 최근 30일 보수적 슬리피지 가정 기반 성과 요약을 `daily_summary`에 기록합니다.

### 4-3. Live 모드
```bash
python -m src.main --mode live
```
- Upbit JWT 서명 기반으로 실제 시장가 주문을 전송합니다.
- 주문 체결이 비정상/부분체결이면 1회만 재시도 후 티커 쿨다운 처리합니다.
- API 키가 없으면 안전하게 모의체결로 폴백됩니다.
- API 키는 `/home/zenith/.upbit_bot.env` 에 저장되어 있으며, `~/.bashrc`에서 자동 로드됩니다.

## 5. 전략/리스크 핵심 규칙
- 유니버스: 5분마다 전체 마켓 거래대금 KRW 환산 Top30 -> 거래가능 필터 -> Top10
- 진입 조건:
  - 1분봉 20분 고점 돌파
  - 1분 거래대금 비율 >= 3.0
  - BTC 레짐(EMA20 >= EMA60)
  - 체결 게이트 통과
  - 최소 주문금액 50,000 KRW 이상
- 포지션 제한:
  - 최대 10개
  - 총 익스포저 70% 이하
  - 동일 코인 12% 이하
- 청산:
  - ATR 기반 하드 스탑
  - 순손익 +5% 시 절반 익절 + 트레일링
  - 8분 성과 미흡 시 소프트컷
  - 20분 강제종료
- 보호장치:
  - 일손실 -2% 이하면 신규 진입 차단
  - 슬리피지 과다 시 티커 30분 쿨다운
  - REST 오류 급증 시 safe mode

## 6. 결과 확인
1) 로그: `logs/bot.log`
2) DB 확인:
```bash
sqlite3 data/trading_bot.db ".tables"
sqlite3 data/trading_bot.db "select count(*) from trades;"
```
3) CSV 리포트: `reports/YYYY-MM-DD_*.csv`

## 7. 자주 발생하는 이슈
- 모듈 없음 오류: 가상환경 활성화 및 패키지 재설치
- 권한 오류: 해당 폴더 쓰기 권한 확인
- 네트워크 제한: paper 모드에서는 mock 폴백으로 동작 가능

## 8. 실전 투입 전 체크리스트
1) API 키 권한(조회/주문) 최소화 및 출금 권한 비활성화
2) 주문 체결/부분체결/재시도 시나리오 소액 검증
3) 장애 상황(WS 끊김/REST 제한) 복구 테스트
4) 수수료/슬리피지/틱사이즈 정합성 재검증
5) 소액 장기간 paper 트래킹 후 단계적 증액
