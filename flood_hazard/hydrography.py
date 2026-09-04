from __future__ import annotations

import json

import geopandas as gpd
import requests
from shapely.geometry import Point

from .models import GaugeObservation

NHDPLUS_HR_FLOWLINE_LAYER = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3"
)


def fetch_nhdplus_flowlines(
    aoi_wgs84: gpd.GeoDataFrame,
    min_stream_order: int = 3,
    page_size: int = 2000,
) -> gpd.GeoDataFrame:
    if min_stream_order < 1:
        raise ValueError("min_stream_order must be >= 1")
    aoi = aoi_wgs84.to_crs(4326)
    minx, miny, maxx, maxy = map(float, aoi.total_bounds)
    endpoint = NHDPLUS_HR_FLOWLINE_LAYER + "/query"
    where = (
        "innetwork = 1 "
        f"AND streamorde >= {int(min_stream_order)} "
        "AND ftype IN (460, 558)"
    )
    fields = (
        "OBJECTID,permanent_identifier,gnis_name,reachcode,ftype,fcode,"
        "lengthkm,nhdplusid,streamleve,streamorde,mainpath,divergence,"
        "hydroseq,dnhydroseq,arbolatesu"
    )
    features: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": where,
            "geometry": json.dumps({
                "xmin": minx, "ymin": miny, "xmax": maxx, "ymax": maxy,
                "spatialReference": {"wkid": 4326},
            }),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": fields,
            "returnGeometry": "true",
            "orderByFields": "OBJECTID ASC",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "geojson",
        }
        response = requests.get(endpoint, params=params,
                                headers={"User-Agent": "ARFA-OASIS-flood-module/0.1"},
                                timeout=120)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"NHDPlus query failed: {payload['error']}")
        batch = payload.get("features", [])
        features.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)

    if not features:
        return gpd.GeoDataFrame(columns=["gnis_name", "streamorde", "geometry"], crs=4326)
    gdf = gpd.GeoDataFrame.from_features(features, crs=4326)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    try:
        gdf = gpd.clip(gdf, aoi[["geometry"]])
    except Exception:
        pass
    return gdf.reset_index(drop=True)



def _normalise_name(value: object) -> str:
    """Normalise a GNIS/gauge name for conservative river-name matching."""
    import re

    text = str(value or "").upper().strip()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return " ".join(text.split())


def _as_intish(value: object) -> int | None:
    try:
        if value is None:
            return None
        f = float(value)
        if not (f == f):
            return None
        return int(f)
    except (TypeError, ValueError, OverflowError):
        return None


def _trace_mainstem_indices(flowlines: gpd.GeoDataFrame, seed_idx: int) -> set[int]:
    """Trace one main upstream branch and the downstream chain using NHDPlus sequencing.

    NHDPlus HR's ``hydroseq`` / ``dnhydroseq`` fields are useful when a river name is
    absent or has short unnamed gaps.  At upstream junctions we intentionally keep
    only the most mainstem-like predecessor rather than every tributary.
    """
    if "hydroseq" not in flowlines.columns or "dnhydroseq" not in flowlines.columns:
        return {seed_idx}

    hydro_to_idx: dict[int, int] = {}
    dn_to_up: dict[int, list[int]] = {}
    for idx, row in flowlines.iterrows():
        h = _as_intish(row.get("hydroseq"))
        d = _as_intish(row.get("dnhydroseq"))
        if h is not None:
            hydro_to_idx[h] = int(idx)
        if d not in (None, 0):
            dn_to_up.setdefault(d, []).append(int(idx))

    selected = {int(seed_idx)}

    # Downstream is normally unambiguous.
    cur = int(seed_idx)
    for _ in range(len(flowlines) + 2):
        dn = _as_intish(flowlines.loc[cur].get("dnhydroseq"))
        nxt = hydro_to_idx.get(dn) if dn not in (None, 0) else None
        if nxt is None or nxt in selected:
            break
        selected.add(nxt)
        cur = nxt

    # Upstream can branch. Choose the predecessor most likely to be the main stem.
    seed_name = _normalise_name(flowlines.loc[seed_idx].get("gnis_name"))
    cur = int(seed_idx)
    for _ in range(len(flowlines) + 2):
        hydro = _as_intish(flowlines.loc[cur].get("hydroseq"))
        preds = dn_to_up.get(hydro, []) if hydro is not None else []
        preds = [i for i in preds if i not in selected]
        if not preds:
            break

        def score(i: int):
            row = flowlines.loc[i]
            same_name = int(bool(seed_name) and _normalise_name(row.get("gnis_name")) == seed_name)
            try:
                order = float(row.get("streamorde") or -1)
            except Exception:
                order = -1.0
            try:
                mainpath = float(row.get("mainpath") or 0)
            except Exception:
                mainpath = 0.0
            try:
                arbolate = float(row.get("arbolatesu") or 0)
            except Exception:
                arbolate = 0.0
            try:
                length = float(row.get("lengthkm") or 0)
            except Exception:
                length = 0.0
            return (same_name, order, mainpath, arbolate, length)

        nxt = max(preds, key=score)
        selected.add(nxt)
        cur = nxt

    return selected


