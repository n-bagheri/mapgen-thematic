# MapGen Tactile Pipeline — v2

Converts area-class / chorochromatic thematic maps (raster images, often scans) into
tactile map masters with braille labeling, a tactile legend, and a reading guide.

**Changes vs. v1:**

- Added **Step 0** (output specification) — the physical medium and page size drive every later decision.
- Added **Step 6** (class aggregation) — merging classes is the primary strategy when a map has more than the available texture slots; dropping to white is the last resort.
- Merged text removal into segmentation (old Steps 4+5): text pixels are filled by growing the surrounding regions, not by generic inpainting.
- Fixed the water contradiction: **water always gets the wavy pattern when present**, so there are 4 thematic slots with water, 5 without.
- Added a second symbol-assignment strategy for **ordered (classed-sequential) data**: texture ramps instead of max-haptic-distance.
- Simplification now happens **in vector space, after segmentation, topology-aware** (shared boundaries simplified once, so no slivers/gaps).
- Added minimum-size generalization driven by physical constants (drop/merge polygons too small to feel; exaggerate critical small features such as islands).
- Defined a structured intermediate artifact per step and three human-review checkpoints.
- Abbreviation collision rule and a clutter-resolution policy for braille labels.
- Scale bar is recomputed for the output scale, never copied from the source.

---

## Step 0 — Output specification (NEW)

Decide before anything else; these values are inputs to Steps 5–12.

- **Medium:** swell/microcapsule paper, braille embosser, or 3D print. Determines
  pattern rendering (line-based vs. dot-based), minimum line width, and resolution.
- **Page size** (e.g., A4 / 11.5×11 in) and orientation.
- **Physical constants** (defaults below; confirm against BANA / local tactile guidelines):

| Constant | Default | Used in |
|---|---|---|
| Braille cell footprint (incl. spacing) | ~6.2 × 10 mm | Steps 8, 12, 13 |
| Min. area for an identifiable texture | ~13 × 13 mm | Steps 5, 6, 7 |
| Min. gap between distinct raised elements | ~3 mm | Steps 5, 8 |
| Min. tactile line width / length | ~1 mm / ~13 mm | Steps 5, 7 |
| Max. distinct area textures per map | Exactly 5 available slots (a ceiling, not a target) | Steps 6, 7 |

The output scale (map units → page mm) is fixed here and never changes downstream.
The pipeline never invents thematic classes to fill unused texture slots: a map
with fewer classes simply uses fewer patterns.

## Step 1 — Semantic interpretation (VLM)

A multimodal model reads the full source image and emits **structured JSON** (schema-validated):

- Map type (chorochromatic / area-class, classed-sequential, other → out of scope flag).
- **Data ordering:** qualitative (unordered classes) vs. ordered (e.g., temperature bands).
  This selects the symbol-assignment strategy in Step 7.
- What the map shows; brief detailed description (feeds the reading guide, Step 14).
- Title (if present), legend presence and entries (labels only — see below).
- Non-thematic content and its priority (water is always priority 1; others ranked by
  relevance to this map).
- Thematic class priorities (tie-break: smaller total area → lower priority).
- Line features and their meaning (borders / rivers / roads / graticule).
- Overlay text expected on the map (city names, river names, region labels) and the capital city if identifiable.

**Rule:** the VLM supplies *semantics only*. Exact colors, coordinates, and bounding
boxes always come from CV/pixel operations (Steps 2–5), because VLMs are unreliable at
pixel-exact work (e.g., near-identical legend blues on the Iran sample).

> **Checkpoint A (human review):** confirm map type, class list, priorities, capital.

## Step 2 — Isolating main map area and legend

Classic CV, seeded by Step 1:

- Detect and crop the **main map area** via connected components / border detection;
  include all islands (components above the minimum-size threshold, plus any the VLM
  flagged as belonging to the mapped territory).
- Detect and crop the **legend box** (if present).
- **Sample legend swatch colors from pixels** (median color per swatch) and pair each
  with its OCR'd label. This produces the authoritative class→color table used as
  segmentation seeds in Step 5. Colors present on the map but absent from the legend
  are recorded as non-thematic candidates.

Artifacts: `map_area.png`, `legend.png`, `classes.json` (label, RGB/Lab color, thematic?, priority).

## Step 3 — Overlay text detection

Scene-text detection + OCR (rotated/curved text supported — river names run along curves):

- Text string, oriented bounding box, and anchor point on the map.
- Classification: thematic area label / city name (capital vs. other — use the point-symbol
  next to the label to confirm city status) / river or line label / other, informed by
  Step 1 semantics.
- Legend text OCR (already used in Step 2) is stored alongside.

Artifact: `labels.json` (text, class, anchor, bbox, priority).

## Step 4 — Segmentation, text removal, and line extraction (merged)

Text removal and segmentation are coupled; generic inpainting smears colors across
boundaries, so:

1. **Mask** all text bboxes from Step 3.
2. **Segment** the unmasked map in a perceptual color space (Lab), using the legend
   swatch colors from Step 2 as cluster seeds; unseeded clusters become non-thematic
   candidates. This absorbs anti-aliasing and JPEG noise instead of enumerating raw colors.
3. **Fill the text mask** by region-growing the surrounding segments into it.
4. **Separate lines from areas** by geometry: thin, elongated components (borders,
   rivers, roads) are skeletonized to centerline polylines and tagged with their meaning
   from Step 1; everything else is polygonized.

Artifact: raster label map + vector `regions.geojson` (polygons with class attribute) and
`lines.geojson` (polylines with type).

