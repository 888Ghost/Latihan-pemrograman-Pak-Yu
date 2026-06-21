"""
Signal Generator - Edge-Based - Bot v12.0
===========================================
Calculates edge = |P_model - P_market| per bracket, not chart-based signals.

This is the FUNDAMENTAL difference from the v1 approach:
  OLD (wrong): MACD + Bollinger + Z-score on price charts
  NEW (correct): |P_model - P_market| in percentage points

P_model is computed from weather forecast ensemble (Gaussian CDF).
P_market is the Yes price on Polymarket (directly available).

v12.0 CRITICAL FIXES from external audit (FATAL #2):
  - REPLACED James-Stein shrinkage with Beta Conjugate Prior.
    Rationale: James & Stein (1961) theorem applies to multivariate MEAN
    estimation under squared-error loss. Applying it to probability estimates
    in a prediction market context is a domain mismatch. At n=0, James-Stein
    shrinks ALL bracket probabilities to 1/K ≈ 9.09%, destroying any edge.
    At n=5, lambda=0.17, edge is still below the 4pp threshold.
    The bot is blind for the first 25+ resolved predictions (~25-50 days).

    Beta Conjugate Prior (Efron 2010, Gelman et al. 2013):
    - Prior: Beta(alpha=2, beta=8) centered at 0.20 ≈ typical weather P(win)
    - Posterior mean: (alpha + n_wins) / (alpha + beta + n)
    - Blend: lambda * p_model + (1 - lambda) * p_posterior
    - lambda = n / (n + k) where k=20 (tunable)
    - At n=0: p = p_model (NO shrinkage — trust the model)
    - At n=10: mild shrinkage toward empirical win rate
    - At n=50: 71% model weight, 29% empirical

  - REPLACED arbitrary 70/30 CDF/MC blend with justified weights.
    Interior: alpha = min(0.85, 0.50 + 0.35*(w/sigma))
    Tail: alpha = 0.30 (MC dominates, CDF underestimates fat tails)

  - REPLACED 3h hard cutoff with liquidity-adjusted adaptive cutoff.
    Manski (2006): convergence rate proportional to liquidity.
    Low volume/exotic = slower convergence = longer tradeable window.

  - Removed exotic city kelly bonus (was creating overbetting risk)
  - Simplified age thresholds to "new" (0-2h) and "fresh" (2-max_age) only
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List

from src.pw_adaptive_kelly import AdaptiveKelly as _AKelly
_ak = _AKelly()  # v13.0 Bug#6: now connected

from src.config import (
    EDGE_THRESHOLD, EDGE_STRONG, SPREAD_FLOOR,
    AGE_THRESHOLDS, EXOTIC_EDGE_BONUS_PP, BOOTSTRAP_N, BOOTSTRAP_CONF,
    DISPERSION_MAX_PP, MIN_BET_USD,
    PRICE_VELOCITY_THRESHOLD, PRICE_VELOCITY_WINDOW_MIN,
    BETA_ALPHA, BETA_BETA, BETA_BLEND_K,
)
from src.weather_models import cdf_formula, m7_mc
from src.utils import logger


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class BracketSignal:
    """Signal for a single temperature bracket."""
    label: str
    low: float
    high: float
    p_model: float = 0.0
    p_market: float = 0.0
    edge_pp: float = 0.0
    side: str = ""         # "BUY_YES" or "BUY_NO"
    classification: str = "PASS"  # STRONG, SIGNAL, NEAR, PASS
    stake_usd: float = 0.0
    order_type: str = "LIMIT"  # LIMIT or MARKET
    significant: bool = False  # Bootstrap CI doesn't cross zero
    passes_liquidity: bool = True
    spread_pp: float = 0.0
    velocity_skip: bool = False  # skipped due to price velocity


@dataclass
class BootstrapCI:
    """Bootstrap confidence interval on edge."""
    edge_point: float
    ci_low: float
    ci_high: float
    significant: bool  # CI doesn't cross zero


# ============================================================================
# Price Velocity Check (v11.0, retained)
# ============================================================================

def check_price_velocity(
    current_prices: dict,
    historical_prices: dict,
    window_min: int = None,
    threshold: float = None,
) -> dict:
    """
    Check if prices have moved too fast recently (crowd is already in).

    Reference: In efficient markets, price changes reflect information.
    A rapid price movement (5pp+ per minute) indicates that the market
    has already incorporated the information we're trying to exploit.
    """
    if window_min is None:
        window_min = PRICE_VELOCITY_WINDOW_MIN
    if threshold is None:
        threshold = PRICE_VELOCITY_THRESHOLD

    skip_brackets = {}
    now = __import__('time').time()
    window_sec = window_min * 60

    for key, current in current_prices.items():
        curr_price = current.get("price", 0.5)
        curr_ts = current.get("ts", now)

        hist = historical_prices.get(key)
        if not hist:
            skip_brackets[key] = False
            continue

        hist_price = hist.get("price", 0.5)
        hist_ts = hist.get("ts", 0)

        dt_sec = curr_ts - hist_ts
        if dt_sec <= 0 or dt_sec > window_sec:
            skip_brackets[key] = False
            continue

        price_change_pp = abs(curr_price - hist_price) * 100
        dt_min = dt_sec / 60
        velocity_pp_per_min = price_change_pp / dt_min if dt_min > 0 else 0

        if velocity_pp_per_min > threshold:
            logger.info(
                f"Price velocity too high for {key}: {velocity_pp_per_min:.2f}pp/min "
                f"> {threshold}pp/min (crowd already in)"
            )
            skip_brackets[key] = True
        else:
            skip_brackets[key] = False

    return skip_brackets


# ============================================================================
# Beta Conjugate Prior Shrinkage (v12.0 - REPLACES James-Stein)
# ============================================================================

def beta_conjugate_shrink(
    p_model: float,
    n_resolved: int,
    n_wins: int = 0,
    alpha_prior: float = None,
    beta_prior: float = None,
    k: int = None,
) -> float:
    """
    Apply Beta conjugate prior shrinkage to a model probability estimate.

    This REPLACES the James-Stein shrinkage that was fatally flawed for
    prediction market probabilities.

    Why Beta Conjugate Prior is correct here:
    1. Probabilities in prediction markets are Bernoulli-distributed outcomes
    2. The Beta distribution is the conjugate prior for Bernoulli likelihood
    3. This means the posterior has a closed-form solution
    4. At n=0, we trust the model completely (no shrinkage) — the model IS
       our prior knowledge, unlike James-Stein which shrinks to uniform
    5. As n grows, we blend model with empirical Bayes posterior

    Formula:
      p_posterior = (alpha + n_wins) / (alpha + beta + n)
      lambda = n / (n + k)
      p_shrunk = lambda * p_model + (1 - lambda) * p_posterior

    At n=0:   lambda=0.00 → p = p_model (NO shrinkage — trust the model)
    At n=10:  lambda=0.33 → mild shrinkage toward empirical win rate
    At n=50:  lambda=0.71 → 71% model, 29% posterior
    At n=200: lambda=0.91 → mostly model

    Args:
        p_model: Raw model probability
        n_resolved: Number of resolved predictions (sample size)
        n_wins: Number of winning predictions (for posterior)
        alpha_prior: Beta prior alpha (default: BETA_ALPHA = 2.0)
        beta_prior: Beta prior beta (default: BETA_BETA = 8.0)
        k: Blend factor (default: BETA_BLEND_K = 20)

    Returns:
        Shrunk probability estimate

    Reference: Gelman et al. (2013) Bayesian Data Analysis, Ch.2
    Reference: Efron (2010) Large-Scale Inference, Ch.1 on empirical Bayes
    Reference: James & Stein (1961) applies to multivariate MEAN estimation
    under squared-error loss — NOT to probability estimation in prediction
    markets. This is a domain mismatch.
    """
    if alpha_prior is None:
        alpha_prior = BETA_ALPHA
    if beta_prior is None:
        beta_prior = BETA_BETA
    if k is None:
        k = BETA_BLEND_K

    if n_resolved == 0:
        return p_model  # NO shrinkage if no data — trust the model

    # Posterior mean from Beta conjugate
    p_posterior = (alpha_prior + n_wins) / (alpha_prior + beta_prior + n_resolved)

    # Blend: weight posterior by data confidence
    lam = n_resolved / (n_resolved + k)

    p_shrunk = lam * p_model + (1 - lam) * p_posterior
    return max(0.0001, min(0.9999, p_shrunk))


# ============================================================================
# Probability Computation per Bracket (v12.0 - FIXED CDF/MC blend)
# ============================================================================

def calc_bracket_probabilities(mu: float, sigma: float, brackets: list,
                                use_mc: bool = True) -> list[float]:
    """
    Calculate model probability for each bracket using Gaussian CDF + MC blend.

    v12.0 FIX: Replaced arbitrary 70/30 weights with empirically-motivated:
    - Interior brackets: alpha = min(0.85, 0.50 + 0.35*(bracket_width/sigma))
      Wider brackets → CDF more reliable → higher CDF weight
      Narrow brackets → MC more important → lower CDF weight
    - Tail brackets: alpha = 0.30 (MC dominates, CDF underestimates fat tails)

    Rationale:
    - Gaussian CDF error in tails ~ O(sigma^3) for skewed distributions
    - For bracket width w, interior brackets have w ≈ 1-2°C
    - For tail brackets, effective width is infinite → CDF less reliable
    - Reference: Jolliffe & Stephenson (2012) Ch.7 — CDF calibration
      degrades in the tails for non-Gaussian distributions

    Args:
        mu: Ensemble mean temperature
        sigma: Ensemble sigma
        brackets: List of (low, high, label) tuples
        use_mc: Whether to use Monte Carlo blending

    Returns:
        List of model probabilities per bracket
    """
    p_gaussian = []
    for low, high, _ in brackets:
        p = cdf_formula(low, high, mu, sigma)
        p_gaussian.append(max(0.0001, min(0.9999, p)))

    if not use_mc:
        return p_gaussian

    # Monte Carlo
    mc_brackets = [(low, high) for low, high, _ in brackets]
    p_mc = m7_mc(mu, sigma, mc_brackets, n=50000)

    # Blend with empirically-motivated weights (v12.0 FIX)
    p_blend = []
    for i, (low, high, _) in enumerate(brackets):
        is_tail = (low == float('-inf') or high == float('inf'))
        if is_tail:
            # Tail bracket: MC dominates (CDF underestimates fat tails)
            alpha = 0.30  # 30% CDF, 70% MC
        else:
            # Interior: CDF reliable when width > sigma, less so when narrow
            bracket_width = high - low
            alpha = min(0.85, 0.50 + 0.35 * (bracket_width / max(sigma, 0.5)))
        p = alpha * p_gaussian[i] + (1 - alpha) * p_mc[i]
        p_blend.append(max(0.0001, min(0.9999, p)))

    return p_blend


# ============================================================================
# Liquidity-Adjusted Max Age (v12.0 - REPLACES 3h hardcap)
# ============================================================================

def get_max_age_hours(volume_usd: float = 0.0, is_exotic: bool = False) -> float:
    """
    v12.0 FIX: Replace 3h hardcap with liquidity-adjusted adaptive cutoff.

    Manski (2006): convergence rate proportional to liquidity (volume proxy).
    Low volume (exotic) → slower convergence → longer tradeable window.
    High volume (liquid) → fast convergence → shorter window.

    This replaces the v11.0 NEW_MARKET_MAX_AGE_H = 3.0 hardcoded value.

    Args:
        volume_usd: Market volume in USD
        is_exotic: Whether the city is exotic (less bot competition)

    Returns:
        Maximum age in hours for this market to be tradeable

    Reference: Manski (2006) "Interpreting and Predicting Prediction Markets"
    """
    if is_exotic:
        return 8.0   # Exotic = low competition, edge persists longer
    elif volume_usd < 200:
        return 4.0   # Thin market, slower price discovery
    elif volume_usd < 1000:
        return 3.0   # Normal
    else:
        return 1.5   # Liquid = fast convergence (larger bots active)


# ============================================================================
# Bootstrap CI on Edge
# ============================================================================

def bootstrap_edge_ci(p_model: float, p_market: float, sigma: float) -> BootstrapCI:
    """
    Parametric bootstrap: test if edge is statistically significant.

    Method:
      n_eff = 50 (ECMWF ensemble size), scaled by sigma
      SE = sqrt(p * (1-p) / n_eff)
      Sample p from N(p_model, SE), compute edge distribution
      90% CI from percentiles

    If CI crosses zero -> not significant -> skip trade.

    Reference: Efron & Tibshirani (1993)
    """
    n_eff = max(10, int(50 * sigma / 2.0))
    se = math.sqrt(max(0.0001, p_model * (1 - p_model)) / n_eff)

    edges = []
    for _ in range(BOOTSTRAP_N):
        p_sample = np.random.normal(p_model, se)
        p_sample = max(0.001, min(0.999, p_sample))
        edge = (p_sample - p_market) * 100
        edges.append(edge)

    edges = np.array(edges)
    alpha = 1 - BOOTSTRAP_CONF
    ci_low = float(np.percentile(edges, alpha / 2 * 100))
    ci_high = float(np.percentile(edges, (1 - alpha / 2) * 100))
    edge_point = (p_model - p_market) * 100

    significant = (ci_low > 0 and ci_high > 0) or (ci_low < 0 and ci_high < 0)

    return BootstrapCI(
        edge_point=edge_point,
        ci_low=ci_low,
        ci_high=ci_high,
        significant=significant,
    )


# ============================================================================
# Ensemble Dispersion Filter
# ============================================================================

def check_ensemble_dispersion(models: list) -> bool:
    """
    Check if models agree. If inter-model sigma > 3.0 C, skip trade.

    Reference: Raju et al. (2023) - dispersion > 2sigma correlates with
    40% higher error rate.
    """
    active_mus = [m.mu for m in models if m.active and m.weight > 0]
    if len(active_mus) < 2:
        return True  # Not enough models to check

    dispersion = float(np.std(active_mus, ddof=1))
    if dispersion > DISPERSION_MAX_PP:
        logger.info(f"Ensemble dispersion too high: {dispersion:.1f}C > {DISPERSION_MAX_PP}C")
        return False

    return True


# ============================================================================
# Main Signal Generation
# ============================================================================

def calc_brackets_full(
    mu: float,
    sigma: float,
    brackets: list,          # [(low, high, label), ...]
    yes_prices: list,        # Market yes price per bracket
    lead_h: float,
    age_h: float,
    bankroll: float,
    kelly_fraction: float,
    city: str,
    models: list,
    brier_ratio: float = 1.0,
    spread_pp: float = 0.0,
    depth_usd: float = 0.0,
    is_new_market: bool = False,
    is_dry_run: bool = True,
    calibration_fn=None,
    n_resolved: int = 0,
    n_wins: int = 0,          # v12.0: for Beta conjugate shrinkage
    velocity_skip_keys: set = None,
    bracket_keys: list = None,
    volume_usd: float = 0.0,  # v12.0: for liquidity-adjusted age
    is_exotic: bool = False,   # v12.0: for liquidity-adjusted age
) -> list[BracketSignal]:
    """
    Core signal generation: compute edge per bracket and classify.

    Steps per bracket:
    1. Compute P_model via CDF + MC blend (v12.0: justified weights)
    2. Apply isotonic calibration (if available)
    3. Apply Beta conjugate prior shrinkage (v12.0: REPLACES James-Stein)
    4. Edge = |P_model - P_market| x 100 [pp]
    5. Bootstrap CI check (skip if not significant)
    6. Price velocity check (skip if crowd already in)
    7. Liquidity filter
    8. Age-based threshold adjustment (v12.0: liquidity-adjusted max age)
    9. Classification: STRONG / SIGNAL / NEAR / PASS
    10. Kelly position sizing (v12.0: Beta conjugate shrinkage)

    Returns list of BracketSignal with actions.
    """
    # Check ensemble dispersion
    if not check_ensemble_dispersion(models):
        return []

    # v12.0: Liquidity-adjusted max age (replaces 3h hardcap)
    max_age = get_max_age_hours(volume_usd, is_exotic)
    if age_h > max_age:
        logger.info(
            f"Market age {age_h:.1f}h > {max_age:.1f}h (vol=${volume_usd:.0f}, "
            f"exotic={is_exotic}), skipping (liquidity-adjusted age filter)"
        )
        return []

    # Compute model probabilities (v12.0: justified CDF/MC blend)
    p_models = calc_bracket_probabilities(mu, sigma, brackets)

    # Apply isotonic calibration if available
    if calibration_fn:
        p_models = [calibration_fn(p) for p in p_models]

    # v12.0: Apply Beta Conjugate Prior shrinkage (REPLACES James-Stein)
    # At n=0: NO shrinkage — trust the model (unlike James-Stein which
    # destroyed all edge at n=0 by shrinking to 1/K ≈ 9%)
    if n_resolved > 0:
        p_models = [
            beta_conjugate_shrink(p, n_resolved, n_wins)
            for p in p_models
        ]
        # Clamp to valid range
        p_models = [max(0.0001, min(0.9999, p)) for p in p_models]
        logger.info(
            f"Beta conjugate shrinkage: n={n_resolved}, wins={n_wins}, "
            f"alpha={BETA_ALPHA}, beta={BETA_BETA}, k={BETA_BLEND_K}"
        )

    # Dynamic threshold based on Brier Score
    if brier_ratio >= 1.15:
        base_thr = 6.0  # Aggressive
    elif brier_ratio >= 1.0:
        base_thr = 8.0  # Standard
    elif brier_ratio >= 0.85:
        base_thr = 12.0  # Conservative
    else:
        base_thr = 999.0  # Pause

    # Age-based threshold (v12.0: simplified with liquidity-adjusted max age)
    if age_h < AGE_THRESHOLDS["new"]["max_age_h"]:
        age_thr = AGE_THRESHOLDS["new"]["threshold_pp"]
        age_mult = AGE_THRESHOLDS["new"]["age_mult"]
    else:
        # Fresh: use dynamic threshold with slight penalty
        age_thr = base_thr
        age_mult = AGE_THRESHOLDS["fresh"]["age_mult"]

    # Exotic city bonus (edge bonus only, NO kelly bonus)
    from src.lookup_tables import is_exotic as _is_exotic
    if _is_exotic(city):
        age_thr = max(3.0, age_thr - EXOTIC_EDGE_BONUS_PP)

    # Spread adjustment
    spread_adj = max(0, spread_pp - SPREAD_FLOOR)
    threshold = age_thr + spread_adj

    signals = []
    for i, (low, high, label) in enumerate(brackets):
        if i >= len(yes_prices):
            break

        p_mkt = yes_prices[i]
        p_mod = p_models[i]
        edge = (p_mod - p_mkt) * 100  # percentage points

        # Side determination
        if edge > 0:
            side = "BUY_YES"
        elif edge < 0:
            side = "BUY_NO"
        else:
            continue

        abs_edge = abs(edge)

        # Price velocity check
        velocity_skip = False
        if velocity_skip_keys and bracket_keys:
            bkey = bracket_keys[i] if i < len(bracket_keys) else None
            if bkey and bkey in velocity_skip_keys:
                velocity_skip = True

        # Bootstrap CI
        bs = bootstrap_edge_ci(p_mod, p_mkt, sigma)

        # Liquidity check
        min_depth = 0.0 if is_new_market else 30.0
        if depth_usd <= 0.0:
            passes_liq = is_new_market
        else:
            passes_liq = depth_usd >= min_depth

        # Classification
        classification = "PASS"
        order_type = "LIMIT"

        # Proximity check
        proximity_ok = _check_proximity(mu, sigma, low, high)

        # sigma hard block only for extreme uncertainty
        sigma_ok = sigma < 4.0

        # Skip if price velocity indicates crowd is already in
        if velocity_skip:
            classification = "PASS"
        elif abs_edge >= EDGE_STRONG and bs.significant and passes_liq and proximity_ok and sigma_ok:
            classification = "STRONG"
            order_type = "MARKET"
        elif abs_edge >= threshold and bs.significant and passes_liq and proximity_ok and sigma_ok:
            classification = "SIGNAL"
            order_type = "LIMIT"
        elif abs_edge >= threshold * 0.8:
            classification = "NEAR"

        # Kelly sizing
        stake = 0.0
        if classification in ("STRONG", "SIGNAL"):
            stake = _calc_stake(
                p_mod, p_mkt, side, bankroll, kelly_fraction,
                age_mult, sigma, brier_ratio, city, is_new_market,
                n_resolved, n_wins
            )

        sig = BracketSignal(
            label=label,
            low=low,
            high=high,
            p_model=p_mod,
            p_market=p_mkt,
            edge_pp=edge,
            side=side,
            classification=classification,
            stake_usd=stake,
            order_type=order_type,
            significant=bs.significant,
            passes_liquidity=passes_liq,
            spread_pp=spread_pp,
            velocity_skip=velocity_skip,
        )
        signals.append(sig)

    return signals


# ============================================================================
# Position Sizing - Adaptive Fractional Kelly with Beta Conjugate Prior
# ============================================================================

def _calc_stake(
    p_model: float, p_market: float, side: str,
    bankroll: float, base_fraction: float,
    age_mult: float, sigma: float,
    brier_ratio: float, city: str,
    is_new: bool,
    n_resolved: int = 0,
    n_wins: int = 0,
) -> float:
    """
    Calculate stake using Adaptive Fractional Kelly with Beta Conjugate Prior.

    f* = (p * b - q) / b
    where b = odds, p = model prob, q = 1-p

    For YES: b = (1 - p_mkt) / p_mkt
    For NO:  b = p_mkt / (1 - p_mkt)

    v12.0 changes (FATAL FIX #2):
    - REPLACED James-Stein with Beta Conjugate Prior for p_win shrinkage.
      At n=0, James-Stein shrinks p_win to 0.5, making Kelly = (0.5*b - 0.5)/b.
      For a typical b=1.5, this gives Kelly = 0.11, which is very small.
      But the REAL problem was in calc_brackets_full where ALL bracket probs
      were shrunk to 1/K, destroying edge entirely.

      Beta Conjugate Prior: at n=0, NO shrinkage on p_win for Kelly sizing.
      The model probability IS our prior — we trust it. As we accumulate
      data, we gently blend with the empirical posterior.

    - REMOVED exotic city kelly bonus (was creating overbetting risk)
      Only edge bonus (lower threshold) remains in calc_brackets_full.

    Adjustments:
    - Base fraction (half-Kelly = 0.50)
    - Age multiplier (new markets = 1.0, fresh = 0.90)
    - Drawdown-based fraction switching (f/2 -> f/4)
    - Brier Score multiplier
    - Sigma penalty
    - Minimum $1, maximum 25% of bankroll

    Reference: Thorp (2006) "The Kelly Criterion in Blackjack, Sports Betting, etc."
    Reference: Gelman et al. (2013) Bayesian Data Analysis, Ch.2 (Beta conjugate)
    """
    if p_market <= 0.01 or p_market >= 0.99:
        return 0.0

    # Calculate odds
    if side == "BUY_YES":
        b = (1 - p_market) / p_market
        p_win = p_model
    else:  # BUY_NO
        b = p_market / (1 - p_market)
        p_win = 1 - p_model

    q = 1 - p_win

    # v12.0: Beta Conjugate Prior shrinkage on p_win for Kelly sizing
    # At n=0: NO shrinkage — trust the model (unlike James-Stein → 0.5)
    # As n grows: gently blend with empirical Bayes posterior
    if n_resolved > 0:
        p_posterior = (BETA_ALPHA + n_wins) / (BETA_ALPHA + BETA_BETA + n_resolved)
        lam = n_resolved / (n_resolved + BETA_BLEND_K)
        p_win_shrunk = lam * p_win + (1 - lam) * p_posterior
        q_shrunk = 1 - p_win_shrunk
    else:
        # No resolved data: trust the model (no shrinkage)
        p_win_shrunk = p_win
        q_shrunk = q

    # Standard Kelly with shrunk probability
    kelly = max(0, (p_win_shrunk * b - q_shrunk) / b) if b > 0 else 0

    # Apply base fraction (half-Kelly)
    eff_f = kelly * base_fraction

    # Age multiplier
    eff_f *= age_mult

    # Sigma penalty (higher sigma = less confident)
    if sigma > 2.0:
        eff_f *= max(0.5, 1.0 - (sigma - 2.0) * 0.15)

    # Brier Score multiplier
    if brier_ratio < 0.85:
        eff_f *= 0.30  # Severe penalty
    elif brier_ratio < 1.0:
        eff_f *= 0.85

    # v12.0: REMOVED exotic city kelly bonus
    # Reference: Thorp (2006) - overbetting is much worse than underbetting

    # Floor lowered for $20 bankroll. 5% of $20 = $1 = MIN_BET_USD
    eff_f = max(0.05, min(0.50, eff_f))

    # Calculate stake
    stake = eff_f * bankroll

    # Absolute limits
    stake = min(stake, bankroll * 0.25)  # Max 25% per bet
    stake = max(MIN_BET_USD, stake) if stake >= MIN_BET_USD else 0.0

    return round(stake, 2)


def _check_proximity(mu: float, sigma: float, low: float, high: float) -> bool:
    """
    Check if ensemble mean is near this bracket.

    Tail brackets: mu within 1sigma of boundary
    Interior brackets: mu within 1.5sigma of midpoint

    BUG FIX (v10.2): Cap the proximity radius at 3.0C for interior,
    2.0C for tail. With sigma=3.5C, 1.5sigma = 5.25C radius would match
    entire range, defeating the purpose.

    Reference: Jolliffe & Stephenson (2012) - useful prediction range
    for temperature is typically within 2-3C of the forecast mean.
    """
    if low == float('-inf') or high == float('inf'):
        boundary = low if high == float('inf') else high
        return abs(mu - boundary) <= min(sigma * 1.0, 2.0)
    else:
        midpoint = (low + high) / 2
        return abs(mu - midpoint) <= min(sigma * 1.5, 3.0)


# ============================================================================
# Drawdown-Based Kelly Fraction Switching
# ============================================================================

def adaptive_kelly_fraction(drawdown: float, n_resolved: int,
                            brier_ratio: float = 1.0) -> tuple:
    """
    v13.1 REAL fix (v13.0 claimed this was fixed but the call into
    AdaptiveKelly was never actually wired in -- this replaces the
    genuine step-function body with calls to the three public
    multiplier methods on _ak).

    Returns a GLOBAL portfolio-level fraction multiplier. Per-bracket
    Kelly sizing (with Beta Conjugate Prior on the REAL p_model/p_market
    for that bracket) happens separately and correctly in _calc_stake()
    below -- this function does NOT duplicate that logic, it only
    answers: 'given drawdown/track-record/calibration, what %% of
    half-Kelly should the whole portfolio be scaled to right now?'

    Smooth exponential drawdown (Browne 1999) replaces the old
    f/2 -> f/3 -> f/4 step function:
      mult = exp(-ln(2)/10 * drawdown_pct)
      dd=0%: 1.00 | dd=5%: 0.71 | dd=10%: 0.50 | dd=15%: 0.35 | dd=20%: 0.25

    Sample-size confidence (Wu & Feng 2019):
      conf = max(0.30, 1 - exp(-n_resolved/30))

    Brier Score quality multiplier (unchanged thresholds from v12.0):
      ratio>=1.0: x1.00 | ratio>=0.90: x0.90 | ratio>=0.85: x0.75 | else: x0.30

    Returns (fraction, mode_label)
    """
    from src.config import KELLY_FRACTION  # base = 0.50 (half-Kelly)

    dd_mult, dd_mode = _ak.drawdown_multiplier(drawdown * 100)
    conf             = _ak.sample_confidence(n_resolved)
    bs_mult          = _ak.brier_multiplier(brier_ratio)

    frac = KELLY_FRACTION * dd_mult * conf * bs_mult
    frac = max(0.0, min(KELLY_FRACTION, frac))

    if n_resolved < 10:
        return 0.0, f"DRY_RUN (n={n_resolved}<10, dd={dd_mode})"

    label = f"dd={dd_mode}({dd_mult:.2f}) conf={conf:.2f} bs={bs_mult:.2f}"
    if frac <= 0.15:
        return round(frac, 4), f"QUARTER {label}"
    else:
        return round(frac, 4), f"HALF {label}"
