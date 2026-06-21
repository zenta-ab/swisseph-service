"""
Swiss Ephemeris Microservice

FastAPI service exposing professional-grade astronomical calculations
via pyswisseph (Swiss Ephemeris). Used by the Astral Vedic engine.

Endpoints:
  POST /calculate - Full chart calculation (planets, houses, ayanamsa)
  GET  /health    - Health check
"""

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import swisseph as swe
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from timezonefinder import TimezoneFinder

# ─── Configuration ───────────────────────────────────────────────

import os

logger = logging.getLogger("swisseph")

EPHE_PATH = os.path.abspath(os.environ.get(
    "SWISSEPH_EPHE_PATH",
    os.path.join(os.path.dirname(__file__), "ephe"),
))
swe.set_ephe_path(EPHE_PATH)

app = FastAPI(title="Swiss Ephemeris Service", version="1.0.0")

# CORS only matters for browser clients. This service is normally called
# server-to-server from a trusted backend over a private network, where CORS
# does not apply - so the default is no cross-origin access. Set
# SWISSEPH_CORS_ORIGINS (comma-separated) only if a browser must call it directly.
_cors_origins = [o.strip() for o in os.environ.get("SWISSEPH_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ─── Planet mapping ──────────────────────────────────────────────

# Vedic planets (used by /calculate - verified against reference charts, DO NOT MODIFY)
PLANETS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mars": swe.MARS,
    "Mercury": swe.MERCURY,
    "Jupiter": swe.JUPITER,
    "Venus": swe.VENUS,
    "Saturn": swe.SATURN,
    "Uranus": swe.URANUS,
    "Neptune": swe.NEPTUNE,
    "Pluto": swe.PLUTO,
    "Rahu": swe.TRUE_NODE,      # True lunar node (ascending)
}

# Extended planets for astrocartography (includes outer bodies)
PLANETS_ACG = {
    **PLANETS,
    "Chiron": swe.CHIRON,
    "MeanNode": swe.MEAN_NODE,  # Mean lunar node (as used by astro.com)
}

# Extra modern/Western bodies, computed only when requested via `extra_bodies`.
# NOT part of the Vedic PLANETS set above (Jyotish does not use these).
EXTRA_BODIES = {
    "Chiron": swe.CHIRON,
    "Lilith": swe.MEAN_APOG,  # Mean Black Moon Lilith (mean lunar apogee)
}

# Ayanamsa systems
AYANAMSA_SYSTEMS = {
    "lahiri": swe.SIDM_LAHIRI,
    "raman": swe.SIDM_RAMAN,
    "krishnamurti": swe.SIDM_KRISHNAMURTI,
    "fagan_bradley": swe.SIDM_FAGAN_BRADLEY,
    "true_chitrapaksha": swe.SIDM_TRUE_CITRA,
}

# House systems
HOUSE_SYSTEMS = {
    "whole_sign": b"W",
    "placidus": b"P",
    "equal": b"E",
    "koch": b"K",
    "campanus": b"C",
}


# ─── Request / Response Models ───────────────────────────────────

class CalculateRequest(BaseModel):
    birth_date: str = Field(..., description="ISO date: YYYY-MM-DD")
    birth_time: Optional[str] = Field(None, description="HH:mm (24h). Omit for noon default.")
    birth_lat: Optional[float] = Field(None, description="Birth latitude (decimal degrees)")
    birth_lng: Optional[float] = Field(None, description="Birth longitude (decimal degrees)")
    reference_date: Optional[str] = Field(None, description="ISO date for transit calculations")
    utc_offset_hours: Optional[float] = Field(None, description="UTC offset of birth timezone (e.g. 1.0 for CET). If None, auto-detected from coordinates + date.")
    ayanamsa: str = Field("lahiri", description="Ayanamsa system")
    house_system: str = Field("whole_sign", description="House system")
    extra_bodies: Optional[list[str]] = Field(None, description="Extra bodies to also compute (e.g. Chiron, Lilith) for the Western chart")


class PlanetPosition(BaseModel):
    name: str
    tropical_longitude: float
    sidereal_longitude: float
    latitude: float
    distance_au: float
    speed_long: float           # degrees/day
    retrograde: bool
    rashi_index: int            # 0-11
    nakshatra_index: int        # 0-26
    nakshatra_pada: int         # 1-4
    degrees_in_nakshatra: float


class HouseCusp(BaseModel):
    house: int                  # 1-12
    sign_index: int             # 0-11
    cusp_longitude: float       # sidereal


class LagnaResult(BaseModel):
    tropical_longitude: float
    sidereal_longitude: float
    rashi_index: int
    degree_in_sign: float
    nakshatra_index: int
    nakshatra_pada: int


class CalculateResponse(BaseModel):
    ayanamsa_value: float
    ayanamsa_system: str
    planets: list[PlanetPosition]
    lagna: Optional[LagnaResult]
    houses: Optional[list[HouseCusp]]
    julian_day: float
    birth_datetime_utc: str
    reference_julian_day: Optional[float]
    reference_ayanamsa: Optional[float]
    reference_moon: Optional[PlanetPosition]
    reference_planets: Optional[list[PlanetPosition]]
    vertex_tropical: Optional[float] = None  # ascmc[3] from swe.houses - the Western Vertex point


# ─── Helpers ─────────────────────────────────────────────────────

def date_to_jd(date_str: str, time_str: Optional[str], utc_offset: float) -> float:
    """Convert date + time to Julian Day (UT)."""
    parts = date_str.split("-")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])

    if time_str:
        tp = time_str.split(":")
        hour = int(tp[0]) + (int(tp[1]) if len(tp) > 1 else 0) / 60.0
    else:
        hour = 12.0

    # Convert local time to UT
    hour -= utc_offset

    jd = swe.julday(year, month, day, hour)
    return jd


