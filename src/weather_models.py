"""
Weather Models - 7-Model Ensemble - Bot v11.0
===============================================
The core of the bot: weather forecast models that compute P_model
for each temperature bracket using Gaussian CDF.

This is NOT technical analysis. These are REAL weather prediction models
that fetch data from meteorological APIs.

v11.0 changes from v10.x:
  - FIX BUG #9: Add temp_type to NWP cache key (highest vs lowest temp)
  - FIX BUG #12: Add fallback climate normal for unknown cities
    Uses NWP-based estimate when city is not in CLIMATE_NORMALS

7 Models:
  M1  NWP-OpenMeteo     : Deterministic NWP forecast, sigma = MAE(lead)
  M2  AR1-persist       : AR(1) model from Wilks (2011)
  M3  Bayesian-post     : Precision-weighted NWP + climate normal
  M4  ECMWF-ENS         : NWP point, sigma from ensemble spread
  M5  Regime-zScore     : Hot/Normal/Cold regime classifier
  M11 VolScale-sigma    : Under-dispersion correction
  M13 Hurst-H0.63       : sigma scaling for persistent processes (H=0.63)

Ensemble Combination:
  mu = Sum(mu_i * w_i) / Sum(w_i)
  sigma^2 = Sum(sigma_i^2 * w_i)/Sum(w_i) + 0.5 * Sum(w_i * (mu_i - mu)^2)/Sum(w_i)

Edge Calculation:
  edge = |P_model - P_market| * 100 [percentage points]
  P_model per bracket = Phi((hi-mu)/sigma) - Phi((lo-mu)/sigma)
"""

import math
import time
import numpy as np
from scipy.stats import norm
from dataclasses import dataclass, field
from typing import Optional

from src.config import OWM_BASE, ENS_BASE, METAR_BASE
from src.lookup_tables import (
    CITY_COORDS, CITY_STATION, STATION_BIAS,
    CLIMATE_NORMALS, DAYTIME_HEATING,
    get_cluster, get_phi, is_exotic,
    get_climate_normal_fallback,  # v11.0: FIX BUG #12
)
from src.utils import logger, api_get, _pf, _mae


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ModelResult:
    """Single model output."""
    model_id: str
    model_name: str
    mu: float = 0.0
    sigma: float = 2.0
    weight: float = 1.0
    active: bool = True
    inactive_reason: str = ""


@dataclass
class EnsembleResult:
    """Combined ensemble output."""
    mu_ensemble: float = 0.0
    sigma_ensemble: float = 2.0
    models: list = field(default_factory=list)
    skill_score: float = 0.0
    kl_total: float = 0.0
    fmea_rpn: float = 0.0
    hmm_state: str = "NORMAL"
    hmm_hot_p: float = 0.0
    hurst_adj: float = 1.0
    dynamic_k_mult: float = 1.0
    robust_disc: float = 0.0
    metar_updated: bool = False
    n_active: int = 0


# ============================================================================
# Data Fetching - Weather APIs
# ============================================================================

# Cache for NWP forecasts
_nwp_cache: dict = {}
_metar_cache: dict = {}
_ens_cache: dict = {}


