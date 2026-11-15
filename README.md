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

<!-- BEGIN RELEASE STATUS -->
## 최신 배포 정보

- 저장소 버전: `v2026.10.10.1`
- [변경사항과 검증 범위](RELEASE_NOTES.md)
- [GitHub 릴리즈](https://github.com/dkdleljh/upbit_bot/releases/latest)
<!-- END RELEASE STATUS -->


## 거래지원 종료 및 주문 장애 안전장치

이 절차는 코드 검증 안내입니다. PR 병합·서버 배포·실계좌 검증을 의미하지 않습니다.

- `market_safety`에서 SOPH·ZIL의 신규 매수(더스트 추가 매수 포함)를 차단합니다.
  `2026-11-16T15:00:00+09:00`부터 해당 자산의 모든 신규 주문과 잔량 재주문을 차단합니다.
  종료 전에는 기존 전략의 매도 판단을 유지하며 강제 청산을 추가하지 않습니다.
- 종목 상세 목록을 60초마다 다시 조회합니다. 목록 조회 실패/120초 초과, 종목 누락,
  매수 대상의 유의종목 표시 또는 경보 정보 누락이면 주문을 차단합니다.
  목록 API에는 종료 예정일이 없으므로 향후 종료 공지는 `trading_ends_at`에 별도로 등록해야 합니다.
- 주문 직전과 실제 전송 직전에 목록·종료 시각·90초 이내 시세/호가를 확인합니다.
  실거래 진입은 180초 이내 캔들도 요구합니다. 종료/누락 종목의 시세 캐시는 제거하지만
  보유 기록·주문 원장은 보존합니다. 데이터 장애 시 매도도 차단될 수 있습니다.
- POST/DELETE는 자동 재시도하지 않습니다. 주문 전 고유 `identifier`와 요청을
  SQLite `order_intents`에 저장합니다. 타임아웃·5xx·불명확한 응답은 `unknown`으로 남기고
  같은 기초자산의 KRW/BTC/USDT 주문을 차단합니다. 일시적 GET 오류만 제한적으로 재시도합니다.
- 지정가 매도는 취소 결과와 최종 `cancel`/`done`을 확인한 후 처리합니다.
  봇이 요청한 취소가 확정된 경우에만 남은 수량을 한 번 시장가로 시도합니다.
  거래소의 외부 취소나 `wait`/조회 실패는 재주문 근거로 사용하지 않습니다.
  두 주문의 실제 체결 수량·대금·수수료를 합산하고 미체결 보유 수량을 유지합니다.
- BTC/USDT 시장가 매수도 `bid + price`를 사용하며 금액은 해당 호가통화 단위입니다.
  현재 주 전략의 시장가 매수 경로는 유지합니다. 사용되지 않던 legacy maker helper는
  안전한 공통 실행 경로로 통합해 실거래 확인·킬스위치·모의매매 구분을 우회하지 못하게 했습니다.

### 재시작 및 미확정 주문 확인

재시작 후와 실행 중 60초마다 **이 버전이 기록한 미확정 주문만 조회**합니다.
조회 시 `uuid` 또는 `identifier`를 사용하고, 새 주문이나 일괄 취소를 실행하지 않습니다.

- 최종 취소/완료이며 체결량이 0임을 확인하면 잠금을 해제할 수 있습니다.
- 체결량이 있거나 조회 실패/404이면 자동으로 거래를 재개하지 않습니다.
  `ORDER_RECONCILIATION_REQUIRED`/`ORDER_STILL_UNKNOWN` 이벤트와 원장에 남깁니다.
  복구된 체결을 자동으로 포트폴리오에 재적용하면 이중 반영될 수 있으므로 수동 대조가 필요합니다.
- 원장을 읽기 전용으로 확인하는 예:

```bash
sqlite3 -readonly data/trading_bot.db \
  "SELECT identifier,market,side,uuid,status,acknowledged,updated_ms FROM order_intents WHERE acknowledged=0;"
```

운영자는 거래소 주문 상세·체결내역, 원장의 `detail_json`, `trades` 및 보유 수량을 대조해야 합니다.
404만으로 주문 미접수를 단정하거나 DB 행을 삭제해 재주문하지 마세요.
수동 대조 후 재개 기능은 이 패치에 포함하지 않았습니다. 확인 전에는 해당 자산이 계속 차단됩니다.
기존 버전/수동 주문은 이 원장에 없으므로 배포 전 별도로 확인해야 합니다.
`live.manage_existing_positions: false`는 유지하며 수동 보유분을 자동 관리 대상으로 편입하지 않습니다.

`UPBIT_KILL_SWITCH=1`은 기존 미체결 주문 취소 기능이 아닙니다. 엔진은 시작 시 값을 읽고,
REST 제출 시에도 현재 프로세스 환경을 확인합니다. 외부 셸이나 환경파일 수정만으로 실행 중
프로세스의 환경은 바뀌지 않습니다. 또한 독립 `scripts/cleanup_dust.py`는 주문 원장과
시세 검증을 연결하지 않으므로 이 버전의 REST 계층에서 실주문이 차단됩니다.

### 오프라인 회귀 검증과 배포 전 확인

```bash
python -m pytest -q tests
```

`tests/conftest.py`가 모든 테스트의 외부 소켓 연결과 실제 키 환경 사용을 차단합니다.
종료 직전·정각·직후, 조회 실패·오래된 시세, 주문 응답 유실, 취소 실패, 부분 체결,
최소 주문금액, 재시작·미확정 주문 잠금, 기존 전략·리스크 회귀를 검증합니다.
`paper` 결과만으로 실주문 오류 경로의 안전성을 판단하지 마세요.

배포 전에는 운영 프로세스/버전, 보유·미체결 주문, 실제 DB 경로를 읽기 전용으로 확인하고
DB를 백업해야 합니다. 신규 테이블은 시작 시 추가되며 기존 테이블과 이력은 삭제하지 않습니다.
롤백해도 `order_intents`를 삭제하지 마세요. 구버전은 이 잠금을 인식하지 못하므로
미확정 주문을 대조하기 전 구버전 실거래를 재개하지 마세요.

공식 API 근거(2026-11-15 확인):
[종목 상세 목록](https://docs.upbit.com/kr/reference/list-trading-pairs),
[주문 생성과 identifier](https://docs.upbit.com/kr/reference/new-order),
[UUID/identifier 주문 조회](https://docs.upbit.com/kr/reference/get-order),
[BTC 최소 금액](https://docs.upbit.com/kr/docs/btc-market-info),
[USDT 최소 금액](https://docs.upbit.com/kr/docs/usdt-market-info).