def sidereal_lon(tropical_lon: float, ayanamsa_val: float) -> float:
    """Convert tropical to sidereal longitude."""
    return (tropical_lon - ayanamsa_val) % 360


def nakshatra_info(sid_lon: float) -> tuple[int, int, float]:
    """Return (nakshatra_index, pada, degrees_in_nakshatra)."""
    span = 360 / 27  # 13.3333...
    normalized = sid_lon % 360
    idx = int(normalized / span)
    deg_in = normalized - idx * span
    pada = min(int(deg_in / (span / 4)) + 1, 4)
    return idx, pada, round(deg_in, 4)


def calc_planet(planet_id: int, name: str, jd: float, aya_val: float, flags: int) -> PlanetPosition:
    """Calculate a single planet position."""
    result, ret_flags = swe.calc_ut(jd, planet_id, flags)
    trop_lon = result[0]
    lat = result[1]
    dist = result[2]
    speed = result[3]

    sid = sidereal_lon(trop_lon, aya_val)
    rashi = int(sid / 30)
    nk_idx, nk_pada, nk_deg = nakshatra_info(sid)

    return PlanetPosition(
        name=name,
        tropical_longitude=round(trop_lon, 4),
        sidereal_longitude=round(sid, 4),
        latitude=round(lat, 4),
        distance_au=round(dist, 6),
        speed_long=round(speed, 4),
        retrograde=speed < 0,
        rashi_index=rashi,
        nakshatra_index=nk_idx,
        nakshatra_pada=nk_pada,
        degrees_in_nakshatra=nk_deg,
    )


# ─── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "engine": "Swiss Ephemeris", "version": swe.version}


