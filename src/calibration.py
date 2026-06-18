"""
Calibration & Brier Tracker - Bot v12.0
=========================================
v12.0 CRITICAL FIX (FATAL #2):
  - REPLACED James-Stein shrinkage with Beta Conjugate Prior.
    James-Stein at n=0 shrinks ALL probs to 1/K ≈ 9%, destroying edge.
    Beta Conjugate at n=0 applies NO shrinkage — trusts the model.
  - BrierTracker.get_win_rate() now also returns n_wins for Beta conjugate

v10.1 fixes (carried forward):
  - BrierTracker now stores bracket_label and condition_id per prediction
  - resolve_prediction() replaces resolve() with full bracket matching
  - get_unresolved() added for resolution checker
  - _calc_win_rate() now reads "result" field that IS written by main.py
  - Win rate properly distinguishes BUY_YES vs BUY_NO
"""

import json
import time
import os
import numpy as np
from typing import Optional
from src.config import FILE_BRIER, FILE_CALIBRATION, BETA_ALPHA, BETA_BETA, BETA_BLEND_K
from src.utils import logger, safe_read, safe_write


# ============================================================================
# Beta Conjugate Prior Shrinkage Helper (v12.0 - REPLACES James-Stein)
# ============================================================================

def beta_conjugate_shrink(p_model: float, n_resolved: int, n_wins: int = 0,
                          alpha_prior: float = None, beta_prior: float = None,
                          k: int = None) -> float:
    """
    Apply Beta conjugate prior shrinkage to a model probability estimate.

    REPLACES James-Stein shrinkage which was fatally flawed for prediction
    market probabilities (FATAL #2).

    Why Beta Conjugate Prior:
    1. Prediction market outcomes are Bernoulli (win/lose)
    2. Beta is the conjugate prior for Bernoulli likelihood
    3. At n=0: NO shrinkage (trust the model), unlike James-Stein → 1/K
    4. Posterior mean has closed-form solution

    Formula:
      p_posterior = (alpha + n_wins) / (alpha + beta + n)
      lambda = n / (n + k)
      p_shrunk = lambda * p_model + (1 - lambda) * p_posterior

    Example blend factors (k=20):
      n=0:  lambda=0.00 → p = p_model (NO shrinkage)
      n=10: lambda=0.33 → mild shrinkage toward empirical
      n=50: lambda=0.71 → mostly model, some posterior
      n=200: lambda=0.91 → strongly model

    Reference: Gelman et al. (2013) Bayesian Data Analysis, Ch.2
    Reference: Efron (2010) Large-Scale Inference, Ch.1
    """
    if alpha_prior is None:
        alpha_prior = BETA_ALPHA
    if beta_prior is None:
        beta_prior = BETA_BETA
    if k is None:
        k = BETA_BLEND_K

    if n_resolved == 0:
        return p_model  # NO shrinkage — trust the model

    p_posterior = (alpha_prior + n_wins) / (alpha_prior + beta_prior + n_resolved)
    lam = n_resolved / (n_resolved + k)
    p_shrunk = lam * p_model + (1 - lam) * p_posterior
    return max(0.0001, min(0.9999, p_shrunk))


# ============================================================================
# Isotonic Calibrator (PAVA)
# ============================================================================

class IsotonicCalibrator:
    """
    Self-calibrating probability output using PAVA.

    Accumulates (p_model, actual_outcome) pairs from resolved markets.
    Produces a monotone non-decreasing mapping from raw -> calibrated probability.
    """

    def __init__(self):
        self.pairs: list[tuple[float, int]] = []  # (p_model, outcome)
        self.calibrated_edges: list[tuple[float, float]] = []  # (raw, calibrated)
        self._load()

    def _load(self):
        """Load calibration state from disk."""
        data = safe_read(FILE_CALIBRATION, {"pairs": []})
        for p in data.get("pairs", []):
            try:
                self.pairs.append((float(p[0]), int(p[1])))
            except (ValueError, IndexError):
                pass
        self._rebuild()

    def _save(self):
        """Save calibration state to disk."""
        safe_write(FILE_CALIBRATION, {
            "pairs": self.pairs[-500:],  # Cap at 500 most recent
        })

    def add_observation(self, p_model: float, actual: int) -> None:
        """
        Add a resolved observation.

        Args:
            p_model: Raw model probability
            actual: 1 if YES won, 0 if NO won
        """
        self.pairs.append((p_model, actual))
        if len(self.pairs) > 500:
            self.pairs = self.pairs[-500:]
        self._rebuild()
        self._save()

    def calibrate(self, p_raw: float) -> float:
        """
        Calibrate a raw probability using the learned mapping.

        Linear interpolation between calibrated bin edges.
        Extrapolation from nearest edge.
        """
        if not self.calibrated_edges or len(self.pairs) < 10:
            return p_raw  # Not enough data

        # Find bracket
        for i in range(len(self.calibrated_edges) - 1):
            lo_raw, lo_cal = self.calibrated_edges[i]
            hi_raw, hi_cal = self.calibrated_edges[i + 1]
            if lo_raw <= p_raw <= hi_raw:
                # Linear interpolation
                if hi_raw == lo_raw:
                    return (lo_cal + hi_cal) / 2
                frac = (p_raw - lo_raw) / (hi_raw - lo_raw)
                return lo_cal + frac * (hi_cal - lo_cal)

        # Extrapolation from nearest edge
        if p_raw < self.calibrated_edges[0][0]:
            return self.calibrated_edges[0][1]
        return self.calibrated_edges[-1][1]

    def _rebuild(self):
        """Rebuild calibration mapping using PAVA."""
        if len(self.pairs) < 10:
            return

        # Sort by raw probability
        sorted_pairs = sorted(self.pairs, key=lambda x: x[0])
        raws = [p[0] for p in sorted_pairs]
        actuals = [p[1] for p in sorted_pairs]

        # PAVA: Pool Adjacent Violators Algorithm
        calibrated = _pava(raws, actuals)

        # Build edges (unique raw -> calibrated mapping)
        self.calibrated_edges = []
        for r, c in zip(raws, calibrated):
            if not self.calibrated_edges or self.calibrated_edges[-1][0] != r:
                self.calibrated_edges.append((r, c))


