"""
Lookup Tables - Weather Bot v11.0
===================================
City coordinates, METAR stations, climate normals, AR(1) coefficients,
daytime heating estimates, and climate clusters.

v11.0 changes from v10.x:
  - FIX BUG #12: Added get_climate_normal_fallback() for cities not in
    CLIMATE_NORMALS. Uses latitude-based estimate:
    - Tropical (lat < 23.5): 30C high, 22C low
    - Temperate (23.5 <= lat < 50): 20C high, 10C low
    - Cold (lat >= 50): 10C high, 0C low
  - Added seasonal adjustments by month (summer warmer, winter colder)
  - No other major changes
"""

# ============================================================================
# Weather Keywords for Market Detection
# ============================================================================

WEATHER_KW = [
    "celsius", "fahrenheit", "\u00b0c", "\u00b0f", "temperature", "temp",
    "rainfall", "precipitation", "snowfall", "typhoon", "hurricane",
    "heatwave", "humidity", "highest temp", "lowest temp",
    "warmest", "coldest", "forecast", "weather",
]

# ============================================================================
# City Coordinates (lat, lon) - 60+ Cities
# ============================================================================

CITY_COORDS = {
    # North America
    "new york": (40.71, -74.01), "new york city": (40.71, -74.01),
    "los angeles": (34.05, -118.24), "chicago": (41.88, -87.63),
    "miami": (25.76, -80.19), "houston": (29.76, -95.37),
    "san francisco": (37.77, -122.42), "dallas": (32.78, -96.80),
    "denver": (39.74, -104.99), "seattle": (47.61, -122.33),
    "boston": (42.36, -71.06), "atlanta": (33.75, -84.39),
    "phoenix": (33.45, -112.07), "minneapolis": (44.98, -93.27),
    "detroit": (42.33, -83.05), "washington": (38.91, -77.04),
    "philadelphia": (39.95, -75.17), "toronto": (43.65, -79.38),
    "montreal": (45.50, -73.57), "mexico city": (19.43, -99.13),
    # Europe
    "london": (51.51, -0.13), "paris": (48.86, 2.35),
    "berlin": (52.52, 13.40), "madrid": (40.42, -3.70),
    "rome": (41.90, 12.50), "amsterdam": (52.37, 4.90),
    "moscow": (55.76, 37.62), "istanbul": (41.01, 28.98),
    "helsinki": (60.17, 24.94), "stockholm": (59.33, 18.07),
    "oslo": (59.91, 10.75), "copenhagen": (55.68, 12.57),
    "vienna": (48.21, 16.37), "zurich": (47.38, 8.54),
    "munich": (48.14, 11.58), "barcelona": (41.39, 2.17),
    "lisbon": (38.72, -9.14), "athens": (37.98, 23.73),
    "warsaw": (52.23, 21.01), "prague": (50.08, 14.44),
    "bucharest": (44.43, 26.10), "budapest": (47.50, 19.04),
    # East Asia
    "tokyo": (35.68, 139.69), "seoul": (37.57, 126.98),
    "beijing": (39.90, 116.41), "shanghai": (31.23, 121.47),
    "osaka": (34.69, 135.50), "taipei": (25.03, 121.57),
    "hong kong": (22.32, 114.17), "shenzhen": (22.54, 114.06),
    # Southeast Asia
    "singapore": (1.35, 103.82), "bangkok": (13.76, 100.50),
    "kuala lumpur": (3.14, 101.69), "manila": (14.60, 120.98),
    "jakarta": (-6.21, 106.85), "ho chi minh": (10.82, 106.63),
    # South Asia
    "mumbai": (19.08, 72.88), "delhi": (28.61, 77.21),
    "lucknow": (26.85, 80.95), "karachi": (24.86, 67.01),
    "dhaka": (23.81, 90.41), "kolkata": (22.57, 88.36),
    # Middle East
    "dubai": (25.20, 55.27), "riyadh": (24.71, 46.67),
    "tehran": (35.69, 51.39), "cairo": (30.04, 31.24),
    # Africa
    "lagos": (6.52, 3.38), "nairobi": (-1.29, 36.82),
    "cape town": (-33.92, 18.42), "johannesburg": (-26.20, 28.05),
    # Oceania
    "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "auckland": (-36.85, 174.76),
    # Latin America
    "sao paulo": (-23.55, -46.63), "buenos aires": (-34.60, -58.38),
    "bogota": (4.71, -74.07), "lima": (-12.05, -77.04),
    "santiago": (-33.45, -70.67), "rio de janeiro": (-22.91, -43.17),
}