@app.post("/calculate", response_model=CalculateResponse)
def calculate(req: CalculateRequest):
    # Thread-safety: re-set ephe path before each calculation
    swe.set_ephe_path(EPHE_PATH)

    # Validate ayanamsa
    aya_key = req.ayanamsa.lower()
    if aya_key not in AYANAMSA_SYSTEMS:
        raise HTTPException(400, f"Unknown ayanamsa: {req.ayanamsa}. Options: {list(AYANAMSA_SYSTEMS.keys())}")

    house_key = req.house_system.lower()
    if house_key not in HOUSE_SYSTEMS:
        raise HTTPException(400, f"Unknown house system: {req.house_system}. Options: {list(HOUSE_SYSTEMS.keys())}")

    swe.set_sid_mode(AYANAMSA_SYSTEMS[aya_key])

    # Resolve UTC offset (auto-detect from coordinates if not provided)
    utc_offset = req.utc_offset_hours
    if utc_offset is None and req.birth_lat is not None and req.birth_lng is not None:
        utc_offset = _auto_utc_offset(req.birth_lat, req.birth_lng, req.birth_date, req.birth_time or "12:00")
    elif utc_offset is None:
        utc_offset = 0.0

    # Julian Day
    jd = date_to_jd(req.birth_date, req.birth_time, utc_offset)

    # Ayanamsa value for birth date
    aya_val = swe.get_ayanamsa_ut(jd)

    # Calculation flags: Swiss Ephemeris + speed
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED

    # ── Planets ──
    planets: list[PlanetPosition] = []
    for name, pid in PLANETS.items():
        p = calc_planet(pid, name, jd, aya_val, flags)
        planets.append(p)

    # Ketu = Rahu + 180°
    rahu = next(p for p in planets if p.name == "Rahu")
    ketu_trop = (rahu.tropical_longitude + 180) % 360
    ketu_sid = sidereal_lon(ketu_trop, aya_val)
    ketu_rashi = int(ketu_sid / 30)
    ketu_nk_idx, ketu_nk_pada, ketu_nk_deg = nakshatra_info(ketu_sid)

    planets.append(PlanetPosition(
        name="Ketu",
        tropical_longitude=round(ketu_trop, 4),
        sidereal_longitude=round(ketu_sid, 4),
        latitude=-rahu.latitude,
        distance_au=rahu.distance_au,
        speed_long=rahu.speed_long,
        retrograde=True,
        rashi_index=ketu_rashi,
        nakshatra_index=ketu_nk_idx,
        nakshatra_pada=ketu_nk_pada,
        degrees_in_nakshatra=ketu_nk_deg,
    ))

    # Mark Rahu as always retrograde
    rahu.retrograde = True

    # ── Extra Western/modern bodies (Chiron, Lilith) on request ──
    for name in (req.extra_bodies or []):
        pid = EXTRA_BODIES.get(name)
        if pid is not None:
            planets.append(calc_planet(pid, name, jd, aya_val, flags))

    # ── Lagna + Houses ──
    lagna: Optional[LagnaResult] = None
    houses: Optional[list[HouseCusp]] = None
    vertex_trop: Optional[float] = None

    if req.birth_lat is not None and req.birth_lng is not None and req.birth_time:
        house_sys_byte = HOUSE_SYSTEMS[house_key]

        cusps, ascmc = swe.houses(jd, req.birth_lat, req.birth_lng, house_sys_byte)

        # ascmc[0] = Ascendant (tropical); ascmc[3] = Vertex (tropical)
        asc_trop = ascmc[0]
        vertex_trop = ascmc[3]
        asc_sid = sidereal_lon(asc_trop, aya_val)
        asc_rashi = int(asc_sid / 30)
        asc_deg_in_sign = asc_sid % 30
        asc_nk_idx, asc_nk_pada, _ = nakshatra_info(asc_sid)

        lagna = LagnaResult(
            tropical_longitude=round(asc_trop, 4),
            sidereal_longitude=round(asc_sid, 4),
            rashi_index=asc_rashi,
            degree_in_sign=round(asc_deg_in_sign, 2),
            nakshatra_index=asc_nk_idx,
            nakshatra_pada=asc_nk_pada,
        )

        # House cusps (sidereal)
        houses = []
        for i, cusp_trop in enumerate(cusps[:12]):
            cusp_sid = sidereal_lon(cusp_trop, aya_val)
            sign_idx = int(cusp_sid / 30)
            houses.append(HouseCusp(
                house=i + 1,
                sign_index=sign_idx,
                cusp_longitude=round(cusp_sid, 4),
            ))

    # ── Reference date (transit planets) ──
    ref_jd: Optional[float] = None
    ref_aya: Optional[float] = None
    ref_moon: Optional[PlanetPosition] = None
    ref_planets: Optional[list[PlanetPosition]] = None

    if req.reference_date:
        ref_jd = date_to_jd(req.reference_date, "12:00", 0)
        ref_aya = swe.get_ayanamsa_ut(ref_jd)

        # Calculate ALL planets at reference date (for Ashtakvarga, Muhurtha, transits)
        ref_planets = []
        for name, pid in PLANETS.items():
            p = calc_planet(pid, name, ref_jd, ref_aya, flags)
            ref_planets.append(p)

        # Ketu at reference date
        ref_rahu = next(p for p in ref_planets if p.name == "Rahu")
        ref_ketu_trop = (ref_rahu.tropical_longitude + 180) % 360
        ref_ketu_sid = sidereal_lon(ref_ketu_trop, ref_aya)
        ref_ketu_rashi = int(ref_ketu_sid / 30)
        ref_ketu_nk_idx, ref_ketu_nk_pada, ref_ketu_nk_deg = nakshatra_info(ref_ketu_sid)
        ref_planets.append(PlanetPosition(
            name="Ketu",
            tropical_longitude=round(ref_ketu_trop, 4),
            sidereal_longitude=round(ref_ketu_sid, 4),
            latitude=-ref_rahu.latitude,
            distance_au=ref_rahu.distance_au,
            speed_long=ref_rahu.speed_long,
            retrograde=True,
            rashi_index=ref_ketu_rashi,
            nakshatra_index=ref_ketu_nk_idx,
            nakshatra_pada=ref_ketu_nk_pada,
            degrees_in_nakshatra=ref_ketu_nk_deg,
        ))
        ref_rahu.retrograde = True

        # Backward-compat: keep reference_moon as shortcut
        ref_moon = next(p for p in ref_planets if p.name == "Moon")

    # Build UTC datetime string
    year, month, day = req.birth_date.split("-")
    hour_f = 12.0
    if req.birth_time:
        tp = req.birth_time.split(":")
        hour_f = int(tp[0]) + (int(tp[1]) if len(tp) > 1 else 0) / 60.0
    hour_f -= utc_offset
    h = int(hour_f)
    m = round((hour_f - h) * 60)
    birth_utc = f"{year}-{month}-{day}T{h:02d}:{m:02d}:00Z"

    return CalculateResponse(
        ayanamsa_value=round(aya_val, 6),
        ayanamsa_system=aya_key,
        planets=planets,
        lagna=lagna,
        houses=houses,
        julian_day=round(jd, 6),
        birth_datetime_utc=birth_utc,
        reference_julian_day=round(ref_jd, 6) if ref_jd else None,
        reference_ayanamsa=round(ref_aya, 6) if ref_aya else None,
        reference_moon=ref_moon,
        vertex_tropical=round(vertex_trop, 4) if vertex_trop is not None else None,
        reference_planets=ref_planets,
    )


