#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  POLYMARKET AUTO-BETTING BOT  v9.2  — PRODUCTION + ADVERSARIAL MOAT     ║
║                                                                          ║
║  BARU vs v9.1:                                                           ║
║  + True ½ Kelly per bracket  (f* × 0.5, bukan fixed 9%)                ║
║  + Validation Protocol       (DRY_RUN→QUARTER→HALF otomatis)            ║
║  + Market Order              (STRONG >20pp = fill segera)               ║
║  + Dynamic Threshold         (6-12pp berdasarkan Brier Score)           ║
║  + CategoryTracker           (Kelly mult per kota/lead/bracket)         ║
║  + scan_new_markets_only()   (untuk cron 5-menit)                       ║
║  + Price velocity detector   (deteksi bot lain sudah masuk)             ║
║  + FAST_SCAN mode            (dedicated untuk cron 5-menit)             ║
║  + 2 GitHub Actions workflows (5-menit new + 30-menit full)             ║
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
DRY_RUN             = os.environ.get("DRY_RUN","true").lower()!="false"
FAST_SCAN           = os.environ.get("FAST_SCAN","false").lower()=="true"

GAMMA_BASE  = "https://gamma-api.polymarket.com"
CLOB_BASE   = "https://clob.polymarket.com"
OWM_BASE    = "https://api.open-meteo.com/v1/forecast"
ENS_BASE    = "https://ensemble-api.open-meteo.com/v1/ensemble"
METAR_BASE  = "https://www.aviationweather.gov/adds/dataserver_current/httpparam"
POLY_RPC    = "https://polygon-rpc.com"

DEFAULT_BANKROLL  = float(os.environ.get("BANKROLL","100"))
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

STATE_FILE   = "seen_markets.json"
BRIER_FILE   = "brier_scores.json"
PERF_FILE    = "performance.jsonl"
BANKROLL_FILE= "bankroll.json"
PENDING_FILE = "pending_orders.json"
BOT_STATE    = "bot_state.json"
CAT_FILE     = "category_stats.json"
PRICE_FILE   = "price_history.json"

INF=float('inf'); HURST_H=0.63
_nwp_cache:Dict[tuple,tuple]={}
_ens_cache:Dict[tuple,float]={}
_metar_cache:Dict[str,Optional[float]]={}

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

# ══════════════════════════════════════════════════════════════════════════
#  §4  HELPERS + STATE
# ══════════════════════════════════════════════════════════════════════════
def _is_exotic_event(ev: dict) -> bool:
    """Moat 2: Cek apakah event adalah kota eksotis (minim kompetitor bot)."""
    text = (ev.get("question","") or ev.get("groupSlug","") or
            ev.get("slug","")).lower().replace("-"," ")
    return any(city in text for city in EXOTIC_CITIES)

def is_weather_market(m:dict)->bool:
    q=(m.get("question","") or m.get("title","")).lower()
    s=(m.get("slug","") or m.get("groupSlug","")).lower()
    return any(kw in q for kw in WEATHER_KW) or \
           any(kw in q for kw in ["°c","°f","temperature","rain","snow"])

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
        json.dump(data,open(tmp,"w"),indent=2,default=str)
        if os.path.exists(path):
            import shutil; shutil.copy2(path,bak)
        os.replace(tmp,path)
    except Exception as e: log.warning(f"Write {path}: {e}")

def safe_read(path:str,default=None)->dict:
    for f in [path,path+".backup"]:
        try: return json.load(open(f))
        except: continue
    return default if default is not None else {}

def load_seen()->dict:
    data=safe_read(STATE_FILE,{})
    cut=(datetime.now(timezone.utc)-timedelta(hours=48)).timestamp()
    return {k:v for k,v in data.items() if isinstance(v,float) and v>cut}

def load_bot_state()->dict:
    return safe_read(BOT_STATE,{"paused":False,"update_offset":0,
                                "daily_start_bankroll":0.0,"last_date":"",
                                "kelly_mode":"DRY_RUN"})

