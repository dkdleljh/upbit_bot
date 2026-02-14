import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from storage import Storage
from utils import env_or_none, load_config

LOGGER = logging.getLogger(__name__)

class PerformanceMonitor:
    def __init__(self, cfg: dict, storage: Storage):
        self.cfg = cfg
        self.storage = storage
        self.report_interval = 60
        self.start_time = time.time()
        self.running = True
        
        self.api_calls = 0
        self.api_errors = 0
        self.successful_trades = 0
        self.response_times = []
        
    async def start_monitoring(self):
        LOGGER.info("실시 성능 모니터링 시작")
        
        while self.running:
            await asyncio.sleep(self.report_interval)
            await self._collect_metrics()
            await self._generate_report()
            
    async def stop_monitoring(self):
        LOGGER.info("성능 모니터링 중지")
        self.running = False
        
    async def _collect_metrics(self):
        elapsed = time.time() - self.start_time
        
        api_success_rate = 1.0 if self.api_calls > 0 else 0.0
        trade_success_rate = self.successful_trades / max(1, self.api_calls) if self.api_calls > 0 else 0.0
        avg_response_time = sum(self.response_times) / len(self.response_times) if self.response_times else 0.0
        
        metrics = {
            "timestamp": datetime.now().isoformat(),
            "elapsed_minutes": elapsed / 60,
            "api_calls": self.api_calls,
            "api_errors": self.api_errors,
            "api_success_rate": api_success_rate,
            "successful_trades": self.successful_trades,
            "trade_success_rate": trade_success_rate,
            "avg_response_time_ms": avg_response_time,
            "trades_per_minute": self.successful_trades / (elapsed / 60) if elapsed > 0 else 0,
        }
        
        LOGGER.info(f"성능 지표: {metrics}")
        return metrics
        
    async def _generate_report(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = Path(f"logs/performance_report_{timestamp}.json")
        
        metrics = await self._collect_metrics()
        
        import json
        with open(report_path, 'w') as f:
            json.dump({
                "report_generated_at": datetime.now().isoformat(),
                "monitoring_duration_minutes": (time.time() - self.start_time) / 60,
                "latest_metrics": metrics,
            }, f, indent=2)
        
        LOGGER.info(f"성능 보고서 생성: {report_path}")

async def main():
    cfg = load_config("config/config.yaml")
    storage = Storage("data/trading_bot.db")
    
    monitor = PerformanceMonitor(cfg, storage)
    
    try:
        await monitor.start_monitoring()
    except KeyboardInterrupt:
        LOGGER.info("성능 모니터링 종료")
        await monitor.stop_monitoring()

if __name__ == "__main__":
    asyncio.run(main())