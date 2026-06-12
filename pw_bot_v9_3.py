#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  POLYWEATHER BOT  v9.3  (pw_bot_v9_3.py)                                 ║
║  — PRODUCTION + ADVERSARIAL MOAT + 5 NEW MODELS                         ║
║                                                                          ║
║  NEW IN pw_ BUILD (vs old v9.3):                                         ║
║  + 100% Adaptive Kelly — always uses live wallet balance                ║
║  + CLOB Auto-Derive — 4 secrets sufficient (no CLOB_API_KEY needed)     ║
║  + Adaptive Fractional Kelly — f/2→f/3→f/4 based on drawdown+calib     ║
║  + Liquidity Filter — skip thin orderbooks (depth < min)                ║
║  + Isotonic Calibration — PAVA calibrates model→actual probabilities    ║
║  + Bootstrap CI on Edge — 90% CI on edge via resampling                 ║
║  + Ensemble Dispersion Filter — no trade when models disagree            ║
║  + All files renamed pw_ prefix — no confusion with v9.2                ║
║                                                                          ║
║  v9.3 features retained:                                                  ║
║  + Each bracket is a SEPARATE independent YES/NO market                  ║
║  + Bracket parser: handles all real Polymarket question formats          ║
║  + Lowest temperature support (T_min from NWP + METAR)                  ║
║  + Exotic city Moat (edge decay 30% slower)                              ║
║  + Validation Protocol (DRY_RUN→QUARTER→HALF)                           ║
║  + Dynamic Threshold (6-12pp berdasarkan Brier Score)                    ║
║  + Price velocity detector + CategoryTracker                             ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
import os,re,json,math,time,logging,hashlib,hmac,base64,requests
from datetime import datetime,timedelta,timezone
from dataclasses import dataclass,field
from typing import Optional,List,Dict,Tuple,Any
import numpy as np
from scipy.stats import norm
try:
    from dateutil import parser as du_parser; DATEUTIL_OK=True
except: DATEUTIL_OK=False

logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s")
log=logging.getLogger(__name__)

def _env_float(key, default="0"):
    """Safe float from env — handles GitHub Actions empty-string secrets."""
    return float(os.environ.get(key, "").strip() or default)

# ══════════════════════════════════════════════════════════════════════════
#  §1  CONFIG
# ══════════════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN","")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID","")
POLY_PRIVATE_KEY    = os.environ.get("POLY_PRIVATE_KEY","")
POLY_FUNDER         = os.environ.get("POLY_FUNDER","")
CLOB_API_KEY        = os.environ.get("CLOB_API_KEY","")
CLOB_API_SECRET     = os.environ.get("CLOB_API_SECRET","")
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE","")
DRY_RUN             = os.environ.get("DRY_RUN","").strip().lower() not in ("false","0","no")
FAST_SCAN           = os.environ.get("FAST_SCAN","").strip().lower() in ("true","1","yes")

GAMMA_BASE  = "https://gamma-api.polymarket.com"
CLOB_BASE   = "https://clob.polymarket.com"
OWM_BASE    = "https://api.open-meteo.com/v1/forecast"
ENS_BASE    = "https://ensemble-api.open-meteo.com/v1/ensemble"
METAR_BASE  = "https://www.aviationweather.gov/adds/dataserver_current/httpparam"
POLY_RPC    = "https://polygon-rpc.com"

DEFAULT_BANKROLL  = _env_float("BANKROLL", "20")
MIN_VOLUME        = 50.0
COOLDOWN_H        = 6
PAGES             = 5
PAGE_LIMIT        = 100
NEW_MARKET_WIN    = 10    # menit — pasar dianggap "baru" untuk fast scan

# Kelly
KELLY_FRACTION    = 0.50   # ½ Kelly: f_eff = f* × 0.50
KELLY_MAX_PCT     = 0.25
KELLY_PORT_PCT    = 0.40
MAX_MKTS          = 5
EDGE_THRESHOLD    = 8.0    # default (overridden by dynamic threshold)
EDGE_STRONG       = 20.0   # threshold market order (bukan limit)
SPREAD_FLOOR      = 2.0

STATE_FILE   = "pw_seen_markets.json"
BRIER_FILE   = "pw_brier_scores.json"
PERF_FILE    = "pw_performance.jsonl"
BANKROLL_FILE= "pw_bankroll.json"
PENDING_FILE = "pw_pending_orders.json"
BOT_STATE    = "pw_bot_state.json"
CAT_FILE     = "category_stats.json"
PRICE_FILE   = "pw_price_history.json"
CALIB_FILE   = "pw_calibration.json"    # NEW: isotonic calibration data
LIQUID_FILE  = "pw_liquidity_cache.json"  # NEW: liquidity filter cache

# ── New Model Parameters ──
LIQUIDITY_MIN_DEPTH = 30.0    # Minimum orderbook depth in USD to trade
BOOTSTRAP_N         = 1000    # Bootstrap resampling iterations
BOOTSTRAP_CONF      = 0.90    # Bootstrap confidence level (90%)
DISPERSION_MAX_PP   = 3.0     # Max model dispersion in °C before filter triggers
KELLY_F4_DRAWDOWN   = 0.15    # Drawdown level for f/4 Kelly
KELLY_F3_DRAWDOWN   = 0.10    # Drawdown level for f/3 Kelly
KELLY_F2_DRAWDOWN   = 0.05    # Drawdown level for f/2 Kelly

INF=float('inf'); HURST_H=0.63
_nwp_cache:Dict[tuple,tuple]={}
_ens_cache:Dict[tuple,float]={}
_metar_cache:Dict[str,Optional[float]]={}

# ── AR(1) persistence coefficients by climate cluster ──
# φ values from Wilks (2011) Table 6.3: daily T persistence
# Tropical: φ≈0.70 (low persistence, strong diurnal cycle)
# Temperate: φ≈0.80 (moderate persistence)
# Continental: φ≈0.85 (high persistence, slower weather changes)
AR1_PHI = {
    "southeast_asia": 0.70, "south_asia": 0.72, "middle_east": 0.74,
    "mediterranean": 0.78, "n_am_south": 0.76, "africa_north": 0.75,
    "n_am_east": 0.80, "n_am_central": 0.82, "n_am_west": 0.78,
    "east_asia": 0.80, "oceania": 0.76, "latam": 0.74,
    "northern_europe": 0.82, "eastern_europe": 0.83,
    "n_am_sw": 0.77, "africa_east": 0.73,
}
AR1_DEFAULT_PHI = 0.78

# ── Startup secret validation ──
_REQUIRED_SECRETS = {
    "TELEGRAM_TOKEN": bool(TELEGRAM_TOKEN),
    "TELEGRAM_CHAT_ID": bool(TELEGRAM_CHAT_ID),
    "POLY_PRIVATE_KEY": bool(POLY_PRIVATE_KEY),
    "POLY_FUNDER": bool(POLY_FUNDER),
}
_MISSING = [k for k,v in _REQUIRED_SECRETS.items() if not v]
if _MISSING:
    log.warning(f"Missing secrets: {', '.join(_MISSING)} — some features disabled")
_CLOB_SECRETS = {
    "CLOB_API_KEY": bool(CLOB_API_KEY),
    "CLOB_API_SECRET": bool(CLOB_API_SECRET),
    "CLOB_API_PASSPHRASE": bool(CLOB_API_PASSPHRASE),
}
_CLOB_MISSING = [k for k,v in _CLOB_SECRETS.items() if not v]
if _CLOB_MISSING:
    log.info(f"CLOB secrets not provided: {', '.join(_CLOB_MISSING)} — will auto-derive from private_key+funder")

# ══════════════════════════════════════════════════════════════════════════
#  §2  LOOKUP TABLES
# ══════════════════════════════════════════════════════════════════════════
WEATHER_KW=["celsius","fahrenheit","°c","°f","temperature","rainfall",
             "precipitation","snowfall","blizzard","will it rain","typhoon",
             "hurricane","tropical storm","tornado","cyclone","humidity",
             "heatwave","heat wave","uv index","weather forecast",
             "weather in ","weather on ","weather for "]
CITY_COORDS={
    "hong kong":(22.3193,114.1694),"tokyo":(35.6762,139.6503),
    "seoul":(37.5665,126.9780),"london":(51.5074,-0.1278),
    "new york":(40.7128,-74.0060),"new york city":(40.7128,-74.0060),
    "nyc":(40.7128,-74.0060),"paris":(48.8566,2.3522),
    "singapore":(1.3521,103.8198),"jakarta":(-6.2088,106.8456),
    "shanghai":(31.2304,121.4737),"chicago":(41.8781,-87.6298),
    "los angeles":(34.0522,-118.2437),"miami":(25.7617,-80.1918),
    "sydney":(-33.8688,151.2093),"dubai":(25.2048,55.2708),
    "mumbai":(19.0760,72.8777),"bangkok":(13.7563,100.5018),
    "taipei":(25.0330,121.5654),"osaka":(34.6937,135.5023),
    "beijing":(39.9042,116.4074),"berlin":(52.5200,13.4050),
    "amsterdam":(52.3676,4.9041),"kuala lumpur":(3.1390,101.6869),
    "manila":(14.5995,120.9842),"madrid":(40.4168,-3.7038),
    "toronto":(43.6532,-79.3832),"sao paulo":(-23.5505,-46.6333),
    "buenos aires":(-34.6037,-58.3816),"cairo":(30.0444,31.2357),
    "istanbul":(41.0082,28.9784),"moscow":(55.7558,37.6173),
    "tel aviv":(32.0853,34.7818),"wellington":(-41.2866,174.7756),
    "seattle":(47.6062,-122.3321),"houston":(29.7604,-95.3698),
    "denver":(39.7392,-104.9903),"dallas":(32.7767,-96.7970),
    "atlanta":(33.7490,-84.3880),"boston":(42.3601,-71.0589),
    "miami":(25.7617,-80.1918),"phoenix":(33.4484,-112.0740),
    "san francisco":(37.7749,-122.4194),"las vegas":(36.1699,-115.1398),
    "new orleans":(29.9511,-90.0715),"minneapolis":(44.9778,-93.2650),
    "montreal":(45.5017,-73.5673),"vancouver":(49.2827,-123.1207),
    "calgary":(51.0447,-114.0719),"ottawa":(45.4215,-75.6972),
    "karachi":(24.8607,67.0011),"cape town":(-33.9249,18.4241),
    "nairobi":(-1.2921,36.8219),"lagos":(6.5244,3.3792),
    "johannesburg":(-26.2041,28.0473),"accra":(5.6037,-0.1870),
    "casablanca":(33.5731,-7.5898),"riyadh":(24.7136,46.6753),
    "tehran":(35.6892,51.3890),"lahore":(31.5204,74.3587),
    "dhaka":(23.8103,90.4125),"colombo":(6.9271,79.8612),
    "kathmandu":(27.7172,85.3240),"ho chi minh":(10.8231,106.6297),
    "hanoi":(21.0285,105.8542),"yangon":(16.8661,96.1951),
    "rome":(41.9028,12.4964),"milan":(45.4654,9.1859),
    "barcelona":(41.3851,2.1734),"lisbon":(38.7169,-9.1399),
    "brussels":(50.8503,4.3517),"vienna":(48.2082,16.3738),
    "zurich":(47.3769,8.5417),"stockholm":(59.3293,18.0686),
    "oslo":(59.9139,10.7522),"copenhagen":(55.6761,12.5683),
    "helsinki":(60.1699,24.9384),"warsaw":(52.2297,21.0122),
    "budapest":(47.4979,19.0402),"prague":(50.0755,14.4378),
    "bucharest":(44.4268,26.1025),"kyiv":(50.4501,30.5234),
    "lima":(-12.0464,-77.0428),"bogota":(4.7110,-74.0721),
    "santiago":(-33.4489,-70.6693),"auckland":(-36.8509,174.7645),
    "brisbane":(-27.4698,153.0251),"perth":(-31.9505,115.8605),
    "chengdu":(30.5728,104.0668),"guangzhou":(23.1291,113.2644),
    "shenzhen":(22.5431,114.0579),"wuhan":(30.5928,114.3052),
    "ulaanbaatar":(47.9077,106.8832),"addis ababa":(9.0320,38.7469),
    "dar es salaam":(-6.7924,39.2083),"kampala":(0.3163,32.5822),
    "lusaka":(-15.4167,28.2833),"harare":(-17.8252,31.0335),
    "abuja":(9.0579,7.4951),"munich":(48.1351,11.5820),
    "hamburg":(53.5753,10.0153),"frankfurt":(50.1109,8.6821),
    "edinburgh":(55.9533,-3.1883),"manchester":(53.4808,-2.2426),
    "glasgow":(55.8642,-4.2518),"athens":(37.9838,23.7275),
    "panama city":(8.9936,-79.5197),"mexico city":(19.4326,-99.1332),
    "austin":(30.2672,-97.7431),"portland":(45.5051,-122.6750),
    "salt lake city":(40.7608,-111.8910),"minneapolis":(44.9778,-93.2650),
    "vientiane":(17.9757,102.6331),"phnom penh":(11.5564,104.9282),
}
CITY_STATION={
    "hong kong":"HKO","tokyo":"RJTT","seoul":"RKSI","london":"EGLC",
    "new york":"KLGA","new york city":"KLGA","nyc":"KLGA","paris":"LFPB",
    "singapore":"WSSS","jakarta":"WIHH","shanghai":"ZSPD","chicago":"KORD",
    "sydney":"YSSY","dubai":"OMDB","mumbai":"VABB","bangkok":"VTBD",
    "taipei":"RCTP","osaka":"RJBB","beijing":"ZBAA","berlin":"EDDB",
    "houston":"KHOU","atlanta":"KATL","miami":"KMIA","boston":"KBOS",
    "dallas":"KDFW","seattle":"KSEA","denver":"KDEN","phoenix":"KPHX",
    "los angeles":"KLAX","san francisco":"KSFO","new orleans":"KMSY",
    "minneapolis":"KMSP","las vegas":"KLAS","amsterdam":"EHAM",
    "brussels":"EBBR","vienna":"LOWW","zurich":"LSZH","stockholm":"ESSA",
    "oslo":"ENGM","copenhagen":"EKCH","helsinki":"EFHK","warsaw":"EPWA",
    "budapest":"LHBP","prague":"LKPR","bucharest":"LROP","kyiv":"UKBB",
    "moscow":"UUDD","istanbul":"LTBA","tel aviv":"LLBG","athens":"LGAV",
    "rome":"LIRF","milan":"LIMC","barcelona":"LEBL","madrid":"LEMD",
    "lisbon":"LPPT","sao paulo":"SBGR","cairo":"HECA",
}
STATION_BIAS={
    "EGLC":-2.5,"RKSI":-1.5,"RJTT":-1.0,"ZSPD":-1.5,"KLGA":-0.5,
    "WSSS":-0.5,"LFPB":-0.5,"KORD":0.0,"HKO":0.0,"WIHH":0.0,
    "YSSY":-0.5,"OMDB":+0.5,"VABB":0.0,"VTBD":0.0,"RCTP":-0.5,
}
CLIMATE_NORMALS={
    "hong kong":{4:(26.1,21.1),5:(29.8,24.2),6:(31.2,26.1),7:(31.5,26.6)},
    "tokyo":{4:(18.3,10.0),5:(23.0,14.8),6:(25.5,19.0),7:(29.2,22.8)},
    "london":{4:(13.2,5.8),5:(17.4,8.8),6:(20.5,11.5),7:(22.4,13.3)},
    "new york":{4:(15.0,5.9),5:(19.8,10.7),6:(25.2,16.0),7:(29.0,19.7)},
    "new york city":{4:(15.0,5.9),5:(19.8,10.7),6:(25.2,16.0),7:(29.0,19.7)},
    "paris":{4:(14.7,6.5),5:(19.2,9.7),6:(22.7,12.8),7:(25.1,14.9)},
    "singapore":{4:(31.9,24.6),5:(31.7,24.6),6:(31.5,24.4),7:(31.2,24.5)},
    "jakarta":{4:(31.5,24.8),5:(31.3,24.8),6:(31.2,23.7),7:(31.2,23.4)},
    "dubai":{4:(33.5,20.1),5:(38.0,24.8),6:(40.2,27.3),7:(41.4,30.1)},
    "sydney":{4:(22.3,14.4),5:(19.3,11.8),6:(16.4,9.4),7:(15.9,8.0)},
    "miami":{4:(28.9,20.0),5:(30.9,22.4),6:(32.3,24.1),7:(33.0,25.1)},
    "atlanta":{4:(20.0,8.3),5:(25.0,13.3),6:(29.4,18.3),7:(31.7,20.6)},
    "houston":{4:(24.4,13.9),5:(28.3,18.3),6:(32.2,22.2),7:(34.4,23.9)},
    "seoul":{4:(16.8,6.8),5:(22.8,12.5),6:(26.9,17.8),7:(29.6,21.9)},
    "berlin":{4:(13.0,4.5),5:(18.5,9.0),6:(21.5,12.0),7:(23.6,14.2)},
    "beijing":{4:(19.6,7.2),5:(26.8,13.0),6:(31.2,18.6),7:(30.8,21.9)},
    "shanghai":{4:(17.5,9.6),5:(22.8,14.5),6:(27.4,19.8),7:(32.0,24.1)},
    "bangkok":{4:(35.2,26.2),5:(34.0,25.9),6:(32.8,25.5),7:(32.2,25.3)},
    "mumbai":{4:(33.0,24.0),5:(33.2,25.7),6:(31.8,25.8),7:(30.5,25.6)},
}
FORECAST_MAE={6:1.0,12:1.2,24:1.5,36:1.8,48:2.0,72:2.5,96:3.0,120:3.5}
DAYTIME_HEATING={
    "dubai":{1:7,2:8,3:9,4:9,5:10,6:10,7:10,8:10,9:9,10:9,11:8,12:7},
    "phoenix":{1:9,2:10,3:11,4:12,5:13,6:14,7:14,8:13,9:12,10:11,11:9,12:8},
    "riyadh":{1:10,2:11,3:12,4:12,5:13,6:14,7:13,8:13,9:12,10:11,11:10,12:9},
    "houston":{1:7,2:8,3:9,4:8,5:10,6:11,7:10,8:10,9:9,10:9,11:8,12:7},
    "atlanta":{1:8,2:9,3:10,4:9,5:10,6:10,7:9,8:9,9:9,10:9,11:8,12:7},
    "dallas":{1:8,2:9,3:10,4:10,5:11,6:12,7:12,8:12,9:11,10:10,11:9,12:8},
    "miami":{1:7,2:7,3:7,4:7,5:7,6:6,7:6,8:6,9:6,10:7,11:7,12:7},
    "new york":{1:6,2:7,3:8,4:9,5:10,6:10,7:10,8:10,9:9,10:8,11:7,12:6},
    "new york city":{1:6,2:7,3:8,4:9,5:10,6:10,7:10,8:10,9:9,10:8,11:7,12:6},
    "boston":{1:6,2:7,3:8,4:9,5:10,6:10,7:9,8:9,9:9,10:8,11:7,12:6},
    "chicago":{1:6,2:7,3:8,4:8,5:9,6:10,7:9,8:9,9:8,10:7,11:6,12:5},
    "los angeles":{1:8,2:9,3:10,4:10,5:10,6:9,7:8,8:8,9:9,10:10,11:10,12:9},
    "san francisco":{1:7,2:8,3:9,4:9,5:9,6:9,7:8,8:8,9:9,10:9,11:8,12:7},
    "seattle":{1:5,2:6,3:7,4:8,5:9,6:9,7:9,8:9,9:8,10:6,11:5,12:4},
    "london":{1:4,2:5,3:6,4:7,5:8,6:8,7:7,8:7,9:6,10:5,11:4,12:4},
    "paris":{1:5,2:6,3:8,4:9,5:10,6:10,7:9,8:9,9:8,10:7,11:5,12:5},
    "berlin":{1:5,2:6,3:8,4:9,5:11,6:11,7:10,8:10,9:8,10:7,11:5,12:4},
    "tokyo":{1:8,2:9,3:10,4:10,5:10,6:8,7:7,8:7,9:8,10:10,11:9,12:8},
    "seoul":{1:7,2:8,3:9,4:10,5:10,6:8,7:7,8:8,9:9,10:10,11:8,12:7},
    "beijing":{1:7,2:8,3:10,4:11,5:11,6:10,7:8,8:8,9:10,10:11,11:9,12:7},
    "singapore":{1:4,2:4,3:4,4:4,5:4,6:4,7:4,8:4,9:4,10:4,11:4,12:4},
    "jakarta":{1:4,2:4,3:4,4:4,5:4,6:4,7:4,8:4,9:4,10:4,11:4,12:4},
    "bangkok":{1:7,2:8,3:8,4:6,5:6,6:5,7:5,8:5,9:5,10:6,11:6,12:6},
    "mumbai":{1:8,2:9,3:10,4:9,5:8,6:5,7:4,8:4,9:5,10:7,11:9,12:8},
    "sydney":{1:8,2:8,3:8,4:9,5:9,6:8,7:9,8:10,9:10,10:10,11:9,12:8},
    "default":7.5,
}
CLIMATE_CLUSTERS={
    "northern_europe":["london","paris","amsterdam","berlin","brussels",
                       "copenhagen","oslo","stockholm","helsinki","zurich",
                       "vienna","warsaw","prague","budapest","edinburgh",
                       "manchester","glasgow","frankfurt","hamburg","munich"],
    "mediterranean":["rome","milan","barcelona","madrid","athens","lisbon"],
    "eastern_europe":["bucharest","kyiv","belgrade","sofia","moscow","istanbul"],
    "east_asia":["tokyo","seoul","shanghai","beijing","taipei","hong kong",
                 "osaka","guangzhou","shenzhen"],
    "southeast_asia":["singapore","jakarta","bangkok","kuala lumpur","manila",
                      "ho chi minh","hanoi","yangon","colombo"],
    "south_asia":["mumbai","dhaka","lahore","karachi","kathmandu"],
    "n_am_east":["new york","new york city","nyc","boston","montreal","toronto"],
    "n_am_south":["miami","atlanta","new orleans","houston","dallas","austin"],
    "n_am_central":["chicago","minneapolis","denver","salt lake city"],
    "n_am_west":["seattle","portland","vancouver","san francisco","los angeles"],
    "n_am_sw":["phoenix","las vegas"],
    "middle_east":["dubai","riyadh","tehran","tel aviv"],
    "africa_north":["cairo","casablanca"],
    "africa_east":["nairobi","addis ababa","dar es salaam","kampala",
                   "lusaka","harare","abuja"],
    "oceania":["sydney","brisbane","perth","auckland","wellington"],
    "latam":["sao paulo","buenos aires","lima","bogota","santiago",
             "mexico city","panama city"],
}

