from __future__ import annotations

import math
from typing import Iterable

import geopandas as gpd
import requests
from pyproj import CRS
from shapely.geometry import box

from .config import FloodConfig


def validate_bbox(bbox: Iterable[float]) -> tuple[float, float, float, float]:
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ValueError("Invalid bbox. Expected [minLon, minLat, maxLon, maxLat].")
    if (max_lon - min_lon) > 1.5 or (max_lat - min_lat) > 1.5:
        raise ValueError("Flood DEM analysis is limited to <=1.5° x 1.5° per request.")
    return min_lon, min_lat, max_lon, max_lat


def bbox_gdf(bbox: Iterable[float]) -> gpd.GeoDataFrame:
    min_lon, min_lat, max_lon, max_lat = validate_bbox(bbox)
    return gpd.GeoDataFrame(geometry=[box(min_lon, min_lat, max_lon, max_lat)], crs=4326)


def geocode_place(place: str, config: FloodConfig) -> tuple[float, float, float, float]:
    """Resolve a place to an envelope for standalone testing.

    The team ARFA server can skip this and pass the map/county bbox directly.
    """
    if not place or not place.strip():
        raise ValueError("place cannot be empty")
    response = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place.strip(), "format": "jsonv2", "limit": 1, "countrycodes": "us"},
        headers={"User-Agent": config.geocode_user_agent},
        timeout=45,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise ValueError(f"Could not geocode place: {place}")
    south, north, west, east = map(float, rows[0]["boundingbox"])
    # Prevent huge state-sized requests in the standalone demo.
    if (east - west) > 1.5 or (north - south) > 1.5:
        lat = float(rows[0]["lat"])
        lon = float(rows[0]["lon"])
        pad = 0.20
        west, east, south, north = lon - pad, lon + pad, lat - pad, lat + pad
    return validate_bbox((west, south, east, north))


def local_projected_crs(aoi_wgs84: gpd.GeoDataFrame) -> CRS:
    estimated = aoi_wgs84.to_crs(4326).estimate_utm_crs()
    return CRS.from_user_input(estimated or CRS.from_epsg(3857))


def buffered_aoi(aoi_wgs84: gpd.GeoDataFrame, buffer_m: float) -> gpd.GeoDataFrame:
    if buffer_m <= 0:
        return aoi_wgs84.to_crs(4326)
    crs = local_projected_crs(aoi_wgs84)
    projected = aoi_wgs84.to_crs(crs)
    geom = projected.geometry.unary_union.buffer(float(buffer_m))
    return gpd.GeoDataFrame(geometry=[geom], crs=crs).to_crs(4326)


def bbox_center(bbox: Iterable[float]) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = validate_bbox(bbox)
    return (0.5 * (min_lat + max_lat), 0.5 * (min_lon + max_lon))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
