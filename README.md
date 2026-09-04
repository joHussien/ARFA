<img width="1869" height="1066" alt="image3" src="https://github.com/user-attachments/assets/2e690f19-f9ab-4d20-8114-0b428b7c48ea" /><img width="1869" height="1066" alt="image3" src="https://github.com/user-attachments/assets/83dea35a-3067-4603-b256-fcd2e0d85812" /># ARFA: An Agentic System for Real-Time Riverine Flood Response

ARFA is a hybrid agentic decision-support system developed for the **ACM SIGSPATIAL OASIS 2026 Challenge**. It connects natural-language responder requests to real-time hydrologic, flood-hazard, infrastructure, routing, and road-condition analysis.

ARFA is designed around a simple separation of responsibilities: **LLM agents interpret requests and explain evidence; deterministic geospatial tools perform the measurements and spatial computations.** The system supports flood assessment, candidate-facility discovery, and multi-criteria evacuation routing while keeping safety-sensitive decisions with the responder.

## Authors

- Youssef Hussein - University of Minnesota
- JangHyeon Lee - University of Minnesota
- Oishee Bintey Hoque - University of Virginia
- Dalton Lunga - Oak Ridge National Laboratory (mentor)

## What ARFA Does

ARFA supports three end-to-end flood-response workflows:

1. **Flood assessment** - resolve a location, retrieve USGS gauge observations and NOAA/NWPS flood information, display NWM inundation, and derive terrain-based HAND flood screening.
2. **Candidate facility discovery** - query the national USA Structures inventory and apply semantic and spatial filters such as facility type and relationship to modeled flood areas.
3. **Evacuation routing** - generate route alternatives with OSRM and present travel time, HAND flood exposure, and reported TomTom road incidents as separate criteria.

ARFA does **not** label a route universally safe or a candidate facility a certified shelter. HAND is used as terrain-based flood screening rather than a hydraulic inundation forecast, and reported road incidents may not capture every closure or hazard.

## ARFA Demo

### Demo1. ARFA assessing flood possibility in Vermont.
<img width="1862" height="1069" alt="image1" src="https://github.com/user-attachments/assets/de0512cf-fd74-4d6e-8b80-b0a322aff90f" />

### Demo2. ARFA Retrieving Schools from USA Structures Data
<img width="1860" height="1075" alt="image2" src="https://github.com/user-attachments/assets/5a7cabd9-1eec-4c7e-b882-f0c19bc93495" />


### Demo3. Evacuation Routing from A Responder to B Shelter
<img width="1869" height="1066" alt="image3" src="https://github.com/user-attachments/assets/95dd7528-06c5-4fbe-be1d-d7dda272e0fd" />




## Architecture

ARFA contains three primary layers:

- **Multi-agent layer** - location understanding, facility-query interpretation, and evidence reasoning. The implementation also contains a constrained workflow controller used to dispatch the next system action.
- **Deterministic geospatial tool layer** - Census/TIGERweb, USGS, NOAA/NWPS, NWM inundation, USGS 3DEP, NHDPlus HR, USA Structures, OSRM, and TomTom operations.
- **Web responder interface** - natural-language interaction, interactive maps, gauges, structures, flood layers, route alternatives, and intermediate tool outputs.

The three task-facing agents are implemented in `arfa_agents.py`:

| Component | Role |
|---|---|
| `location_agent` | Converts a location request into structured geographic intent. |
| `structure_agent` | Converts a facility request into structured USA Structures filters. |
| `reasoning_agent` | Summarizes tool outputs without performing the underlying spatial computation. |
| `controller_agent` | Internal workflow dispatcher that selects the next supported action. |

## Repository Structure

```text
ARFA/
├── arfa_agents.py              # Agent prompts and constrained agent functions
├── stage1.py                   # LLM backend and location-resolution workflow
├── server.py                   # Flask API and orchestration
├── structures.py               # USA Structures query engine + pyramid-index access
├── structures_autorepair.py    # Missing-state recovery orchestration
├── download_usa_structures.py  # FEMA/ORNL state-package downloader
├── build_index.py              # USA Structures pyramid-index builder
├── flood_hazard/               # DEM, HAND, hydrography, exposure, route-risk tools
├── USA_Structures_Index/       # Lightweight spatial-index metadata
├── static/                     # Frontend JavaScript and CSS
├── templates/                  # Web interface
├── requirements.txt
└── run_arfa.sh
```

### Why there are two USA Structures modules

