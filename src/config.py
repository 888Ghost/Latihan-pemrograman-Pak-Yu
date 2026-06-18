"""
Configuration - Polymarket Weather Bot v12.0
=============================================
CRITICAL FIXES from external audit (FATAL #2, #3):

v12.0 changes from v11.0:
  - REMOVED JAMES_STEIN_K (FATAL #2: James-Stein kills all early signals)
    Replaced with Beta Conjugate Prior: BETA_ALPHA, BETA_BETA, BETA_BLEND_K
    At n=0: NO shrinkage (trust the model, unlike James-Stein → 1/K ≈ 9%)
  - REMOVED hardcoded NEW_MARKET_MAX_AGE_H = 3.0
    Replaced with liquidity-adjusted adaptive cutoff (Manski 2006)
    Exotic/thin markets: max 8h; Normal: 3h; Liquid: 1.5h
  - Added DEFAULT_MAX_AGE_H as fallback when volume unknown
  - BANKROLL ValueError already fixed via _env_float() in v10.x

v11.0 changes from v10.x:
  - NEW_MARKET_MAX_AGE_H = 3.0 (only enter markets younger than 3h)
  - PRICE_VELOCITY_THRESHOLD = 0.05 (5pp/min = crowd already in)
  - Simplified AGE_THRESHOLDS: "new" (0-2h) and "fresh" (2-3h) only
  - Removed EXOTIC_KELLY_BONUS (was creating overbetting risk)
"""

import os
from dataclasses import dataclass, field


