#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ARFA_STRUCTURES_INDEX="${ARFA_STRUCTURES_INDEX:-$PWD/USA_Structures_Index}"
export ARFA_STRUCTURES_GDB_DIR="${ARFA_STRUCTURES_GDB_DIR:-$PWD/Data_USA_Structures/2025_06}"
export ARFA_PORT="${ARFA_PORT:-5050}"
python server.py --port "$ARFA_PORT"