# ══════════════════════════════════════════════════════════════════════════
#  §5  API + TELEGRAM + COMMANDS
# ══════════════════════════════════════════════════════════════════════════
def api_get(url,params=None,retries=3):
    h={"Accept":"application/json","User-Agent":"PolyWeather/9.2"}
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
#  §6  SCANNER
# ══════════════════════════════════════════════════════════════════════════
def _normalize(m:dict)->dict:
    ev=m.get("groupSlug") or m.get("group_slug") or m.get("slug","")
    slug=ev or m.get("slug","")
    cleaned=re.sub(
        r'(?<=\d{4})-\d+(?:-\d+)?[cf](?:orhigher|orbelow|orabove|orlower)?$',
        '',slug,flags=re.I)
    ev=cleaned or ev
    return {"slug":m.get("slug",""),"groupSlug":ev,
            "url":f"https://polymarket.com/event/{ev}",
            "question":m.get("question") or m.get("title") or ev,
            "volume":_pf(m.get("volume")),"createdAt":m.get("createdAt",""),
            "endDate":m.get("endDate",""),"markets":[],"_raw":m}

def scan_new_markets_only()->list:
    """Fast scan: HANYA market yang dibuat dalam NEW_MARKET_WIN menit terakhir.
    Digunakan oleh cron 5-menit. Mengambil edge SEBELUM crowd datang."""
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=NEW_MARKET_WIN)
    pool={}
    try:
        data=api_get(f"{GAMMA_BASE}/markets",{
            "active":"true","closed":"false",
            "order":"createdAt","ascending":"false","limit":25})
        if not data: return []
        markets=data if isinstance(data,list) else data.get("data",[])
        for m in markets:
            created=m.get("createdAt","")
            if created:
                try:
                    if _parse_dt(created)<cutoff: break  # sorted desc, dapat berhenti
                except: pass
            if not is_weather_market(m): continue
            key=m.get("groupSlug") or m.get("slug","")
            if key and key not in pool: pool[key]=_normalize(m)
    except Exception as e: log.warning(f"New market scan: {e}")
    # Moat 2: Exotic cities first — edge persists longer, less competition
    result = sorted(pool.values(),
                    key=lambda ev: 0 if _is_exotic_event(ev) else 1)
    exotic_n = sum(1 for ev in result if _is_exotic_event(ev))
    log.info(f"[FAST_SCAN] {len(result)} new markets | exotic: {exotic_n} (priority)")
    return result

def _scan_page(pool,extra,label):
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
            key=m.get("groupSlug") or m.get("slug","")
            if not key or key in pool: continue
            pool[key]=_normalize(m); hits+=1
        if len(markets)<PAGE_LIMIT: break
        time.sleep(0.3)
    log.info(f"  {label}: {total} scanned | {hits} hits")

def _scan_events(pool):
    hits=0
    for page in range(PAGES):
        data=api_get(f"{GAMMA_BASE}/events",{
            "tag":"weather","active":"true","closed":"false","order":"createdAt",
            "ascending":"false","limit":PAGE_LIMIT,"offset":page*PAGE_LIMIT})
        if not data: break
        events=data if isinstance(data,list) else data.get("data",[])
        if not events: break
        for ev in events:
            gslug=ev.get("slug",""); children=ev.get("markets",[]) or []
            if not is_weather_market({**ev,"slug":gslug}):
                if not any(is_weather_market(cm) for cm in children): continue
            if gslug and gslug not in pool:
                vol=sum(_pf(cm.get("volume")) for cm in children) if children else _pf(ev.get("volume"))
                q=ev.get("title") or (children[0].get("question","") if children else "") or gslug
                pool[gslug]={"slug":gslug,"groupSlug":gslug,
                             "url":f"https://polymarket.com/event/{gslug}",
                             "question":q,"volume":vol,"createdAt":ev.get("createdAt",""),
                             "endDate":ev.get("endDate",""),"markets":children}
                hits+=1
        if len(events)<PAGE_LIMIT: break
        time.sleep(0.3)
    log.info(f"  S3: {hits} hits")

def fetch_all_events()->list:
    pool={}
    log.info("[S1] volume DESC"); _scan_page(pool,{"order":"volume","ascending":"false"},"S1")
    log.info("[S2] endDate ASC"); _scan_page(pool,{"order":"endDate","ascending":"true"},"S2")
    log.info("[S3] events?tag=weather"); _scan_events(pool)
    log.info(f"Total: {len(pool)}")
    return list(pool.values())