def fetch_nwp(lat: float, lon: float, target_date: str,
              temp_type: str = "highest") -> Optional[tuple]:
    """
    Fetch deterministic NWP forecast from Open-Meteo.

    v11.0 FIX BUG #9: Added temp_type to cache key.
    The original cache key was f"{lat:.2f}_{lon:.2f}_{target_date}",
    which meant that fetching "highest" and "lowest" temperatures for
    the same location/date would return the SAME cached result. This
    caused the bot to use the max temperature forecast when it should
    have been using the min temperature forecast (or vice versa).

    Fix: Include temp_type in the cache key so that highest and lowest
    temperature forecasts are cached separately.

    Returns (mu_max, mu_min, lead_hours) or None.
    """
    # FIX BUG #9: Include temp_type in cache key
    cache_key = f"{lat:.2f}_{lon:.2f}_{target_date}_{temp_type}"
    if cache_key in _nwp_cache:
        cached_time, cached_data = _nwp_cache[cache_key]
        if time.time() - cached_time < 1800:  # 30 min cache
            return cached_data

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "start_date": target_date,
        "end_date": target_date,
    }

    data = api_get(OWM_BASE, params=params)
    if not data:
        return None

    try:
        daily = data.get("daily", {})
        t_max = daily.get("temperature_2m_max", [None])
        t_min = daily.get("temperature_2m_min", [None])

        if t_max and t_max[0] is not None:
            mu_max = float(t_max[0])
            mu_min = float(t_min[0]) if t_min and t_min[0] is not None else mu_max - 8

            # Estimate lead time
            from datetime import datetime, timezone
            try:
                target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc, hour=12
                )
                lead_h = max(1, (target_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
            except ValueError:
                lead_h = 24.0

            result = (mu_max, mu_min, lead_h)
            _nwp_cache[cache_key] = (time.time(), result)
            return result
    except (KeyError, IndexError, TypeError) as e:
        logger.warning(f"NWP parse error: {e}")

    return None


def fetch_ens_sigma(lat: float, lon: float, target_date: str) -> Optional[float]:
    """
    Fetch ECMWF ensemble spread (sigma from 51 members).

    Returns sigma in Celsius, clamped to [1.0, 5.0].
    """
    cache_key = f"{lat:.2f}_{lon:.2f}_{target_date}"
    if cache_key in _ens_cache:
        cached_time, cached_data = _ens_cache[cache_key]
        if time.time() - cached_time < 1800:
            return cached_data

    params = {
        "latitude": lat,
        "longitude": lon,
        "models": "ecmwf_ifs025",
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "start_date": target_date,
        "end_date": target_date,
    }

    data = api_get(ENS_BASE, params=params)
    if not data:
        return None

    try:
        daily = data.get("daily", {})
        t_max = daily.get("temperature_2m_max", [])
        if t_max:
            vals = [float(v) for v in t_max if v is not None]
            if len(vals) >= 5:
                sigma = max(1.0, min(5.0, float(np.std(vals, ddof=1))))
                _ens_cache[cache_key] = (time.time(), sigma)
                return sigma
    except (KeyError, TypeError) as e:
        logger.warning(f"ENS parse error: {e}")

    return None


def fetch_metar(station: str) -> Optional[float]:
    """
    Fetch current temperature from METAR aviation observation.

    Returns temperature in Celsius or None.
    """
    if station in _metar_cache:
        cached_time, cached_data = _metar_cache[station]
        if time.time() - cached_time < 900:  # 15 min cache
            return cached_data

    params = {
        "dataSource": "metars",
        "requestType": "retrieve",
        "format": "json",
        "stationString": station,
        "hoursBeforeNow": 2,
        "mostRecent": "true",
    }

    data = api_get(METAR_BASE, params=params)
    if not data:
        return None

    try:
        metars = data.get("data", {}).get("METAR", [])
        if metars:
            m = metars[0] if isinstance(metars, list) else metars
            temp_c = m.get("temp_c")
            if temp_c is not None:
                temp = float(temp_c)
                # Apply known station bias
                bias = STATION_BIAS.get(station, 0.0)
                temp += bias
                _metar_cache[station] = (time.time(), temp)
                return temp
    except (KeyError, TypeError) as e:
        logger.warning(f"METAR parse error for {station}: {e}")

    return None


# ============================================================================
# Model Implementations
# ============================================================================

def m1_nwp(nwp_mu: float, lead_h: float) -> ModelResult:
    """M1: Deterministic NWP with MAE-based sigma."""
    sigma = _mae(lead_h)
    return ModelResult("m1_nwp", "NWP-OpenMeteo", mu=nwp_mu, sigma=sigma, weight=1.0)


def m2_ar1(nwp_mu: float, nwp_sigma: float, city: str, lead_h: float,
           target_date: str = "") -> ModelResult:
    """M2: AR(1) persistence model (Wilks 2011 Ch.6).

    mu_AR1 = mu_clim * (1 - phi^h) + mu_NWP * phi^h
    sigma_AR1 = sigma_1 * sqrt((1 - phi^(2h)) / (1 - phi^2))
    """
    phi = get_phi(city)
    h_steps = max(1, lead_h / 24)

    # Climate normal as baseline (FIX #12: use target_date month)
    clim = _get_climate_mu(city, target_date=target_date)
    if clim is None:
        clim = nwp_mu  # Fallback

    mu_ar1 = clim * (1 - phi ** h_steps) + nwp_mu * phi ** h_steps
    sigma_ar1 = nwp_sigma * math.sqrt((1 - phi ** (2 * h_steps)) / max(0.01, 1 - phi ** 2))
    sigma_ar1 = max(1.0, min(5.0, sigma_ar1))

    return ModelResult("m2_ar1", "AR1-persist", mu=mu_ar1, sigma=sigma_ar1, weight=0.8)


def m3_bayesian(nwp_mu: float, nwp_sigma: float, city: str,
               target_date: str = "") -> ModelResult:
    """M3: Bayesian posterior of NWP + climate normal.

    tau_NWP = 1/sigma_NWP^2, tau_clim = 1/sigma_clim^2
    mu_post = (tau_NWP * mu_NWP + tau_clim * mu_clim) / (tau_NWP + tau_clim)
    """
    clim_mu, clim_sigma = _get_climate_mu_sigma(city, target_date=target_date)
    if clim_mu is None:
        # v11.0 FIX BUG #12: Use NWP-based fallback for unknown cities
        # instead of marking model inactive
        fallback_mu, fallback_sigma = _get_nwp_fallback_climate(city, nwp_mu)
        if fallback_mu is not None:
            clim_mu = fallback_mu
            clim_sigma = fallback_sigma
        else:
            return ModelResult("m3_bayesian", "Bayesian-post", mu=nwp_mu, sigma=nwp_sigma,
                              weight=0.5, active=False, inactive_reason="No climate data")

    tau_nwp = 1.0 / (nwp_sigma ** 2)
    tau_clim = 1.0 / (clim_sigma ** 2)
    mu_post = (tau_nwp * nwp_mu + tau_clim * clim_mu) / (tau_nwp + tau_clim)
    sigma_post = math.sqrt(1.0 / (tau_nwp + tau_clim))

    return ModelResult("m3_bayesian", "Bayesian-post", mu=mu_post, sigma=sigma_post, weight=0.7)


def m4_ecmwf_ens(nwp_mu: float, ens_sigma: Optional[float], lead_h: float) -> ModelResult:
    """M4: ECMWF ensemble spread for uncertainty."""
    if ens_sigma is None:
        return ModelResult("m4_ens", "ECMWF-ENS", mu=nwp_mu, sigma=_mae(lead_h),
                          weight=0.5, active=False, inactive_reason="No ensemble data")

    return ModelResult("m4_ens", "ECMWF-ENS", mu=nwp_mu, sigma=ens_sigma, weight=0.9)


def m5_regime(nwp_mu: float, city: str, target_date: str = "") -> ModelResult:
    """M5: Z-score regime classifier (HOT/NORMAL/COLD).

    z = (mu - mu_clim) / sigma_clim
    HOT if z > 0.7, COLD if z < -0.7, else NORMAL
    """
    clim_mu, clim_sigma = _get_climate_mu_sigma(city, target_date=target_date)
    if clim_mu is None:
        # v11.0 FIX BUG #12: Use NWP-based fallback
        fallback_mu, fallback_sigma = _get_nwp_fallback_climate(city, nwp_mu)
        if fallback_mu is not None:
            clim_mu = fallback_mu
            clim_sigma = fallback_sigma
        else:
            return ModelResult("m5_regime", "Regime-zScore", mu=nwp_mu, sigma=2.0,
                              weight=0.3, active=False, inactive_reason="No climate data")

    z = (nwp_mu - clim_mu) / max(clim_sigma, 0.5)
    shift = 0.5 if z > 0.7 else (-0.5 if z < -0.7 else 0.0)

    return ModelResult("m5_regime", "Regime-zScore", mu=nwp_mu + shift, sigma=2.0, weight=0.3)


def m11_vol_scale(ens_sigma: float, lead_h: float) -> ModelResult:
    """M11: Volatility scaling with under-dispersion correction.

    sigma_lead = sigma_ens * sqrt(lead/24) * 1.15
    Reference: Hagedorn et al. (2008); Jolliffe & Stephenson (2012)
    """
    sigma_scaled = ens_sigma * math.sqrt(lead_h / 24) * 1.15
    sigma_scaled = max(1.0, min(5.0, sigma_scaled))

    return ModelResult("m11_volscale", "VolScale-sigma", mu=0, sigma=sigma_scaled, weight=0.5,
                      active=False, inactive_reason="Sigma-only model")


def m13_hurst(sigma: float, lead_h: float) -> ModelResult:
    """M13: Hurst-based sigma scaling for persistent processes.

    v13.1 BUG FIX: exponent was (H - 0.5), corrected to H.

    For fractional Brownian motion, Std(t) is proportional to t^H
    (NOT t^(H-0.5)). The original formula used the wrong exponent,
    which made sigma UNDERESTIMATE uncertainty at long lead times by
    34-62% relative to empirical NWP MAE -- exactly when overconfidence
    is most dangerous for Kelly sizing.

    sigma_H = sigma_ref * (lead/24)^H   where H = 0.63

    Verification against NWP_MAE empirical table:
      lead=48h: old=1.86 (-34% vs MAE=2.80) | new=2.63 (-6% vs MAE=2.80)
      lead=72h: old=1.96 (-48% vs MAE=3.80) | new=3.40 (-11% vs MAE=3.80)
      lead=120h: old=2.10 (-62% vs MAE=5.50)| new=4.69 (-15% vs MAE=5.50)

    Reference: Koscielny-Bunde et al. (1998) "Indication of a universal
    persistence law governing atmospheric variability" Phys. Rev. Lett.
    Found H approx 0.65 for daily temperature across many stations
    (global average). H=0.63 used here is consistent with this range.
    """
    H = 0.63
    sigma_h = sigma * (lead_h / 24) ** H  # FIXED: was (H - 0.5)
    sigma_h = max(1.0, min(6.0, sigma_h))  # ceiling raised 5.0->6.0
    # to accommodate corrected long-lead values without artificial clip

    return ModelResult("m13_hurst", "Hurst-H0.63", mu=0, sigma=sigma_h, weight=0.5,
                      active=False, inactive_reason="Sigma-only model")


# ============================================================================
# Kalman Update with METAR
# ============================================================================

def kalman_update(prior_mu: float, prior_sigma: float, metar_temp: float,
                  city: str, month: int, temp_type: str = "highest") -> tuple:
    """
    Bayesian Kalman update: fuse NWP prior with METAR observation.

    For temp_type="highest":
      obs_mu = metar_temp + DAYTIME_HEATING[city][month]
      obs_sigma = 0.5C (tight - high confidence in heating model)

    For temp_type="lowest":
      obs_mu = metar_temp
      obs_sigma = 1.5C (wider - less certain about time-of-day)

    Update:
      tau_prior = 1/sigma_prior^2
      tau_obs = 1/sigma_obs^2
      mu_post = (tau_prior * mu_prior + tau_obs * mu_obs) / (tau_prior + tau_obs)
      sigma_post = sqrt(1 / (tau_prior + tau_obs))
    """
    if temp_type == "highest":
        heating = _get_daytime_heating(city, month)
        obs_mu = metar_temp + heating
        obs_sigma = 0.5  # Tight
    else:
        obs_mu = metar_temp
        obs_sigma = 1.5  # Wider

    tau_prior = 1.0 / (prior_sigma ** 2)
    tau_obs = 1.0 / (obs_sigma ** 2)

    post_mu = (tau_prior * prior_mu + tau_obs * obs_mu) / (tau_prior + tau_obs)
    post_sigma = math.sqrt(1.0 / (tau_prior + tau_obs))

    return post_mu, max(0.5, post_sigma)


# ============================================================================
# Monte Carlo Bracket Probabilities
# ============================================================================

def m7_mc(mu: float, sigma: float, brackets: list, n: int = 50000) -> list[float]:
    """
    Monte Carlo simulation for bracket probabilities.

    Samples N paths from N(mu, sigma), counts proportion in each bracket.

    Args:
        mu: Ensemble mean temperature
        sigma: Ensemble sigma
        brackets: List of (low, high) tuples
        n: Number of MC samples

    Returns:
        List of probabilities per bracket
    """
    samples = np.random.normal(mu, sigma, n)
    probs = []
    for low, high in brackets:
        if low == float('-inf') and high == float('inf'):
            probs.append(1.0)
        elif low == float('-inf'):
            p = float(np.mean(samples <= high))
        elif high == float('inf'):
            p = float(np.mean(samples >= low))
        else:
            p = float(np.mean((samples >= low) & (samples < high)))
        probs.append(max(0.0001, min(0.9999, p)))
    return probs


def cdf_formula(lo: float, hi: float, mu: float, sigma: float) -> float:
    """
    Gaussian CDF probability for a bracket.

    P = Phi((hi - mu) / sigma) - Phi((lo - mu) / sigma)

    For tail brackets:
      "or below": P = Phi((hi - mu) / sigma)
      "or higher": P = 1 - Phi((lo - mu) / sigma)
    """
    if sigma <= 0:
        sigma = 1.0

    if lo == float('-inf') and hi == float('inf'):
        return 1.0
    elif lo == float('-inf'):
        return float(norm.cdf((hi - mu) / sigma))
    elif hi == float('inf'):
        return float(1 - norm.cdf((lo - mu) / sigma))
    else:
        return float(norm.cdf((hi - mu) / sigma) - norm.cdf((lo - mu) / sigma))


# ============================================================================
# Ensemble Runner
# ============================================================================

def run_ensemble(city: str, target_date: str, temp_type: str = "highest") -> EnsembleResult:
    """
    Run the full 7-model ensemble for a city and date.

    v11.0: Passes temp_type to fetch_nwp for correct cache key (BUG #9).

    Steps:
    1. Fetch NWP forecast (M1)
    2. Fetch ECMWF ensemble spread (M4)
    3. Run AR(1) model (M2)
    4. Run Bayesian model (M3)
    5. Run regime classifier (M5)
    6. Run volatility scaling (M11)
    7. Run Hurst scaling (M13)
    8. Combine with weighted average
    9. Apply METAR Kalman update if available
    10. Compute skill score, KL, FMEA
    """
    # Get coordinates
    coords = CITY_COORDS.get(city.lower())
    if not coords:
        logger.warning(f"City not found: {city}")
        return EnsembleResult()
    lat, lon = coords

    # Fetch NWP (v11.0: pass temp_type for correct cache key)
    nwp_data = fetch_nwp(lat, lon, target_date, temp_type=temp_type)
    if not nwp_data:
        logger.warning(f"NWP fetch failed for {city} on {target_date}")
        return EnsembleResult()

    nwp_mu_max, nwp_mu_min, lead_h = nwp_data
    nwp_mu = nwp_mu_max if temp_type == "highest" else nwp_mu_min

    # Fetch ensemble sigma
    ens_sigma = fetch_ens_sigma(lat, lon, target_date)

    # Run models
    models = []

    # M1: NWP
    m1 = m1_nwp(nwp_mu, lead_h)
    models.append(m1)

    # M2: AR(1)
    m2 = m2_ar1(nwp_mu, m1.sigma, city, lead_h, target_date=target_date)
    models.append(m2)

    # M3: Bayesian
    m3 = m3_bayesian(nwp_mu, m1.sigma, city, target_date=target_date)
    models.append(m3)

    # M4: ECMWF ensemble
    m4 = m4_ecmwf_ens(nwp_mu, ens_sigma, lead_h)
    models.append(m4)

    # M5: Regime
    m5 = m5_regime(nwp_mu, city, target_date=target_date)
    models.append(m5)

    # M11: Volatility scaling
    base_sigma = ens_sigma if ens_sigma else m1.sigma
    m11 = m11_vol_scale(base_sigma, lead_h)
    models.append(m11)

    # M13: Hurst scaling
    m13 = m13_hurst(base_sigma, lead_h)
    models.append(m13)

    # Ensemble combination including sigma-only models
    # Mu: only from active mu-contributing models
    active_mu_models = [m for m in models if m.active and m.weight > 0]
    total_weight = sum(m.weight for m in active_mu_models)

    if total_weight == 0:
        return EnsembleResult(mu_ensemble=nwp_mu, sigma_ensemble=m1.sigma)

    mu_ens = sum(m.mu * m.weight for m in active_mu_models) / total_weight

    # Sigma: weighted avg of individual sigmas (ALL models) + between-model spread (mu models only)
    all_weighted_models = [m for m in models if m.weight > 0]  # Includes sigma-only
    total_sigma_weight = sum(m.weight for m in all_weighted_models)
    sigma_within = sum(m.sigma ** 2 * m.weight for m in all_weighted_models) / total_sigma_weight
    sigma_between = 0.5 * sum(m.weight * (m.mu - mu_ens) ** 2 for m in active_mu_models) / total_weight
    sigma_ens = math.sqrt(sigma_within + sigma_between)
    sigma_ens = max(1.0, min(5.0, sigma_ens))

    # METAR Kalman update (if lead <= 24h and station available)
    metar_updated = False
    station = CITY_STATION.get(city.lower())
    month = _get_month_from_date(target_date)
    if station and lead_h <= 24:
        metar_temp = fetch_metar(station)
        if metar_temp is not None:
            mu_ens, sigma_ens = kalman_update(mu_ens, sigma_ens, metar_temp, city, month, temp_type)
            metar_updated = True
            logger.info(f"METAR Kalman update: {city} mu={mu_ens:.1f}C sigma={sigma_ens:.2f}")

    # Skill score
    skill = max(0, 1 - _mae(lead_h) / 3.5)  # 3.5C = climate MAE

    # Dynamic Kelly multiplier based on lead time
    if lead_h <= 6:
        dyn_k = 1.0
    elif lead_h <= 24:
        dyn_k = 0.85
    elif lead_h <= 48:
        dyn_k = 0.70
    elif lead_h <= 72:
        dyn_k = 0.50
    else:
        dyn_k = 0.30

    n_active = len(active_mu_models)

    return EnsembleResult(
        mu_ensemble=mu_ens,
        sigma_ensemble=sigma_ens,
        models=models,
        skill_score=skill,
        dynamic_k_mult=dyn_k,
        metar_updated=metar_updated,
        n_active=n_active,
    )


# ============================================================================
# Helpers
# ============================================================================

def _get_climate_mu(city: str, target_date: str = "") -> Optional[float]:
    """Get climate normal mean for the TARGET DATE's month.

    Uses target_date month, not current month.
    If target is July 1 but today is June 30, we need July's normal.
    """
    month = _get_month_from_date(target_date) if target_date else datetime.now().month
    normals = CLIMATE_NORMALS.get(city.lower())
    if normals and month in normals:
        hi, lo = normals[month]
        return hi

    # v11.0 FIX BUG #12: Fallback for unknown cities
    fallback = get_climate_normal_fallback(city, month)
    if fallback:
        return fallback[0]  # Return high temperature normal

    return None


def _get_climate_mu_sigma(city: str, target_date: str = "") -> tuple:
    """Get climate normal (mu, sigma) for the TARGET DATE's month.

    Uses target_date month, not current month.
    """
    month = _get_month_from_date(target_date) if target_date else datetime.now().month
    normals = CLIMATE_NORMALS.get(city.lower())
    if normals and month in normals:
        hi, lo = normals[month]
        mu = (hi + lo) / 2
        sigma = max(1.5, (hi - lo) / 2)
        return mu, sigma

    # v11.0 FIX BUG #12: Fallback for unknown cities
    fallback = get_climate_normal_fallback(city, month)
    if fallback:
        hi, lo = fallback
        mu = (hi + lo) / 2
        sigma = max(1.5, (hi - lo) / 2)
        return mu, sigma

    return None, None


def _get_nwp_fallback_climate(city: str, nwp_mu: float) -> tuple:
    """
    v11.0 FIX BUG #12: Generate fallback climate normal from NWP estimate.

    When a city is not in CLIMATE_NORMALS, we can use the NWP forecast
    as a rough estimate of the climate normal for the current month.

    This is less accurate than a true 30-year normal, but it's much
    better than marking the model as inactive and losing its contribution.

    Returns (mu_estimate, sigma_estimate) or (None, None)
    """
    if nwp_mu is None:
        return None, None

    # Use the NWP forecast as the mean estimate
    # Use a wider sigma to reflect the uncertainty
    sigma_estimate = 4.0  # Wide sigma to reflect uncertainty in fallback
    return nwp_mu, sigma_estimate


def _get_daytime_heating(city: str, month: int) -> float:
    """Get expected daytime heating for city and month."""
    heating = DAYTIME_HEATING.get(city.lower(), {})
    return heating.get(month, 7)  # Default 7C


def _get_month_from_date(date_str: str) -> int:
    """Extract month from date string."""
    try:
        from datetime import datetime
        return datetime.strptime(date_str, "%Y-%m-%d").month
    except ValueError:
        return 6  # Default June
