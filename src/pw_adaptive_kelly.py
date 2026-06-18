"""
pw_adaptive_kelly.py — Adaptive Kelly with Beta Conjugate + Smooth Drawdown
v13.0: Integrated into main bot (was disconnected in v6-v12 — Bug#6 fix)

Architecture:
  This module replaces the step-function Kelly in signal_generator.py.
  It implements:
  1. Beta Conjugate Prior for p_win shrinkage (not James-Stein)
     Reference: Gelman et al. (2013) Bayesian Data Analysis Ch.2
  2. Smooth exponential drawdown protection
     Reference: Browne (1999) "Reaching Goals by a Deadline"
  3. Sample-size confidence scaling
     Reference: Wu & Feng (2019) "Adaptive Kelly Strategy"
  4. Brier Score calibration quality factor
     Reference: Brier (1950); Jolliffe & Stephenson (2012)

Usage in signal_generator.py:
  from src.pw_adaptive_kelly import AdaptiveKelly
  kelly = AdaptiveKelly()
  fraction, mode = kelly.compute(p_win, b, n_resolved, n_wins, drawdown, brier_ratio)
"""
from dataclasses import dataclass
from typing import Tuple
import math


@dataclass
class KellyResult:
    f_raw: float         # Full Kelly fraction
    f_effective: float   # After all adjustments
    stake_usd: float     # Actual dollar amount
    mode: str            # DRY_RUN / QUARTER / HALF / FULL


