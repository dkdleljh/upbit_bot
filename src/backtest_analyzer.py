"""
Enhanced backtest analysis and reporting system.
Provides detailed analytics and visualization for backtest results.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from .backtest_engine_simple import BacktestResult
from .storage import Storage

LOGGER = logging.getLogger(__name__)


class BacktestAnalyzer:
    """Analyze backtest results with detailed metrics."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
    
    def analyze_results(self, result: BacktestResult) -> Dict:
        """Perform comprehensive analysis of backtest results."""
        analysis = {
            'basic_metrics': self._calculate_basic_metrics(result),
            'risk_metrics': self._calculate_risk_metrics(result),
            'performance_metrics': self._calculate_performance_metrics(result),
            'market_analysis': self._analyze_markets(result),
            'time_analysis': self._analyze_time_patterns(result),
            'recommendations': self._generate_recommendations(result)
        }
        
        return analysis
    
    def _calculate_basic_metrics(self, result: BacktestResult) -> Dict:
        """Calculate basic trading metrics."""
        return {
            'total_trades': result.total_trades,
            'win_rate': result.win_rate,
            'loss_rate': 1 - result.win_rate,
            'net_pnl_pct': result.net_pnl_pct,
            'net_pnl_amount': result.net_pnl_pct * 10000000,  # Assuming 10M initial equity
            'avg_hold_seconds': result.avg_hold_seconds,
            'avg_hold_minutes': result.avg_hold_seconds / 60,
            'sharpe_ratio': result.sharpe_ratio,
            'profit_factor': result.profit_factor,
            'max_drawdown_pct': result.max_drawdown_pct
        }
    
    def _calculate_risk_metrics(self, result: BacktestResult) -> Dict:
        """Calculate risk-adjusted metrics."""
        if result.daily_returns:
            returns = pd.Series(result.daily_returns)
            
            # Calculate additional risk metrics
            volatility = returns.std()
            downside_volatility = returns[returns < 0].std()
            var_95 = returns.quantile(0.05)
            
            # Sortino ratio (downside deviation)
            excess_returns = returns - 0.02 / 252  # Assuming 2% risk-free rate
            sortino_ratio = excess_returns.mean() / downside_volatility if downside_volatility > 0 else 0
            
            # Maximum consecutive losses
            returns_binary = (returns > 0).astype(int)
            consecutive_losses = 0
            max_consecutive_losses = 0
            
            for ret in returns_binary:
                if ret == 0:
                    consecutive_losses += 1
                    max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
                else:
                    consecutive_losses = 0
            
            return {
                'volatility': float(volatility),
                'downside_volatility': float(downside_volatility),
                'var_95': float(var_95),
                'sortino_ratio': sortino_ratio,
                'max_consecutive_losses': max_consecutive_losses,
                'calmar_ratio': result.net_pnl_pct / max(result.max_drawdown_pct, 0.001)
            }
        
        return {}
    
    def _calculate_performance_metrics(self, result: BacktestResult) -> Dict:
        """Calculate detailed performance metrics."""
        if result.total_trades == 0:
            return {}
        
        # Win/Loss analysis
        win_rate = result.win_rate
        loss_rate = 1 - win_rate
        
        # Average win/loss amounts (would need trade-level data)
        avg_win_amount = 0.02  # Placeholder
        avg_loss_amount = -0.015  # Placeholder
        
        # Expectancy
        expectancy = (win_rate * avg_win_amount) + (loss_rate * avg_loss_amount)
        
        # Monthly and annual returns
        monthly_return = result.net_pnl_pct * 30 / 365 if result.net_pnl_pct else 0
        annual_return = result.net_pnl_pct
        
        return {
            'expectancy': expectancy,
            'monthly_return_pct': monthly_return,
            'annual_return_pct': annual_return,
            'avg_win_amount': avg_win_amount,
            'avg_loss_amount': avg_loss_amount,
            'largest_win': 0.05,  # Placeholder
            'largest_loss': -0.03,  # Placeholder
            'recovery_factor': abs(annual_return / result.max_drawdown_pct) if result.max_drawdown_pct > 0 else 0
        }
    
    def _analyze_markets(self, result: BacktestResult) -> Dict:
        """Analyze performance by market."""
        market_analysis = {}
        
        for market, stats in result.per_market_stats.items():
            market_analysis[market] = {
                'trades': stats['trades'],
                'win_rate': stats['win_rate'],
                'pnl_pct': stats['pnl'],
                'contribution': stats['trades'] / max(1, result.total_trades),
                'performance_grade': self._grade_performance(stats['pnl'], stats['win_rate'])
            }
        
        # Sort markets by performance
        sorted_markets = sorted(market_analysis.items(), 
                              key=lambda x: x[1]['pnl'], 
                              reverse=True)
        
        return {
            'by_market': market_analysis,
            'best_market': sorted_markets[0] if sorted_markets else None,
            'worst_market': sorted_markets[-1] if sorted_markets else None,
            'market_diversification': len(market_analysis)
        }
    
    def _analyze_time_patterns(self, result: BacktestResult) -> Dict:
        """Analyze time-based patterns (placeholder)."""
        return {
            'avg_daily_trades': result.total_trades / 30,  # Assuming 30-day backtest
            'peak_hour': None,  # Would need timestamp data
            'trough_hour': None,
            'weekday_vs_weekend': None,
            'volatility_periods': None
        }
    
    def _grade_performance(self, pnl_pct: float, win_rate: float) -> str:
        """Grade performance based on P&L and win rate."""
        if pnl_pct > 0.1 and win_rate > 0.6:
            return "A+"
        elif pnl_pct > 0.05 and win_rate > 0.55:
            return "A"
        elif pnl_pct > 0.02 and win_rate > 0.5:
            return "B+"
        elif pnl_pct > 0 and win_rate > 0.45:
            return "B"
        elif pnl_pct > -0.02 and win_rate > 0.4:
            return "C+"
        elif pnl_pct > -0.05 and win_rate > 0.35:
            return "C"
        else:
            return "D"
    
    def _generate_recommendations(self, result: BacktestResult) -> List[str]:
        """Generate actionable recommendations based on results."""
        recommendations = []
        
        # Based on win rate
        if result.win_rate < 0.4:
            recommendations.append("낮은 승률: 진입 조건을 더 보수적으로 조정하세요")
        elif result.win_rate < 0.5:
            recommendations.append("승률 개선 필요: BTC 레짐 필터를 강화하세요")
        
        # Based on drawdown
        if result.max_drawdown_pct > 0.15:
            recommendations.append("높은 최대 손실: 손절 레벨을 더 보수적으로 설정하세요")
        elif result.max_drawdown_pct > 0.1:
            recommendations.append("손실 관리 개선: 포지션 크기를 줄이세요")
        
        # Based on hold time
        if result.avg_hold_seconds < 300:  # Less than 5 minutes
            recommendations.append("과도한 단타: 최소 보유 시간을 늘리세요")
        elif result.avg_hold_seconds > 1200:  # More than 20 minutes
            recommendations.append("과도한 장기 보유: 익절 조건을 더 빠르게 설정하세요")
        
        # Based on Sharpe ratio
        if result.sharpe_ratio < 0.5:
            recommendations.append("낮은 샤프 비율: 리스크 조정 수준을 재검토하세요")
        elif result.sharpe_ratio > 2.0:
            recommendations.append("매우 높은 샤프 비율: 백테스트 과정을 확인하세요")
        
        # Based on profit factor
        if result.profit_factor < 1.0:
            recommendations.append("손익 비율 불리: 수수료/슬리피지를 최적화하세요")
        
        # Market-specific recommendations
        if result.per_market_stats:
            best_market = max(result.per_market_stats.items(), key=lambda x: x[1]['pnl'])
            worst_market = min(result.per_market_stats.items(), key=lambda x: x[1]['pnl'])
            
            if best_market[1]['pnl'] > 0.05:
                recommendations.append(f"우수 성과 마켓: {best_market[0]} - 이 마켓에 집중하세요")
            
            if worst_market[1]['pnl'] < -0.03:
                recommendations.append(f"부진 마켓: {worst_market[0]} - 이 마켓을 제외하세요")
        
        return recommendations