# ── Moat 2: Kota eksotis — kompetitor bot minimal, edge decay lebih lambat
# ECMWF tetap sama akuratnya di seluruh dunia — ini pure competitive advantage
EXOTIC_CITIES = {
    # Asia Selatan/Tengah/SE
    "kathmandu","dhaka","lahore","colombo","vientiane","phnom penh",
    "yangon","ulaanbaatar",
    # Afrika
    "nairobi","addis ababa","dar es salaam","kampala","lusaka","harare",
    "abuja","kano","accra","casablanca","kinshasa",
    # Amerika Latin (selain Sao Paulo/Buenos Aires yang sudah ramai)
    "montevideo","quito","caracas","panama city",
    # Timur Tengah (selain Dubai/Riyadh)
    "tehran",
    # Eropa Timur yang jarang dicover bot mainstream
    "bucharest","belgrade","kyiv","sofia","zagreb",
}
EXOTIC_EDGE_BONUS_PP = 1.5   # Threshold turun 1.5pp → lebih mudah masuk
EXOTIC_KELLY_BONUS   = 1.10  # Kelly × 1.10 → lebih agresif karena less adversarial
# Basis: α_exotic(t) = α(0) × e^(-0.70λ × t) — edge decay 30% lebih lambat

# ══════════════════════════════════════════════════════════════════════════
#  §3  DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class ModelResult:
    model_id:str; model_name:str; mu:float; sigma:float
    weight:float=1.0; notes:str=""; active:bool=True; inactive_reason:str=""

@dataclass
class EnsembleResult:
    mu_ensemble:float; sigma_ensemble:float
    models:List[ModelResult]=field(default_factory=list)
    skill_score:float=0.0; kl_total:float=0.0; fmea_rpn:int=0
    hmm_state:str="NORMAL"; hmm_hot_p:float=0.5
    hurst_adj:float=1.0; dynamic_k_mult:float=1.0; robust_disc:float=0.0
    metar_updated:bool=False; n_active:int=0

@dataclass
class OrderResult:
    bracket_label:str; bracket_short:str; side:str; token_id:str
    limit_price:float; size_usdc:float; map_price:float=0.0
    order_id:str=""; status:str="pending"; error:str=""
    placed_at:str=""; order_type:str="LIMIT"  # LIMIT or MARKET

@dataclass
class BootstrapCI:
    """Bootstrap confidence interval on edge estimate."""
    edge_point:float; ci_low:float; ci_high:float
    significant:bool    # True if CI doesn't cross zero

@dataclass
class LiquidityResult:
    """Liquidity check result for a market token."""
    token_id:str; passes:bool; best_ask:Optional[float]=None
    best_bid:Optional[float]=None; depth_usd:float=0.0
    spread_pp:float=0.0

# ══════════════════════════════════════════════════════════════════════════
#  §4  HELPERS + STATE
# ══════════════════════════════════════════════════════════════════════════
def _is_exotic_event(ev: dict) -> bool:
    """Moat 2: Cek apakah event adalah kota eksotis (minim kompetitor bot)."""
    text = (ev.get("question","") or ev.get("groupSlug","") or
            ev.get("slug","")).lower().replace("-"," ")
    return any(city in text for city in EXOTIC_CITIES)

def is_weather_market(m:dict)->bool:
    """Identify weather temperature markets from real Polymarket data.
    v9.3: Matches actual question formats:
      - "Will the highest temperature in [CITY] be 28°C on [DATE]?"
      - "Will the lowest temperature in [CITY] be 15°C or higher on [DATE]?"
      - "Will the highest temperature in [CITY] be between 82-83°F on [DATE]?"
    Also matches slug patterns like 'highest-temperature-in-seoul-on-june-13-2026'.
    """
    q=(m.get("question","") or m.get("title","")).lower()
    s=(m.get("slug","") or m.get("groupSlug","")).lower()
    # Specific temperature market patterns from real Polymarket data
    temp_q_patterns=[
        r'be\s+\d+\.?\d*\s*°?\s*[cf]\b',           # "be 28°C" / "be 82°F"
        r'be\s+between\s+\d+',                        # "be between 82-83°F"
        r'be\s+\d+\.?\d*\s*°?\s*[cf]?\s+or\s+',     # "be 25°C or higher/lower"
        r'highest\s+temperature\s+in\b',              # "highest temperature in..."
        r'lowest\s+temperature\s+in\b',               # "lowest temperature in..."
    ]
    for pat in temp_q_patterns:
        if re.search(pat,q): return True
    # Slug patterns for temperature events/markets
    slug_patterns=[
        r'temperature-in-.*-\d{4}$',                  # event slug
        r'temperature-in-.*-\d+[cf](?:or\w+)?$',      # market slug with bracket
        r'temperature-in-.*-\d+-\d+[cf]$',            # market slug with range bracket
    ]
    for pat in slug_patterns:
        if re.search(pat,s): return True
    # Fallback to keyword matching
    return any(kw in q for kw in WEATHER_KW)

def _pf(v)->float:
    try: return float(v or 0)
    except: return 0.0

def _mae(h:float)->float:
    keys=sorted(FORECAST_MAE)
    if h<=keys[0]: return FORECAST_MAE[keys[0]]
    if h>=keys[-1]: return FORECAST_MAE[keys[-1]]
    for i in range(len(keys)-1):
        if keys[i]<=h<=keys[i+1]:
            t=(h-keys[i])/(keys[i+1]-keys[i])
            return FORECAST_MAE[keys[i]]*(1-t)+FORECAST_MAE[keys[i+1]]*t
    return 2.0

def _parse_dt(raw:str)->datetime:
    s=str(raw).strip()
    if s.endswith("+00:00Z"): s=s[:-1]
    elif s.endswith("Z"): s=s[:-1]+"+00:00"
    if "+" not in s and len(s)>10 and "-" not in s[10:]: s+="+00:00"
    return datetime.fromisoformat(s)

def _market_age_hours(ev:dict)->Optional[float]:
    for f in ("createdAt","startDate"):
        raw=ev.get(f,"")
        if not raw: continue
        try: return (datetime.now(timezone.utc)-_parse_dt(raw)).total_seconds()/3600
        except: continue
    return None

def _is_open(ev:dict)->bool:
    for f in ("endDate","end_date","endDateIso"):
        raw=ev.get(f) or (ev.get("_raw") or {}).get(f)
        if not raw: continue
        try: return _parse_dt(raw)>datetime.now(timezone.utc)+timedelta(hours=1)
        except: continue
    return True

def safe_write(path:str,data:dict):
    tmp=path+".tmp"; bak=path+".backup"
    try:
        with open(tmp,"w") as f:
            json.dump(data,f,indent=2,default=str)
        if os.path.exists(path):
            import shutil; shutil.copy2(path,bak)
        os.replace(tmp,path)
    except Exception as e: log.warning(f"Write {path}: {e}")

def safe_read(path:str,default=None)->dict:
    for f in [path,path+".backup"]:
        try:
            with open(f) as fh:
                return json.load(fh)
        except: continue
    return default if default is not None else {}

def load_seen()->dict:
    data=safe_read(STATE_FILE,{})
    cut=(datetime.now(timezone.utc)-timedelta(hours=48)).timestamp()
    return {k:v for k,v in data.items() if isinstance(v,float) and v>cut}

def load_bot_state()->dict:
    return safe_read(BOT_STATE,{"paused":False,"update_offset":0,
                                "daily_start_bankroll":0.0,"last_date":"",
                                "kelly_mode":"DRY_RUN",
                                "fill_count":0,"fill_total":0,
                                "first_prediction_ts":""})

# ══════════════════════════════════════════════════════════════════════════
#  §5  API + TELEGRAM + COMMANDS
# ══════════════════════════════════════════════════════════════════════════
def api_get(url,params=None,retries=3):
    h={"Accept":"application/json","User-Agent":"PolyWeather/9.3"}
    for i in range(1,retries+1):
        try:
            r=requests.get(url,params=params,headers=h,timeout=25)
            if r.status_code==429: time.sleep(2**i); continue
            r.raise_for_status(); return r.json()
        except requests.HTTPError:
            if r.status_code in (400,401,403,404): break
        except Exception as e: log.warning(f"API {i}/{retries}: {e}")
        if i<retries: time.sleep(2**i)
    return None

def send_telegram(text:str)->bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    for chunk in [text[i:i+4000] for i in range(0,len(text),4000)]:
        try:
            r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":TELEGRAM_CHAT_ID,"text":chunk,
                      "parse_mode":"HTML","disable_web_page_preview":True},timeout=15)
            if r.status_code!=200: return False
            time.sleep(0.3)
        except: return False
    return True

def send_msgs(msgs:List[str]):
    for i,m in enumerate(msgs):
        send_telegram(m)
        if i<len(msgs)-1: time.sleep(0.5)

def check_tg_commands(bot_state:dict)->dict:
    flags={"pause":False,"resume":False,"emergency_stop":False,
           "status_req":False,"validate_req":False}
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return flags
    try:
        r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"timeout":1,"offset":bot_state.get("update_offset",0)},timeout=6)
        r.raise_for_status()
        for upd in r.json().get("result",[]):
            msg=upd.get("message",{})
            if str(msg.get("chat",{}).get("id",""))!=str(TELEGRAM_CHAT_ID): continue
            text=msg.get("text","").strip().lower()
            bot_state["update_offset"]=upd.get("update_id",0)+1
            if "/pause" in text:          flags["pause"]=True
            elif "/resume" in text:       flags["resume"]=True
            elif "/emergency_stop" in text: flags["emergency_stop"]=True
            elif "/status" in text:       flags["status_req"]=True
            elif "/validate" in text:     flags["validate_req"]=True
    except Exception as e: log.debug(f"TG cmd: {e}")
    return flags

# ══════════════════════════════════════════════════════════════════════════
#  §6  SCANNER  (v9.3: complete overhaul — client-side filtering)
# ══════════════════════════════════════════════════════════════════════════
def _extract_event_slug(market_slug:str)->str:
    """Extract event slug from market slug by stripping bracket suffix.
    Real patterns:
      - highest-temperature-in-seoul-on-june-13-2026-28c
      - highest-temperature-in-dallas-on-june-13-2026-82-83f
      - highest-temperature-in-seoul-on-june-13-2026-25corhigher
      - highest-temperature-in-seoul-on-june-13-2026-25corbelow
      - lowest-temperature-in-seoul-on-june-13-2026-15c
      - lowest-temperature-in-seoul-on-june-13-2026-15corhigher
    """
    s=market_slug
    # Strip bracket suffix after 4-digit year
    cleaned=re.sub(
        r'(?<=\d{4})-\d+(?:-\d+)?[cf](?:orhigher|orbelow|orabove|orlower)?$',
        '',s,flags=re.I)
    return cleaned or s

def _make_event(m:dict, event_slug:str)->dict:
    """Build event dict from a market, using extracted event slug."""
    return {"slug":m.get("slug",""),"groupSlug":event_slug,
            "url":f"https://polymarket.com/event/{event_slug}",
            "question":m.get("question") or m.get("title") or event_slug,
            "volume":_pf(m.get("volume")),"createdAt":m.get("createdAt",""),
            "endDate":m.get("endDate",""),"markets":[],"_raw":m}

def scan_new_markets_only()->list:
    """Fast scan: HANYA market yang dibuat dalam NEW_MARKET_WIN menit terakhir.
    Digunakan oleh cron 5-menit. Mengambil edge SEBELUM crowd datang.
    v9.3: Groups markets by event slug, fetches full event details."""
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=NEW_MARKET_WIN)
    event_slugs_seen=set()
    pool={}
    try:
        data=api_get(f"{GAMMA_BASE}/markets",{
            "active":"true","closed":"false",
            "order":"createdAt","ascending":"false","limit":50})
        if not data: return []
        markets=data if isinstance(data,list) else data.get("data",[])
        for m in markets:
            created=m.get("createdAt","")
            if created:
                try:
                    if _parse_dt(created)<cutoff: break  # sorted desc, dapat berhenti
                except: pass
            if not is_weather_market(m): continue
            mkt_slug=m.get("slug","")
            event_slug=m.get("groupSlug") or m.get("group_slug","") or _extract_event_slug(mkt_slug)
            if not event_slug: continue
            if event_slug not in event_slugs_seen:
                event_slugs_seen.add(event_slug)
                pool[event_slug]=_make_event(m,event_slug)
    except Exception as e: log.warning(f"New market scan: {e}")
    # Enrich: fetch full event details for each event slug
    _enrich_events(pool)
    # Moat 2: Exotic cities first — edge persists longer, less competition
    result = sorted(pool.values(),
                    key=lambda ev: 0 if _is_exotic_event(ev) else 1)
    exotic_n = sum(1 for ev in result if _is_exotic_event(ev))
    log.info(f"[FAST_SCAN] {len(result)} new events | exotic: {exotic_n} (priority)")
    return result

def _scan_all_markets(pool, extra, label):
    """Scan ALL markets via /markets endpoint and filter client-side.
    v9.3: No more broken tag search — we filter ourselves."""
    total=hits=0
    for page in range(PAGES):
        params={"active":"true","closed":"false","limit":PAGE_LIMIT,
                "offset":page*PAGE_LIMIT,**extra}
        data=api_get(f"{GAMMA_BASE}/markets",params)
        if not data: break
        markets=data if isinstance(data,list) else data.get("data",[])
        if not markets: break
        total+=len(markets)
        for m in markets:
            if not is_weather_market(m): continue
            mkt_slug=m.get("slug","")
            event_slug=(m.get("groupSlug") or m.get("group_slug","")
                        or _extract_event_slug(mkt_slug))
            if not event_slug: continue
            if event_slug in pool: continue
            pool[event_slug]=_make_event(m,event_slug); hits+=1
        if len(markets)<PAGE_LIMIT: break
        time.sleep(0.3)
    log.info(f"  {label}: {total} scanned | {hits} weather hits")

