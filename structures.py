"""
structures.py — USA Structures query module with Pyramid Spatial Index

At startup: loads the lightweight JSON index (pyramid_config, states, cells).
At query time:
  1. Traverse pyramid to find leaf cells intersecting viewport bbox
  2. Determine which state .gdb files to query
  3. Use geopandas bbox filter on each .gdb (uses .gdb internal spatial index)
  4. Merge, cap, return GeoJSON

No .gdb files are loaded at startup — only the small JSON index files.
"""
import json
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import geopandas as gpd
from pathlib import Path
from shapely.geometry import mapping
from flask import Blueprint, Response, jsonify, request, stream_with_context

structures_bp = Blueprint("structures", __name__)

# ── Runtime state ──────────────────────────────────────────────────────────────
_config      = None   # pyramid_config.json
_states      = None   # states_index.json
_cells       = None   # cells_index.json
_index_dir   = None
_gdb_dir     = None   # root directory that contains per-state GDB folders

MAX_RESULTS  = int(os.getenv("ARFA_MAX_STRUCTURES", "1000000"))
SIMPLIFY_TOL = 0.00005  # degrees, for polygon simplification
STREAM_BATCH_SIZE = 5000   # polygons sent to browser per streamed batch
MAX_QUERY_WORKERS = 8      # parallel state-GDB readers

# ── Popup columns and display names ───────────────────────────────────────────
POPUP_COLS = [
    "BUILD_ID", "OCC_CLS", "PRIM_OCC",
    "PROP_ADDR", "PROP_CITY", "PROP_ST", "PROP_ZIP", "PROP_CNTY",
    "HEIGHT", "SQFEET", "SQMETERS",
    "POP_MEDIAN", "POP_CI95_lower", "POP_CI95_UPPER",
    "B_CODE", "FIPS", "CENSUSCODE",
    "LONGITUDE", "LATITUDE", "SOURCE", "PROD_DATE",
]

DISPLAY_NAMES = {
    "BUILD_ID":        "Building ID",
    "OCC_CLS":         "Occupancy Class",
    "PRIM_OCC":        "Primary Use",
    "PROP_ADDR":       "Address",
    "PROP_CITY":       "City",
    "PROP_ST":         "State",
    "PROP_ZIP":        "ZIP Code",
    "PROP_CNTY":       "County",
    "HEIGHT":          "Building Height (m)",
    "SQFEET":          "Area (sq ft)",
    "SQMETERS":        "Area (sq m)",
    "POP_MEDIAN":      "Est. Population (median)",
    "POP_CI95_lower":  "Est. Population (lower 95%)",
    "POP_CI95_UPPER":  "Est. Population (upper 95%)",
    "B_CODE":          "Building Code",
    "FIPS":            "FIPS Code",
    "CENSUSCODE":      "Census Tract",
    "LONGITUDE":       "Longitude",
    "LATITUDE":        "Latitude",
    "SOURCE":          "Data Source",
    "PROD_DATE":       "Data Date",
}


# ── Index loading ──────────────────────────────────────────────────────────────

def load_index(index_dir: str):
    """
    Load the three JSON index files into memory at server startup.
    Fast — total index size is a few MB at most.
    """
    global _config, _states, _cells, _index_dir, _gdb_dir
    idx = Path(index_dir)

    cfg_path = idx / "pyramid_config.json"
    si_path  = idx / "states_index.json"
    ci_path  = idx / "cells_index.json"

    for p in [cfg_path, si_path, ci_path]:
        if not p.exists():
            print(f"[structures] Index file not found: {p}")
            print(f"[structures] Run build_index.py first to build the index.")
            return False

    _config    = json.loads(cfg_path.read_text())
    _states    = json.loads(si_path.read_text())
    _cells     = json.loads(ci_path.read_text())
    _index_dir = idx

    # Resolve the GDB root portably. Older pre-built indexes can contain
    # machine-specific absolute paths (for example /home/tyh/ARFA/...).
    # Never use a stale path from another machine as the download target.
    env_gdb_dir = os.getenv("ARFA_STRUCTURES_GDB_DIR", "").strip()
    project_gdb_dir = Path(__file__).resolve().parent / "Data_USA_Structures" / "2025_06"

    if env_gdb_dir:
        inferred_gdb_dir = Path(env_gdb_dir).expanduser()
    else:
        inferred_gdb_dir = None
        if _states:
            sample_gdb = next(iter(_states.values()), {}).get("gdb", "")
            if sample_gdb:
                sample_path = Path(sample_gdb).expanduser()
                # Only infer from legacy absolute paths when they actually exist
                # on this machine. Relative paths are resolved from _gdb_dir later.
                if sample_path.is_absolute():
                    indexed_root = sample_path.parent.parent
                    if indexed_root.exists():
                        inferred_gdb_dir = indexed_root
        if inferred_gdb_dir is None:
            inferred_gdb_dir = project_gdb_dir

    _gdb_dir = str(inferred_gdb_dir)

    print(f"[structures] Index loaded:")
    print(f"  States: {len(_states)}")
    print(f"  Cells:  {len(_cells)} total, "
          f"{sum(1 for c in _cells.values() if c['level']==_config['H'])} leaf")
    print(f"  Pyramid H={_config['H']}, "
          f"grid={_config['GRID_COLS']}×{_config['GRID_ROWS']}")
    if _gdb_dir:
        print(f"  GDB root: {_gdb_dir}")
    return True


