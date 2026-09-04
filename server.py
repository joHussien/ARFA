"""
server.py — ARFA backend
- Uses stage1.py LLM geography resolution (llm_detect / fallback_detect)
- Resolves any query (city, county, state) → county FIPS codes
- Fetches USGS gauges using countyCd= (county-exact, not state-wide)
- Also proxies NOAA for per-gauge metadata and stageflow

pip install flask flask-cors
python server.py
Open http://localhost:5050
"""
import sys, json, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, str(Path(__file__).parent))
from stage1 import llm_detect, fallback_detect, get_state_fips, get_county_fips, city_to_counties, get_all_counties_in_state, agent_generate
from arfa_agents import location_agent, structure_agent, controller_agent, reasoning_agent

# Optional pre-warm. Disabled by default so the map/server can start before
# loading a large local model. Set ARFA_PREWARM_LLM=1 for competition demos.
if os.getenv("ARFA_PREWARM_LLM", "0").lower() in {"1","true","yes"}:
    print("Pre-loading LLM...")
    try:
        llm_detect("Oak Ridge, TN")
        print("LLM ready.")
    except Exception as e:
        print(f"LLM pre-load failed: {e}")


app  = Flask(__name__)
CORS(app)

# app.register_blueprint(structures_bp)
# Path to your structures gdb — edit this path
from structures import structures_bp, load_index
app.register_blueprint(structures_bp)
_default_index = str(Path(__file__).resolve().parent / "USA_Structures_Index")
load_index(os.getenv("ARFA_STRUCTURES_INDEX", _default_index))

# ── Structures auto-repair endpoints ─────────────────────────────────────────

from structures_autorepair import start_repair_background, get_status as _repair_get_status

@app.route("/api/structures/repair", methods=["POST"])
def structures_repair():
    """
    Trigger an on-demand download of missing USA Structures GDB files and
    rebuild the pyramid index without restarting the server.

    POST body:
      { "states": ["IN", "IL", "MO"] }   — list of 2-letter state codes

    Returns immediately with { "started": true } if a background job was
    launched, or { "started": false, "reason": "already running" } if one
    is already in progress.

    Poll GET /api/structures/repair/status for live progress.
    """
    body = request.get_json(silent=True) or {}
    states = [str(s).upper().strip() for s in (body.get("states") or []) if s]
    if not states:
        return jsonify({"error": "states list required"}), 400

    started = start_repair_background(states)
    if not started:
        status = _repair_get_status()
        return jsonify({"started": False, "reason": "already running", "status": status})

    print(f"[repair] Background repair started for: {states}")
    return jsonify({"started": True, "states": states})


@app.route("/api/structures/repair/status")
def structures_repair_status():
    """Return the current state of the background repair job."""
    return jsonify(_repair_get_status())

from flood_hazard.blueprint import flood_bp
app.register_blueprint(flood_bp)

NOAA    = "https://api.water.noaa.gov/nwps/v1"
USGS_IV = "https://waterservices.usgs.gov/nwis/iv/"
HDR     = {"Accept": "application/json", "User-Agent": "ARFA/1.0"}


def fetch(url):
    try:
        req = urllib.request.Request(url, headers=HDR)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 404 from NOAA is expected — many USGS gauges have no NOAA record
        if e.code != 404:
            print(f"  fetch error: HTTP {e.code}  url={url[:120]}")
        return {"error": str(e)}
    except Exception as e:
        print(f"  fetch error: {e}  url={url[:120]}")
        return {"error": str(e)}

def fetch_usgs(url):
    try:
        req = urllib.request.Request(url, headers=HDR)
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  fetch error: {e}  url={url[:120]}")
        return {"error": str(e)}


# ── NOAA NWM Flood Inundation Extent ─────────────────────────────────────────
NWM_FIM = ("https://maps.water.noaa.gov/server/rest/services/"
           "nwm/ana_inundation_extent/FeatureServer/0/query")

import math
from flood_hazard.geo import haversine_km as _haversine_km

def merc_to_wgs84(x, y):
    """Convert Web Mercator (EPSG:3857) to WGS84 (EPSG:4326)."""
    lon = x / 20037508.34 * 180
    lat = math.degrees(2 * math.atan(math.exp(y / 20037508.34 * math.pi)) - math.pi / 2)
    return [round(lon, 6), round(lat, 6)]

def esri_rings_to_geojson(rings, native_sr=3857):
    """Convert Esri ring geometry to GeoJSON Polygon coordinates."""
    converted = []
    for ring in rings:
        if native_sr == 3857:
            converted.append([merc_to_wgs84(pt[0], pt[1]) for pt in ring])
        else:
            converted.append([[round(pt[0], 6), round(pt[1], 6)] for pt in ring])
    return {"type": "Polygon", "coordinates": converted}

def classify_severity(streamflow_cfs, gauge_thresholds):
    """
    Classify flood severity by comparing streamflow to gauge thresholds.
    gauge_thresholds: dict with keys action, minor, moderate, major (in cfs).
    Falls back to AEP-based classification if thresholds unavailable.
    """
    if not gauge_thresholds or streamflow_cfs is None:
        return "unknown"
    maj = gauge_thresholds.get("major")
    mod = gauge_thresholds.get("moderate")
    mnr = gauge_thresholds.get("minor")
    act = gauge_thresholds.get("action")
    if maj and streamflow_cfs >= maj:  return "major"
    if mod and streamflow_cfs >= mod:  return "moderate"
    if mnr and streamflow_cfs >= mnr:  return "minor"
    if act and streamflow_cfs >= act:  return "action"
    return "no_flooding"

