from __future__ import annotations

from typing import Iterable

import geopandas as gpd
import numpy as np
from rasterio.features import shapes
from shapely.geometry import shape
from shapely.ops import unary_union

from .config import FloodConfig
from .geo import bbox_gdf
from .models import GaugeObservation, RasterLayer


def category_to_hand_threshold(category: str, config: FloodConfig) -> float:
    return float(config.hand_thresholds_m.get(category, config.static_fallback_hand_m))


def hand_mask(hand: RasterLayer, threshold_m: float) -> np.ndarray:
    if threshold_m <= 0:
        return np.zeros(hand.shape, dtype=bool)
    return np.isfinite(hand.data) & (hand.data >= 0) & (hand.data <= float(threshold_m))


def mask_to_geojson(mask: np.ndarray, reference: RasterLayer,
                    clip_bbox: Iterable[float] | None = None) -> dict:
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return {"type": "FeatureCollection", "features": []}
    geoms = []
    for geom, value in shapes(mask.astype("uint8"), mask=mask, transform=reference.transform):
        if int(value) == 1:
            geoms.append(shape(geom))
    if not geoms:
        return {"type": "FeatureCollection", "features": []}
    merged = unary_union(geoms)
    # Simplify in projected DEM units to keep route-intersection payloads small.
    pixel = max(abs(float(reference.transform.a)), abs(float(reference.transform.e)))
    merged = merged.simplify(max(1.0, 0.5 * pixel), preserve_topology=True)
    gdf = gpd.GeoDataFrame(geometry=[merged], crs=reference.crs)
    if clip_bbox is not None:
        clip = bbox_gdf(clip_bbox).to_crs(reference.crs)
        try:
            gdf = gpd.clip(gdf, clip)
        except Exception:
            gdf["geometry"] = gdf.geometry.intersection(clip.geometry.iloc[0])
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    return gdf.to_crs(4326).__geo_interface__


def select_dynamic_threshold(
    gauge: GaugeObservation | None,
    config: FloodConfig,
    force_category: str | None = None,
    allow_static_fallback: bool = True,
) -> tuple[str, float, bool, str, list[str]]:
    warnings: list[str] = []
    if force_category:
        category = force_category
        if category not in config.hand_thresholds_m:
            raise ValueError(f"Unknown forced flood category: {force_category}")
        warnings.append("Flood category is manually overridden for scenario testing.")
        return category, category_to_hand_threshold(category, config), True, "scenario", warnings

    if gauge is None:
        if not allow_static_fallback:
            return "unknown", 0.0, False, "none", ["No active gage-height gauge found in the AOI."]
        warnings.append("No active gauge found; using low-confidence static HAND fallback.")
        return "unknown", config.static_fallback_hand_m, False, "low", warnings

    category = gauge.flood_category
    if category == "no_flooding":
        return category, 0.0, True, "medium", warnings
    if category in {"action", "minor", "moderate", "major", "record"}:
        warnings.append(
            "Gauge stage is used only to determine NOAA flood category; it is NOT treated as flood depth. "
            "The category-to-HAND mapping is a screening heuristic that must be calibrated."
        )
        return category, category_to_hand_threshold(category, config), True, "medium", warnings

    if allow_static_fallback:
        warnings.append(
            "Gauge exists but NOAA flood category is unavailable; using low-confidence static HAND fallback."
        )
        return "unknown", config.static_fallback_hand_m, False, "low", warnings
    return "unknown", 0.0, False, "none", ["NOAA flood category unavailable."]
