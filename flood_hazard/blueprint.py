from __future__ import annotations

from flask import Blueprint, jsonify, request

from .gauges import canonical_category, public_gauge_dict
from .exposure import assess_structure_exposure
from .route_risk import score_routes
from .service import FloodHazardService

flood_bp = Blueprint("flood_hazard", __name__)
_service = FloodHazardService()


def _bbox_from_request():
    vals = [request.args.get("minLon"), request.args.get("minLat"),
            request.args.get("maxLon"), request.args.get("maxLat")]
    if all(v is not None for v in vals):
        return tuple(map(float, vals))
    return None


@flood_bp.get("/api/flood/health")
def health():
    return jsonify({"ok": True, "module": "ARFA gauge+DEM/HAND flood + structure exposure screening", "version": "0.3"})


@flood_bp.get("/api/flood/gauges")
def gauges():
    try:
        bbox = _service.resolve_bbox(_bbox_from_request(), request.args.get("place"))
        rows = [public_gauge_dict(g) for g in _service.get_gauges(bbox)]
        return jsonify({"bbox": list(bbox), "gauges": rows})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@flood_bp.get("/api/flood/hazard")
def hazard():
    try:
        bbox = _bbox_from_request()
        force = request.args.get("force_category")
        if force:
            force = canonical_category(force)
        refresh = request.args.get("refresh", "0").lower() in {"1", "true", "yes"}
        fallback = request.args.get("static_fallback", "1").lower() not in {"0", "false", "no"}
        # Accept pre-fetched ARFA gauges from caller to avoid duplicate USGS/NOAA round-trip.
        # Frontend sends these as JSON body; GET with body is non-standard but Flask handles it.
        body = request.get_json(silent=True, force=True) or {}
        arfa_gauges = body.get("gauges") or None
        result = _service.get_hazard(
            bbox=bbox, place=request.args.get("place"), refresh=refresh,
            force_category=force, allow_static_fallback=fallback,
            arfa_gauges=arfa_gauges,
        )
        payload = dict(result.hazard_geojson)
        payload["metadata"] = result.metadata
        payload["reference_river"] = result.reference_river_geojson
        payload["warnings"] = result.warnings
        payload["gauges"] = [public_gauge_dict(g) for g in result.gauges]

        include_structures = request.args.get("include_structures", "0").lower() in {"1", "true", "yes"}
        if include_structures:
            auto_download = request.args.get("auto_download_structures", "0").lower() in {"1", "true", "yes"}
            exposure = assess_structure_exposure(
                result.metadata["bbox_wgs84"],
                result.hazard_geojson,
                _service.config,
                auto_download=auto_download,
            )
            payload["exposure"] = exposure
            payload["warnings"] = payload["warnings"] + exposure.get("warnings", [])
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@flood_bp.post("/api/flood/score-routes")
def route_scores():
    try:
        body = request.get_json(force=True) or {}
        routes = body.get("routes")
        if not routes:
            raise ValueError("JSON body must contain routes as a GeoJSON FeatureCollection")
        hazard_fc = body.get("hazard")
        hazard_meta = None
        if hazard_fc is None:
            bbox = body.get("bbox")
            place = body.get("place")
            if bbox is None and not place:
                # Fully automatic branch integration: derive the flood-analysis AOI
                # from Youssef's candidate route geometries when the caller does not
                # explicitly provide a map bbox.
                coords = []
                def collect(obj):
                    if isinstance(obj, (list, tuple)) and len(obj) >= 2 and all(isinstance(v, (int, float)) for v in obj[:2]):
                        coords.append((float(obj[0]), float(obj[1])))
                    elif isinstance(obj, (list, tuple)):
                        for child in obj:
                            collect(child)
                for feature in routes.get("features", []):
                    collect((feature.get("geometry") or {}).get("coordinates", []))
                if not coords:
                    raise ValueError("Could not derive bbox from route geometries")
                xs = [c[0] for c in coords]; ys = [c[1] for c in coords]
                pad = 0.03
                bbox = [min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad]
            force = body.get("force_category")
            if force:
                force = canonical_category(force)
            arfa_gauges = body.get("gauges") or None
            hz = _service.get_hazard(bbox=bbox, place=place, force_category=force,
                                     arfa_gauges=arfa_gauges)
            hazard_fc = hz.hazard_geojson
            hazard_meta = hz.metadata
        scored = score_routes(routes, hazard_fc)
        scored["hazard_metadata"] = hazard_meta
        return jsonify(scored)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@flood_bp.get("/api/flood/exposure")
def structure_exposure():
    """Return the flood-screening hazard plus USA Structures footprints that intersect it."""
    try:
        bbox = _bbox_from_request()
        force = request.args.get("force_category")
        if force:
            force = canonical_category(force)
        refresh = request.args.get("refresh", "0").lower() in {"1", "true", "yes"}
        fallback = request.args.get("static_fallback", "1").lower() not in {"0", "false", "no"}
        auto_download = request.args.get("auto_download_structures", "0").lower() in {"1", "true", "yes"}
        result = _service.get_hazard(
            bbox=bbox, place=request.args.get("place"), refresh=refresh,
            force_category=force, allow_static_fallback=fallback,
        )
        exposure = assess_structure_exposure(
            result.metadata["bbox_wgs84"],
            result.hazard_geojson,
            _service.config,
            auto_download=auto_download,
        )
        return jsonify({
            "hazard": result.hazard_geojson,
            "reference_river": result.reference_river_geojson,
            "metadata": result.metadata,
            "exposure": exposure,
            "warnings": result.warnings + exposure.get("warnings", []),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
