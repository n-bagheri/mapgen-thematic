"""Step 1 -- Semantic interpretation.

A vision-language model reads the whole source image and returns schema-
validated JSON: map type, data ordering, class lists with priorities, lines,
and expected overlay text. Semantics only -- exact colors, coordinates, and
bounding boxes come from CV in Steps 2-5 (color *names* here are hints for
pairing legend swatches to labels, never authoritative values).

The API key is taken from GEMINI_API_KEY / GOOGLE_API_KEY if set, else from
gemini_api.txt in the project root. Model defaults to Gemma 4 (26B A4B);
override with GEMINI_MODEL or the CLI --model flag.
"""

from __future__ import annotations

import json
import mimetypes
import os
from enum import Enum
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, ValidationError, model_validator

AVAILABLE_MODELS = (
    ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite"),
    # Gemma 4 is exposed through the Gemini API by concrete model variant.
    ("gemma-4-26b-a4b-it", "Gemma 4 26B A4B"),
    ("gemma-4-31b-it", "Gemma 4 31B"),
)
DEFAULT_MODEL = "gemma-4-26b-a4b-it"
KEY_FILE = Path(__file__).resolve().parent.parent / "gemini_api.txt"


# --------------------------------------------------------------------------- schema

class MapType(str, Enum):
    area_class = "area_class_chorochromatic"   # qualitative area classes (land use, climate zones, geology)
    choropleth = "choropleth"                  # statistical values per enumeration unit
    isopleth = "isopleth"                      # filled contours derived from a continuous variable
    other = "other"

    @classmethod
    def _missing_(cls, value: object):
        # Read Step 1 artifacts produced before classed_sequential was renamed.
        if value == "classed_sequential":
            return cls.isopleth
        return None


class DataOrdering(str, Enum):
    qualitative = "qualitative"
    ordered = "ordered"


class LineKind(str, Enum):
    border = "border"
    river = "river"
    road = "road"
    coastline = "coastline"
    graticule = "graticule"
    other = "other"


class LegendEntry(BaseModel):
    label: str = Field(description="Legend text for this entry, verbatim")
    color_hint: str = Field(description="Approximate color name as seen (e.g. 'pale yellow'); a hint only")
    is_thematic: bool = Field(description="False for water, no-data, or other non-thematic entries")


class ThematicClass(BaseModel):
    label: str
    priority: int = Field(description="1 = highest. Tie-break: the class covering less area gets the LOWER priority (higher number)")
    approx_area_share_percent: float = Field(description="Rough share of the mapped territory this class covers, 0-100")


class NonThematicFeature(BaseModel):
    name: str = Field(description="e.g. 'sea', 'neighbouring countries', 'no-data area'")
    color_hint: str
    priority: int = Field(description="1 = highest. Water areas are ALWAYS priority 1 when present")
    reason: str = Field(description="Why this feature matters (or not) for a tactile reader of this map")


class LineFeature(BaseModel):
    kind: LineKind
    description: str = Field(description="What the line represents and roughly where it appears")


class OverlayTextExpectation(BaseModel):
    has_city_labels: bool
    capital_city: str | None = Field(description="Name of the capital if it is labelled on the map, else null")
    has_region_labels: bool = Field(description="Labels naming thematic areas or regions")
    has_line_labels: bool = Field(description="Labels along lines, e.g. river names")
    notes: str


