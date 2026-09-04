from __future__ import annotations

from typing import Any

import geopandas as gpd
import numpy as np
from rasterio.features import rasterize
from rasterio.transform import array_bounds
from shapely.geometry import box

from .models import RasterLayer


def _valid_mask(layer: RasterLayer) -> np.ndarray:
    valid = np.isfinite(layer.data)
    if layer.nodata is not None:
        try:
            if np.isfinite(layer.nodata):
                valid &= layer.data != layer.nodata
        except TypeError:
            pass
    return valid


def clip_hydrography_to_dem(hydrography: gpd.GeoDataFrame, dem: RasterLayer) -> gpd.GeoDataFrame:
    if hydrography is None or hydrography.empty:
        return hydrography
    rivers = hydrography.to_crs(dem.crs).copy()
    h, w = dem.shape
    left, bottom, right, top = array_bounds(h, w, dem.transform)
    footprint = box(left, bottom, right, top)
    rivers = rivers[rivers.geometry.notna() & ~rivers.geometry.is_empty]
    rivers = rivers[rivers.geometry.intersects(footprint)].copy()
    if rivers.empty:
        return rivers
    rivers["geometry"] = rivers.geometry.intersection(footprint)
    return rivers[rivers.geometry.notna() & ~rivers.geometry.is_empty].reset_index(drop=True)


def rasterize_centerlines(hydrography: gpd.GeoDataFrame, dem: RasterLayer) -> np.ndarray:
    valid = _valid_mask(dem)
    if hydrography is None or hydrography.empty:
        return np.zeros(dem.shape, dtype=bool)
    rivers = hydrography.to_crs(dem.crs)
    shapes = [(g, 1) for g in rivers.geometry if g is not None and not g.is_empty]
    if not shapes:
        return np.zeros(dem.shape, dtype=bool)
    return rasterize(shapes, out_shape=dem.shape, transform=dem.transform, fill=0,
                     default_value=1, dtype="uint8", all_touched=True).astype(bool) & valid