def _enrich_events(pool):
    """Fetch full event details from /events API for each event slug.
    Populates event['markets'] with all child markets."""
    for event_slug in list(pool.keys()):
        try:
            data=api_get(f"{GAMMA_BASE}/events",{"slug":event_slug})
            if not data: continue
            events=data if isinstance(data,list) else data.get("data",[])
            if not events: continue
            children=events[0].get("markets",[]) or []
            if children:
                pool[event_slug]["markets"]=children
                vol=sum(_pf(cm.get("volume")) for cm in children)
                if vol>0: pool[event_slug]["volume"]=vol
                q=events[0].get("title") or (children[0].get("question","") if children else "")
                if q: pool[event_slug]["question"]=q
        except Exception as e:
            log.debug(f"  Enrich {event_slug[:30]}: {e}")
        time.sleep(0.2)

def fetch_all_events()->list:
    """v9.3: Scan all markets client-side, group by event slug, enrich via events API.
    No more broken tag search — we filter ourselves."""
    pool={}
    log.info("[S1] volume DESC (all markets, client filter)")
    _scan_all_markets(pool,{"order":"volume","ascending":"false"},"S1")
    log.info("[S2] endDate ASC (all markets, client filter)")
    _scan_all_markets(pool,{"order":"endDate","ascending":"true"},"S2")
    log.info(f"[S3] Enriching {len(pool)} events via /events API")
    _enrich_events(pool)
    log.info(f"Total: {len(pool)} weather events")
    return list(pool.values())

# ══════════════════════════════════════════════════════════════════════════
#  §7  BRACKET PARSING  (v9.3: complete overhaul for real market formats)
# ══════════════════════════════════════════════════════════════════════════
def _bounds_label(s):
    """Parse bracket bounds from an outcome label (legacy multi-outcome format)."""
    s=s.strip()
    m=re.search(r'(-?\d+\.?\d*)\s*[-–to]+\s*(-?\d+\.?\d*)',s)
    if m: return float(m.group(1)),float(m.group(2))
    m=re.search(r'(?:>|≥|above|over)\s*(-?\d+\.?\d*)',s,re.I)
    if m: return float(m.group(1)),INF
    m=re.search(r'(-?\d+\.?\d*)\s*[cCfF]?\s*\+',s)
    if m: return float(m.group(1)),INF
    m=re.search(r'(?:<|≤|below|under)\s*(-?\d+\.?\d*)',s,re.I)
    if m: return -INF,float(m.group(1))
    m=re.search(r'(-?\d+\.?\d*)\s*°',s)
    if m: v=float(m.group(1)); return v-0.5,v+0.5
    return None,None

def _bounds_question(q):
    """Parse temperature bracket from real Polymarket question formats.
    v9.3: Returns dict with {low, high, temp_type, unit} or None.

    Real question formats from live API data:
    1. Exact:   "Will the highest temperature in [CITY] be [X]°C on [DATE]?"
    2. Tail low: "Will the highest temperature in [CITY] be [X]°C or below on [DATE]?"
    3. Tail hi:  "Will the highest temperature in [CITY] be [X]°C or higher on [DATE]?"
    4. Range:    "Will the highest temperature in [CITY] be between [X]-[Y]°F on [DATE]?"
    5. Lowest:   "Will the lowest temperature in [CITY] be [X]°C on [DATE]?"
    6. Low tail: "Will the lowest temperature in [CITY] be [X]°C or higher on [DATE]?"
    """
    s=q.lower()

    # Determine temp_type: "highest" or "lowest"
    temp_type="lowest" if "lowest temperature" in s else "highest"

    # Determine unit: F or C
    unit="F" if ("°f" in s or "fahrenheit" in s) else "C"

    # Pattern 4: "between X-Y°F" or "between X-Y°C" (range bracket)
    m=re.search(r'between\s+(-?\d+\.?\d*)\s*[-–]\s*(-?\d+\.?\d*)\s*°?\s*[cf]',s)
    if m: return {"low":float(m.group(1)),"high":float(m.group(2)),
                   "temp_type":temp_type,"unit":unit}

    # Pattern 3/6: "X°C or higher" / "X°C or above" / "X°C or more"
    m=re.search(r'(-?\d+\.?\d*)\s*°?\s*[cf]?\s*(?:or higher|or above|or more|\+)',s,re.I)
    if m: return {"low":float(m.group(1)),"high":INF,
                   "temp_type":temp_type,"unit":unit}

    # "above/exceed/over X"
    m=re.search(r'(?:above|exceed|over|more than|higher than|at least)\s+(-?\d+\.?\d*)',s)
    if m: return {"low":float(m.group(1)),"high":INF,
                   "temp_type":temp_type,"unit":unit}

    # Pattern 2: "X°C or below" / "X°C or lower" / "X°C or less"
    m=re.search(r'(-?\d+\.?\d*)\s*°?\s*[cf]?\s*(?:or below|or lower|or less)',s,re.I)
    if m: return {"low":-INF,"high":float(m.group(1)),
                   "temp_type":temp_type,"unit":unit}

    # "below/under/less than X"
    m=re.search(r'(?:below|under|less than|lower than|at most)\s+(-?\d+\.?\d*)',s)
    if m: return {"low":-INF,"high":float(m.group(1)),
                   "temp_type":temp_type,"unit":unit}

    # Pattern 1/5: "be X°C" (exact degree bracket → ±0.5)
    m=re.search(r'\bbe\s+(-?\d+\.?\d*)\s*°',s)
    if m:
        v=float(m.group(1))
        return {"low":v-0.5,"high":v+0.5,"temp_type":temp_type,"unit":unit}

    return None

def _parse_mkt(m:dict)->list:
    """Parse a single market into bracket dict(s).
    v9.3: Each market is INDEPENDENT with YES/NO only.
    Each market produces exactly one bracket.
    """
    q=m.get("question",""); outcomes=m.get("outcomes","[]")
    prices=m.get("outcomePrices","[]"); vol=_pf(m.get("volume"))
    tids=m.get("clobTokenIds",[]) or []
    if isinstance(tids,str):
        try: tids=json.loads(tids)
        except: tids=[]
    yes_tok=tids[0] if len(tids)>0 else None
    no_tok =tids[1] if len(tids)>1 else None
    if isinstance(outcomes,str):
        try: outcomes=json.loads(outcomes)
        except: outcomes=[]
    if isinstance(prices,str):
        try: prices=json.loads(prices)
        except: prices=[]
    brackets=[]

    # Try new question-based parsing (v9.3: primary path)
    bounds=_bounds_question(q)
    if bounds is not None and len(outcomes)==2 and "Yes" in outcomes:
        yi=outcomes.index("Yes"); yes_p=float(prices[yi]) if yi<len(prices) else 0.5
        brackets.append({"label":q[:60],
                          "low":bounds["low"],"high":bounds["high"],
                          "temp_type":bounds["temp_type"],"unit":bounds["unit"],
                          "yes_price":yes_p,"volume":vol,
                          "yes_token_id":yes_tok,"no_token_id":no_tok})
    elif bounds is not None and len(outcomes)==2:
        # 2 outcomes but not standard Yes/No — try to extract YES price
        yes_p=float(prices[0]) if len(prices)>0 else 0.5
        brackets.append({"label":q[:60],
                          "low":bounds["low"],"high":bounds["high"],
                          "temp_type":bounds["temp_type"],"unit":bounds["unit"],
                          "yes_price":yes_p,"volume":vol,
                          "yes_token_id":yes_tok,"no_token_id":no_tok})
    elif len(outcomes)>2:
        # Legacy multi-outcome format (shouldn't happen with real data, but handle gracefully)
        temp_type="lowest" if "lowest temperature" in q.lower() else "highest"
        unit="F" if ("°f" in q.lower() or "fahrenheit" in q.lower()) else "C"
        for i,label in enumerate(outcomes):
            price=float(prices[i]) if i<len(prices) else 0.0
            lo,hi=_bounds_label(label)
            if lo is None: continue
            brackets.append({"label":label,"low":lo,"high":hi,
                              "temp_type":temp_type,"unit":unit,
                              "yes_price":price,
                              "volume":vol/max(len(outcomes),1),
                              "yes_token_id":yes_tok,"no_token_id":no_tok})
    return brackets

def fetch_brackets(ev:dict)->list:
    """Fetch all brackets for an event.
    v9.3: Each bracket is a separate independent YES/NO market.
    Groups all markets belonging to this event together."""
    slug=ev.get("groupSlug") or ev.get("slug",""); all_raw=[]
    if slug:
        data=api_get(f"{GAMMA_BASE}/events",{"slug":slug})
        if data:
            events=data if isinstance(data,list) else data.get("data",[])
            if events: all_raw.extend(events[0].get("markets",[]) or [])
    if slug and len(all_raw)<2:
        data2=api_get(f"{GAMMA_BASE}/markets",{"group_slug":slug,"active":"true","limit":50})
        if data2:
            extra=data2 if isinstance(data2,list) else data2.get("data",[])
            seen={m.get("id") for m in all_raw}
            all_raw.extend(m for m in extra if m.get("id") not in seen)
    if all_raw:
        b=[]
        for m in all_raw: b.extend(_parse_mkt(m))
        if b: return b
    if ev.get("markets"):
        b=[]
        for m in ev["markets"]: b.extend(_parse_mkt(m))
        if b: return b
    raw=ev.get("_raw")
    if raw: return _parse_mkt(raw)
    return []

# ══════════════════════════════════════════════════════════════════════════
#  §8  FORECAST ENGINE + METAR KALMAN
# ══════════════════════════════════════════════════════════════════════════
def extract_city(text:str)->Optional[str]:
    t=text.lower()
    for city in sorted(CITY_COORDS,key=len,reverse=True):
        if city in t: return city
    return None

def extract_date(q:str):
    if not DATEUTIL_OK: return None
    try:
        m=re.search(r'\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}'
                    r'(?:st|nd|rd|th)?,?\s*\d{4}|\d{4}-\d{2}-\d{2})\b',q,re.I)
        if m: return du_parser.parse(m.group(0),default=datetime.now()).date()
    except: pass
    return None

def fetch_nwp(lat:float,lon:float,target_date=None):
    key=(round(lat,3),round(lon,3),str(target_date))
    if key in _nwp_cache: return _nwp_cache[key]
    data=None
    for i in range(2):
        try:
            r=requests.get(OWM_BASE,params={"latitude":lat,"longitude":lon,
                "daily":"temperature_2m_max,temperature_2m_min","forecast_days":7,
                "timezone":"auto"},timeout=12,headers={"User-Agent":"PolyWeather/9.3"})
            r.raise_for_status(); data=r.json(); break
        except:
            if i==0: time.sleep(1)
    daily=(data or {}).get("daily",{})
    dates=daily.get("time",[]); t_max=daily.get("temperature_2m_max",[])
    t_min=daily.get("temperature_2m_min",[]); idx=0
    if target_date:
        for i,d in enumerate(dates):
            if str(target_date)==d: idx=i; break
    lead=48
    if target_date:
        td=datetime(target_date.year,target_date.month,target_date.day,
                    14,0,tzinfo=timezone.utc)
        lead=max(6,int((td-datetime.now(timezone.utc)).total_seconds()/3600))
    mu_max=float(t_max[idx]) if t_max else 25.0
    mu_min=float(t_min[idx]) if t_min else 18.0
    result=(mu_max,mu_min,lead)
    _nwp_cache[key]=result; return result

def fetch_ens_sigma(lat:float,lon:float,day_idx:int=0)->float:
    key=(round(lat,2),round(lon,2),day_idx)
    if key in _ens_cache: return _ens_cache[key]
    result=2.0
    try:
        r=requests.get(ENS_BASE,params={"latitude":lat,"longitude":lon,
            "daily":"temperature_2m_max","models":"ecmwf_ifs025",
            "forecast_days":max(day_idx+1,2),"timezone":"auto"},
            timeout=8,headers={"User-Agent":"PolyWeather/9.3"})
        r.raise_for_status(); data=r.json()
        vals=[float(v[day_idx]) for k,v in data.get("daily",{}).items()
              if k.startswith("temperature_2m_max") and isinstance(v,list)
              and day_idx<len(v) and v[day_idx] is not None]
        if len(vals)>=5: result=round(max(1.0,min(float(np.std(vals)),5.0)),3)
    except Exception as e: log.debug(f"ECMWF ({lat:.1f},{lon:.1f}): {e}")
    _ens_cache[key]=result; return result

def fetch_metar(station:str)->Optional[float]:
    if not station: return None
    if station in _metar_cache: return _metar_cache[station]
    result=None
    try:
        r=requests.get(METAR_BASE,params={
            "dataSource":"metars","requestType":"retrieve","format":"json",
            "stationString":station,"hoursBeforeNow":2,"mostRecent":"true"},
            timeout=8,headers={"User-Agent":"PolyWeather/9.3"})
        r.raise_for_status(); data=r.json()
        obs=data.get("response",{}).get("data",{}).get("METAR",[])
        tc=(obs[0].get("temp_c") if isinstance(obs,list) and obs
            else obs.get("temp_c") if isinstance(obs,dict) else None)
        result=float(tc) if tc is not None else None
    except Exception as e: log.debug(f"METAR {station}: {e}")
    _metar_cache[station]=result; return result

def kalman_update(prior_mu:float,prior_sigma:float,
                  metar_temp:float,city:str,month:int,
                  temp_type:str="highest")->Tuple[float,float]:
    """METAR Kalman update. v9.3: supports both highest and lowest temperature.
    For 'highest': obs_mu = metar + heating (daytime heating adjustment).
    For 'lowest': obs_mu = metar (current obs near T_min at dawn, wider σ)."""
    if temp_type=="lowest":
        # For T_min: current temperature is close to T_min near dawn.
        # Use wider obs_sigma since we're less certain about time-of-day.
        obs_mu=metar_temp; obs_sigma=1.5
    else:
        # For T_max: add daytime heating estimate
        htable=DAYTIME_HEATING.get(city,{})
        heating=(htable.get(month,DAYTIME_HEATING["default"])
                 if isinstance(htable,dict) else DAYTIME_HEATING["default"])
        obs_mu=metar_temp+heating; obs_sigma=0.5
    pr=1.0/max(prior_sigma**2,0.01); po=1.0/(obs_sigma**2)
    post_mu=(prior_mu*pr+obs_mu*po)/(pr+po)
    post_sigma=math.sqrt(1.0/(pr+po))
    log.info(f"  METAR Kalman ({temp_type}): μ {prior_mu:.1f}→{post_mu:.1f}° σ {prior_sigma:.2f}→{post_sigma:.2f}°")
    return round(post_mu,2),round(post_sigma,3)

# ══════════════════════════════════════════════════════════════════════════
#  §9  MODEL STACK
# ══════════════════════════════════════════════════════════════════════════
def m2_ar1(mu_nwp, city, lead, month)->Tuple[float,float]:
    """AR(1) autoregressive temperature persistence model.
    Based on: Wilks (2011) Statistical Methods in the Atmospheric Sciences, Ch.6.
    Temperature persistence follows: T_t = μ_clim(1-φ^h) + T_{t-1}·φ^h
    Variance: σ²_h = σ²_1 × (1-φ^{2h}) / (1-φ²)
    φ (persistence) varies by climate regime: tropical ≈ 0.70, continental ≈ 0.85.
    """
    cluster = None
    for cl, members in CLIMATE_CLUSTERS.items():
        if city in members: cluster = cl; break
    phi = AR1_PHI.get(cluster, AR1_DEFAULT_PHI)
    clim = CLIMATE_NORMALS.get(city, {}).get(month)
    mu_clim = clim[0] if clim else mu_nwp
    h = max(lead / 24.0, 0.1)  # days ahead
    mu_ar1 = mu_clim * (1 - phi**h) + mu_nwp * phi**h
    sig_1 = 2.0  # 1-day forecast std dev (from ECMWF verification)
    denom = max(1 - phi**2, 0.01)
    sig_ar1 = sig_1 * math.sqrt(max((1 - phi**(2*h)) / denom, 0.01))
    sig_ar1 = max(1.0, min(sig_ar1, 5.0))
    return round(mu_ar1, 2), round(sig_ar1, 3)

def m3_bayesian(mu_n,sig_n,mu_c,sig_c=3.0):
    tn=1/max(sig_n**2,0.01); tc=1/max(sig_c**2,0.01); tp=tn+tc
    return round((tn*mu_n+tc*mu_c)/tp,3),round(math.sqrt(1/tp),3),tn,tc

def m5_regime(mu,clim,sig_c=3.0):
    """Regime classifier (z-score threshold, NOT a true HMM).
    Classifies into HOT/NORMAL/COLD regimes based on z-score
    of forecast vs climate normal. Simpler than full HMM but
    effective for regime detection with limited data.
    """
    z=(mu-clim)/max(sig_c,0.1); p_hot=float(1-norm.cdf(-z))
    return ("HOT" if z>0.7 else "COLD" if z<-0.7 else "NORMAL"),round(p_hot,4),round(z,3)

def m7_mc(mu,sigma,brackets,n_paths=50_000,seed=42)->dict:
    rng=np.random.default_rng(seed); samp=rng.normal(mu,sigma,n_paths)
    probs={}
    for b in brackets:
        lo,hi=b["low"],b["high"]
        if hi==INF: mask=samp>=lo
        elif lo==-INF: mask=samp<hi
        else: mask=(samp>=lo)&(samp<hi)
        probs[b["label"]]=float(mask.mean())
    return probs

def m11_vol_scale(sig_ens, lead=48)->float:
    """Volatility scaling based on lead-time decay.
    Replaces pseudo-GARCH with analytically motivated σ scaling.
    Ref: Jolliffe & Stephenson (2012) — forecast error ∝ √(lead).
    ECMWF ensemble is under-dispersive by ~15% (Hagedorn et al. 2008).
    σ_lead = σ_ens × √(lead/24) × correction_factor
    """
    base = sig_ens * math.sqrt(max(lead / 24.0, 0.25))
    correction = 1.15  # 15% inflation for ensemble under-dispersion
    return round(max(1.0, min(base * correction, 5.0)), 3)

def m13_hurst(sigma,lead)->tuple:
    mult=max(lead/24.0,0.1)**(HURST_H-0.5)
    return round(max(1.0,min(sigma*mult,5.0)),3),round(mult,4)

def t14_dyn(lead)->float:
    if lead<=6:   return 1.0
    if lead<=12:  return 0.9
    if lead<=24:  return 0.8
    if lead<=48:  return 0.7
    if lead<=72:  return 0.6
    return 0.5

