#!/usr/bin/env bash
set -euo pipefail

cd /home/zenith/Desktop/upbit_bot

echo "=== 업비트 과거 데이터 수집 스크립트 ==="

# 가상환경 체크
PY=python3
if [ -x .venv/bin/python ]; then
  if .venv/bin/python - <<'PYCHK' >/dev/null 2>&1
import aiohttp, pandas
PYCHK
  then
    PY=.venv/bin/python
  fi
fi

# 시작/종료일 설정 (기본 최근 30일)
if [ $# -eq 2 ]; then
    START_DATE=$1
    END_DATE=$2
else
    END_DATE=$(date +"%Y-%m-%d")
    START_DATE=$(date -d "30 days ago" +"%Y-%m-%d")
fi

echo "수집 기간: $START_DATE ~ $END_DATE"

# 데이터 수집 실행
"$PY" <<'PYEOF'
import asyncio
import sys
from datetime import datetime, timedelta

from src.data_manager import DataManager
from src.storage import Storage

async def main():
    try:
        storage = Storage("data/trading_bot.db")
        data_manager = DataManager(storage)
        
        # 상위 마켓 목록
        markets = [
            "KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-ADA", "KRW-DOT",
            "KRW-LTC", "KRW-BCH", "KRW-ETC", "KRW-SOL", "KRW-MATIC",
            "KRW-AVAX", "KRW-LINK", "KRW-UNI", "KRW-AAVE", "KRW-ATOM"
        ]
        
        # 날짜 파싱
        start_date = datetime.strptime("$START_DATE", "%Y-%m-%d")
        end_date = datetime.strptime("$END_DATE", "%Y-%m-%d")
        
        print(f"{len(markets)}개 마켓 데이터 수집 시작...")
        results = await data_manager.collect_universe_data(markets, start_date, end_date)
        
        # 결과 요약
        successful = sum(1 for success in results.values() if success)
        total = len(results)
        
        print(f"\n=== 수집 결과 ===")
        print(f"성공: {successful}/{total} 마켓")
        
        failed_markets = [m for m, success in results.items() if not success]
        if failed_markets:
            print(f"실패: {', '.join(failed_markets)}")
        
        # 커버리지 상세 정보
        print(f"\n=== 데이터 커버리지 ===")
        for market in markets[:5]:  # 상위 5개만 표시
            coverage = data_manager.collector.get_data_coverage(market, start_date, end_date)
            print(f"{market}: {coverage['coverage_pct']:.1f}% ({coverage['total_candles']}캔들)")
        
        storage.close()
        
    except Exception as e:
        print(f"오류 발생: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
PYEOF

echo "데이터 수집 완료!"