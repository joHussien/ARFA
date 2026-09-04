from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

from .config import FloodConfig
from .geo import bbox_center, local_projected_crs, validate_bbox

# Keep only fields useful for exposure summaries / map popups.  USA Structures
# deliveries are not perfectly schema-identical across states, so all lookups
# below are case-insensitive.
STRUCTURE_FIELDS = [
    "BUILD_ID", "OCC_CLS", "PRIM_OCC", "PROP_ADDR", "PROP_CITY", "PROP_ST",
    "PROP_ZIP", "PROP_CNTY", "HEIGHT", "SQFEET", "SQMETERS", "POP_MEDIAN",
    "B_CODE", "FIPS", "CENSUSCODE", "LONGITUDE", "LATITUDE", "SOURCE", "PROD_DATE",
]

STATE_NAME_TO_CODE = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
    "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
    "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
    "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
    "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
    "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
    "district of columbia":"DC",
}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def structures_data_dir(config: FloodConfig | None = None) -> Path:
    config = config or FloodConfig()
    raw = os.getenv("ARFA_STRUCTURES_DATA_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    # Keep the potentially large state data beside the flood cache, not in $HOME.
    return (config.cache_dir.parent / "usa_structures").resolve()


def resolve_state_code(bbox: Iterable[float], config: FloodConfig) -> str:
    """Resolve the state containing the AOI center.

    Evacuation routes are local, so a center-point lookup is sufficient for the
    current ARFA use case.  If a future route crosses a state border, the
    structures provider should be extended to query both intersecting states.
    """
    bbox = validate_bbox(bbox)
    lat, lon = bbox_center(bbox)
    r = requests.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "zoom": 5,
            "addressdetails": 1,
        },
        headers={"User-Agent": config.geocode_user_agent},
        timeout=45,
    )
    r.raise_for_status()
    address = (r.json() or {}).get("address") or {}
    iso = address.get("ISO3166-2-lvl4") or address.get("ISO3166-2-lvl3")
    if isinstance(iso, str) and iso.upper().startswith("US-"):
        return iso.split("-", 1)[1].upper()
    state = str(address.get("state") or "").strip().lower()
    code = STATE_NAME_TO_CODE.get(state)
    if not code:
        raise ValueError(f"Could not resolve US state for structures query: {address.get('state')!r}")
    return code


def _find_state_gdb(data_dir: Path, state_code: str) -> Path | None:
    state_dir = data_dir / state_code.upper()
    preferred = state_dir / f"{state_code.upper()}_Structures.gdb"
    if preferred.exists():
        return preferred
    if state_dir.exists():
        matches = list(state_dir.glob("*_Structures.gdb"))
        if matches:
            return matches[0]
    return None


def ensure_state_structures(
    state_code: str,
    config: FloodConfig,
    *,
    auto_download: bool = False,
) -> tuple[Path | None, list[str]]:
    """Return a state FileGDB, optionally downloading it on demand."""
    warnings: list[str] = []
    data_dir = structures_data_dir(config)
    data_dir.mkdir(parents=True, exist_ok=True)
    state_code = state_code.upper()

    existing = _find_state_gdb(data_dir, state_code)
    if existing is not None:
        return existing, warnings

    auto_download = auto_download or _bool_env("ARFA_STRUCTURES_AUTO_DOWNLOAD", False)
    if not auto_download:
        warnings.append(
            f"USA Structures data for {state_code} is not installed. "
            "Enable auto-download for this request or set ARFA_STRUCTURES_AUTO_DOWNLOAD=1."
        )
        return None, warnings

    # Reuse the downloader already shipped in ARFA.  Only the one required
    # state is downloaded; the nationwide dataset is never fetched here.
    try:
        from download_usa_structures import (
            USER_AGENT,
            discover_packages,
            process_package,
        )
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        packages = discover_packages(session)
        package = next((p for p in packages if p.get("code") == state_code), None)
        if package is None:
            raise RuntimeError(f"No USA Structures package advertised for {state_code}")
        process_package(
            session,
            package,
            data_dir,
            force=False,
            delete_zips=True,
        )
    except Exception as exc:
        warnings.append(f"Could not automatically acquire USA Structures {state_code}: {exc}")
        return None, warnings

    gdb = _find_state_gdb(data_dir, state_code)
    if gdb is None:
        warnings.append(f"USA Structures download finished but no {state_code} FileGDB was found.")
    return gdb, warnings