# ══════════════════════════════════════════════════════════════════════════
#  §7  BRACKET PARSING
# ══════════════════════════════════════════════════════════════════════════
def _bounds_label(s):
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
    s=q.lower()
    m=re.search(r'between\s+(-?\d+\.?\d*)\s*[-–and]+\s*(-?\d+\.?\d*)',s)
    if m: return float(m.group(1)),float(m.group(2))
    m=re.search(r'(-?\d+\.?\d*)\s*°?\s*[cf]?\s*(?:or higher|or above|or more|\+)',s,re.I)
    if m: return float(m.group(1)),INF
    m=re.search(r'(?:above|exceed|over|more than|higher than|at least)\s+(-?\d+\.?\d*)',s)
    if m: return float(m.group(1)),INF
    m=re.search(r'(-?\d+\.?\d*)\s*°?\s*[cf]?\s*(?:or below|or lower|or less)',s,re.I)
    if m: return -INF,float(m.group(1))
    m=re.search(r'(?:below|under|less than|lower than|at most)\s+(-?\d+\.?\d*)',s)
    if m: return -INF,float(m.group(1))
    m=re.search(r'\bbe\s+(-?\d+\.?\d*)\s*°',s)
    if m: v=float(m.group(1)); return v-0.5,v+0.5
    return None,None

def _parse_mkt(m:dict)->list:
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
    if len(outcomes)==2 and "Yes" in outcomes:
        yi=outcomes.index("Yes"); yes_p=float(prices[yi]) if yi<len(prices) else 0.5
        lo,hi=_bounds_question(q)
        if lo is not None:
            brackets.append({"label":q[:60],"low":lo,"high":hi,"yes_price":yes_p,
                              "volume":vol,"yes_token_id":yes_tok,"no_token_id":no_tok})
    else:
        for i,label in enumerate(outcomes):
            price=float(prices[i]) if i<len(prices) else 0.0
            lo,hi=_bounds_label(label)
            if lo is None: continue
            brackets.append({"label":label,"low":lo,"high":hi,"yes_price":price,
                              "volume":vol/max(len(outcomes),1),
                              "yes_token_id":yes_tok,"no_token_id":no_tok})
    return brackets

def fetch_brackets(ev:dict)->list:
    slug=ev.get("groupSlug") or ev.get("slug",""); all_raw=[]
    if slug:
        data=api_get(f"{GAMMA_BASE}/events",{"slug":slug})
        if data:
            events=data if isinstance(data,list) else data.get("data",[])
            if events: all_raw.extend(events[0].get("markets",[]) or [])
    if slug and len(all_raw)<8:
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
                "timezone":"auto"},timeout=12,headers={"User-Agent":"PolyWeather/9.2"})
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
            timeout=8,headers={"User-Agent":"PolyWeather/9.2"})
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
            timeout=8,headers={"User-Agent":"PolyWeather/9.2"})
        r.raise_for_status(); data=r.json()
        obs=data.get("response",{}).get("data",{}).get("METAR",[])
        tc=(obs[0].get("temp_c") if isinstance(obs,list) and obs
            else obs.get("temp_c") if isinstance(obs,dict) else None)
        result=float(tc) if tc is not None else None
    except Exception as e: log.debug(f"METAR {station}: {e}")
    _metar_cache[station]=result; return result

def kalman_update(prior_mu:float,prior_sigma:float,
                  metar_temp:float,city:str,month:int)->Tuple[float,float]:
    """METAR Kalman update. obs_sigma=0.5 (METAR ±0.5°C). BUKAN 1.0."""
    htable=DAYTIME_HEATING.get(city,{})
    heating=(htable.get(month,DAYTIME_HEATING["default"])
             if isinstance(htable,dict) else DAYTIME_HEATING["default"])
    obs_mu=metar_temp+heating; obs_sigma=0.5
    pr=1.0/max(prior_sigma**2,0.01); po=1.0/(obs_sigma**2)
    post_mu=(prior_mu*pr+obs_mu*po)/(pr+po)
    post_sigma=math.sqrt(1.0/(pr+po))
    log.info(f"  METAR Kalman: μ {prior_mu:.1f}→{post_mu:.1f}° σ {prior_sigma:.2f}→{post_sigma:.2f}°")
    return round(post_mu,2),round(post_sigma,3)