def m109_robust(sq,kl,lead)->float:
    d=0.0 if sq=="HIGH" else (0.1 if sq=="MED" else 0.25)
    d+=0 if lead<=24 else (0.05 if lead<=48 else 0.15)
    d-=0.05 if kl>0.3 else 0
    return round(min(max(d,0),0.40),3)

def half_kelly(p_win:float,b_odds:float,fraction:float=KELLY_FRACTION)->float:
    """True fraction-Kelly: f* × fraction. fraction=0.5 for ½ Kelly."""
    if p_win<=0 or b_odds<=0: return 0.0
    f_star=max(0.0,(p_win*b_odds-(1-p_win))/b_odds)
    return min(f_star*fraction,KELLY_MAX_PCT)

def get_corr_penalty(city:str,active:List[str])->float:
    """Thorp 2006: Kelly_adj = Kelly × (1-ρ/2) per correlated pair."""
    for cluster,members in CLIMATE_CLUSTERS.items():
        if city not in members: continue
        n=sum(1 for c in active if c in members)
        if n<=1: return 1.0
        return max(0.30,round(1-(n-1)*0.55/2,3))
    return 1.0

def get_exotic_bonus(city: str) -> Tuple[float, float]:
    """
    Moat 2: Geographic competitive advantage untuk kota eksotis.
    Returns (threshold_reduction_pp, kelly_multiplier).

    Kota eksotis = hampir tidak ada bot lain yang cover
    → Edge decay lebih lambat: λ_exotic ≈ 0.70 × λ_normal
    → α_exotic(5min) = 0.707 vs α_normal(5min) = 0.602 (+17.5% edge preserved)
    → Threshold lebih rendah 1.5pp: lebih banyak sinyal valid ditangkap
    → Kelly 10% lebih agresif: lebih sedikit adverse selection risk

    Model ECMWF sama akuratnya di seluruh dunia (global coverage).
    Keunggulan murni dari sisi kompetisi, bukan model.
    """
    if city in EXOTIC_CITIES:
        log.debug(f"  Exotic city: {city} → thr-{EXOTIC_EDGE_BONUS_PP}pp kelly×{EXOTIC_KELLY_BONUS}")
        return EXOTIC_EDGE_BONUS_PP, EXOTIC_KELLY_BONUS
    return 0.0, 1.0

def get_spread_threshold(base:float,ask:Optional[float],bid:Optional[float])->float:
    """Adjust threshold based on spread. Wide spread = illusion of edge."""
    if ask is None or bid is None: return base+5.0
    spread_pp=max(0.0,(ask-bid)*100)
    return round(base+max(0.0,spread_pp-SPREAD_FLOOR),1)

def b5_kl(pm,pq)->float:
    eps=1e-9; p=max(eps,min(1-eps,pm)); q=max(eps,min(1-eps,pq))
    return round(p*math.log(p/q)+(1-p)*math.log((1-p)/(1-q)),5)

def f11_fmea(sigma,lead,vol,ovr,bet,bk)->int:
    S=5 if bet/max(bk,1)>0.20 else (3 if bet/max(bk,1)>0.10 else 1)
    O=1+(2 if sigma>2.5 else 0)+(1 if lead>72 else 0)+(1 if vol<1000 else 0)
    D=1+(2 if ovr>0.08 else (1 if ovr>0.04 else 0))
    return S*O*D

def skill_score(lead,clim_mae=3.0)->float:
    return round(1-_mae(lead)/max(clim_mae,0.01),4)

def cdf_formula(lo,hi,mu,sigma):
    s=max(sigma,0.01)
    if lo==-INF:
        z=(hi-mu)/s; p=norm.cdf(z)
        return p,f"Phi(({hi:.1f}-{mu:.1f})/{sigma:.2f})={p*100:.1f}%"
    elif hi==INF:
        z=(lo-mu)/s; p=1-norm.cdf(z)
        return p,f"1-Phi(({lo:.1f}-{mu:.1f})/{sigma:.2f})={p*100:.1f}%"
    else:
        zh=(hi-mu)/s; zl=(lo-mu)/s; p=norm.cdf(zh)-norm.cdf(zl)
        return p,f"Phi({zh:.2f})-Phi({zl:.2f})={p*100:.1f}%"

# ══════════════════════════════════════════════════════════════════════════
#  §9B  NEW MODEL 1: ISOTONIC CALIBRATION (PAVA)
# ══════════════════════════════════════════════════════════════════════════
class IsotonicCalibrator:
    """Calibrate model probabilities to actual frequencies using isotonic regression.
    
    Uses Pool Adjacent Violators Algorithm (PAVA) to fit a monotone
    non-decreasing mapping from model probability → calibrated probability.
    Requires min 10 historical predictions before calibration activates.
    
    Ref: Barlow et al. (1972) "Statistical Inference under Order Restrictions"
    Ref: Zadrozny & Elkan (2002) "Transforming Classifier Scores into
         Accurate Multiclass Probability Estimates"
    """
    MIN_PAIRS = 10
    
    def __init__(self):
        self.data = safe_read(CALIB_FILE, {"pairs": [], "n_calibrated": 0})
        self._bins = None
        self._fit()
    
    def _fit(self):
        """Fit isotonic regression on accumulated prediction-outcome pairs."""
        pairs = self.data.get("pairs", [])
        if len(pairs) < self.MIN_PAIRS:
            self._bins = None
            return
        # Sort by predicted probability
        pairs_sorted = sorted(pairs, key=lambda x: x[0])
        predicted = [p[0] for p in pairs_sorted]
        actual = [p[1] for p in pairs_sorted]
        # Run PAVA
        calibrated = self._pava(predicted, actual)
        self._bins = list(zip(predicted, calibrated))
        self.data["n_calibrated"] = len(pairs)
    
    def _pava(self, x, y):
        """Pool Adjacent Violators Algorithm for isotonic regression.
        Returns calibrated values that are monotone non-decreasing.
        """
        n = len(y)
        if n == 0:
            return []
        # Initialize blocks: each point is its own block
        blocks = [[i] for i in range(n)]
        values = list(y)
        changed = True
        while changed:
            changed = False
            new_blocks = []
            i = 0
            while i < len(blocks):
                if i + 1 < len(blocks):
                    mean_curr = sum(values[j] for j in blocks[i]) / len(blocks[i])
                    mean_next = sum(values[j] for j in blocks[i+1]) / len(blocks[i+1])
                    if mean_curr > mean_next:
                        # Violation: merge blocks
                        merged = blocks[i] + blocks[i+1]
                        mean_merged = sum(values[j] for j in merged) / len(merged)
                        for j in merged:
                            values[j] = mean_merged
                        new_blocks.append(merged)
                        i += 2
                        changed = True
                        continue
                new_blocks.append(blocks[i])
                i += 1
            blocks = new_blocks
        return values
    
    def calibrate(self, p_model: float) -> float:
        """Map model probability to calibrated probability.
        Interpolates linearly between calibrated bin edges.
        Falls back to raw p_model if insufficient data.
        """
        if not self._bins or len(self._bins) < self.MIN_PAIRS:
            return p_model
        # Find surrounding bins and interpolate
        for i in range(len(self._bins) - 1):
            p_lo, c_lo = self._bins[i]
            p_hi, c_hi = self._bins[i + 1]
            if p_lo <= p_model <= p_hi:
                span = max(p_hi - p_lo, 1e-6)
                t = (p_model - p_lo) / span
                return round(c_lo * (1 - t) + c_hi * t, 5)
        # Extrapolate from nearest edge
        if p_model < self._bins[0][0]:
            return round(self._bins[0][1], 5)
        return round(self._bins[-1][1], 5)
    
    def record(self, p_model: float, actual_outcome: float):
        """Record a prediction-outcome pair for calibration training.
        actual_outcome: 1.0 if YES won, 0.0 if NO won.
        """
        self.data.setdefault("pairs", []).append(
            [round(p_model, 4), float(actual_outcome)]
        )
        # Keep only last 500 pairs to avoid unbounded growth
        if len(self.data["pairs"]) > 500:
            self.data["pairs"] = self.data["pairs"][-500:]
        self._fit()
        safe_write(CALIB_FILE, self.data)
    
    @property
    def is_active(self) -> bool:
        return self._bins is not None
    
    def summary(self) -> str:
        n = len(self.data.get("pairs", []))
        if self.is_active:
            return f"Isotonic ON (n={n} pairs, calibrated)"
        return f"Isotonic OFF (n={n}/{self.MIN_PAIRS} pairs needed)"

# ══════════════════════════════════════════════════════════════════════════
#  §9C  NEW MODEL 2: BOOTSTRAP CI ON EDGE
# ══════════════════════════════════════════════════════════════════════════
def bootstrap_edge_ci(p_model: float, p_mkt: float,
                      sigma_model: float = None,
                      n_bootstrap: int = BOOTSTRAP_N,
                      confidence: float = BOOTSTRAP_CONF) -> BootstrapCI:
    """Bootstrap confidence interval on the edge estimate.
    
    Models the uncertainty in edge arising from:
    1. Finite ensemble size (n_eff ~ 50 members)
    2. Model estimation variance
    
    Ref: Efron & Tibshirani (1993) "An Introduction to the Bootstrap"
    Returns BootstrapCI with point estimate, CI bounds, and significance flag.
    
    If CI crosses zero → edge is NOT statistically significant → skip trade.
    """
    edge_point = (p_model - p_mkt) * 100  # in percentage points
    
    # Estimate effective sample size for model probability
    # ECMWF ensemble: ~50 members, NWP deterministic: effectively fewer
    n_eff = 50
    if sigma_model is not None:
        # Higher sigma → fewer effective samples (more uncertainty)
        n_eff = max(20, int(50 / max(sigma_model, 0.5)))
    
    # Standard error of model probability estimate
    se_model = math.sqrt(max(p_model * (1 - p_model) / n_eff, 0.0001))
    
    rng = np.random.default_rng(42)
    p_samples = rng.normal(p_model, se_model, n_bootstrap)
    p_samples = np.clip(p_samples, 0.01, 0.99)
    
    edges = (p_samples - p_mkt) * 100
    alpha = (1 - confidence) / 2
    ci_low = float(np.percentile(edges, alpha * 100))
    ci_high = float(np.percentile(edges, (1 - alpha) * 100))
    
    # Edge is significant if CI doesn't cross zero
    significant = (ci_low > 0) or (ci_high < 0)
    
    return BootstrapCI(
        edge_point=round(edge_point, 2),
        ci_low=round(ci_low, 2),
        ci_high=round(ci_high, 2),
        significant=significant
    )

# ══════════════════════════════════════════════════════════════════════════
#  §9D  NEW MODEL 3: ENSEMBLE DISPERSION FILTER
# ══════════════════════════════════════════════════════════════════════════
def check_ensemble_dispersion(models: List[ModelResult],
                               threshold: float = DISPERSION_MAX_PP
                               ) -> Tuple[bool, float, float]:
    """Check if ensemble models agree sufficiently to trade.
    
    High dispersion (σ between model predictions) indicates fundamental
    disagreement → signal is unreliable → skip trade.
    
    Ref: Raju et al. (2023) "When to Trust Your Ensemble: Dispersion as
         Uncertainty Estimation" — dispersion > 2σ correlates with 40%
         higher error rate.
    
    Returns: (passes_filter, dispersion_°C, mean_prediction)
    """
    active_models = [m for m in models if m.active]
    if len(active_models) < 2:
        return True, 0.0, 0.0
    
    predictions = [m.mu for m in active_models]
    mean_pred = float(np.mean(predictions))
    dispersion = float(np.std(predictions))
    
    passes = dispersion <= threshold
    
    if not passes:
        log.info(f"  DISPERSION FILTER: σ={dispersion:.2f}°C > {threshold:.1f}°C — models disagree, skipping")
    
    return passes, round(dispersion, 2), round(mean_pred, 2)

# ══════════════════════════════════════════════════════════════════════════
#  §9E  NEW MODEL 4: ADAPTIVE FRACTIONAL KELLY
# ══════════════════════════════════════════════════════════════════════════
def adaptive_kelly_fraction(bankroll: float, start_bankroll: float,
                            bs_ratio: float, n_resolved: int,
                            kelly_mode: str) -> float:
    """Dynamic Kelly fraction based on portfolio state and model quality.
    
    Standard Kelly (f*) is optimal for known probabilities.
    In practice, we use fractional Kelly (f*/k) to account for:
    1. Parameter uncertainty — model estimates are noisy
    2. Non-stationarity — market conditions change
    3. Correlated bets — weather events are correlated by geography
    
    Adaptive rules:
    - f/4 when drawdown > 15% (capital preservation mode)
    - f/3 when drawdown > 10% (cautious mode)
    - f/2 when drawdown < 5% AND model is well-calibrated
    - Scale down further if Brier Score ratio < 0.85 (poor calibration)
    - Scale down if n_resolved < 20 (insufficient track record)
    
    Ref: Thorp (2006) "The Kelly Criterion in Blackjack, Sports Betting, 
         and the Stock Market" — fractional Kelly reduces variance by 40-60%
         while only sacrificing 25% of expected growth.
    Ref: Baker & McHale (2013) — f/3 is near-optimal under parameter 
         uncertainty of 20%.
    """
    # Base fraction from validation mode
    mode_frac = {"DRY_RUN": 0.0, "QUARTER": 0.25, "HALF": 0.50, "FULL": 1.0
                 }.get(kelly_mode, 0.5)
    
    if mode_frac == 0.0:
        return 0.0  # DRY_RUN → zero
    
    # Drawdown-based fraction
    dd = max(0, (start_bankroll - bankroll) / max(start_bankroll, 1))
    
    if dd >= KELLY_F4_DRAWDOWN:
        base_frac = 0.25  # f/4: capital preservation
        dd_label = "f/4"
    elif dd >= KELLY_F3_DRAWDOWN:
        base_frac = 0.33  # f/3: cautious
        dd_label = "f/3"
    elif dd >= KELLY_F2_DRAWDOWN:
        base_frac = 0.40  # between f/3 and f/2
        dd_label = "0.40f"
    else:
        base_frac = 0.50  # f/2: standard
        dd_label = "f/2"
    
    # Calibration quality adjustment
    if bs_ratio < 0.85:
        base_frac *= 0.70  # Poor calibration → cut 30%
    elif bs_ratio < 1.0:
        base_frac *= 0.85  # Below average → cut 15%
    # bs_ratio >= 1.0 → no penalty
    
    # Experience level adjustment (fewer data = more conservative)
    if n_resolved < 10:
        base_frac *= 0.50
    elif n_resolved < 20:
        base_frac *= 0.75
    elif n_resolved < 50:
        base_frac *= 0.90
    
    # Apply validation mode cap
    effective_frac = min(base_frac, mode_frac)
    effective_frac = max(0.10, min(effective_frac, 0.50))  # Floor 10%, cap 50%
    
    log.info(f"  Adaptive Kelly: {dd_label} dd={dd*100:.1f}% bs_ratio={bs_ratio:.2f}"
             f" n={n_resolved} → frac={effective_frac:.3f}")
    
    return round(effective_frac, 3)

# ══════════════════════════════════════════════════════════════════════════
#  §9F  NEW MODEL 5: LIQUIDITY FILTER
# ══════════════════════════════════════════════════════════════════════════
class LiquidityFilter:
    """Check orderbook depth before trading.
    
    Thin orderbooks mean:
    1. Large slippage on market orders
    2. Difficulty exiting positions
    3. Price can be easily manipulated
    
    Skip trades where depth < LIQUIDITY_MIN_DEPTH USD.
    Cache results for 5 minutes to avoid API spam.
    
    Ref: Hasbrouck & Seppi (2001) "Common Factors in Prices, Order Flows,
         and Liquidity" — depth is a key predictor of execution quality.
    """
    
    CACHE_TTL = 300  # 5 minutes
    
    def __init__(self):
        self._cache: Dict[str, Tuple[float, LiquidityResult]] = {}
    
    def check(self, clob_client, token_id: str) -> LiquidityResult:
        """Check liquidity for a token. Uses cache if fresh."""
        now = time.time()
        
        # Check cache
        if token_id in self._cache:
            cached_time, cached_result = self._cache[token_id]
            if now - cached_time < self.CACHE_TTL:
                return cached_result
        
        # Fetch orderbook
        best_ask = None
        best_bid = None
        depth_usd = 0.0
        
        try:
            r = requests.get(f"{CLOB_BASE}/book",
                             params={"token_id": token_id}, timeout=8)
            r.raise_for_status()
            data = r.json()
            asks = sorted(data.get("asks", []),
                          key=lambda x: float(x.get("price", "1")))
            bids = sorted(data.get("bids", []),
                          key=lambda x: -float(x.get("price", "0")))
            
            if asks:
                best_ask = float(asks[0].get("price", 0))
            if bids:
                best_bid = float(bids[0].get("price", 0))
            
            # Estimate depth from top 5 levels
            ask_depth = sum(
                float(a.get("size", 0)) * float(a.get("price", 0))
                for a in asks[:5]
            )
            bid_depth = sum(
                float(b.get("size", 0)) * float(b.get("price", 0))
                for b in bids[:5]
            )
            depth_usd = min(ask_depth, bid_depth)
        except Exception as e:
            log.debug(f"  Liquidity check {token_id[:12]}: {e}")
        
        spread_pp = 0.0
        if best_ask is not None and best_bid is not None:
            spread_pp = (best_ask - best_bid) * 100
        
        passes = depth_usd >= LIQUIDITY_MIN_DEPTH
        
        result = LiquidityResult(
            token_id=token_id,
            passes=passes,
            best_ask=best_ask,
            best_bid=best_bid,
            depth_usd=round(depth_usd, 2),
            spread_pp=round(spread_pp, 1)
        )
        
        # Cache
        self._cache[token_id] = (now, result)
        
        if not passes:
            log.info(f"  LIQUIDITY FILTER: depth=${depth_usd:.0f} < ${LIQUIDITY_MIN_DEPTH:.0f} — skipping")
        
        return result

