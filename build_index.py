"""
build_index.py — Pyramid Spatial Index Builder for USA Structures

Builds a lightweight spatial index on top of existing .gdb files.
No .gdb files are read or modified — only metadata XMLs are parsed.

Output files:
  {INDEX_DIR}/pyramid_config.json   — pyramid parameters
  {INDEX_DIR}/states_index.json     — per-state bbox + gdb path
  {INDEX_DIR}/cells_index.json      — per-cell bbox + states + counts

Usage:
  python build_index.py
  python build_index.py --data-dir ./Data_USA_Structures/2025_06
  python build_index.py --data-dir /path/to/data --index-dir /path/to/index --H 6
  python build_index.py --check     # validate index after building
"""

import os
import sys
import json
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


# ── Default config ─────────────────────────────────────────────────────────────
DEFAULT_DATA_DIR  = str(Path(__file__).resolve().parent / "Data_USA_Structures" / "2025_06")
DEFAULT_INDEX_DIR = str(Path(__file__).resolve().parent / "USA_Structures_Index")

# Pyramid parameters — all configurable via CLI
DEFAULT_H          = 5       # pyramid depth (leaf cells = 2^H × 2^H)
USA_BBOX           = [-180, 15, -60, 75]   # [min_lon, min_lat, max_lon, max_lat]
# Covers contiguous US + AK + HI + territories (PR, GU, VI, AS, MP)


# ── XML parsing ────────────────────────────────────────────────────────────────