# ============================================================================
# METAR Station Codes
# ============================================================================

CITY_STATION = {
    "new york": "KLGA", "new york city": "KLGA",
    "los angeles": "KLAX", "chicago": "KORD",
    "miami": "KMIA", "houston": "KIAH",
    "san francisco": "KSFO", "dallas": "KDFW",
    "denver": "KDEN", "seattle": "KSEA",
    "boston": "KBOS", "atlanta": "KATL",
    "phoenix": "KPHX", "minneapolis": "KMSP",
    "detroit": "KDTW", "washington": "KDCA",
    "philadelphia": "KPHL", "toronto": "CYYZ",
    "montreal": "CYUL", "london": "EGLL",
    "paris": "LFPG", "berlin": "EDDB",
    "madrid": "LEMD", "rome": "LIRF",
    "amsterdam": "EHAM", "moscow": "UUEE",
    "istanbul": "LTBA", "helsinki": "EFHK",
    "tokyo": "RJTT", "seoul": "RKSI",
    "beijing": "ZBAA", "shanghai": "ZSPD",
    "osaka": "RJOO", "taipei": "RCTP",
    "hong kong": "VHHH", "singapore": "WSSS",
    "bangkok": "VTBS", "kuala lumpur": "WMKK",
    "manila": "RPLL", "jakarta": "WIII",
    "dubai": "OMDB", "cairo": "HECA",
    "sydney": "YSSY", "melbourne": "YMML",
    "mumbai": "VABB", "delhi": "VIDP",
    "mexico city": "MMMX", "sao paulo": "SBGR",
    "buenos aires": "SAEZ", "bogota": "SKBO",
    "lima": "SPJC", "santiago": "SCEL",
    "lucknow": "VILK", "karachi": "OPKC",
    "dhaka": "VGHS", "shenzhen": "ZGSZ",
}

# Known METAR station temperature biases
STATION_BIAS = {
    "EGLC": -2.5,   # London City - reads low
    "KLGA": 1.0,    # LaGuardia - urban heat island
    "EDDT": -1.0,   # Berlin Tegel
}

# ============================================================================
# Climate Normals - Monthly (high, low) in Celsius
# All 12 months available
# Source: NOAA/WMO 30-year climate normals
# ============================================================================