class MapSemantics(BaseModel):
    map_type: MapType
    in_scope: bool = Field(
        description="True only for area_class_chorochromatic or isopleth maps"
    )
    data_ordering: DataOrdering
    map_language: str = Field(
        min_length=1,
        description=(
            "Language used for the written text on the map; use a language "
            "name such as English, Persian, or French"
        ),
    )
    subject: str = Field(description="One line: what the map shows and of where")
    description: str = Field(description="Detailed prose description of the map; feeds the reading guide (Step 14)")
    title: str | None = Field(description="Map title verbatim if printed on the image, else null")
    legend_present: bool
    legend_title: str | None = Field(
        default=None,
        description="Heading above the legend swatches, verbatim; null if absent",
    )
    legend_entries: list[LegendEntry]
    water_present: bool = Field(
        description=(
            "True only when water is a separately styled non-thematic area; "
            "false when the mapped theme itself continues across seas or oceans"
        )
    )
    thematic_classes: list[ThematicClass]
    non_thematic: list[NonThematicFeature]
    lines: list[LineFeature]
    overlay_text: OverlayTextExpectation

    @model_validator(mode="before")
    @classmethod
    def _upgrade_cached_semantics(cls, data: object) -> object:
        """Allow pre-language Step 1 artifacts to remain readable downstream."""
        if isinstance(data, dict):
            data = {**data}
            if not data.get("map_language"):
                data["map_language"] = "unknown"
            # Older/imperfect model responses sometimes put a text-only
            # legend heading into legend_entries. Entries are swatch rows by
            # definition; move any leading no-color, non-thematic rows into
            # the dedicated heading field. This also repairs cached artifacts
            # before Step 2 pairs entries with detected swatches.
            entries = list(data.get("legend_entries") or [])
            headings: list[str] = []
            no_color = {"", "none", "n/a", "na", "no color", "text only", "unknown"}
            while entries:
                first = entries[0]
                if not isinstance(first, dict):
                    break
                hint = str(first.get("color_hint") or "").strip().lower()
                if first.get("is_thematic") is not False or hint not in no_color:
                    break
                headings.append(str(first.get("label") or "").strip())
                entries.pop(0)
            if headings:
                data["legend_entries"] = entries
                if not data.get("legend_title"):
                    data["legend_title"] = " ".join(h for h in headings if h)
            # Scope is deterministic. Correct both old cached values and any
            # inconsistent value returned by a model.
            data["in_scope"] = data.get("map_type") in {
                "area_class_chorochromatic", "isopleth", "classed_sequential",
            }
        return data


class MissingLegendError(ValueError):
    """Raised when the pipeline cannot continue without a source legend."""


class OutOfScopeMapError(ValueError):
    """Raised when a downstream step is requested for an unsupported map."""


def require_pipeline_eligible(sem: MapSemantics, step_name: str = "The pipeline") -> MapSemantics:
    """Reject semantics that cannot be consumed by Steps 2 and later."""
    if not sem.in_scope:
        raise OutOfScopeMapError(
            f"{step_name} cannot run: Step 1 classified map type "
            f"'{sem.map_type.value}' as out of scope. Only chorochromatic "
            "and isopleth maps are supported."
        )
    if not sem.legend_present:
        raise MissingLegendError(
            "The tactile-map pipeline cannot continue: no legend was detected; "
            "a visible legend is required to identify and symbolize the map classes."
        )
    return sem