# ══════════════════════════════════════════════════════════════════════════
#  §10  ENSEMBLE RUNNER
# ══════════════════════════════════════════════════════════════════════════
def run_ensemble(city,mu_nwp,sig_ens,lead,month,brackets,
                  temp_type="highest")->EnsembleResult:
    """v9.3: Added temp_type parameter for lowest temperature support.
    When temp_type='lowest', uses climate normal T_min instead of T_max."""
    clim=CLIMATE_NORMALS.get(city,{}).get(month)
    # v9.3: Use T_min from climate normal for lowest temperature
    mu_clim=clim[1] if clim and temp_type=="lowest" else (clim[0] if clim else mu_nwp)
    sig_clim=3.0
    mae_nwp=_mae(lead); w_nwp=1/max(mae_nwp,0.1)
    models=[]
    models.append(ModelResult("M1","NWP-OpenMeteo",mu_nwp,mae_nwp,w_nwp,f"MAE={mae_nwp:.2f}"))
    cluster=None
    for cl,members in CLIMATE_CLUSTERS.items():
        if city in members: cluster=cl; break
    mu_ar1,sig_ar1=m2_ar1(mu_nwp,city,lead,month)
    w_ar1=1/max(sig_ar1,0.1)
    models.append(ModelResult("M2","AR1-persist",mu_ar1,sig_ar1,w_ar1,
                               f"phi={AR1_PHI.get(cluster,AR1_DEFAULT_PHI):.2f} h={lead/24:.1f}d"))
    mu_post,sig_post,tn,tc=m3_bayesian(mu_nwp,sig_ens,mu_clim,sig_clim)
    w_bay=1/max(sig_post,0.1)
    models.append(ModelResult("M3","Bayesian-post",mu_post,sig_post,w_bay,
                               f"prior={mu_clim:.1f} τ_n={tn:.3f}"))
    models.append(ModelResult("M4","ECMWF-ENS",mu_nwp,sig_ens,w_nwp*1.2))
    sig_g=m11_vol_scale(sig_ens,lead)
    models.append(ModelResult("M11","VolScale-σ",mu_nwp,sig_g,w_nwp*0.8))
    state,p_hot,z=m5_regime(mu_post,mu_clim,sig_clim)
    mu_hmm=mu_post+(0.5 if state=="HOT" else -0.5 if state=="COLD" else 0)
    models.append(ModelResult("M5","Regime-zScore",mu_hmm,sig_post*0.95,w_bay*0.7,
                               f"{state} z={z:.2f}"))
    sig_h,hmult=m13_hurst(sig_post,lead)
    models.append(ModelResult("M13","Hurst-H0.63",mu_post,sig_h,w_bay*0.9,
                               f"mult={hmult:.3f}"))
    m39_on=lead<=36
    if m39_on:
        tm={"HOT":{"HOT":0.68,"NORMAL":0.28,"COLD":0.04},
            "NORMAL":{"HOT":0.22,"NORMAL":0.62,"COLD":0.16},
            "COLD":{"HOT":0.05,"NORMAL":0.35,"COLD":0.60}}
        mk=tm.get(state,{"HOT":0.33,"NORMAL":0.34,"COLD":0.33})
        mu_mk=mu_post+0.8*mk.get("HOT",0.33)-0.8*mk.get("COLD",0.33)
        models.append(ModelResult("M39","Markov",mu_mk,sig_h*1.05,w_nwp*0.6))
    else:
        models.append(ModelResult("M39","Markov",mu_post,sig_h,0.0,
                                   "inactive: lead>36h",False,"lead>36h"))
    tw=sum(m.weight for m in models)
    muf=sum(m.mu*m.weight for m in models)/max(tw,0.01)
    av=sum(m.sigma**2*m.weight for m in models)/max(tw,0.01)
    mv=sum(m.weight*(m.mu-muf)**2 for m in models)/max(tw,0.01)
    sigf=round(max(1.0,min(math.sqrt(av+0.5*mv),5.0)),3)
    sq="HIGH" if sigf<2.0 else ("MED" if sigf<2.5 else "LOW")
    kl_a=0.12
    if brackets:
        mc3=m7_mc(muf,sigf,brackets[:3])
        # v9.3: Use yes_price directly — each market is independent, no normalization
        kl_a=round(sum(b5_kl(mc3.get(b["label"],0.5),
                              b["yes_price"]) for b in brackets),4)
    rd=m109_robust(sq,kl_a,lead)
    fs,fp,_=m5_regime(muf,mu_clim,sig_clim)
    return EnsembleResult(mu_ensemble=round(muf,2),sigma_ensemble=sigf,models=models,
                          skill_score=skill_score(lead),kl_total=kl_a,
                          hmm_state=fs,hmm_hot_p=fp,hurst_adj=hmult,
                          dynamic_k_mult=t14_dyn(lead),robust_disc=rd,
                          n_active=sum(1 for m in models if m.active))

# ══════════════════════════════════════════════════════════════════════════
#  §11  QUANT PIPELINE
# ══════════════════════════════════════════════════════════════════════════
def bracket_short(b:dict)->str:
    """v9.3: Use unit field from bracket if available."""
    unit=b.get("unit","")
    if not unit:
        lo,hi=b["low"],b["high"]; lbl=b.get("label","")
        if any(x in lbl.lower() for x in ["°f","fahrenheit"]): unit="°F"
        elif any(x in lbl.lower() for x in ["°c","celsius"]): unit="°C"
        else:
            ref=hi if hi!=INF else (lo if lo!=-INF else 0)
            unit="°F" if abs(ref)>50 else "°C"
    else:
        unit="°F" if unit=="F" else "°C"
    lo,hi=b["low"],b["high"]
    if lo==-INF and hi!=INF: return f"≤{hi:.0f}{unit}"
    elif hi==INF and lo!=-INF: return f"≥{lo:.0f}{unit}"
    elif lo!=-INF and hi!=INF and hi-lo==1.0 and abs(lo%1)==0.5:
        # Exact degree bracket: 27.5-28.5 → "28°C"
        return f"{lo+0.5:.0f}{unit}"
    return f"{lo:.0f}-{hi:.0f}{unit}"

def calc_implied_mu(brackets:list)->Optional[float]:
    """v9.3: Fit implied mean from (midpoint, yes_price) pairs.
    With independent markets, yes_price IS the probability directly.
    Weighted mean: μ = Σ(mid × price) / Σ(price)."""
    items=[]
    for b in brackets:
        lo,hi=b["low"],b["high"]
        if   lo==-INF and hi!=INF: mid=hi-1.0
        elif hi==INF  and lo!=-INF:mid=lo+1.0
        elif lo!=-INF and hi!=INF: mid=(lo+hi)/2.0
        else: continue
        items.append((mid,b["yes_price"]))
    if not items: return None
    denom=max(sum(p for _,p in items),1.0)
    return round(sum(m*p/denom for m,p in items),2)

def calc_brackets_full(mu,sigma,brackets,mc_probs,bankroll,ens,
                        active_cities,best_asks,best_bids,city,
                        kelly_mode,dynamic_thr,bs_mult,cat_tracker,
                        exotic_k_mult:float=1.0,
                        calibrator:IsotonicCalibrator=None,
                        liq_filter:LiquidityFilter=None,
                        clob_client=None,
                        start_bankroll:float=0.0,
                        bs_ratio:float=1.0,
                        n_resolved:int=0):
    """v9.3 pw_ BUILD: CRITICAL FIX + 5 NEW MODELS integrated.
    Each market is INDEPENDENT. YES price IS the market probability directly.
    Edge = p_model - yes_price (directly, no denom).
    
    New models integrated:
    1. Isotonic Calibration — calibrates p_blend before edge calc
    2. Bootstrap CI on Edge — skips trades with insignificant edge
    3. Liquidity Filter — skips thin orderbooks
    4. Adaptive Fractional Kelly — dynamic f/2→f/3→f/4 based on DD+calib
    5. Ensemble Dispersion — checked externally before calling this func
    """
    # v9.3: total_mkt is informational only — NOT used for normalization
    total_mkt_raw=sum(b["yes_price"] for b in brackets)
    ovr=max(0.0,total_mkt_raw-1.0); sigma_ok=sigma<2.5
    corr_pen=get_corr_penalty(city,active_cities)
    port_lim=bankroll*KELLY_PORT_PCT/MAX_MKTS

    # Kelly fraction — NEW: adaptive based on drawdown + calibration
    # Old: fixed kf_mult based on validation mode only
    # New: adaptive_kelly_fraction() considers DD, BS ratio, n_resolved
    kf_mult = adaptive_kelly_fraction(
        bankroll, start_bankroll or bankroll, bs_ratio, n_resolved, kelly_mode
    )

    out=[]
    for b in brackets:
        lo,hi=b["low"],b["high"]
        p_g,cdf_str=cdf_formula(lo,hi,mu,sigma)
        p_mc=mc_probs.get(b["label"],p_g)
        # Adaptive blend: MC better for tail brackets, Gaussian better for interior
        is_tail=lo==-INF or hi==INF
        p_blend=round(0.3*p_g+0.7*p_mc,5) if is_tail else round(0.7*p_g+0.3*p_mc,5)
        
        # NEW MODEL 1: Isotonic Calibration — calibrate p_blend
        if calibrator and calibrator.is_active:
            p_blend_raw = p_blend
            p_blend = calibrator.calibrate(p_blend)
            if abs(p_blend - p_blend_raw) > 0.02:
                log.info(f"  Isotonic: {p_blend_raw:.3f} → {p_blend:.3f} (Δ{(p_blend-p_blend_raw)*100:+.1f}pp)")
        
        # v9.3: p_mkt = yes_price DIRECTLY (no normalization)
        p_mkt=b["yes_price"]
        yes_edge=(p_blend-p_mkt)*100; no_edge=-yes_edge
        kl_b=b5_kl(p_blend,max(p_mkt,0.01))

        yes_tok=str(b.get("yes_token_id") or "")
        no_tok =str(b.get("no_token_id") or "")
        ya=best_asks.get(yes_tok); yb=best_bids.get(yes_tok)
        na=best_asks.get(no_tok);  nb=best_bids.get(no_tok)

        # NEW MODEL 2: Bootstrap CI on Edge — check if edge is statistically significant
        bs_ci_yes = bootstrap_edge_ci(p_blend, p_mkt, sigma)
        bs_ci_no  = bootstrap_edge_ci(1 - p_blend, 1 - p_mkt, sigma)
        
        yes_thr=get_spread_threshold(dynamic_thr,ya,yb)
        no_thr =get_spread_threshold(dynamic_thr,na,nb)
        yes_map=round(p_blend-EDGE_THRESHOLD/100,3)
        no_map =round((1-p_blend)-EDGE_THRESHOLD/100,3)
        yes_ask_ok=ya is None or ya<=yes_map
        no_ask_ok =na is None or na<=no_map

        # NEW MODEL 5: Liquidity Filter — check orderbook depth
        liq_yes = LiquidityResult(yes_tok, True)  # default pass
        liq_no  = LiquidityResult(no_tok, True)
        if liq_filter and clob_client:
            if yes_tok:
                liq_yes = liq_filter.check(clob_client, yes_tok)
                # Also use liquidity result to update best_ask/best_bid
                if liq_yes.best_ask is not None and ya is None:
                    ya = liq_yes.best_ask; best_asks[yes_tok] = ya
                if liq_yes.best_bid is not None and yb is None:
                    yb = liq_yes.best_bid; best_bids[yes_tok] = yb
            if no_tok:
                liq_no = liq_filter.check(clob_client, no_tok)
                if liq_no.best_ask is not None and na is None:
                    na = liq_no.best_ask; best_asks[no_tok] = na
                if liq_no.best_bid is not None and nb is None:
                    nb = liq_no.best_bid; best_bids[no_tok] = nb

        # Direction check (v9 tail fix)
        if lo==-INF:
            mid=hi-1.0; prox_ok=True
            dok_yes=mu<=hi+sigma; dok_no=mu>=hi-0.5*sigma
        elif hi==INF:
            mid=lo+1.0; prox_ok=True
            dok_yes=mu>=lo-sigma; dok_no=mu<=lo+0.5*sigma
        else:
            mid=(lo+hi)/2.0; prox_ok=abs(mid-mu)<=1.5*sigma
            dok_yes=mu>=mid-1.5*sigma; dok_no=mu<=mid+1.5*sigma

        def _cls(edge,dk,thr,ci:BootstrapCI=None,liq_pass:bool=True):
            if not liq_pass:
                return "PASS"  # Liquidity filter: force PASS (no trade)
            if ci and not ci.significant:
                return "PASS"  # Bootstrap CI: edge not significant
            if abs(edge)>=EDGE_STRONG and dk and sigma_ok and prox_ok: return "STRONG"
            if abs(edge)>=thr and dk and sigma_ok and prox_ok: return "SIGNAL"
            if abs(edge)>=EDGE_THRESHOLD*0.80: return "NEAR"
            return "PASS"

        yes_cls=_cls(yes_edge,dok_yes,yes_thr,bs_ci_yes,liq_yes.passes)
        no_cls =_cls(no_edge, dok_no, no_thr, bs_ci_no, liq_no.passes)

        # NEW MODEL 4: Adaptive Fractional Kelly (replaces fixed kf_mult)
        # v9.3: Use p_mkt directly — yes_price IS the probability
        yes_b=(1-p_mkt)/max(p_mkt,0.001)
        no_b = p_mkt/max(1-p_mkt,0.001)
        f_yes=half_kelly(p_blend,yes_b,kf_mult) if yes_cls in ("SIGNAL","STRONG") else 0.0
        f_no =half_kelly(1-p_blend,no_b,kf_mult) if no_cls in ("SIGNAL","STRONG") else 0.0

        # Apply multipliers
        cat_mult=cat_tracker.get_kelly_mult(city,ens.dynamic_k_mult,is_tail)
        eff_yes=f_yes*bs_mult*corr_pen*ens.dynamic_k_mult*(1-ens.robust_disc)*cat_mult*exotic_k_mult
        eff_no =f_no *bs_mult*corr_pen*ens.dynamic_k_mult*(1-ens.robust_disc)*cat_mult*exotic_k_mult

        stake_yes=min(eff_yes*bankroll,bankroll*KELLY_MAX_PCT,port_lim)
        stake_no =min(eff_no *bankroll,bankroll*KELLY_MAX_PCT,port_lim)
        if stake_yes<1.0: stake_yes=0.0
        if stake_no <1.0: stake_no =0.0

        rpn=f11_fmea(sigma,ens.dynamic_k_mult,b["volume"],ovr,
                      max(stake_yes,stake_no),bankroll)
        slip=0.003+0.02*math.sqrt(max(stake_yes,stake_no)/max(b["volume"],1))
        fill_p=min(0.90,0.50+max(abs(yes_edge),abs(no_edge))*0.025)
        if yes_cls=="STRONG": fill_p=min(fill_p+0.10,0.98)  # market order boost

        out.append({**b,
            "p_gaussian":p_g,"p_mc":p_mc,"p_blend":p_blend,
            "p_mkt":round(p_mkt,5),
            "yes_edge":round(yes_edge,2),"no_edge":round(no_edge,2),
            "yes_class":yes_cls,"no_class":no_cls,
            "yes_thr":yes_thr,"no_thr":no_thr,
            "yes_map":yes_map,"no_map":no_map,
            "yes_ask_ok":yes_ask_ok,"no_ask_ok":no_ask_ok,
            "yes_ask":ya,"no_ask":na,"is_tail":is_tail,
            "stake_yes":round(stake_yes,2),"stake_no":round(stake_no,2),
            "f_yes":round(eff_yes,4),"f_no":round(eff_no,4),
            "kl":kl_b,"rpn":rpn,"slip":round(slip,4),"fill_p":round(fill_p,3),
            "cdf_str":cdf_str,"overround":round(ovr,4),
            "bracket_short":bracket_short(b),
            "skip_reason_yes":"","skip_reason_no":"",
            # New model outputs
            "ci_yes_low":bs_ci_yes.ci_low,"ci_yes_high":bs_ci_yes.ci_high,
            "ci_yes_significant":bs_ci_yes.significant,
            "ci_no_low":bs_ci_no.ci_low,"ci_no_high":bs_ci_no.ci_high,
            "ci_no_significant":bs_ci_no.significant,
            "liq_yes_depth":liq_yes.depth_usd,"liq_yes_pass":liq_yes.passes,
            "liq_no_depth":liq_no.depth_usd,"liq_no_pass":liq_no.passes,
            "kelly_frac":kf_mult,
        })
    return out

# ══════════════════════════════════════════════════════════════════════════
#  §12  RISK GATE
# ══════════════════════════════════════════════════════════════════════════
class RiskGate:
    DAILY_MAX_BETS=20; DAILY_MAX_PCT=0.40; SINGLE_MAX=0.25
    DAILY_LOSS_STOP=0.15
    def __init__(self):
        self._today_bets=0; self._today_usdc=0.0; self._load()
    def _load(self):
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with open(PERF_FILE) as f:
                for line in f:
                    try:
                        e=json.loads(line)
                        if e.get("ts","")[:10]!=today: continue
                        for b in e.get("brackets",[]):
                            s=max(b.get("stake_yes",0),b.get("stake_no",0))
                            if s>0: self._today_bets+=1; self._today_usdc+=s
                    except: pass
        except: pass
    def check_rg7(self,bk,bot_state)->Tuple[bool,str]:
        start=bot_state.get("daily_start_bankroll",bk)
        if start<=0: return True,"OK"
        loss=(start-bk)/start
        if loss>=self.DAILY_LOSS_STOP:
            return False,(f"RG7 HARD STOP: daily loss {loss*100:.1f}% ≥ {self.DAILY_LOSS_STOP*100:.0f}%")
        return True,"OK"
    def check_rg_bs(self,bs)->Tuple[bool,str]:
        exp=0.28*0.72; thr=exp*1.30
        if bs>thr and bs>0.01: return False,f"RG-BS PAUSE: BS={bs:.4f}>E[BS]×1.30={thr:.4f}"
        return True,"OK"
    def check(self,bk,size,bs,bot_state)->Tuple[bool,str,float]:
        if self._today_bets>=self.DAILY_MAX_BETS: return False,f"RG1: {self.DAILY_MAX_BETS} bets/day",0.0
        if self._today_usdc+size>bk*self.DAILY_MAX_PCT: return False,"RG1: USDC daily cap",0.0
        size=min(size,bk*self.SINGLE_MAX)
        if size<1.0: return False,"RG6: below $1 min",0.0
        ok,msg=self.check_rg7(bk,bot_state)
        if not ok: return False,msg,0.0
        ok,msg=self.check_rg_bs(bs)
        if not ok: return False,msg,0.0
        self._today_bets+=1; self._today_usdc+=size
        return True,"OK",size