def reload_index():
    """Hot-reload the index after auto-repair without restarting the server."""
    if _index_dir is None:
        return False
    return load_index(str(_index_dir))


# ── Pyramid traversal ──────────────────────────────────────────────────────────

def bboxes_intersect(a: list, b: list) -> bool:
    """Check if two [min_lon, min_lat, max_lon, max_lat] bboxes intersect."""
    return not (a[2] <= b[0] or b[2] <= a[0] or
                a[3] <= b[1] or b[3] <= a[1])


def find_states_for_bbox(query_bbox: list) -> list[str]:
    """
    Traverse the pyramid to find all state .gdb files that intersect
    the query bbox. Returns list of state codes.

    Algorithm:
      Start at root (h0_0_0).
      DFS: if cell intersects query_bbox, recurse into children.
      At leaf level H: collect states listed in that cell.
    """
    if _cells is None:
        return []

    H = _config["H"]
    root_id = "h0_0_0"

    if root_id not in _cells:
        # Fallback: linear scan of leaf cells (slower but correct)
        print("[structures] Warning: root cell not found, falling back to linear scan")
        states = set()
        for cid, cell in _cells.items():
            if cell["level"] == H and bboxes_intersect(query_bbox, cell["bbox"]):
                states.update(cell.get("states", []))
        return list(states)

    # DFS traversal
    states = set()
    stack  = [root_id]
    visited = 0

    while stack:
        cid  = stack.pop()
        cell = _cells.get(cid)
        if cell is None:
            continue
        if not bboxes_intersect(query_bbox, cell["bbox"]):
            continue

        visited += 1

        if cell["level"] == H:
            # Leaf: collect states
            states.update(cell.get("states", []))
        else:
            # Internal: recurse into children
            for child in cell.get("children", []):
                stack.append(child)

    print(f"[structures] Pyramid traversal: {visited} cells visited → "
          f"states: {sorted(states)}")
    return list(states)


# ── GDB querying ───────────────────────────────────────────────────────────────

GDB_MISSING = object()  # sentinel: GDB file not on disk