# ─── Design Date (88° Solar Arc) Endpoint ────────────────────────

class DesignDateRequest(BaseModel):
    birth_date: str = Field(..., description="ISO date: YYYY-MM-DD")
    birth_time: Optional[str] = Field(None, description="HH:mm (24h)")
    utc_offset_hours: float = Field(0.0, description="UTC offset of birth timezone")
    ayanamsa: str = Field("lahiri", description="Ayanamsa system")


class DesignDateResponse(BaseModel):
    design_date: str
    design_time: str
    design_jd: float
    birth_sun_tropical: float
    design_sun_tropical: float
    target_sun_tropical: float
    planets: list[PlanetPosition]


@app.post("/design-date", response_model=DesignDateResponse)
def design_date(req: DesignDateRequest):
    """Find the exact moment when the Sun was 88° before the birth Sun position.

    Used for Human Design 'Design' (unconscious) calculation.
    Returns all planet positions at that exact moment.
    """
    swe.set_ephe_path(EPHE_PATH)
    aya_key = req.ayanamsa.lower()
    if aya_key not in AYANAMSA_SYSTEMS:
        raise HTTPException(400, f"Unknown ayanamsa: {req.ayanamsa}")

    swe.set_sid_mode(AYANAMSA_SYSTEMS[aya_key])

    # Get birth Sun tropical longitude
    birth_jd = date_to_jd(req.birth_date, req.birth_time, req.utc_offset_hours)
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    birth_sun = swe.calc_ut(birth_jd, swe.SUN, flags)
    birth_sun_trop = birth_sun[0][0]

    # Target: Sun at birth_sun - 88° (tropical)
    target_lon = (birth_sun_trop - 88) % 360

    # Start with ~88 days before birth
    design_jd = birth_jd - 88.0

    # Newton's method: Sun moves ~1°/day
    for _ in range(10):
        sun_pos = swe.calc_ut(design_jd, swe.SUN, flags)
        actual_lon = sun_pos[0][0]

        # Signed angle difference (target - actual), normalized to [-180, 180]
        error = target_lon - actual_lon
        if error > 180:
            error -= 360
        if error < -180:
            error += 360

        if abs(error) < 0.0001:  # ~0.4 arc-seconds precision
            break

        # Adjust: Sun moves ~1°/day, so shift by error days
        design_jd += error

    # Calculate all planets at the design date
    aya_val = swe.get_ayanamsa_ut(design_jd)

    planets: list[PlanetPosition] = []
    for name, pid in PLANETS.items():
        p = calc_planet(pid, name, design_jd, aya_val, flags)
        planets.append(p)

    # Ketu = Rahu + 180°
    rahu = next(p for p in planets if p.name == "Rahu")
    ketu_trop = (rahu.tropical_longitude + 180) % 360
    ketu_sid = sidereal_lon(ketu_trop, aya_val)
    ketu_rashi = int(ketu_sid / 30)
    ketu_nk_idx, ketu_nk_pada, ketu_nk_deg = nakshatra_info(ketu_sid)
    planets.append(PlanetPosition(
        name="Ketu",
        tropical_longitude=round(ketu_trop, 4),
        sidereal_longitude=round(ketu_sid, 4),
        latitude=-rahu.latitude,
        distance_au=rahu.distance_au,
        speed_long=rahu.speed_long,
        retrograde=True,
        rashi_index=ketu_rashi,
        nakshatra_index=ketu_nk_idx,
        nakshatra_pada=ketu_nk_pada,
        degrees_in_nakshatra=ketu_nk_deg,
    ))
    rahu.retrograde = True

    # Convert JD back to calendar date/time
    jd_int = int(design_jd)
    jd_frac = design_jd - jd_int
    year, month, day, hour_frac = swe.revjul(design_jd)
    h = int(hour_frac)
    m = int((hour_frac - h) * 60)

    design_date_str = f"{year:04d}-{month:02d}-{day:02d}"
    design_time_str = f"{h:02d}:{m:02d}"

    final_sun = swe.calc_ut(design_jd, swe.SUN, flags)

    return DesignDateResponse(
        design_date=design_date_str,
        design_time=design_time_str,
        design_jd=round(design_jd, 6),
        birth_sun_tropical=round(birth_sun_trop, 4),
        design_sun_tropical=round(final_sun[0][0], 4),
        target_sun_tropical=round(target_lon, 4),
        planets=planets,
    )