def _env_float(key: str, default: float) -> float:
    """Parse float from env, handling empty strings from GitHub Actions."""
    try:
        val = os.environ.get(key, "").strip()
        return float(val) if val else default
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Parse bool from env, handling empty strings."""
    val = os.environ.get(key, "").strip().lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    """Parse int from env, handling empty strings."""
    try:
        val = os.environ.get(key, "").strip()
        return int(val) if val else default
    except (ValueError, TypeError):
        return default


# ============================================================================
# API Endpoints
# ============================================================================

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
OWM_BASE = "https://api.open-meteo.com/v1/forecast"
ENS_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
METAR_BASE = "https://www.aviationweather.gov/adds/dataserver_current/httpparam"
POLY_RPC = "https://polygon-rpc.com"
POLY_RPC_BACKUP = "https://polygon-mainnet.g.alchemy.com/v2/demo"

# V2 CLOB Exchange contract (April 2026)
CLOB_V2_CONTRACT = "0xE111180000d2663C0091e4f400237545B87B996B"
PUSD_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLYGON_CHAIN_ID = 137

# ============================================================================
# Trading Parameters
# ============================================================================

DEFAULT_BANKROLL = _env_float("BANKROLL", 20.0)
DRY_RUN = _env_bool("DRY_RUN", True)
FAST_SCAN = _env_bool("FAST_SCAN", True)  # v13.0 Bug#11 fix

# Scan parameters
PAGES = 8
PAGE_LIMIT = 100
NEW_MARKET_WIN = 90       # minutes - "brand new" window
NEW_MARKET_FAST = 180     # minutes - re-evaluation window
COOLDOWN_H = 6            # hours between re-bets on same market

# === v12.0: Liquidity-Adjusted Max Age (REPLACES 3h hardcap) ===
# Bot only enters markets younger than this threshold.
# The actual threshold is DYNAMIC based on volume and market type:
#   Exotic cities: 8h (low competition, edge persists longer)
#   Thin markets (vol < $200): 4h (slower price discovery)
#   Normal markets (vol < $1000): 3h
#   Liquid markets (vol >= $1000): 1.5h (fast convergence)
# This value is used as DEFAULT when volume is unknown.
# Reference: Manski (2006) - convergence rate proportional to liquidity
DEFAULT_MAX_AGE_H = _env_float("DEFAULT_MAX_AGE_H", 3.0)

# === v12.0: Beta Conjugate Prior (REPLACES James-Stein) ===
# FATAL FIX #2: James-Stein shrinks ALL bracket probs to 1/K ≈ 9% at n=0,
# destroying all edge. Beta Conjugate Prior is the correct approach for
# Bernoulli-distributed prediction market outcomes.
#
# Prior: Beta(alpha=2, beta=8) centered at 0.20 ≈ typical weather P(win)
# In weather markets with ~11 brackets, the winning bracket probability
# averages around 0.15-0.25, so Beta(2,8) with mean 0.20 is a reasonable
# informative prior.
# Posterior mean: (alpha + n_wins) / (alpha + beta + n)
# Blend: lambda * p_model + (1-lambda) * p_posterior
# lambda = n / (n + k) where k = BETA_BLEND_K
# At n=0:  lambda=0 → p = p_model (NO shrinkage — trust the model)
# At n=10: lambda=0.33 → mild shrinkage toward empirical
# At n=50: lambda=0.71 → mostly model, some empirical
# Reference: Gelman et al. (2013) Bayesian Data Analysis, Ch.2
# Reference: Efron (2010) Large-Scale Inference, Ch.1
BETA_ALPHA = _env_float("BETA_ALPHA", 2.0)    # Prior pseudocount wins
BETA_BETA = _env_float("BETA_BETA", 8.0)      # Prior pseudocount losses
BETA_BLEND_K = _env_int("BETA_BLEND_K", 20)   # Blend factor (tunable)

# === Price Velocity Threshold (retained from v11.0) ===
PRICE_VELOCITY_THRESHOLD = _env_float("PRICE_VELOCITY_THRESHOLD", 0.05)
PRICE_VELOCITY_WINDOW_MIN = _env_int("PRICE_VELOCITY_WINDOW_MIN", 5)

# Volume & liquidity
MIN_VOLUME = 50.0         # USD - min volume for ESTABLISHED markets (age >= 2h)
MIN_VOLUME_NEW = 0.0      # USD - no volume limit for NEW markets
LIQUIDITY_MIN_DEPTH = 30.0     # USD - min orderbook depth for established
LIQUIDITY_MIN_DEPTH_NEW = 0.0  # USD - no depth for new markets
LIQUIDITY_CACHE_TTL = 300      # seconds

# Edge thresholds
EDGE_THRESHOLD = 8.0      # pp - default dynamic threshold
EDGE_STRONG = 20.0        # pp - threshold for MARKET orders (vs limit)
SPREAD_FLOOR = 2.0        # pp - spread adjustment floor

# Kelly Criterion
KELLY_FRACTION = 0.50     # Base half-Kelly
KELLY_MAX_PCT = 0.25      # Max 25% bankroll per single bet
KELLY_PORT_PCT = 0.40     # Max 40% bankroll across all positions
KELLY_F4_DRAWDOWN = 0.15  # 15% drawdown -> switch to f/4 Kelly
KELLY_F3_DRAWDOWN = 0.10  # 10% drawdown -> f/3 Kelly
KELLY_F2_DRAWDOWN = 0.05  # 5% drawdown -> 0.40f Kelly

# Risk gate
MAX_MKTS = 5              # Max concurrent markets
MAX_DAILY_BETS = 20
DAILY_USDC_CAP = 0.40     # fraction of bankroll
DAILY_LOSS_STOP = 0.15    # 15% drawdown -> hard stop

# v13.1: market order (FOK) slippage guard. Confirmed via official
# Polymarket docs that FOK/FAK are genuine market order types -- price
# field acts as worst-case execution ceiling, not literal fill price.
MAX_MARKET_PRICE = 0.28  # never market-buy above 28c -- breakeven WR too high
MIN_BET_USD = 1.00

# Model parameters
BOOTSTRAP_N = 1000
BOOTSTRAP_CONF = 0.90
DISPERSION_MAX_PP = 3.0   # max inter-model dispersion (deg C)

# Exotic city bonuses (v12.0: edge bonus only, NO kelly bonus)
# REMOVED: EXOTIC_KELLY_BONUS = 1.10 (was creating overbetting risk)
# Reference: Thorp (2006) - overbetting is worse than underbetting
EXOTIC_EDGE_BONUS_PP = 1.5

# v12.0: Age-based thresholds (with liquidity-adjusted max age)
# Only two categories: "new" (0-2h) and "fresh" (2-max_age) only.
# Markets older than max_age are skipped by the liquidity-adjusted filter.
AGE_THRESHOLDS = {
    "new":   {"max_age_h": 2, "threshold_pp": 4, "age_mult": 1.0},
    "fresh": {"max_age_h": 999, "threshold_pp": None, "age_mult": 0.90},
}

# Validation protocol
VALIDATION_DAYS_PHASE1 = 7
VALIDATION_DAYS_PHASE2 = 14

# ============================================================================
# Secrets (from GitHub Actions)
# ============================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()
POLY_FUNDER = os.environ.get("POLY_FUNDER", "").strip()
CLOB_API_KEY = os.environ.get("CLOB_API_KEY", "").strip()
CLOB_API_SECRET = os.environ.get("CLOB_API_SECRET", "").strip()
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "").strip()

# ============================================================================
# Required Secrets Check
# ============================================================================

REQUIRED_SECRETS = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "POLY_PRIVATE_KEY", "POLY_FUNDER"]


def validate_secrets() -> list[str]:
    """Return list of missing required secrets."""
    missing = []
    for name in REQUIRED_SECRETS:
        val = os.environ.get(name, "").strip()
        if not val:
            missing.append(name)
    return missing


# ============================================================================
# Persistence Files
# ============================================================================

FILE_SEEN = "data/pw_seen_markets.json"
FILE_BET = "data/pw_bet_markets.json"
FILE_BRIER = "data/pw_brier_scores.json"
FILE_PERF = "data/pw_performance.jsonl"
FILE_BANKROLL = "data/pw_bankroll.json"
FILE_PENDING = "data/pw_pending_orders.json"
FILE_BOT_STATE = "data/pw_bot_state.json"
FILE_CATEGORY = "data/pw_category_stats.json"
FILE_PRICE_HIST = "data/pw_price_history.json"
FILE_CALIBRATION = "data/pw_calibration.json"
FILE_LIQUIDITY = "data/pw_liquidity_cache.json"
FILE_PRICE_VELOCITY = "data/pw_price_velocity.json"


def config_summary() -> str:
    """Human-readable config summary."""
    mode = "DRY_RUN" if DRY_RUN else "LIVE"
    scan = "FAST (5min)" if FAST_SCAN else "FULL"
    return (
        f"Mode: {mode} | Scan: {scan}\n"
        f"Bankroll: ${DEFAULT_BANKROLL:.2f}\n"
        f"NEW-MARKET-ONLY: liquidity-adjusted max age (default {DEFAULT_MAX_AGE_H}h)\n"
        f"Price velocity threshold: {PRICE_VELOCITY_THRESHOLD}pp/min\n"
        f"Beta Conjugate: alpha={BETA_ALPHA}, beta={BETA_BETA}, k={BETA_BLEND_K}\n"
        f"Kelly: {KELLY_FRACTION:.0%} base, max {KELLY_MAX_PCT:.0%}/bet, {KELLY_PORT_PCT:.0%}/portfolio\n"
        f"Edge: {EDGE_THRESHOLD}pp default, {EDGE_STRONG}pp for market orders\n"
        f"Risk: {MAX_MKTS} max markets, {DAILY_LOSS_STOP:.0%} daily loss stop\n"
        f"Secrets: {len(validate_secrets())} missing"
    )