`structures.py` and `structures_autorepair.py` are **not duplicate implementations**. `structures.py` performs normal indexed structure queries. `structures_autorepair.py` is only invoked when required state geodatabases are missing; it calls `download_usa_structures.py`, rebuilds the pyramid index, and hot-reloads the index. Keeping recovery separate prevents download/index-maintenance logic from being mixed into the query engine.

## Installation

Create a Python environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Optional API keys

Set keys through environment variables. **Do not commit API keys to the repository.**

```bash
export ARFA_GEMINI_KEY="your-key"
export ARFA_TOMTOM_KEY="your-key"
```

Without a Gemini key, ARFA can use the configured local Hugging Face fallback. TomTom incident functionality requires a TomTom key.

## USA Structures Setup

USA Structures geodatabases are intentionally not committed because they are large. ARFA includes the lightweight pyramid index metadata and can acquire missing state packages at runtime.

By default, the project uses a local data directory:

```text
./Data_USA_Structures/2025_06/
```

You can override it:

```bash
export ARFA_STRUCTURES_GDB_DIR=/path/to/Data_USA_Structures/2025_06
export ARFA_STRUCTURES_INDEX=./USA_Structures_Index
```

To download states manually:

```bash
python download_usa_structures.py \
  --output ./Data_USA_Structures/2025_06 \
  --states VT IN CA \
  --delete-zips
```

Then rebuild the index if needed:

```bash
python build_index.py \
  --data-dir ./Data_USA_Structures/2025_06 \
  --index-dir ./USA_Structures_Index
```

### Automatic missing-state recovery

If a viewport query intersects a state whose GDB is absent, the structures stream reports the missing state code. ARFA then starts the recovery workflow, downloads only the missing state package(s), extracts them, rebuilds the pyramid index, and hot-reloads it without restarting the server.

The responsibilities are intentionally separated:

```text
structures.py
    -> detects missing state GDB
structures_autorepair.py
    -> coordinates recovery
 download_usa_structures.py
    -> downloads/extracts requested state package
 build_index.py
    -> rebuilds pyramid index
```

## Run ARFA

```bash
python server.py
```

or:

```bash
bash run_arfa.sh
```

Then open:

```text
http://localhost:5050
```

The port can be changed with `ARFA_PORT`.

## Example Queries

```text
What is the current flooding situation in Chittenden County, Vermont?
```

```text
Identify schools in this area that can be used as probable shelters.
```

```text
Find schools outside the flood area.
```

For routing, select the responder origin and a returned candidate facility in the interface. ARFA generates alternatives and keeps travel time, modeled flood exposure, and reported road incidents separate for responder assessment.

## Main Data and Services

| Source | Purpose |
|---|---|
| USGS NWIS | Current gauge observations and stage/flow history |
| NOAA/NWPS | Flood categories and published thresholds |
| NOAA National Water Model | Analysis inundation polygons |
| USGS 3DEP | Elevation data for terrain analysis |
| NHDPlus HR | Hydrography for HAND drainage reference |
| Census TIGERweb | Location/FIPS and geographic boundaries |
| USA Structures (ORNL/FEMA) | National building inventory and facility attributes |
| OSRM | Road-network route generation |
| TomTom Traffic | Reported traffic incidents and road-condition context |

## Human-in-the-Loop and Safety

ARFA is a **decision-support system, not a decision-maker**. It presents evidence and alternatives rather than evacuation directives. Travel time, modeled HAND flood exposure, and reported road incidents remain separate criteria so responders can apply local knowledge and operational priorities. Candidate facilities are treated as probable/candidate shelters unless authoritative shelter information is available.

The system is designed to degrade gracefully when a service or dataset is unavailable: missing measurements are reported as unavailable rather than replaced with invented values.

## Limitations

- HAND represents terrain-based riverine flood screening, not a complete hydraulic inundation forecast.
- External services can be unavailable, rate-limited, or incomplete.
- Reported traffic incidents do not guarantee that an unreported road is passable.
- USA Structures candidate facilities are not equivalent to certified emergency shelters.
- Large-area DEM/HAND processing and first-time state downloads can take significant time.
- Current data coverage is focused on the United States and territories supported by the underlying federal/national datasets.

## Paper

**ARFA: An Agentic System for Real-Time Riverine Flood Response**  
Youssef Hussein, JangHyeon Lee, Oishee Bintey Hoque, and Dalton Lunga.  
ACM SIGSPATIAL OASIS Challenge, Riverside, California, November 2026.

## Acknowledgment

This work was developed during research internships at Oak Ridge National Laboratory. The accompanying paper contains the applicable UT-Battelle / U.S. Department of Energy acknowledgment and publication notice.