def snap_hydrography_to_flow(
    raw_nhd: np.ndarray,
    upstream_area: np.ndarray,
    dem: RasterLayer,
    channel_threshold_km2: float,
    snap_distance_m: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    from scipy.ndimage import distance_transform_edt

    valid = _valid_mask(dem)
    terrain_channel = valid & np.isfinite(upstream_area) & (upstream_area >= channel_threshold_km2)
    raw_nhd = np.asarray(raw_nhd, dtype=bool) & valid
    if dem.crs.is_geographic:
        raise ValueError("Hydrography snapping requires a projected DEM CRS")
    if not raw_nhd.any() or not terrain_channel.any():
        return np.zeros(dem.shape, bool), terrain_channel, {
            "median_distance_m": None,
            "within_20m_fraction": 0.0,
            "snapped_cell_count": 0,
        }
    xres, yres = abs(float(dem.transform.a)), abs(float(dem.transform.e))
    dist_to_channel = distance_transform_edt(~terrain_channel, sampling=(yres, xres))
    nhd_dist = dist_to_channel[raw_nhd]
    dist_to_nhd = distance_transform_edt(~raw_nhd, sampling=(yres, xres))
    snapped = terrain_channel & (dist_to_nhd <= snap_distance_m)
    finite = nhd_dist[np.isfinite(nhd_dist)]
    metrics = {
        "raw_nhd_cell_count": int(raw_nhd.sum()),
        "terrain_channel_cell_count": int(terrain_channel.sum()),
        "snapped_cell_count": int(snapped.sum()),
        "channel_threshold_km2": float(channel_threshold_km2),
        "snap_distance_m": float(snap_distance_m),
        "median_distance_m": float(np.median(finite)) if finite.size else None,
        "within_10m_fraction": float(np.mean(finite <= 10)) if finite.size else 0.0,
        "within_20m_fraction": float(np.mean(finite <= 20)) if finite.size else 0.0,
        "within_30m_fraction": float(np.mean(finite <= 30)) if finite.size else 0.0,
    }
    return snapped, terrain_channel, metrics


def derive_hydrology(
    dem: RasterLayer,
    hydrography: gpd.GeoDataFrame | None,
    fallback_drainage_threshold_km2: float = 2.0,
    channel_threshold_km2: float = 0.25,
    snap_distance_m: float = 20.0,
) -> dict[str, Any]:
    import pyflwdir

    elev = np.asarray(dem.data, dtype="float32").copy()
    valid = _valid_mask(dem)
    if not valid.any():
        raise ValueError("DEM has no valid cells")
    elev[~valid] = np.nan

    # Critical consistency rule: flow directions and HAND use the SAME
    # depression-filled elevation surface.
    filled, d8 = pyflwdir.dem.fill_depressions(elev, nodata=float("nan"), outlets="edge")
    filled = np.asarray(filled, dtype="float32")
    flw = pyflwdir.from_array(
        d8, ftype="d8", check_ftype=False, mask=valid,
        transform=dem.transform, latlon=dem.crs.is_geographic,
    )
    upstream = flw.upstream_area("km2").astype("float32")
    upstream[~valid] = np.nan

    raw_nhd = np.zeros(dem.shape, bool)
    terrain_channels = np.zeros(dem.shape, bool)
    drain = np.zeros(dem.shape, bool)
    metrics: dict[str, Any] = {}
    source = "derived_upstream_area_fallback"

    if hydrography is not None and not hydrography.empty:
        rivers = clip_hydrography_to_dem(hydrography, dem)
        raw_nhd = rasterize_centerlines(rivers, dem)
        if raw_nhd.any():
            drain, terrain_channels, metrics = snap_hydrography_to_flow(
                raw_nhd, upstream, dem,
                channel_threshold_km2=channel_threshold_km2,
                snap_distance_m=snap_distance_m,
            )
            if drain.any():
                source = "usgs_nhdplus_hr_snapped_to_dem_flow"

    if not drain.any():
        drain = valid & (upstream >= fallback_drainage_threshold_km2)
        if not drain.any():
            adaptive = float(np.nanpercentile(upstream[valid], 98.5))
            drain = valid & (upstream >= adaptive)

    hand = flw.hand(drain=drain, elevtn=filled).astype("float32")
    hand[~valid] = np.nan
    # Cells that never reach a selected mapped drainage are outside this HAND domain.
    hand[np.isfinite(hand) & (hand < -1e-3)] = np.nan
    hand[np.isfinite(hand) & (hand < 0)] = 0.0

    xres, yres = abs(float(dem.transform.a)), abs(float(dem.transform.e))
    fill_for_slope = filled.copy()
    median = float(np.nanmedian(fill_for_slope[valid]))
    fill_for_slope[~valid] = median
    gy, gx = np.gradient(fill_for_slope, yres, xres)
    slope = np.sqrt(gx * gx + gy * gy).astype("float32")
    slope[~valid] = np.nan

    return {
        "hand": hand,
        "upstream_area": upstream,
        "slope": slope,
        "drainage": drain.astype("float32"),
        "raw_nhd": raw_nhd.astype("float32"),
        "terrain_channels": terrain_channels.astype("float32"),
        "drainage_source": source,
        "alignment_metrics": metrics,
    }



def derive_gauge_specific_hand(
    dem: RasterLayer,
    upstream_area: np.ndarray,
    selected_flowlines: gpd.GeoDataFrame,
    channel_threshold_km2: float = 0.25,
    snap_distance_m: float = 20.0,
) -> dict[str, Any]:
    """Recompute HAND using only the river network represented by the selected gauge.

    The flow-direction surface is rebuilt from the same depression-filled DEM used by
    the baseline terrain stack.  The selected NHDPlus river is rasterized and snapped
    to DEM-derived high-flow cells; those snapped cells become the *only* drains for
    HAND. Cells that never reach this selected drainage remain outside the valid HAND
    domain (PyFlwDir returns a negative nodata sentinel, which we convert to NaN).
    """
    import pyflwdir

    if selected_flowlines is None or selected_flowlines.empty:
        raise ValueError("No gauge-linked NHDPlus flowlines were selected")

    elev = np.asarray(dem.data, dtype="float32").copy()
    valid = _valid_mask(dem)
    elev[~valid] = np.nan
    filled, d8 = pyflwdir.dem.fill_depressions(elev, nodata=float("nan"), outlets="edge")
    filled = np.asarray(filled, dtype="float32")
    flw = pyflwdir.from_array(
        d8, ftype="d8", check_ftype=False, mask=valid,
        transform=dem.transform, latlon=dem.crs.is_geographic,
    )

    selected = clip_hydrography_to_dem(selected_flowlines, dem)
    raw_selected = rasterize_centerlines(selected, dem)
    snapped, terrain_channels, alignment = snap_hydrography_to_flow(
        raw_selected,
        np.asarray(upstream_area, dtype="float32"),
        dem,
        channel_threshold_km2=channel_threshold_km2,
        snap_distance_m=snap_distance_m,
    )
    if not snapped.any():
        raise ValueError("Gauge-linked NHDPlus river could not be snapped to the DEM flow network")

    hand = flw.hand(drain=snapped, elevtn=filled).astype("float32")
    hand[~valid] = np.nan
    # PyFlwDir uses a negative sentinel (commonly -9999) for cells that do not
    # encounter the selected drain along their downstream flow path.
    hand[np.isfinite(hand) & (hand < -1e-3)] = np.nan
    hand[np.isfinite(hand) & (hand < 0)] = 0.0

    return {
        "hand": hand,
        "drainage": snapped.astype("float32"),
        "raw_selected_nhd": raw_selected.astype("float32"),
        "terrain_channels": terrain_channels.astype("float32"),
        "alignment_metrics": alignment,
        "valid_hand_cell_count": int(np.isfinite(hand).sum()),
    }
