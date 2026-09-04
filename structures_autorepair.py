"""
structures_autorepair.py — On-demand download and index rebuild for missing USA Structures GDBs.

Called by the /api/structures/repair endpoint when the stream returns missing_gdbs.
Downloads only the requested states, then rebuilds the pyramid index and hot-reloads
it into the running server without a restart.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# ── Repair job state (one job at a time) ──────────────────────────────────────
_repair_lock   = threading.Lock()
_repair_status: dict = {"running": False, "states": [], "log": [], "error": None, "done": False}


def get_status() -> dict:
    return dict(_repair_status)


def _log(msg: str, cb: Callable | None = None):
    print(f"[autorepair] {msg}")
    _repair_status["log"].append(msg)
    if cb:
        cb(msg)


def repair_states(state_codes: list[str], progress_cb: Callable | None = None) -> dict:
    """
    Download the missing state GDB(s) and rebuild + reload the index.

    Returns a dict:
      { "ok": bool, "states": [...], "log": [...], "error": str|None }

    Runs synchronously — call from a background thread in the Flask endpoint.
    """
    global _repair_status

    if not state_codes:
        return {"ok": False, "error": "No state codes supplied", "log": [], "states": []}

    state_codes = [s.upper().strip() for s in state_codes]

    # Determine output (GDB root) and index directories from structures module
    try:
        import structures as _st
        gdb_root = _st._gdb_dir
        index_dir = str(_st._index_dir) if _st._index_dir else None
    except Exception as e:
        return {"ok": False, "error": f"Cannot read structures config: {e}", "log": [], "states": state_codes}

    if not gdb_root:
        return {
            "ok": False,
            "error": (
                "ARFA_STRUCTURES_GDB_DIR is not set and could not be inferred from the index. "
                "Set the environment variable and restart."
            ),
            "log": [],
            "states": state_codes,
        }

    if not index_dir:
        return {"ok": False, "error": "Structures index directory unknown.", "log": [], "states": state_codes}

    log = []

    def _cb(msg):
        log.append(msg)
        if progress_cb:
            progress_cb(msg)

    _log(f"Starting auto-repair for states: {state_codes}", _cb)
    _log(f"GDB root: {gdb_root}", _cb)
    _log(f"Index dir: {index_dir}", _cb)

    # ── Step 1: Download missing GDB(s) ──────────────────────────────────────
    try:
        _log("Discovering FEMA package list…", _cb)

        import download_usa_structures as dl
        import requests
        session = requests.Session()
        session.headers.update({"User-Agent": "ARFA-AutoRepair/1.0", "Accept": "*/*"})

        packages = dl.discover_packages(session)
        wanted   = set(state_codes)
        to_fetch = [p for p in packages if p["code"] in wanted]
        found    = {p["code"] for p in to_fetch}
        unknown  = wanted - found

        if unknown:
            _log(f"WARNING: these states are not on FEMA's page: {sorted(unknown)}", _cb)

        output_dir = Path(gdb_root)
        output_dir.mkdir(parents=True, exist_ok=True)

        for pkg in to_fetch:
            _log(f"Downloading {pkg['code']} — {pkg['name']} …", _cb)
            try:
                dl.process_package(session, pkg, output_dir, force=False, delete_zips=True)
                _log(f"{pkg['code']} downloaded and extracted.", _cb)
            except Exception as e:
                _log(f"ERROR downloading {pkg['code']}: {e}", _cb)
                return {"ok": False, "error": str(e), "log": log, "states": state_codes}

    except Exception as e:
        _log(f"Download phase failed: {e}", _cb)
        return {"ok": False, "error": str(e), "log": log, "states": state_codes}

    # ── Step 2: Rebuild the pyramid index ────────────────────────────────────
    try:
        _log("Rebuilding pyramid index…", _cb)

        import build_index as bi
        import sys as _sys
        _old_argv = _sys.argv
        _sys.argv = [
            "build_index.py",
            "--data-dir",  gdb_root,
            "--index-dir", index_dir,
        ]
        try:
            bi.main()
        finally:
            _sys.argv = _old_argv
        _log("Pyramid index rebuilt.", _cb)

    except Exception as e:
        _log(f"Index rebuild failed: {e}", _cb)
        return {"ok": False, "error": str(e), "log": log, "states": state_codes}

    # ── Step 3: Hot-reload index into the running server ─────────────────────
    try:
        import structures as _st
        ok = _st.reload_index()
        if ok:
            _log("Index hot-reloaded. Structures data is now available.", _cb)
        else:
            _log("WARNING: reload_index() returned False — check index files.", _cb)
    except Exception as e:
        _log(f"Hot-reload failed: {e}", _cb)
        return {"ok": False, "error": str(e), "log": log, "states": state_codes}

    return {"ok": True, "error": None, "log": log, "states": state_codes}


def start_repair_background(state_codes: list[str]) -> bool:
    """
    Launch repair in a background thread. Returns False if a repair is already running.
    Poll /api/structures/repair/status for progress.
    """
    global _repair_status

    if not _repair_lock.acquire(blocking=False):
        return False

    _repair_status = {
        "running": True,
        "states": state_codes,
        "log": [],
        "error": None,
        "done": False,
        "started_at": time.time(),
    }

    def _run():
        global _repair_status
        try:
            result = repair_states(state_codes)
            _repair_status.update({
                "running": False,
                "done": True,
                "ok": result["ok"],
                "error": result.get("error"),
                "log": result.get("log", []),
                "finished_at": time.time(),
            })
        finally:
            _repair_lock.release()

    t = threading.Thread(target=_run, daemon=True, name="arfa-autorepair")
    t.start()
    return True
