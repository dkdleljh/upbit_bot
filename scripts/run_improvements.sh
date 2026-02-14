#!/usr/bin/env bash
set -euo pipefail

cd /home/zenith/Desktop/upbit_bot

echo "=== 개선된 업비트 봇 테스트 실행 스크립트 ==="

# 가상환경 체크
PY=python3
if [ -x .venv/bin/python ]; then
  if .venv/bin/python - <<'PYCHK' >/dev/null 2>&1
import numpy, pandas, aiohttp
PYCHK
  then
    PY=.venv/bin/python
  fi
fi

# 테스트 실행 함수
run_tests() {
    echo "🧪 $1 테스트 시작..."
    "$PY" -m pytest tests/ -v --tb=short --disable-warnings
    echo ""
}

# 백테스트 실행 함수
run_backtest() {
    echo "📊 백테스트 실행 (과거 데이터 기반)..."
    "$PY" -m src.main --mode backtest
    echo ""
}

# 데이터 수집 함수
collect_data() {
    echo "📥 과거 데이터 수집..."
    "$PY" scripts/collect_historical_data.sh "$1" "$2"
    echo ""
}

# 보고서 생성 함수
generate_report() {
    echo "📋 테스트 보고서 생성..."
    "$PY" <<'PYEOF'
import asyncio
from src.storage import Storage
from src.backtest_analyzer import BacktestReporter
from datetime import datetime, timedelta

async def main():
    try:
        storage = Storage("data/trading_bot.db")
        reporter = BacktestReporter(storage)
        
        # Mock backtest result for demonstration
        from src.backtest_engine_simple import BacktestResult
        
        mock_result = BacktestResult(
            total_trades=150,
            win_rate=0.53,
            net_pnl_pct=0.032,
            max_drawdown_pct=0.067,
            avg_hold_seconds=450,
            avg_slippage_pct=0.0018,
            sharpe_ratio=1.24,
            profit_factor=1.31,
            per_market_stats={
                "KRW-BTC": {"trades": 50, "pnl": 0.045, "win_rate": 0.58},
                "KRW-ETH": {"trades": 40, "pnl": 0.028, "win_rate": 0.52},
                "KRW-XRP": {"trades": 30, "pnl": 0.015, "win_rate": 0.50}
            },
            equity_curve=[10000000 + i*1000 for i in range(30)],
            daily_returns=[0.001, -0.002, 0.003, -0.001, 0.002] * 6
        )
        
        report = reporter.generate_report(
            mock_result, 
            f"reports/demo_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        
        print("데모 보고서 생성 완료!")
        print("reports/ 폴더에서 확인하세요.")
        
        storage.close()
        
    except Exception as e:
        print(f"오류 발생: {e}")

if __name__ == "__main__":
    asyncio.run(main())
PYEOF
    echo ""
}

# 메뉴 표시
show_menu() {
    echo "🤖 업비트 봇 개선 과제 실행 메뉴"
    echo ""
    echo "1. 단위 테스트 실행 (SignalEngine, Portfolio, RiskManager)"
    echo "2. 통합 테스트 실행 (전체 시스템)"
    echo "3. 백테스트 실행 (과거 데이터 기반)"
    echo "4. 과거 데이터 수집"
    echo "5. 테스트 보고서 생성 (데모)"
    echo "6. 전체 성능 검증"
    echo "7. 나가기"
    echo ""
}

# 전체 성능 검증
run_performance_check() {
    echo "⚡ 전체 성능 검증..."
    
    echo "📦 필요한 패키지 확인..."
    "$PY" -c "
import sys
packages = ['numpy', 'pandas', 'aiohttp', 'pytest', 'pyyaml']
missing = []
for pkg in packages:
    try:
        __import__(pkg)
    except ImportError:
        missing.append(pkg)
        
if missing:
    print(f'❌ 누락된 패키지: {missing}')
    sys.exit(1)
else:
    print('✅ 모든 필요한 패키지 설치됨')
"
    
    echo ""
    echo "🏗️ 코드 품질 검사..."
    "$PY" -m flake8 src/ --max-line-length=120 --ignore=E501,W503 || echo "⚠️ 일부 코드 스타일 경고"
    
    echo ""
    echo "🧪 테스트 실행..."
    run_tests "기본"
    
    echo ""
    echo "📊 백테스트 검증..."
    run_backtest
}

# 메인 루프
main() {
    case "${1:-menu}" in
        "1")
            show_menu
            run_tests "SignalEngine"
            ;;
        "2")
            show_menu
            run_tests "통합"
            ;;
        "3")
            show_menu
            run_backtest
            ;;
        "4")
            show_menu
            collect_data "$(date -d '30 days ago' +%Y-%m-%d)" "$(date +%Y-%m-%d)"
            ;;
        "5")
            show_menu
            generate_report
            ;;
        "6")
            show_menu
            run_performance_check
            ;;
        "7")
            echo "👋 안녕!"
            exit 0
            ;;
        "menu"|*)
            show_menu
            echo "선택지를 입력하세요 (1-7):"
            read -r choice
            main "$choice"
            ;;
    esac
}

echo "🚀 업비트 봇 개선 과제 실행 시스템"
echo ""
echo "📋 개선 완료된 과제:"
echo "✅ 1. 백테스트 시스템 재구축"
echo "✅ 2. 과거 데이터 수집 및 저장 시스템"
echo "✅ 3. 실제 신호 생성 백테스트 로직"
echo "✅ 4. 슬리피지/수수료/포지션 관리 백테스트 반영"
echo "✅ 5. 백테스트 결과 분석 및 리포팅 시스템"
echo "✅ 6. 테스트 인프라 구축"
echo ""
echo "원하는 작업을 선택하세요:"
main "$1"