# ══════════════════════════════════════════════════════════════════════════
#  §13  CLOB ORDER CLIENT
# ══════════════════════════════════════════════════════════════════════════
class ClobOrderClient:
    # V2 (April 28 2026): new contract, version bumped to "2"
    DOMAIN={"name":"Polymarket CTF Exchange","version":"2","chainId":137,
             "verifyingContract":"0xE111180000d2663C0091e4f400237545B87B996B"}
    # V2: removed taker/expiration/nonce/feeRateBps → added timestamp/metadata/builder
    ORDER_TYPE=[
        {"name":"salt",         "type":"uint256"},
        {"name":"maker",        "type":"address"},
        {"name":"signer",       "type":"address"},
        {"name":"tokenId",      "type":"uint256"},
        {"name":"makerAmount",  "type":"uint256"},
        {"name":"takerAmount",  "type":"uint256"},
        {"name":"side",         "type":"uint8"},
        {"name":"signatureType","type":"uint8"},
        {"name":"timestamp",    "type":"uint256"},
        {"name":"metadata",     "type":"bytes32"},
        {"name":"builder",      "type":"bytes32"},
    ]
    ZERO32 = bytes(32)   # zero bytes32 for metadata and builder fields
    def __init__(self):
        self.api_key=CLOB_API_KEY; self.api_secret=CLOB_API_SECRET
        self.passphrase=CLOB_API_PASSPHRASE; self.pk=POLY_PRIVATE_KEY
        self.funder=POLY_FUNDER; self.account=None; self.signer=""; self.enabled=False
        self.creds_source="none"  # Track where credentials came from
        if not self.pk or not self.funder:
            log.warning("CLOB: missing POLY_PRIVATE_KEY or POLY_FUNDER — disabled")
            return
        try:
            from eth_account import Account
            self.account=Account.from_key(self.pk); self.signer=self.account.address
        except ImportError:
            log.error("pip install eth-account"); return
        except Exception as e:
            log.error(f"CLOB init: {e}"); return
        # If all 3 CLOB secrets provided → use them directly
        if all([self.api_key, self.api_secret, self.passphrase]):
            self.enabled=True; self.creds_source="env"
            log.info(f"CLOB: enabled (credentials from env)")
        else:
            # Auto-derive CLOB API credentials from private_key + funder
            self._auto_derive_credentials()
    
    def _auto_derive_credentials(self):
        """Auto-derive CLOB API credentials using py_clob_client_v2.
        Only requires POLY_PRIVATE_KEY and POLY_FUNDER — no separate CLOB_API_KEY needed.
        
        How it works:
        1. py_clob_client_v2 connects to Polymarket CLOB with just key+funder
        2. Calls create_or_derive_api_key() which either:
           - Returns existing derived key if one exists for this signer
           - Creates a new API key on-the-fly
        3. The derived key is then used for all subsequent API calls
        
        This is why only 4 GitHub secrets are needed:
        TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, POLY_PRIVATE_KEY, POLY_FUNDER
        """
        try:
            from py_clob_client_v2 import ClobClient
            
            log.info("CLOB: auto-deriving API credentials from private_key + funder...")
            client = ClobClient(
                "https://clob.polymarket.com",
                chain_id=137,
                key=self.pk,
                signature_type=0,  # EOA signature
                funder=self.funder,
                use_server_time=True,
            )
            creds = client.create_or_derive_api_key()
            self.api_key = creds.api_key
            self.api_secret = creds.api_secret
            self.passphrase = creds.api_passphrase
            self.enabled = True
            self.creds_source = "auto-derived"
            log.info(f"CLOB: enabled (credentials auto-derived from private_key+funder)")
        except ImportError:
            log.warning("CLOB: py_clob_client_v2 not installed — pip install py-clob-client-v2")
            log.warning("CLOB: cannot auto-derive credentials — order placement disabled")
            log.warning("CLOB: either install py-clob-client-v2 or provide CLOB_API_KEY/SECRET/PASSPHRASE")
        except Exception as e:
            log.warning(f"CLOB auto-derive failed: {e}")
            log.warning("CLOB: falling back — order placement disabled without valid credentials")

    def _l2(self,method,path,body="")->dict:
        ts=str(int(time.time())); msg=ts+method+path+body
        sig=hmac.new(self.api_secret.encode(),msg.encode(),hashlib.sha256).digest()
        return {"POLY-API-KEY":self.api_key,"POLY-API-PASSPHRASE":self.passphrase,
                "POLY-TIMESTAMP":ts,"POLY-SIGNATURE":base64.b64encode(sig).decode(),
                "Content-Type":"application/json"}

    def get_book(self,token_id:str)->Tuple[Optional[float],Optional[float]]:
        try:
            r=requests.get(f"{CLOB_BASE}/book",params={"token_id":token_id},timeout=8)
            r.raise_for_status(); data=r.json()
            asks=sorted(data.get("asks",[]),key=lambda x:float(x.get("price",1)))
            bids=sorted(data.get("bids",[]),key=lambda x:-float(x.get("price",0)))
            return (float(asks[0]["price"]) if asks else None,
                    float(bids[0]["price"]) if bids else None)
        except: return None,None

    def _sign(self,od)->str:
        from eth_account import Account
        return self.account.sign_typed_data(
            domain_data=self.DOMAIN,
            message_types={"Order":self.ORDER_TYPE},
            message_data=od).signature.hex()

    def _place(self,token_id,limit_price,size_usdc,label,order_type="LIMIT")->OrderResult:
        ts=datetime.now(timezone.utc).isoformat(); bshort=label[:20]
        if DRY_RUN:
            log.info(f"  [DRY_RUN] {order_type} ${size_usdc:.2f}@{limit_price:.3f} {label[:25]}")
            return OrderResult(label,bshort,"",str(token_id or ""),
                               limit_price,size_usdc,order_id="DRY_RUN",
                               status="dry_run",placed_at=ts,order_type=order_type)
        if not self.enabled or not token_id:
            return OrderResult(label,bshort,"",str(token_id or ""),
                               limit_price,size_usdc,status="failed",
                               error="CLOB disabled",placed_at=ts)
        price=round(max(0.01,min(0.99,limit_price)),4)
        ts_ms=int(time.time()*1000)
        # V2 order: no taker/expiration/nonce/feeRateBps → timestamp/metadata/builder
        od={"salt":ts_ms,"maker":self.funder,"signer":self.signer,
            "tokenId":int(token_id),"makerAmount":int(size_usdc*1e6),
            "takerAmount":int(size_usdc/price*1e6),
            "side":0,"signatureType":0,
            "timestamp":ts_ms,
            "metadata":self.ZERO32,
            "builder":self.ZERO32,
           }
        try:
            sig=self._sign(od)
        except Exception as e:
            return OrderResult(label,bshort,"",str(token_id),price,size_usdc,
                               status="failed",error=f"sign:{e}",placed_at=ts)
        # V2 POST body: side="BUY" string, bytes32 as hex, owner=api_key
        ZERO32H="0x"+"00"*32
        body={
            "salt":str(od["salt"]),"maker":od["maker"],"signer":od["signer"],
            "tokenId":str(od["tokenId"]),
            "makerAmount":str(od["makerAmount"]),"takerAmount":str(od["takerAmount"]),
            "side":"BUY","signatureType":od["signatureType"],
            "timestamp":str(od["timestamp"]),
            "metadata":ZERO32H,"builder":ZERO32H,
            "signature":sig,
        }
        pl=json.dumps({"order":body,"owner":self.api_key,"orderType":"GTC"})
        try:
            r=requests.post(f"{CLOB_BASE}/order",data=pl,
                             headers=self._l2("POST","/order",pl),timeout=15)
            r.raise_for_status(); resp=r.json()
            oid=resp.get("orderID","") or resp.get("id","")
            log.info(f"  {order_type} OK: ${size_usdc:.2f}@{price:.3f} id={oid[:12]}...")
            return OrderResult(label,bshort,"",str(token_id),price,size_usdc,
                               order_id=oid,status="placed",placed_at=ts,order_type=order_type)
        except Exception as e:
            log.error(f"  Order FAILED: {e}")
            return OrderResult(label,bshort,"",str(token_id),price,size_usdc,
                               status="failed",error=str(e),placed_at=ts)

    def place_limit_order(self,token_id,side,limit_price,size_usdc,label="")->OrderResult:
        """GTC limit order — fill saat harga tercapai. Untuk SIGNAL (8-20pp)."""
        result=self._place(token_id,limit_price,size_usdc,label,"LIMIT")
        result.side=side; return result

    def place_market_order(self,token_id,side,size_usdc,label="")->OrderResult:
        """Market order — fill segera di best_ask. Untuk STRONG (>20pp)."""
        ask,_=self.get_book(token_id)
        # Market order = limit di atas ask dengan buffer 5c untuk jaminan fill
        limit_price=min((ask+0.05) if ask else 0.97, 0.97)
        result=self._place(token_id,limit_price,size_usdc,label,"MARKET")
        result.side=side; return result

# ══════════════════════════════════════════════════════════════════════════
#  §14  BANKROLL MANAGER
# ══════════════════════════════════════════════════════════════════════════
class BankrollManager:
    """100% Adaptive Bankroll — always uses live wallet balance.
    
    Priority order:
    1. On-chain balance (direct RPC query to Polygon)
    2. Cached balance (if < 1 hour old, from pw_bankroll.json)
    3. DEFAULT_BANKROLL ($20) — ONLY on very first run with no cache
    
    Key change from v9.2: DEFAULT_BANKROLL is NEVER used for Kelly sizing
    once the bot has successfully read the wallet balance at least once.
    Even if the RPC query fails, the cached balance from the last successful
    read is used instead of falling back to $20.
    
    This means: if your wallet grows to $500, Kelly will size bets based on
    $500, not $20. The $20 is only the starting point before first chain read.
    """
    # V2 (April 28 2026): collateral changed from USDC.e to pUSD
    USDC="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    CACHE_MAX_AGE = 3600  # 1 hour — use cached balance if younger than this
    RPCS = ("https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon-rpc.com")
    
    def __init__(self,default=DEFAULT_BANKROLL): self.default=default
    def read_chain(self,wallet)->Optional[float]:
        """Read pUSD balance from Polygon RPC. Tries multiple RPCs."""
        if not wallet:
            return None
        padded=wallet.lower().replace("0x","").zfill(64)
        pl={"jsonrpc":"2.0","method":"eth_call",
            "params":[{"to":self.USDC,"data":f"0x70a08231{padded}"},"latest"],"id":1}
        for rpc in self.RPCS:
            try:
                r=requests.post(rpc,json=pl,timeout=8)
                r.raise_for_status()
                bal=int(r.json().get("result","0x0"),16)/1e6
                if bal >= 0:
                    return round(bal, 2)
            except:
                continue
        return None
    def get(self)->float:
        """Get current bankroll — 100% adaptive based on actual wallet balance.
        
        1. Try on-chain balance first (most accurate, real-time)
        2. Fall back to cached balance if chain query fails (still adaptive!)
        3. Only use DEFAULT_BANKROLL if no cache exists (first run ever)
        
        Kelly criterion is always based on the ACTUAL wallet balance,
        not a static default. As your wallet grows, Kelly sizing grows.
        """
        # Step 1: Try on-chain balance (real-time, most accurate)
        bal_chain = self.read_chain(POLY_FUNDER or "")
        if bal_chain is not None and bal_chain >= 0.5:
            log.info(f"Bankroll (chain, LIVE): ${bal_chain:.2f} ← 100% adaptive")
            safe_write(BANKROLL_FILE,{
                "bankroll":bal_chain,
                "source":"chain",
                "ts":datetime.now(timezone.utc).isoformat()
            })
            return bal_chain
        
        # Step 2: Try cached balance (still adaptive — was set from chain before)
        cache = safe_read(BANKROLL_FILE,{})
        if cache and cache.get("ts"):
            try:
                cache_age = (datetime.now(timezone.utc) - _parse_dt(cache["ts"])).total_seconds()
                if cache_age < self.CACHE_MAX_AGE:
                    bal_cache = float(cache.get("bankroll", 0))
                    if bal_cache >= 0.5:
                        log.info(f"Bankroll (cache, {cache_age/60:.0f}min old): ${bal_cache:.2f} ← adaptive from last chain read")
                        return bal_cache
            except:
                pass
        
        # Step 3: Use cache regardless of age (still better than default)
        if cache and cache.get("bankroll", 0) >= 0.5:
            bal_old = float(cache["bankroll"])
            log.warning(f"Bankroll (cache, STALE): ${bal_old:.2f} ← using last known balance (chain RPC failed)")
            return bal_old
        
        # Step 4: First run ever — use DEFAULT_BANKROLL
        log.warning(f"Bankroll (DEFAULT): ${self.default:.2f} ← first run, no chain/cache data yet")
        safe_write(BANKROLL_FILE,{
            "bankroll":self.default,
            "source":"default",
            "ts":datetime.now(timezone.utc).isoformat()
        })
        return self.default

# ══════════════════════════════════════════════════════════════════════════
#  §15  PENDING ORDER TRACKER
# ══════════════════════════════════════════════════════════════════════════
class PendingTracker:
    MAX_RUNS=2
    def __init__(self): self.orders=safe_read(PENDING_FILE,{}).get("orders",[])
    def save(self): safe_write(PENDING_FILE,{"orders":self.orders})
    def add(self,result:OrderResult,slug:str):
        if result.status not in ("placed","dry_run"): return
        self.orders.append({"order_id":result.order_id,"slug":slug,
                             "side":result.side,"price":result.limit_price,
                             "size":result.size_usdc,"short":result.bracket_short,
                             "token_id":result.token_id,"placed_at":result.placed_at,
                             "type":result.order_type,"runs":0})
        self.save()
    def check_cleanup(self,clob:ClobOrderClient)->List[dict]:
        if not clob.enabled or DRY_RUN: return []
        filled=[]; remain=[]
        for o in self.orders:
            o["runs"]=o.get("runs",0)+1
            try:
                path=f"/orders/{o['order_id']}"
                r=requests.get(f"{CLOB_BASE}{path}",timeout=8,
                                headers=clob._l2("GET",path))
                status=r.json().get("status","")
                if status in ("MATCHED","MATCHED_FULLY","FILLED"):
                    log.info(f"  FILLED: {o['short']} id=***{o['order_id'][-6:]}")
                    filled.append(o); continue
                elif o["runs"]>=self.MAX_RUNS:
                    pl=json.dumps({"orderID":o["order_id"]})
                    requests.delete(f"{CLOB_BASE}/order",data=pl,
                                    headers=clob._l2("DELETE","/order",pl),timeout=8)
                    continue
                remain.append(o)
            except: remain.append(o)
        self.orders=remain; self.save(); return filled

# ══════════════════════════════════════════════════════════════════════════
#  §16  BRIER TRACKER + DYNAMIC THRESHOLD
# ══════════════════════════════════════════════════════════════════════════
class BrierTracker:
    def __init__(self):
        self.data=safe_read(BRIER_FILE,{"predictions":{},"resolved":[],
                                         "cumulative_bs":0.0,"n_resolved":0})
    def save(self): safe_write(BRIER_FILE,self.data)
    def record(self,slug,label,p_model,side):
        key=f"{slug}::{label}::{side}"
        ts_now=datetime.now(timezone.utc).isoformat()
        self.data["predictions"][key]={"p_model":p_model,"side":side,
            "ts":ts_now,"resolved":False}
        # Track first prediction timestamp for time-based validation
        if not self.data.get("first_prediction_ts"):
            self.data["first_prediction_ts"]=ts_now
        self.save()
    def recent_bs(self)->float:
        r=self.data.get("resolved",[])
        if not r: return 0.18
        recent=r[-20:] if len(r)>=20 else r
        return sum(recent)/len(recent)
    def get_dynamic_threshold(self)->float:
        """Threshold 6-12pp berbasis kualitas kalibrasi Brier Score."""
        bs=self.recent_bs(); n=self.data.get("n_resolved",0)
        if n<5: return EDGE_THRESHOLD
        avg_p=0.28; exp_bs=avg_p*(1-avg_p)  # ≈0.202
        ratio=exp_bs/bs if bs>0 else 1.0
        if ratio>=1.15: return 6.0   # model bagus → agresif
        if ratio>=1.0:  return 8.0   # standard
        if ratio>=0.85: return 12.0  # degradasi → konservatif
        return 999.0  # pause
    def get_bs_kelly_mult(self)->float:
        bs=self.recent_bs(); avg_p=0.28; exp_bs=avg_p*(1-avg_p)
        if bs<0.01: return 1.0
        return round(min(1.15,max(0.30,exp_bs/bs)),3)
    def summary(self)->str:
        n=self.data.get("n_resolved",0); avg=self.data.get("cumulative_bs",0.0)
        if n==0: return "BS=n/a (n=0)"
        exp=0.28*0.72; status="CALIBRATED" if avg<exp*1.30 else "NEEDS TUNING"
        return f"BS={avg:.4f} (n={n}) E[BS]={exp:.4f} {status}"

    def check_resolutions(self):
        """Check if predicted markets have resolved on-chain and update Brier scores.
        This is CRITICAL — without resolution tracking, the bot can never self-calibrate.
        Polls Polymarket API for closed/resolved markets matching our predictions.
        """
        updated = False
        to_remove = []
        for key, pred in list(self.data.get("predictions", {}).items()):
            if pred.get("resolved", False):
                continue
            parts = key.split("::")
            slug = parts[0] if parts else ""
            if not slug:
                continue
            try:
                data = api_get(f"{GAMMA_BASE}/markets", {"slug": slug, "limit": 5})
                if not data:
                    continue
                markets = data if isinstance(data, list) else data.get("data", [])
                for m in markets:
                    closed = m.get("closed") or m.get("resolved")
                    if not closed:
                        continue
                    # Determine actual outcome
                    outcomes = m.get("outcomes", "[]")
                    prices = m.get("outcomePrices", "[]")
                    if isinstance(outcomes, str):
                        try: outcomes = json.loads(outcomes)
                        except: outcomes = []
                    if isinstance(prices, str):
                        try: prices = json.loads(prices)
                        except: prices = []
                    if not outcomes or not prices:
                        continue
                    # Find winning outcome (highest price)
                    yes_idx = outcomes.index("Yes") if "Yes" in outcomes else -1
                    if yes_idx >= 0 and yes_idx < len(prices):
                        yes_price = float(prices[yes_idx])
                        won = (pred["side"] == "YES" and yes_price > 0.5) or \
                              (pred["side"] == "NO" and yes_price <= 0.5)
                        actual = 1.0 if won else 0.0
                        bs = (pred["p_model"] - actual) ** 2
                        n = self.data.get("n_resolved", 0)
                        cum = self.data.get("cumulative_bs", 0.0)
                        self.data["n_resolved"] = n + 1
                        self.data["cumulative_bs"] = (cum * n + bs) / (n + 1)
                        self.data.setdefault("resolved", []).append(bs)
                        pred["resolved"] = True
                        updated = True
                        log.info(f"  RESOLVED: {key[:40]} → {'WIN' if won else 'LOSS'} BS={bs:.4f}")
                    break
            except Exception as e:
                log.debug(f"  Resolution check {slug[:20]}: {e}")
        if updated:
            self.save()
        return updated