def query_structures_gdb(gdb_path: Path, bbox: Iterable[float], max_candidates: int) -> tuple[gpd.GeoDataFrame, bool]:
    """Read structures inside bbox using the FileGDB spatial index."""
    bbox = validate_bbox(bbox)
    gdf = gpd.read_file(str(gdb_path), bbox=tuple(bbox))
    if gdf.empty:
        return gdf.to_crs(4326) if gdf.crs else gdf.set_crs(4326), False
    if gdf.crs is None:
        # USA Structures is normally geographic; fail safe rather than silently
        # reprojecting an unknown CRS.
        gdf = gdf.set_crs(4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    actual = {str(c).upper(): c for c in gdf.columns if c != "geometry"}
    cols = []
    rename = {}
    for canonical in STRUCTURE_FIELDS:
        src = actual.get(canonical.upper())
        if src is not None:
            cols.append(src)
            rename[src] = canonical
    cols.append("geometry")
    gdf = gdf[cols].rename(columns=rename)

    capped = len(gdf) > max_candidates
    if capped:
        gdf = gdf.iloc[:max_candidates].copy()
    return gdf, capped


def _hazard_gdf(hazard_geojson: dict) -> gpd.GeoDataFrame:
    features = (hazard_geojson or {}).get("features") or []
    geoms = []
    for feature in features:
        geom = (feature or {}).get("geometry")
        if geom:
            try:
                geoms.append(shape(geom))
            except Exception:
                pass
    return gpd.GeoDataFrame(geometry=geoms, crs=4326)


def score_structure_exposure(
    structures: gpd.GeoDataFrame,
    hazard_geojson: dict,
    *,
    max_returned: int = 20000,
) -> tuple[dict, dict]:
    """Intersect building footprints with the binary HAND screening hazard.

    A building is *screened exposed* if its footprint intersects the hazard.
    This is deliberately not labelled a damage probability or flood depth.
    """
    empty_fc = {"type": "FeatureCollection", "features": []}
    hazard = _hazard_gdf(hazard_geojson)
    if structures is None or structures.empty or hazard.empty:
        summary = {
            "screened_exposed_buildings": 0,
            "candidate_buildings_examined": 0 if structures is None else int(len(structures)),
            "returned": 0,
            "estimated_population_sum": 0.0,
            "primary_use_counts": {},
            "criterion": "building footprint intersects HAND screening hazard",
        }
        return empty_fc, summary

    structures = structures.to_crs(4326).copy()
    # Repair occasional invalid polygons defensively.
    structures = structures[structures.geometry.notna() & ~structures.geometry.is_empty].copy()
    if structures.empty:
        return empty_fc, {
            "screened_exposed_buildings": 0,
            "candidate_buildings_examined": 0,
            "returned": 0,
            "estimated_population_sum": 0.0,
            "primary_use_counts": {},
            "criterion": "building footprint intersects HAND screening hazard",
        }

    projected_crs = local_projected_crs(hazard)
    b = structures.to_crs(projected_crs)
    h = hazard.to_crs(projected_crs)
    hazard_union = h.geometry.union_all()

    # Fast vectorized candidate filter first.
    hit = b.geometry.intersects(hazard_union)
    exposed = b.loc[hit].copy()
    if exposed.empty:
        return empty_fc, {
            "screened_exposed_buildings": 0,
            "candidate_buildings_examined": int(len(structures)),
            "returned": 0,
            "estimated_population_sum": 0.0,
            "primary_use_counts": {},
            "criterion": "building footprint intersects HAND screening hazard",
        }

    footprint_area = exposed.geometry.area
    overlap_area = exposed.geometry.intersection(hazard_union).area
    # Avoid the removed pandas option ``mode.use_inf_as_na`` (pandas >= 2.x).
    # Zero-area/invalid footprints become NaN through the masked denominator and
    # are converted to zero overlap explicitly.
    denom = footprint_area.where(footprint_area > 0)
    frac = (
        (overlap_area / denom)
        .replace([float("inf"), float("-inf")], 0.0)
        .fillna(0.0)
        .clip(0, 1)
    )
    exposed["HAZARD_OVERLAP_M2"] = overlap_area.round(2)
    exposed["HAZARD_OVERLAP_FRACTION"] = frac.round(4)
    exposed["CENTROID_IN_HAZARD"] = exposed.geometry.centroid.within(hazard_union)
    exposed["EXPOSURE_CLASS"] = [
        "inside_screening_hazard" if c or f >= 0.5 else "partial_intersection"
        for c, f in zip(exposed["CENTROID_IN_HAZARD"], exposed["HAZARD_OVERLAP_FRACTION"])
    ]

    total_exposed = int(len(exposed))
    returned = exposed.iloc[:max_returned].copy()
    returned_wgs = returned.to_crs(4326)

    # GeoPandas handles numpy / missing scalars cleanly through to_json.
    fc = returned_wgs.__geo_interface__

    pop_sum = 0.0
    if "POP_MEDIAN" in exposed.columns:
        pop_sum = float(pd.to_numeric(exposed["POP_MEDIAN"], errors="coerce").fillna(0).sum())
    use_counts = {}
    if "PRIM_OCC" in exposed.columns:
        vals = exposed["PRIM_OCC"].fillna("Unknown").astype(str)
        use_counts = dict(Counter(vals).most_common(15))

    summary = {
        "screened_exposed_buildings": total_exposed,
        "candidate_buildings_examined": int(len(structures)),
        "returned": int(len(returned)),
        "capped": total_exposed > max_returned,
        "estimated_population_sum": round(pop_sum, 1),
        "primary_use_counts": use_counts,
        "criterion": "building footprint intersects HAND screening hazard",
        "interpretation": (
            "These structures intersect a flood-screening polygon. This is exposure screening, "
            "not a prediction of building damage or indoor flood depth."
        ),
    }
    return fc, summary


def assess_structure_exposure(
    bbox: Iterable[float],
    hazard_geojson: dict,
    config: FloodConfig,
    *,
    auto_download: bool = False,
    max_candidates: int = 100000,
    max_returned: int = 20000,
) -> dict:
    bbox = validate_bbox(bbox)
    warnings: list[str] = []

    # If there is no active/scenario hazard, do not download a large building
    # dataset unnecessarily.
    if not (hazard_geojson or {}).get("features"):
        return {
            "structures": {"type": "FeatureCollection", "features": []},
            "summary": {
                "screened_exposed_buildings": 0,
                "candidate_buildings_examined": 0,
                "returned": 0,
                "estimated_population_sum": 0.0,
                "primary_use_counts": {},
                "criterion": "building footprint intersects HAND screening hazard",
            },
            "state_code": None,
            "data_source": "FEMA/ORNL USA Structures",
            "warnings": ["No flood-screening polygon exists, so no structures are flagged."],
        }

    state_code = resolve_state_code(bbox, config)
    gdb, acquire_warnings = ensure_state_structures(
        state_code,
        config,
        auto_download=auto_download,
    )
    warnings.extend(acquire_warnings)
    if gdb is None:
        return {
            "structures": {"type": "FeatureCollection", "features": []},
            "summary": {
                "screened_exposed_buildings": 0,
                "candidate_buildings_examined": 0,
                "returned": 0,
                "estimated_population_sum": 0.0,
                "primary_use_counts": {},
                "criterion": "building footprint intersects HAND screening hazard",
            },
            "state_code": state_code,
            "data_source": "FEMA/ORNL USA Structures",
            "data_path": None,
            "warnings": warnings,
        }

    candidates, candidate_cap = query_structures_gdb(gdb, bbox, max_candidates=max_candidates)
    fc, summary = score_structure_exposure(candidates, hazard_geojson, max_returned=max_returned)
    if candidate_cap:
        warnings.append(
            f"Structure candidate query was capped at {max_candidates:,}; exposure counts may be incomplete."
        )
    return {
        "structures": fc,
        "summary": summary,
        "state_code": state_code,
        "data_source": "FEMA/ORNL USA Structures",
        "data_path": str(gdb),
        "warnings": warnings,
    }