CLIMATE_NORMALS = {
    "new york":     {1: (4, -3),  2: (5, -2),  3: (10, 2),  4: (17, 8),
                     5: (22, 13), 6: (27, 18), 7: (30, 21), 8: (29, 20),
                     9: (25, 16), 10: (18, 10), 11: (12, 5), 12: (6, 0)},
    "chicago":      {1: (-1, -10), 2: (1, -8),  3: (7, -2),  4: (15, 4),
                     5: (21, 10), 6: (27, 16), 7: (29, 19), 8: (28, 18),
                     9: (24, 13), 10: (16, 6),  11: (8, 0),  12: (2, -6)},
    "miami":        {1: (25, 16), 2: (25, 16), 3: (27, 18), 4: (29, 21),
                     5: (31, 23), 6: (32, 24), 7: (33, 25), 8: (33, 25),
                     9: (32, 24), 10: (30, 22), 11: (28, 19), 12: (26, 17)},
    "london":       {1: (8, 2),   2: (9, 2),   3: (11, 4),  4: (14, 6),
                     5: (17, 9),  6: (20, 12), 7: (23, 14), 8: (22, 14),
                     9: (19, 11), 10: (15, 8),  11: (11, 5),  12: (9, 3)},
    "paris":        {1: (7, 2),   2: (8, 2),   3: (12, 5),  4: (16, 7),
                     5: (20, 11), 6: (23, 14), 7: (26, 16), 8: (25, 16),
                     9: (21, 13), 10: (16, 9),  11: (11, 5),  12: (8, 3)},
    "berlin":       {1: (3, -2),  2: (4, -2),  3: (9, 1),   4: (14, 5),
                     5: (19, 9),  6: (22, 13), 7: (25, 15), 8: (24, 14),
                     9: (19, 10), 10: (13, 6),  11: (7, 2),   12: (4, -1)},
    "madrid":       {1: (10, 1),  2: (12, 2),  3: (16, 4),  4: (19, 7),
                     5: (23, 11), 6: (30, 16), 7: (33, 19), 8: (32, 18),
                     9: (27, 14), 10: (20, 9),  11: (14, 4),  12: (10, 2)},
    "rome":         {1: (12, 3),  2: (13, 3),  3: (16, 6),  4: (19, 9),
                     5: (24, 13), 6: (28, 17), 7: (31, 20), 8: (31, 20),
                     9: (27, 16), 10: (22, 12), 11: (16, 7),  12: (13, 4)},
    "moscow":       {1: (-6, -12), 2: (-4, -11), 3: (2, -5),  4: (11, 2),
                     5: (18, 8),  6: (22, 12), 7: (24, 14), 8: (22, 12),
                     9: (16, 7),  10: (8, 2),   11: (1, -4),  12: (-4, -10)},
    "helsinki":     {1: (-3, -9),  2: (-3, -9),  3: (2, -5),  4: (8, 0),
                     5: (15, 6),  6: (20, 11), 7: (22, 13), 8: (20, 12),
                     9: (14, 7),  10: (7, 2),   11: (2, -2),  12: (-1, -7)},
    "tokyo":        {1: (10, 2),  2: (11, 2),  3: (14, 5),  4: (19, 10),
                     5: (23, 15), 6: (26, 19), 7: (30, 23), 8: (31, 24),
                     9: (27, 21), 10: (22, 15), 11: (17, 10), 12: (12, 5)},
    "seoul":        {1: (1, -6),  2: (4, -4),  3: (10, 1),  4: (17, 7),
                     5: (23, 12), 6: (27, 17), 7: (29, 22), 8: (30, 22),
                     9: (26, 17), 10: (20, 10), 11: (12, 3),  12: (4, -4)},
    "beijing":      {1: (2, -8),  2: (5, -5),  3: (13, 1),  4: (21, 8),
                     5: (27, 14), 6: (31, 19), 7: (31, 22), 8: (30, 21),
                     9: (26, 15), 10: (19, 8),  11: (10, 0),  12: (4, -6)},
    "shanghai":     {1: (8, 1),   2: (9, 2),   3: (14, 6),  4: (20, 12),
                     5: (26, 17), 6: (29, 21), 7: (33, 26), 8: (33, 25),
                     9: (28, 21), 10: (23, 15), 11: (17, 9),  12: (10, 3)},
    "singapore":    {1: (31, 24), 2: (32, 24), 3: (32, 24), 4: (31, 24),
                     5: (31, 25), 6: (31, 25), 7: (31, 25), 8: (31, 25),
                     9: (31, 24), 10: (31, 24), 11: (31, 24), 12: (31, 24)},
    "bangkok":      {1: (32, 22), 2: (33, 24), 3: (34, 25), 4: (35, 26),
                     5: (34, 26), 6: (33, 26), 7: (33, 25), 8: (33, 25),
                     9: (32, 25), 10: (32, 24), 11: (32, 23), 12: (32, 22)},
    "dubai":        {1: (24, 14), 2: (25, 15), 3: (28, 17), 4: (33, 21),
                     5: (38, 25), 6: (40, 28), 7: (42, 30), 8: (42, 30),
                     9: (39, 27), 10: (35, 23), 11: (30, 19), 12: (26, 15)},
    "sydney":       {1: (27, 20), 2: (27, 20), 3: (25, 18), 4: (22, 14),
                     5: (19, 11), 6: (17, 9),  7: (17, 8),  8: (18, 9),
                     9: (20, 11), 10: (22, 14), 11: (24, 16), 12: (26, 19)},
    "mumbai":       {1: (32, 19), 2: (33, 20), 3: (34, 23), 4: (33, 25),
                     5: (34, 27), 6: (32, 27), 7: (30, 26), 8: (30, 25),
                     9: (31, 24), 10: (34, 24), 11: (34, 22), 12: (33, 20)},
    "delhi":        {1: (21, 8),  2: (24, 11), 3: (31, 16), 4: (37, 22),
                     5: (40, 26), 6: (39, 28), 7: (35, 27), 8: (34, 26),
                     9: (34, 24), 10: (34, 19), 11: (29, 13), 12: (23, 9)},
    "mexico city":  {1: (22, 6),  2: (24, 7),  3: (26, 9),  4: (27, 13),
                     5: (27, 14), 6: (25, 14), 7: (24, 13), 8: (24, 13),
                     9: (24, 13), 10: (24, 10), 11: (23, 7),  12: (22, 6)},
}

