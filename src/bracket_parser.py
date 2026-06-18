"""
Bracket Parser - Weather Bot v10.0
====================================
Parses multi-outcome bracket markets from Polymarket's weather events.

KEY INSIGHT (from FINAL_BREAKDOWN doc):
  v9.2 assumed binary markets with threshold in question text.
  v9.3+ uses groupItemTitle from API for structured bracket parsing.
  Each weather event has ~11 brackets, only ONE resolves YES (negRisk).
"""

import re
from typing import Optional
from src.utils import logger, _pf


def parse_group_item_title(title: str) -> Optional[tuple]:
    """
    Parse groupItemTitle into (low, high, unit) in Celsius.

    Handles 6 real Polymarket formats:
      1. "28 Celsius"           -> (27.5, 28.5, "C")
      2. "30 Celsius or higher" -> (29.5, float('inf'), "C")
      3. "20 Celsius or below"  -> (float('-inf'), 20.5, "C")
      4. "82-83F"               -> (27.78, 28.89, "C") [converted from F]
      5. "67F or below"         -> (-inf, 19.72, "C")
      6. "86F or higher"        -> (30.0, inf, "C")

    Returns:
        (low_celsius, high_celsius, unit) or None if parsing fails
    """
    if not title:
        return None

    t = title.strip()

    # Pattern 1: "28 Celsius" (single degree bracket) — FIX: allow negative temps
    m = re.match(r'^(-?\d+(?:\.\d+)?)\s*[°]?Celsius$', t, re.I)
    if m:
        val = float(m.group(1))
        return (val - 0.5, val + 0.5, "C")

    # Pattern 1b: "28-30 Celsius" (Celsius range) — NEW
    m = re.match(r'^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\s*[°]?Celsius$', t, re.I)
    if m:
        lo_val = float(m.group(1))
        hi_val = float(m.group(2))
        return (lo_val - 0.5, hi_val + 0.5, "C")

    # Pattern 2: "30 Celsius or higher" — FIX: allow negative
    m = re.match(r'^(-?\d+(?:\.\d+)?)\s*[°]?Celsius\s+or\s+higher$', t, re.I)
    if m:
        val = float(m.group(1))
        return (val - 0.5, float('inf'), "C")

    # Pattern 3: "20 Celsius or below" — FIX: allow negative
    m = re.match(r'^(-?\d+(?:\.\d+)?)\s*[°]?Celsius\s+or\s+below$', t, re.I)
    if m:
        val = float(m.group(1))
        return (float('-inf'), val + 0.5, "C")

    # Pattern 4: "82-83F" (Fahrenheit range)
    m = re.match(r'^(\d+)-(\d+)\s*[°]?F$', t, re.I)
    if m:
        lo_f, hi_f = float(m.group(1)), float(m.group(2))
        # Convert to Celsius with half-degree padding
        lo_c = (lo_f - 32) * 5 / 9 - 0.5 * 5 / 9
        hi_c = (hi_f - 32) * 5 / 9 + 0.5 * 5 / 9
        return (lo_c, hi_c, "C")

    # Pattern 5: "67F or below"
    m = re.match(r'^(\d+)\s*[°]?F\s+or\s+below$', t, re.I)
    if m:
        val_f = float(m.group(1))
        val_c = (val_f - 32) * 5 / 9
        return (float('-inf'), val_c + 0.5 * 5 / 9, "C")

    # Pattern 6: "86F or higher"
    m = re.match(r'^(\d+)\s*[°]?F\s+or\s+higher$', t, re.I)
    if m:
        val_f = float(m.group(1))
        val_c = (val_f - 32) * 5 / 9
        return (val_c - 0.5 * 5 / 9, float('inf'), "C")

    # Fallback: try to extract any number + unit
    m = re.match(r'^(\d+(?:\.\d+)?)\s*[°]?(C|F)$', t, re.I)
    if m:
        val = float(m.group(1))
        unit = m.group(2).upper()
        if unit == "F":
            val = (val - 32) * 5 / 9
        return (val - 0.5, val + 0.5, "C")

    logger.warning(f"Could not parse groupItemTitle: '{t}'")
    return None


def _bounds_question(q: str) -> Optional[tuple]:
    """
    Parse temperature bounds from question text (legacy fallback).

    Returns (low, high, temp_type, unit) or None.
    """
    if not q:
        return None

    q_lower = q.lower()

    # Determine temp type
    temp_type = "highest"
    if "lowest" in q_lower or "minimum" in q_lower:
        temp_type = "lowest"

    # Pattern: "28°C", "28 celsius", "82°F", "82 fahrenheit"
    # "or higher", "or above", "or more", "at least"
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*[°]?\s*(celsius|°c|c|fahrenheit|°f|f)'
        r'\s*(or\s+higher|or\s+above|or\s+more|at\s+least|or\s+lower|or\s+below|or\s+less|at\s+most)?',
        q_lower
    )
    if not m:
        return None

    val = float(m.group(1))
    unit_raw = m.group(2)
    tail = m.group(3)

    unit = "C"
    if unit_raw in ("fahrenheit", "°f", "f"):
        unit = "F"
        val = (val - 32) * 5 / 9  # Convert to Celsius

    if tail and ("higher" in tail or "above" in tail or "more" in tail or "least" in tail):
        if temp_type == "highest":
            return (val - 0.5, float('inf'), temp_type, unit)
        else:
            return (val - 0.5, float('inf'), temp_type, unit)
    elif tail and ("lower" in tail or "below" in tail or "less" in tail or "most" in tail):
        if temp_type == "highest":
            return (float('-inf'), val + 0.5, temp_type, unit)
        else:
            return (float('-inf'), val + 0.5, temp_type, unit)
    else:
        return (val - 0.5, val + 0.5, temp_type, unit)


def is_weather_market(market: dict) -> bool:
    """
    Detect weather temperature markets via multiple signals.

    Priority:
    1. feeType == "weather_fees" (most reliable)
    2. negRisk == True (weather markets use negRisk)
    3. Keyword matching on question/slug
    """
    # Check feeType (most reliable)
    fee = market.get("feeType", "").lower()
    if fee == "weather_fees":
        return True

    # Check question and slug for keywords
    q = (market.get("question", "") or "").lower()
    slug = (market.get("slug", "") or "").lower()
    desc = (market.get("description", "") or "").lower()

    from src.lookup_tables import WEATHER_KW
    text = f"{q} {slug} {desc}"
    return any(kw in text for kw in WEATHER_KW)


def extract_city(text: str) -> Optional[str]:
    """Extract city name from question/slug text."""
    from src.lookup_tables import CITY_COORDS
    text_lower = text.lower()
    # Longest match first
    sorted_cities = sorted(CITY_COORDS.keys(), key=len, reverse=True)
    for city in sorted_cities:
        if city in text_lower:
            return city
    return None


def extract_date(q: str) -> Optional[str]:
    """Extract target date from question text."""
    try:
        from dateutil import parser as dp
        # Try patterns like "June 13, 2026" or "2026-06-13"
        m = re.search(r'(?:on|by|for)\s+(.+?)(?:\?|$)', q, re.I)
        if m:
            date_str = m.group(1).strip().rstrip("?")
            parsed = dp.parse(date_str, fuzzy=True)
            return parsed.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None
