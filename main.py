from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import json
import os
import sqlite3
import threading
import time
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FLOODGUARD_DB_PATH", str(ROOT / "floodguard.db")))
OSRM_BASE_URL = os.getenv("FLOODGUARD_OSRM_URL", "https://router.project-osrm.org").rstrip("/")
NOMINATIM_BASE_URL = os.getenv("FLOODGUARD_NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")
PHOTON_BASE_URL = os.getenv("FLOODGUARD_PHOTON_URL", "https://photon.komoot.io").rstrip("/")
GEOCODE_USER_AGENT = os.getenv("FLOODGUARD_GEOCODE_USER_AGENT", "FloodGuardAI/1.2 (hackathon prototype; configure contact URL before deployment)")
_geocode_cache: dict[str, tuple[float, list[dict]]] = {}
_geocode_lock = threading.Lock()
_last_geocode_request = 0.0
# Approximate Bengaluru Urban search area (min lon, min lat, max lon, max lat).
# Photon results are additionally restricted to India/Karnataka and rural-district matches are excluded.
BENGALURU_BBOX = (77.25, 12.65, 77.95, 13.25)
_place_suggest_cache: dict[str, tuple[float, list[dict]]] = {}
_place_suggest_lock = threading.Lock()
_last_photon_request = 0.0
ROUTE_REPORT_RADIUS_M = 250
MAX_REPORT_AGE_HOURS = 72
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_CACHE_SECONDS = 600
_weather_cache: dict[tuple[float, float], tuple[float, dict]] = {}
MODEL_PATH = Path(os.getenv("FLOODGUARD_MODEL_PATH", str(ROOT / "models" / "bengaluru_flood_model.joblib")))
MODEL_META_PATH = Path(os.getenv("FLOODGUARD_MODEL_META_PATH", str(MODEL_PATH.with_name("bengaluru_flood_model_meta.json"))))

app = FastAPI(title="FloodGuard AI API", version="1.1.0", description=(
    "Urban flood reporting and prototype flood-aware route comparison. Route exposure scores are heuristic, not forecasts."
))
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500", "http://localhost:5500",
                   "http://127.0.0.1:5173", "http://localhost:5173",
                   "https://localhost", "capacitor://localhost", "null"],
    allow_credentials=False, allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class ReportIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    location: str = Field(min_length=2, max_length=160)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    severity: Literal["low", "moderate", "high"]
    description: str = Field(min_length=3, max_length=1000)

    @field_validator("location", "description")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("This field cannot be blank.")
        return value.strip()

    @model_validator(mode="after")
    def coordinates_are_a_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Provide both latitude and longitude, or neither.")
        return self


class EstimateIn(BaseModel):
    rainfall_mm_24h: float = Field(ge=0, le=1000)
    observed_severity: Literal["none", "low", "moderate", "high"] = "none"
    water_depth_cm: float = Field(default=0, ge=0, le=500)
    drainage_blocked: bool = False


class StatusIn(BaseModel):
    status: Literal["Reported", "Under review", "Resolved"]


class RouteCompareIn(BaseModel):
    origin_lat: float = Field(ge=-90, le=90)
    origin_lon: float = Field(ge=-180, le=180)
    destination_lat: float = Field(ge=-90, le=90)
    destination_lon: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def locations_differ(self):
        if abs(self.origin_lat - self.destination_lat) < 1e-6 and abs(self.origin_lon - self.destination_lon) < 1e-6:
            raise ValueError("Origin and destination must be different locations.")
        return self


class GeocodeIn(BaseModel):
    query: str = Field(min_length=3, max_length=180)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a place or address to search.")
        return value


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL, latitude REAL, longitude REAL,
            severity TEXT NOT NULL CHECK(severity IN ('low','moderate','high')),
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Reported' CHECK(status IN ('Reported','Under review','Resolved')),
            risk INTEGER NOT NULL CHECK(risk BETWEEN 0 AND 100), created_at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)")


def report_score(severity: str) -> int:
    return {"low": 22, "moderate": 48, "high": 78}[severity]


