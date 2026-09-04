from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import geopandas as gpd
import numpy as np
from affine import Affine
from pyproj import CRS


@dataclass(slots=True)
class RasterLayer:
    data: np.ndarray
    transform: Affine
    crs: CRS
    nodata: float | int | None = None
    name: str = "raster"
    units: str | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return self.data.shape


@dataclass(slots=True)
class GaugeObservation:
    site_no: str
    name: str
    latitude: float
    longitude: float
    stage_ft: float | None = None
    observed_time: str | None = None
    flood_category: str = "unknown"
    noaa_lid: str | None = None
    noaa_reach_id: str | None = None
    source: str = "USGS + NOAA NWPS"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TerrainBundle:
    dem: RasterLayer
    hand: RasterLayer
    upstream_area: RasterLayer
    slope: RasterLayer
    drainage: RasterLayer
    raw_nhd: RasterLayer
    terrain_channels: RasterLayer
    flowlines: gpd.GeoDataFrame
    drainage_source: str
    alignment_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HazardResult:
    hazard_geojson: dict[str, Any]
    reference_river_geojson: dict[str, Any]
    gauges: list[GaugeObservation]
    selected_gauge: GaugeObservation | None
    flood_category: str
    hand_threshold_m: float
    dynamic: bool
    confidence: str
    warnings: list[str]
    metadata: dict[str, Any]
