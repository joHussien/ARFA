from __future__ import annotations

import math
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import requests
from pyproj import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.windows import Window, transform as window_transform
from rasterio.warp import Resampling, reproject
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .geo import local_projected_crs
from .models import RasterLayer

USGS_3DEP_IMAGE_SERVER = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer/exportImage"
)


def _session() -> requests.Session:
    """HTTP session with retries for transient National Map failures."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "ARFA-OASIS-flood-module/0.2"})
    return s


def _fetch_export(
    session: requests.Session,
    bbox: tuple[float, float, float, float],
    epsg: int,
    width_px: int,
    height_px: int,
    timeout: int = 120,
) -> tuple[np.ndarray, rasterio.Affine, CRS]:
    minx, miny, maxx, maxy = bbox
    params = {
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "bboxSR": str(epsg),
        "imageSR": str(epsg),
        "size": f"{width_px},{height_px}",
        "format": "tiff",
        "pixelType": "F32",
        "noData": "-9999",
        "returnSquarePixels": "true",
        "f": "image",
    }
    response = session.get(USGS_3DEP_IMAGE_SERVER, params=params, timeout=timeout)
    if response.status_code >= 400:
        body = response.text[:800] if response.text else ""
        raise requests.HTTPError(
            f"USGS 3DEP HTTP {response.status_code}: {body}", response=response
        )
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type or response.content[:1] in (b"{", b"["):
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:1000]
        raise RuntimeError(f"USGS 3DEP export failed: {detail}")

    with MemoryFile(response.content) as memfile:
        with memfile.open() as src:
            data = src.read(1).astype("float32")
            nodata = src.nodata
            if nodata is not None:
                data[data == nodata] = np.nan
            data[~np.isfinite(data)] = np.nan
            return data, src.transform, CRS.from_user_input(src.crs)


def _fetch_tiled(
    bbox: tuple[float, float, float, float],
    target_crs: CRS,
    epsg: int,
    width_px: int,
    height_px: int,
    tile_px: int = 768,
) -> tuple[np.ndarray, rasterio.Affine]:
    """Fetch a DEM as smaller exports and stitch them onto one exact grid."""
    minx, miny, maxx, maxy = bbox
    transform = from_bounds(minx, miny, maxx, maxy, width_px, height_px)
    out = np.full((height_px, width_px), np.nan, dtype="float32")
    session = _session()

    tiles_x = math.ceil(width_px / tile_px)
    tiles_y = math.ceil(height_px / tile_px)
    print(
        f"USGS 3DEP: downloading {tiles_x * tiles_y} terrain tiles "
        f"({tile_px}px max per tile)...",
        flush=True,
    )

    for y0 in range(0, height_px, tile_px):
        h = min(tile_px, height_px - y0)
        for x0 in range(0, width_px, tile_px):
            w = min(tile_px, width_px - x0)
            win = Window(x0, y0, w, h)
            dst_transform = window_transform(win, transform)

            left = dst_transform.c
            top = dst_transform.f
            right = left + dst_transform.a * w
            bottom = top + dst_transform.e * h
            tile_bbox = (float(left), float(bottom), float(right), float(top))

            src_data, src_transform, src_crs = _fetch_export(
                session, tile_bbox, epsg, w, h, timeout=120
            )
            tile = np.full((h, w), np.nan, dtype="float32")
            reproject(
                source=src_data,
                destination=tile,
                src_transform=src_transform,
                src_crs=src_crs,
                src_nodata=np.nan,
                dst_transform=dst_transform,
                dst_crs=target_crs,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            out[y0:y0 + h, x0:x0 + w] = tile
            time.sleep(0.15)

    return out, transform


def fetch_3dep_dem(
    aoi_wgs84: gpd.GeoDataFrame,
    resolution_m: float = 10.0,
    max_pixels: int = 6000,
) -> RasterLayer:
    """Download a projected USGS 3DEP bare-earth elevation raster.

    The National Map image service occasionally returns 502/504 responses for a
    single medium-sized export. We first try the efficient single request, then
    automatically fall back to smaller tiled exports with retries.
    """
    if resolution_m <= 0:
        raise ValueError("resolution_m must be > 0")
    target_crs = local_projected_crs(aoi_wgs84)
    epsg = target_crs.to_epsg()
    if epsg is None:
        raise ValueError("Could not choose an EPSG projected CRS for the AOI")
    projected = aoi_wgs84.to_crs(target_crs)
    minx, miny, maxx, maxy = map(float, projected.total_bounds)
    width_m, height_m = maxx - minx, maxy - miny
    min_res = max(width_m / max_pixels, height_m / max_pixels)
    actual_res = max(float(resolution_m), float(min_res))
    width_px = min(max_pixels, max(1, int(math.ceil(width_m / actual_res))))
    height_px = min(max_pixels, max(1, int(math.ceil(height_m / actual_res))))
    bbox = (minx, miny, maxx, maxy)

    try:
        data, transform, crs = _fetch_export(
            _session(), bbox, epsg, width_px, height_px, timeout=90
        )
    except (requests.RequestException, RuntimeError) as exc:
        print(
            "USGS 3DEP single-image request failed; "
            f"falling back to tiled download. Reason: {exc}",
            flush=True,
        )
        data, transform = _fetch_tiled(
            bbox=bbox,
            target_crs=target_crs,
            epsg=epsg,
            width_px=width_px,
            height_px=height_px,
            tile_px=768,
        )
        crs = target_crs

    if not np.isfinite(data).any():
        raise RuntimeError("USGS 3DEP returned no valid elevation cells")
    return RasterLayer(
        data=data,
        transform=transform,
        crs=crs,
        nodata=np.nan,
        name="usgs_3dep_dem",
        units="m",
    )


def write_raster(path: Path, layer: RasterLayer, dtype: str = "float32") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(layer.data)
    nodata = -9999.0 if np.issubdtype(np.dtype(dtype), np.floating) else 255
    out = np.where(np.isfinite(arr), arr, nodata).astype(dtype)
    with rasterio.open(
        path, "w", driver="GTiff", height=out.shape[0], width=out.shape[1], count=1,
        dtype=dtype, crs=layer.crs, transform=layer.transform, nodata=nodata,
        compress="deflate", tiled=True,
    ) as dst:
        dst.write(out, 1)


def read_raster(path: Path, name: str = "raster", units: str | None = None) -> RasterLayer:
    with rasterio.open(path) as src:
        data = src.read(1).astype("float32")
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        return RasterLayer(data=data, transform=src.transform, crs=CRS.from_user_input(src.crs),
                           nodata=np.nan, name=name, units=units)