## Step 5 — Vector simplification and generalization

Operates on the shared **boundary graph**, not per-polygon:

- Topology-aware simplification (Visvalingam / Douglas–Peucker on shared arcs) so
  neighboring regions stay gap- and sliver-free.
- **Minimum-size generalization** using Step 0 constants at output scale:
  - Polygons below the minimum texture area → merge into the dominant neighbor
    (or drop, if isolated speckle — e.g., the vineyard speckles on the France sample).
  - Critical small features flagged in Step 1 (islands, capital region) → exaggerate to
    the minimum feelable size instead of dropping.
  - Parallel lines closer than the minimum gap → merge or displace.
- Smooth remaining boundaries for clean tactile tracing.

> **Checkpoint B (human review):** overlay simplified vectors on the source; fix
> mis-segmented regions before symbolization.

## Step 6 — Class aggregation (NEW)

Compute available texture slots: **4 if water is present, else 5** (see Step 7).
If thematic classes exceed the slots:

- **Qualitative maps:** the VLM proposes semantic merges (e.g., "Grassland" +
  "Grassland and crops"; specialty crops → "other agriculture"), human confirms.
- **Ordered maps:** re-bin into ≤ slots contiguous bins (e.g., 11 temperature bands → 4).
- **Only if aggregation is impossible:** keep the highest-priority classes and set the
  rest to plain white. Region boundary lines are always embossed (Step 7), so adjacent
  white regions remain separable — but this is the last resort, not the default.

## Step 7 — Tactile symbol assignment

**Areas** (patterns from the 5-group pattern library):

- **Water present:** water gets the wavy pattern — always, regardless of the thematic
  count. The wave group is then closed; the remaining ≤ 4 thematic classes draw one
  pattern each from the other four groups.
- **No water:** ≤ 5 thematic classes get patterns; the wave group contributes only the
  triangular wave.
- **Qualitative data:** group chosen by the meaning of the class (e.g., desert →
  granular, mountains → solid black); pattern within the group chosen to **maximize
  haptic distance** to the patterns already used (requires the pattern-similarity
  matrix — see Open questions).
- **Ordered data:** use a **texture ramp** with perceived ordering (e.g., increasing dot
  density), not max-distance assignment — the reader must feel "more/less", matching the corrected bins from Step 6.
- Non-thematic areas beyond water: patterned only if slots remain, in priority order;
  otherwise plain white.
- **All region boundaries are embossed as lines**, so unpatterned regions stay distinguishable.

**Lines:** distinct tactile line styles for borders / rivers / roads (e.g., solid thick,
solid thin, dashed), respecting the minimum gap to area boundaries.

## Step 8 — Braille labels

- **Abbreviation rule:** first two letters; on collision, first letter + next
  distinguishing consonant; uniqueness enforced in one table; every abbreviation is
  listed in the legend (Step 13).
- Cities are omitted except the **capital**: its 2-letter abbreviation goes in the text-box
  icon at the Step 3 anchor.
- Other overlay labels: 2-letter abbreviation in the text-box icon at their anchors.
- **Clutter resolution** (braille is fixed-size and cannot be scaled): if boxes collide or
  a region is too small to host one, drop labels in ascending priority
  (site labels → region labels → … capital last) until the layout is clean. Avoid tactile
  leader lines — they read as map lines. Everything dropped here is still covered by the
  legend and reading guide.

## Steps 9–12 — Frame and furniture layout

- **9. Map frame/border** placement.
- **10. Scale bar** — recomputed for the output scale from Step 0 (never copied from the
  source; after generalization it is approximate and the guide says so).
- **11. North sign** placement.
- **12. Map title** in braille (space budget: title consumes one braille line height + margin).

Furniture must respect the minimum-gap constant relative to map content.

## Step 13 — Legend creation

- Each used pattern → class name (post-aggregation names from Step 6).
- Each line style → meaning.
- Capital-city symbol and text-box symbol, if used.
- Abbreviation list (abbr → full text).
- **May overflow the map page: multi-page legend is expected and fine** (standard tactile practice).

## Step 14 — Reading guide creation

Merge the Step 1 detailed description with the adaptation record: what was aggregated
(Step 6), what was dropped or exaggerated (Steps 5, 8), what each pattern means, and how
to read the sheet. Output as braille-ready text and/or audio script.

> **Checkpoint C (human review):** final proof of the master + guide before production.

---

## Data model (cross-cutting)

Every step emits a schema-validated artifact (`classes.json`, `labels.json`,
`regions.geojson`, `lines.geojson`, final layout as SVG). Steps are independently
testable, and a human can correct any artifact at the checkpoints without re-running
earlier steps.

## Open questions

1. **Pattern-similarity (haptic distance) matrix** — source it from published
   discriminability studies or run a small user study; Step 7 depends on it.
2. Exact physical constants per chosen medium (Step 0 table holds defaults only).
3. Whether classed-sequential maps (Iran temperature sample) are officially in scope —
   the pipeline now handles them via Steps 6/7, but scope should be explicit.
4. Braille standard/language and contraction grade for labels and legend.
5. Evaluation protocol with tactile readers (at minimum: texture identification,
   region tracing, label lookup tasks).
6. On the France map the model returned water_present: false — defensible, because the sea there is blank white, not a colored polygon. But it's a policy question for us: a tactile France map probably still wants the wavy pattern on the sea so readers can find the coastline. I'd resolve this in Step 6/7 policy (e.g., "coastal territory → treat water as present") rather than by prompt-tweaking; worth deciding before Step 2, and easy to change either way.

