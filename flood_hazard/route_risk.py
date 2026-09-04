from __future__ import annotations

from typing import Any

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import shape, MultiLineString, LineString, GeometryCollection, mapping
from shapely.ops import unary_union


def _feature_geometries(fc: dict[str, Any]) -> list:
    return [shape(f["geometry"]) for f in fc.get("features", []) if f.get("geometry")]


def _extract_intersection_points(route_geom_4326, hazard_union_4326) -> list[dict]:
    """
    Return a list of GeoJSON Point features marking where the route
    enters and exits the flood zone (boundary crossings) and any
    short puncture points in between.

    Works in WGS-84 to keep coordinates directly renderable.
    """
    if hazard_union_4326 is None or route_geom_4326 is None:
        return []
    try:
        boundary = hazard_union_4326.boundary
        crossings = route_geom_4326.intersection(boundary)
        points = []
        def _collect(geom):
            from shapely.geometry import Point, MultiPoint
            if geom.is_empty:
                return
            if geom.geom_type == "Point":
                points.append({"type": "Feature",
                               "geometry": {"type": "Point", "coordinates": [round(geom.x, 6), round(geom.y, 6)]},
                               "properties": {"marker": "flood_crossing"}})
            elif geom.geom_type == "MultiPoint":
                for p in geom.geoms:
                    _collect(p)
            elif geom.geom_type in ("GeometryCollection", "MultiLineString", "LineString"):
                # Boundary overlaps (route runs *along* flood edge) — take midpoint
                try:
                    mid = geom.interpolate(0.5, normalized=True)
                    _collect(mid)
                except Exception:
                    pass
        _collect(crossings)
        return points
    except Exception:
        return []


def _flooded_segments_geojson(route_geom_4326, hazard_union_4326) -> dict | None:
    """
    Return a GeoJSON MultiLineString of the route portions that lie
    inside the flood zone, in WGS-84.  Returns None if no overlap.
    """
    if hazard_union_4326 is None:
        return None
    try:
        overlap = route_geom_4326.intersection(hazard_union_4326)
        if overlap.is_empty:
            return None
        return mapping(overlap)
    except Exception:
        return None


def score_routes(routes_fc: dict[str, Any], hazard_fc: dict[str, Any]) -> dict[str, Any]:
    route_features = routes_fc.get("features", [])
    if not route_features:
        return {"type": "FeatureCollection", "features": [], "ranking": []}
    route_geoms = _feature_geometries(routes_fc)
    if not route_geoms:
        return {"type": "FeatureCollection", "features": [], "ranking": []}

    # Pick a local projected CRS from route geometry for meter-based lengths.
    rgdf = gpd.GeoDataFrame(geometry=route_geoms, crs=4326)
    projected_crs = rgdf.estimate_utm_crs() or CRS.from_epsg(3857)
    rgdf = rgdf.to_crs(projected_crs)

    hazard_geoms = _feature_geometries(hazard_fc)
    hazard_union_proj = None
    hazard_union_4326 = None
    if hazard_geoms:
        raw_union = unary_union(hazard_geoms)
        hgdf = gpd.GeoDataFrame(geometry=[raw_union], crs=4326).to_crs(projected_crs)
        hazard_union_proj = hgdf.geometry.iloc[0]
        hazard_union_4326 = raw_union  # keep WGS-84 copy for point/segment extraction

    out_features = []
    ranking = []
    for i, (feature, geom_m) in enumerate(zip(route_features, rgdf.geometry)):
        props = dict(feature.get("properties") or {})
        route_id = str(props.get("id", props.get("route_id", i)))
        total_m = float(geom_m.length)
        flooded_m = float(geom_m.intersection(hazard_union_proj).length) if hazard_union_proj is not None else 0.0
        fraction = flooded_m / total_m if total_m > 0 else 0.0
        # For evacuation, any detected flood overlap is treated conservatively.
        if flooded_m <= 1.0:
            status, score = "safe", 0.0
        elif flooded_m <= 50.0 and fraction <= 0.02:
            status, score = "caution", min(0.5, 0.2 + fraction * 10)
        else:
            status, score = "avoid", min(1.0, 0.5 + fraction * 5)

        # --- Flood intersection geometry (WGS-84, for frontend rendering) ---
        original_geom_4326 = shape(feature["geometry"])
        crossing_points = _extract_intersection_points(original_geom_4326, hazard_union_4326)
        flooded_segments = _flooded_segments_geojson(original_geom_4326, hazard_union_4326)

        props.update({
            "flooded_length_m": round(flooded_m, 2),
            "route_length_m": round(total_m, 2),
            "flooded_fraction": round(fraction, 6),
            "flood_risk_score": round(score, 4),
            "flood_status": status,
            # New: geometry of where the route overlaps the flood zone
            "flooded_segments": flooded_segments,
            # New: entry/exit crossing points as a GeoJSON FeatureCollection
            "flood_crossing_points": {"type": "FeatureCollection", "features": crossing_points},
        })
        out_features.append({"type": "Feature", "geometry": feature.get("geometry"), "properties": props})
        ranking.append({"route_id": route_id, "status": status, "flooded_length_m": flooded_m,
                        "flooded_fraction": fraction, "risk_score": score,
                        "travel_time_min": props.get("travel_time_min"),
                        "distance_km": props.get("distance_km")})

    ranking.sort(key=lambda x: (x["flooded_length_m"], x["risk_score"],
                                x["travel_time_min"] if x["travel_time_min"] is not None else 1e12,
                                x["distance_km"] if x["distance_km"] is not None else 1e12))
    for rank, item in enumerate(ranking, 1):
        item["rank"] = rank
    return {"type": "FeatureCollection", "features": out_features, "ranking": ranking}