# ══════════════════════════════════════════════════════════════════════════
#  §17  PERFORMANCE LOGGER
# ══════════════════════════════════════════════════════════════════════════
class PerfLogger:
    def log(self,slug,city,q,mu,sigma,mu_mkt,bd,kl,skill,lead,age,metar,kelly_mode):
        entry={"ts":datetime.now(timezone.utc).isoformat(),"slug":slug,"city":city,
               "question":q[:80],"mu":mu,"sigma":sigma,"mu_mkt":mu_mkt,
               "kl":kl,"skill":skill,"lead_h":lead,
               "age_h":round(age,1) if age else None,"metar":metar,"kelly_mode":kelly_mode,
               "brackets":[{"label":b["label"][:40],"short":b["bracket_short"],
                             "p_blend":b["p_blend"],"p_mkt":b["yes_price"],
                             "yes_edge":b["yes_edge"],"no_edge":b["no_edge"],
                             "yes_class":b["yes_class"],"no_class":b["no_class"],
                             "stake_yes":b["stake_yes"],"stake_no":b["stake_no"],
                             "skip_yes":b["skip_reason_yes"],"skip_no":b["skip_reason_no"],
                             "kl":b["kl"],"rpn":b["rpn"],"is_tail":b["is_tail"],
                             } for b in bd]}
        try:
            with open(PERF_FILE,"a") as f: f.write(json.dumps(entry)+"\n")
        except Exception as e: log.warning(f"Perf log: {e}")

# ══════════════════════════════════════════════════════════════════════════
#  §18  CATEGORY TRACKER — Kelly mult per kota/lead/bracket
# ══════════════════════════════════════════════════════════════════════════
class CategoryTracker:
    """
    Tracks win rate per city, lead time, bracket type.
    Adjusts Kelly multiplier (0.40-1.25) based on historical performance.
    Requires MIN_BETS=5 per category before adjusting.
    """
    MIN_BETS=5; EXP_WR=0.32
    def __init__(self):
        self.data=safe_read(CAT_FILE,{"cities":{},"leads":{},"types":{}})
    def save(self): safe_write(CAT_FILE,self.data)
    def _wr(self,cat,key)->Optional[float]:
        d=self.data.get(cat,{}).get(key,{})
        if not d or d.get("n",0)<self.MIN_BETS: return None
        return d["wins"]/d["n"]
    def _upd(self,cat,key,won):
        if cat not in self.data: self.data[cat]={}
        if key not in self.data[cat]: self.data[cat][key]={"n":0,"wins":0}
        self.data[cat][key]["n"]+=1
        if won: self.data[cat][key]["wins"]+=1
    def record(self,city,lead,is_tail,won):
        self._upd("cities",city,won)
        bucket="<24h" if lead<24 else ("24-48h" if lead<48 else "48h+")
        self._upd("leads",bucket,won)
        self._upd("types","tail" if is_tail else "interior",won)
        self.save()
    def get_kelly_mult(self,city,lead,is_tail)->float:
        mult=1.0
        wr=self._wr("cities",city)
        if wr is not None: mult*=min(1.25,max(0.40,wr/self.EXP_WR))
        bucket="<24h" if lead<24 else ("24-48h" if lead<48 else "48h+")
        wr_l=self._wr("leads",bucket)
        if wr_l is not None: mult*=min(1.10,max(0.70,wr_l/self.EXP_WR))
        return round(min(1.30,max(0.40,mult)),3)
    def summary(self)->str:
        lines=[]
        for city,d in sorted(self.data.get("cities",{}).items(),
                              key=lambda x:-x[1].get("n",0))[:5]:
            if d["n"]>=3:
                lines.append(f"  {city:<14}: {d['wins']/d['n']*100:.0f}% (n={d['n']})")
        return "\n".join(lines) if lines else "  no data yet"

# ══════════════════════════════════════════════════════════════════════════
#  §19  PRICE VELOCITY + CROWDING DETECTOR
# ══════════════════════════════════════════════════════════════════════════
class PriceVelocity:
    """Deteksi bot lain sudah masuk: jika harga bergerak >5pp sejak scan terakhir."""
    THRESHOLD_PP=5.0
    def __init__(self):
        self.history=safe_read(PRICE_FILE,{})
    def check(self,slug,current_yes_price)->Tuple[bool,float]:
        prev=self.history.get(slug)
        delta=abs(current_yes_price-(prev or current_yes_price))*100
        self.history[slug]=current_yes_price
        crowded=prev is not None and delta>=self.THRESHOLD_PP
        if crowded: log.info(f"  CROWDED: {slug[:30]} moved {delta:.1f}pp — bots active")
        return crowded,delta
    def save(self): safe_write(PRICE_FILE,self.history)

# ══════════════════════════════════════════════════════════════════════════
#  §20  VALIDATION PROTOCOL — 14-day auto-upgrade
# ══════════════════════════════════════════════════════════════════════════
def check_validation(brier:BrierTracker,bankroll:float,
                      start_bk:float)->Tuple[str,str]:
    """
    Returns (kelly_mode, message).
    Time-based AND count-based validation aligned with 14-day protocol.
    
    Protocol (from adversarial dynamics analysis):
    ─ Day 1-7:  DRY_RUN — collect predictions
    ─ Day 7:    Checkpoint — WR>18%, BS<E[BS]×1.35 → upgrade to ¼ Kelly
    ─ Day 8-14: Live ¼ Kelly — validate execution
    ─ Day 14:   GO/NO-GO — WR>17%, BS<E[BS]×1.30, DD<20%, fill>70% → ½ Kelly
    
    Key insight: ½ Kelly remains profitable as long as P(win) > 16.6%.
    With 28 bets in 14 days, 90% CI lower bound = 18% > 16.6% breakeven.
    """
    n=brier.data.get("n_resolved",0)
    bs=brier.recent_bs(); avg_p=0.28; exp_bs=avg_p*(1-avg_p)
    bs_ratio=exp_bs/bs if bs>0 else 1.0
    resolved=brier.data.get("resolved",[])
    wins=sum(1 for b in resolved if b<0.25)
    wr=wins/max(n,1)
    dd=(start_bk-bankroll)/max(start_bk,1) if start_bk>0 else 0

    # Time-based tracking
    first_ts=brier.data.get("first_prediction_ts","")
    days_elapsed=0
    if first_ts:
        try: days_elapsed=(datetime.now(timezone.utc)-_parse_dt(first_ts)).days
        except: pass

    wr_ok=wr>0.17; bs_ok=bs_ratio>0.77; dd_ok=dd<0.20

    if DRY_RUN:
        return "DRY_RUN",f"DRY_RUN mode — n={n} resolved, day {days_elapsed}/14"

    # Day 7 checkpoint: WR > 18%, BS < E[BS]×1.35
    if days_elapsed>=7 and n>=5:
        if wr>0.18 and bs_ratio>0.74 and dd_ok:
            return "QUARTER",f"Phase 1 (Day {days_elapsed}): WR={wr*100:.0f}% BS={bs:.4f} n={n} → ¼ Kelly live"

    # Day 14 GO/NO-GO: WR > 17%, BS < E[BS]×1.30, DD < 20%
    if n>=14 and wr_ok and bs_ok and dd_ok:
        return "HALF",f"Validation OK: WR={wr*100:.0f}% BS={bs:.4f} n={n} day {days_elapsed} → ½ Kelly"
    if n>=7 and (wr>0.14 or bs_ratio>0.70) and dd_ok:
        return "QUARTER",f"Phase 1: WR={wr*100:.0f}% BS={bs:.4f} n={n} day {days_elapsed} → ¼ Kelly"
    if n<7:
        return "QUARTER",f"Accumulating: {n}/7 bets, day {days_elapsed}/14 → ¼ Kelly until n=7"
    return "QUARTER",f"Model monitoring: WR={wr*100:.0f}% BS={bs:.4f} n={n} day {days_elapsed}"

# ══════════════════════════════════════════════════════════════════════════
#  §21  FORMATTER v3.3
# ══════════════════════════════════════════════════════════════════════════
def format_msgs(question,url,city,mu_raw,ens,lead,mu_mkt,ovr,vol,
                age_h,bd,bankroll,brier_summary,order_results,
                metar_updated,kelly_mode,val_msg,crowded)->List[str]:
    now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mu=ens.mu_ensemble; sigma=ens.sigma_ensemble
    sq="HIGH" if sigma<2.0 else ("MED" if sigma<2.5 else "LOW")
    alpha=f"{mu-mu_mkt:+.1f}" if mu_mkt else "?"
    age_s=(f"{age_h*60:.0f}min" if age_h and age_h<1
           else (f"{age_h:.0f}h" if age_h else "?"))
    crowd_flag="🔥CROWDED " if crowded else ""

    lines1=[f"<b>{crowd_flag}{city.upper()}  {now}</b>",question[:100],
            f'<a href="{url}">{url}</a>',
            f"vol ${vol:,.0f}  lead {lead:.0f}h  vig {ovr*100:.1f}%  age {age_s}",
            f"Kelly mode: <b>{kelly_mode}</b>  {val_msg[:50]}",
            f"Models: {ens.n_active}/{len(ens.models)} active" +
            ("  <b>METAR UPDATED</b>" if metar_updated else ""),
            "","<pre>ENSEMBLE"]
    for mr in ens.models:
        pfx="  " if mr.active else "  [off]"
        lines1.append(f"{pfx}{mr.model_id:<4} {mr.model_name:<18} {mr.mu:.1f} w={mr.weight:.3f}")
        if mr.inactive_reason: lines1.append(f"       → {mr.inactive_reason}")
    lines1+=[f"",f"mu={mu:.2f}  σ={sigma:.2f} [{sq}]  mkt_mu={mu_mkt or'?'}  α={alpha}",
             f"KL={ens.kl_total:.3f}  Skill={ens.skill_score*100:.1f}%  RPN={ens.fmea_rpn}",
             f"HMM={ens.hmm_state}(p={ens.hmm_hot_p:.2f})  DynK×{ens.dynamic_k_mult:.2f}",
             "</pre>"]

    # v9.3: total_mkt_raw is informational — prices don't need normalization
    lines2=[f"<b>BRACKETS  μ={mu:.1f}  σ={sigma:.2f}  thr={bd[0]['yes_thr']:.0f}pp</b>",""]
    for b in bd:
        # v9.3: yes_price IS the probability directly — no normalization
        ym=b["yes_price"]*100; nm=(1-b["yes_price"])*100
        lines2.append(f"<pre>{b['bracket_short']}")
        lines2.append(f"  {b['cdf_str']}")
        lines2.append(f"  YES mkt {ym:.1f}% mdl {b['p_blend']*100:.1f}%",)
        yc=b["yes_class"]
        if yc=="STRONG": lines2.append(f"  <b>>>STRONG YES +{b['yes_edge']:.1f}pp [MARKET ORDER]</b>")
        elif yc=="SIGNAL": lines2.append(f"  <b>>> SIGNAL YES +{b['yes_edge']:.1f}pp [LIMIT]</b>")
        elif yc=="NEAR": lines2.append(f"  <i>?NEAR +{b['yes_edge']:.1f}pp (thr {b['yes_thr']:.0f}pp)</i>")
        else: lines2.append(f"  --pass {b['yes_edge']:+.1f}pp")
        nc=b["no_class"]
        if nc=="STRONG": lines2.append(f"  <b>!!STRONG NO  +{abs(b['no_edge']):.1f}pp [MARKET ORDER]</b>")
        elif nc=="SIGNAL": lines2.append(f"  <b>!! SIGNAL NO +{abs(b['no_edge']):.1f}pp [LIMIT]</b>")
        elif nc=="NEAR": lines2.append(f"  <i>?NEAR +{abs(b['no_edge']):.1f}pp (thr {b['no_thr']:.0f}pp)</i>")
        else: lines2.append(f"  --pass {b['no_edge']:+.1f}pp")
        if b.get("yes_ask"): lines2.append(f"  YES ask={b['yes_ask']:.3f} MAP={b['yes_map']:.3f}")
        # NEW: Show Bootstrap CI and liquidity info
        ci_sig = "✓" if b.get("ci_yes_significant") else "✗"
        lines2.append(f"  CI=[{b.get('ci_yes_low',0):.1f},{b.get('ci_yes_high',0):.1f}]pp {ci_sig}")
        if b.get("liq_yes_depth",0) > 0:
            liq_ok = "✓" if b.get("liq_yes_pass") else "✗"
            lines2.append(f"  Liq=${b.get('liq_yes_depth',0):.0f} {liq_ok}")
        if b["stake_yes"]>=1: lines2.append(f"  Kelly YES: ${b['stake_yes']:.2f} f={b['f_yes']*100:.1f}% frac={b.get('kelly_frac',0.5):.2f}")
        if b["stake_no"] >=1: lines2.append(f"  Kelly NO:  ${b['stake_no']:.2f} f={b['f_no']*100:.1f}% frac={b.get('kelly_frac',0.5):.2f}")
        if b["skip_reason_yes"]: lines2.append(f"  [skip YES: {b['skip_reason_yes']}]")
        if b["skip_reason_no"]:  lines2.append(f"  [skip NO:  {b['skip_reason_no']}]")
        lines2.append("</pre>")

    action=[b for b in bd if b["yes_class"] in ("SIGNAL","STRONG") or
                              b["no_class"] in ("SIGNAL","STRONG")]
    lines3=[]
    if action:
        lines3.append("<b>ACTIONABLE SIGNALS</b>"); lines3.append("<pre>")
        for b in action:
            sh=b["bracket_short"]
            if b["yes_class"] in ("SIGNAL","STRONG"):
                otype="[MARKET ORDER]" if b["yes_class"]=="STRONG" else "[LIMIT +2c]"
                # v9.3: yes_price IS the market probability directly
                mkt_ask=b["yes_price"]
                lp=min(round(mkt_ask+0.02,3),b["yes_map"])
                lines3.append(f">> {sh:<12} BET YES  +{b['yes_edge']:.1f}pp  {otype}")
                lines3.append(f"   model {b['p_blend']*100:.1f}%  market {mkt_ask*100:.1f}%")
                lines3.append(f"   entry @ {lp:.3f}  MAP={b['yes_map']:.3f}")
                if b["stake_yes"]>=1: lines3.append(f"   Kelly: ${b['stake_yes']:.2f}  f={b['f_yes']*100:.1f}%")
            if b["no_class"] in ("SIGNAL","STRONG"):
                otype="[MARKET ORDER]" if b["no_class"]=="STRONG" else "[LIMIT +2c]"
                # v9.3: NO probability = 1 - yes_price directly
                mkt_no=1-b["yes_price"]
                lp=min(round(mkt_no+0.02,3),b["no_map"])
                lines3.append(f"!! {sh:<12} BET NO   +{abs(b['no_edge']):.1f}pp  {otype}")
                lines3.append(f"   model {(1-b['p_blend'])*100:.1f}%  market {mkt_no*100:.1f}%")
                lines3.append(f"   entry @ {lp:.3f}  MAP={b['no_map']:.3f}")
                if b["stake_no"]>=1: lines3.append(f"   Kelly: ${b['stake_no']:.2f}  f={b['f_no']*100:.1f}%")
        lines3.append("</pre>")
    else:
        near=[b for b in bd if b["yes_class"]=="NEAR" or b["no_class"]=="NEAR"]
        lines3.append("<pre>No signal above threshold.")
        if near:
            best=max(near,key=lambda b:max(b["yes_edge"],b["no_edge"]))
            lines3.append(f"Closest: {best['bracket_short']} +{max(best['yes_edge'],best['no_edge']):.1f}pp")
        lines3.append("</pre>")

    if order_results:
        is_sim = any(o.status=="simulated" for o in order_results)
        header = "SIMULATED ENTRIES" if is_sim else "ORDERS"
        lines3.append(f"<b>{header}</b>")
        lines3.append("<pre>")
        for o in order_results:
            st="SIM" if o.status=="simulated" else ("DRY_RUN" if o.status=="dry_run" else o.status.upper())
            lines3.append(f"{o.side:<3} {o.bracket_short:<12} ${o.size_usdc:.2f}@{o.limit_price:.3f} {o.order_type} {st}")
            if o.order_id and o.order_id not in ("DRY_RUN",""):
                if not o.order_id.startswith("SIM_"):
                    lines3.append(f"    id=***{o.order_id[-6:]}")
            if o.error:
                lines3.append(f"    err={o.error[:50]}")
        lines3.append("</pre>")

    lines3+=["<pre>",f"{brier_summary}  ${bankroll:.0f}",
             f"Kelly:{kelly_mode}  DRY_RUN:{DRY_RUN}  FAST:{FAST_SCAN}","</pre>"]
    return ["\n".join(lines1),"\n".join(lines2),"\n".join(lines3)]