# ─── Astrocartography Endpoint ────────────────────────────────────

ACG_PLANETS_DEFAULT = ["Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Chiron", "MeanNode"]

class AstroLinesRequest(BaseModel):
    birth_date: str = Field(..., description="ISO date: YYYY-MM-DD")
    birth_time: str = Field(..., description="HH:mm (24h). Required for ACG.")
    birth_lat: float = Field(..., description="Birth latitude (decimal degrees)")
    birth_lng: float = Field(..., description="Birth longitude (decimal degrees)")
    utc_offset_hours: Optional[float] = Field(None, description="UTC offset of birth timezone. If None, auto-detected from coordinates + date.")
    planets: Optional[list[str]] = Field(None, description="Planet filter. Default: all.")


class AstroLinePoint(BaseModel):
    lat: float
    lng: float


class AstroLine(BaseModel):
    planet: str
    angle: str               # MC, IC, ASC, DSC
    line_type: str           # "meridian" or "curve"
    longitude: Optional[float] = None  # For meridian lines (MC/IC)
    coordinates: list[list[float]]  # [[lat, lng], ...]
    zenith: Optional[list[float]] = None  # [lng, lat] - point where planet is directly overhead/underfoot


class AstroLinesMetadata(BaseModel):
    birth_datetime_utc: str
    julian_day: float
    obliquity: float
    gast_degrees: float


