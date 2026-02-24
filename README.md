# Upbit 자동매매 봇 (upbit_bot)

업비트 현물(KRW/BTC/USDT) 멀티마켓 자동매매 봇입니다.

> ⚠️ 면책
> - 투자 조언이 아닙니다.
> - 실거래는 반드시 `paper → backtest → 소액 live → 증액` 순서로 진행하세요.

---

## 문서
- 초보자용 사용설명서: `사용설명서.md`
- (기존 상세 문서): `사용설명서_업비트봇_MVP.md`
- 보안: `SECURITY.md`

---

## 빠른 시작

```bash
cd ~/Desktop/upbit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python -m src.main --mode paper
```

---

## 매매 전략(요약)

업비트봇은 단일 규칙 1개가 아니라, **시장 국면(regime) + 리스크 상태**에 따라 시그널/집행을 라우팅하는 구조입니다.

- **Regime-aware**: 추세/변동성/유동성 상태에 따라 서로 다른 시그널을 사용(또는 거래를 스킵)
- **Quality Gate**: 스프레드/호가 깊이/슬리피지 조건이 나쁘면 진입 자체를 제한
- **Maker-first 실행**: 가능하면 지정가 우선으로 수수료/체결 품질을 개선
- **리스크 브레이커**:
  - 일일 손실 제한, 드로다운 브레이커, 연속 손실 시 사이즈 축소
  - 필요 시 `UPBIT_KILL_SWITCH`로 즉시 주문 차단

> 실전 운용은 반드시 `paper → backtest → 소액 live` 순서로 검증하세요.

---

## 실행 모드

- `paper`: 모의매매(추천)
- `backtest`: 백테스트
- `live`: 실거래

실행:
```bash
python -m src.main --mode paper
python -m src.main --mode backtest
python -m src.main --mode live
```

상태 점검:
```bash
python -m src.main --health
```

---

## 보안(중요)

- `.env` 파일(키/시크릿)은 **절대 커밋 금지**
- 키가 노출되면 즉시 폐기/재발급

---

## 로그/데이터

- `logs/` : 실행 로그
- `data/trading_bot.db` : SQLite
- `reports/` : 리포트
