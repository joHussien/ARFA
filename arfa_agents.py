"""Constrained semantic agents for ARFA.

ARFA deliberately uses LLMs only for language understanding and evidence synthesis.
All geospatial computation remains in deterministic tools in server.py, structures.py,
and flood_hazard/.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from stage1 import agent_generate, llm_detect, fallback_detect

ALLOWED_FACILITY_TYPES = {"hospitals","schools","community","government","religious","recreation"}
ALLOWED_ACTIONS = {
    "analyze_location", "offer_facilities", "load_facilities", "filter_facilities",
    "query_structures", "set_origin", "generate_routes", "compare_routes",
    "answer_from_context", "repair_missing_structures"
}


def _json_object(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    a, b = text.find("{"), text.rfind("}") + 1
    if a < 0 or b <= a:
        raise ValueError("model returned no JSON object")
    return json.loads(text[a:b])


def _call_json(system: str, payload: dict[str, Any], max_tokens: int = 220) -> dict[str, Any]:
    raw = agent_generate(system, json.dumps(payload, ensure_ascii=False), max_new_tokens=max_tokens)
    return _json_object(raw)


def location_agent(message: str) -> dict[str, Any]:
    """Semantic location extraction only. Census/TIGER resolution stays deterministic."""
    try:
        locs = llm_detect(message)
        if isinstance(locs, dict):
            locs = locs.get("locations", [])
    except Exception:
        fb = fallback_detect(message)
        locs = [] if fb.get("type") == "unknown" else [fb]
    return {"locations": locs or []}


STRUCTURE_SYSTEM = """You are ARFA's Structure Query Agent.
Translate the responder's natural-language request into USA Structures query parameters.
Do NOT answer the flood question and do NOT invent database values.
Return ONLY JSON with this exact shape:
{
  "is_structure_query": true|false,
  "facility_types": ["hospitals"|"schools"|"community"|"government"|"religious"|"recreation"],
  "filters": {
    "occ_cls": [string],
    "prim_occ_keywords": [string],
    "name_keywords": [string],
    "min_height_m": number|null,
    "max_height_m": number|null,
    "min_sqfeet": number|null
  },
  "hazard_relation": "any"|"outside"|"inside",
  "reply": "short description of the interpreted search"
}
Use canonical occupancy classes when evident: Education, Healthcare, Government, Assembly, Commercial, Residential.
Convert feet to meters for height. Do not put null fields into filters. Keep keywords short and literal.
A request to identify probable shelters/facilities counts as a structure query."""


def structure_agent(message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    try:
        out = _call_json(STRUCTURE_SYSTEM, {"message": message, "context": context}, 220)
    except Exception:
        out = _structure_fallback(message)
    out["is_structure_query"] = bool(out.get("is_structure_query"))
    out["facility_types"] = [x for x in (out.get("facility_types") or []) if x in ALLOWED_FACILITY_TYPES]
    filters = out.get("filters") if isinstance(out.get("filters"), dict) else {}
    out["filters"] = {k:v for k,v in filters.items() if v not in (None, [], "")}
    if out.get("hazard_relation") not in {"any","inside","outside"}:
        out["hazard_relation"] = "any"
    return out


def _structure_fallback(message: str) -> dict[str, Any]:
    t = message.lower(); occ=[]; prim=[]; types=[]
    def add(ft, oc=None, keys=()):
        if ft not in types: types.append(ft)
        if oc and oc not in occ: occ.append(oc)
        prim.extend(k for k in keys if k not in prim)
    if re.search(r"school|education|university|college|pre-?k", t): add("schools","Education",("school","education"))
    if re.search(r"hospital|health|medical|clinic", t): add("hospitals","Healthcare",("hospital","health","medical","clinic"))
    if re.search(r"government|courthouse|city hall|municipal", t): add("government","Government",("government","municipal","courthouse"))
    if re.search(r"church|religious|mosque|synagogue|temple|worship", t): add("religious",None,("church","religious","worship"))
    if re.search(r"community|civic|library", t): add("community",None,("community","civic","library"))
    if re.search(r"assembly|recreation|arena|gym", t): add("recreation","Assembly",("recreation","assembly","arena"))
    filters={}
    if occ: filters["occ_cls"]=occ
    if prim: filters["prim_occ_keywords"]=prim
    m=re.search(r"(?:taller|higher|above|over|more than)\s*(\d+(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet)",t)
    if m:
        v=float(m.group(1)); filters["min_height_m"]=v*0.3048 if m.group(2).startswith(("f",)) else v
    m=re.search(r"(?:shorter|lower|below|under|less than)\s*(\d+(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet)",t)
    if m:
        v=float(m.group(1)); filters["max_height_m"]=v*0.3048 if m.group(2).startswith(("f",)) else v
    relation="outside" if re.search(r"outside|unaffected|not flooded|away from flood",t) else "inside" if re.search(r"inside|affected|flooded structures?",t) else "any"
    return {"is_structure_query": bool(types or filters or re.search(r"structure|building|facilit|shelter",t)), "facility_types":types, "filters":filters, "hazard_relation":relation, "reply":"Interpreted structure search"}


CONTROLLER_SYSTEM = """You are ARFA's constrained workflow controller.
Choose exactly ONE next high-level action. You are not a general planner.
Allowed actions: analyze_location, offer_facilities, load_facilities, filter_facilities, query_structures, set_origin, generate_routes, compare_routes, answer_from_context, repair_missing_structures.
Return ONLY JSON: {"action":"...","facility_types":[],"reply":"brief optional reply","reason":"one short reason"}.
Rules:
- New place/current flood situation -> analyze_location.
- Requests for buildings/facilities/schools/hospitals -> query_structures or filter_facilities if structures are already loaded.
- Requests to choose/set responder origin -> set_origin.
- Route request with origin+destination -> generate_routes.
- Ask which existing route is best -> compare_routes.
- If the tool observation reports missing_gdbs (structure database files not found on disk) -> repair_missing_structures. Include the missing state codes in the reply field.
- Do not invent a tool sequence; choose only the next step."""


def controller_agent(message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context=context or {}
    try:
        out=_call_json(CONTROLLER_SYSTEM,{"message":message,"context":context},150)
    except Exception:
        out={"action":"analyze_location","facility_types":[],"reply":"","reason":"fallback"}
    if out.get("action") not in ALLOWED_ACTIONS: out["action"]="analyze_location"
    out["facility_types"]=[x for x in (out.get("facility_types") or []) if x in ALLOWED_FACILITY_TYPES]
    return out


REASON_SYSTEM = """You are ARFA's Evidence Reasoning Agent for emergency-response flood decision support.
Reason ONLY from the compact deterministic tool observations supplied to you. Never invent values, routes, closures, exposure, structural condition, or safety.
Use only source-grounded gauge classifications. Prefer an official NOAA/NWPS flood category when supplied. If a current stage has been compared against official NOAA/NWPS action/minor/moderate/major thresholds, describe it as threshold-derived from NOAA/NWPS thresholds, not as an official current category. If neither an official category nor official thresholds are available, call the gauge unclassified and report the observed stage/discharge without inventing a severity label. Do not create any project-specific gauge severity categories beyond the supplied NOAA/NWPS evidence. Gage height is ft; discharge is cfs.
For facilities, call them candidate/probable facilities, never certified shelters unless evidence explicitly says so.
For routes, compare travel time, HAND flood exposure, and live road incidents separately. Never call a route safe/passable/open solely from terrain screening.
If the observation reports missing_gdbs (structure database files not found on disk), diagnose this clearly: state which states are missing, explain that the USA Structures GDB files for those states are not present on disk, and state that ARFA will now attempt to download and index them automatically. Be direct and operational — this is a data availability problem, not a flood question.
Give concise operational prose: evidence -> interpretation -> next sensible action. Mention uncertainty when present."""


def reasoning_agent(message: str, observation: dict[str, Any], history: list[dict[str, Any]] | None = None) -> str:
    payload={"responder_message":message,"tool_observation":observation,"recent_conversation":(history or [])[-6:]}
    return agent_generate(REASON_SYSTEM,json.dumps(payload,ensure_ascii=False),max_new_tokens=320).strip()