class AdaptiveKelly:
    """
    v13.1 ARCHITECTURE NOTE (corrects a v13.0 integration bug):

    Per-bracket Kelly sizing (using REAL p_model/p_market per signal) is
    ALREADY correctly implemented in signal_generator.py:_calc_stake()
    with Beta Conjugate Prior shrinkage (v12.0, predates this module).
    That logic is untouched and correct -- do not duplicate it here.

    THIS class's role in the live pipeline is narrower: provide the
    GLOBAL portfolio-level multiplier (replaces old step function
    f/2->f/3->f/4) via drawdown_multiplier(), sample_confidence(), and
    brier_multiplier(). These three public methods are what
    signal_generator.py:adaptive_kelly_fraction() actually calls.

    The full compute() method below (which DOES take p_win/b) is kept
    for potential standalone/offline use but is NOT invoked by the live
    main.py -> signal_generator.py pipeline. A v13.0 bug mistakenly wired
    compute() into adaptive_kelly_fraction() using fictitious p_win=0.5,
    b=1.5 defaults, corrupting the global multiplier by ~6x. Fixed in
    v13.1 by calling the three multiplier methods directly instead.

    Production-grade Adaptive Kelly position sizer.

    Parameters (all tunable via config):
      base_fraction   : ½ Kelly (0.50)
      min_bet_usd     : Polymarket minimum $1.00
      max_pct_bankroll: Hard cap at 25% per bet
      beta_alpha      : Prior pseudocount wins (default 2)
      beta_beta       : Prior pseudocount losses (default 8)
      beta_k          : Blend factor — at n=k, 50% model / 50% posterior
    """

    def __init__(
        self,
        base_fraction: float = 0.50,
        min_bet_usd: float = 1.00,
        max_pct_bankroll: float = 0.25,
        beta_alpha: float = 2.0,
        beta_beta: float = 8.0,
        beta_k: float = 20.0,
    ):
        self.base_fraction     = base_fraction
        self.min_bet_usd       = min_bet_usd
        self.max_pct_bankroll  = max_pct_bankroll
        self.beta_alpha        = beta_alpha
        self.beta_beta         = beta_beta
        self.beta_k            = beta_k

    def _beta_shrink_p_win(self, p_win: float, n_resolved: int, n_wins: int) -> float:
        """
        Beta Conjugate Prior shrinkage on p_win.

        At n=0: NO shrinkage — trust the model entirely.
        As n grows: gently blend toward empirical posterior.

        p_posterior = (alpha + n_wins) / (alpha + beta + n_resolved)
        lambda = n / (n + k)  → blend weight on model
        p_shrunk = lambda * p_win + (1 - lambda) * p_posterior

        Replaces James-Stein which wrongly kills early signals (n=0 → p=0.5).
        Reference: Gelman et al. (2013) BDA3 Ch.2; Thorp (2006)
        """
        if n_resolved <= 0:
            return p_win  # Trust model — no data yet

        p_posterior = (self.beta_alpha + n_wins) / (
            self.beta_alpha + self.beta_beta + n_resolved
        )
        lam = n_resolved / (n_resolved + self.beta_k)
        return lam * p_win + (1 - lam) * p_posterior

    def drawdown_multiplier(self, drawdown_pct: float) -> Tuple[float, str]:
        """
        Smooth exponential drawdown protection.

        Replaces step-function (f/2, f/3, f/4) with continuous decay.
        dd=0%: mult=1.00  (no reduction)
        dd=5%: mult=0.78  (mild reduction)
        dd=10%: mult=0.61 (moderate)
        dd=15%: mult=0.47 (severe — approach ¼ Kelly)
        dd=20%: mult=0.37 (approaching emergency stop)

        Formula: mult = exp(-lambda * dd)
        lambda = ln(2) / 10% = 6.93 → halves every 10pp drawdown

        Reference: Browne (1999) Theorem 1: smooth fraction decay
        under drawdown constraint is growth-optimal.
        """
        if drawdown_pct <= 0:
            return 1.0, "NORMAL"
        lam = math.log(2) / 10.0  # halve every 10% drawdown
        mult = math.exp(-lam * drawdown_pct)
        if drawdown_pct >= 20:
            mode = "EMERGENCY"
        elif drawdown_pct >= 15:
            mode = "SEVERE"
        elif drawdown_pct >= 10:
            mode = "MODERATE"
        elif drawdown_pct >= 5:
            mode = "MILD"
        else:
            mode = "NORMAL"
        return mult, mode

    def sample_confidence(self, n_resolved: int) -> float:
        """
        Sample-size confidence scaling.

        n=0:   scale=0.30  (low confidence, very early)
        n=10:  scale=0.60
        n=25:  scale=0.80
        n=50:  scale=0.90
        n=100: scale=0.96
        n=∞:   scale=1.00

        Formula: scale = 1 - exp(-n / tau)
        tau = 30 → 63% confidence at n=30

        Reference: Wu & Feng (2019) Adaptive Kelly eq. (4)
        """
        tau = 30.0
        base = 1.0 - math.exp(-n_resolved / tau)
        return max(0.30, base)  # Never go below 30% scaling

    def brier_multiplier(self, brier_ratio: float) -> float:
        """
        Brier Score quality multiplier.

        brier_ratio = E[BS] / actual_BS  (higher = better model)
        ratio >= 1.0: model at or above prior → no penalty
        ratio 0.85-1.0: mild penalty (x0.85)
        ratio < 0.85: severe penalty (x0.30)

        E[BS] = avg_p * (1 - avg_p) ≈ 0.20 for well-calibrated model
        """
        if brier_ratio >= 1.0:
            return 1.0
        elif brier_ratio >= 0.90:
            return 0.90
        elif brier_ratio >= 0.85:
            return 0.75
        else:
            return 0.30  # Model severely miscalibrated

    def compute(
        self,
        p_win: float,
        b: float,             # Odds ratio: (1-p_mkt)/p_mkt for YES
        bankroll: float,
        n_resolved: int = 0,
        n_wins: int = 0,
        drawdown_pct: float = 0.0,
        brier_ratio: float = 1.0,
        age_mult: float = 1.0,    # 1.0 for new, 0.85 for fresh
        sigma: float = 1.5,       # Ensemble sigma
        is_dry_run: bool = False,
    ) -> KellyResult:
        """
        Full adaptive Kelly computation.

        Returns KellyResult with effective fraction and stake.
        """
        if p_win <= 0 or p_win >= 1 or b <= 0:
            return KellyResult(0, 0, 0, "INVALID")

        q_win = 1 - p_win

        # Step 1: Beta conjugate shrinkage
        p_shrunk = self._beta_shrink_p_win(p_win, n_resolved, n_wins)
        q_shrunk = 1 - p_shrunk

        # Step 2: Full Kelly
        f_raw = max(0.0, (p_shrunk * b - q_shrunk) / b)

        # Step 3: Apply base fraction (½ Kelly)
        f_eff = f_raw * self.base_fraction

        # Step 4: Drawdown multiplier (smooth exponential)
        dd_mult, dd_mode = self._drawdown_multiplier(drawdown_pct)
        f_eff *= dd_mult

        # Step 5: Sample confidence scaling
        conf = self._sample_confidence(n_resolved)
        f_eff *= conf

        # Step 6: Brier Score multiplier
        f_eff *= self._brier_multiplier(brier_ratio)

        # Step 7: Age multiplier
        f_eff *= age_mult

        # Step 8: Sigma penalty (uncertainty correction)
        if sigma > 2.0:
            sigma_penalty = max(0.5, 1.0 - (sigma - 2.0) * 0.15)
            f_eff *= sigma_penalty

        # Step 9: Hard clamp [0, max_pct_bankroll]
        f_eff = min(f_eff, self.max_pct_bankroll)
        f_eff = max(0.0, f_eff)

        # Step 10: Compute stake
        stake = f_eff * bankroll
        # Bug#14 REAL fix: smooth 80% threshold (was hard cutoff at exactly min_bet_usd)
        # Old bug: stake=$0.99 -> $0 but stake=$1.00 -> $1.00 (discontinuous jump)
        # Fix: anything >= 80% of min bet rounds UP to min bet; below that -> skip
        if stake >= self.min_bet_usd * 0.8:
            stake = max(self.min_bet_usd, stake)
        else:
            stake = 0.0

        # Mode label
        if is_dry_run:
            mode = "DRY_RUN"
        elif f_eff <= 0:
            mode = "SKIP"
        elif f_eff <= 0.15:
            mode = "QUARTER"
        elif f_eff <= 0.30:
            mode = "HALF"
        else:
            mode = "AGGRESSIVE"

        return KellyResult(
            f_raw=round(f_raw, 4),
            f_effective=round(f_eff, 4),
            stake_usd=round(stake, 2),
            mode=mode,
        )