class AstroLinesResponse(BaseModel):
    lines: list[AstroLine]
    metadata: AstroLinesMetadata


def _mc_longitude_for_planet(trop_lon: float, obliquity: float, gast_deg: float) -> float:
    """Calculate MC line longitude: where planet's ecliptic longitude = MC cusp.

    MC cusp (in ecliptic) for a given ARMC:
      tan(MC) = tan(ARMC) / cos(obliquity)

    Solve for ARMC where MC = planet's tropical longitude:
      tan(planet_lon) = tan(ARMC) / cos(obliquity)
      tan(ARMC) = tan(planet_lon) * cos(obliquity)
      ARMC = atan(tan(planet_lon) * cos(obliquity))

    Then geo_longitude = ARMC - GAST
    """
    lon_rad = math.radians(trop_lon)
    obl_rad = math.radians(obliquity)

    # ARMC where MC cusp = planet's ecliptic longitude
    armc_rad = math.atan2(
        math.sin(lon_rad),
        math.cos(lon_rad) * math.cos(obl_rad)
    )
    armc_deg = math.degrees(armc_rad) % 360

    # Ensure correct quadrant (MC and ARMC share the same quadrant)
    # Planet longitude quadrant check
    if 90 < trop_lon <= 270:
        if armc_deg < 180:
            armc_deg += 180
    else:
        if armc_deg >= 180:
            armc_deg -= 180

    geo_lng = (armc_deg - gast_deg) % 360
    if geo_lng > 180:
        geo_lng -= 360
    return geo_lng


def _find_asc_armc(planet_lon: float, geo_lat: float, obliquity: float) -> Optional[float]:
    """Find ARMC where ASC cusp = planet's tropical longitude using Newton's method.

    Uses swe.houses_armc() with Equal house system (works at all latitudes).
    DEPRECATED: Kept for reference. Use _mundane_asc_dsc_lng() instead.
    """
    obl = obliquity
    target = planet_lon % 360

    # Initial guess: ARMC roughly opposite to planet longitude
    armc = (target + 90) % 360

    for _ in range(50):
        try:
            cusps, ascmc = swe.houses_armc(armc, geo_lat, obl, b"E")
        except Exception:
            return None  # Cannot compute at this latitude

        asc = ascmc[0] % 360

        # Error: difference between current ASC and target
        error = target - asc
        # Normalize to [-180, 180]
        if error > 180:
            error -= 360
        if error < -180:
            error += 360

        if abs(error) < 0.001:  # ~3.6 arc-seconds precision
            return armc

        # Numerical derivative
        armc2 = (armc + 0.1) % 360
        try:
            _, ascmc2 = swe.houses_armc(armc2, geo_lat, obl, b"E")
        except Exception:
            return None

        asc2 = ascmc2[0] % 360

        dasc = asc2 - asc
        if dasc > 180:
            dasc -= 360
        if dasc < -180:
            dasc += 360

        if abs(dasc) < 1e-10:
            return None  # No solution (degenerate case)

        # Newton step
        deriv = dasc / 0.1
        armc = (armc + error / deriv) % 360

    return None  # Did not converge


def _find_dsc_armc(planet_lon: float, geo_lat: float, obliquity: float) -> Optional[float]:
    """Find ARMC where DSC cusp = planet's tropical longitude.

    DSC = ASC + 180°, so we find where ASC = planet_lon + 180°.
    DEPRECATED: Kept for reference. Use _mundane_asc_dsc_lng() instead.
    """
    opposite = (planet_lon + 180) % 360
    return _find_asc_armc(opposite, geo_lat, obliquity)