def parse_metadata_xml(xml_path: Path) -> dict | None:
    """
    Extract bounding box and metadata from a FEMA USA Structures metadata XML.
    Returns dict or None if parsing fails.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Bounding box lives at: metadata/idinfo/spdom/bounding
        bounding = root.find(".//spdom/bounding")
        if bounding is None:
            print(f"  [warn] No bounding box in {xml_path.name}")
            return None

        west  = float(bounding.findtext("westbc",  default="0"))
        east  = float(bounding.findtext("eastbc",  default="0"))
        south = float(bounding.findtext("southbc", default="0"))
        north = float(bounding.findtext("northbc", default="0"))

        # Publication date
        pub_date = root.findtext(".//citation/citeinfo/pubdate", default="")

        # Title (e.g. "VT_Structures")
        title = root.findtext(".//citation/citeinfo/title", default="")

        return {
            "bbox":     [west, south, east, north],  # [min_lon, min_lat, max_lon, max_lat]
            "pub_date": pub_date,
            "title":    title,
        }
    except Exception as e:
        print(f"  [error] Failed to parse {xml_path}: {e}")
        return None


# ── State folder scanning ──────────────────────────────────────────────────────

def scan_state_folders(data_dir: Path) -> dict:
    """
    Scan data directory for state folders.
    Expects structure: {data_dir}/{STATE}/{STATE}_Structures.gdb
                       {data_dir}/{STATE}/{STATE}_Structures_metadata.xml

    Returns dict: state_code → {name, gdb, xml, bbox, pub_date, structure_count}
    """
    states = {}

    if not data_dir.exists():
        print(f"[error] Data directory not found: {data_dir}")
        return states

    entries = sorted(data_dir.iterdir())
    print(f"Found {len(entries)} entries in {data_dir}")

    for folder in entries:
        if not folder.is_dir():
            continue

        state_code = folder.name.upper()
        if len(state_code) not in (2, 3):   # state codes are 2-3 chars
            continue

        # Find .gdb and .xml
        gdb_path = folder / f"{state_code}_Structures.gdb"
        xml_path = folder / f"{state_code}_Structures_metadata.xml"

        if not gdb_path.exists():
            # Try case-insensitive
            matches = list(folder.glob("*_Structures.gdb"))
            gdb_path = matches[0] if matches else None

        if not xml_path.exists():
            matches = list(folder.glob("*_metadata.xml"))
            xml_path = matches[0] if matches else None

        if not gdb_path or not gdb_path.exists():
            print(f"  [skip] {state_code} — no .gdb found")
            continue

        if not xml_path or not xml_path.exists():
            print(f"  [warn] {state_code} — no metadata XML, using full bbox")
            meta = None
        else:
            meta = parse_metadata_xml(xml_path)

        if meta is None:
            print(f"  [skip] {state_code} — metadata parse failed")
            continue

        # Estimate structure count from .gdb file size (rough: ~2KB per structure)
        try:
            gdb_size = sum(f.stat().st_size for f in gdb_path.rglob("*") if f.is_file())
            est_count = max(1, gdb_size // 2048)
        except Exception:
            est_count = 0

        states[state_code] = {
            "name":            state_code,
            "gdb":             str(gdb_path.resolve()),
            "xml":             str(xml_path.resolve()),
            "bbox":            meta["bbox"],
            "pub_date":        meta["pub_date"],
            "title":           meta["title"],
            "structure_count": est_count,
        }
        print(f"  ✓ {state_code:4s} bbox={[round(v,3) for v in meta['bbox']]} "
              f"gdb={gdb_path.name}")

    return states


# ── Pyramid geometry ───────────────────────────────────────────────────────────

def cell_bbox(col: int, row: int, H: int, usa_bbox: list) -> list:
    """
    Compute the geographic bounding box of a leaf cell at level H.
    Grid is 2^H columns × 2^H rows.

    Returns [min_lon, min_lat, max_lon, max_lat]
    """
    grid_size = 2 ** H
    min_lon, min_lat, max_lon, max_lat = usa_bbox

    cell_w = (max_lon - min_lon) / grid_size
    cell_h = (max_lat - min_lat) / grid_size

    return [
        round(min_lon + col * cell_w, 6),
        round(min_lat + row * cell_h, 6),
        round(min_lon + (col + 1) * cell_w, 6),
        round(min_lat + (row + 1) * cell_h, 6),
    ]


def cell_id(level: int, col: int, row: int) -> str:
    return f"h{level}_{col}_{row}"


def bboxes_intersect(a: list, b: list) -> bool:
    """
    Check if two bboxes intersect.
    Both in [min_lon, min_lat, max_lon, max_lat] format.
    """
    return not (a[2] <= b[0] or b[2] <= a[0] or
                a[3] <= b[1] or b[3] <= a[1])


def which_leaf_cells(state_bbox: list, H: int, usa_bbox: list) -> list:
    """
    Find all leaf cells (level H) whose bbox intersects the state bbox.
    Returns list of (col, row) tuples.
    """
    grid_size = 2 ** H
    min_lon, min_lat, max_lon, max_lat = usa_bbox
    cell_w = (max_lon - min_lon) / grid_size
    cell_h = (max_lat - min_lat) / grid_size

    s_minlon, s_minlat, s_maxlon, s_maxlat = state_bbox

    # Compute column/row range
    col_min = max(0, int((s_minlon - min_lon) / cell_w))
    col_max = min(grid_size - 1, int((s_maxlon - min_lon) / cell_w))
    row_min = max(0, int((s_minlat - min_lat) / cell_h))
    row_max = min(grid_size - 1, int((s_maxlat - min_lat) / cell_h))

    cells = []
    for col in range(col_min, col_max + 1):
        for row in range(row_min, row_max + 1):
            cb = cell_bbox(col, row, H, usa_bbox)
            if bboxes_intersect(state_bbox, cb):
                cells.append((col, row))
    return cells


def build_ancestor_cells(leaf_cells: set, H: int) -> dict:
    """
    Given a set of (level, col, row) leaf cells, build all ancestor cells
    up to level 0. Returns dict of cell_id → {level, col, row, children}.
    """
    all_cells = {}

    # Start with leaves
    for col, row in leaf_cells:
        cid = cell_id(H, col, row)
        if cid not in all_cells:
            all_cells[cid] = {"level": H, "col": col, "row": row, "children": []}

    # Walk up the pyramid
    for level in range(H, 0, -1):
        for cid, node in list(all_cells.items()):
            if node["level"] != level:
                continue
            p_col = node["col"] // 2
            p_row = node["row"] // 2
            p_level = level - 1
            p_cid = cell_id(p_level, p_col, p_row)
            if p_cid not in all_cells:
                all_cells[p_cid] = {
                    "level": p_level, "col": p_col, "row": p_row, "children": []
                }
            if cid not in all_cells[p_cid]["children"]:
                all_cells[p_cid]["children"].append(cid)

    return all_cells


# ── Index builder ──────────────────────────────────────────────────────────────

def build_cells_index(states: dict, H: int, usa_bbox: list) -> dict:
    """
    Build the cells_index: for each leaf cell, which states intersect it.
    Also assigns structure_count as sum of state counts weighted by overlap.
    """
    cells = {}   # cell_id → {bbox, states, structure_count}
    leaf_set = set()

    for state_code, state in states.items():
        sbbox = state["bbox"]
        matching = which_leaf_cells(sbbox, H, usa_bbox)

        for col, row in matching:
            cid = cell_id(H, col, row)
            leaf_set.add((col, row))

            if cid not in cells:
                cells[cid] = {
                    "level":           H,
                    "col":             col,
                    "row":             row,
                    "bbox":            cell_bbox(col, row, H, usa_bbox),
                    "states":          [],
                    "structure_count": 0,
                }

            if state_code not in cells[cid]["states"]:
                cells[cid]["states"].append(state_code)

            # Distribute structure count proportionally across cells
            n_cells = len(matching)
            cells[cid]["structure_count"] += state["structure_count"] // max(n_cells, 1)

    # Build ancestor hierarchy
    hierarchy = build_ancestor_cells(leaf_set, H)

    # Add ancestor cells to index (non-leaf, for traversal)
    for cid, node in hierarchy.items():
        if cid in cells:
            continue  # already a leaf
        level = node["level"]
        col   = node["col"]
        row   = node["row"]
        cells[cid] = {
            "level":           level,
            "col":             col,
            "row":             row,
            "bbox":            cell_bbox(col, row, level, usa_bbox)
                               if level == H else
                               cell_bbox_at_level(col, row, level, H, usa_bbox),
            "states":          [],   # populated below
            "structure_count": 0,
            "children":        node["children"],
        }

    # Propagate states up from leaves
    for level in range(H, 0, -1):
        for cid, cell in cells.items():
            if cell["level"] != level:
                continue
            p_col   = cell["col"] // 2
            p_row   = cell["row"] // 2
            p_level = level - 1
            p_cid   = cell_id(p_level, p_col, p_row)
            if p_cid in cells:
                for st in cell["states"]:
                    if st not in cells[p_cid]["states"]:
                        cells[p_cid]["states"].append(st)
                cells[p_cid]["structure_count"] += cell["structure_count"]

    return cells


def cell_bbox_at_level(col: int, row: int, level: int, H: int, usa_bbox: list) -> list:
    """Compute bbox for a cell at an arbitrary level (not just H)."""
    grid_size = 2 ** level
    min_lon, min_lat, max_lon, max_lat = usa_bbox
    cell_w = (max_lon - min_lon) / grid_size
    cell_h = (max_lat - min_lat) / grid_size
    return [
        round(min_lon + col * cell_w, 6),
        round(min_lat + row * cell_h, 6),
        round(min_lon + (col + 1) * cell_w, 6),
        round(min_lat + (row + 1) * cell_h, 6),
    ]


# ── Query helpers (used at runtime, included here for self-containment) ────────

def find_leaf_cells_for_bbox(query_bbox: list, cells_index: dict, H: int) -> list:
    """
    Given a query bbox, return all leaf cells (level == H) that intersect it.
    Traverses the pyramid top-down for efficiency.
    """
    root = cell_id(0, 0, 0)
    if root not in cells_index:
        return []

    results = []
    stack = [root]

    while stack:
        cid = stack.pop()
        cell = cells_index.get(cid)
        if cell is None:
            continue
        if not bboxes_intersect(query_bbox, cell["bbox"]):
            continue
        if cell["level"] == H:
            results.append(cid)
        else:
            for child in cell.get("children", []):
                stack.append(child)

    return results


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build pyramid spatial index for USA Structures"
    )
    parser.add_argument("--data-dir",  default=DEFAULT_DATA_DIR,
                        help=f"Root data directory (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR,
                        help=f"Output index directory (default: {DEFAULT_INDEX_DIR})")
    parser.add_argument("--H", type=int, default=DEFAULT_H,
                        help=f"Pyramid depth (default: {DEFAULT_H}). "
                             f"Leaf grid = 2^H × 2^H cells")
    parser.add_argument("--usa-bbox", nargs=4, type=float,
                        default=USA_BBOX,
                        metavar=("MIN_LON","MIN_LAT","MAX_LON","MAX_LAT"),
                        help="USA bounding box (default: -180 15 -60 75)")
    parser.add_argument("--check", action="store_true",
                        help="Validate existing index without rebuilding")
    args = parser.parse_args()

    data_dir  = Path(args.data_dir)
    index_dir = Path(args.index_dir)
    H         = args.H
    usa_bbox  = args.usa_bbox
    grid_size = 2 ** H

    if args.check:
        validate_index(index_dir)
        return

    index_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  USA Structures Pyramid Index Builder")
    print(f"  Data:  {data_dir}")
    print(f"  Index: {index_dir}")
    print(f"  H={H}  Grid={grid_size}×{grid_size}  "
          f"Cell≈{(usa_bbox[2]-usa_bbox[0])/grid_size:.2f}°lon × "
          f"{(usa_bbox[3]-usa_bbox[1])/grid_size:.2f}°lat")
    print(f"{'='*60}\n")

    # Phase 1: scan state folders
    print("[1/3] Scanning state folders and parsing metadata XMLs...")
    states = scan_state_folders(data_dir)
    print(f"\n  → {len(states)} states indexed\n")

    if not states:
        print("No states found. Check --data-dir path.")
        sys.exit(1)

    # Phase 2: build cells index
    print("[2/3] Building pyramid cells index...")
    cells = build_cells_index(states, H, usa_bbox)
    leaf_cells   = [c for c in cells.values() if c["level"] == H]
    parent_cells = [c for c in cells.values() if c["level"] <  H]
    print(f"  → {len(leaf_cells)} non-empty leaf cells  "
          f"({len(leaf_cells)}/{grid_size**2} = "
          f"{100*len(leaf_cells)/grid_size**2:.1f}% occupied)")
    print(f"  → {len(parent_cells)} ancestor cells\n")

    # Phase 3: write JSON files
    print("[3/3] Writing index files...")

    # pyramid_config.json
    config = {
        "H":               H,
        "USA_BBOX":        usa_bbox,
        "GRID_COLS":       grid_size,
        "GRID_ROWS":       grid_size,
        "CELL_WIDTH_DEG":  round((usa_bbox[2]-usa_bbox[0])/grid_size, 6),
        "CELL_HEIGHT_DEG": round((usa_bbox[3]-usa_bbox[1])/grid_size, 6),
        "TOTAL_STATES":    len(states),
        "TOTAL_CELLS":     len(cells),
        "LEAF_CELLS":      len(leaf_cells),
        "built_at":        datetime.now(timezone.utc).isoformat(),
        "data_dir":        str(data_dir),
    }
    cfg_path = index_dir / "pyramid_config.json"
    cfg_path.write_text(json.dumps(config, indent=2))
    print(f"  ✓ {cfg_path}  ({cfg_path.stat().st_size/1024:.1f} KB)")

    # states_index.json
    si_path = index_dir / "states_index.json"
    si_path.write_text(json.dumps(states, indent=2))
    print(f"  ✓ {si_path}  ({si_path.stat().st_size/1024:.1f} KB)")

    # cells_index.json
    ci_path = index_dir / "cells_index.json"
    ci_path.write_text(json.dumps(cells, indent=2))
    print(f"  ✓ {ci_path}  ({ci_path.stat().st_size/1024:.1f} KB)")

    # Summary
    total_structs = sum(s["structure_count"] for s in states.values())
    print(f"\n{'='*60}")
    print(f"  Index built successfully")
    print(f"  States:     {len(states)}")
    print(f"  Est. total structures: {total_structs:,}")
    print(f"  Leaf cells: {len(leaf_cells)} / {grid_size**2}")
    print(f"  Total cells (all levels): {len(cells)}")
    print(f"  Index size: {sum(p.stat().st_size for p in index_dir.glob('*.json'))/1024:.1f} KB")
    print(f"{'='*60}\n")

    print("Validating index...")
    validate_index(index_dir)


def validate_index(index_dir: Path):
    """Quick validation pass on the built index."""
    cfg_path = index_dir / "pyramid_config.json"
    si_path  = index_dir / "states_index.json"
    ci_path  = index_dir / "cells_index.json"

    ok = True
    for p in [cfg_path, si_path, ci_path]:
        if not p.exists():
            print(f"  ✗ Missing: {p}")
            ok = False
        else:
            data = json.loads(p.read_text())
            print(f"  ✓ {p.name}  ({len(data)} entries, {p.stat().st_size/1024:.1f} KB)")

    if not ok:
        print("  Validation failed.")
        return

    cfg    = json.loads(cfg_path.read_text())
    states = json.loads(si_path.read_text())
    cells  = json.loads(ci_path.read_text())
    H      = cfg["H"]

    # Test a sample query
    test_bbox = [-73.5, 44.0, -72.5, 45.0]  # Vermont
    hits = find_leaf_cells_for_bbox(test_bbox, cells, H)
    hit_states = set()
    for cid in hits:
        hit_states.update(cells[cid].get("states", []))

    print(f"\n  Sample query: Vermont bbox {test_bbox}")
    print(f"    → {len(hits)} leaf cells")
    print(f"    → States: {sorted(hit_states)}")
    print(f"    → Expected: VT should be present: {'✓' if 'VT' in hit_states else '✗ MISSING'}")
    print(f"\n  Index OK ✓" if ok else "  Index has issues ✗")


if __name__ == "__main__":
    main()