def query_state_gdb(state_code: str, bbox: tuple):
    """
    Query a single state .gdb file with a bbox filter.
    Returns:
      GeoDataFrame  — results found
      None          — GDB exists but returned no rows in bbox
      GDB_MISSING   — GDB file does not exist on disk
    """
    state_info = _states.get(state_code)
    if not state_info:
        print(f"[structures] Unknown state code: {state_code}")
        return None

    indexed_gdb_path = Path(state_info["gdb"]).expanduser()
    if not indexed_gdb_path.is_absolute() and _gdb_dir:
        indexed_gdb_path = Path(_gdb_dir) / indexed_gdb_path
    local_gdb_path = (
        Path(_gdb_dir) / state_code.upper() / f"{state_code.upper()}_Structures.gdb"
        if _gdb_dir else None
    )

    # Prefer the configured/resolved local data root. Fall back to the path
    # stored in the index only when that path actually exists on this machine.
    if local_gdb_path is not None and local_gdb_path.exists():
        gdb_path = local_gdb_path
    elif indexed_gdb_path.exists():
        gdb_path = indexed_gdb_path
    else:
        gdb_path = local_gdb_path or indexed_gdb_path
        print(f"[structures] GDB not found: {gdb_path}")
        return GDB_MISSING

    t0 = time.time()
    try:
        gdf = gpd.read_file(
            gdb_path,
            bbox=bbox,   # (min_lon, min_lat, max_lon, max_lat)
        )
        elapsed = time.time() - t0

        if gdf.empty:
            print(f"[structures] {state_code}: 0 structures in bbox ({elapsed:.2f}s)")
            return None

        # Resolve requested fields case-insensitively because FileGDB schemas
        # are not perfectly consistent across state deliveries (for example
        # POP_CI95_lower vs POP_CI95_LOWER).
        actual_by_upper = {str(c).upper(): c for c in gdf.columns if c != "geometry"}

        selected = []
        rename_map = {}
        for canonical in POPUP_COLS:
            actual = actual_by_upper.get(canonical.upper())
            if actual is not None:
                selected.append(actual)
                rename_map[actual] = canonical

        selected.append("geometry")
        gdf = gdf[selected].rename(columns=rename_map)

        # Ensure EPSG:4326
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

        print(f"[structures] {state_code}: {len(gdf):,} structures "
              f"({elapsed:.2f}s)")
        return gdf

    except Exception as e:
        print(f"[structures] Error querying {state_code}: {e}")
        return None


# ── GeoJSON serialization ──────────────────────────────────────────────────────

def gdf_to_features(gdf: gpd.GeoDataFrame) -> list:
    """Convert GeoDataFrame rows to GeoJSON feature dicts."""
    features = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        # Simplify for faster transfer
        try:
            geom_s = geom.simplify(SIMPLIFY_TOL, preserve_topology=False)
            if geom_s.is_empty:
                geom_s = geom
        except Exception:
            geom_s = geom

        # Convert Shapely geometry directly to a GeoJSON-compatible mapping.
        # This is substantially cheaper than creating a GeoSeries + to_json()
        # for every individual structure.
        try:
            geom_dict = mapping(geom_s)
        except Exception:
            continue

        # Build a stable popup schema. Every configured field is emitted,
        # even when that particular structure has no value. This lets the
        # frontend distinguish "field exists but value unavailable" from
        # "field was accidentally dropped by serialization".
        props = {}
        for col in POPUP_COLS:
            display = DISPLAY_NAMES.get(col, col)

            if col not in row.index:
                props[display] = None
                continue

            val = row[col]

            # Convert numpy/pandas scalar types.
            if hasattr(val, "item"):
                try:
                    val = val.item()
                except Exception:
                    pass

            if hasattr(val, "isoformat"):
                try:
                    val = val.isoformat()[:10]
                except Exception:
                    pass

            # Normalize NaN / pandas missing values to JSON null.
            try:
                if val != val:
                    val = None
            except Exception:
                pass

            props[display] = val

        features.append({
            "type":       "Feature",
            "geometry":   geom_dict,
            "properties": props,
        })

    return features


# ── Flask endpoint ─────────────────────────────────────────────────────────────