@app.route("/api/inundation")
def inundation():
    """
    Fetch real-time NWM flood inundation polygons for a bbox.
    Converts Esri JSON geometry (3857) → GeoJSON (4326).
    Classifies severity using NOAA gauge flow thresholds.
    Returns GeoJSON FeatureCollection.
    """
    try:
        min_lat = float(request.args.get("minLat"))
        min_lon = float(request.args.get("minLon"))
        max_lat = float(request.args.get("maxLat"))
        max_lon = float(request.args.get("maxLon"))
    except (TypeError, ValueError):
        return jsonify({"error": "minLat,minLon,maxLat,maxLon required"}), 400

    # Optional: gauge flow thresholds passed from frontend
    # Format: {"action":X,"minor":Y,"moderate":Z,"major":W} in cfs
    try:
        thresholds = json.loads(request.args.get("thresholds", "{}"))
    except Exception:
        thresholds = {}

    params = urllib.parse.urlencode({
        "geometry":          f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType":      "esriGeometryEnvelope",
        "inSR":              "4326",
        "outSR":             "3857",  # get native coords, we convert manually
        "spatialRel":        "esriSpatialRelIntersects",
        "outFields":         "feature_id,streamflow_cfs,reference_time,update_time",
        "f":                 "json",
        "resultRecordCount": 500,
        "where":             "1=1",
        "returnGeometry":    "true",
    })

    url = f"{NWM_FIM}?{params}"
    print(f"[inundation] bbox=({min_lat:.3f},{min_lon:.3f},{max_lat:.3f},{max_lon:.3f})")

    try:
        req = urllib.request.Request(url, headers={**HDR, "Referer": "https://water.noaa.gov"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  [inundation] fetch error: {e}")
        return jsonify({
            "type": "FeatureCollection",
            "features": [], "coverage": False, "error": str(e)
        })

    raw_features = data.get("features", [])
    print(f"  [inundation] {len(raw_features)} raw polygons")

    # Convert to GeoJSON
    features = []
    for f in raw_features:
        attrs = f.get("attributes", {})
        geom  = f.get("geometry", {})
        rings  = geom.get("rings", [])
        if not rings:
            continue

        streamflow = attrs.get("streamflow_cfs")
        severity   = classify_severity(streamflow, thresholds)

        try:
            geojson_geom = esri_rings_to_geojson(rings, native_sr=3857)
        except Exception:
            continue

        features.append({
            "type": "Feature",
            "geometry": geojson_geom,
            "properties": {
                "feature_id":    attrs.get("feature_id"),
                "streamflow_cfs": round(streamflow, 1) if streamflow else None,
                "severity":      severity,
                "reference_time": attrs.get("reference_time"),
                "update_time":   attrs.get("update_time"),
            }
        })

    print(f"  [inundation] {len(features)} converted polygons, coverage={len(features)>0}")
    return jsonify({
        "type":     "FeatureCollection",
        "features": features,
        "count":    len(features),
        "coverage": len(features) > 0,
        "source":   "noaa_nwm_analysis",
    })    
@app.route("/api/gauges/all")
def all_gauges():
    """Fetch all active USGS stream gauges nationwide — called once at startup."""
    url = (f"{USGS_IV}?parameterCd=00065&siteStatus=active"
           f"&format=json&period=PT1H&siteType=ST")
    data = fetch_usgs(url)
    series = data.get("value", {}).get("timeSeries", [])
    result = []
    for ts in series:
        si = ts["sourceInfo"]
        geo = si.get("geoLocation", {}).get("geogLocation", {})
        result.append({
            "lid": si["siteCode"][0]["value"],
            "name": si["siteName"],
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        })
    print(f"[all_gauges] {len(result)} national gauges")
    return jsonify(result)
# ── Serve dashboard ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── Resolve query → list of counties with FIPS ────────────────────────────
@app.route("/api/resolve")
def resolve():
    """
    Given a free-text query, return resolved counties.
    Uses stage1 LLM detect + TIGERweb.
    Response: [{ name, state_fips, county_fips, usgs_county_cd }, ...]
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    print(f"\n[resolve] query: {query}")

    # Geography detection
    try:
        detected_list = llm_detect(query)
        print(f"  [llm] detected: {detected_list}")
    except Exception as e:
        print(f"  [llm] failed ({e}), fallback")
        detected_list = [fallback_detect(query)]

    if not detected_list:
        return jsonify({"error": "Could not identify a US location"}), 400

    counties = []
    for det in detected_list:
        loc_type    = det.get("type", "unknown")
        name        = det.get("name", "").strip()
        state_abbrev = det.get("state", "").strip().upper()

        if loc_type == "unknown" or not state_abbrev:
            continue

        state_fips = get_state_fips(state_abbrev)
        if not state_fips:
            continue

        if loc_type == "state":
            resolved = get_all_counties_in_state(state_fips)
        elif loc_type == "county":
            resolved = get_county_fips(name, state_fips)
        else:  # city
            resolved = city_to_counties(name, state_fips)

        for c in resolved:
            # USGS countyCd = 5-digit: state_fips(2) + county_fips(3)
            usgs_cd = c["state"] + c["county"]
            counties.append({
                "name":          c["name"],
                "state_abbrev":  state_abbrev,
                "state_fips":    c["state"],
                "county_fips":   c["county"],
                "usgs_county_cd": usgs_cd,
            })

    print(f"  resolved {len(counties)} counties: {[c['name'] for c in counties[:5]]}")
    return jsonify({
        "query":    query,
        "detected": detected_list,
        "counties": counties,
    })


# ── USGS gauges for a county (exact) ─────────────────────────────────────
@app.route("/api/gauges")
def gauges():
    """
    county_cd: 5-digit USGS county code (e.g. 22005 for Ascension Parish LA)
    Returns normalized gauge list.
    """
    county_cd = request.args.get("county_cd", "")
    if not county_cd:
        return jsonify({"error": "county_cd required"}), 400

    # Fetch current gage height (00065) AND discharge (00060) in one USGS call.
    # USGS returns one timeSeries per parameter, so we merge the series by site id
    # before returning gauges to the frontend.
    url = (f"{USGS_IV}?countyCd={county_cd}"
           f"&parameterCd=00065,00060"
           f"&siteStatus=active"
           f"&format=json"
           f"&period=P1D")

    print(f"[gauges] USGS countyCd={county_cd}")
    data   = fetch(url)
    series = data.get("value", {}).get("timeSeries", [])

    by_site = {}
    for ts in series:
        si = ts.get("sourceInfo", {})
        codes = si.get("siteCode") or []
        if not codes:
            continue
        lid = str(codes[0].get("value", "")).strip()
        if not lid:
            continue

        geo = si.get("geoLocation", {}).get("geogLocation", {})
        rec = by_site.setdefault(lid, {
            "lid": lid,
            "name": si.get("siteName") or lid,
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
            "county_cd": county_cd,
            "_stage": None,
            "_discharge_cfs": None,
            "_discharge_m3s": None,
            "_sev": "unknown",
        })

        variable_codes = ts.get("variable", {}).get("variableCode", [])
        parameter = str(variable_codes[0].get("value", "")) if variable_codes else ""
        vals = ts.get("values", [{}])[0].get("value", []) if ts.get("values") else []
        latest = None
        for item in reversed(vals):
            raw = item.get("value")
            try:
                value = float(raw)
                if value > -900:
                    latest = value
                    break
            except (TypeError, ValueError):
                continue

        if parameter == "00065":
            rec["_stage"] = latest
            # Collect all valid readings for 7-day average (used as fallback threshold basis)
            all_stage = [float(v.get("value")) for v in vals
                         if v.get("value") not in (None, "", "-999", "-999999")]
            all_stage = [v for v in all_stage if v > -900]
            if all_stage:
                rec["_stage_avg"] = round(sum(all_stage) / len(all_stage), 3)
        elif parameter == "00060":
            rec["_discharge_cfs"] = latest
            rec["_discharge_m3s"] = round(latest * 0.028316846592, 3) if latest is not None else None

    sites = list(by_site.values())

    # Enrich with NOAA/NWPS flood classification (parallel).
    # Priority:
    #   1. Official NOAA/NWPS observed floodCategory  (authoritative)
    #   2. Observed stage vs. NOAA/NWPS published flood-stage thresholds
    #   3. Observed stage vs. 7-day average * 1.5/3.0/4.0  (fallback, no NOAA thresholds)
    #   4. Reporting gauge with no classifiable data -> no_flooding
    def _enrich(g):
        stage = g.get("_stage")
        avg   = g.get("_stage_avg")
        try:
            d = fetch(f"{NOAA}/gauges/{g['lid']}")
            if not d or d.get("error"):
                raise ValueError("no noaa data")
            # 1. Official category
            cat = (d.get("status", {}).get("observed", {}).get("floodCategory") or "").strip().lower()
            if "major"    in cat: g["_sev"] = "major";    return g
            if "moderate" in cat: g["_sev"] = "moderate"; return g
            if "minor"    in cat: g["_sev"] = "minor";    return g
            if "action"   in cat: g["_sev"] = "action";   return g
            if cat and ("no flood" in cat or "normal" in cat or cat == "none"):
                g["_sev"] = "no_flooding"; return g
            # 2. NOAA/NWPS published stage thresholds
            cats = (d.get("flood", {}) or {}).get("categories", {}) or {}
            th = {}
            for k in ("action", "minor", "moderate", "major"):
                node = cats.get(k)
                sv = node.get("stage") if isinstance(node, dict) else None
                try:
                    if sv is not None:
                        fv = float(sv)
                        if fv > 0: th[k] = fv
                except (TypeError, ValueError):
                    pass
            if stage is not None and th:
                if th.get("major")    and stage >= th["major"]:    g["_sev"] = "major"
                elif th.get("moderate") and stage >= th["moderate"]: g["_sev"] = "moderate"
                elif th.get("minor")   and stage >= th["minor"]:   g["_sev"] = "minor"
                elif th.get("action")  and stage >= th["action"]:  g["_sev"] = "action"
                else:                                               g["_sev"] = "no_flooding"
                return g
        except Exception:
            pass
        # 3. Fallback: 7-day avg * 1.5/3.0/4.0
        if stage is not None and avg and avg > 0 and stage > 0:
            if   stage >= avg * 4.0: g["_sev"] = "major"
            elif stage >= avg * 3.0: g["_sev"] = "moderate"
            elif stage >= avg * 1.5: g["_sev"] = "minor"
            else:                    g["_sev"] = "no_flooding"
        else:
            g["_sev"] = "no_flooding" if stage is not None else "unknown"
        return g

    # Guard: USGS may return 503/empty on transient errors — don't crash
    if not sites:
        print(f"  0 sites returned (USGS may be temporarily unavailable)")
        return jsonify([])

    with ThreadPoolExecutor(max_workers=min(8, len(sites))) as pool:
        sites = list(pool.map(_enrich, sites))

    result = sites
    print(f"  {len(result)} sites returned ({len(series)} parameter series)")
    return jsonify(result)


# ── Census tracts for a county ───────────────────────────────────────────
@app.route("/api/tracts")
def tracts():
    state_fips  = request.args.get("state_fips","").zfill(2)
    county_fips = request.args.get("county_fips","").zfill(3)
    if not state_fips or not county_fips:
        return jsonify({"error": "state_fips and county_fips required"}), 400
    url = (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Current/MapServer/8/query"
        f"?f=geojson&where=STATE='{state_fips}'%20AND%20COUNTY='{county_fips}'"
        "&outFields=GEOID,STATE,COUNTY,TRACT,NAME"
        "&returnGeometry=true&outSR=4326&resultRecordCount=500"
    )
    print(f"[tracts] state={state_fips} county={county_fips}")
    data = fetch(url)
    feats = data.get("features", []) if data and "features" in data else []
    print(f"  {len(feats)} tracts")
    return jsonify({"features": feats})


# ── USGS gauges by bounding box (for progressive map loading) ────────────
@app.route("/api/gauges/bbox")
def gauges_bbox():
    """
    Returns up to 500 gauges in the current map viewport.
    Params: minLat, maxLat, minLon, maxLon
    Returns minimal payload: [{lid, name, latitude, longitude}]
    """
    try:
        min_lat = float(request.args.get("minLat", 0))
        max_lat = float(request.args.get("maxLat", 0))
        min_lon = float(request.args.get("minLon", 0))
        max_lon = float(request.args.get("maxLon", 0))
    except ValueError:
        return jsonify({"error": "Invalid bbox"}), 400

    # USGS bBox format: minLon,minLat,maxLon,maxLat
    bbox = f"{min_lon:.4f},{min_lat:.4f},{max_lon:.4f},{max_lat:.4f}"
    url = (f"{USGS_IV}?bBox={bbox}"
           f"&parameterCd=00065"
           f"&siteStatus=active"
           f"&format=json"
           f"&period=P1D"
           f"&siteType=ST")   # streams only

    print(f"[bbox] {bbox}")
    data   = fetch_usgs(url)
    series = data.get("value", {}).get("timeSeries", [])

    result = []
    for ts in series[:500]:   # hard cap 500
        si  = ts["sourceInfo"]
        geo = si.get("geoLocation", {}).get("geogLocation", {})
        lat = geo.get("latitude")
        lon = geo.get("longitude")
        if lat is None or lon is None:
            continue
        vals   = ts.get("values", [{}])[0].get("value", [])
        latest = vals[-1]["value"] if vals else None
        result.append({
            "lid":       si["siteCode"][0]["value"],
            "name":      si["siteName"],
            "latitude":  lat,
            "longitude": lon,
            "_stage":    latest,
        })

    print(f"  {len(result)} gauges in bbox")
    return jsonify(result)


# ── NOAA: single gauge metadata ───────────────────────────────────────────
@app.route("/api/gauge/<lid>")
def gauge_meta(lid):
    return jsonify(fetch(f"{NOAA}/gauges/{lid}"))


# ── NOAA: stage/flow recent history ─────────────────────────────────────
@app.route("/api/gauge/<lid>/stageflow")
def stageflow(lid):
    data = fetch(f"{NOAA}/gauges/{lid}/stageflow")
    # Keep enough high-frequency observations for roughly 10 days of fallback history
    if "observed" in data and "data" in data.get("observed", {}):
        data["observed"]["data"] = data["observed"]["data"][-1200:]
    return jsonify(data)


# ── USGS: 10-day recent history (stage + discharge) ──────────────────────
@app.route("/api/usgs/<site_no>")
def usgs_site(site_no):
    url = (f"{USGS_IV}?sites={site_no}"
           f"&parameterCd=00065,00060"
           f"&format=json&period=P10D")
    data = fetch_usgs(url)
    # Strip sentinel values from all timeSeries
    for ts in data.get("value", {}).get("timeSeries", []):
        for vgroup in ts.get("values", []):
            vgroup["value"] = [
                v for v in vgroup.get("value", [])
                if v.get("value") not in ("-999999", "-999", "")
                and float(v.get("value", 0)) > -900
            ]
    return jsonify(data)

@app.route("/api/usgs/<site_no>/history")
def usgs_history(site_no):
    """
    Return USGS instantaneous gage-height history (parameter 00065).

    Frontend periods:
      P7D   -> raw ~15-minute observations; frontend averages to 15 min
      P30D  -> raw instantaneous observations; frontend averages to 1 hour
      P365D -> raw instantaneous observations fetched in smaller date chunks;
               frontend averages to 12 hours

    Why instantaneous values for all periods?
    Daily-value data only provides one summarized value per day, which cannot
    support the 30-day hourly or 1-year 12-hour views used by the dashboard.

    For the 1-year request we split the USGS request into 90-day windows to
    keep individual upstream responses reasonably sized and then merge them
    back into one WaterML-like JSON response expected by the frontend.
    """
    period = request.args.get("period", "P7D").upper()

    allowed = {"P7D": 7, "P30D": 30, "P365D": 365}
    if period not in allowed:
        return jsonify({
            "error": "Unsupported period",
            "allowed": sorted(allowed.keys())
        }), 400

    days = allowed[period]

    def clean_values(values):
        """Remove USGS missing/sentinel measurements."""
        cleaned = []
        for v in values or []:
            raw = v.get("value")
            try:
                if raw in ("-999999", "-999", "", None):
                    continue
                if float(raw) <= -900:
                    continue
                cleaned.append(v)
            except (TypeError, ValueError):
                continue
        return cleaned

    def stage_series(data):
        """Return the 00065 timeSeries object from a USGS WaterML JSON response."""
        for ts in data.get("value", {}).get("timeSeries", []):
            codes = ts.get("variable", {}).get("variableCode", [])
            code = str(codes[0].get("value", "")) if codes else ""
            if code == "00065":
                return ts
        return None

    # 7D and 30D are small enough for one instantaneous-values request.
    if period in ("P7D", "P30D"):
        url = (
            f"{USGS_IV}?sites={urllib.parse.quote(site_no)}"
            f"&parameterCd=00065"
            f"&format=json"
            f"&period={period}"
            f"&siteStatus=all"
        )

        print(f"[usgs history] site={site_no} period={period}")
        data = fetch_usgs(url)

        if data.get("error"):
            return jsonify(data), 502

        for ts in data.get("value", {}).get("timeSeries", []):
            for vg in ts.get("values", []):
                vg["value"] = clean_values(vg.get("value", []))

        return jsonify(data)

    # 1Y: retrieve IV history in 90-day date windows, newest window included.
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days)
    chunk_days = 90

    cursor = start_dt
    template_ts = None
    merged_by_time = {}
    chunk_count = 0
    failed_chunks = []

    print(f"[usgs history] site={site_no} period=P365D using {chunk_days}-day chunks")

    while cursor < now:
        chunk_end = min(cursor + timedelta(days=chunk_days), now)

        # USGS accepts ISO dates/times. Explicit ranges cannot be mixed with period.
        start_iso = cursor.strftime("%Y-%m-%dT%H:%MZ")
        end_iso = chunk_end.strftime("%Y-%m-%dT%H:%MZ")

        url = (
            f"{USGS_IV}?sites={urllib.parse.quote(site_no)}"
            f"&parameterCd=00065"
            f"&format=json"
            f"&startDT={urllib.parse.quote(start_iso)}"
            f"&endDT={urllib.parse.quote(end_iso)}"
            f"&siteStatus=all"
        )

        data = fetch_usgs(url)
        chunk_count += 1

        if data.get("error"):
            failed_chunks.append({
                "start": start_iso,
                "end": end_iso,
                "error": data.get("error")
            })
            cursor = chunk_end
            continue

        ts = stage_series(data)
        if ts:
            if template_ts is None:
                # Deep copy through JSON so mutating values below cannot alter
                # an object shared with the upstream response.
                template_ts = json.loads(json.dumps(ts))

            groups = ts.get("values", [])
            if groups:
                for item in clean_values(groups[0].get("value", [])):
                    dt = item.get("dateTime")
                    if dt:
                        # Dictionary key removes duplicate boundary observations
                        # between adjacent chunks.
                        merged_by_time[dt] = item

        cursor = chunk_end

    if template_ts is None:
        return jsonify({
            "value": {"timeSeries": []},
            "history_meta": {
                "site": site_no,
                "period": period,
                "chunks": chunk_count,
                "failed_chunks": failed_chunks,
                "readings": 0
            }
        })

    merged_values = sorted(
        merged_by_time.values(),
        key=lambda item: item.get("dateTime", "")
    )

    if template_ts.get("values"):
        template_ts["values"][0]["value"] = merged_values
        # Drop additional value groups if any; dashboard reads the first group.
        template_ts["values"] = [template_ts["values"][0]]
    else:
        template_ts["values"] = [{"value": merged_values}]

    response = {
        "value": {
            "timeSeries": [template_ts]
        },
        "history_meta": {
            "site": site_no,
            "period": period,
            "chunks": chunk_count,
            "failed_chunks": failed_chunks,
            "readings": len(merged_values),
            "start": start_dt.isoformat(),
            "end": now.isoformat()
        }
    }

    print(
        f"  merged {len(merged_values):,} readings from "
        f"{chunk_count} chunks; failures={len(failed_chunks)}"
    )

    return jsonify(response)


# ── USGS: estimated discharge return-period thresholds ───────────────────
# ── OSM: roads + critical facilities for a bbox ───────────────────────────
OVERPASS = "https://overpass-api.de/api/interpreter"

def overpass_post(query):
    try:
        data = urllib.parse.urlencode({"data": query}).encode()
        req  = urllib.request.Request(OVERPASS, data=data, headers=HDR)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  overpass error: {e}")
        return {"error": str(e)}

@app.route("/api/roads")
def roads():
    """
    Fetch OSM road network + critical facilities for a bounding box.
    Returns GeoJSON FeatureCollection of roads + point facilities.
    bbox: minLat,minLon,maxLat,maxLon (Overpass format)
    """
    try:
        min_lat = float(request.args.get("minLat"))
        min_lon = float(request.args.get("minLon"))
        max_lat = float(request.args.get("maxLat"))
        max_lon = float(request.args.get("maxLon"))
    except (TypeError, ValueError):
        return jsonify({"error": "minLat,minLon,maxLat,maxLon required"}), 400
    # Reject if bbox too large (>1.0° = ~55km)
    if (max_lat - min_lat) > 1.0 or (max_lon - min_lon) > 1.0:
        return jsonify({"error": "Area too large for road analysis. Zoom in more."}), 400
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    print(f"[roads] bbox={bbox}")

    query = f"""
        [out:json][timeout:25][maxsize:10000000];
        (
        way["highway"~"^(primary|secondary|tertiary|trunk|motorway)$"]({bbox});
        node["amenity"~"^(hospital|clinic|fire_station|police|shelter)$"]({bbox});
        );
        out geom;
        """
    result = overpass_post(query)
    if "error" in result:
        return jsonify(result), 500

    features = []
    for el in result.get("elements", []):
        if el["type"] == "way":
            coords = [[n["lon"], n["lat"]] for n in el.get("geometry", [])]
            if len(coords) >= 2:
                tags = el.get("tags", {})
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "osm_id":   el["id"],
                        "type":     "road",
                        "highway":  tags.get("highway", ""),
                        "name":     tags.get("name", ""),
                    }
                })
        elif el["type"] == "node" and ("lat" in el):
            tags = el.get("tags", {})
            amenity = tags.get("amenity") or tags.get("emergency", "facility")
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                "properties": {
                    "osm_id":  el["id"],
                    "type":    "facility",
                    "amenity": amenity,
                    "name":    tags.get("name", amenity.replace("_", " ").title()),
                }
            })

    roads_count    = sum(1 for f in features if f["properties"]["type"] == "road")
    facility_count = sum(1 for f in features if f["properties"]["type"] == "facility")
    print(f"  {roads_count} road segments, {facility_count} facilities")
    return jsonify({"type": "FeatureCollection", "features": features})


# ── Road routing (OSRM) ───────────────────────────────────────────────────────
# Public OSRM is convenient for the prototype. Set OSRM_BASE_URL to a self-hosted
# instance later without changing the frontend/API contract.
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")


def _osrm_route(points, alternatives=False, timeout=20):
    """Call OSRM for a list of (lat, lon) points and return raw route objects."""
    coords = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)
    params = urllib.parse.urlencode({
        "alternatives": "3" if alternatives else "false",
        "steps": "true",
        "overview": "full",
        "geometries": "geojson",
        "annotations": "false",
        "continue_straight": "false",
    })
    url = f"{OSRM_BASE_URL}/route/v1/driving/{coords}?{params}"
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if data.get("code") != "Ok":
        raise RuntimeError(data.get("message") or data.get("code") or "No route found")
    return data.get("routes", [])


def _route_signature(route):
    """Coarse geometry signature used to avoid returning the same route repeatedly."""
    geom = (route or {}).get("geometry") or {}
    coords = geom.get("coordinates") or []
    if not coords:
        return None
    # Sample up to ~12 points and round enough to collapse near-identical paths.
    stride = max(1, len(coords) // 12)
    sample = coords[::stride][:12]
    return tuple((round(float(x), 4), round(float(y), 4)) for x, y in sample)


def _detour_waypoints(origin_lat, origin_lon, dest_lat, dest_lon):
    """
    Generate small left/right midpoint offsets. They are only a fallback when OSRM
    returns one alternative. OSRM still snaps/routes them on the actual road network.
    """
    import math

    mean_lat = math.radians((origin_lat + dest_lat) / 2.0)
    dx = (dest_lon - origin_lon) * 111_320.0 * max(0.2, math.cos(mean_lat))
    dy = (dest_lat - origin_lat) * 110_540.0
    d = math.hypot(dx, dy)
    if d < 400:  # Very short trips often have no meaningful alternatives.
        return []

    # Offset is proportional to trip length, bounded so city routes remain sensible.
    offset_m = max(250.0, min(1800.0, d * 0.20))
    nx, ny = -dy / d, dx / d
    mid_lat = (origin_lat + dest_lat) / 2.0
    mid_lon = (origin_lon + dest_lon) / 2.0

    out = []
    for sign in (1.0, -1.0, 1.65, -1.65):
        ox = nx * offset_m * sign
        oy = ny * offset_m * sign
        lat = mid_lat + oy / 110_540.0
        lon = mid_lon + ox / (111_320.0 * max(0.2, math.cos(mean_lat)))
        out.append((lat, lon))
    return out


def _normalize_route(route, idx, generation="osrm_alternative"):
    legs = route.get("legs", [])
    steps = []
    for leg in legs:
        for step in leg.get("steps", []):
            maneuver = step.get("maneuver", {})
            steps.append({
                "name": step.get("name") or "",
                "distance_m": round(float(step.get("distance") or 0), 1),
                "duration_s": round(float(step.get("duration") or 0), 1),
                "instruction_type": maneuver.get("type") or "",
                "modifier": maneuver.get("modifier") or "",
            })
    return {
        "id": f"route_{idx}",
        "label": "Fastest" if idx == 1 else f"Alternative {idx - 1}",
        "distance_m": round(float(route.get("distance") or 0), 1),
        "duration_s": round(float(route.get("duration") or 0), 1),
        "geometry": route.get("geometry"),
        "steps": steps,
        "generation": generation,
    }



# ── Route flood-exposure analysis ─────────────────────────────────────────────
# NOTE: /api/routes/exposure is superseded by /api/flood/score-routes which uses
# HAND-derived hazard polygons (flood_hazard blueprint) instead of this NWM
# point-in-polygon approximation. Kept as a thin fallback only.
def _haversine_m(lat1, lon1, lat2, lon2):
    return _haversine_km(lat1, lon1, lat2, lon2) * 1000.0


# ── Structures: filtered query endpoint (Feature 2) ───────────────────────────
@app.route("/api/structures/query", methods=["POST"])
def structures_query():
    """
    Query USA Structures within a bbox with server-side filters.
    Supports responder queries like:
      "schools taller than 5 metres"
      "all hospitals in the area"
      "buildings named 'community center'"

    POST body:
      bbox: [minLon, minLat, maxLon, maxLat]   (required)
      filters (all optional):
        occ_cls:      list[str]    occupancy classes e.g. ["Education","Government"]
        prim_occ_keywords: list[str]  case-insensitive substrings in PRIM_OCC
        name_keywords:     list[str]  case-insensitive substrings in PROP_ADDR/PRIM_OCC
        min_height_m: float   minimum building height in metres
        max_height_m: float   maximum building height in metres
        min_sqfeet:   float   minimum footprint sq ft
      limit: int  max features to return (default 5000)
    """
    from structures import _cells, find_states_for_bbox, query_state_gdb, gdf_to_features
    import pandas as pd

    if _cells is None:
        return jsonify({"error": "Spatial index not loaded"}), 503

    body = request.get_json(silent=True) or {}
    raw_bbox = body.get("bbox")
    if not raw_bbox or len(raw_bbox) != 4:
        return jsonify({"error": "bbox [minLon,minLat,maxLon,maxLat] required"}), 400

    try:
        min_lon, min_lat, max_lon, max_lat = map(float, raw_bbox)
    except (TypeError, ValueError):
        return jsonify({"error": "bbox must contain four numbers"}), 400

    filters = body.get("filters") or {}
    occ_cls_filter       = [str(x).strip() for x in (filters.get("occ_cls") or [])]
    prim_keywords        = [str(x).lower().strip() for x in (filters.get("prim_occ_keywords") or [])]
    name_keywords        = [str(x).lower().strip() for x in (filters.get("name_keywords") or [])]
    min_height           = filters.get("min_height_m")
    max_height           = filters.get("max_height_m")
    min_sqfeet           = filters.get("min_sqfeet")
    limit                = int(body.get("limit") or 5000)

    bbox_tuple = (min_lon, min_lat, max_lon, max_lat)
    state_codes = find_states_for_bbox([min_lon, min_lat, max_lon, max_lat])
    if not state_codes:
        return jsonify({"type": "FeatureCollection", "features": [], "returned": 0})

    all_gdfs = []
    for sc in state_codes:
        gdf = query_state_gdb(sc, bbox_tuple)
        if gdf is not None and not gdf.empty:
            all_gdfs.append(gdf)

    if not all_gdfs:
        return jsonify({"type": "FeatureCollection", "features": [], "returned": 0})

    import geopandas as gpd
    combined = gpd.pd.concat(all_gdfs, ignore_index=True) if len(all_gdfs) > 1 else all_gdfs[0]

    # Apply filters
    mask = pd.Series([True] * len(combined), index=combined.index)

    if occ_cls_filter:
        occ_col = next((c for c in combined.columns if c.upper() == "OCC_CLS"), None)
        if occ_col:
            mask &= combined[occ_col].str.strip().isin(occ_cls_filter)

    if prim_keywords:
        prim_col = next((c for c in combined.columns if c.upper() == "PRIM_OCC"), None)
        if prim_col:
            prim_lower = combined[prim_col].fillna("").str.lower()
            mask &= prim_lower.apply(lambda v: any(k in v for k in prim_keywords))

    if name_keywords:
        addr_col = next((c for c in combined.columns if c.upper() == "PROP_ADDR"), None)
        prim_col = next((c for c in combined.columns if c.upper() == "PRIM_OCC"), None)
        if addr_col and prim_col:
            combined_text = (combined[addr_col].fillna("") + " " + combined[prim_col].fillna("")).str.lower()
            mask &= combined_text.apply(lambda v: any(k in v for k in name_keywords))

    if min_height is not None:
        ht_col = next((c for c in combined.columns if c.upper() == "HEIGHT"), None)
        if ht_col:
            ht = pd.to_numeric(combined[ht_col], errors="coerce")
            mask &= ht >= float(min_height)

    if max_height is not None:
        ht_col = next((c for c in combined.columns if c.upper() == "HEIGHT"), None)
        if ht_col:
            ht = pd.to_numeric(combined[ht_col], errors="coerce")
            mask &= ht <= float(max_height)

    if min_sqfeet is not None:
        sq_col = next((c for c in combined.columns if c.upper() == "SQFEET"), None)
        if sq_col:
            sq = pd.to_numeric(combined[sq_col], errors="coerce")
            mask &= sq >= float(min_sqfeet)

    filtered = combined[mask].copy()

    # Optional flood-aware semantic relation. The LLM only chooses inside/outside;
    # this intersection is deterministic and uses the current HAND hazard.
    hazard_relation = str(body.get("hazard_relation") or "any").lower()
    hazard_applied = False
    if hazard_relation in {"inside", "outside"} and not filtered.empty:
        try:
            from flood_hazard.service import FloodHazardService
            from shapely.geometry import shape
            from shapely.ops import unary_union
            hz = FloodHazardService().get_hazard(
                bbox=[min_lon, min_lat, max_lon, max_lat],
                arfa_gauges=body.get("gauges") or None,
            )
            polys = [shape(f.get("geometry")) for f in (hz.hazard_geojson.get("features") or []) if f.get("geometry")]
            if polys:
                hazard_geom = unary_union(polys)
                if filtered.crs is not None and str(filtered.crs).upper() != "EPSG:4326":
                    filtered = filtered.to_crs(4326)
                intersects = filtered.geometry.intersects(hazard_geom)
                filtered = filtered[intersects if hazard_relation == "inside" else ~intersects]
                hazard_applied = True
        except Exception as exc:
            print(f"[structures/query] hazard relation unavailable: {exc}")

    total = len(filtered)
    capped = total > limit
    if capped:
        filtered = filtered.iloc[:limit]

    print(f"[structures/query] {total} matches → returning {len(filtered)} (filters={filters})")
    features = gdf_to_features(filtered)
    return jsonify({
        "type": "FeatureCollection",
        "features": features,
        "total_matched": total,
        "returned": len(features),
        "capped": capped,
        "filters_applied": filters,
        "hazard_relation": hazard_relation,
        "hazard_relation_applied": hazard_applied,
    })


# ── Live road conditions (Feature 3) ─────────────────────────────────────────
# Uses TomTom Traffic Incidents API (free tier) to get live road closures and
# incidents along candidate routes. No API key needed for basic incident data.
# If TomTom is unavailable, falls back to OpenStreetMap-based HERE-style check.
_TOMTOM_INCIDENTS = "https://api.tomtom.com/traffic/services/5/incidentDetails"
_OVERPASS_CLOSED  = OVERPASS  # reuse existing Overpass constant

@app.route("/api/roads/conditions", methods=["POST"])
def road_conditions():
    """
    Check live road conditions along one or more route geometries.
    Uses TomTom Traffic Incidents API (key optional — returns incidents without
    it in many regions) with an Overpass-based closure fallback.

    POST body:
      routes: list of {id, geometry: {type: "LineString", coordinates: [...]}}
      bbox:   [minLon, minLat, maxLon, maxLat]  (derived from routes if omitted)
      tomtom_key: str (optional — pass from frontend env if available)

    Returns per-route incident list + a summary flag per route:
      {
        "route_id": "route_1",
        "incidents": [...],
        "has_closures": bool,
        "incident_count": int
      }
    """
    body = request.get_json(silent=True) or {}
    routes = body.get("routes") or []
    if not routes:
        return jsonify({"error": "routes required"}), 400

    tomtom_key = body.get("tomtom_key") or os.environ.get("ARFA_TOMTOM_KEY", "")

    # Derive bbox from route geometry
    all_coords = []
    for r in routes:
        coords = (r.get("geometry") or {}).get("coordinates") or []
        all_coords.extend(coords)
    if not all_coords:
        return jsonify({"error": "route geometries required"}), 400

    lons = [c[0] for c in all_coords]
    lats = [c[1] for c in all_coords]
    pad = 0.01
    min_lon, max_lon = min(lons) - pad, max(lons) + pad
    min_lat, max_lat = min(lats) - pad, max(lats) + pad
    bbox = body.get("bbox") or [min_lon, min_lat, max_lon, max_lat]

    incidents = []

    # --- TomTom Traffic Incidents ---
    if tomtom_key:
        try:
            params = urllib.parse.urlencode({
                "key":       tomtom_key,
                "bbox":      f"{min_lon},{min_lat},{max_lon},{max_lat}",
                "fields":    "{incidents{type,geometry,properties{iconCategory,magnitudeOfDelay,events{description,code},startTime,endTime,from,to,length,delay,roadNumbers,aci{probabilityOfOccurrence}}}}",
                "language":  "en-GB",
                "categoryFilter": "0,1,2,3,4,5,6,7,8,9,10,11",  # all incident types
                "timeValidityFilter": "present",
                "f":         "json",
            })
            req = urllib.request.Request(f"{_TOMTOM_INCIDENTS}?{params}", headers=HDR)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            for inc in data.get("incidents") or []:
                props = inc.get("properties") or {}
                events = props.get("events") or [{}]
                geom = inc.get("geometry") or {}
                incidents.append({
                    "source": "TomTom",
                    "type": inc.get("type", ""),
                    "category": props.get("iconCategory", ""),
                    "delay_s": props.get("magnitudeOfDelay", 0),
                    "description": events[0].get("description", "") if events else "",
                    "from": props.get("from", ""),
                    "to": props.get("to", ""),
                    "start_time": props.get("startTime", ""),
                    "end_time": props.get("endTime", ""),
                    "geometry": geom,
                    "is_closure": props.get("iconCategory", "") in {6, 7, "ROAD_CLOSED", "LANE_CLOSED"},
                })
        except Exception as e:
            print(f"[road_conditions] TomTom failed: {e}")

    # --- Overpass fallback: OSM-tagged closed/restricted roads ---
    if not incidents:
        try:
            overpass_bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
            query = f"""
                [out:json][timeout:15];
                (
                  way["access"="no"]({overpass_bbox});
                  way["highway"]["closed"="yes"]({overpass_bbox});
                  way["highway"]["seasonal"="yes"]({overpass_bbox});
                );
                out geom;
            """
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(_OVERPASS_CLOSED, data=data, headers=HDR)
            with urllib.request.urlopen(req, timeout=20) as resp:
                osm = json.loads(resp.read())
            for el in osm.get("elements", []):
                tags = el.get("tags", {})
                geom_nodes = el.get("geometry", [])
                coords = [[n["lon"], n["lat"]] for n in geom_nodes if "lon" in n]
                incidents.append({
                    "source": "OSM",
                    "type": "ROAD_CLOSED",
                    "category": "ROAD_CLOSED",
                    "delay_s": 9999,
                    "description": f"Access restricted: {tags.get('access') or tags.get('closed') or 'seasonal'}",
                    "from": tags.get("name", ""),
                    "to": "",
                    "start_time": "",
                    "end_time": "",
                    "geometry": {"type": "LineString", "coordinates": coords} if coords else {},
                    "is_closure": True,
                })
        except Exception as e:
            print(f"[road_conditions] Overpass fallback failed: {e}")

    # --- Intersect incidents with each route ---
    from shapely.geometry import LineString, shape
    from shapely.ops import unary_union

    incident_geoms = []
    for inc in incidents:
        try:
            if inc.get("geometry") and inc["geometry"].get("coordinates"):
                geom = shape(inc["geometry"])
                if not geom.is_empty:
                    incident_geoms.append(geom)
        except Exception:
            continue

    incident_union = unary_union(incident_geoms) if incident_geoms else None

    results = []
    for route in routes:
        rid = route.get("id", "route")
        coords = (route.get("geometry") or {}).get("coordinates") or []
        if not coords or len(coords) < 2:
            results.append({"route_id": rid, "incidents": [], "has_closures": False, "incident_count": 0})
            continue
        try:
            route_line = LineString(coords)
            if incident_union is not None and route_line.intersects(incident_union):
                route_incidents = [
                    inc for inc, g in zip(incidents, incident_geoms)
                    if route_line.intersects(g)
                ]
            else:
                route_incidents = []
        except Exception:
            route_incidents = []

        has_closures = any(i.get("is_closure") for i in route_incidents)
        results.append({
            "route_id": rid,
            "incidents": route_incidents,
            "has_closures": has_closures,
            "incident_count": len(route_incidents),
        })

    return jsonify({
        "routes": results,
        "total_incidents_in_area": len(incidents),
        "sources_used": list({i["source"] for i in incidents}) if incidents else ["none"],
        "note": "Incident data is indicative only. Verify road conditions before travel.",
    })



# ── ARFA constrained agent layer ─────────────────────────────────────────────
# LLM responsibilities are deliberately narrow: semantic interpretation and
# evidence synthesis. Geometry, filtering, HAND/DEM and routing remain tools.

@app.route("/api/agent/location", methods=["POST"])
def agent_location():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify({"error":"message required"}), 400
    return jsonify(location_agent(message))

@app.route("/api/agent/structure-query", methods=["POST"])
def agent_structure_query():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify({"error":"message required"}), 400
    return jsonify(structure_agent(message, body.get("context") or {}))

@app.route("/api/agent/reason", methods=["POST"])
def agent_reason():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    observation = body.get("observation") or {}
    history = body.get("history") or []
    if not message and not observation:
        return jsonify({"error":"message or observation required"}), 400
    try:
        return jsonify({"response": reasoning_agent(message, observation, history)})
    except Exception as exc:
        print(f"[evidence-agent] {exc}")
        return jsonify({"error":str(exc)}), 500

@app.route("/api/agent/decide", methods=["POST"])
def agent_decide():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify({"error":"message required"}), 400
    return jsonify(controller_agent(message, body.get("context") or {}))

@app.route("/api/agent/dispatch", methods=["POST"])
def agent_dispatch():
    """Single constrained semantic turn used by the frontend.

    It returns one next action plus an optional structure interpretation.  It
    never executes GIS operations itself.
    """
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    context = body.get("context") or {}
    if not message:
        return jsonify({"error":"message required"}), 400
    decision = controller_agent(message, context)
    structure = None
    if decision.get("action") in {"query_structures","filter_facilities","load_facilities","offer_facilities"}:
        structure = structure_agent(message, context)
    return jsonify({"decision":decision,"structure":structure})

@app.route("/api/routes")
def route_candidates():
    """Generate up to three distinct road-network routes between origin/destination."""
    try:
        origin_lat = float(request.args.get("originLat"))
        origin_lon = float(request.args.get("originLon"))
        dest_lat   = float(request.args.get("destLat"))
        dest_lon   = float(request.args.get("destLon"))
    except (TypeError, ValueError):
        return jsonify({"error": "originLat,originLon,destLat,destLon required"}), 400

    for lat, lon, label in [
        (origin_lat, origin_lon, "origin"),
        (dest_lat, dest_lon, "destination"),
    ]:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify({"error": f"Invalid {label} coordinates"}), 400

    print(f"[routes] {origin_lat:.5f},{origin_lon:.5f} -> {dest_lat:.5f},{dest_lon:.5f}")

    try:
        primary = _osrm_route(
            [(origin_lat, origin_lon), (dest_lat, dest_lon)],
            alternatives=True,
        )
    except Exception as e:
        print(f"  [routes] OSRM fetch error: {e}")
        return jsonify({"error": "Routing service unavailable", "detail": str(e)}), 502

    candidates = []
    signatures = set()

    def add_candidate(route, generation):
        if not route or not route.get("geometry"):
            return
        sig = _route_signature(route)
        if sig is not None and sig in signatures:
            return
        if sig is not None:
            signatures.add(sig)
        candidates.append((route, generation))

    for route in primary:
        add_candidate(route, "osrm_alternative")
        if len(candidates) >= 3:
            break

    # The public OSRM service frequently returns only one route. If that happens,
    # ask OSRM to route through offset midpoint waypoints to obtain additional,
    # still road-valid, candidate corridors. These are route alternatives only;
    # they are NOT yet flood-safe recommendations.
    if len(candidates) < 3 and candidates:
        fastest_s = float(candidates[0][0].get("duration") or 0)
        for waypoint in _detour_waypoints(origin_lat, origin_lon, dest_lat, dest_lon):
            try:
                detours = _osrm_route(
                    [(origin_lat, origin_lon), waypoint, (dest_lat, dest_lon)],
                    alternatives=False,
                    timeout=12,
                )
            except Exception as e:
                print(f"  [routes] detour candidate failed: {e}")
                continue
            if not detours:
                continue
            route = detours[0]
            duration = float(route.get("duration") or 0)
            # Avoid absurd alternatives while allowing meaningful city detours.
            if fastest_s and duration > fastest_s * 1.85:
                continue
            add_candidate(route, "waypoint_alternative")
            if len(candidates) >= 3:
                break

    if not candidates:
        return jsonify({"error": "No drivable route found between these points"}), 404

    # Sort so route 1 is genuinely the fastest, irrespective of generation method.
    candidates.sort(key=lambda item: float(item[0].get("duration") or 1e30))
    routes = [_normalize_route(route, idx, generation) for idx, (route, generation) in enumerate(candidates[:3], 1)]

    return jsonify({
        "origin": {"lat": origin_lat, "lon": origin_lon},
        "destination": {"lat": dest_lat, "lon": dest_lon},
        "routes": routes,
        "source": "osrm",
        "note": "Candidate road-network routes. Flood exposure is assessed separately after route generation.",
    })


@app.route("/api/routes/flood-aware")
def route_candidates_flood_aware():
    """
    Flood-aware routing: generate routes that actively try to avoid the current
    HAND/NWM flood hazard zone by injecting intermediate waypoints that steer
    OSRM around flooded road segments.

    Query params (same as /api/routes):
        originLat, originLon, destLat, destLon

    Algorithm:
      1. Fetch up to 3 standard OSRM candidates (same as /api/routes).
      2. Compute the HAND flood hazard for the route bbox.
      3. For each flooded route, extract candidate avoidance waypoints:
         - Find flood-zone boundary crossing points along the route.
         - Offset those points perpendicularly (or sample hazard-polygon exterior
           ring) to find road-network points outside the flood zone.
         - Re-route via OSRM through those waypoints.
      4. Return both original and avoidance routes, clearly labelled, together
         with per-route flood scoring so the frontend can compare them.

    Returns:
      {
        "origin": {...},
        "destination": {...},
        "standard_routes": [...],   // original OSRM candidates, scored
        "flood_aware_routes": [...], // avoidance attempts, scored; empty if no
                                     // flooded segments or avoidance failed
        "hazard_metadata": {...},
        "note": "..."
      }
    """
    try:
        origin_lat = float(request.args.get("originLat"))
        origin_lon = float(request.args.get("originLon"))
        dest_lat   = float(request.args.get("destLat"))
        dest_lon   = float(request.args.get("destLon"))
    except (TypeError, ValueError):
        return jsonify({"error": "originLat,originLon,destLat,destLon required"}), 400

    for lat, lon, label in [
        (origin_lat, origin_lon, "origin"),
        (dest_lat, dest_lon, "destination"),
    ]:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify({"error": f"Invalid {label} coordinates"}), 400

    print(f"[flood-aware-routes] {origin_lat:.5f},{origin_lon:.5f} -> {dest_lat:.5f},{dest_lon:.5f}")

    # ── Step 1: Standard OSRM candidates ─────────────────────────────────────
    try:
        primary = _osrm_route(
            [(origin_lat, origin_lon), (dest_lat, dest_lon)],
            alternatives=True,
        )
    except Exception as e:
        return jsonify({"error": "Routing service unavailable", "detail": str(e)}), 502

    candidates = []
    signatures = set()

    def add_candidate(route, generation):
        if not route or not route.get("geometry"):
            return
        sig = _route_signature(route)
        if sig is not None and sig in signatures:
            return
        if sig is not None:
            signatures.add(sig)
        candidates.append((route, generation))

    for route in primary:
        add_candidate(route, "osrm_alternative")
        if len(candidates) >= 3:
            break

    if len(candidates) < 3 and candidates:
        fastest_s = float(candidates[0][0].get("duration") or 0)
        for waypoint in _detour_waypoints(origin_lat, origin_lon, dest_lat, dest_lon):
            try:
                detours = _osrm_route(
                    [(origin_lat, origin_lon), waypoint, (dest_lat, dest_lon)],
                    alternatives=False,
                    timeout=12,
                )
            except Exception:
                continue
            if not detours:
                continue
            route = detours[0]
            if fastest_s and float(route.get("duration") or 0) > fastest_s * 1.85:
                continue
            add_candidate(route, "waypoint_alternative")
            if len(candidates) >= 3:
                break

    if not candidates:
        return jsonify({"error": "No drivable route found between these points"}), 404

    candidates.sort(key=lambda item: float(item[0].get("duration") or 1e30))
    standard_routes = [_normalize_route(r, idx, gen) for idx, (r, gen) in enumerate(candidates[:3], 1)]

    # ── Step 2: HAND flood hazard for the bounding box of all routes ─────────
    hazard_fc = None
    hazard_meta = None
    try:
        from flood_hazard.service import FloodHazardService
        from flood_hazard.route_risk import score_routes

        all_coords = []
        for r in standard_routes:
            coords = (r.get("geometry") or {}).get("coordinates") or []
            all_coords.extend(coords)
        xs = [c[0] for c in all_coords]
        ys = [c[1] for c in all_coords]
        pad = 0.03
        bbox = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]

        hz = FloodHazardService().get_hazard(bbox=bbox)
        hazard_fc = hz.hazard_geojson
        hazard_meta = hz.metadata
    except Exception as e:
        print(f"[flood-aware-routes] HAND unavailable: {e}")

    # ── Step 3: Score the standard routes ────────────────────────────────────
    def _to_routes_fc(routes):
        return {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": r["geometry"],
                 "properties": {"id": r["id"], "distance_km": round(r["distance_m"] / 1000, 3),
                                "travel_time_min": round(r["duration_s"] / 60, 2)}}
                for r in routes
            ]
        }

    scored_standard = standard_routes
    if hazard_fc:
        try:
            from flood_hazard.route_risk import score_routes
            scored_fc = score_routes(_to_routes_fc(standard_routes), hazard_fc)
            by_id = {f["properties"]["id"]: f["properties"] for f in scored_fc.get("features", [])}
            ranking_map = {r["route_id"]: r for r in scored_fc.get("ranking", [])}
            for r in standard_routes:
                props = by_id.get(r["id"], {})
                rank  = ranking_map.get(r["id"], {})
                r["flood_exposure"] = {
                    "flooded_length_m":     props.get("flooded_length_m"),
                    "flooded_fraction":     props.get("flooded_fraction"),
                    "flood_status":         props.get("flood_status"),
                    "flood_risk_score":     props.get("flood_risk_score"),
                    "rank":                 rank.get("rank"),
                    "flooded_segments":     props.get("flooded_segments"),
                    "flood_crossing_points": props.get("flood_crossing_points"),
                } if props else None
            scored_standard = standard_routes
        except Exception as e:
            print(f"[flood-aware-routes] scoring failed: {e}")

    # ── Step 4: Build flood-avoidance waypoints and re-route ─────────────────
    flood_aware_routes = []
    if hazard_fc:
        try:
            import math as _math
            from shapely.geometry import shape as _shape, Point as _Point, LineString as _LineString
            from shapely.ops import unary_union as _unary_union

            hazard_geoms = [_shape(f["geometry"]) for f in hazard_fc.get("features", []) if f.get("geometry")]
            hazard_union = _unary_union(hazard_geoms) if hazard_geoms else None

            def _perpendicular_offsets(pt_lon, pt_lat, bearing_deg, distances_m=(300, 500, 800)):
                """Return candidate (lat,lon) pairs offset left and right of a point."""
                R = 6371000.0
                offsets = []
                for dist in distances_m:
                    for side in (90, -90):
                        b = _math.radians(bearing_deg + side)
                        lat1, lon1 = _math.radians(pt_lat), _math.radians(pt_lon)
                        lat2 = _math.asin(_math.sin(lat1) * _math.cos(dist / R) +
                                          _math.cos(lat1) * _math.sin(dist / R) * _math.cos(b))
                        lon2 = lon1 + _math.atan2(_math.sin(b) * _math.sin(dist / R) * _math.cos(lat1),
                                                   _math.cos(dist / R) - _math.sin(lat1) * _math.sin(lat2))
                        candidate = (_math.degrees(lat2), _math.degrees(lon2))
                        pt_candidate = _Point(_math.degrees(lon2), _math.degrees(lat2))
                        if hazard_union is None or not hazard_union.contains(pt_candidate):
                            offsets.append(candidate)
                return offsets

            def _bearing(lat1, lon1, lat2, lon2):
                import math as m
                dl = m.radians(lon2 - lon1)
                la1, la2 = m.radians(lat1), m.radians(lat2)
                x = m.sin(dl) * m.cos(la2)
                y = m.cos(la1) * m.sin(la2) - m.sin(la1) * m.cos(la2) * m.cos(dl)
                return (m.degrees(m.atan2(x, y)) + 360) % 360

            # For each flooded standard route, attempt avoidance
            fa_sigs = set()
            fa_idx = 1
            for r in scored_standard:
                exp = r.get("flood_exposure") or {}
                if exp.get("flood_status") not in ("caution", "avoid"):
                    continue
                coords = (r.get("geometry") or {}).get("coordinates") or []
                if len(coords) < 2:
                    continue

                # Find crossing points along route
                route_line = _LineString(coords)  # (lon, lat)
                if hazard_union is None:
                    continue
                boundary = hazard_union.boundary
                crossings = route_line.intersection(boundary)
                from shapely.geometry import MultiPoint, Point as _Pt
                crossing_pts = []
                def _collect_pts(g):
                    if g.is_empty: return
                    if g.geom_type == "Point": crossing_pts.append(g)
                    elif hasattr(g, "geoms"):
                        for sub in g.geoms: _collect_pts(sub)
                _collect_pts(crossings)

                if not crossing_pts:
                    # Route is fully inside flood — use midpoint
                    mid = route_line.interpolate(0.5, normalized=True)
                    crossing_pts = [mid]

                # Pick the first crossing point to build avoidance waypoints
                # Sort by distance from origin
                crossing_pts.sort(key=lambda p: route_line.project(p, normalized=True))

                # Build a midpoint near the first crossing for bearing estimation
                cp = crossing_pts[0]
                cp_lon, cp_lat = cp.x, cp.y
                # Bearing at that point
                frac = route_line.project(cp, normalized=True)
                frac2 = min(frac + 0.05, 1.0)
                p2 = route_line.interpolate(frac2, normalized=True)
                bear = _bearing(cp_lat, cp_lon, p2.y, p2.x)

                avoidance_added = False
                for waypoint_latlon in _perpendicular_offsets(cp_lon, cp_lat, bear):
                    # Build waypoint list: origin → avoidance point → dest
                    waypoints = [
                        (origin_lat, origin_lon),
                        waypoint_latlon,
                        (dest_lat, dest_lon),
                    ]
                    try:
                        fa_osrm = _osrm_route(waypoints, alternatives=False, timeout=15)
                    except Exception:
                        continue
                    if not fa_osrm:
                        continue
                    fa_route = fa_osrm[0]
                    sig = _route_signature(fa_route)
                    if sig in fa_sigs:
                        continue
                    fa_sigs.add(sig)
                    norm = _normalize_route(fa_route, fa_idx, "flood_aware")
                    norm["label"] = f"Flood-Aware {fa_idx}"
                    norm["generation"] = "flood_aware"
                    flood_aware_routes.append(norm)
                    fa_idx += 1
                    avoidance_added = True
                    break  # one avoidance route per flooded candidate

                if len(flood_aware_routes) >= 2:
                    break

            # Score flood-aware routes
            if flood_aware_routes and hazard_fc:
                try:
                    fa_scored_fc = score_routes(_to_routes_fc(flood_aware_routes), hazard_fc)
                    by_id = {f["properties"]["id"]: f["properties"] for f in fa_scored_fc.get("features", [])}
                    ranking_map = {r2["route_id"]: r2 for r2 in fa_scored_fc.get("ranking", [])}
                    for r in flood_aware_routes:
                        props = by_id.get(r["id"], {})
                        rank  = ranking_map.get(r["id"], {})
                        r["flood_exposure"] = {
                            "flooded_length_m":     props.get("flooded_length_m"),
                            "flooded_fraction":     props.get("flooded_fraction"),
                            "flood_status":         props.get("flood_status"),
                            "flood_risk_score":     props.get("flood_risk_score"),
                            "rank":                 rank.get("rank"),
                            "flooded_segments":     props.get("flooded_segments"),
                            "flood_crossing_points": props.get("flood_crossing_points"),
                        } if props else None
                except Exception as e:
                    print(f"[flood-aware-routes] FA scoring failed: {e}")

        except Exception as e:
            print(f"[flood-aware-routes] avoidance failed: {e}")

    return jsonify({
        "origin":      {"lat": origin_lat, "lon": origin_lon},
        "destination": {"lat": dest_lat,   "lon": dest_lon},
        "standard_routes":    scored_standard,
        "flood_aware_routes": flood_aware_routes,
        "hazard_metadata": hazard_meta,
        "note": (
            "standard_routes are the fastest OSRM candidates, scored for flood exposure. "
            "flood_aware_routes are re-routed alternatives that attempt to avoid the current "
            "HAND flood hazard zone. Compare travel_time and flood_status to choose."
        ),
    })


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("ARFA_PORT", "5050")))
    args = parser.parse_args()
    print(f"ARFA Hybrid backend → http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)