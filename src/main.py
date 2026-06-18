"""
Main Orchestrator - Polymarket Weather Bot v12.0
==================================================
Single-pass execution designed for GitHub Actions cron.
Runs once per trigger, scans markets, analyzes, and places orders.

v12.0 CRITICAL FIXES from external audit:
  - FATAL #1: CLOB V2 side field fixed (uint8 for signing, string for POST)
  - FATAL #2: Beta Conjugate Prior REPLACES James-Stein shrinkage
  - FATAL #3: BANKROLL ValueError already fixed via _env_float()
  - Replaced arbitrary 70/30 CDF/MC blend with justified weights
  - Replaced 3h hardcap with liquidity-adjusted adaptive cutoff
  - Lock mechanism for concurrent GitHub Actions runs (BUG #13)
  - n_wins passed through to signal generator for Beta conjugate

v11.0 changes (carried forward):
  - NEW-MARKET-ONLY TRADING: bot only enters new markets
  - Price velocity check before placing orders
  - James-Stein shrinkage in Kelly calculation (now replaced by Beta conjugate)

v10.1 fixes (carried forward):
  - _check_resolutions() fully implemented (was stub)
  - Optional/Path imports fixed
  - daily_start_bankroll tracking added
  - Telegram command deduplication
  - Weekly report deduplication
  - spread_pp now fetched in DRY_RUN too
  - bracket_label stored in Brier predictions
  - Performance log now tracks resolution status

Cron timing (v12.0):
  Single workflow: Every 5 minutes (polymarket_bot.yml)
  Both modes now only scan new markets (liquidity-adjusted max age)
"""

import json
import os
import time
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, List

from src.config import (
    DRY_RUN, FAST_SCAN, POLY_FUNDER, POLY_PRIVATE_KEY,
    MAX_MKTS, COOLDOWN_H, MIN_VOLUME, MIN_VOLUME_NEW,
    DAILY_LOSS_STOP, MAX_DAILY_BETS, DAILY_USDC_CAP, MAX_MARKET_PRICE,
    validate_secrets, config_summary,
    FILE_BOT_STATE, FILE_PERF,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    GAMMA_BASE, DEFAULT_MAX_AGE_H,
)
from src.utils import (
    logger, setup_logging, safe_read, safe_write, send_telegram, _escape_html,
    _market_age_hours, api_get,
)
from src.lookup_tables import is_exotic
from src.market_discovery import (
    scan_new_markets_only, fetch_all_events, save_bet_market,
    WeatherEvent, WeatherBracket,
    get_velocity_skip_keys,
)
from src.weather_models import run_ensemble
from src.signal_generator import calc_brackets_full, adaptive_kelly_fraction, get_max_age_hours
from src.calibration import IsotonicCalibrator, BrierTracker, get_kelly_mode
from src.bankroll_manager import BankrollManager
from src.clob_client import ClobOrderClient


# ============================================================================
# Lock Mechanism (BUG #13 fix)
# ============================================================================

LOCK_FILE = "data/pw_bot.lock"
LOCK_TIMEOUT_SEC = 600  # 10 minutes


def _acquire_lock() -> bool:
    """Acquire lock file to prevent concurrent runs."""
    try:
        Path("data").mkdir(exist_ok=True)
        if os.path.exists(LOCK_FILE):
            lock_age = time.time() - os.path.getmtime(LOCK_FILE)
            if lock_age < LOCK_TIMEOUT_SEC:
                logger.info(f"Another run is in progress (lock age {lock_age:.0f}s). Skipping.")
                return False
            else:
                logger.warning(f"Stale lock found (age {lock_age:.0f}s). Removing.")
                os.remove(LOCK_FILE)
        Path(LOCK_FILE).touch()
        return True
    except Exception as e:
        logger.error(f"Lock acquisition failed: {e}")
        return True  # Continue anyway - lock is advisory


