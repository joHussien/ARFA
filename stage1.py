"""
stage1.py — LLM interface for ARFA.

Primary backend: Google Gemini API (free tier via Google AI Studio).
Fallback:        Local HuggingFace model (loaded lazily if no Gemini key).

Set ARFA_GEMINI_KEY in your environment or .env file.
Get a free key at: https://aistudio.google.com/app/apikey  (no credit card needed)

Model priority:
  1. Gemini 2.0 Flash  (fast, free, GPT-4o quality for structured tasks)
  2. Gemini 1.5 Flash  (slightly older, still excellent)
  3. Local HF model    (ARFA_HF_MODEL env var, default Llama-3.2-3B-Instruct)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ── Gemini client (pure urllib, no extra SDK needed) ─────────────────────────

GEMINI_KEY   = os.getenv("ARFA_GEMINI_KEY", "")
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models"
# Model cascade: try Flash 2.0 first, fall back to 1.5 Flash
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",       # best working — newest lite
    "gemini-3.1-flash-lite",       # fallback
    "gemini-flash-lite-latest",    # alias fallback
    "gemini-3.1-flash-lite-preview",
]
HDR = {"Content-Type": "application/json", "User-Agent": "ARFA/1.0"}


def _gemini_call(system: str, user: str, max_tokens: int = 400) -> str:
    """Call Gemini generateContent. Returns model text or raises RuntimeError."""
    if not GEMINI_KEY:
        raise RuntimeError("No ARFA_GEMINI_KEY set")

    body = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.0,           # deterministic for structured outputs
            "candidateCount": 1,
        },
    }).encode()

    last_err = None
    for model in GEMINI_MODELS:
        url = f"{GEMINI_BASE}/{model}:generateContent?key={GEMINI_KEY}"
        req = urllib.request.Request(url, data=body, headers=HDR, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            # Extract text from response
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"No candidates in Gemini response: {data}")
            parts = candidates[0].get("content", {}).get("parts", [])
            # Some models prepend a 'thought' part — extract only text parts
            text = "".join(
                p.get("text", "") for p in parts
                if p.get("text") and not p.get("thought")
            ).strip()
            if text:
                return text
            raise RuntimeError("Empty text from Gemini")
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()[:200]
            last_err = f"{model}: HTTP {e.code}"
            if e.code == 404:                  # model not on this account → try next silently
                continue
            if e.code in (429, 503):           # rate limit / overload → brief pause + try next
                time.sleep(1)
                continue
            raise RuntimeError(f"{last_err} — {body_err}")
        except Exception as exc:
            last_err = f"{model}: {exc}"
            continue

    raise RuntimeError(f"All Gemini models failed. Last: {last_err}")


def _extract_json_obj(text: str) -> dict:
    """Extract first balanced {...} from text, tolerating markdown fences."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError(f"No JSON object in: {text[:200]}")
    return json.loads(text[start:end])


# ── Location detection system prompt ─────────────────────────────────────────

SYSTEM = """You are a US geography expert. Given a user query about flood risk or vulnerability,
identify ALL locations mentioned and classify each one.

Return ONLY valid JSON:
{
  "locations": [
    {"type": "city" | "county" | "state", "name": "<name>", "state": "<2-letter abbrev>"},
    ...
  ]
}
Rules:
- "city": user mentions a city or neighborhood (e.g. "Houston", "Los Angeles", "Duluth")
- "county": user mentions a county (e.g. "Harris County", "Chittenden County")
- "state": user mentions a whole state (e.g. "Vermont", "Texas", "Florida")
- "name": strip "County", "City" etc. Just the base name.
- "state": always the 2-letter abbreviation (TX, CA, VT, MN...)
- If only one location, still return a list with one item.
- If you cannot identify a clear US location, return {"locations": []}."""


# ── Local HF model (lazy, only loaded if no Gemini key) ──────────────────────

DEFAULT_MODEL = os.getenv("ARFA_HF_MODEL", "meta-llama/Llama-3.2-3B-Instruct")
_hf_cache: dict = {}


def _hf_load():
    """Load the local HF model once and cache it."""
    if "pipe" in _hf_cache:
        return _hf_cache["pipe"]
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    model_id = DEFAULT_MODEL
    print(f"[llm] loading local model {model_id}…")
    gpu = os.getenv("ARFA_DEVICE", "0")
    device = f"cuda:{gpu}" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16 if device.startswith("cuda") else torch.float32,
    ).to(device)
    _hf_cache["pipe"] = (model, tokenizer)
    print(f"[llm] local model ready on {device}")
    return _hf_cache["pipe"]