def _mundane_asc_dsc_lng(mc_geo_lng: float, dec: float, geo_lat: float) -> tuple[Optional[float], Optional[float]]:
    """Calculate ASC/DSC geographic longitudes using the mundane (hour angle) method.

    This is the standard astrocartography method used by astro.com:
    - Computes the hour angle H at which a planet rises/sets at a given latitude
    - H = arccos(-tan(lat) * tan(dec))
    - ASC geo_lng = MC_geo_lng - H (planet rises east of MC)
    - DSC geo_lng = MC_geo_lng + H (planet sets west of MC)

    Returns (asc_lng, dsc_lng) or None for circumpolar/never-rises cases.
    """
    lat_rad = math.radians(geo_lat)
    dec_rad = math.radians(dec)

    cos_h = -math.tan(lat_rad) * math.tan(dec_rad)

    if cos_h > 1.0 or cos_h < -1.0:
        return None, None  # Planet is circumpolar or never rises at this latitude

    H = math.degrees(math.acos(cos_h))

    asc_lng = mc_geo_lng - H
    dsc_lng = mc_geo_lng + H

    # Wrap to [-180, 180]
    if asc_lng < -180:
        asc_lng += 360
    if asc_lng > 180:
        asc_lng -= 360
    if dsc_lng < -180:
        dsc_lng += 360
    if dsc_lng > 180:
        dsc_lng -= 360

    return asc_lng, dsc_lng


# ─── Timezone auto-detection ──────────────────────────────────────

_tf = TimezoneFinder()


def _auto_utc_offset(lat: float, lng: float, date_str: str, time_str: str) -> float:
    """Resolve the EXACT UTC offset from coordinates + date/time (IANA timezone).

    Astrocartography is offset-sensitive, so we never approximate: if the
    timezone can't be resolved precisely we raise HTTPException(422), so the
    caller gets a clean 4xx (not an unhandled 500) for unresolvable input such as
    open-ocean coordinates. (Prefer passing utc_offset_hours from the app, which
    avoids this path entirely.)
    """
    tz_name = _tf.timezone_at(lat=lat, lng=lng)
    if not tz_name:
        raise HTTPException(422, f"Could not resolve IANA timezone for coordinates ({lat}, {lng})")

    parts = date_str.split("-")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    tp = time_str.split(":")
    hour, minute = int(tp[0]), int(tp[1]) if len(tp) > 1 else 0

    # ZoneInfo requires the IANA tz database (system zoneinfo or the `tzdata`
    # package). If it's missing we WANT this to raise - no silent fallback.
    tz = ZoneInfo(tz_name)
    dt = datetime(year, month, day, hour, minute, tzinfo=tz)
    offset = dt.utcoffset()
    if offset is None:
        raise HTTPException(422, f"Could not compute UTC offset for timezone {tz_name}")
    return offset.total_seconds() / 3600.0