class BacktestReporter:
    """Generate comprehensive backtest reports."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
        self.analyzer = BacktestAnalyzer(storage)
    
    def generate_report(self, result: BacktestResult, output_path: str = None) -> str:
        """Generate comprehensive backtest report."""
        analysis = self.analyzer.analyze_results(result)
        
        report = self._build_report(result, analysis)
        
        if output_path:
            self._save_report(report, output_path)
        
        return report
    
    def _build_report(self, result: BacktestResult, analysis: Dict) -> str:
        """Build formatted report string."""
        report_lines = []
        
        # Header
        report_lines.append("=" * 60)
        report_lines.append("업비트 멀티마켓 스캘핑 봇 백테스트 결과 보고서")
        report_lines.append("=" * 60)
        report_lines.append(f"생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append("")
        
        # Basic Metrics
        report_lines.append("📊 기본 성과 지표")
        report_lines.append("-" * 30)
        basic = analysis['basic_metrics']
        report_lines.append(f"총 거래 횟수: {basic['total_trades']:,}회")
        report_lines.append(f"승률: {basic['win_rate']:.1%}")
        report_lines.append(f"손실률: {basic['loss_rate']:.1%}")
        report_lines.append(f"순수익률: {basic['net_pnl_pct']:.2%}")
        report_lines.append(f"순수익 금액: {basic['net_pnl_amount']:,.0f} KRW")
        report_lines.append(f"평균 보유 시간: {basic['avg_hold_minutes']:.1f}분")
        report_lines.append(f"샤프 비율: {basic['sharpe_ratio']:.2f}")
        report_lines.append(f"수익 팩터: {basic['profit_factor']:.2f}")
        report_lines.append(f"최대 손실률: {basic['max_drawdown_pct']:.2%}")
        report_lines.append("")
        
        # Risk Metrics
        risk = analysis.get('risk_metrics') or {}
        if risk:
            report_lines.append("⚠️ 리스크 분석")
            report_lines.append("-" * 30)
            report_lines.append(f"변동성: {risk['volatility']:.3f}")
            report_lines.append(f"하방향 변동성: {risk['downside_volatility']:.3f}")
            report_lines.append(f"VaR 95%: {risk['var_95']:.3f}")
            report_lines.append(f"소르티노 비율: {risk['sortino_ratio']:.2f}")
            report_lines.append(f"최대 연속 손실: {risk['max_consecutive_losses']}회")
            report_lines.append(f"칼마 비율: {risk['calmar_ratio']:.2f}")
            report_lines.append("")
        
        # Performance Metrics
        if analysis['performance_metrics']:
            report_lines.append("🎯 상세 성과 분석")
            report_lines.append("-" * 30)
            perf = analysis['performance_metrics']
            report_lines.append(f"기대값: {perf['expectancy']:.4f}")
            report_lines.append(f"월간 수익률: {perf['monthly_return_pct']:.2%}")
            report_lines.append(f"연간 수익률: {perf['annual_return_pct']:.2%}")
            report_lines.append(f"평균 수익: {perf['avg_win_amount']:.2%}")
            report_lines.append(f"평균 손실: {perf['avg_loss_amount']:.2%}")
            report_lines.append(f"최대 수익: {perf['largest_win']:.2%}")
            report_lines.append(f"최대 손실: {perf['largest_loss']:.2%}")
            report_lines.append(f"회복 팩터: {perf['recovery_factor']:.2f}")
            report_lines.append("")
        
        # Market Analysis
        if analysis['market_analysis']:
            report_lines.append("🏪 마켓별 성과")
            report_lines.append("-" * 30)
            market = analysis['market_analysis']
            
            # Top 5 markets
            sorted_markets = sorted(market['by_market'].items(), 
                                  key=lambda x: x[1]['pnl'], 
                                  reverse=True)
            
            report_lines.append("상위 5개 마켓:")
            for i, (market_name, stats) in enumerate(sorted_markets[:5], 1):
                grade = stats['performance_grade']
                report_lines.append(f"{i}. {market_name}: {stats['pnl']:.2%} ({stats['trades']}회, {stats['win_rate']:.1%}, {grade})")
            
            if market['best_market']:
                best = market['best_market']
                report_lines.append(f"최고 성과: {best[0]} ({best[1]['performance_grade']})")
            
            if market['worst_market']:
                worst = market['worst_market']
                report_lines.append(f"최저 성과: {worst[0]} ({worst[1]['performance_grade']})")
            
            report_lines.append(f"마켓 다변화: {market['market_diversification']}개")
            report_lines.append("")
        
        # Recommendations
        if analysis['recommendations']:
            report_lines.append("💡 개선 제안")
            report_lines.append("-" * 30)
            for i, rec in enumerate(analysis['recommendations'], 1):
                report_lines.append(f"{i}. {rec}")
            report_lines.append("")
        
        # Summary
        report_lines.append("📋 요약")
        report_lines.append("-" * 30)
        
        # Performance grading
        overall_grade = self._calculate_overall_grade(basic, risk)
        report_lines.append(f"종합 등급: {overall_grade}")
        
        # Status
        status = self._determine_status(basic)
        report_lines.append(f"전략 상태: {status}")
        
        report_lines.append("")
        report_lines.append("=" * 60)
        
        return "\n".join(report_lines)
    
    def _calculate_overall_grade(self, basic: Dict, risk: Dict) -> str:
        """Calculate overall performance grade."""
        score = 0
        
        # P&L component (40 points)
        pnl = basic['net_pnl_pct']
        if pnl > 0.1:
            score += 40
        elif pnl > 0.05:
            score += 30
        elif pnl > 0:
            score += 20
        elif pnl > -0.02:
            score += 10
        
        # Sharpe ratio component (30 points)
        sharpe = basic['sharpe_ratio']
        if sharpe > 2.0:
            score += 30
        elif sharpe > 1.5:
            score += 25
        elif sharpe > 1.0:
            score += 20
        elif sharpe > 0.5:
            score += 10
        
        # Drawdown component (20 points)
        drawdown = basic['max_drawdown_pct']
        if drawdown < 0.05:
            score += 20
        elif drawdown < 0.1:
            score += 15
        elif drawdown < 0.15:
            score += 10
        elif drawdown < 0.2:
            score += 5
        
        # Win rate component (10 points)
        win_rate = basic['win_rate']
        if win_rate > 0.6:
            score += 10
        elif win_rate > 0.5:
            score += 7
        elif win_rate > 0.4:
            score += 4
        elif win_rate > 0.3:
            score += 1
        
        # Convert to grade
        if score >= 90:
            return "A+"
        elif score >= 80:
            return "A"
        elif score >= 70:
            return "B+"
        elif score >= 60:
            return "B"
        elif score >= 50:
            return "C+"
        elif score >= 40:
            return "C"
        else:
            return "D"
    
    def _determine_status(self, basic: Dict) -> str:
        """Determine overall strategy status."""
        pnl = basic['net_pnl_pct']
        win_rate = basic['win_rate']
        
        if pnl > 0.05 and win_rate > 0.55:
            return "우수 (추천)"
        elif pnl > 0 and win_rate > 0.45:
            return "양호"
        elif pnl > -0.02 and win_rate > 0.4:
            return "보통 (개선 필요)"
        elif pnl > -0.05:
            return "부진 (전략 재검토 필요)"
        else:
            return "심각 (전략 중단 권장)"
    
    def _save_report(self, report: str, output_path: str):
        """Save report to file."""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(report)
            LOGGER.info(f"백테스트 보고서 저장: {output_path}")
        except Exception as e:
            LOGGER.error(f"보고서 저장 실패: {e}")
    
    def export_detailed_csv(self, result: BacktestResult, trades_data: List[Dict], output_path: str):
        """Export detailed trade data to CSV."""
        try:
            if trades_data:
                df = pd.DataFrame(trades_data)
                df.to_csv(output_path, index=False)
                LOGGER.info(f"상세 거래 데이터 CSV 저장: {output_path}")
        except Exception as e:
            LOGGER.error(f"CSV 저장 실패: {e}")
    
    def create_summary_dashboard_data(self, result: BacktestResult, analysis: Dict) -> Dict:
        """Create data structure for dashboard visualization."""
        return {
            'overview': analysis['basic_metrics'],
            'risk_metrics': analysis['risk_metrics'],
            'performance': analysis['performance_metrics'],
            'markets': analysis['market_analysis'],
            'recommendations': analysis['recommendations'],
            'chart_data': {
                'equity_curve': result.equity_curve,
                'returns': result.daily_returns,
                'monthly_pnl': self._calculate_monthly_pnl(result)
            }
        }
    
    def _calculate_monthly_pnl(self, result: BacktestResult) -> List[Dict]:
        """Calculate monthly P&L (placeholder)."""
        # Simplified monthly breakdown
        months = ['1월', '2월', '3월', '4월', '5월', '6월']
        monthly_data = []
        
        cumulative_pnl = 0
        monthly_pnl = result.net_pnl_pct / len(months)
        
        for month in months:
            cumulative_pnl += monthly_pnl
            monthly_data.append({
                'month': month,
                'pnl_pct': monthly_pnl,
                'cumulative_pct': cumulative_pnl
            })
        
        return monthly_data