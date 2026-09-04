from __future__ import annotations

from typing import Iterable

import geopandas as gpd

from .cache import cache_folder, load_terrain, save_terrain
from .config import FloodConfig
from .dem import fetch_3dep_dem
from .gauges import (
    choose_primary_gauge, public_gauge_dict,
    arfa_gauges_to_observations,
)
from .geo import bbox_gdf, buffered_aoi, geocode_place, validate_bbox
from .hazard import hand_mask, mask_to_geojson, select_dynamic_threshold
from .hydrography import fetch_nhdplus_flowlines, select_gauge_connected_flowlines
from .models import RasterLayer, TerrainBundle, HazardResult
from .terrain import derive_hydrology, derive_gauge_specific_hand


class FloodHazardService:
    """Automatic DEM + NHDPlus + gauge flood-screening provider for ARFA."""

    def __init__(self, config: FloodConfig | None = None):
        self.config = config or FloodConfig()

    def resolve_bbox(self, bbox: Iterable[float] | None = None, place: str | None = None):
        if bbox is not None:
            return validate_bbox(bbox)
        if place:
            return geocode_place(place, self.config)
        raise ValueError("Provide bbox or place")

    def build_terrain(self, bbox: Iterable[float], refresh: bool = False) -> TerrainBundle:
        bbox = validate_bbox(bbox)
        folder = cache_folder(bbox, self.config)
        if not refresh:
            cached = load_terrain(folder)
            if cached is not None:
                return cached

        output_aoi = bbox_gdf(bbox)
        analysis_aoi = buffered_aoi(output_aoi, self.config.context_buffer_m)
        dem = fetch_3dep_dem(
            analysis_aoi,
            resolution_m=self.config.dem_resolution_m,
            max_pixels=self.config.max_dem_pixels,
        )
        flowlines = fetch_nhdplus_flowlines(
            analysis_aoi,
            min_stream_order=self.config.nhd_min_stream_order,
        )
        derived = derive_hydrology(
            dem,
            hydrography=flowlines,
            fallback_drainage_threshold_km2=self.config.fallback_drainage_threshold_km2,
            channel_threshold_km2=self.config.dem_channel_threshold_km2,
            snap_distance_m=self.config.nhd_snap_distance_m,
        )
        bundle = TerrainBundle(
            dem=dem,
            hand=RasterLayer(derived["hand"], dem.transform, dem.crs, name="hand", units="m"),
            upstream_area=RasterLayer(derived["upstream_area"], dem.transform, dem.crs,
                                      name="upstream_area", units="km2"),
            slope=RasterLayer(derived["slope"], dem.transform, dem.crs, name="slope", units="rise/run"),
            drainage=RasterLayer(derived["drainage"], dem.transform, dem.crs, name="drainage"),
            raw_nhd=RasterLayer(derived["raw_nhd"], dem.transform, dem.crs, name="raw_nhd"),
            terrain_channels=RasterLayer(derived["terrain_channels"], dem.transform, dem.crs,
                                         name="terrain_channels"),
            flowlines=flowlines,
            drainage_source=derived["drainage_source"],
            alignment_metrics=derived["alignment_metrics"],
        )
        save_terrain(folder, bundle)
        return bundle

    def get_gauges(self, bbox: Iterable[float], arfa_gauges: list[dict] | None = None):
        """
        Return GaugeObservation list for the bbox.
        If arfa_gauges is supplied (already fetched by the main ARFA pipeline),
        convert them rather than making a second identical USGS+NOAA round-trip.
        """
        if arfa_gauges is not None:
            obs = arfa_gauges_to_observations(arfa_gauges)
            if obs:
                return obs
        # Fallback: no pre-fetched gauges — fetch via bbox directly.
        # Import here to keep the no-network test path clean.
        from .gauges import _fetch_gauges_bbox_fallback
        return _fetch_gauges_bbox_fallback(
            validate_bbox(bbox), max_enrich=self.config.max_noaa_enrich_gauges
        )

    def get_hazard(
        self,
        bbox: Iterable[float] | None = None,
        place: str | None = None,
        refresh: bool = False,
        force_category: str | None = None,
        allow_static_fallback: bool = True,
        arfa_gauges: list[dict] | None = None,
    ) -> HazardResult:
        resolved_bbox = self.resolve_bbox(bbox=bbox, place=place)
        terrain = self.build_terrain(resolved_bbox, refresh=refresh)
        gauges = self.get_gauges(resolved_bbox, arfa_gauges=arfa_gauges)
        primary = choose_primary_gauge(gauges, resolved_bbox)
        category, threshold, dynamic, confidence, warnings = select_dynamic_threshold(
            primary, self.config, force_category=force_category,
            allow_static_fallback=allow_static_fallback,
        )
        # By default the precomputed HAND surface references every mapped drainage
        # network in the AOI.  When a gauge is available, narrow the HAND reference
        # to the river represented by that gauge so one gauge cannot flood unrelated
        # creeks/lowlands elsewhere in the city.
        hand_layer = terrain.hand
        reference_river_geojson = {"type": "FeatureCollection", "features": []}
        connectivity = {"applied": False, "reason": "not_needed_or_no_gauge"}
        hand_reference = "all_mapped_drainage"
        gauge_hand_metrics = None

        if primary is not None and threshold > 0 and terrain.flowlines is not None and not terrain.flowlines.empty:
            selected_river, connectivity = select_gauge_connected_flowlines(
                terrain.flowlines,
                primary,
                projected_crs=terrain.dem.crs,
                max_gauge_distance_m=self.config.gauge_max_flowline_distance_m,
            )
            if connectivity.get("applied") and not selected_river.empty:
                try:
                    specific = derive_gauge_specific_hand(
                        terrain.dem,
                        terrain.upstream_area.data,
                        selected_river,
                        channel_threshold_km2=self.config.dem_channel_threshold_km2,
                        snap_distance_m=self.config.nhd_snap_distance_m,
                    )
                    hand_layer = RasterLayer(
                        specific["hand"], terrain.dem.transform, terrain.dem.crs,
                        name="gauge_specific_hand", units="m",
                    )
                    gauge_hand_metrics = specific["alignment_metrics"]
                    connectivity["valid_hand_cell_count"] = specific["valid_hand_cell_count"]
                    reference_river_geojson = selected_river.to_crs(4326).__geo_interface__
                    hand_reference = "selected_gauge_river_network"
                    warnings.append(
                        f"HAND is restricted to the gauge-linked river network ({connectivity.get('seed_river_name') or 'selected NHDPlus mainstem'}), "
                        "rather than every drainage network in the AOI."
                    )
                except Exception as exc:
                    connectivity = dict(connectivity)
                    connectivity["applied"] = False
                    connectivity["reason"] = "gauge_specific_hand_failed"
                    connectivity["error"] = str(exc)
                    warnings.append(
                        "Gauge-linked river HAND could not be constructed; falling back to the AOI-wide HAND surface."
                    )

        mask = hand_mask(hand_layer, threshold)
        geojson = mask_to_geojson(mask, hand_layer, clip_bbox=resolved_bbox)

        metadata = {
            "method": "gauge-linked river HAND screening" if hand_reference == "selected_gauge_river_network" else "gauge-conditioned HAND riverine screening",
            "bbox_wgs84": list(resolved_bbox),
            "dem_source": "USGS 3DEP",
            "hydrography_source": "USGS NHDPlus HR",
            "gauge_sources": ["USGS NWIS instantaneous values", "NOAA NWPS metadata/status"],
            "dem_resolution_requested_m": self.config.dem_resolution_m,
            "drainage_source": terrain.drainage_source,
            "alignment_metrics": terrain.alignment_metrics,
            "gauge_river_connectivity": connectivity,
            "gauge_specific_alignment_metrics": gauge_hand_metrics,
            "hand_reference": hand_reference,
            "selected_gauge": public_gauge_dict(primary) if primary else None,
            "flood_category": category,
            "hand_threshold_m": threshold,
            "dynamic": dynamic,
            "confidence": confidence,
            "stage_used_as_water_depth": False,
            "important_limitation": (
                "This is a screening layer, not a hydraulic inundation forecast. NOAA flood category "
                "selects a configurable HAND threshold; raw gage height is never interpreted as local flood depth."
            ),
        }
        return HazardResult(
            hazard_geojson=geojson,
            reference_river_geojson=reference_river_geojson,
            gauges=gauges,
            selected_gauge=primary,
            flood_category=category,
            hand_threshold_m=threshold,
            dynamic=dynamic,
            confidence=confidence,
            warnings=warnings,
            metadata=metadata,
        )