@app.post("/astrolines", response_model=AstroLinesResponse)
def astrolines(req: AstroLinesRequest):
    """Calculate Astrocartography lines for a birth chart.

    Uses the mundane (in-mundo / hour angle) method matching astro.com:
    - MC line: where planet culminates (RA-based, vertical)
    - IC line: MC + 180° (vertical)
    - ASC line: where planet rises (hour angle method, curved)
    - DSC line: where planet sets (hour angle method, curved)
    """
    swe.set_ephe_path(EPHE_PATH)

    # Resolve UTC offset (auto-detect from coordinates if not provided)
    utc_offset = req.utc_offset_hours
    if utc_offset is None:
        utc_offset = _auto_utc_offset(req.birth_lat, req.birth_lng, req.birth_date, req.birth_time)

    # Julian Day
    jd = date_to_jd(req.birth_date, req.birth_time, utc_offset)

    # Calculation flags
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED

    # Ensure ephemeris path is set (needed for thread safety)
    swe.set_ephe_path(EPHE_PATH)

    # True obliquity of the ecliptic
    ecl_nut = swe.calc_ut(jd, swe.ECL_NUT, 0)
    obliquity = ecl_nut[0][0]  # True obliquity

    # Greenwich Apparent Sidereal Time (degrees)
    gast_hours = swe.sidtime(jd)
    gast_deg = gast_hours * 15.0

    # Filter planets
    requested = req.planets if req.planets else ACG_PLANETS_DEFAULT

    # Calculate planet positions (tropical)
    planet_data: dict[str, tuple[float, float]] = {}  # name -> (trop_lon, ecl_lat)
    for name, pid in PLANETS_ACG.items():
        if name in requested or "Ketu" in requested:
            # Degrade gracefully: a single body whose ephemeris file is missing
            # (e.g. Chiron needs seas_18.se1, which has no Moshier fallback) must
            # not 500 the whole request - skip it so the other lines still render.
            try:
                result, _ = swe.calc_ut(jd, pid, flags)
                planet_data[name] = (result[0], result[1])
            except Exception as e:
                logger.warning("astrolines: skipping %s - calc failed: %s", name, e)

    # Ketu
    if "Ketu" in requested and "Rahu" in planet_data:
        rahu_lon, rahu_lat = planet_data["Rahu"]
        planet_data["Ketu"] = ((rahu_lon + 180) % 360, -rahu_lat)

    # Generate lines
    lines: list[AstroLine] = []

    # Latitude range for ASC/DSC curves (±85° matching astro.com)
    lat_step = 0.5
    lat_range = [i * lat_step for i in range(int(-85 / lat_step), int(85 / lat_step) + 1)]

    for planet_name in requested:
        if planet_name not in planet_data:
            continue

        trop_lon, ecl_lat = planet_data[planet_name]

        # ── Convert ecliptic to equatorial (RA, Dec) ──
        obl_rad = math.radians(obliquity)
        ra_dec = swe.cotrans((trop_lon, ecl_lat, 1.0), -obliquity)
        ra = ra_dec[0]   # Right Ascension (degrees)
        dec = ra_dec[1]  # Declination (degrees)

        # ── MC line (where planet culminates) ──
        mc_lng = (ra - gast_deg) % 360
        if mc_lng > 180:
            mc_lng -= 360

        # Zenith point: MC longitude + planet's declination as latitude
        # This is where the planet passes directly overhead
        zenith_lat = round(dec, 4)

        lines.append(AstroLine(
            planet=planet_name,
            angle="MC",
            line_type="meridian",
            longitude=round(mc_lng, 4),
            coordinates=[[85, round(mc_lng, 4)], [-85, round(mc_lng, 4)]],
            zenith=[round(mc_lng, 4), zenith_lat],
        ))

        # ── IC line (opposite MC) ──
        ic_lng = mc_lng + 180 if mc_lng < 0 else mc_lng - 180

        lines.append(AstroLine(
            planet=planet_name,
            angle="IC",
            line_type="meridian",
            longitude=round(ic_lng, 4),
            coordinates=[[85, round(ic_lng, 4)], [-85, round(ic_lng, 4)]],
            zenith=[round(ic_lng, 4), round(-dec, 4)],
        ))

        # ── ASC/DSC lines (mundane hour angle method) ──
        asc_points: list[list[float]] = []
        dsc_points: list[list[float]] = []

        for geo_lat in lat_range:
            asc_lng, dsc_lng = _mundane_asc_dsc_lng(mc_lng, dec, geo_lat)
            if asc_lng is not None:
                asc_points.append([geo_lat, round(asc_lng, 4)])
            if dsc_lng is not None:
                dsc_points.append([geo_lat, round(dsc_lng, 4)])

        if asc_points:
            lines.append(AstroLine(
                planet=planet_name,
                angle="ASC",
                line_type="curve",
                coordinates=asc_points,
            ))

        if dsc_points:
            lines.append(AstroLine(
                planet=planet_name,
                angle="DSC",
                line_type="curve",
                coordinates=dsc_points,
            ))

    # Metadata
    year, month, day = req.birth_date.split("-")
    tp = req.birth_time.split(":")
    hour_f = int(tp[0]) + (int(tp[1]) if len(tp) > 1 else 0) / 60.0
    hour_f -= utc_offset
    h = int(hour_f)
    m = round((hour_f - h) * 60)
    birth_utc = f"{year}-{month}-{day}T{h:02d}:{m:02d}:00Z"

    return AstroLinesResponse(
        lines=lines,
        metadata=AstroLinesMetadata(
            birth_datetime_utc=birth_utc,
            julian_day=round(jd, 6),
            obliquity=round(obliquity, 6),
            gast_degrees=round(gast_deg, 4),
        ),
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("SWISSEPH_PORT", "9047"))
    uvicorn.run(app, host="127.0.0.1", port=port)
