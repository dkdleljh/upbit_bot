# Upbit 멀티마켓 자동매매 봇 (초보자용 가이드)

업비트 현물(KRW/BTC/USDT) 멀티마켓 자동매매 봇입니다.

- **paper 모드(모의매매)**: 실주문 없이 동작 → **처음엔 무조건 여기서 시작**
- **backtest 모드(백테스트)**: 과거 데이터 기반 간이 검증
- **live 모드(실거래)**: 실제 주문 전송 → **키/안전장치 설정 필수**

---

## 0) 가장 중요한 안전 수칙(필독)

### 0-1. API 키/토큰은 절대 GitHub에 올리지 마세요
- 실제 키는 **`.env` 또는 `~/.upbit_bot.env` 같은 로컬 전용 파일**에만 저장하세요.
- 이 레포에는 **키 없는 템플릿 파일인 `.env.example`만** 포함됩니다.
- **한 번이라도** 키가 Git 히스토리에 들어가면(나중에 삭제해도) 흔적이 남습니다.
  - 그 경우: **키 폐기/재발급**이 정답입니다.

### 0-2. 처음 실행은 paper 모드로
- live 모드는 실주문이 나갑니다. 실계좌 투입 전:
  - paper 모드로 충분히 검증
  - 소액으로 단계적 전환

---

## 1) 준비물

- OS: Linux/Ubuntu 기준(다른 OS도 가능)
- Python 3.x
- (권장) 가상환경: `venv`

현재 폴더 위치(이 프로젝트는 아래 경로를 기준으로 작성됨):
```bash
/home/zenith/Desktop/upbit_bot
```

---

## 2) 설치(처음 한 번)

### 2-1. 폴더로 이동
```bash
cd /home/zenith/Desktop/upbit_bot
```

### 2-2. 가상환경 생성/활성화
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2-3. 필수 패키지 설치
```bash
pip install --upgrade pip
pip install aiohttp pyyaml numpy pandas pytest
```

---

## 3) (중요) 키 설정 방법 — 초보자용

### 3-1. 템플릿 복사해서 `.env` 만들기
```bash
cd /home/zenith/Desktop/upbit_bot
cp .env.example .env
```

### 3-2. `.env` 파일에 실제 키 입력(로컬에서만)
```bash
nano .env
```

- `UPBIT_ACCESS_KEY=...`
- `UPBIT_SECRET_KEY=...`
- (선택) 텔레그램 알림을 쓰면 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`도 입력

### 3-3. 마지막 확인: `.env`는 Git에 잡히면 안 됩니다
```bash
git status
```
- `git status` 결과에 **`.env`가 보이면 안 됩니다.**
- 보인다면 즉시 중단하고 `.gitignore` 설정을 확인하세요.

> 참고: `.env`는 `.gitignore`로 커밋/푸시에서 제외되도록 설정돼 있어야 합니다.

---

## 4) 실행 방법(초보자 추천 순서)

### 4-1. paper 모드(처음 실행은 이걸로)
```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
python -m src.main --mode paper
```

### 4-2. backtest 모드
```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
python -m src.main --mode backtest
```

### 4-3. health 체크(프로세스/DB/WS 상태 확인)
```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
python -m src.main --health
```

### 4-4. live 모드(실거래: 매우 주의)
```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
python -m src.main --mode live
```

- live 모드는 **실제 주문이 나갈 수 있습니다.**
- 키가 없으면 안전하게 동작을 제한하거나 paper로 폴백하도록 구성되어 있습니다.

---

## 5) 설정 파일은 어디서 바꾸나요?

전략/리스크 파라미터는 여기서 조정합니다:
- `config/config.yaml`

예: 최대 포지션 수, 총 익스포저, 스프레드 컷, 손절/익절/트레일링 등

---

## 6) 결과/로그는 어디에 저장되나요?

(프로젝트 설정에 따라 다를 수 있으나 기본은 다음과 같습니다)
- DB: `data/trading_bot.db`
- 로그: `logs/`
- 리포트: `reports/`

주의:
- `logs/`, `reports/`, `data/*.db` 같은 **실행 산출물은 Git에 올리지 않는 것을 권장**합니다.

---

## 7) 테스트(문제 생겼을 때 가장 먼저)

### 7-1. 전체 테스트
```bash
cd /home/zenith/Desktop/upbit_bot
source .venv/bin/activate
PYTHONPATH=/home/zenith/Desktop/upbit_bot pytest tests/ -v
```

---

## 8) 커밋 전 안전장치(pre-commit)

이 레포는 실수로 키가 커밋되는 것을 막기 위해 **pre-commit hook**을 사용합니다.

### 8-1. 설정(1회)
```bash
cd /home/zenith/Desktop/upbit_bot
git config core.hooksPath .githooks
```

### 8-2. 커밋하면 자동으로 하는 일
- staged 파일에서 **키/토큰처럼 보이는 패턴이 감지되면 커밋 차단**
- `scripts/smoke_check.sh` 실행
  - pytest 실행
  - healthcheck 실행

---

## 9) 자주 하는 실수 / 해결

### Q1. `.env`가 Git에 잡혀요(추적되거나 stage됨)
1) 일단 커밋 중단
2) stage에서 제거:
```bash
git restore --staged .env
```
3) `.gitignore`에 `.env`가 들어있는지 확인

### Q2. 모듈이 없다고 나와요
가상환경 활성화 후 다시 설치:
```bash
source .venv/bin/activate
pip install -r requirements.txt  # (있다면)
# 또는
pip install aiohttp pyyaml numpy pandas pytest
```

### Q3. live 모드가 무서워요
정상입니다. 아래 순서대로만 가세요:
- paper → backtest → 소액 live

---

## 10) 폴더 구조(대략)
- `src/` : 실행 코드
- `config/` : 전략 파라미터
- `scripts/` : 실행/유틸 스크립트
- `tests/` : 테스트

---

## 면책
본 코드는 MVP이며, 실거래 손실에 대한 책임은 사용자에게 있습니다. 실계좌 투입 전 충분히 검증하세요.