@structures_bp.route("/api/structures")
def structures():
    """
    Return structures within the map viewport as GeoJSON.

    Query params: minLat, maxLat, minLon, maxLon
    Returns GeoJSON FeatureCollection + metadata.

    Flow:
      1. Parse viewport bbox
      2. Traverse pyramid → find intersecting states
      3. Query each state .gdb with bbox filter
      4. Merge, cap at MAX_RESULTS, serialize
    """
    if _cells is None:
        return jsonify({
            "type": "FeatureCollection",
            "features": [],
            "message": "Spatial index not loaded. Run build_index.py first.",
        })

    try:
        min_lat = float(request.args.get("minLat"))
        max_lat = float(request.args.get("maxLat"))
        min_lon = float(request.args.get("minLon"))
        max_lon = float(request.args.get("maxLon"))
    except (TypeError, ValueError):
        return jsonify({"error": "minLat, maxLat, minLon, maxLon required"}), 400

    query_bbox = [min_lon, min_lat, max_lon, max_lat]
    bbox_tuple = (min_lon, min_lat, max_lon, max_lat)

    print(f"\n[structures] Query bbox: lat={min_lat:.3f}–{max_lat:.3f} "
          f"lon={min_lon:.3f}–{max_lon:.3f}")

    t_total = time.time()

    # Step 1: pyramid traversal → which states to query
    state_codes = find_states_for_bbox(query_bbox)

    if not state_codes:
        return jsonify({
            "type":         "FeatureCollection",
            "features":     [],
            "total_in_area": 0,
            "returned":     0,
            "capped":       False,
            "states_queried": [],
            "message":      "No structure data available for this area.",
        })

    # Step 2: query each state .gdb
    all_gdfs = []
    missing_gdbs = []
    for sc in state_codes:
        gdf = query_state_gdb(sc, bbox_tuple)
        if gdf is GDB_MISSING:
            missing_gdbs.append(sc)
            continue
        if gdf is not None and not gdf.empty:
            all_gdfs.append(gdf)

    if not all_gdfs:
        return jsonify({
            "type":         "FeatureCollection",
            "features":     [],
            "total_in_area": 0,
            "returned":     0,
            "capped":       False,
            "states_queried": state_codes,
            "missing_gdbs": missing_gdbs,
            "message":      "No structures found in this viewport.",
        })

    # Step 3: merge
    combined = gpd.pd.concat(all_gdfs, ignore_index=True) \
               if len(all_gdfs) > 1 else all_gdfs[0]

    total   = len(combined)
    capped  = total > MAX_RESULTS
    if capped:
        combined = combined.iloc[:MAX_RESULTS]

    print(f"[structures] Total: {total:,} → returning {len(combined):,} "
          f"({'capped' if capped else 'all'}) "
          f"in {time.time()-t_total:.2f}s")

    # Step 4: serialize to GeoJSON
    features = gdf_to_features(combined)

    return jsonify({
        "type":           "FeatureCollection",
        "features":       features,
        "total_in_area":  total,
        "returned":       len(features),
        "capped":         capped,
        "cap_limit":      MAX_RESULTS,
        "states_queried": state_codes,
    })


# ── Progressive streaming endpoint ─────────────────────────────────────────────