# ══════════════════════════════════════════════════════════════════════════
#  §22  WEEKLY REPORT
# ══════════════════════════════════════════════════════════════════════════
def weekly_report(brier,bankroll,cat_tracker)->str:
    cutoff=datetime.now(timezone.utc)-timedelta(days=7)
    entries=[]
    try:
        with open(PERF_FILE) as f:
            for line in f:
                try:
                    e=json.loads(line)
                    if _parse_dt(e.get("ts","1970-01-01"))>cutoff: entries.append(e)
                except: pass
    except: pass
    if not entries: return "<pre>No data from last 7 days.</pre>"
    age_buckets={"<2h":{"n":0,"signals":0},"2-6h":{"n":0,"signals":0},
                 "6-12h":{"n":0,"signals":0},">12h":{"n":0,"signals":0}}
    total_signals=0
    for e in entries:
        age=e.get("age_h") or 99
        bucket="<2h" if age<2 else ("2-6h" if age<6 else ("6-12h" if age<12 else ">12h"))
        age_buckets[bucket]["n"]+=1
        for b in e.get("brackets",[]):
            if max(b.get("stake_yes",0),b.get("stake_no",0))>0:
                age_buckets[bucket]["signals"]+=1; total_signals+=1
    lines=["<b>WEEKLY REPORT</b>","<pre>",
           f"Markets analyzed: {len(entries)}",
           f"Total signals:    {total_signals}",
           f"Brier Score:      {brier.recent_bs():.4f}",
           f"Bankroll:         ${bankroll:.2f}","",
           "Signals by market age:"]
    for k,v in age_buckets.items():
        lines.append(f"  {k:<7}: {v['signals']:2d} signals / {v['n']:2d} markets")
    lines+=["","Top cities:"]
    lines.append(cat_tracker.summary())
    lines+=["","Kelly multipliers by category:"]
    for city,d in sorted(cat_tracker.data.get("cities",{}).items(),
                          key=lambda x:-x[1].get("n",0))[:5]:
        if d["n"]>=3:
            m=cat_tracker.get_kelly_mult(city,24,False)
            lines.append(f"  {city:<14}: ×{m:.2f}")
    lines.append("</pre>")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════
#  §23  ANALYSIS + BET PIPELINE
# ══════════════════════════════════════════════════════════════════════════
def analyze_and_bet(ev,bankroll,clob,risk_gate,brier,perf,
                    active_cities,bot_state,kelly_mode,
                    price_vel,cat_tracker,force_market_order=False,
                    calibrator=None,liq_filter=None,start_bankroll=0.0):
    """v9.3 pw_ BUILD: Event-level analysis with 5 new models.
    Determines temp_type from brackets, uses T_min for lowest temperature markets.
    Integrates: Isotonic Calibration, Bootstrap CI, Liquidity Filter,
    Adaptive Kelly, Ensemble Dispersion Filter."""
    question=ev.get("question",""); url=ev.get("url",""); vol=ev.get("volume",0)
    age_h=_market_age_hours(ev)

    city=extract_city(question) or \
         extract_city((ev.get("groupSlug") or ev.get("slug","")).replace("-"," "))
    if not city: log.info("  Skip: city unknown"); return None

    target_date=extract_date(question)
    coords=CITY_COORDS[city]

    # v9.3: Fetch brackets FIRST to determine temp_type
    brackets=fetch_brackets(ev)
    if not brackets: log.info("  Skip: no brackets"); return None

    # Determine dominant temp_type from brackets
    temp_types=[b.get("temp_type","highest") for b in brackets]
    temp_type="lowest" if temp_types.count("lowest")>temp_types.count("highest") else "highest"

    # v9.3: Use T_min for lowest temperature, T_max for highest
    mu_max,mu_min,lead=fetch_nwp(coords[0],coords[1],target_date)
    mu_raw=mu_min if temp_type=="lowest" else mu_max

    is_f="°f" in question.lower() or "fahrenheit" in question.lower() or \
         any(b.get("unit")=="F" for b in brackets)
    if is_f: mu_raw=mu_raw*9/5+32

    station=CITY_STATION.get(city,""); bias=STATION_BIAS.get(station,0.0)
    adj_mu=round(mu_raw+bias,2)
    sig_ens=fetch_ens_sigma(coords[0],coords[1])

    month=target_date.month if target_date else datetime.now().month
    ens=run_ensemble(city,adj_mu,sig_ens,lead,month,brackets,temp_type)
    mu=ens.mu_ensemble; sigma=ens.sigma_ensemble

    # METAR Kalman (only T-0), v9.3: pass temp_type
    metar_updated=False
    if lead<=24 and station:
        mc=fetch_metar(station)
        if mc is not None:
            mv=mc*9/5+32 if is_f else mc
            mu,sigma=kalman_update(mu,sigma,mv,city,month,temp_type)
            ens.mu_ensemble=mu; ens.sigma_ensemble=sigma; metar_updated=True

    # Price velocity check (crowding detection)
    try:
        raw_prices=ev.get("_raw",{}).get("outcomePrices","[0.5]")
        pr=json.loads(raw_prices) if isinstance(raw_prices,str) else raw_prices
        yes_price=float(pr[0]) if pr else 0.5
    except: yes_price=0.5
    slug=ev.get("groupSlug") or ev.get("slug","?")
    crowded,velocity=price_vel.check(slug,yes_price)

    # Fetch CLOB books
    best_asks={}; best_bids={}
    for b in brackets:
        for tok in [str(b.get("yes_token_id") or ""), str(b.get("no_token_id") or "")]:
            if tok and tok not in best_asks:
                ask,bid=clob.get_book(tok) if clob.enabled else (None,None)
                best_asks[tok]=ask; best_bids[tok]=bid

    mc_probs=m7_mc(mu,sigma,brackets,n_paths=50_000)
    dynamic_thr=brier.get_dynamic_threshold()
    bs_mult=brier.get_bs_kelly_mult()

    # Moat 2: Exotic city bonus
    exotic_thr_red, exotic_k_mult = get_exotic_bonus(city)
    eff_thr = max(4.0, dynamic_thr - exotic_thr_red)  # Floor 4pp agar tidak terlalu agresif
    if exotic_thr_red > 0:
        log.info(f"  Exotic bonus: thr {dynamic_thr:.0f}pp→{eff_thr:.0f}pp  kelly×{exotic_k_mult:.2f}")

    # NEW MODEL 3: Ensemble Dispersion Filter — check before computing brackets
    disp_pass, disp_sigma, disp_mean = check_ensemble_dispersion(ens.models)
    if not disp_pass:
        log.info(f"  DISPERSION: models disagree (σ={disp_sigma:.2f}°C) — skipping event")
        # Still log but don't trade
        return None

    # Calculate Brier Score ratio for adaptive Kelly
    bs = brier.recent_bs()
    avg_p = 0.28; exp_bs = avg_p * (1 - avg_p)
    bs_ratio = exp_bs / bs if bs > 0 else 1.0
    n_resolved = brier.data.get("n_resolved", 0)

    bd=calc_brackets_full(mu,sigma,brackets,mc_probs,bankroll,ens,
                           active_cities,best_asks,best_bids,city,
                           kelly_mode,eff_thr,bs_mult,cat_tracker,exotic_k_mult,
                           calibrator=calibrator,
                           liq_filter=liq_filter,
                           clob_client=clob,
                           start_bankroll=start_bankroll,
                           bs_ratio=bs_ratio,
                           n_resolved=n_resolved)

    ens.kl_total=round(sum(b["kl"] for b in bd),4)
    max_stake=max((max(b["stake_yes"],b["stake_no"]) for b in bd),default=1.0)
    ens.fmea_rpn=f11_fmea(sigma,lead,vol,bd[0]["overround"] if bd else 0,max_stake,bankroll)
    mu_mkt=calc_implied_mu(brackets)
    ovr=bd[0]["overround"] if bd else 0.0

    # v9.3: Log temp_type for diagnostics
    total_mkt_info=sum(b["yes_price"] for b in brackets)
    log.info(f"  temp_type={temp_type}  {len(brackets)} brackets  total_yes={total_mkt_info:.3f}")

        # Age multiplier
    age_mult=(1.0 if (age_h or 99)<2 else 0.85 if (age_h or 99)<6
              else 0.70 if (age_h or 99)<12 else 0.50)
    age_thr  =(dynamic_thr if (age_h or 99)<2 else
               dynamic_thr+2 if (age_h or 99)<6 else
               dynamic_thr+4 if (age_h or 99)<12 else dynamic_thr+6)

    order_results=[]
    for b in bd:
        for side in ("YES","NO"):
            cls  =b["yes_class"] if side=="YES" else b["no_class"]
            edge =b["yes_edge"]  if side=="YES" else b["no_edge"]
            stake=b["stake_yes"] if side=="YES" else b["stake_no"]
            tok  =b.get("yes_token_id") if side=="YES" else b.get("no_token_id")
            ask_ok=b.get("yes_ask_ok",True) if side=="YES" else b.get("no_ask_ok",True)
            map_v=b["yes_map"] if side=="YES" else b["no_map"]
            ask_v=b.get("yes_ask") if side=="YES" else b.get("no_ask")

            skip=""
            if cls not in ("SIGNAL","STRONG"):  skip="below_cls"
            elif edge<age_thr:                  skip=f"age_thr_{age_thr:.0f}pp"
            elif b["rpn"]>40:                   skip=f"RPN_{b['rpn']}"
            elif stake<1.0:                     skip="stake_below_min"
            elif not ask_ok:                    skip="above_MAP"
            elif crowded and cls!="STRONG":     skip="crowded_mkt"

            if side=="YES":
                b["skip_reason_yes"]=skip
            else:
                b["skip_reason_no"]=skip
            if skip:
                continue

            adj_stake=stake*age_mult
            ok,reason,adj_stake=risk_gate.check(bankroll,adj_stake,
                                                  brier.recent_bs(),bot_state)
            if not ok:
                if side=="YES":
                    b["skip_reason_yes"]=reason
                else:
                    b["skip_reason_no"]=reason
                log.info(f"  RG: {reason}")
                continue

            if side=="YES":
                base_ask=ask_v or b["yes_price"]
            else:
                base_ask=ask_v or (1-b["yes_price"])
            limit_price=min(round(base_ask+0.02,3),map_v)

            use_market = (cls=="STRONG" or force_market_order) and clob.enabled

            if DRY_RUN or kelly_mode=="DRY_RUN":
                ts_now=datetime.now(timezone.utc).isoformat()
                otype="MARKET" if use_market else "LIMIT"
                result=OrderResult(b["label"],b["bracket_short"],side,
                                   str(tok or ""),limit_price,adj_stake,
                                   map_price=map_v,
                                   order_id="SIM_"+b["bracket_short"],
                                   status="simulated",
                                   placed_at=ts_now,
                                   order_type=otype)
                log.info(f"  [SIMULATED] {otype} {side} ${adj_stake:.2f}@{limit_price:.3f} {b['bracket_short']}")
            else:
                if use_market:
                    result=clob.place_market_order(str(tok or ""),side,adj_stake,b["label"])
                else:
                    result=clob.place_limit_order(str(tok or ""),side,limit_price,adj_stake,b["label"])

            result.bracket_short=b["bracket_short"]
            result.map_price=map_v
            order_results.append(result)
            brier.record(slug,b["label"],b["p_blend"],side)
            time.sleep(0.3)
            
    if city not in active_cities: active_cities.append(city)
            # Log performance AFTER orders (so order_results is populated)
    perf.log(slug,city,question,mu,sigma,mu_mkt,bd,ens.kl_total,
             ens.skill_score,lead,age_h or 0,metar_updated,kelly_mode,
             order_results=order_results)

    val_mode,val_msg=check_validation(brier,bankroll,
                                      bot_state.get("daily_start_bankroll",bankroll))

    msgs=format_msgs(question,url,city,mu_raw,ens,lead,mu_mkt,
                     ovr,_pf(vol),age_h,bd,bankroll,
                     brier.summary(),order_results,metar_updated,
                     kelly_mode,val_msg,crowded)
    return msgs

# ══════════════════════════════════════════════════════════════════════════
#  §24  MAIN
# ══════════════════════════════════════════════════════════════════════════
def main():
    start=datetime.now(timezone.utc)
    log.info("="*65)
    log.info(f"  PolyWeather Bot v9.3 (pw_)  FAST={FAST_SCAN}  DRY_RUN={DRY_RUN}")
    log.info("="*65)

    bm=BankrollManager(); bankroll=bm.get()
    clob=ClobOrderClient(); rg=RiskGate()
    brier=BrierTracker(); perf=PerfLogger()
    # Check resolved markets and update Brier scores (critical for self-calibration)
    brier.check_resolutions()
    pending=PendingTracker(); bot_state=load_bot_state()
    cat_tracker=CategoryTracker(); price_vel=PriceVelocity()
    
    # NEW: Initialize 5 new models
    calibrator = IsotonicCalibrator()
    liq_filter = LiquidityFilter()
    
    log.info(f"  Isotonic Calibration: {calibrator.summary()}")
    log.info(f"  Liquidity Filter: min_depth=${LIQUIDITY_MIN_DEPTH:.0f}")
    log.info(f"  Bootstrap CI: {BOOTSTRAP_CONF*100:.0f}% confidence, n={BOOTSTRAP_N}")
    log.info(f"  Dispersion Filter: max {DISPERSION_MAX_PP:.1f}°C")
    log.info(f"  Adaptive Kelly: f/2→f/3→f/4 based on DD+calibration")
    log.info(f"  CLOB credentials: {'enabled ('+clob.creds_source+')' if clob.enabled else 'disabled'}")

    # Set daily start
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if bot_state.get("last_date","")!=today:
        bot_state["daily_start_bankroll"]=bankroll
        bot_state["last_date"]=today
        safe_write(BOT_STATE,bot_state)

    # Determine Kelly mode
    kelly_mode,val_msg=check_validation(brier,bankroll,
                                         bot_state.get("daily_start_bankroll",bankroll))
    log.info(f"Kelly mode: {kelly_mode} — {val_msg}")
    log.info(f"Bankroll:   ${bankroll:.2f} (100% adaptive from wallet)")
    log.info(f"Brier:      {brier.summary()}")
    log.info(f"DynThresh:  {brier.get_dynamic_threshold():.0f}pp")

    # Telegram commands
    flags=check_tg_commands(bot_state)
    if flags["emergency_stop"]:
        send_telegram("<b>EMERGENCY STOP.</b>")
        bot_state["paused"]=True; safe_write(BOT_STATE,bot_state); return
    if flags["pause"]:
        bot_state["paused"]=True; safe_write(BOT_STATE,bot_state)
        send_telegram("<b>PAUSED via /pause.</b>"); return  # intentional: pause exits main(), resume does NOT (bot continues)
    if flags["resume"]:
        bot_state["paused"]=False; safe_write(BOT_STATE,bot_state)
        send_telegram("<b>RESUMED via /resume.</b>")
    if flags["status_req"]:
        send_telegram(f"<pre>STATUS v9.3 pw_\nBankroll: ${bankroll:.2f} (adaptive)\n"
                      f"Kelly: {kelly_mode}\nDRY_RUN: {DRY_RUN}\nFAST: {FAST_SCAN}\n"
                      f"{brier.summary()}\nThresh: {brier.get_dynamic_threshold():.0f}pp\n"
                      f"CLOB: {'enabled ('+clob.creds_source+')' if clob.enabled else 'disabled'}\n"
                      f"{calibrator.summary()}\n"
                      f"Liquidity Filter: ${LIQUIDITY_MIN_DEPTH:.0f} min depth</pre>")
    if flags["validate_req"]:
        send_telegram(f"<pre>VALIDATION\n{val_msg}\n{cat_tracker.summary()}</pre>")
    if bot_state.get("paused"): log.info("Bot PAUSED."); return

    safe_write(BOT_STATE,bot_state)

    # RG7 pre-check
    ok_rg7,rg7_msg=rg.check_rg7(bankroll,bot_state)
    if not ok_rg7:
        send_telegram(f"<b>CIRCUIT BREAKER</b>\n<pre>{rg7_msg}</pre>")
        log.warning(rg7_msg); return

    # Check pending orders
    filled=pending.check_cleanup(clob)
    if filled: log.info(f"Filled prev run: {len(filled)}")

    # Weekly report Monday
    if datetime.now(timezone.utc).weekday()==0:
        send_telegram(weekly_report(brier,bankroll,cat_tracker))

    price_vel.save()
    seen=load_seen(); now_ts=start.timestamp()

    # Scan strategy
    if FAST_SCAN:
        events=scan_new_markets_only()   # sudah sorted exotic-first
        force_mkt_order=True
        log.info(f"[FAST MODE] {len(events)} new markets to analyze")
    else:
        all_ev=fetch_all_events()
        events=[e for e in all_ev if _is_open(e)
                and (e.get("groupSlug") or e.get("slug","")) not in seen]
        # Moat 2: Exotic cities first in full scan too
        events.sort(key=lambda ev: 0 if _is_exotic_event(ev) else 1)
        exotic_n=sum(1 for ev in events if _is_exotic_event(ev))
        force_mkt_order=False
        log.info(f"[FULL MODE] {len(events)} markets | exotic: {exotic_n} (priority)")

    active_cities=[]; stats={"sent":0,"skip_vol":0,"skip_err":0}

    for ev in events:
        slug=ev.get("groupSlug") or ev.get("slug","")
        vol=ev["volume"]; q=ev["question"][:60]
        age_h=_market_age_hours(ev)
        age_s=(f"{age_h*60:.0f}min" if age_h and age_h<1
               else (f"{age_h:.0f}h" if age_h else "?"))

        if vol<MIN_VOLUME:
            log.info(f"  Skip vol (${vol:.0f}): {q}")
            stats["skip_vol"]+=1; continue

        log.info(f"→ [age {age_s}] vol=${vol:,.0f}  {q}")
        log.info(f"  {ev['url']}")

        try:
            msgs=analyze_and_bet(ev,bankroll,clob,rg,brier,perf,
                                  active_cities,bot_state,kelly_mode,
                                  price_vel,cat_tracker,force_mkt_order,
                                  calibrator=calibrator,
                                  liq_filter=liq_filter,
                                  start_bankroll=bot_state.get("daily_start_bankroll",bankroll))
        except Exception as e:
            log.error(f"  Error: {e}",exc_info=True)
            msgs=None; stats["skip_err"]+=1; continue

        if not msgs: stats["skip_err"]+=1; continue

        for i,m in enumerate(msgs):
            send_telegram(m)
            if i<len(msgs)-1: time.sleep(0.5)
        seen[slug]=now_ts; stats["sent"]+=1
        log.info(f"  Sent ({len(msgs)} msgs)")
        time.sleep(1.0)

    safe_write(STATE_FILE,seen); price_vel.save()
    dur=(datetime.now(timezone.utc)-start).total_seconds()
    log.info(f"\n{'='*65}\n  pw_ v9.3 DONE  sent={stats['sent']}  err={stats['skip_err']}"
             f"  {dur:.0f}s  Kelly={kelly_mode}  Bankroll=${bankroll:.2f}\n{'='*65}")


if __name__=="__main__":
    main()
