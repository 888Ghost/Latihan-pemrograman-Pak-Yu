"""
Utilities - Weather Bot v11.0
==============================
Helper functions for JSON I/O, HTTP requests, Telegram, and date parsing.

v11.0: No major changes from v10.0. FORECAST_MAE imports from lookup_tables
(as fixed in v10.x). All utility functions remain unchanged.
"""

import json
import logging
import os
import time
import re
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import requests

from src.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID


# ============================================================================
# Logging
# ============================================================================

def setup_logging(name: str = "weather_bot", level: str = "INFO") -> logging.Logger:
    """Configure structured logging with console + rotating file output."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    Path("logs").mkdir(exist_ok=True)
    fh = RotatingFileHandler("logs/bot.log", maxBytes=10*1024*1024, backupCount=5)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


logger = logging.getLogger("weather_bot")

# ============================================================================
# JSON I/O (Atomic writes)
# ============================================================================

def safe_write(path: str, data: Any) -> bool:
    """Atomic write JSON with backup."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        # Backup existing
        if os.path.exists(path):
            try:
                os.replace(path, path + ".backup")
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except (IOError, TypeError) as e:
        logger.error(f"safe_write failed for {path}: {e}")
        return False


def safe_read(path: str, default: Any = None) -> Any:
    """Read JSON with fallback to backup."""
    for p in [path, path + ".backup"]:
        try:
            if os.path.exists(p):
                with open(p, "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
    return default if default is not None else {}


# ============================================================================
# HTTP Client
# ============================================================================

def api_get(url: str, params: dict = None, retries: int = 3, timeout: int = 25) -> Optional[Any]:
    """GET request with retry and 429 backoff."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"Accept": "application/json"})
            if r.status_code == 429:
                wait = min(2 ** attempt * 2, 30)
                logger.warning(f"Rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                logger.error(f"API GET failed after {retries} attempts: {url} - {e}")
                return None
            time.sleep(1 * (attempt + 1))
    return None


# ============================================================================
# Telegram
# ============================================================================

def send_telegram(text: str) -> bool:
    """Send HTML-formatted message via Telegram Bot API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Chunk to 4000 chars
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False
        if i + 4000 < len(text):
            time.sleep(0.5)
    return True


# ============================================================================
# Date Parsing
# ============================================================================

def _parse_dt(raw: str) -> Optional[datetime]:
    """Robust datetime parser handling various ISO formats."""
    if not raw:
        return None
    try:
        from dateutil import parser as dp
        return dp.parse(raw).replace(tzinfo=timezone.utc) if not dp.parse(raw).tzinfo else dp.parse(raw)
    except Exception:
        return None


def _market_age_hours(ev: dict) -> float:
    """Hours since market creation."""
    created = _parse_dt(ev.get("createdAt", "") or ev.get("created_date", ""))
    if not created:
        return 999.0
    return max(0, (datetime.now(timezone.utc) - created).total_seconds() / 3600)


# ============================================================================
# Math Helpers
# ============================================================================

def _pf(v) -> float:
    """Safe float parse."""
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _mae(lead_h: float) -> float:
    """Interpolate MAE from FORECAST_MAE table by lead time."""
    from src.lookup_tables import FORECAST_MAE
    if lead_h <= 6:
        return FORECAST_MAE[6]
    if lead_h >= 120:
        return FORECAST_MAE[120]
    # Linear interpolation
    lower = max(k for k in FORECAST_MAE if k <= lead_h)
    upper = min(k for k in FORECAST_MAE if k >= lead_h)
    if lower == upper:
        return FORECAST_MAE[lower]
    frac = (lead_h - lower) / (upper - lower)
    return FORECAST_MAE[lower] + frac * (FORECAST_MAE[upper] - FORECAST_MAE[lower])


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
