from __future__ import annotations

from typing import Iterable

from .geo import bbox_center, haversine_km, validate_bbox
from .models import GaugeObservation

# ── Severity helpers ──────────────────────────────────────────────────────────

SEVERITY_RANK = {
    "unknown": -1, "no_flooding": 0,
    "action": 1, "minor": 2, "moderate": 3, "major": 4, "record": 5,
}


def canonical_category(value: str | None) -> str:
    s = (value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "none": "no_flooding", "normal": "no_flooding", "not_defined": "unknown",
        "no_flood": "no_flooding", "noflooding": "no_flooding",
        "action_stage": "action", "minor_flooding": "minor",
        "moderate_flooding": "moderate", "major_flooding": "major",
        "record_flooding": "record",
    }
    return aliases.get(s, s if s in {"no_flooding", "action", "minor", "moderate", "major", "record"} else "unknown")


def public_gauge_dict(gauge: GaugeObservation) -> dict:
    """Small JSON-safe gauge payload; intentionally omits bulky raw API responses."""
    return {
        "site_no":       gauge.site_no,
        "name":          gauge.name,
        "latitude":      gauge.latitude,
        "longitude":     gauge.longitude,
        "stage_ft":      gauge.stage_ft,
        "observed_time": gauge.observed_time,
        "flood_category": gauge.flood_category,
        "noaa_lid":      gauge.noaa_lid,
        "noaa_reach_id": gauge.noaa_reach_id,
        "source":        gauge.source,
    }


def choose_primary_gauge(
    gauges: list[GaugeObservation], bbox: Iterable[float]
) -> GaugeObservation | None:
    """Pick the most flood-relevant gauge closest to the AOI centre."""
    if not gauges:
        return None
    center_lat, center_lon = bbox_center(bbox)
    return sorted(
        gauges,
        key=lambda g: (
            -SEVERITY_RANK.get(g.flood_category, -1),
            haversine_km(center_lat, center_lon, g.latitude, g.longitude),
        ),
    )[0]


# ── Converter: ARFA gauge dict → GaugeObservation ────────────────────────────
# ARFA's /api/gauges endpoint already fetches USGS + NOAA data for every gauge
# in the resolved county.  The flood service reuses those results so we never
# make a second identical round-trip to the same APIs.

def arfa_gauge_to_observation(g: dict) -> GaugeObservation | None:
    """
    Convert an ARFA gauge dict (from /api/gauges or /api/gauges/bbox) to a
    GaugeObservation that FloodHazardService can use for primary-gauge selection
    and HAND threshold determination.

    ARFA gauge keys used:
      lid / _stage / _sev / _d (NOAA metadata dict) / latitude / longitude / name
    """
    try:
        lat = float(g.get("latitude") or 0)
        lon = float(g.get("longitude") or 0)
    except (TypeError, ValueError):
        return None
    if lat == 0 and lon == 0:
        return None

    site_no = str(g.get("lid") or g.get("site_no") or "").strip()
    if not site_no:
        return None

    stage = g.get("_stage")
    try:
        stage = float(stage) if stage is not None else None
    except (TypeError, ValueError):
        stage = None

    # _sev is already a canonical category from ARFA's enrichGauges() pipeline.
    # Map ARFA severity names (which include no_flooding/action/minor/moderate/major)
    # directly. ARFA-specific arfa_* prefixes fall back to unknown.
    raw_sev = str(g.get("_sev") or "unknown")
    flood_category = canonical_category(raw_sev)

    # Grab NOAA lid / reach id if the full NOAA metadata dict was stored on the gauge
    noaa_meta = g.get("_d") or {}
    noaa_lid = noaa_meta.get("lid") or g.get("noaa_lid")
    noaa_reach_id = str(noaa_meta.get("reachId")) if noaa_meta.get("reachId") is not None else None

    return GaugeObservation(
        site_no=site_no,
        name=str(g.get("name") or site_no),
        latitude=lat,
        longitude=lon,
        stage_ft=stage,
        observed_time=None,
        flood_category=flood_category,
        noaa_lid=noaa_lid,
        noaa_reach_id=noaa_reach_id,
        source="ARFA USGS+NOAA pipeline",
    )