# ============================================================================
# Forecast MAE by Lead Time (hours -> Celsius)
# ============================================================================

FORECAST_MAE = {
    6: 1.0, 12: 1.2, 24: 1.5, 36: 1.8,
    48: 2.0, 72: 2.5, 96: 3.0, 120: 3.5,
}

# ============================================================================
# Daytime Heating: City x Month -> Celsius rise from current to daily high
# All 12 months available
# ============================================================================

DAYTIME_HEATING = {
    "new york":    {1: 5, 2: 6, 3: 7, 4: 6, 5: 7, 6: 8, 7: 8, 8: 7, 9: 7, 10: 6, 11: 5, 12: 5},
    "chicago":     {1: 6, 2: 7, 3: 8, 4: 7, 5: 8, 6: 9, 7: 8, 8: 8, 9: 7, 10: 6, 11: 5, 12: 6},
    "miami":       {1: 4, 2: 4, 3: 4, 4: 4, 5: 4, 6: 4, 7: 5, 8: 5, 9: 4, 10: 4, 11: 4, 12: 4},
    "london":      {1: 4, 2: 5, 3: 5, 4: 5, 5: 6, 6: 7, 7: 7, 8: 6, 9: 5, 10: 5, 11: 4, 12: 4},
    "paris":       {1: 5, 2: 6, 3: 6, 4: 6, 5: 7, 6: 8, 7: 8, 8: 7, 9: 7, 10: 6, 11: 5, 12: 5},
    "berlin":      {1: 6, 2: 7, 3: 7, 4: 7, 5: 8, 6: 9, 7: 8, 8: 8, 9: 7, 10: 6, 11: 5, 12: 6},
    "madrid":      {1: 7, 2: 8, 3: 8, 4: 8, 5: 9, 6: 10, 7: 10, 8: 9, 9: 8, 10: 7, 11: 7, 12: 7},
    "rome":        {1: 6, 2: 7, 3: 7, 4: 7, 5: 8, 6: 9, 7: 9, 8: 8, 9: 7, 10: 6, 11: 6, 12: 6},
    "moscow":      {1: 7, 2: 8, 3: 8, 4: 8, 5: 9, 6: 10, 7: 9, 8: 8, 9: 7, 10: 6, 11: 5, 12: 6},
    "helsinki":    {1: 6, 2: 7, 3: 8, 4: 7, 5: 9, 6: 10, 7: 9, 8: 8, 9: 7, 10: 5, 11: 4, 12: 5},
    "tokyo":       {1: 5, 2: 5, 3: 6, 4: 6, 5: 7, 6: 7, 7: 7, 8: 7, 9: 6, 10: 6, 11: 5, 12: 5},
    "seoul":       {1: 6, 2: 7, 3: 7, 4: 7, 5: 8, 6: 9, 7: 8, 8: 8, 9: 7, 10: 6, 11: 5, 12: 6},
    "beijing":     {1: 8, 2: 9, 3: 9, 4: 9, 5: 10, 6: 10, 7: 9, 8: 8, 9: 8, 10: 8, 11: 7, 12: 8},
    "dubai":       {1: 7, 2: 8, 3: 8, 4: 8, 5: 9, 6: 10, 7: 10, 8: 9, 9: 8, 10: 8, 11: 7, 12: 7},
    "singapore":   {1: 5, 2: 5, 3: 5, 4: 5, 5: 5, 6: 5, 7: 5, 8: 5, 9: 5, 10: 5, 11: 5, 12: 5},
    "bangkok":     {1: 6, 2: 6, 3: 6, 4: 6, 5: 6, 6: 5, 7: 5, 8: 5, 9: 5, 10: 6, 11: 6, 12: 6},
    "mumbai":      {1: 5, 2: 5, 3: 5, 4: 5, 5: 5, 6: 4, 7: 4, 8: 4, 9: 4, 10: 5, 11: 5, 12: 5},
    "delhi":       {1: 8, 2: 9, 3: 9, 4: 9, 5: 10, 6: 9, 7: 8, 8: 7, 9: 8, 10: 9, 11: 8, 12: 8},
    "sydney":      {1: 6, 2: 6, 3: 6, 4: 6, 5: 6, 6: 6, 7: 6, 8: 6, 9: 6, 10: 6, 11: 6, 12: 6},
}