def _release_lock() -> None:
    """Release lock file."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


# ============================================================================
# Resolution Checker - THE CRITICAL SELF-LEARNING FEEDBACK LOOP
# ============================================================================

def _check_resolutions(brier: BrierTracker, calibrator: IsotonicCalibrator,
                       clob: ClobOrderClient) -> int:
    """
    Check for resolved markets and update Brier scores + calibration.

    This is the SELF-LEARNING feedback loop:
      Predict -> Record -> Resolve -> Measure -> Adjust -> Better predictions

    Without this, the bot CANNOT learn from its mistakes, CANNOT calculate
    win rate, CANNOT adjust Kelly, and CANNOT graduate from DRY_RUN.

    How it works:
    1. Get all unresolved predictions from BrierTracker
    2. For each unique slug, query Gamma API for resolution status
    3. Find the winning bracket (outcomePrices -> 1.0 for YES winner)
    4. For each prediction on that event:
       - Match prediction bracket to winning bracket
       - Determine actual outcome (1=won, 0=lost)
       - Call brier.resolve_prediction() to update Brier Score
       - Call calibrator.add_observation() for isotonic calibration
       - Update performance log with result
    5. Brier Score ratio -> Kelly threshold adjustment
    6. Win rate -> Adaptive Kelly fraction adjustment
    """
    unresolved = brier.get_unresolved()
    if not unresolved:
        return 0

    resolved_count = 0

    # Group by slug for batch processing
    slug_preds = {}
    for pred in unresolved:
        slug = pred.get("slug", "")
        if slug not in slug_preds:
            slug_preds[slug] = []
        slug_preds[slug].append(pred)

    for slug, preds in slug_preds.items():
        try:
            # Try event-level query (most reliable for resolution)
            market_list = None

            event_data = api_get(f"{GAMMA_BASE}/events", params={
                "slug": slug,
                "limit": 1,
            })
            if event_data and isinstance(event_data, list) and len(event_data) > 0:
                market_list = event_data[0].get("markets", [])
            elif event_data and isinstance(event_data, dict):
                market_list = event_data.get("markets", [])

            # Fallback: markets endpoint
            if not market_list:
                market_list = api_get(f"{GAMMA_BASE}/markets", params={
                    "slug": slug,
                    "limit": 20,
                })

            if not market_list:
                continue

            if not isinstance(market_list, list):
                market_list = []

            # Find resolved markets in this event
            resolved_brackets = {}
            for market in market_list:
                if not market.get("resolved", False):
                    continue

                group_item = market.get("groupItemTitle", "")
                outcome_prices = market.get("outcomePrices", "")

                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except (json.JSONDecodeError, TypeError):
                        outcome_prices = []

                if outcome_prices:
                    try:
                        yes_price = float(outcome_prices[0])
                        if yes_price > 1:
                            yes_price /= 100
                        resolved_brackets[group_item] = 1 if yes_price > 0.5 else 0
                    except (ValueError, IndexError):
                        continue

            if not resolved_brackets:
                continue

            # Resolve each prediction for this slug
            for pred in preds:
                bracket_label = pred.get("bracket_label", "")
                condition_id = pred.get("condition_id", "")
                side = pred.get("side", "")

                actual = None

                if bracket_label in resolved_brackets:
                    actual = resolved_brackets[bracket_label]
                else:
                    for market in market_list:
                        if market.get("conditionId") == condition_id and market.get("resolved"):
                            op = market.get("outcomePrices", "")
                            if isinstance(op, str):
                                try:
                                    op = json.loads(op)
                                except:
                                    op = []
                            if op:
                                try:
                                    yes_final = float(op[0])
                                    if yes_final > 1:
                                        yes_final /= 100
                                    actual = 1 if yes_final > 0.5 else 0
                                except (ValueError, IndexError):
                                    pass
                            break

                if actual is None:
                    continue

                p_model = pred.get("p_model", 0.5)

                # Resolve in BrierTracker
                bs = brier.resolve_prediction(pred, actual)
                if bs is not None:
                    calibrator.add_observation(p_model, actual)
                    resolved_count += 1

                    _update_performance_result(pred, actual, side)

                    logger.info(
                        f"RESOLVED: {slug} | bracket={bracket_label} | "
                        f"p_model={p_model:.3f} actual={actual} | BS={bs:.4f}"
                    )

        except Exception as e:
            logger.error(f"Resolution check failed for {slug}: {e}")
            continue

    if resolved_count > 0:
        logger.info(f"Resolved {resolved_count} predictions this run")
        brier._save()
        calibrator._save()

        _self_learning_feedback(brier, resolved_count)

    return resolved_count


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """
    Main entry point - single-pass execution for cron.

    v12.0: Lock mechanism to prevent concurrent runs (BUG #13).
    If two GitHub Actions runs overlap (e.g., 5-min cron while previous
    run is still going), they could both try to place orders on the same
    market simultaneously, leading to double-betting.
    """
    setup_logging()
    logger.info("=" * 60)
    logger.info("Polymarket Weather Bot v12.0 (NEW-MARKET-ONLY + BETA CONJUGATE)")
    logger.info(config_summary())
    logger.info("=" * 60)

    # Lock mechanism (BUG #13)
    if not _acquire_lock():
        return

    try:
        _run_bot()
    finally:
        _release_lock()


def _run_bot() -> None:
    """Core bot logic, separated from main() for lock management."""
    # Validate secrets
    missing = validate_secrets()
    if missing:
        logger.warning(f"Missing secrets: {missing}")

    # Initialize components
    calibrator = IsotonicCalibrator()
    brier = BrierTracker()
    bankroll_mgr = BankrollManager(POLY_FUNDER)
    clob = ClobOrderClient()
    bot_state = _load_bot_state()

    # v13.1 Bug#18 mitigation: fail-fast EIP-712 self-test.
    # Catches silent signing corruption (e.g. another Polymarket struct
    # migration) BEFORE attempting any live order, not after rejection.
    if not DRY_RUN:
        ok, msg = clob.self_test_signing()
        if not ok:
            logger.error(f"SIGNING SELF-TEST FAILED: {msg}")
            send_telegram(
                f"\U0001F6A8 <b>SIGNING SELF-TEST FAILED</b>\n{msg}\n"
                f"Live trading ABORTED this run. Bot will retry next cycle.\n"
                f"If this persists, Polymarket likely changed their EIP-712 "
                f"spec again -- check docs.polymarket.com before re-enabling."
            )
            return
        logger.info(f"Signing self-test: {msg}")

    # Update daily_start_bankroll at start of each day
    today_str = date.today().isoformat()

    if bot_state.get("daily_date") != today_str:
        current = bankroll_mgr.get_bankroll()
        bot_state["daily_date"] = today_str
        bot_state["daily_start_bankroll"] = current
        logger.info(f"New day: daily_start_bankroll = ${current:.2f}")
    _save_bot_state(bot_state)

    # Check Brier resolutions
    _check_resolutions(brier, calibrator, clob)

    # Determine Kelly mode
    drawdown = bankroll_mgr.get_drawdown()
    brier_ratio = brier.get_ratio()
    n_resolved = brier.n_resolved
    n_wins = brier.get_win_count()  # v12.0: for Beta conjugate
    kelly_frac, kelly_label = adaptive_kelly_fraction(drawdown, n_resolved, brier_ratio)

    logger.info(f"Kelly mode: {kelly_label} (frac={kelly_frac:.2f})")
    logger.info(f"Brier: n_resolved={n_resolved}, n_wins={n_wins}, ratio={brier_ratio:.2f}")

    # Update bot state
    bot_state["kelly_mode"] = kelly_label
    bot_state["kelly_fraction"] = kelly_frac
    _save_bot_state(bot_state)

    # Telegram command deduplication
    _check_commands(bot_state)

    # Risk gate pre-check
    if bot_state.get("paused"):
        logger.info("Bot is PAUSED (Telegram command). Skipping.")
        return

    # Daily loss check
    daily_start = bot_state.get("daily_start_bankroll", bankroll_mgr.get_bankroll())
    current_bankroll = bankroll_mgr.get_bankroll()
    if daily_start > 0 and (daily_start - current_bankroll) / daily_start >= DAILY_LOSS_STOP:
        logger.warning("Daily loss stop triggered. Skipping.")
        send_telegram("\u26a0\ufe0f <b>DAILY LOSS STOP</b>\nBot paused for the day.")
        return

    # Check pending orders
    clob.check_pending()

    # Scan for new markets
    if FAST_SCAN:
        events = scan_new_markets_only()
        logger.info(f"FAST_SCAN: {len(events)} new-market events")
    else:
        events = fetch_all_events()
        logger.info(f"FULL_SCAN: {len(events)} new-market events")

    # Process each event
    msgs = []
    orders_placed = 0
    bankroll = bankroll_mgr.get_bankroll()

    for event in events:
        if orders_placed >= MAX_MKTS:
            break

        # Bug#16 REAL fix: duplicate age check REMOVED from this loop.
        # The authoritative check now lives ONLY inside _analyze_and_bet()
        # below — this loop no longer pre-filters by age (was redundant
        # and produced confusing duplicate log lines for the same event).
        try:
            event_msgs, event_orders = _analyze_and_bet(
                event=event,
                bankroll=bankroll,
                kelly_frac=kelly_frac,
                calibrator=calibrator,
                brier=brier,
                clob=clob,
                is_dry_run=DRY_RUN,
                n_resolved=n_resolved,
                n_wins=n_wins,  # v12.0: for Beta conjugate
            )
            msgs.extend(event_msgs)
            orders_placed += event_orders
        except Exception as e:
            logger.error(f"Error processing {event.slug}: {e}", exc_info=True)
            msgs.append(f"\u274c Error: {event.slug}: {_escape_html(str(e)[:100])}")

    # Send Telegram summary
    if msgs:
        summary = "\n".join(msgs[:20])
        send_telegram(summary)

    # Save state
    _save_bot_state(bot_state)

    logger.info(f"Run complete: {orders_placed} orders, bankroll=${bankroll:.2f}")

    # Weekly report deduplication
    if datetime.now(timezone.utc).weekday() == 0:
        last_weekly = bot_state.get("last_weekly_date", "")
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if last_weekly != today_utc:
            _send_weekly_report(brier, bankroll_mgr, n_resolved, brier_ratio)
            bot_state["last_weekly_date"] = today_utc
            _save_bot_state(bot_state)


# ============================================================================
# Analysis & Betting Pipeline
# ============================================================================

def _analyze_and_bet(
    event: WeatherEvent,
    bankroll: float,
    kelly_frac: float,
    calibrator: IsotonicCalibrator,
    brier: BrierTracker,
    clob: ClobOrderClient,
    is_dry_run: bool = True,
    n_resolved: int = 0,
    n_wins: int = 0,  # v12.0: for Beta conjugate
) -> tuple:
    """
    Analyze a weather event and place bets on mispriced brackets.

    v12.0 changes:
    - Liquidity-adjusted max age (replaces 3h hardcap)
    - n_wins passed to signal generator for Beta conjugate shrinkage
    - volume_usd and is_exotic passed for liquidity-adjusted age
    """
    msgs = []
    orders = 0

    # v12.0: Liquidity-adjusted max age check
    max_age = get_max_age_hours(event.volume, event.is_exotic)
    if event.age_h > max_age:
        logger.info(f"Market expired: {event.slug} age={event.age_h:.1f}h > max={max_age:.1f}h")
        return msgs, orders

    # Volume filter (age-dependent)
    min_vol = MIN_VOLUME_NEW if event.age_h < 2 else MIN_VOLUME
    if event.volume < min_vol:
        return msgs, orders

    # Run weather ensemble
    ensemble = run_ensemble(event.city, event.target_date, event.temp_type)

    if ensemble.n_active == 0:
        msgs.append(f"\u23f8 {event.city}: No active models")
        return msgs, orders

    # Build bracket data for signal generation
    brackets = [(b.low, b.high, b.label) for b in event.brackets]
    yes_prices = [b.yes_price for b in event.brackets]

    # Price velocity skip keys
    velocity_skip = get_velocity_skip_keys(event)
    bracket_keys = [f"{event.slug}::{b.condition_id}" for b in event.brackets]

    # Generate signals (v12.0: pass n_wins, volume_usd, is_exotic)
    signals = calc_brackets_full(
        mu=ensemble.mu_ensemble,
        sigma=ensemble.sigma_ensemble,
        brackets=brackets,
        yes_prices=yes_prices,
        lead_h=_estimate_lead(event.target_date),
        age_h=event.age_h,
        bankroll=bankroll,
        kelly_fraction=kelly_frac,
        city=event.city,
        models=ensemble.models,
        brier_ratio=brier.get_ratio(),
        spread_pp=0.0,
        depth_usd=0.0,
        is_new_market=event.age_h < 2,
        is_dry_run=is_dry_run,
        calibration_fn=calibrator.calibrate,
        n_resolved=n_resolved,
        n_wins=n_wins,            # v12.0: for Beta conjugate
        velocity_skip_keys=velocity_skip,
        bracket_keys=bracket_keys,
        volume_usd=event.volume,   # v12.0: for liquidity-adjusted age
        is_exotic=event.is_exotic,  # v12.0: for liquidity-adjusted age
    )

    # Process signals
    for i, sig in enumerate(signals):
        if sig.classification not in ("STRONG", "SIGNAL"):
            continue

        if sig.velocity_skip:
            logger.info(
                f"Skipping {event.slug} bracket {sig.label}: "
                f"price velocity too high (crowd already in)"
            )
            continue

        bracket = event.brackets[i] if i < len(event.brackets) else None
        if not bracket:
            continue

        # Fetch spread even in DRY_RUN for realistic simulation
        if bracket.yes_token_id:
            book = clob.get_book(bracket.yes_token_id)
        else:
            book = None

        spread_pp = _calc_spread_pp(book)
        depth_usd = _calc_depth_usd(book)

        if spread_pp > 10:
            continue

        if depth_usd > 0 and not is_dry_run and depth_usd < 30.0 and event.age_h >= 2:
            continue
        # v13.0 Bug#13 FIX: removed DRY_RUN-more-restrictive depth check

        # Determine token and price
        if sig.side == "BUY_YES":
            token_id = bracket.yes_token_id
            limit_price = min(sig.p_model, 0.95)
        else:
            token_id = bracket.no_token_id
            limit_price = min(1 - sig.p_model, 0.95)

        # v13.1 Bug fix (Polymarket CLOB API confirmed via official docs):
        # FOK/FAK are GENUINE market order types -- not "aggressive GTC".
        # GTC/GTD: limit orders, rest on book at exact price.
        # FOK: must fill completely immediately or the whole order is killed.
        # FAK: fill whatever is available immediately, kill the remainder.
        # For market orders, "price" is a WORST-CASE slippage guard, not
        # the execution price -- the matching engine fills at the best
        # available price(s) up to that guard.
        wire_order_type = "GTC"
        if sig.order_type == "MARKET" and book:
            best_ask = _get_best_ask(book)
            if best_ask:
                # Slippage guard: willing to pay up to 5pp above best ask.
                # This is NOT the execution price -- FOK fills at actual
                # book price(s), this is just the worst-case ceiling.
                limit_price = min(best_ask + 0.05, limit_price, MAX_MARKET_PRICE)
            wire_order_type = "FOK"  # genuine market order
        elif book:
            best_ask = _get_best_ask(book)
            if best_ask:
                limit_price = min(best_ask + 0.02, limit_price)
            wire_order_type = "GTC"  # genuine resting limit order

        # Place order
        result = clob.place_order(
            token_id=token_id,
            side="BUY",
            price=limit_price,
            size_usd=sig.stake_usd,
            order_type=wire_order_type,
        )

        if result:
            orders += 1

            brier.record_prediction(
                slug=event.slug,
                p_model=sig.p_model,
                side=sig.side,
                bracket_label=bracket.label,
                condition_id=bracket.condition_id,
            )

            save_bet_market(event.slug, [sig.side], sig.edge_pp)

            mode_emoji = "\U0001f4cb" if is_dry_run else "\U0001f4b0"
            cls_emoji = "\U0001f525" if sig.classification == "STRONG" else "\U0001f4e1"
            msgs.append(
                f"{cls_emoji} {mode_emoji} {event.city} | "
                f"{bracket.label} | {sig.side} | "
                f"Edge={sig.edge_pp:+.1f}pp | "
                f"Stake=${sig.stake_usd:.2f} | "
                f"P_model={sig.p_model:.3f} P_mkt={sig.p_market:.3f}"
            )

            _log_performance(event, bracket, sig, ensemble, is_dry_run)

    # Summary message
    if ensemble.n_active > 0:
        msgs.append(
            f"\U0001f324 {event.city} ({event.temp_type}) | "
            f"\u03bc={ensemble.mu_ensemble:.1f}\u00b0C "
            f"\u03c3={ensemble.sigma_ensemble:.2f} | "
            f"Models={ensemble.n_active} | "
            f"METAR={'Y' if ensemble.metar_updated else 'N'}"
        )

    return msgs, orders


# ============================================================================
# Helper Functions
# ============================================================================

def _load_bot_state() -> dict:
    return safe_read(FILE_BOT_STATE, {
        "paused": False,
        "kelly_mode": "DRY_RUN",
        "kelly_fraction": 0.0,
        "daily_start_bankroll": 0,
        "daily_date": "",
        "last_weekly_date": "",
        "last_telegram_update_id": 0,
    })


def _save_bot_state(state: dict) -> None:
    safe_write(FILE_BOT_STATE, state)


def _check_commands(bot_state: dict) -> None:
    """Check Telegram for /pause, /resume, /emergency_stop, /status commands."""
    if not TELEGRAM_TOKEN:
        return

    last_update_id = bot_state.get("last_telegram_update_id", 0)

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        import requests
        r = requests.get(url, params={
            "timeout": 5,
            "offset": last_update_id + 1,
        }, timeout=10)
        data = r.json()

        max_update_id = last_update_id

        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            if update_id > max_update_id:
                max_update_id = update_id

            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()

            if text == "/pause":
                bot_state["paused"] = True
                send_telegram("\u23f8 Bot PAUSED")
            elif text == "/resume":
                bot_state["paused"] = False
                send_telegram("\u25b6 Bot RESUMED")
            elif text == "/emergency_stop":
                bot_state["paused"] = True
                send_telegram("\U0001f6d1 EMERGENCY STOP activated")
            elif text == "/status":
                bankroll = BankrollManager(POLY_FUNDER).get_bankroll()
                send_telegram(
                    f"\U0001f4ca Status: {'PAUSED' if bot_state.get('paused') else 'RUNNING'}\n"
                    f"Bankroll: ${bankroll:.2f}\n"
                    f"Kelly: {bot_state.get('kelly_mode', 'unknown')}"
                )

        if max_update_id > last_update_id:
            bot_state["last_telegram_update_id"] = max_update_id

    except Exception as e:
        logger.debug(f"Telegram command check failed: {e}")


def _estimate_lead(target_date: str) -> float:
    """Estimate lead time in hours from now to target date."""
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc, hour=12
        )
        lead = (target_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        return max(1, lead)
    except ValueError:
        return 24.0


def _calc_spread_pp(book: Optional[dict]) -> float:
    """Calculate bid-ask spread in percentage points."""
    if not book:
        return 0.0
    try:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            return (best_ask - best_bid) * 100
    except (ValueError, IndexError):
        pass
    return 0.0


def _calc_depth_usd(book: Optional[dict]) -> float:
    """Calculate order book depth in USD."""
    if not book:
        return 0.0
    try:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
        return min(bid_depth, ask_depth)
    except (ValueError, IndexError):
        return 0.0


def _get_best_ask(book: dict) -> Optional[float]:
    """Get best ask price from order book."""
    try:
        asks = book.get("asks", [])
        if asks:
            return float(asks[0].get("price", 0))
    except (ValueError, IndexError):
        pass
    return None


def _log_performance(event, bracket, sig, ensemble, is_dry_run) -> None:
    """Append performance log entry with all fields needed for resolution."""
    entry = {
        "timestamp": time.time(),
        "slug": event.slug,
        "city": event.city,
        "bracket": bracket.label,
        "condition_id": bracket.condition_id,
        "side": sig.side,
        "edge_pp": sig.edge_pp,
        "p_model": sig.p_model,
        "p_market": sig.p_market,
        "mu": ensemble.mu_ensemble,
        "sigma": ensemble.sigma_ensemble,
        "stake_usd": sig.stake_usd,
        "classification": sig.classification,
        "dry_run": is_dry_run,
        "result": None,
        "resolved": False,
        "age_h": event.age_h,
    }
    try:
        Path("data").mkdir(exist_ok=True)
        with open(FILE_PERF, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _update_performance_result(pred: dict, actual: int, side: str) -> None:
    """Update performance log with resolution result."""
    try:
        slug = pred.get("slug", "")
        bracket_label = pred.get("bracket_label", "")
        log_path = "data/pw_performance.jsonl"
        if not Path(log_path).exists():
            return

        entries = []
        with open(log_path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if (entry.get("slug") == slug and
                        entry.get("bracket") == bracket_label and
                        not entry.get("resolved", False)):
                        if side == "BUY_YES":
                            entry["result"] = "win" if actual == 1 else "loss"
                        else:
                            entry["result"] = "win" if actual == 0 else "loss"
                        entry["resolved"] = True
                        entry["resolved_at"] = time.time()
                    entries.append(entry)
                except (json.JSONDecodeError, KeyError):
                    continue

        with open(log_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    except Exception as e:
        logger.error(f"Failed to update performance result: {e}")


def _send_weekly_report(brier: BrierTracker, bankroll_mgr: BankrollManager,
                         n_resolved: int, brier_ratio: float) -> None:
    """Send weekly performance report via Telegram."""
    bankroll = bankroll_mgr.get_bankroll()
    drawdown = bankroll_mgr.get_drawdown()

    from src.calibration import _calc_win_rate
    win_rate = _calc_win_rate()

    report = (
        f"\U0001f4ca <b>WEEKLY REPORT</b>\n\n"
        f"<b>Bankroll:</b> ${bankroll:.2f}\n"
        f"<b>Drawdown:</b> {drawdown:.1%}\n"
        f"<b>Win Rate:</b> {win_rate:.1%}\n"
        f"<b>Brier Score:</b> {brier.cumulative_bs:.4f}\n"
        f"<b>BS Ratio:</b> {brier_ratio:.2f}\n"
        f"<b>Resolved:</b> {n_resolved}\n"
        f"<b>Kelly Mode:</b> {get_kelly_mode(n_resolved, brier_ratio, drawdown)[1]}\n"
    )
    send_telegram(report)


def _self_learning_feedback(brier: BrierTracker, resolved_count: int) -> None:
    """
    Close the self-learning feedback loop after resolutions are processed.

    Reference: Thorp (2006) - Kelly Criterion requires accurate probability
    estimates; systematic bias must be corrected via empirical feedback.
    """
    if brier.n_resolved < 3:
        return

    actual_wr = brier.get_win_rate()
    bs_ratio = brier.get_ratio()

    total_expected = 0.0
    n_for_wr = 0
    for r in brier.resolved:
        side = r.get("side", "")
        p_model = r.get("p_model", 0.5)
        if side == "BUY_YES":
            total_expected += p_model
        elif side == "BUY_NO":
            total_expected += (1 - p_model)
        n_for_wr += 1

    expected_wr = total_expected / n_for_wr if n_for_wr > 0 else 0.0

    logger.info(
        f"SELF-LEARNING: WR_actual={actual_wr:.3f} vs WR_expected={expected_wr:.3f} | "
        f"BS_ratio={bs_ratio:.3f} | N={brier.n_resolved}"
    )

    if n_for_wr >= 10:
        wr_gap = abs(actual_wr - expected_wr)

        if actual_wr < expected_wr - 0.10:
            logger.warning(
                f"MODEL OVERCONFIDENT: actual_wr={actual_wr:.3f} < expected_wr={expected_wr:.3f} "
                f"(gap={wr_gap:.3f}). Kelly will be reduced via BS ratio penalty."
            )
            send_telegram(
                f"\u26a0\ufe0f <b>Model Overconfidence Detected</b>\n"
                f"Actual WR: {actual_wr:.1%}\n"
                f"Expected WR: {expected_wr:.1%}\n"
                f"Gap: {wr_gap:.1%}\n"
                f"Kelly will be automatically reduced via BS ratio penalty."
            )
        elif actual_wr > expected_wr + 0.10:
            logger.info(
                f"MODEL UNDERCONFIDENT: actual_wr={actual_wr:.3f} > expected_wr={expected_wr:.3f}. "
                f"Model may be ready for higher Kelly."
            )
        else:
            logger.info(
                f"MODEL WELL-CALIBRATED: WR gap = {wr_gap:.3f} (< 10pp). "
                f"Self-learning loop healthy."
            )


if __name__ == "__main__":
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    main()
