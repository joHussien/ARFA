from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_cache_dir() -> Path:
    here = Path(__file__).resolve()
    return Path(os.getenv("ARFA_FLOOD_CACHE_DIR", here.parents[2] / "data" / "flood_cache"))


def _thresholds_from_env() -> dict[str, float]:
    # These are intentionally transparent MVP screening defaults, NOT hydraulic
    # stage-to-depth conversions. Calibrate them with event flood observations.
    defaults = {
        "no_flooding": 0.0,
        "action": 0.5,
        "minor": 1.5,
        "moderate": 3.0,
        "major": 5.0,
        "record": 7.0,
        "unknown": 1.0,
    }
    raw = os.getenv("ARFA_HAND_THRESHOLDS_M")
    if not raw:
        return defaults
    parsed = json.loads(raw)
    defaults.update({str(k): float(v) for k, v in parsed.items()})
    return defaults


@dataclass(frozen=True)
class FloodConfig:
    cache_dir: Path = field(default_factory=_default_cache_dir)
    dem_resolution_m: float = float(os.getenv("ARFA_DEM_RESOLUTION_M", "10"))
    context_buffer_m: float = float(os.getenv("ARFA_HYDRO_CONTEXT_BUFFER_M", "2000"))
    nhd_min_stream_order: int = int(os.getenv("ARFA_NHD_MIN_STREAM_ORDER", "3"))
    dem_channel_threshold_km2: float = float(os.getenv("ARFA_DEM_CHANNEL_THRESHOLD_KM2", "0.25"))
    nhd_snap_distance_m: float = float(os.getenv("ARFA_NHD_SNAP_DISTANCE_M", "20"))
    gauge_max_flowline_distance_m: float = float(os.getenv("ARFA_GAUGE_MAX_FLOWLINE_DISTANCE_M", "1500"))
    fallback_drainage_threshold_km2: float = float(os.getenv("ARFA_FALLBACK_DRAINAGE_THRESHOLD_KM2", "2"))
    max_dem_pixels: int = int(os.getenv("ARFA_MAX_DEM_PIXELS", "6000"))
    geocode_user_agent: str = os.getenv("ARFA_GEOCODE_USER_AGENT", "ARFA-OASIS-flood-module/0.1")
    max_noaa_enrich_gauges: int = int(os.getenv("ARFA_MAX_GAUGES", "12"))
    static_fallback_hand_m: float = float(os.getenv("ARFA_STATIC_FALLBACK_HAND_M", "1.0"))
    hand_thresholds_m: dict[str, float] = field(default_factory=_thresholds_from_env)

    def ensure_cache(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir
