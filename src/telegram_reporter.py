"""
Telegram Reporter Module - Bot v12.0
=====================================
Sends trade notifications and weekly performance reports
via Telegram Bot API.

v12.0 FIX: Removed broken imports from non-existent modules
(position_sizing, trade_executor, portfolio_tracker). [dry_run_manager.py deleted in v13.0 -- was broken, never integrated]
These modules were from an older architecture and don't exist in v12.0.
Now uses only the actual modules: calibration, bankroll_manager, config.

Two Report Types:
1. REAL-TIME Trade Notifications (entry, risk alerts)
2. WEEKLY Performance Reports (win rate, Brier, bankroll, Kelly status)

Telegram Bot Setup:
1. Create bot via @BotFather -> get TELEGRAM_TOKEN
2. Get chat ID via /getUpdates API -> TELEGRAM_CHAT_ID
3. Set as GitHub Secrets
"""

import time
from datetime import datetime, timezone
from typing import Optional

from src.config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DEFAULT_BANKROLL, DRY_RUN,
    FILE_PERF, FILE_BRIER, config_summary,
)
from src.utils import logger, send_telegram as _send_telegram


class TelegramReporter:
    """
    Telegram Bot reporter for trade notifications and weekly reports.
    Uses the shared send_telegram() from utils for actual message delivery.
    """

    def __init__(self):
        self._last_weekly_report: Optional[datetime] = None

    def notify_trade_entry(
        self,
        city: str,
        bracket_label: str,
        side: str,
        edge_pp: float,
        stake_usd: float,
        p_model: float,
        p_market: float,
        is_dry_run: bool = True,
        classification: str = "SIGNAL",
    ) -> None:
        """Send notification when a new trade is entered."""
        mode_label = "DRY_RUN" if is_dry_run else "LIVE"
        cls_emoji = "STRONG" if classification == "STRONG" else "SIGNAL"

        message = (
            f"{cls_emoji} {mode_label} TRADE ENTRY\n\n"
            f"City: {city}\n"
            f"Bracket: {bracket_label}\n"
            f"Side: {side}\n"
            f"Edge: {edge_pp:+.1f}pp\n"
            f"Stake: ${stake_usd:.2f}\n"
            f"P_model: {p_model:.3f} P_mkt: {p_market:.3f}\n"
        )

        _send_telegram(message)

    def notify_risk_alert(self, alert_type: str, message_text: str) -> None:
        """Send risk alert notification."""
        message = (
            f"RISK ALERT: {alert_type}\n\n"
            f"{message_text}\n"
        )
        _send_telegram(message)

    def notify_new_market(self, question: str, priority: float) -> None:
        """Send notification when a new weather market is discovered."""
        message = (
            f"NEW WEATHER MARKET\n\n"
            f"Market: {question[:150]}\n"
            f"Priority: {priority:.2f}\n"
            f"Bot is analyzing for entry signal...\n"
        )
        _send_telegram(message)

    def notify_error(self, error: str, context: str = "") -> None:
        """Send error notification."""
        message = (
            f"ERROR\n\n"
            f"Context: {context}\n"
            f"Error: {error[:200]}\n"
        )
        _send_telegram(message)

    def send_weekly_report(
        self,
        bankroll: float,
        drawdown: float,
        win_rate: float,
        brier_score: float,
        brier_ratio: float,
        n_resolved: int,
        kelly_mode: str,
    ) -> None:
        """Send comprehensive weekly performance report."""
        report = (
            f"WEEKLY REPORT\n\n"
            f"Bankroll: ${bankroll:.2f}\n"
            f"Drawdown: {drawdown:.1%}\n"
            f"Win Rate: {win_rate:.1%}\n"
            f"Brier Score: {brier_score:.4f}\n"
            f"BS Ratio: {brier_ratio:.2f}\n"
            f"Resolved: {n_resolved}\n"
            f"Kelly Mode: {kelly_mode}\n"
        )
        _send_telegram(report)
        self._last_weekly_report = datetime.now(timezone.utc)

    def notify_startup(self) -> None:
        """Send bot startup notification."""
        mode = "DRY_RUN (Paper Trading)" if DRY_RUN else "LIVE TRADING"
        message = (
            f"BOT STARTED\n\n"
            f"Mode: {mode}\n"
            f"Bankroll: ${DEFAULT_BANKROLL:.2f}\n"
            f"Strategy: Weather Markets Only\n\n"
            f"{config_summary()}\n"
        )
        _send_telegram(message)


# Global Telegram reporter instance
telegram_reporter = TelegramReporter()