def _pava(x: list[float], y: list[int]) -> list[float]:
    """
    Pool Adjacent Violators Algorithm for isotonic regression.

    Produces a monotone non-decreasing sequence that minimizes
    squared error to the original data.
    """
    n = len(x)
    if n == 0:
        return []

    # Initialize blocks
    blocks = [[i] for i in range(n)]
    values = [float(y[i]) for i in range(n)]

    i = 0
    while i < len(blocks) - 1:
        if values[i] > values[i + 1]:
            # Violation: merge blocks
            blocks[i].extend(blocks[i + 1])
            values[i] = sum(y[j] for j in blocks[i]) / len(blocks[i])
            blocks.pop(i + 1)
            values.pop(i + 1)
            # Check previous block
            if i > 0:
                i -= 1
        else:
            i += 1

    # Expand back to per-element
    result = [0.0] * n
    for block_idx, block in enumerate(blocks):
        for element_idx in block:
            result[element_idx] = values[block_idx]

    return result


# ============================================================================
# Brier Score Tracker
# ============================================================================

class BrierTracker:
    """
    Track Brier Score for self-calibration and dynamic thresholds.

    BS = (p_model - actual)^2
    Cumulative BS = mean of all individual BS values.

    EXPECTED_BS = 0.20 as a practical benchmark for well-calibrated model.
    Reference: Brier (1950), Jolliffe & Stephenson (2012) Ch.7.
    """

    EXPECTED_BS = 0.20  # Practical benchmark for well-calibrated model

    def __init__(self):
        self.predictions: list[dict] = []  # Full prediction records
        self.resolved: list[dict] = []     # Resolved records with Brier scores
        self.cumulative_bs: float = 0.0
        self.n_resolved: int = 0
        self._load()

    def _load(self):
        data = safe_read(FILE_BRIER, {
            "predictions": [], "resolved": [],
            "cumulative_bs": 0.0, "n_resolved": 0,
        })
        self.predictions = data.get("predictions", [])
        self.resolved = data.get("resolved", [])
        self.cumulative_bs = data.get("cumulative_bs", 0.0)
        self.n_resolved = data.get("n_resolved", 0)

    def _save(self):
        safe_write(FILE_BRIER, {
            "predictions": self.predictions[-500:],
            "resolved": self.resolved[-500:],
            "cumulative_bs": self.cumulative_bs,
            "n_resolved": self.n_resolved,
        })

    def record_prediction(self, slug: str, p_model: float, side: str,
                          bracket_label: str = "", condition_id: str = "") -> None:
        """Record a prediction for later Brier evaluation."""
        self.predictions.append({
            "slug": slug,
            "p_model": p_model,
            "side": side,
            "bracket_label": bracket_label,
            "condition_id": condition_id,
            "timestamp": time.time(),
        })
        if len(self.predictions) > 500:
            self.predictions = self.predictions[-500:]
        self._save()

    def resolve_prediction(self, pred: dict, actual: int) -> Optional[float]:
        """
        Resolve a specific prediction with actual outcome.

        BUG FIX (v10.2): `pred not in self.predictions` uses identity
        comparison on dicts loaded from JSON. After deserialization,
        pred is a different object. Use index-based search instead.

        Args:
            pred: The prediction dict from self.predictions
            actual: 1 if YES won, 0 if NO won

        Returns:
            Brier score for this prediction, or None if not found
        """
        # Find matching prediction by content, not identity
        idx = None
        for i, p in enumerate(self.predictions):
            if (p.get("slug") == pred.get("slug") and
                p.get("bracket_label") == pred.get("bracket_label") and
                p.get("condition_id") == pred.get("condition_id") and
                abs(p.get("p_model", 0) - pred.get("p_model", 0)) < 0.001 and
                p.get("side") == pred.get("side")):
                idx = i
                break

        if idx is None:
            return None

        matched_pred = self.predictions[idx]
        bs = (matched_pred["p_model"] - actual) ** 2
        self.resolved.append({
            "slug": matched_pred.get("slug", ""),
            "bracket_label": matched_pred.get("bracket_label", ""),
            "condition_id": matched_pred.get("condition_id", ""),
            "p_model": matched_pred["p_model"],
            "side": matched_pred.get("side", ""),
            "actual": actual,
            "bs": bs,
        })
        self.n_resolved += 1
        # Update cumulative Brier Score
        self.cumulative_bs = (
            (self.cumulative_bs * (self.n_resolved - 1) + bs) / self.n_resolved
        )
        self.predictions.pop(idx)
        self._save()
        return bs

    def get_unresolved(self) -> list[dict]:
        """Get all unresolved predictions for the resolution checker."""
        return list(self.predictions)  # Return a copy

    def get_ratio(self) -> float:
        """
        Brier Score ratio: E[BS] / actual_BS.

        ratio >= 1.15 -> aggressive (6pp threshold)
        ratio >= 1.0  -> standard (8pp)
        ratio >= 0.85 -> conservative (12pp)
        ratio < 0.85  -> pause (999pp)
        """
        if self.n_resolved < 3:
            return 1.0  # Not enough data, use standard
        actual_bs = self.cumulative_bs
        if actual_bs <= 0:
            return 2.0
        return self.EXPECTED_BS / actual_bs

    def get_win_rate(self) -> float:
        """Calculate win rate from resolved predictions.

        A prediction is a WIN if:
        - BUY_YES and actual=1 (the bracket we bet YES on won)
        - BUY_NO and actual=0 (the bracket we bet NO on lost)
        """
        if not self.resolved:
            return 0.0

        wins = 0
        total = 0
        for r in self.resolved:
            side = r.get("side", "")
            actual = r.get("actual", -1)
            if actual < 0:
                continue
            total += 1
            if side == "BUY_YES" and actual == 1:
                wins += 1
            elif side == "BUY_NO" and actual == 0:
                wins += 1

        return wins / total if total > 0 else 0.0

    def get_win_count(self) -> int:
        """Get number of winning predictions (for Beta conjugate shrinkage)."""
        if not self.resolved:
            return 0

        wins = 0
        for r in self.resolved:
            side = r.get("side", "")
            actual = r.get("actual", -1)
            if actual < 0:
                continue
            if side == "BUY_YES" and actual == 1:
                wins += 1
            elif side == "BUY_NO" and actual == 0:
                wins += 1

        return wins

    def get_bs_kelly_mult(self) -> float:
        """Kelly multiplier based on Brier Score calibration."""
        ratio = self.get_ratio()
        return min(1.15, max(0.30, ratio))


