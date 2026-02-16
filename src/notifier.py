"""
Telegram 알림 모듈
실시간 거래 상태, 포지션, 에러 등을 Telegram으로 알림
"""
import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

import aiohttp

LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    """Telegram Bot API를 통한 실시간 알림"""
    
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        
        #_rate limiting
        self._last_send_ms: int = 0
        self._min_interval_ms: int = 1000  # 1초 최소 간격
        
        if not self.enabled:
            LOGGER.info("Telegram 알림이 비활성화되었습니다 (TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정)")
        else:
            LOGGER.info(f"Telegram 알림 활성화됨 (chat_id: {self.chat_id[:4]}...)")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def send(
        self,
        message: str,
        parse_mode: str = "Markdown",
        disable_notification: bool = False,
    ) -> bool:
        """Telegram으로 메시지 전송"""
        if not self.enabled:
            return False
        
        from .utils import now_ms
        
        # Rate limiting
        now = now_ms()
        if now - self._last_send_ms < self._min_interval_ms:
            return False
        
        self._last_send_ms = now
        
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }
        
        try:
            session = await self._get_session()
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                else:
                    LOGGER.warning(f"Telegram发送失败: {resp.status}")
                    return False
        except Exception as e:
            LOGGER.warning(f"Telegram 알림 전송 실패: {e}")
            return False
    
    # ========================================
    # 거래 관련 알림
    # ========================================
    
    async def notify_entry(self, market: str, qty: float, price: float, mode: str) -> bool:
        """진입 알림"""
        emoji = "🟢"
        message = f"""
{emoji} *진입 완료*
━━━━━━━━━━━━━━━━━━━━━
• 마켓: `{market}`
• 수량: `{qty:.4f}`
• 가격: `₩{price:,.0f}`
• 모드: `{mode}`
"""
        return await self.send(message)
    
    async def notify_exit(
        self, 
        market: str, 
        qty: float, 
        price: float, 
        pnl_pct: float,
        reason: str,
        mode: str
    ) -> bool:
        """청산 알림"""
        emoji = "🔴" if pnl_pct < 0 else "🟢"
        sign = "+" if pnl_pct > 0 else ""
        message = f"""
{emoji} *청산 완료*
━━━━━━━━━━━━━━━━━━━━━
• 마켓: `{market}`
• 수량: `{qty:.4f}`
• 가격: `₩{price:,.0f}`
• 손익: `{sign}{pnl_pct:.2f}%`
• 사유: `{reason}`
• 모드: `{mode}`
"""
        return await self.send(message)
    
    async def notify_tp(self, market: str, pnl_pct: float, reason: str) -> bool:
        """익절 알림"""
        message = """
🎯 *익절 발생*
━━━━━━━━━━━━━━━━━━━━━
• 마켓: `{market}`
• 손익: `+{pnl_pct:.2f}%`
• 사유: `{reason}`
"""
        return await self.send(message.format(market=market, pnl_pct=pnl_pct, reason=reason))
    
    async def notify_sl(self, market: str, pnl_pct: float) -> bool:
        """손절 알림"""
        message = f"""
⚠️ *손절 발생*
━━━━━━━━━━━━━━━━━━━━━
• 마켓: `{market}`
• 손익: `{pnl_pct:.2f}%`
"""
        return await self.send(message)
    
    # ========================================
    # 시스템 알림
    # ========================================
    
    async def notify_startup(self, mode: str, initial_equity: float) -> bool:
        """시작 알림"""
        message = f"""
🚀 *업비트 봇 시작*
━━━━━━━━━━━━━━━━━━━━━
• 모드: `{mode}`
• 초기 자본: `₩{initial_equity:,.0f}`
• 시간: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`
"""
        return await self.send(message)
    
    async def notify_shutdown(self, reason: str = "사용자 요청") -> bool:
        """종료 알림"""
        message = f"""
🛑 *업비트 봇 종료*
━━━━━━━━━━━━━━━━━━━━━
• 사유: `{reason}`
• 시간: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`
"""
        return await self.send(message)
    
    async def notify_error(self, error_type: str, message: str, recovery: Optional[str] = None) -> bool:
        """에러 알림"""
        msg = f"""
❌ *에러 발생*
━━━━━━━━━━━━━━━━━━━━━
• 유형: `{error_type}`
• 내용: `{message}`
"""
        if recovery:
            msg += f"\n• 대응: `{recovery}`"
        
        return await self.send(msg)
    
    async def notify_safe_mode(self, reason: str, duration_minutes: int) -> bool:
        """세이프 모드 알림"""
        message = f"""
🛡️ *세이프 모드 활성화*
━━━━━━━━━━━━━━━━━━━━━
• 사유: `{reason}`
• 지속: `{duration_minutes}분`
"""
        return await self.send(message)
    
    async def notify_safe_mode_recovered(self) -> bool:
        """세이프 모드 해제 알림"""
        message = """
✅ *세이프 모드 해제*
━━━━━━━━━━━━━━━━━━━━━
• 정상 운영 재개
"""
        return await self.send(message)
    
    # ========================================
    # 상태 보고
    # ========================================
    
    async def notify_status(
        self,
        mode: str,
        equity: float,
        daily_pnl_pct: float,
        positions_count: int,
        active_trades: int,
    ) -> bool:
        """정기 상태 보고"""
        sign = "+" if daily_pnl_pct > 0 else ""
        message = f"""
📊 *상태 보고*
━━━━━━━━━━━━━━━━━━━━━
• 모드: `{mode}`
• 평가액: `₩{equity:,.0f}`
• 일일 손익: `{sign}{daily_pnl_pct:.2f}%`
• 포지션: `{positions_count}개`
• 진행중: `{active_trades}건`
"""
        return await self.send(message, disable_notification=True)
    
    async def notify_universe_change(self, top10: list[str]) -> bool:
        """유니버스 변경 알림"""
        markets_str = "\n".join([f"  {i+1}. {m}" for i, m in enumerate(top10[:5])])
        message = f"""
🔄 *유니버스 갱신*
━━━━━━━━━━━━━━━━━━━━━
Top 5:
{markets_str}
...
"""
        return await self.send(message, disable_notification=True)
    
    async def notify_circuit_breaker(self, reason: str, duration_minutes: int) -> bool:
        """서킷 브레이커 알림"""
        message = f"""
⛔ *서킷 브레이커*
━━━━━━━━━━━━━━━━━━━━━
• 사유: `{reason}`
• 중단: `{duration_minutes}분`
"""
        return await self.send(message)
    
    async def notify_order_failed(self, market: str, side: str, reason: str) -> bool:
        """주문 실패 알림"""
        message = f"""
❌ *주문 실패*
━━━━━━━━━━━━━━━━━━━━━
• 마켓: `{market}`
• 유형: `{side}`
• 사유: `{reason}`
"""
        return await self.send(message)
    
    async def notify_daily_summary(
        self,
        trades: int,
        win_rate: float,
        total_pnl_pct: float,
        max_drawdown: float,
        final_equity: float,
    ) -> bool:
        """일일 요약 보고"""
        sign = "+" if total_pnl_pct > 0 else ""
        message = f"""
📈 *일일 거래 요약*
━━━━━━━━━━━━━━━━━━━━━
• 거래 횟수: `{trades}회`
• 승률: `{win_rate:.1f}%`
• 순손익: `{sign}{total_pnl_pct:.2f}%`
• 최대 드로우다운: `{max_drawdown:.2f}%`
• 최종 평가액: `₩{final_equity:,.0f}`

⏰ {datetime.now().strftime('%Y-%m-%d')} 기준
"""
        return await self.send(message)


# ========================================
# 전역 인스턴스 (lazy initialization)
# ========================================

_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """전역 Telegram 알림 인스턴스 반환"""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


async def init_notifier(token: Optional[str] = None, chat_id: Optional[str] = None) -> TelegramNotifier:
    """Telegram 알림 모듈 초기화"""
    global _notifier
    _notifier = TelegramNotifier(token=token, chat_id=chat_id)
    return _notifier