@structures_bp.route("/api/structures/stream")
def structures_stream():
    """
    Progressively stream structures for the current viewport as NDJSON.

    Why NDJSON instead of one huge GeoJSON response?
      - State GDBs can be queried concurrently.
      - The browser can draw the first batch while later batches are prepared.
      - A progress bar can update as state files finish.
      - We avoid holding one giant serialized JSON response in memory.

    Query params:
      minLat, maxLat, minLon, maxLon

    Stream message types:
      meta:
        {"kind":"meta","states":[...],"states_total":N,"batch_size":5000}

      batch:
        {"kind":"batch","features":[...],"batch_count":N,
         "loaded_so_far":N,"state":"XX"}

      state_done:
        {"kind":"state_done","state":"XX","state_count":N,
         "states_done":N,"states_total":N}

      done:
        {"kind":"done","total_in_area":N,"returned":N,"capped":bool,
         "cap_limit":MAX_RESULTS,"states_queried":[...]}

      error:
        {"kind":"error","message":"..."}

    Notes:
      * Parallelism is across independent state .gdb files. This is where
        concurrency is useful and avoids repeatedly hitting one GDB from
        multiple workers.
      * Within a state, results are serialized and emitted in batches.
      * STREAM_BATCH_SIZE controls browser update granularity.
    """
    if _cells is None:
        return jsonify({
            "error": "Spatial index not loaded. Run build_index.py first."
        }), 503

    try:
        min_lat = float(request.args.get("minLat"))
        max_lat = float(request.args.get("maxLat"))
        min_lon = float(request.args.get("minLon"))
        max_lon = float(request.args.get("maxLon"))
    except (TypeError, ValueError):
        return jsonify({
            "error": "minLat, maxLat, minLon, maxLon required"
        }), 400

    query_bbox = [min_lon, min_lat, max_lon, max_lat]
    bbox_tuple = (min_lon, min_lat, max_lon, max_lat)

    state_codes = sorted(find_states_for_bbox(query_bbox))

    def emit(obj):
        # One compact JSON object per line.
        return json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n"

    @stream_with_context
    def generate():
        t_total = time.time()

        yield emit({
            "kind": "meta",
            "states": state_codes,
            "states_total": len(state_codes),
            "batch_size": STREAM_BATCH_SIZE,
            "cap_limit": MAX_RESULTS,
        })

        if not state_codes:
            yield emit({
                "kind": "done",
                "total_in_area": 0,
                "returned": 0,
                "capped": False,
                "cap_limit": MAX_RESULTS,
                "states_queried": [],
                "elapsed_s": round(time.time() - t_total, 3),
            })
            return

        # Read different state files concurrently. For a viewport entirely
        # inside one state there is only one worker, which avoids pointless
        # competing reads against the same GDB.
        workers = min(MAX_QUERY_WORKERS, len(state_codes))
        loaded_so_far = 0
        total_in_area = 0
        states_done = 0
        capped = False
        missing_gdbs = []   # state codes whose GDB file was not found on disk

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(query_state_gdb, sc, bbox_tuple): sc
                for sc in state_codes
            }

            for future in as_completed(futures):
                sc = futures[future]

                try:
                    gdf = future.result()
                except Exception as exc:
                    states_done += 1
                    yield emit({
                        "kind": "state_done",
                        "state": sc,
                        "state_count": 0,
                        "states_done": states_done,
                        "states_total": len(state_codes),
                        "warning": str(exc),
                    })
                    continue

                if gdf is GDB_MISSING:
                    missing_gdbs.append(sc)
                    states_done += 1
                    yield emit({
                        "kind": "state_done",
                        "state": sc,
                        "state_count": 0,
                        "states_done": states_done,
                        "states_total": len(state_codes),
                        "gdb_missing": True,
                    })
                    continue

                state_count = 0 if gdf is None else len(gdf)
                total_in_area += state_count

                if gdf is not None and not gdf.empty and loaded_so_far < MAX_RESULTS:
                    remaining = MAX_RESULTS - loaded_so_far
                    usable = gdf.iloc[:remaining]

                    # Serialize and flush one browser-sized chunk at a time.
                    for start in range(0, len(usable), STREAM_BATCH_SIZE):
                        chunk = usable.iloc[start:start + STREAM_BATCH_SIZE]
                        features = gdf_to_features(chunk)

                        if not features:
                            continue

                        loaded_so_far += len(features)

                        yield emit({
                            "kind": "batch",
                            "state": sc,
                            "features": features,
                            "batch_count": len(features),
                            "loaded_so_far": loaded_so_far,
                        })

                states_done += 1
                yield emit({
                    "kind": "state_done",
                    "state": sc,
                    "state_count": state_count,
                    "states_done": states_done,
                    "states_total": len(state_codes),
                })

        capped = total_in_area > MAX_RESULTS

        print(
            f"[structures/stream] Total: {total_in_area:,} → "
            f"streamed {loaded_so_far:,} "
            f"({'capped' if capped else 'all'}) "
            f"in {time.time()-t_total:.2f}s"
        )
        if missing_gdbs:
            print(f"[structures/stream] Missing GDBs: {missing_gdbs}")

        yield emit({
            "kind": "done",
            "total_in_area": total_in_area,
            "returned": loaded_so_far,
            "capped": capped,
            "cap_limit": MAX_RESULTS,
            "states_queried": state_codes,
            "missing_gdbs": missing_gdbs,
            "elapsed_s": round(time.time() - t_total, 3),
        })

    response = Response(generate(), mimetype="application/x-ndjson")
    # Important when running behind nginx or another reverse proxy:
    # do not buffer the stream, otherwise the browser receives everything
    # only after the backend has finished.
    response.headers["Cache-Control"] = "no-cache, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


# ── Index status endpoint ──────────────────────────────────────────────────────

@structures_bp.route("/api/structures/status")
def structures_status():
    """Return index status for debugging."""
    if _config is None:
        return jsonify({"loaded": False, "message": "Index not loaded"})
    H = _config["H"]
    return jsonify({
        "loaded":        True,
        "states":        len(_states),
        "cells_total":   len(_cells),
        "cells_leaf":    sum(1 for c in _cells.values() if c["level"] == H),
        "pyramid_H":     H,
        "grid":          f"{_config['GRID_COLS']}×{_config['GRID_ROWS']}",
        "cell_size_deg": f"{_config['CELL_WIDTH_DEG']:.3f}°×{_config['CELL_HEIGHT_DEG']:.3f}°",
        "built_at":      _config.get("built_at", "unknown"),
    })