# ============================================================================
# Win Rate Calculator
# ============================================================================

def _calc_win_rate() -> float:
    """Calculate win rate from performance log.

    Reads the "result" field that IS written by
    _update_performance_result() in main.py when markets resolve.
    """
    try:
        log_path = "data/pw_performance.jsonl"
        if not os.path.exists(log_path):
            return 0.0
        wins = 0
        total = 0
        with open(log_path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("resolved", False) and "result" in entry:
                        total += 1
                        if entry["result"] == "win":
                            wins += 1
                except (json.JSONDecodeError, KeyError):
                    continue
        return wins / total if total > 0 else 0.0
    except Exception:
        return 0.0


# ============================================================================
# Validation Protocol
# ============================================================================

def get_kelly_mode(n_resolved: int, brier_ratio: float,
                   drawdown: float, fills_pct: float = 1.0) -> tuple:
    """
    14-day auto-upgrade validation protocol.

    Day 1-7:   DRY_RUN (0%)
    Day 7 checkpoint: WR>18%, BS ratio>=0.85, DD<20% -> QUARTER (25%)
    Day 8-14:  QUARTER
    Day 14 GO/NO-GO: WR>17%, BS ratio>=0.90, DD<20%, fills>70% -> HALF (50%)
    Failure: Stay QUARTER or pause

    Returns (fraction, mode_label)

    BUG FIX (v10.2): Docstring now correctly describes brier_ratio.
    brier_ratio = E[BS]/actual_BS, so ratio >= 1.0 means model is GOOD.
    """
    if n_resolved == 0:
        return 0.0, "DRY_RUN"

    win_rate = _calc_win_rate()
    bs_ok = brier_ratio >= 0.85
    dd_ok = drawdown < 0.20

    if n_resolved < 10:
        return 0.0, "DRY_RUN (N<10)"

    if n_resolved < 20:
        if win_rate > 0.18 and bs_ok and dd_ok:
            return 0.25, "QUARTER"
        return 0.0, "DRY_RUN (checkpoint fail)"

    if n_resolved < 50:
        if win_rate > 0.17 and brier_ratio >= 0.90 and dd_ok and fills_pct > 0.70:
            return 0.50, "HALF"
        return 0.25, "QUARTER (GO fail)"

    # Mature
    if brier_ratio >= 0.90 and dd_ok:
        return 0.50, "HALF"
    elif brier_ratio >= 0.85 and dd_ok:
        return 0.25, "QUARTER"
    else:
        return 0.0, "PAUSE (BS/DD)"