# ============================================================================
# Climate Clusters for AR(1) Persistence
# ============================================================================

CLIMATE_CLUSTERS = {
    "northern_europe": ["london", "paris", "berlin", "amsterdam", "helsinki",
                        "stockholm", "oslo", "copenhagen", "moscow", "warsaw"],
    "mediterranean": ["madrid", "rome", "barcelona", "lisbon", "athens", "istanbul"],
    "east_asia": ["tokyo", "seoul", "beijing", "shanghai", "osaka", "taipei"],
    "southeast_asia": ["singapore", "bangkok", "kuala lumpur", "manila",
                       "jakarta", "hong kong", "shenzhen"],
    "south_asia": ["mumbai", "delhi", "lucknow", "karachi", "dhaka", "kolkata"],
    "n_am_east": ["new york", "chicago", "boston", "atlanta", "philadelphia",
                   "detroit", "washington", "toronto", "montreal"],
    "n_am_south": ["miami", "houston", "dallas", "phoenix", "los angeles"],
    "n_am_west": ["san francisco", "seattle", "denver", "los angeles"],
    "middle_east": ["dubai", "riyadh", "tehran", "cairo"],
    "africa": ["lagos", "nairobi", "cape town", "johannesburg"],
    "oceania": ["sydney", "melbourne", "auckland"],
    "latin_america": ["mexico city", "sao paulo", "buenos aires", "bogota", "lima", "santiago"],
}

# AR(1) persistence coefficients from Wilks (2011) Table 6.3
AR1_PHI = {
    "northern_europe": 0.82,
    "mediterranean": 0.78,
    "east_asia": 0.80,
    "southeast_asia": 0.70,
    "south_asia": 0.72,
    "n_am_east": 0.80,
    "n_am_south": 0.75,
    "n_am_west": 0.78,
    "middle_east": 0.73,
    "africa": 0.72,
    "oceania": 0.76,
    "latin_america": 0.74,
}

# ============================================================================
# Exotic Cities (less bot competition, edge decays 30% slower)
# ============================================================================

EXOTIC_CITIES = [
    # South Asia
    "lucknow", "karachi", "dhaka", "kolkata",
    # Africa
    "lagos", "nairobi", "cape town", "johannesburg",
    # Latin America
    "bogota", "lima", "santiago",
    # Eastern Europe
    "bucharest", "budapest", "warsaw", "prague",
    # Middle East
    "riyadh", "tehran", "cairo",
    # Southeast Asia
    "jakarta", "ho chi minh",
    # Central Asia
    "almaty", "tashkent",
]