def estimate_risk(data: EstimateIn) -> dict:
    """Transparent demo heuristic. Values are illustrative, not calibrated to outcomes."""
    score = min(data.rainfall_mm_24h / 100 * 40, 40)
    factors = [f"24-hour rainfall contributes {score:.0f}/40 points (input supplied by user)."]
    severity_points = {"none": 0, "low": 8, "moderate": 18, "high": 28}[data.observed_severity]
    score += severity_points
    if severity_points:
        factors.append(f"Observed severity contributes {severity_points}/28 points.")
    depth_points = min(data.water_depth_cm / 20 * 20, 20)
    score += depth_points
    if depth_points:
        factors.append(f"Reported water depth contributes {depth_points:.0f}/20 points.")
    if data.drainage_blocked:
        score += 12
        factors.append("Blocked drainage contributes 12 points.")
    value = min(round(score), 100)
    level = "Low" if value < 30 else "Moderate" if value < 60 else "High"
    return {"score": value, "level": level, "method": "transparent_demo_heuristic",
            "validated_forecast": False, "factors": factors,
            "disclaimer": "Illustrative decision-support only. Inputs are unverified; this is not a forecast or emergency guidance."}


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Coordinates are (latitude, longitude)."""
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371000 * 2 * asin(min(1, sqrt(h)))


def distance_to_route_m(report_point: tuple[float, float], coordinates: list[list[float]]) -> float:
    """Approximate distance to route by sampling each route segment at ~100 m intervals."""
    if not coordinates:
        return float("inf")
    points = [(float(lat), float(lon)) for lon, lat in coordinates]
    best = min(haversine_m(report_point, point) for point in points)
    for start, end in zip(points, points[1:]):
        segment_len = haversine_m(start, end)
        steps = min(100, max(1, int(segment_len / 100)))
        for step in range(1, steps):
            fraction = step / steps
            point = (start[0] + (end[0] - start[0]) * fraction,
                     start[1] + (end[1] - start[1]) * fraction)
            best = min(best, haversine_m(report_point, point))
            if best < 15:
                return best
    return best


def get_active_geo_reports() -> list[dict]:
    with db() as con:
        rows = con.execute("""SELECT id,location,latitude,longitude,severity,description,status,risk,created_at
            FROM reports WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND status != 'Resolved'
            ORDER BY id DESC LIMIT 2000""").fetchall()
    now = datetime.now(timezone.utc)
    active = []
    for row in rows:
        item = dict(row)
        created = parse_time(item["created_at"])
        if not created:
            continue
        age_hours = max(0.0, (now - created).total_seconds() / 3600)
        if age_hours <= MAX_REPORT_AGE_HOURS:
            item["age_hours"] = age_hours
            active.append(item)
    return active


def score_route(route: dict, reports: list[dict]) -> dict:
    geometry = route.get("geometry", {}).get("coordinates", [])
    hazards = []
    contributions = []
    base_points = {"low": 15, "moderate": 35, "high": 60}
    for report in reports:
        point = (float(report["latitude"]), float(report["longitude"]))
        distance = distance_to_route_m(point, geometry)
        if distance > ROUTE_REPORT_RADIUS_M:
            continue
        proximity = max(0.0, 1 - distance / ROUTE_REPORT_RADIUS_M)
        age_factor = max(0.0, 1 - report["age_hours"] / MAX_REPORT_AGE_HOURS)
        verification_factor = 0.7 if report["status"] == "Under review" else 1.0
        contribution = base_points[report["severity"]] * proximity * age_factor * verification_factor
        if contribution < 0.5:
            continue
        contributions.append(contribution)
        hazards.append({"report_id": report["id"], "location": report["location"],
                        "severity": report["severity"], "status": report["status"],
                        "age_hours": round(report["age_hours"], 1),
                        "distance_m": round(distance), "description": report["description"]})
    exposure = min(100, round(sum(contributions)))
    return {"distance_m": round(route.get("distance", 0)),
            "duration_seconds": round(route.get("duration", 0)),
            "geometry": geometry, "exposure_score": exposure,
            "exposure_label": "Higher known exposure" if exposure >= 40 else "Some known exposure" if exposure >= 12 else "Lower known exposure",
            "nearby_hazards": sorted(hazards, key=lambda h: (h["distance_m"], -{"low": 1, "moderate": 2, "high": 3}[h["severity"]])),
            "hazard_count": len(hazards)}


def fetch_osrm_routes(origin_lon: float, origin_lat: float, destination_lon: float, destination_lat: float) -> list[dict]:
    coords = f"{origin_lon},{origin_lat};{destination_lon},{destination_lat}"
    url = f"{OSRM_BASE_URL}/route/v1/driving/{quote(coords, safe=',;.-')}?alternatives=3&overview=full&geometries=geojson&steps=false"
    request = Request(url, headers={"User-Agent": "FloodGuardAI/1.1 (prototype route comparison)"})
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Routing service is unavailable. Please retry later.") from exc
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise HTTPException(status_code=422, detail="No driving route was found for those locations. Try points closer to a mapped road.")
    return payload["routes"]


def geocode_place(query: str) -> list[dict]:
    """User-triggered, cached lookup through Nominatim; not for autocomplete/bulk search."""
    global _last_geocode_request
    normalized = " ".join(query.casefold().split())
    cached = _geocode_cache.get(normalized)
    if cached and time.time() - cached[0] < 86400:
        return cached[1]
    # Nominatim's public instance allows at most one request per second per application.
    with _geocode_lock:
        cached = _geocode_cache.get(normalized)
        if cached and time.time() - cached[0] < 86400:
            return cached[1]
        wait = 1.05 - (time.monotonic() - _last_geocode_request)
        if wait > 0:
            time.sleep(wait)
        params = urlencode({"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1})
        request = Request(f"{NOMINATIM_BASE_URL}/search?{params}", headers={
            "User-Agent": GEOCODE_USER_AGENT, "Accept": "application/json"})
        _last_geocode_request = time.monotonic()
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=502, detail="Place search is temporarily unavailable. Enter coordinates or retry later.") from exc
        matches = [{"display_name": item.get("display_name", ""),
                    "latitude": float(item["lat"]), "longitude": float(item["lon"])}
                   for item in payload if item.get("lat") is not None and item.get("lon") is not None]
        _geocode_cache[normalized] = (time.time(), matches)
        return matches


def _format_photon_feature(feature: dict) -> dict | None:
    properties = feature.get("properties") or {}
    coordinates = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coordinates) < 2:
        return None
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    min_lon, min_lat, max_lon, max_lat = BENGALURU_BBOX
    if not (min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat):
        return None
    country_code = str(properties.get("countrycode") or "").upper()
    state = str(properties.get("state") or "")
    district = str(properties.get("district") or "")
    county = str(properties.get("county") or "")
    if country_code and country_code != "IN":
        return None
    if state and "karnataka" not in state.casefold():
        return None
    if any("rural" in value.casefold() for value in (district, county)):
        return None

    name = str(properties.get("name") or "").strip()
    street = " ".join(str(properties.get(key) or "").strip() for key in ("housenumber", "street")).strip()
    place = str(properties.get("locality") or properties.get("suburb") or district or
                 properties.get("city") or county or "").strip()
    parts: list[str] = []
    for value in (street, name, place, state or "Karnataka"):
        if value and value.casefold() not in {part.casefold() for part in parts}:
            parts.append(value)
    display_name = ", ".join(parts)
    if not name and not street:
        return None
    return {"display_name": display_name, "latitude": latitude, "longitude": longitude}


def suggest_bengaluru_places(query: str) -> list[dict]:
    """Search-as-you-type places using Photon, biased and bounded to Bengaluru Urban."""
    global _last_photon_request
    normalized = " ".join(query.casefold().split())
    cached = _place_suggest_cache.get(normalized)
    if cached and time.time() - cached[0] < 21600:
        return cached[1]
    with _place_suggest_lock:
        cached = _place_suggest_cache.get(normalized)
        if cached and time.time() - cached[0] < 21600:
            return cached[1]
        # Debounced client requests plus a small global throttle keep use of the public demo reasonable.
        wait = 0.35 - (time.monotonic() - _last_photon_request)
        if wait > 0:
            time.sleep(wait)
        min_lon, min_lat, max_lon, max_lat = BENGALURU_BBOX
        params = urlencode({
            "q": query, "countrycode": "IN", "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
            "lat": 12.9716, "lon": 77.5946, "limit": 8, "lang": "en",
        })
        request = Request(f"{PHOTON_BASE_URL}/api/?{params}", headers={
            "User-Agent": "FloodGuardAI/1.3 (Bengaluru community flood reporting prototype)",
            "Accept": "application/json",
        })
        _last_photon_request = time.monotonic()
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=502, detail="Place suggestions are unavailable right now. Check internet access or pin the location on the map.") from exc
        matches: list[dict] = []
        seen: set[tuple[float, float, str]] = set()
        for feature in payload.get("features", []):
            match = _format_photon_feature(feature)
            if not match:
                continue
            key = (round(match["latitude"], 5), round(match["longitude"], 5), match["display_name"].casefold())
            if key not in seen:
                matches.append(match)
                seen.add(key)
            if len(matches) == 6:
                break
        _place_suggest_cache[normalized] = (time.time(), matches)
        return matches


def reverse_bengaluru_place(latitude: float, longitude: float) -> str | None:
    global _last_photon_request
    min_lon, min_lat, max_lon, max_lat = BENGALURU_BBOX
    if not (min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat):
        return None
    with _place_suggest_lock:
        wait = 0.35 - (time.monotonic() - _last_photon_request)
        if wait > 0:
            time.sleep(wait)
        params = urlencode({"lat": latitude, "lon": longitude, "lang": "en"})
        request = Request(f"{PHOTON_BASE_URL}/reverse?{params}", headers={
            "User-Agent": "FloodGuardAI/1.3 (Bengaluru community flood reporting prototype)",
            "Accept": "application/json",
        })
        _last_photon_request = time.monotonic()
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        features = payload.get("features") or []
        if not features:
            return None
        match = _format_photon_feature(features[0])
        return match["display_name"] if match else None


def fetch_weather(latitude: float, longitude: float) -> dict:
    """Fetch current and hourly forecast context from Open-Meteo with a 10-minute cache."""
    import time
    key = (round(latitude, 3), round(longitude, 3))
    cached = _weather_cache.get(key)
    if cached and time.time() - cached[0] < WEATHER_CACHE_SECONDS:
        return cached[1]
    params = {
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,precipitation,rain,weather_code",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation,precipitation_probability",
        "past_days": 7, "forecast_days": 2, "timezone": "auto",
    }
    url = OPEN_METEO_URL + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "FloodGuardAI/1.2 (urban flood research prototype)"})
    try:
        with urlopen(req, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Live weather is temporarily unavailable. Check internet access and retry.") from exc
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        raise HTTPException(status_code=502, detail="Weather provider returned no hourly forecast data.")
    current_time = (payload.get("current") or {}).get("time", "")
    idx = 0
    # Open-Meteo returns its current local hour in the same format as hourly.time.
    if current_time in times:
        idx = times.index(current_time)
    else:
        now_prefix = datetime.now().strftime("%Y-%m-%dT%H:00")
        candidates = [i for i, value in enumerate(times) if str(value) <= now_prefix]
        if candidates:
            idx = candidates[-1]
    precip = hourly.get("precipitation", [])
    probs = hourly.get("precipitation_probability", [])
    temps = hourly.get("temperature_2m", [])
    humidity = hourly.get("relative_humidity_2m", [])
    def values(arr, start, end):
        return [float(v) for v in arr[max(0, start):min(len(arr), end)] if v is not None]
    future24 = values(precip, idx, idx + 24)
    past24 = values(precip, idx - 24, idx)
    past48 = values(precip, idx - 48, idx)
    past144 = values(precip, idx - 144, idx)
    future_probs = values(probs, idx, idx + 24)
    future_temps = values(temps, idx, idx + 24)
    future_humidity = values(humidity, idx, idx + 24)
    current = payload.get("current") or {}
    weather = {
        "latitude": payload.get("latitude", latitude), "longitude": payload.get("longitude", longitude),
        "timezone": payload.get("timezone", "local"), "current": current,
        "forecast": {
            "rainfall_next_24h_mm": round(sum(future24), 1),
            "rainfall_next_72h_mm_estimate": round(sum(past48) + sum(future24), 1),
            "rainfall_next_7d_window_mm_estimate": round(sum(past144) + sum(future24), 1),
            "precipitation_probability_max_next_24h_pct": round(max(future_probs), 1) if future_probs else None,
            "temperature_mean_next_24h_c": round(sum(future_temps) / len(future_temps), 1) if future_temps else current.get("temperature_2m"),
            "humidity_mean_next_24h_pct": round(sum(future_humidity) / len(future_humidity), 1) if future_humidity else current.get("relative_humidity_2m"),
            "rain_hours_next_24h": sum(1 for x in future24 if x >= 0.1),
        },
        "source": "Open-Meteo Forecast API", "source_url": "https://open-meteo.com/en/docs",
        "fetched_at": datetime.now(timezone.utc).isoformat(), "cache_seconds": WEATHER_CACHE_SECONDS,
        "disclaimer": "Weather observations and forecasts are not direct flood measurements. Use with verified local reports and official alerts.",
    }
    _weather_cache[key] = (time.time(), weather)
    return weather


def load_flood_model():
    if not MODEL_PATH.exists():
        return None, None
    try:
        import joblib
        model = joblib.load(MODEL_PATH)
        meta = json.loads(MODEL_META_PATH.read_text(encoding="utf-8")) if MODEL_META_PATH.exists() else {}
        return model, meta
    except Exception:
        return None, {"status": "load_error"}


def prediction_features(weather: dict) -> dict[str, float]:
    current = weather.get("current", {})
    forecast = weather.get("forecast", {})
    return {
        "rainfall_1d": float(forecast.get("rainfall_next_24h_mm") or 0),
        "rainfall_3d": float(forecast.get("rainfall_next_72h_mm_estimate") or 0),
        "rainfall_7d": float(forecast.get("rainfall_next_7d_window_mm_estimate") or 0),
        "temperature_mean": float(forecast.get("temperature_mean_next_24h_c") or current.get("temperature_2m") or 0),
        "humidity_mean": float(forecast.get("humidity_mean_next_24h_pct") or current.get("relative_humidity_2m") or 0),
        "precipitation_hours": float(forecast.get("rain_hours_next_24h") or 0),
    }


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    try:
        with db() as con:
            con.execute("SELECT 1")
        return {"status": "ok", "service": "FloodGuard AI API", "database": "connected",
                "routing_configured": bool(OSRM_BASE_URL), "weather_provider": "Open-Meteo Forecast API", "ml_model_trained": MODEL_PATH.exists(), "risk_model_validated": False}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable.") from exc


@app.post("/api/geocode")
def search_place(request: GeocodeIn) -> dict:
    matches = geocode_place(request.query)
    return {"matches": matches, "provider": "OpenStreetMap Nominatim",
            "attribution": "© OpenStreetMap contributors", "cached_for_seconds": 86400,
            "disclaimer": "Place search requires internet access. Public Nominatim is rate-limited; this endpoint is user-triggered, not autocomplete."}


@app.get("/api/places/suggest")
def place_suggestions(query: str = Query(min_length=2, max_length=120)) -> dict:
    cleaned = " ".join(query.split())
    if len(cleaned) < 2:
        raise HTTPException(status_code=422, detail="Type at least 2 characters to search.")
    return {
        "matches": suggest_bengaluru_places(cleaned),
        "provider": "Photon / OpenStreetMap",
        "attribution": "© OpenStreetMap contributors",
        "service_area": "Bengaluru Urban search area",
    }


@app.get("/api/places/reverse")
def reverse_place(latitude: float = Query(ge=-90, le=90),
                  longitude: float = Query(ge=-180, le=180)) -> dict:
    return {
        "display_name": reverse_bengaluru_place(latitude, longitude),
        "provider": "Photon / OpenStreetMap",
        "attribution": "© OpenStreetMap contributors",
    }


@app.get("/api/reports")
def get_reports(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT * FROM reports ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/reports", status_code=201)
def create_report(report: ReportIn) -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    with db() as con:
        cur = con.execute(
            """INSERT INTO reports(location,latitude,longitude,severity,description,status,risk,created_at)
               VALUES(?,?,?,?,?,'Reported',?,?)""",
            (report.location, report.latitude, report.longitude, report.severity,
             report.description, report_score(report.severity), created_at))
        row = con.execute("SELECT * FROM reports WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.get("/api/risk")
def get_risk() -> dict:
    with db() as con:
        rows = con.execute("SELECT location,severity,risk,status,created_at FROM reports ORDER BY id DESC LIMIT 200").fetchall()
    return {"method": "severity_score_for_community_reports", "validated_forecast": False,
            "items": [dict(row) for row in rows],
            "note": "Report severity scores summarize user observations; they do not forecast flooding."}


@app.post("/api/risk/estimate")
def risk_estimate(data: EstimateIn) -> dict:
    return estimate_risk(data)


@app.get("/api/weather")
def weather(latitude: float = Query(default=12.9716, ge=-90, le=90),
            longitude: float = Query(default=77.5946, ge=-180, le=180)) -> dict:
    """Live current weather and hourly forecast context; defaults to Bengaluru."""
    return fetch_weather(latitude, longitude)


@app.get("/api/prediction/flood")
def flood_prediction(latitude: float = Query(default=12.9716, ge=-90, le=90),
                     longitude: float = Query(default=77.5946, ge=-180, le=180)) -> dict:
    """Experimental district-level event prediction, only when trained model is present."""
    weather_data = fetch_weather(latitude, longitude)
    features = prediction_features(weather_data)
    model, meta = load_flood_model()
    if model is None:
        return {
            "model_status": "not_trained", "prediction_available": False,
            "location": {"latitude": latitude, "longitude": longitude},
            "weather": weather_data, "features": features,
            "message": "Live weather is connected, but the machine-learning model has not been trained yet. Run train_model.py with internet access to build it from historical weather and the India Flood Inventory.",
            "model_scope": "Bengaluru Urban district-level historical flood-event signal; not street-level waterlogging prediction.",
            "validated_forecast": False,
        }
    try:
        import pandas as pd
        probability = float(model.predict_proba(pd.DataFrame([features]))[0][1])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="The trained model could not produce a prediction. Retrain it using train_model.py.") from exc
    return {
        "model_status": "trained_experimental", "prediction_available": True,
        "probability_percent": round(probability * 100, 1),
        "risk_band": "Elevated" if probability >= 0.60 else "Watch" if probability >= 0.30 else "Lower model signal",
        "location": {"latitude": latitude, "longitude": longitude},
        "weather": weather_data, "features": features, "model_metadata": meta,
        "model_scope": "Bengaluru Urban district-level historical flood-event signal; not street-level waterlogging prediction.",
        "validated_forecast": False,
        "disclaimer": "Experimental model output, not a calibrated probability or emergency warning. Labels are district-scale historical events and may not represent local road flooding. Follow official alerts.",
    }


@app.get("/api/dashboard")
def dashboard() -> dict:
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        high = con.execute("SELECT COUNT(*) FROM reports WHERE severity='high'").fetchone()[0]
        review = con.execute("SELECT COUNT(*) FROM reports WHERE status='Under review'").fetchone()[0]
    return {"total_reports": total, "high_severity": high, "under_review": review,
            "weather_connected": True, "weather_provider": "Open-Meteo Forecast API", "ml_model_trained": MODEL_PATH.exists(), "validated_forecast": False}


@app.patch("/api/reports/{report_id}/status")
def update_status(report_id: int, body: StatusIn) -> dict:
    with db() as con:
        cur = con.execute("UPDATE reports SET status=? WHERE id=?", (body.status, report_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Report not found.")
        row = con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    return dict(row)


@app.post("/api/routes/compare")
def compare_routes(request: RouteCompareIn) -> dict:
    raw_routes = fetch_osrm_routes(request.origin_lon, request.origin_lat,
                                   request.destination_lon, request.destination_lat)
    active_reports = get_active_geo_reports()
    scored = [score_route(route, active_reports) for route in raw_routes]
    if not scored:
        raise HTTPException(status_code=422, detail="No route alternatives were returned.")
    fastest_seconds = min(route["duration_seconds"] for route in scored)
    for route in scored:
        # Explicit trade-off: 1 exposure point is weighted against 1.5 minutes of delay.
        delay_minutes = max(0, route["duration_seconds"] - fastest_seconds) / 60
        route["delay_vs_fastest_minutes"] = round(delay_minutes, 1)
        route["comparison_score"] = round(route["exposure_score"] + delay_minutes * 1.5, 1)
        route["is_fastest"] = route["duration_seconds"] == fastest_seconds
    scored.sort(key=lambda route: (route["comparison_score"], route["duration_seconds"]))
    for index, route in enumerate(scored, start=1):
        route["option"] = index
    return {"routes": scored, "routing_provider": "OSRM",
            "report_window_hours": MAX_REPORT_AGE_HOURS,
            "known_report_count": len(active_reports),
            "method": "heuristic_route_exposure_v1", "validated_forecast": False,
            "disclaimer": "Routes are ranked using recent, geolocated community reports and illustrative weights. No reports do not mean a road is safe. This is not emergency navigation; obey official closures and instructions."}


# Serve the UI from the same origin as the API, simplifying local setup.
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
