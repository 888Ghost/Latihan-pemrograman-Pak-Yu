"""
Market Discovery & Scanner - Bot v12.0
========================================
Scans Polymarket for weather markets using feeType=weather_fees,
groups by negRiskMarketID for multi-outcome bracket events,
and prioritizes new markets for early entry.

v12.0 changes from v11.0:
  - Replaced NEW_MARKET_MAX_AGE_H hard 3h with liquidity-adjusted cutoff
    via signal_generator.get_max_age_hours() (Manski 2006)
  - DEFAULT_MAX_AGE_H used as fallback when volume unknown

v11.0 changes from v10.x:
  - NEW-MARKET-ONLY: Both scan modes now filter by max age
  - FIX BUG #11: bet_markets cooldown now uses COOLDOWN_H (6h), not hardcoded 72h
  - Added price velocity tracking
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from src.config import (
    GAMMA_BASE, PAGES, PAGE_LIMIT, NEW_MARKET_WIN, NEW_MARKET_FAST,
    MIN_VOLUME, MIN_VOLUME_NEW, FAST_SCAN, FILE_SEEN, FILE_BET,
    DEFAULT_MAX_AGE_H, COOLDOWN_H,
    FILE_PRICE_VELOCITY,
)
from src.signal_generator import get_max_age_hours
from src.bracket_parser import is_weather_market, extract_city, parse_group_item_title
from src.utils import logger, api_get, safe_read, safe_write, _parse_dt, _market_age_hours


@dataclass
class WeatherBracket:
    """A single temperature bracket within a weather event."""
    label: str
    low: float   # Celsius
    high: float  # Celsius
    unit: str    # "C"
    yes_price: float = 0.5
    yes_token_id: str = ""
    no_token_id: str = ""
    condition_id: str = ""
    group_item_threshold: int = 0
    volume: float = 0.0
    liquidity: float = 0.0


@dataclass
class WeatherEvent:
    """A weather event with multiple temperature brackets."""
    slug: str
    title: str
    city: str
    target_date: str
    temp_type: str  # "highest" or "lowest"
    neg_risk_id: str = ""
    brackets: list = field(default_factory=list)
    volume: float = 0.0
    age_h: float = 999.0
    is_exotic: bool = False


# ============================================================================
# Price Velocity Tracking (v11.0)
# ============================================================================

def load_price_velocity() -> dict:
    """Load price velocity history for all brackets.

    Returns:
        {bracket_key: {"price": float, "ts": float}}
        where bracket_key is typically "{slug}::{condition_id}"
    """
    return safe_read(FILE_PRICE_VELOCITY, {})


def save_price_velocity(data: dict) -> None:
    """Save price velocity history."""
    # Keep only last 1000 entries to prevent unbounded growth
    if len(data) > 1000:
        # Sort by timestamp, keep most recent
        sorted_items = sorted(data.items(), key=lambda x: x[1].get("ts", 0), reverse=True)
        data = dict(sorted_items[:1000])
    safe_write(FILE_PRICE_VELOCITY, data)


def update_price_velocity(event: WeatherEvent) -> None:
    """
    Record current prices for velocity tracking.

    Called after each scan to record the current prices of all brackets
    in all discovered events. This enables the price velocity check
    in signal_generator.py to detect when the crowd is already moving.
    """
    velocity_data = load_price_velocity()
    now = time.time()

    for bracket in event.brackets:
        key = f"{event.slug}::{bracket.condition_id}"
        velocity_data[key] = {
            "price": bracket.yes_price,
            "ts": now,
        }

    save_price_velocity(velocity_data)


def get_velocity_skip_keys(event: WeatherEvent, threshold: float = None,
                           window_min: int = None) -> set:
    """
    Get the set of bracket keys that should be skipped due to high price velocity.

    Args:
        event: The weather event to check
        threshold: Max allowed pp per minute (default: config PRICE_VELOCITY_THRESHOLD)
        window_min: Lookback window in minutes (default: config PRICE_VELOCITY_WINDOW_MIN)

    Returns:
        Set of bracket keys (format: "{slug}::{condition_id}") to skip
    """
    from src.config import PRICE_VELOCITY_THRESHOLD, PRICE_VELOCITY_WINDOW_MIN
    from src.signal_generator import check_price_velocity

    if threshold is None:
        threshold = PRICE_VELOCITY_THRESHOLD
    if window_min is None:
        window_min = PRICE_VELOCITY_WINDOW_MIN

    velocity_data = load_price_velocity()
    now = time.time()

    # Build current prices dict
    current_prices = {}
    for bracket in event.brackets:
        key = f"{event.slug}::{bracket.condition_id}"
        current_prices[key] = {"price": bracket.yes_price, "ts": now}

    # Check velocity
    skip_dict = check_price_velocity(current_prices, velocity_data, window_min, threshold)

    # Return only keys that should be skipped
    return {k for k, v in skip_dict.items() if v}


# ============================================================================
# Market Detection
# ============================================================================

def scan_new_markets_only() -> list[WeatherEvent]:
    """
    FAST_SCAN mode: fetch most recent markets, filter for weather and age.

    v12.0: Now uses liquidity-adjusted max age (Manski 2006) instead of
    hardcoded 3h. Exotic/thin markets have longer tradeable windows.

    Two windows within the new-market range:
    - Brand new (< 90 min)
    - Re-eval (< 180 min, not yet bet on)
    """
    events = []
    seen = _load_seen()
    bet_markets = _load_bet_markets()

    # Fetch recent markets ordered by creation date
    data = api_get(f"{GAMMA_BASE}/markets", params={
        "limit": PAGE_LIMIT,
        "active": "true",
        "order": "createdAt",
        "ascending": "false",
    })

    if not data:
        return events

    weather_markets = [m for m in data if is_weather_market(m)]
    logger.info(f"FAST_SCAN: {len(weather_markets)} weather markets in recent {PAGE_LIMIT}")

    # Group by event slug
    event_groups = _group_by_event(weather_markets)

    for slug, markets in event_groups.items():
        # Skip already bet on (within cooldown)
        if slug in bet_markets:
            continue

        # Build event first to get volume/is_exotic for age check
        event = _build_weather_event(slug, markets)
        if not event or not event.brackets:
            continue

        # v12.0: Liquidity-adjusted max age check
        max_age = get_max_age_hours(event.volume, event.is_exotic)
        if event.age_h > max_age:
            continue

        # Update price velocity tracking
        update_price_velocity(event)
        events.append(event)

    # Sort: exotic first, then newest first
    events.sort(key=lambda e: (not e.is_exotic, e.age_h))

    return events


def fetch_all_events() -> list[WeatherEvent]:
    """
    FULL scan: paginate through markets, filter weather and age.

    v12.0: Now uses liquidity-adjusted max age (Manski 2006) instead of
    hardcoded 3h. Exotic/thin markets have longer tradeable windows.
    """
    events = []
    all_weather = []
    bet_markets = _load_bet_markets()

    for page in range(PAGES):
        data = api_get(f"{GAMMA_BASE}/markets", params={
            "limit": PAGE_LIMIT,
            "offset": page * PAGE_LIMIT,
            "active": "true",
        })
        if not data:
            break
        weather = [m for m in data if is_weather_market(m)]
        all_weather.extend(weather)
        if len(data) < PAGE_LIMIT:
            break

    logger.info(f"FULL_SCAN: {len(all_weather)} weather markets total")

    # Group by event
    event_groups = _group_by_event(all_weather)

    for slug, markets in event_groups.items():
        if slug in bet_markets:
            continue

        # Build event first to get volume/is_exotic
        event = _build_weather_event(slug, markets)
        if not event or not event.brackets:
            continue

        # v12.0: Liquidity-adjusted max age check
        max_age = get_max_age_hours(event.volume, event.is_exotic)
        if event.age_h > max_age:
            continue

        update_price_velocity(event)
        events.append(event)

    events.sort(key=lambda e: (not e.is_exotic, e.age_h))
    return events


# ============================================================================
# Event Building
# ============================================================================

def _group_by_event(markets: list) -> dict:
    """Group markets by event slug (strip bracket suffixes)."""
    groups = {}
    for m in markets:
        slug = _extract_event_slug(m.get("slug", ""))
        if slug:
            groups.setdefault(slug, []).append(m)
    return groups


def _extract_event_slug(slug: str) -> str:
    """Strip bracket suffixes like -28c, -82-83f, -25corhigher."""
    # Remove trailing bracket patterns
    cleaned = re.sub(
        r'-\d+c(orhigher|orbelow)?$|'
        r'-\d+-\d+f$|'
        r'-\d+for(higher|below)$|'
        r'-\d+[\-]?\d*[fc].*$',
        '', slug, flags=re.I
    )
    return cleaned if cleaned else slug


def _build_weather_event(slug: str, markets: list) -> Optional[WeatherEvent]:
    """Build a WeatherEvent from grouped markets."""
    if not markets:
        return None

    first = markets[0]
    title = first.get("question", slug)
    city = extract_city(title) or extract_city(slug) or "unknown"

    # Determine temp type
    q_lower = (title + " " + slug).lower()
    temp_type = "lowest" if "lowest" in q_lower or "minimum" in q_lower else "highest"

    # Extract date
    from src.bracket_parser import extract_date
    target_date = extract_date(title) or extract_date(slug) or ""

    # Get negRisk group ID
    neg_risk_id = first.get("negRiskMarketID", "")

    # Build brackets
    brackets = []
    total_vol = 0.0
    for m in markets:
        bracket = _parse_market_to_bracket(m)
        if bracket:
            brackets.append(bracket)
            total_vol += bracket.volume

    if not brackets:
        return None

    # Sort brackets by threshold
    brackets.sort(key=lambda b: b.low if b.low != float('-inf') else -999)

    age_h = _market_age_hours(first)
    from src.lookup_tables import is_exotic as _is_exotic
    is_ex = _is_exotic(city)

    return WeatherEvent(
        slug=slug,
        title=title,
        city=city,
        target_date=target_date,
        temp_type=temp_type,
        neg_risk_id=neg_risk_id,
        brackets=brackets,
        volume=total_vol,
        age_h=age_h,
        is_exotic=is_ex,
    )


def _parse_market_to_bracket(m: dict) -> Optional[WeatherBracket]:
    """Parse a single market into a WeatherBracket."""
    # Try groupItemTitle first (v9.3+ approach)
    title = m.get("groupItemTitle", "")
    parsed = parse_group_item_title(title) if title else None

    if not parsed:
        # Fallback to question parsing
        from src.bracket_parser import _bounds_question
        q = m.get("question", "")
        result = _bounds_question(q)
        if result:
            low, high, temp_type, unit = result
            parsed = (low, high, unit)
        else:
            return None

    low, high, unit = parsed

    # Get prices
    outcomes = m.get("outcomes", "")
    if isinstance(outcomes, str):
        try:
            import json
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []

    outcome_prices = m.get("outcomePrices", "")
    if isinstance(outcome_prices, str):
        try:
            import json
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = []

    yes_price = 0.5
    try:
        if outcome_prices and len(outcome_prices) > 0:
            yes_price = float(outcome_prices[0]) / 100 if float(outcome_prices[0]) > 1 else float(outcome_prices[0])
    except (ValueError, IndexError):
        pass

    # Get token IDs
    clob_ids = m.get("clobTokenIds", "")
    if isinstance(clob_ids, str):
        try:
            import json
            clob_ids = json.loads(clob_ids)
        except Exception:
            clob_ids = clob_ids.split(",") if clob_ids else []

    yes_token = clob_ids[0] if clob_ids and len(clob_ids) > 0 else ""
    no_token = clob_ids[1] if clob_ids and len(clob_ids) > 1 else ""

    vol = float(m.get("volume24hr", 0) or 0)
    liq = float(m.get("liquidityNum", 0) or 0)
    threshold = int(m.get("groupItemThreshold", 0) or 0)

    return WeatherBracket(
        label=title or f"{low}-{high}",
        low=low,
        high=high,
        unit=unit,
        yes_price=max(0.001, min(0.999, yes_price)),
        yes_token_id=yes_token.strip(),
        no_token_id=no_token.strip(),
        condition_id=m.get("conditionId", ""),
        group_item_threshold=threshold,
        volume=vol,
        liquidity=liq,
    )


# ============================================================================
# State Persistence
# ============================================================================

def _load_seen() -> dict:
    """Load seen markets with 48h TTL."""
    data = safe_read(FILE_SEEN, {})
    now = time.time()
    # Clean expired entries (48h TTL)
    return {k: v for k, v in data.items() if now - v.get("ts", 0) < 172800}


def _save_seen(seen: dict) -> None:
    safe_write(FILE_SEEN, seen)


def _load_bet_markets() -> dict:
    """
    Load bet markets with COOLDOWN_H TTL.

    FIX BUG #11: Original code used hardcoded 72h (259200 seconds) TTL.
    This meant that once a bet was placed on a market, the bot would
    not reconsider that market for 72 hours - even though COOLDOWN_H
    was set to 6 hours. This prevented the bot from re-evaluating
    markets within the fresh window (2-3h).

    Fix: Use COOLDOWN_H * 3600 seconds as TTL instead of hardcoded 72h.
    """
    data = safe_read(FILE_BET, {})
    now = time.time()
    ttl_seconds = COOLDOWN_H * 3600  # FIX #11: was hardcoded 259200 (72h)
    return {k: v for k, v in data.items() if now - v.get("ts", 0) < ttl_seconds}


def save_bet_market(slug: str, sides: list, edge: float) -> None:
    """Record that a bet was placed."""
    data = _load_bet_markets()
    data[slug] = {"ts": time.time(), "sides": sides, "edge": edge}
    safe_write(FILE_BET, data)