# ══════════════════════════════════════════════════════════════════════════
#  §9  MODEL STACK
# ══════════════════════════════════════════════════════════════════════════
def m3_bayesian(mu_n,sig_n,mu_c,sig_c=3.0):
    tn=1/max(sig_n**2,0.01); tc=1/max(sig_c**2,0.01); tp=tn+tc
    return round((tn*mu_n+tc*mu_c)/tp,3),round(math.sqrt(1/tp),3),tn,tc

def m5_hmm(mu,clim,sig_c=3.0):
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

def m11_garch(sig_ens,lead=48)->float:
    lr=math.sqrt(0.10/max(1-0.20-0.70,0.01))
    return round(max(1.0,min(0.5*_mae(lead)+0.3*sig_ens+0.2*lr,5.0)),3)

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
#  §10  ENSEMBLE RUNNER
# ══════════════════════════════════════════════════════════════════════════
def run_ensemble(city,mu_nwp,sig_ens,lead,month,brackets)->EnsembleResult:
    clim=CLIMATE_NORMALS.get(city,{}).get(month)
    mu_clim=clim[0] if clim else mu_nwp; sig_clim=3.0
    mae_nwp=_mae(lead); w_nwp=1/max(mae_nwp,0.1)
    models=[]
    models.append(ModelResult("M1","NWP-OpenMeteo",mu_nwp,mae_nwp,w_nwp,f"MAE={mae_nwp:.2f}"))
    mu_post,sig_post,tn,tc=m3_bayesian(mu_nwp,sig_ens,mu_clim,sig_clim)
    w_bay=1/max(sig_post,0.1)
    models.append(ModelResult("M3","Bayesian-post",mu_post,sig_post,w_bay,
                               f"prior={mu_clim:.1f} τ_n={tn:.3f}"))
    models.append(ModelResult("M4","ECMWF-ENS",mu_nwp,sig_ens,w_nwp*1.2))
    sig_g=m11_garch(sig_ens,lead)
    models.append(ModelResult("M11","GARCH-σ",mu_nwp,sig_g,w_nwp*0.8))
    state,p_hot,z=m5_hmm(mu_post,mu_clim,sig_clim)
    mu_hmm=mu_post+(0.5 if state=="HOT" else -0.5 if state=="COLD" else 0)
    models.append(ModelResult("M5","HMM-regime",mu_hmm,sig_post*0.95,w_bay*0.7,
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
        denom=max(sum(b["yes_price"] for b in brackets),1.0)
        kl_a=round(sum(b5_kl(mc3.get(b["label"],0.5),
                              b["yes_price"]/denom) for b in brackets),4)
    rd=m109_robust(sq,kl_a,lead)
    fs,fp,_=m5_hmm(muf,mu_clim,sig_clim)
    return EnsembleResult(mu_ensemble=round(muf,2),sigma_ensemble=sigf,models=models,
                          skill_score=skill_score(lead),kl_total=kl_a,
                          hmm_state=fs,hmm_hot_p=fp,hurst_adj=hmult,
                          dynamic_k_mult=t14_dyn(lead),robust_disc=rd,
                          n_active=sum(1 for m in models if m.active))

# ══════════════════════════════════════════════════════════════════════════
#  §11  QUANT PIPELINE
# ══════════════════════════════════════════════════════════════════════════
def bracket_short(b:dict)->str:
    lo,hi=b["low"],b["high"]; lbl=b.get("label","")
    if any(x in lbl.lower() for x in ["°f","fahrenheit"]): unit="°F"
    elif any(x in lbl.lower() for x in ["°c","celsius"]): unit="°C"
    else:
        ref=hi if hi!=INF else (lo if lo!=-INF else 0)
        unit="°F" if abs(ref)>50 else "°C"
    if lo==-INF and hi!=INF: return f"≤{hi:.0f}{unit}"
    elif hi==INF and lo!=-INF: return f"≥{lo:.0f}{unit}"
    return f"{lo:.0f}-{hi:.0f}{unit}"

def calc_implied_mu(brackets:list)->Optional[float]:
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
                        exotic_k_mult:float=1.0):
    total_mkt=sum(b["yes_price"] for b in brackets); denom=max(total_mkt,1.0)
    ovr=max(0.0,total_mkt-1.0); sigma_ok=sigma<2.5
    corr_pen=get_corr_penalty(city,active_cities)
    port_lim=bankroll*KELLY_PORT_PCT/MAX_MKTS

    # Kelly fraction based on validation mode
    kf_mult={"DRY_RUN":0.0,"QUARTER":0.25,"HALF":0.50,"FULL":1.0}.get(kelly_mode,0.5)

    out=[]
    for b in brackets:
        lo,hi=b["low"],b["high"]
        p_g,cdf_str=cdf_formula(lo,hi,mu,sigma)
        p_mc=mc_probs.get(b["label"],p_g); p_blend=round(0.5*p_g+0.5*p_mc,5)
        p_mkt_n=b["yes_price"]/denom
        yes_edge=(p_blend-p_mkt_n)*100; no_edge=-yes_edge
        kl_b=b5_kl(p_blend,max(p_mkt_n,0.01))
        is_tail=lo==-INF or hi==INF

        yes_tok=str(b.get("yes_token_id") or "")
        no_tok =str(b.get("no_token_id") or "")
        ya=best_asks.get(yes_tok); yb=best_bids.get(yes_tok)
        na=best_asks.get(no_tok);  nb=best_bids.get(no_tok)

        yes_thr=get_spread_threshold(dynamic_thr,ya,yb)
        no_thr =get_spread_threshold(dynamic_thr,na,nb)
        yes_map=round(p_blend-EDGE_THRESHOLD/100,3)
        no_map =round((1-p_blend)-EDGE_THRESHOLD/100,3)
        yes_ask_ok=ya is None or ya<=yes_map
        no_ask_ok =na is None or na<=no_map

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

        def _cls(edge,dk,thr):
            if abs(edge)>=EDGE_STRONG and dk and sigma_ok and prox_ok: return "STRONG"
            if abs(edge)>=thr and dk and sigma_ok and prox_ok: return "SIGNAL"
            if abs(edge)>=EDGE_THRESHOLD*0.80: return "NEAR"
            return "PASS"

        yes_cls=_cls(yes_edge,dok_yes,yes_thr)
        no_cls =_cls(no_edge, dok_no, no_thr)

        # True ½ Kelly (or QUARTER/FULL based on validation mode)
        yes_b=(1-p_mkt_n)/max(p_mkt_n,0.001)
        no_b = p_mkt_n/max(1-p_mkt_n,0.001)
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
            "p_mkt_norm":round(p_mkt_n,5),
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
    DOMAIN={"name":"Polymarket CTF Exchange","version":"1","chainId":137,
             "verifyingContract":"0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"}
    ORDER_TYPE=[
        {"name":"salt","type":"uint256"},{"name":"maker","type":"address"},
        {"name":"signer","type":"address"},{"name":"taker","type":"address"},
        {"name":"tokenId","type":"uint256"},{"name":"makerAmount","type":"uint256"},
        {"name":"takerAmount","type":"uint256"},{"name":"expiration","type":"uint256"},
        {"name":"nonce","type":"uint256"},{"name":"feeRateBps","type":"uint256"},
        {"name":"side","type":"uint8"},{"name":"signatureType","type":"uint8"},
    ]
    def __init__(self):
        self.api_key=CLOB_API_KEY; self.api_secret=CLOB_API_SECRET
        self.passphrase=CLOB_API_PASSPHRASE; self.pk=POLY_PRIVATE_KEY
        self.funder=POLY_FUNDER; self.account=None; self.signer=""; self.enabled=False
        if all([self.api_key,self.api_secret,self.passphrase,self.pk,self.funder]):
            try:
                from eth_account import Account
                self.account=Account.from_key(self.pk); self.signer=self.account.address
                self.enabled=True; log.info(f"CLOB: {self.signer[:12]}...")
            except ImportError: log.error("pip install eth-account")
            except Exception as e: log.error(f"CLOB init: {e}")

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
        od={"salt":int(time.time()*1000),"maker":self.funder,"signer":self.signer,
            "taker":"0x0000000000000000000000000000000000000000",
            "tokenId":int(token_id),"makerAmount":int(size_usdc*1e6),
            "takerAmount":int(size_usdc/price*1e6),"expiration":0,
            "nonce":0,"feeRateBps":0,"side":0,"signatureType":0}
        try:
            sig=self._sign(od)
        except Exception as e:
            return OrderResult(label,bshort,"",str(token_id),price,size_usdc,
                               status="failed",error=f"sign:{e}",placed_at=ts)
        pl=json.dumps({"order":{**od,"signature":sig},"owner":self.funder,"orderType":"GTC"})
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
    USDC="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    def __init__(self,default=DEFAULT_BANKROLL): self.default=default
    def read_chain(self,wallet)->Optional[float]:
        try:
            padded=wallet.lower().replace("0x","").zfill(64)
            pl={"jsonrpc":"2.0","method":"eth_call",
                "params":[{"to":self.USDC,"data":f"0x70a08231{padded}"},"latest"],"id":1}
            r=requests.post(POLY_RPC,json=pl,timeout=8); r.raise_for_status()
            return round(int(r.json().get("result","0x0"),16)/1e6,2)
        except: return None
    def get(self)->float:
        bal=self.read_chain(POLY_FUNDER or "")
        if bal and bal>=1.0:
            log.info(f"Bankroll (chain): ${bal:.2f}")
            safe_write(BANKROLL_FILE,{"bankroll":bal,"ts":datetime.now(timezone.utc).isoformat()})
            return bal
        d=safe_read(BANKROLL_FILE,{})
        bal=float(d.get("bankroll",self.default))
        log.info(f"Bankroll (cache): ${bal:.2f}"); return bal

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
                    log.info(f"  FILLED: {o['short']} id={o['order_id'][:12]}")
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
        self.data["predictions"][key]={"p_model":p_model,"side":side,
            "ts":datetime.now(timezone.utc).isoformat(),"resolved":False}
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
        n=self.data["n_resolved"]; avg=self.data["cumulative_bs"]
        if n==0: return "BS=n/a (n=0)"
        exp=0.28*0.72; status="CALIBRATED" if avg<exp*1.30 else "NEEDS TUNING"
        return f"BS={avg:.4f} (n={n}) E[BS]={exp:.4f} {status}"

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
    Modes: DRY_RUN → QUARTER → HALF
    
    Upgrade ke HALF: WR>17%, BS<E[BS]×1.30, DD<20%, n≥14 bets
    Upgrade ke QUARTER: WR>14%, partial checks, n≥7 bets
    """
    n=brier.data.get("n_resolved",0)
    bs=brier.recent_bs(); avg_p=0.28; exp_bs=avg_p*(1-avg_p)
    bs_ratio=exp_bs/bs if bs>0 else 1.0
    resolved=brier.data.get("resolved",[])
    wins=sum(1 for b in resolved if b<0.25)
    wr=wins/max(n,1)
    dd=(start_bk-bankroll)/max(start_bk,1) if start_bk>0 else 0

    wr_ok=wr>0.17; bs_ok=bs_ratio>0.77; dd_ok=dd<0.20

    if DRY_RUN:
        return "DRY_RUN",f"DRY_RUN mode — n={n} predictions logged"
    if n>=14 and wr_ok and bs_ok and dd_ok:
        return "HALF",f"Validation OK: WR={wr*100:.0f}% BS={bs:.4f} n={n} → ½ Kelly"
    if n>=7 and (wr>0.14 or bs_ratio>0.70) and dd_ok:
        return "QUARTER",f"Phase 1: WR={wr*100:.0f}% BS={bs:.4f} n={n} → ¼ Kelly"
    if n<7:
        return "QUARTER",f"Accumulating data: {n}/7 bets → ¼ Kelly until n=7"
    return "QUARTER",f"Model needs monitoring: WR={wr*100:.0f}% BS={bs:.4f} n={n}"

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

    total_mkt=max(sum(b["yes_price"] for b in bd),1.0)
    lines2=[f"<b>BRACKETS  μ={mu:.1f}  σ={sigma:.2f}  thr={bd[0]['yes_thr']:.0f}pp</b>",""]
    for b in bd:
        ym=b["yes_price"]/total_mkt*100; nm=100-ym
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
        if b["stake_yes"]>=1: lines2.append(f"  Kelly YES: ${b['stake_yes']:.2f} f={b['f_yes']*100:.1f}%")
        if b["stake_no"] >=1: lines2.append(f"  Kelly NO:  ${b['stake_no']:.2f} f={b['f_no']*100:.1f}%")
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
                mkt_ask=b["yes_price"]/total_mkt
                lp=min(round(mkt_ask+0.02,3),b["yes_map"])
                lines3.append(f">> {sh:<12} BET YES  +{b['yes_edge']:.1f}pp  {otype}")
                lines3.append(f"   model {b['p_blend']*100:.1f}%  market {mkt_ask*100:.1f}%")
                lines3.append(f"   entry @ {lp:.3f}  MAP={b['yes_map']:.3f}")
                if b["stake_yes"]>=1: lines3.append(f"   Kelly: ${b['stake_yes']:.2f}  f={b['f_yes']*100:.1f}%")
            if b["no_class"] in ("SIGNAL","STRONG"):
                otype="[MARKET ORDER]" if b["no_class"]=="STRONG" else "[LIMIT +2c]"
                mkt_no=1-b["yes_price"]/total_mkt
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
        lines3.append("<b>ORDERS</b>"); lines3.append("<pre>")
        for o in order_results:
            st="DRY_RUN" if o.status=="dry_run" else o.status.upper()
            lines3.append(f"{o.side:<3} {o.bracket_short:<12} ${o.size_usdc:.2f}@{o.limit_price:.3f} {o.order_type} {st}")
            if o.order_id and o.order_id!="DRY_RUN": lines3.append(f"    id={o.order_id[:20]}...")
            if o.error: lines3.append(f"    err={o.error[:50]}")
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
                    price_vel,cat_tracker,force_market_order=False):
    question=ev.get("question",""); url=ev.get("url",""); vol=ev.get("volume",0)
    age_h=_market_age_hours(ev)

    city=extract_city(question) or \
         extract_city((ev.get("groupSlug") or ev.get("slug","")).replace("-"," "))
    if not city: log.info("  Skip: city unknown"); return None

    target_date=extract_date(question)
    coords=CITY_COORDS[city]
    mu_raw,_,lead=fetch_nwp(coords[0],coords[1],target_date)
    is_f="°f" in question.lower() or "fahrenheit" in question.lower()
    if is_f: mu_raw=mu_raw*9/5+32

    station=CITY_STATION.get(city,""); bias=STATION_BIAS.get(station,0.0)
    adj_mu=round(mu_raw+bias,2)
    sig_ens=fetch_ens_sigma(coords[0],coords[1])
    brackets=fetch_brackets(ev)
    if not brackets: log.info("  Skip: no brackets"); return None

    month=target_date.month if target_date else datetime.now().month
    ens=run_ensemble(city,adj_mu,sig_ens,lead,month,brackets)
    mu=ens.mu_ensemble; sigma=ens.sigma_ensemble

    # METAR Kalman (only T-0)
    metar_updated=False
    if lead<=24 and station:
        mc=fetch_metar(station)
        if mc is not None:
            mv=mc*9/5+32 if is_f else mc
            mu,sigma=kalman_update(mu,sigma,mv,city,month)
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

    bd=calc_brackets_full(mu,sigma,brackets,mc_probs,bankroll,ens,
                           active_cities,best_asks,best_bids,city,
                           kelly_mode,eff_thr,bs_mult,cat_tracker,exotic_k_mult)

    ens.kl_total=round(sum(b["kl"] for b in bd),4)
    max_stake=max((max(b["stake_yes"],b["stake_no"]) for b in bd),default=1.0)
    ens.fmea_rpn=f11_fmea(sigma,lead,vol,bd[0]["overround"] if bd else 0,max_stake,bankroll)
    mu_mkt=calc_implied_mu(brackets)
    ovr=bd[0]["overround"] if bd else 0.0

    perf.log(slug,city,question,mu,sigma,mu_mkt,bd,ens.kl_total,
             ens.skill_score,lead,age_h or 0,metar_updated,kelly_mode)

    # Age multiplier
    age_mult=(1.0 if (age_h or 99)<2 else 0.85 if (age_h or 99)<6
              else 0.70 if (age_h or 99)<12 else 0.50)
    age_thr  =(dynamic_thr if (age_h or 99)<2 else
               dynamic_thr+2 if (age_h or 99)<6 else
               dynamic_thr+4 if (age_h or 99)<12 else dynamic_thr+6)

    order_results=[]
    if kelly_mode!="DRY_RUN":
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

                if side=="YES": b["skip_reason_yes"]=skip
                else:           b["skip_reason_no"] =skip
                if skip: continue

                adj_stake=stake*age_mult
                ok,reason,adj_stake=risk_gate.check(bankroll,adj_stake,
                                                      brier.recent_bs(),bot_state)
                if not ok:
                    if side=="YES": b["skip_reason_yes"]=reason
                    else:           b["skip_reason_no"] =reason
                    log.info(f"  RG: {reason}"); continue

                total_mkt_s=max(sum(x["yes_price"] for x in bd),1.0)
                if side=="YES": base_ask=ask_v or b["yes_price"]/total_mkt_s
                else:           base_ask=ask_v or 1-b["yes_price"]/total_mkt_s
                limit_price=min(round(base_ask+0.02,3),map_v)

                # Execution tier: MARKET for STRONG, LIMIT for SIGNAL
                use_market = (cls=="STRONG" or force_market_order) and clob.enabled
                if use_market:
                    result=clob.place_market_order(str(tok or ""),side,adj_stake,b["label"])
                else:
                    result=clob.place_limit_order(str(tok or ""),side,limit_price,adj_stake,b["label"])

                result.bracket_short=b["bracket_short"]; result.map_price=map_v
                order_results.append(result)
                brier.record(slug,b["label"],b["p_blend"],side)
                time.sleep(0.3)

    if city not in active_cities: active_cities.append(city)

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
    log.info(f"  Polymarket Bot v9.2  FAST={FAST_SCAN}  DRY_RUN={DRY_RUN}")
    log.info("="*65)

    bm=BankrollManager(); bankroll=bm.get()
    clob=ClobOrderClient(); rg=RiskGate()
    brier=BrierTracker(); perf=PerfLogger()
    pending=PendingTracker(); bot_state=load_bot_state()
    cat_tracker=CategoryTracker(); price_vel=PriceVelocity()

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
    log.info(f"Bankroll:   ${bankroll:.2f}")
    log.info(f"Brier:      {brier.summary()}")
    log.info(f"DynThresh:  {brier.get_dynamic_threshold():.0f}pp")

    # Telegram commands
    flags=check_tg_commands(bot_state)
    if flags["emergency_stop"]:
        send_telegram("<b>EMERGENCY STOP.</b>")
        bot_state["paused"]=True; safe_write(BOT_STATE,bot_state); return
    if flags["pause"]:
        bot_state["paused"]=True; safe_write(BOT_STATE,bot_state)
        send_telegram("<b>PAUSED via /pause.</b>"); return
    if flags["resume"]:
        bot_state["paused"]=False; safe_write(BOT_STATE,bot_state)
        send_telegram("<b>RESUMED via /resume.</b>")
    if flags["status_req"]:
        send_telegram(f"<pre>STATUS v9.2\nBankroll: ${bankroll:.2f}\n"
                      f"Kelly: {kelly_mode}\nDRY_RUN: {DRY_RUN}\nFAST: {FAST_SCAN}\n"
                      f"{brier.summary()}\nThresh: {brier.get_dynamic_threshold():.0f}pp\n"
                      f"CLOB: {'enabled' if clob.enabled else 'disabled'}</pre>")
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
                                  price_vel,cat_tracker,force_mkt_order)
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
    log.info(f"\n{'='*65}\n  v9.2 DONE  sent={stats['sent']}  err={stats['skip_err']}"
             f"  {dur:.0f}s  Kelly={kelly_mode}\n{'='*65}")


if __name__=="__main__":
    main()