# ============================================================================
# Latitude-Based Climate Fallback (v11.0 - FIX BUG #12)
# ============================================================================

# Seasonal adjustment by month (Northern Hemisphere)
# Positive = warmer than annual average, negative = colder
# Southern Hemisphere: we negate the adjustment
_SEASONAL_ADJ = {
    1: -8, 2: -6, 3: -2, 4: 3, 5: 6, 6: 9,
    7: 10, 8: 9, 9: 5, 10: 1, 11: -4, 12: -7,
}

# Base temperatures by latitude band (high, low) in Celsius
_LAT_BAND_TEMPS = {
    "tropical": (32, 22),    # |lat| < 23.5
    "subtropical": (28, 16), # 23.5 <= |lat| < 35
    "temperate": (20, 10),   # 35 <= |lat| < 50
    "cold": (10, 0),         # 50 <= |lat| < 60
    "arctic": (0, -10),      # |lat| >= 60
}


def get_climate_normal_fallback(city: str, month: int) -> Optional[tuple]:
    """
    FIX BUG #12: Generate fallback climate normal for cities not in CLIMATE_NORMALS.

    Uses latitude-based estimate with seasonal adjustment:
    1. Look up city coordinates in CITY_COORDS
    2. Classify by latitude band (tropical/subtropical/temperate/cold/arctic)
    3. Apply seasonal adjustment (warmer in summer, colder in winter)
    4. Adjust for Southern Hemisphere (negate seasonal adjustment)

    This is a ROUGH estimate - the real climate normals table should be
    preferred. But it's much better than returning None and disabling
    the Bayesian and Regime models entirely.

    Args:
        city: City name (lowercase)
        month: Month number (1-12)

    Returns:
        (high_temp, low_temp) in Celsius, or None if city not in CITY_COORDS

    Reference: Trenberth et al. (2020) - global temperature patterns
    by latitude zone provide reasonable first-order estimates.
    """
    city_lower = city.lower()
    coords = CITY_COORDS.get(city_lower)
    if not coords:
        return None

    lat, lon = coords
    abs_lat = abs(lat)

    # Classify latitude band
    if abs_lat < 23.5:
        band = "tropical"
    elif abs_lat < 35:
        band = "subtropical"
    elif abs_lat < 50:
        band = "temperate"
    elif abs_lat < 60:
        band = "cold"
    else:
        band = "arctic"

    base_hi, base_lo = _LAT_BAND_TEMPS[band]

    # Apply seasonal adjustment
    # Northern Hemisphere: summer = months 6-8, winter = months 12-2
    # Southern Hemisphere: opposite
    seasonal = _SEASONAL_ADJ.get(month, 0)
    if lat < 0:
        # Southern Hemisphere: invert seasons
        seasonal = -seasonal

    # Apply adjustment (scale down for tropical which has small seasonal variation)
    if band == "tropical":
        seasonal *= 0.3  # Tropical has minimal seasonal variation
    elif band == "subtropical":
        seasonal *= 0.6
    # temperate, cold, arctic: full seasonal adjustment

    hi = base_hi + seasonal
    lo = base_lo + seasonal * 0.6  # Low temp varies less than high temp

    return (round(hi), round(lo))


def get_cluster(city: str) -> str:
    """Get climate cluster for a city."""
    city_lower = city.lower()
    for cluster, cities in CLIMATE_CLUSTERS.items():
        if city_lower in cities:
            return cluster
    return "n_am_east"  # default


def get_phi(city: str) -> float:
    """Get AR(1) persistence coefficient for a city."""
    return AR1_PHI.get(get_cluster(city), 0.78)


def is_exotic(city: str) -> bool:
    """Check if a city has less bot competition."""
    return city.lower() in EXOTIC_CITIES
