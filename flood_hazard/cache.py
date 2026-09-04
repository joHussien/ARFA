from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

import geopandas as gpd

from .config import FloodConfig
from .dem import read_raster, write_raster
from .models import RasterLayer, TerrainBundle


def terrain_cache_key(bbox: Iterable[float], config: FloodConfig) -> str:
    payload = {
        "bbox": [round(float(x), 5) for x in bbox],
        "resolution_m": config.dem_resolution_m,
        "context_buffer_m": config.context_buffer_m,
        "nhd_min_stream_order": config.nhd_min_stream_order,
        "dem_channel_threshold_km2": config.dem_channel_threshold_km2,
        "snap_distance_m": config.nhd_snap_distance_m,
        "version": 2,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def cache_folder(bbox: Iterable[float], config: FloodConfig) -> Path:
    return config.ensure_cache() / terrain_cache_key(bbox, config)


def save_terrain(folder: Path, bundle: TerrainBundle) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    write_raster(folder / "dem.tif", bundle.dem)
    write_raster(folder / "hand.tif", bundle.hand)
    write_raster(folder / "upstream_area.tif", bundle.upstream_area)
    write_raster(folder / "slope.tif", bundle.slope)
    write_raster(folder / "drainage.tif", bundle.drainage)
    write_raster(folder / "raw_nhd.tif", bundle.raw_nhd)
    write_raster(folder / "terrain_channels.tif", bundle.terrain_channels)
    if bundle.flowlines is not None and not bundle.flowlines.empty:
        bundle.flowlines.to_file(folder / "nhdplus_flowlines.geojson", driver="GeoJSON")
    (folder / "terrain_metadata.json").write_text(json.dumps({
        "drainage_source": bundle.drainage_source,
        "alignment_metrics": bundle.alignment_metrics,
    }, indent=2))


def load_terrain(folder: Path) -> TerrainBundle | None:
    required = ["dem.tif", "hand.tif", "upstream_area.tif", "slope.tif", "drainage.tif",
                "raw_nhd.tif", "terrain_channels.tif", "terrain_metadata.json"]
    if not all((folder / f).exists() for f in required):
        return None
    meta = json.loads((folder / "terrain_metadata.json").read_text())
    flow_path = folder / "nhdplus_flowlines.geojson"
    flowlines = gpd.read_file(flow_path) if flow_path.exists() else gpd.GeoDataFrame(geometry=[], crs=4326)
    return TerrainBundle(
        dem=read_raster(folder / "dem.tif", "dem", "m"),
        hand=read_raster(folder / "hand.tif", "hand", "m"),
        upstream_area=read_raster(folder / "upstream_area.tif", "upstream_area", "km2"),
        slope=read_raster(folder / "slope.tif", "slope", "rise/run"),
        drainage=read_raster(folder / "drainage.tif", "drainage", "boolean"),
        raw_nhd=read_raster(folder / "raw_nhd.tif", "raw_nhd", "boolean"),
        terrain_channels=read_raster(folder / "terrain_channels.tif", "terrain_channels", "boolean"),
        flowlines=flowlines,
        drainage_source=meta.get("drainage_source", "unknown"),
        alignment_metrics=meta.get("alignment_metrics", {}),
    )