def load_pipeline_semantics(out_dir: Path, step_name: str = "The pipeline") -> MapSemantics:
    """Load Step 1 semantics and enforce the global downstream gate."""
    sem = MapSemantics.model_validate_json(
        (out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    return require_pipeline_eligible(sem, step_name)


# --------------------------------------------------------------------------- prompt

PROMPT = """\
You are the semantic-interpretation stage of a pipeline that converts thematic
maps into tactile maps for blind readers. Analyse the attached map image and
fill the response schema. Rules:

- Report SEMANTICS ONLY. Never estimate pixel coordinates or exact color
  values; color fields are approximate color NAMES used later only as hints.
- map_type: 'choropleth' for values assigned to predefined administrative
  units, regardless of whether the values use ordered bins or sequential
  colors; 'area_class_chorochromatic' for qualitative or nominal area classes
  (such as land use, climate zones, geology, vegetation); 'isopleth' for
  filled-contour maps where areas are ordered bins of one continuous variable
  (e.g. temperature or precipitation ranges) and the area boundaries are
  derived from that variable rather than administrative units.
- Only 'area_class_chorochromatic' and 'isopleth' maps are in scope.
  'choropleth' and 'other' maps are out of scope.
- data_ordering is 'ordered' exactly when the classes form a natural sequence.
- map_language: identify the language used for the written text on the map.
  Read all visible text, especially the title, legend heading, legend labels,
  scale-bar unit, and source note. Return the language name (for example
  "English" or "Persian"), not a script name or country. Use "unknown" only
  when there is no readable natural-language text anywhere in the image.
- legend_title: transcribe the heading that describes the legend (for example
  "Annual Mean Temperature (°C)") separately. It has no color swatch.
- legend_entries: include ONLY rows that have a visible color swatch or symbol,
  transcribed verbatim from top to bottom. Never include "Legend", the legend
  title, units, explanatory headings, or source text as entries. Mark swatch
  rows that are not the mapped theme (water, no data) as is_thematic=false.
- A visible legend is required for this pipeline. Set legend_present=false
  when there is no legend; Step 1 will record that result and the pipeline
  must stop before Step 2.
- thematic_classes: every distinct thematic class shown on the MAP (normally
  the thematic legend entries). Priorities: 1 = most important for
  understanding this map. When two classes are otherwise equal, the one
  covering LESS area gets the lower priority (larger number).
- First decide where the thematic encoding applies. A geographic feature is
  NOT automatically non-thematic. In particular, when the mapped variable's
  legend colors continue across coastlines into oceans or seas, those colored
  ocean/sea areas are thematic data. Do not list ocean/sea as non_thematic in
  that case, and set water_present=false.
- water_present means that water is visibly separated from the theme by its
  own uniform/background styling (for example, a uniform blue sea omitted
  from the thematic legend). It does not merely mean that the mapped extent
  geographically contains an ocean, sea, lake, or river.
- non_thematic: everything visibly styled on the map but not encoded by the
  mapped theme -- separately styled water, surrounding countries, no-data
  regions. Separately styled water is priority 1 when present. Rank the rest
  by how much a tactile reader of this specific map needs them.
- lines: report only explicit geographic linework visibly drawn with a
  distinct solid, dashed, or dotted stroke, such as administrative borders,
  rivers, roads, coastlines, or graticules. A line must be visually
  distinguishable from the edges of adjacent filled areas. Do not infer
  linework from geography or from color transitions. In particular:
  - Report a coastline only when a separate visible stroke outlines the coast;
    a land-water color boundary alone is not a line.
  - Report an administrative border only when it has a distinct visible
    stroke; a boundary between differently colored areas alone is not a line.
  - Report a graticule only when latitude or longitude lines extend through
    the map area; coordinate labels and tick marks around the frame do not
    constitute a graticule.
  - Do not report thematic-class edges, contour-fill boundaries, the map
    frame/neatline, tick marks, legend boxes, scale bars, north-arrow strokes,
    or text strokes.
  - If the map contains no explicit geographic linework, return an empty
    lines list.
- overlay_text: describe what kinds of text sit ON the map area. Name the
  capital city only if it is actually labelled on the map.
- description: a thorough prose description a sighted person would give a
  blind colleague: territory shown, spatial arrangement of the classes, where
  each dominates, notable patterns, islands, and anything unusual.
- If the image is not a thematic map of these kinds, set in_scope=false and
  still describe what you see.
"""


# --------------------------------------------------------------------------- runner

MAX_IMAGE_BYTES = 19_000_000  # stay under the 20 MB inline-data limit
API_TIMEOUT_MS = 360_000  # Gemma image/schema requests can take about five minutes


def _ensure_api_key() -> None:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return
    if KEY_FILE.exists():
        os.environ["GEMINI_API_KEY"] = KEY_FILE.read_text(encoding="utf-8").strip()
        return
    raise RuntimeError(f"GEMINI_API_KEY is not set and {KEY_FILE} does not exist")


def generate_json(contents, schema, model: str | None = None, temperature: float = 0.2,
                  retries: int = 1, status: Callable[[str], None] | None = None):
    """Schema-enforced model call with a deadline and same-model retries."""
    import re as _re
    import time as _time

    from google import genai
    from google.genai import types

    _ensure_api_key()
    resolved = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    is_gemma = resolved.startswith("gemma-")
    request_contents = contents
    if is_gemma:
        # Gemma's server-side response_schema path can stall and eventually
        # return an internal error for this nested schema. Supplying the same
        # schema as text returns promptly; Pydantic still validates it below.
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        schema_instruction = (
            "Return ONLY one valid JSON object matching this JSON Schema exactly:\n"
            + schema_text
        )
        request_contents = list(contents) if isinstance(contents, (list, tuple)) else [contents]
        request_contents.append(schema_instruction)
    client = genai.Client(http_options=types.HttpOptions(timeout=API_TIMEOUT_MS))
    last: Exception | None = None
    if status:
        status(f"requesting model {resolved} (timeout {API_TIMEOUT_MS // 1000}s)")
    try:
        for attempt in range(retries + 1):
            try:
                config_args = {
                    "response_mime_type": "application/json",
                    # If a model produced malformed JSON, make the retry fully
                    # deterministic to reduce formatting drift.
                    "temperature": 0.0 if attempt else temperature,
                }
                if is_gemma:
                    config_args["max_output_tokens"] = 8192
                else:
                    config_args["response_schema"] = schema
                response = client.models.generate_content(
                    model=resolved,
                    contents=request_contents,
                    config=types.GenerateContentConfig(**config_args),
                )
                parsed = response.parsed
                if not isinstance(parsed, schema):
                    parsed = schema.model_validate_json(response.text)
                return parsed
            except Exception as exc:  # noqa: BLE001 - classify API failures
                last = exc
                msg = str(exc)
                upper = f"{type(exc).__name__} {msg}".upper()
                code = getattr(exc, "code", None)
                rate_limited = code == 429 or "RESOURCE_EXHAUSTED" in upper or "429" in upper
                timed_out = code == 408 or "TIMEOUT" in upper or "TIMED OUT" in upper
                server_error = code in {500, 502, 503, 504} or any(
                    marker in upper for marker in ("INTERNAL", "UNAVAILABLE", "SERVICE UNAVAILABLE"))
                malformed = (
                    isinstance(exc, (ValidationError, json.JSONDecodeError))
                    or "JSON_INVALID" in upper
                    or "INVALID JSON" in upper
                )
                if not (rate_limited or timed_out or server_error or malformed):
                    raise
                if status:
                    reason = (
                        "rate limited" if rate_limited
                        else "timed out" if timed_out
                        else "returned malformed JSON" if malformed
                        else "service error"
                    )
                    status(f"{resolved} {reason} on attempt {attempt + 1}")
                if attempt >= retries:
                    break
                if rate_limited:
                    m = _re.search(r"retry in (\d+(?:\.\d+)?)s", msg, flags=_re.IGNORECASE)
                    delay = min(70.0, (float(m.group(1)) + 2.0) if m else 30.0)
                else:
                    delay = 0.0 if malformed else 2.0
                if status:
                    status(
                        f"retrying {resolved}"
                        + (f" in {delay:g}s" if delay else "")
                    )
                if delay:
                    _time.sleep(delay)
    finally:
        client.close()

    if last is not None:
        raise last
    raise RuntimeError(f"{resolved} returned no response")


def interpret_map(image_path: Path, model: str | None = None,
                  status: Callable[[str], None] | None = None) -> MapSemantics:
    """Run Step 1 on one map image and return validated semantics."""
    from google.genai import types

    data = image_path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{image_path.name} is {len(data)/1e6:.1f} MB; downscale it below 19 MB first")
    mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"

    semantics = generate_json(
        [types.Part.from_bytes(data=data, mime_type=mime), PROMPT],
        MapSemantics, model=model, status=status)
    return postprocess(semantics)


def postprocess(sem: MapSemantics) -> MapSemantics:
    """Enforce invariants the model may get wrong; deterministic, no AI."""
    sem.in_scope = sem.map_type in (MapType.area_class, MapType.isopleth)

    # ``water_present`` means separately styled non-thematic water, not merely
    # that a sea or ocean is geographically visible.  Resolve contradictory
    # model output in favour of that explicit boolean so thematic ocean data
    # can never be turned into a synthetic water class downstream.
    water_words = ("water", "sea", "ocean", "lake", "gulf")
    waters = [f for f in sem.non_thematic if any(w in f.name.lower() for w in water_words)]
    if not sem.water_present:
        sem.non_thematic = [f for f in sem.non_thematic if f not in waters]
    elif not waters:
        sem.non_thematic.insert(0, NonThematicFeature(
            name="water", color_hint="blue", priority=1,
            reason="added by postprocess: separately styled water was reported",
        ))
    for f in sem.non_thematic:
        if any(w in f.name.lower() for w in water_words):
            f.priority = 1

    # Deterministic order; resolve duplicate priorities by area (smaller -> lower priority).
    sem.thematic_classes.sort(key=lambda c: (c.priority, -c.approx_area_share_percent))
    for i, c in enumerate(sem.thematic_classes, start=1):
        c.priority = i
    sem.non_thematic.sort(key=lambda f: f.priority)

    return sem


def save_semantics(sem: MapSemantics, image_path: Path, runs_dir: Path = Path("runs")) -> Path:
    out_dir = runs_dir / image_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "step1_semantics.json"
    out.write_text(sem.model_dump_json(indent=2), encoding="utf-8")
    return out


def semantics_artifact_is_current(path: Path) -> bool:
    """Return whether Step 1 produced a complete, schema-valid artifact.

    ``unknown`` is a valid semantic result when the source contains no readable
    natural-language text.  Legacy artifacts that do not contain the
    ``map_language`` field still need one rerun; check the raw JSON before
    validation because MapSemantics upgrades those artifacts for downstream
    compatibility.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False
        language = raw.get("map_language")
        if not isinstance(language, str) or not language.strip():
            return False
        sem = MapSemantics.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError):
        return False
    return True
