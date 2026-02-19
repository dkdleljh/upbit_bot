"""
External Notification System for Upbit Trading Bot.

Supports:
- Slack webhooks
- Telegram Bot API
- Email notifications
- Custom webhook endpoints

Designed for 24/7 monitoring without manual supervision.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

LOGGER = logging.getLogger(__name__)


class NotificationPriority(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class NotificationChannel(Enum):
    SLACK = "slack"
    TELEGRAM = "telegram"
    EMAIL = "email"
    WEBHOOK = "webhook"


@dataclass
class NotificationConfig:
    enabled: bool = True
    slack_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: str = ""
    custom_webhook_url: str = ""
    notify_on_trade: bool = True
    notify_on_stoploss: bool = True
    notify_on_error: bool = True
    notify_on_safe_mode: bool = True
    notify_on_circuit_breaker: bool = True
    notify_on_daily_summary: bool = True
    min_priority: NotificationPriority = NotificationPriority.WARNING


@dataclass
class NotificationMessage:
    priority: NotificationPriority
    title: str
    body: str
    fields: Dict[str, str] = None
    market: str = ""
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        if self.fields is None:
            self.fields = {}


class NotificationService:
    def __init__(self, config: NotificationConfig):
        self.config = config
        self._notification_history: List[NotificationMessage] = []
        self._cooldown_until: Dict[str, float] = {}
        self._cooldown_seconds = 300

    def should_notify(self, priority: NotificationPriority) -> bool:
        priority_order = [
            NotificationPriority.DEBUG,
            NotificationPriority.INFO,
            NotificationPriority.WARNING,
            NotificationPriority.ERROR,
            NotificationPriority.CRITICAL,
        ]

        min_idx = priority_order.index(self.config.min_priority)
        prio_idx = priority_order.index(priority)

        return prio_idx >= min_idx

    async def send(self, message: NotificationMessage) -> bool:
        if not self.config.enabled:
            return False

        if not self.should_notify(message.priority):
            return False

        cooldown_key = f"{message.priority.value}_{message.title}"
        now = asyncio.get_event_loop().time()
        if cooldown_key in self._cooldown_until:
            if now < self._cooldown_until[cooldown_key]:
                return False

        self._cooldown_until[cooldown_key] = now + self._cooldown_seconds

        self._notification_history.append(message)
        if len(self._notification_history) > 1000:
            self._notification_history = self._notification_history[-500:]

        success = True

        if self.config.slack_webhook_url:
            success = await self._send_slack(message) and success

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            success = await self._send_telegram(message) and success

        if self.config.email_smtp_host:
            success = await self._send_email(message) and success

        if self.config.custom_webhook_url:
            success = await self._send_webhook(message) and success

        return success

    async def _send_slack(self, message: NotificationMessage) -> bool:
        try:
            color = {
                NotificationPriority.DEBUG: "#95a5a6",
                NotificationPriority.INFO: "#3498db",
                NotificationPriority.WARNING: "#f39c12",
                NotificationPriority.ERROR: "#e74c3c",
                NotificationPriority.CRITICAL: "#8e44ad",
            }.get(message.priority, "#95a5a6")

            emoji = {
                NotificationPriority.DEBUG: ":information_source:",
                NotificationPriority.INFO: ":white_check_mark:",
                NotificationPriority.WARNING: ":warning:",
                NotificationPriority.ERROR: ":x:",
                NotificationPriority.CRITICAL: ":rotating_light:",
            }.get(message.priority, ":bell:")

            payload = {
                "attachments": [
                    {
                        "color": color,
                        "title": f"{emoji} {message.title}",
                        "text": message.body,
                        "fields": [
                            {"title": k, "value": str(v), "short": True}
                            for k, v in (message.fields or {}).items()
                        ],
                        "footer": "upbit_bot",
                        "ts": int(message.timestamp.timestamp()),
                    }
                ]
            }

            if message.market:
                payload["text"] = f"Market: {message.market}"

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: urlopen(
                    Request(
                        self.config.slack_webhook_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=10,
                ),
            )

            LOGGER.info(f"Slack notification sent: {message.title}")
            return True

        except (URLError, HTTPError) as e:
            LOGGER.error(f"Failed to send Slack notification: {e}")
            return False
        except Exception as e:
            LOGGER.error(f"Unexpected error sending Slack notification: {e}")
            return False

    async def _send_telegram(self, message: NotificationMessage) -> bool:
        try:
            emoji = {
                NotificationPriority.DEBUG: "ℹ️",
                NotificationPriority.INFO: "✅",
                NotificationPriority.WARNING: "⚠️",
                NotificationPriority.ERROR: "❌",
                NotificationPriority.CRITICAL: "🚨",
            }.get(message.priority, "🔔")

            text = f"{emoji} *{message.title}*\n\n{message.body}"

            if message.fields:
                text += "\n\n"
                for k, v in message.fields.items():
                    text += f"*{k}:* `{v}`\n"

            text += f"\n_{message.timestamp.strftime('%Y-%m-%d %H:%M:%S')}_"

            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: urlopen(
                    Request(
                        f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=10,
                ),
            )

            LOGGER.info(f"Telegram notification sent: {message.title}")
            return True

        except (URLError, HTTPError) as e:
            LOGGER.error(f"Failed to send Telegram notification: {e}")
            return False
        except Exception as e:
            LOGGER.error(f"Unexpected error sending Telegram notification: {e}")
            return False

    async def _send_email(self, message: NotificationMessage) -> bool:
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            msg = MIMEMultipart()
            msg["From"] = self.config.email_from
            msg["To"] = self.config.email_to
            msg["Subject"] = (
                f"[upbit_bot] {message.priority.value.upper()}: {message.title}"
            )

            body = f"{message.body}\n\n"
            if message.fields:
                for k, v in message.fields.items():
                    body += f"{k}: {v}\n"
            body += f"\nTimestamp: {message.timestamp.isoformat()}"

            msg.attach(MIMEText(body, "plain"))

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: (
                    smtplib.SMTP(
                        self.config.email_smtp_host, self.config.email_smtp_port
                    )
                    .starttls()
                    .login(self.config.email_username, self.config.email_password)
                    .send_message(msg)
                ),
            )

            LOGGER.info(f"Email notification sent: {message.title}")
            return True

        except Exception as e:
            LOGGER.error(f"Failed to send email notification: {e}")
            return False

    async def _send_webhook(self, message: NotificationMessage) -> bool:
        try:
            payload = {
                "timestamp": message.timestamp.isoformat(),
                "priority": message.priority.value,
                "title": message.title,
                "body": message.body,
                "market": message.market,
                "fields": message.fields or {},
            }

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: urlopen(
                    Request(
                        self.config.custom_webhook_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=10,
                ),
            )

            LOGGER.info(f"Custom webhook notification sent: {message.title}")
            return True

        except (URLError, HTTPError) as e:
            LOGGER.error(f"Failed to send webhook notification: {e}")
            return False
        except Exception as e:
            LOGGER.error(f"Unexpected error sending webhook notification: {e}")
            return False

    def notify_trade(
        self,
        market: str,
        side: str,
        qty: float,
        price: float,
        pnl: float = 0,
    ) -> bool:
        if not self.config.notify_on_trade:
            return False

        priority = NotificationPriority.INFO
        title = f"Trade Executed: {side} {market}"
        body = f"Successfully executed {side} order for {market}"

        fields = {
            "Side": side,
            "Qty": f"{qty:.8f}",
            "Price": f"₩{price:,.0f}",
        }

        if pnl != 0:
            fields["PnL"] = f"₩{pnl:,.0f}"
            priority = (
                NotificationPriority.WARNING if pnl < 0 else NotificationPriority.INFO
            )

        return asyncio.create_task(
            self.send(
                NotificationMessage(
                    priority=priority,
                    title=title,
                    body=body,
                    fields=fields,
                    market=market,
                )
            )
        )

    def notify_stoploss(
        self,
        market: str,
        qty: float,
        entry_price: float,
        exit_price: float,
        pnl: float,
    ) -> bool:
        if not self.config.notify_on_stoploss:
            return False

        return asyncio.create_task(
            self.send(
                NotificationMessage(
                    priority=NotificationPriority.ERROR,
                    title=f"Stop Loss: {market}",
                    body=f"Stop loss triggered for {market}",
                    fields={
                        "Qty": f"{qty:.8f}",
                        "Entry": f"₩{entry_price:,.0f}",
                        "Exit": f"₩{exit_price:,.0f}",
                        "PnL": f"₩{pnl:,.0f}",
                    },
                    market=market,
                )
            )
        )

    def notify_error(
        self,
        error_type: str,
        message: str,
        details: str = "",
    ) -> bool:
        if not self.config.notify_on_error:
            return False

        return asyncio.create_task(
            self.send(
                NotificationMessage(
                    priority=NotificationPriority.ERROR,
                    title=f"Error: {error_type}",
                    body=message,
                    fields={"Details": details} if details else {},
                )
            )
        )

    def notify_safe_mode(
        self,
        reason: str,
        error_count: int = 0,
    ) -> bool:
        if not self.config.notify_on_safe_mode:
            return False

        return asyncio.create_task(
            self.send(
                NotificationMessage(
                    priority=NotificationPriority.CRITICAL,
                    title="Safe Mode Activated",
                    body=f"Trading bot entered safe mode: {reason}",
                    fields={
                        "Reason": reason,
                        "Error Count": str(error_count),
                    },
                )
            )
        )

    def notify_circuit_breaker(
        self,
        breaker_type: str,
        count: int,
        pause_minutes: int,
    ) -> bool:
        if not self.config.notify_on_circuit_breaker:
            return False

        return asyncio.create_task(
            self.send(
                NotificationMessage(
                    priority=NotificationPriority.WARNING,
                    title=f"Circuit Breaker: {breaker_type}",
                    body=f"Circuit breaker activated due to {count} events",
                    fields={
                        "Type": breaker_type,
                        "Count": str(count),
                        "Pause": f"{pause_minutes} minutes",
                    },
                )
            )
        )

    def notify_daily_summary(
        self,
        total_trades: int,
        wins: int,
        losses: int,
        total_pnl: float,
        current_equity: float,
        max_drawdown: float,
    ) -> bool:
        if not self.config.notify_on_daily_summary:
            return False

        win_rate = wins / total_trades * 100 if total_trades > 0 else 0

        return asyncio.create_task(
            self.send(
                NotificationMessage(
                    priority=NotificationPriority.INFO,
                    title="Daily Summary",
                    body=f"Trading performance for today",
                    fields={
                        "Total Trades": str(total_trades),
                        "Wins": str(wins),
                        "Losses": str(losses),
                        "Win Rate": f"{win_rate:.1f}%",
                        "Total PnL": f"₩{total_pnl:,.0f}",
                        "Equity": f"₩{current_equity:,.0f}",
                        "Max DD": f"{max_drawdown:.2f}%",
                    },
                )
            )
        )

    def get_notification_history(
        self,
        priority: Optional[NotificationPriority] = None,
        limit: int = 100,
    ) -> List[NotificationMessage]:
        history = self._notification_history

        if priority:
            history = [m for m in history if m.priority == priority]

        return history[-limit:]


def create_notification_service_from_config(cfg: dict) -> NotificationService:
    notification_cfg = cfg.get("notifications", {})

    config = NotificationConfig(
        enabled=notification_cfg.get("enabled", True),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        email_smtp_host=os.getenv("EMAIL_SMTP_HOST", ""),
        email_smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
        email_username=os.getenv("EMAIL_USERNAME", ""),
        email_password=os.getenv("EMAIL_PASSWORD", ""),
        email_from=os.getenv("EMAIL_FROM", ""),
        email_to=os.getenv("EMAIL_TO", ""),
        custom_webhook_url=os.getenv("CUSTOM_WEBHOOK_URL", ""),
        notify_on_trade=notification_cfg.get("notify_on_trade", True),
        notify_on_stoploss=notification_cfg.get("notify_on_stoploss", True),
        notify_on_error=notification_cfg.get("notify_on_error", True),
        notify_on_safe_mode=notification_cfg.get("notify_on_safe_mode", True),
        notify_on_circuit_breaker=notification_cfg.get(
            "notify_on_circuit_breaker", True
        ),
        notify_on_daily_summary=notification_cfg.get("notify_on_daily_summary", True),
    )

    priority_str = notification_cfg.get("min_priority", "warning").upper()
    config.min_priority = NotificationPriority[priority_str]

    return NotificationService(config)