def _hf_call(system: str, user: str, max_new_tokens: int = 200) -> str:
    """Call local HF model. Returns generated text."""
    import torch
    model, tokenizer = _hf_load()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    # Left-truncate so the instruction tail is never cut off
    prev_side = getattr(tokenizer, "truncation_side", "right")
    tokenizer.truncation_side = "left"
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=6144).to(model.device)
    tokenizer.truncation_side = prev_side
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            repetition_penalty=1.1,
            pad_token_id=tokenizer.eos_token_id,
        )
    return tokenizer.decode(output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()


# ── Public interface ──────────────────────────────────────────────────────────

def llm_detect(query: str) -> list:
    """
    Detect US location(s) from free text. Returns a list of location dicts.
    Uses Gemini if ARFA_GEMINI_KEY is set, otherwise falls back to local model.
    """
    use_gemini = bool(GEMINI_KEY)
    try:
        if use_gemini:
            raw = _gemini_call(SYSTEM, query, max_tokens=150)
        else:
            raw = _hf_call(SYSTEM, query, max_new_tokens=100)
        parsed = _extract_json_obj(raw)
        locs = parsed.get("locations", [])
        if isinstance(locs, list):
            return locs
        # Single-object fallback (old format)
        if parsed.get("type"):
            return [parsed]
        return []
    except Exception as exc:
        backend = "Gemini" if use_gemini else "local model"
        print(f"[llm_detect] {backend} failed: {exc}")
        return []


def agent_generate(system_prompt: str, user_prompt: str, max_new_tokens: int = 320) -> str:
    """
    Generate a response from the active LLM backend.
    Used by arfa_agents.py for structure query interpretation and evidence synthesis.
    Gemini primary → local HF fallback.
    """
    # Gemini path
    if GEMINI_KEY:
        try:
            return _gemini_call(system_prompt, user_prompt, max_tokens=max_new_tokens)
        except Exception as exc:
            print(f"[agent_generate] Gemini failed ({exc}), falling back to local model")

    # Local model fallback
    try:
        return _hf_call(system_prompt, user_prompt, max_new_tokens=max_new_tokens)
    except Exception as exc:
        print(f"[agent_generate] local model also failed: {exc}")
        return ""


# ── Regex fallback (no LLM needed) ───────────────────────────────────────────

def fallback_detect(query: str) -> dict:
    """Pure-regex location detection. Used when both LLM backends are unavailable."""
    states = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
        "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
        "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
        "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
        "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
        "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
        "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
        "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
        "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
        "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
        "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    }
    abbrevs = {v: v for v in states.values()}
    q_lower = query.lower()

    for name, abbrev in states.items():
        if re.search(rf"\b{re.escape(name)}\b", q_lower):
            if re.search(r"\bcounty\b", q_lower):
                m = re.search(r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+county", query, re.I)
                county_name = m.group(1).strip() if m else name.title()
                return {"type": "county", "name": county_name, "state": abbrev}
            m = re.match(r"^.*?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s+" + re.escape(name), query, re.I)
            if m:
                return {"type": "city", "name": m.group(1).strip(), "state": abbrev}
            return {"type": "state", "name": name.title(), "state": abbrev}

    m = re.search(r"\b([A-Z]{2})\b", query)
    if m and m.group(1) in abbrevs:
        abbrev = m.group(1)
        before = query[:m.start()].strip(" ,")
        name = re.sub(r"\bcounty\b", "", before, flags=re.I).strip()
        loc_type = "county" if re.search(r"\bcounty\b", before, re.I) else "city"
        return {"type": loc_type, "name": name, "state": abbrev}

    return {"type": "unknown", "name": "", "state": ""}


# ── Census API helpers (unchanged) ────────────────────────────────────────────

def _get_json(url: str, params: dict | None = None, timeout: int = 30) -> dict:
    if params:
        url = url + "?" + urllib.parse.urlencode(params, safe=",:")
    req = urllib.request.Request(url, headers={"User-Agent": "ARFA-stage1/0.1",
                                                "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


STATE_FIPS_CACHE: dict = {}

def get_state_fips(abbrev: str) -> str | None:
    if abbrev in STATE_FIPS_CACHE:
        return STATE_FIPS_CACHE[abbrev]
    payload = _get_json(
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "State_County/MapServer/0/query",
        {"f": "json", "where": f"STUSAB='{abbrev.upper()}'",
         "outFields": "STATE,STUSAB", "returnGeometry": "false", "resultRecordCount": 1},
    )
    feats = payload.get("features") or []
    if not feats:
        return None
    fips = str(feats[0]["attributes"]["STATE"]).zfill(2)
    STATE_FIPS_CACHE[abbrev] = fips
    return fips


def get_county_fips(county_name: str, state_fips: str) -> list[dict]:
    name = county_name.strip().replace("'", "''")
    payload = _get_json(
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "State_County/MapServer/1/query",
        {"f": "json", "where": f"UPPER(NAME) LIKE UPPER('%{name}%') AND STATE='{state_fips}'",
         "outFields": "NAME,STATE,COUNTY", "returnGeometry": "false", "resultRecordCount": 10},
    )
    return [{"name": f["attributes"]["NAME"],
             "state": str(f["attributes"]["STATE"]).zfill(2),
             "county": str(f["attributes"]["COUNTY"]).zfill(3)}
            for f in payload.get("features") or []]


def city_to_counties(city_name: str, state_fips: str) -> list[dict]:
    name = city_name.strip().replace("'", "''")
    payload = _get_json(
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Current/MapServer/28/query",
        {"f": "json", "where": f"UPPER(NAME) LIKE UPPER('%{name}%') AND STATE='{state_fips}'",
         "outFields": "NAME,STATE,PLACE", "returnGeometry": "true",
         "outSR": "4326", "resultRecordCount": 5},
    )
    feats = payload.get("features") or []
    if not feats:
        return get_county_fips(city_name, state_fips)
    rings = feats[0].get("geometry", {}).get("rings") or []
    if not rings:
        return get_county_fips(city_name, state_fips)
    pts = rings[0]
    lons = [p[0] for p in pts]; lats = [p[1] for p in pts]
    envelope = json.dumps({
        "xmin": min(lons), "ymin": min(lats), "xmax": max(lons), "ymax": max(lats),
        "spatialReference": {"wkid": 4326}
    })
    county_payload = _get_json(
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Current/MapServer/82/query",
        {"f": "json", "geometry": envelope, "geometryType": "esriGeometryEnvelope",
         "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
         "where": f"STATE='{state_fips}'", "outFields": "NAME,STATE,COUNTY",
         "returnGeometry": "false", "resultRecordCount": 10},
    )
    counties = [{"name": f["attributes"]["NAME"],
                 "state": str(f["attributes"]["STATE"]).zfill(2),
                 "county": str(f["attributes"]["COUNTY"]).zfill(3)}
                for f in county_payload.get("features") or []]
    return counties or get_county_fips(city_name, state_fips)


def get_all_counties_in_state(state_fips: str) -> list[dict]:
    payload = _get_json(
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Current/MapServer/82/query",
        {"f": "json", "where": f"STATE='{state_fips}'",
         "outFields": "NAME,STATE,COUNTY", "returnGeometry": "false", "resultRecordCount": 300},
    )
    return [{"name": f["attributes"]["NAME"],
             "state": str(f["attributes"]["STATE"]).zfill(2),
             "county": str(f["attributes"]["COUNTY"]).zfill(3)}
            for f in payload.get("features") or []]


def get_tracts_for_county(state_fips: str, county_fips: str) -> list[dict]:
    payload = _get_json(
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Current/MapServer/8/query",
        {"f": "geojson", "where": f"STATE='{state_fips}' AND COUNTY='{county_fips}'",
         "outFields": "GEOID,STATE,COUNTY,TRACT,NAME",
         "returnGeometry": "true", "outSR": "4326", "resultRecordCount": 2000},
        timeout=60,
    )
    return payload.get("features") or []


# ── CLI (for testing) ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ARFA stage1 — location detection test")
    parser.add_argument("query", nargs="?", default="flooding in Vigo County Indiana")
    args = parser.parse_args()

    print(f"Query: {args.query}")
    print(f"Gemini key: {'SET' if GEMINI_KEY else 'NOT SET — will use local model'}")
    locs = llm_detect(args.query)
    if locs:
        print(f"Detected: {locs}")
    else:
        fb = fallback_detect(args.query)
        print(f"Fallback: {fb}")


if __name__ == "__main__":
    main()