def select_gauge_connected_flowlines(
    flowlines: gpd.GeoDataFrame,
    gauge: GaugeObservation,
    projected_crs,
    max_gauge_distance_m: float = 1500.0,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Select the NHDPlus main river represented by a gauge.

    Selection is intentionally automatic and conservative:
      1. Find NHDPlus lines near the gauge.
      2. Prefer a nearby line whose GNIS river name appears in the gauge name.
      3. Use that line as a seed.
      4. Keep same-named reaches and the NHDPlus mainstem sequence through the seed.

    This is a *river association* step, not a hydraulic model.  Its purpose is to
    prevent one gauge category from being applied to unrelated drainage networks in
    the same city/AOI.
    """
    empty = gpd.GeoDataFrame(geometry=[], crs=getattr(flowlines, "crs", 4326) or 4326)
    if flowlines is None or flowlines.empty:
        return empty, {"applied": False, "reason": "no_nhdplus_flowlines"}

    rivers = flowlines[flowlines.geometry.notna() & ~flowlines.geometry.is_empty].copy().reset_index(drop=True)
    if rivers.empty:
        return empty, {"applied": False, "reason": "no_valid_nhdplus_geometry"}

    projected = rivers.to_crs(projected_crs)
    gauge_pt = gpd.GeoSeries([Point(float(gauge.longitude), float(gauge.latitude))], crs=4326).to_crs(projected_crs).iloc[0]
    distances = projected.geometry.distance(gauge_pt)
    if distances.empty:
        return empty, {"applied": False, "reason": "could_not_measure_gauge_distance"}

    gauge_name = _normalise_name(gauge.name)
    nearby = [int(i) for i, d in distances.items() if float(d) <= float(max_gauge_distance_m)]
    candidates = nearby or [int(distances.idxmin())]

    # Prefer named reaches that are explicitly mentioned by the gauge name.
    name_matches = []
    for i in candidates:
        river_name = _normalise_name(rivers.loc[i].get("gnis_name"))
        if river_name and river_name in gauge_name:
            name_matches.append(i)
    seed_idx = min(name_matches, key=lambda i: float(distances.loc[i])) if name_matches else int(distances.idxmin())
    seed_distance = float(distances.loc[seed_idx])
    if seed_distance > float(max_gauge_distance_m):
        return empty, {
            "applied": False,
            "reason": "nearest_nhdplus_reach_too_far_from_gauge",
            "nearest_distance_m": seed_distance,
            "max_distance_m": float(max_gauge_distance_m),
        }

    seed_name_raw = rivers.loc[seed_idx].get("gnis_name")
    seed_name = _normalise_name(seed_name_raw)
    selected_idx: set[int] = _trace_mainstem_indices(rivers, seed_idx)
    method_parts = ["nhdplus_hydrosequence_mainstem"]

    # A stable GNIS name is strong evidence that multiple segmented reaches belong to
    # the same river. Add those segments within the already-bounded analysis AOI.
    if seed_name:
        same_name = {
            int(i) for i, value in rivers.get("gnis_name", []).items()
            if _normalise_name(value) == seed_name
        } if "gnis_name" in rivers.columns else set()
        if same_name:
            selected_idx |= same_name
            method_parts.insert(0, "gnis_name")

    selected = rivers.loc[sorted(selected_idx)].copy().reset_index(drop=True)
    selected_proj = selected.to_crs(projected_crs)
    length_km = float(selected_proj.length.sum() / 1000.0)

    metrics = {
        "applied": True,
        "selection_method": "+".join(method_parts),
        "gauge_site_no": gauge.site_no,
        "gauge_name": gauge.name,
        "seed_river_name": str(seed_name_raw or ""),
        "seed_distance_m": seed_distance,
        "selected_segment_count": int(len(selected)),
        "selected_river_length_km": length_km,
        "max_gauge_distance_m": float(max_gauge_distance_m),
    }
    return selected, metrics