def arfa_gauges_to_observations(
    gauges: list[dict],
) -> list[GaugeObservation]:
    """Batch-convert ARFA gauge dicts, silently dropping unconvertible entries."""
    obs = [arfa_gauge_to_observation(g) for g in (gauges or [])]
    return [o for o in obs if o is not None]


# ── Fallback: direct USGS+NOAA fetch (standalone / test use only) ─────────────
# In the integrated ARFA system, FloodHazardService.get_gauges() always receives
# pre-fetched gauges from the main pipeline, so this path is never called in
# normal operation. It exists only for standalone testing and the /api/flood/gauges
# endpoint which is called before any county data is loaded.

import requests

USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
NOAA_NWPS_GAUGE_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{site_no}"


def _latest_valid(values_block: list[dict]) -> tuple[float | None, str | None]:
    candidates = []
    for block in values_block or []:
        for item in block.get("value", []) or []:
            try:
                val = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            if val <= -900:
                continue
            candidates.append((item.get("dateTime") or "", val))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    dt, value = candidates[-1]
    return value, dt or None


def _enrich_with_noaa(gauge: GaugeObservation) -> GaugeObservation:
    try:
        r = requests.get(
            NOAA_NWPS_GAUGE_URL.format(site_no=gauge.site_no), timeout=30,
            headers={"User-Agent": "ARFA-OASIS-flood-module/0.1"},
        )
        if r.status_code == 404:
            return gauge
        r.raise_for_status()
        meta = r.json()
    except Exception:
        return gauge
    status = meta.get("status", {}).get("observed", {}) or {}
    stage = gauge.stage_ft
    if status.get("primary") is not None and str(status.get("primaryUnit", "")).lower() in {"ft", "feet"}:
        try:
            stage = float(status["primary"])
        except (TypeError, ValueError):
            pass
    gauge.stage_ft = stage
    gauge.observed_time = status.get("validTime") or gauge.observed_time
    gauge.flood_category = canonical_category(status.get("floodCategory"))
    gauge.noaa_lid = meta.get("lid")
    gauge.noaa_reach_id = str(meta.get("reachId")) if meta.get("reachId") is not None else None
    return gauge


def _fetch_gauges_bbox_fallback(
    bbox: Iterable[float], max_enrich: int = 12
) -> list[GaugeObservation]:
    """Direct USGS+NOAA fetch used only for standalone testing / /api/flood/gauges."""
    min_lon, min_lat, max_lon, max_lat = validate_bbox(bbox)
    params = {
        "format": "json",
        "bBox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "parameterCd": "00065",
        "siteStatus": "active",
        "period": "P1D",
    }
    response = requests.get(
        USGS_IV_URL, params=params,
        headers={"User-Agent": "ARFA-OASIS-flood-module/0.1"}, timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    gauges: dict[str, GaugeObservation] = {}
    for ts in payload.get("value", {}).get("timeSeries", []) or []:
        info = ts.get("sourceInfo", {})
        codes = info.get("siteCode", []) or []
        if not codes:
            continue
        site_no = str(codes[0].get("value", "")).strip()
        if not site_no:
            continue
        loc = info.get("geoLocation", {}).get("geogLocation", {})
        try:
            lat, lon = float(loc.get("latitude")), float(loc.get("longitude"))
        except (TypeError, ValueError):
            continue
        stage, observed = _latest_valid(ts.get("values", []))
        gauges[site_no] = GaugeObservation(
            site_no=site_no, name=info.get("siteName") or site_no,
            latitude=lat, longitude=lon, stage_ft=stage,
            observed_time=observed, flood_category="unknown",
        )
    result = list(gauges.values())
    center_lat, center_lon = bbox_center(bbox)
    result.sort(key=lambda g: haversine_km(center_lat, center_lon, g.latitude, g.longitude))
    for g in result[:max_enrich]:
        _enrich_with_noaa(g)
    return result
