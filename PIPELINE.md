# MapGen Tactile Pipeline — v2

Converts area-class / chorochromatic thematic maps (raster images, often scans) into
tactile map masters with braille labeling, a tactile legend, and a reading guide.

**Changes vs. v1:**

- Added **Step 0** (output specification) — the physical medium and page size drive every later decision.
- Added **Step 5** (class aggregation before simplification) — merging classes is the primary strategy when a map has more than the available texture slots; dropping to white is the last resort.
- Merged text removal into segmentation (old Steps 4+5): text pixels are filled by growing the surrounding regions, not by generic inpainting.
- Fixed the water contradiction: **water always gets the wavy pattern when present**, so there are 4 thematic slots with water, 5 without.
- Added a second symbol-assignment strategy for **ordered (classed-sequential) data**: texture ramps instead of max-haptic-distance.
- Consolidated symbols, selective compound boundaries, and component-layer cleanup
  into **Step 7**, which produces the clean tactile master used by Step 8.
- Added **Step 8**: a local, live editor for full Grade 1 Braille labels, visibility,
  and manually draggable location pins.
- Simplification now happens **in vector space, after segmentation, topology-aware** (shared boundaries simplified once, so no slivers/gaps).
- Added minimum-size generalization driven by physical constants (drop/merge polygons too small to feel; exaggerate critical small features such as islands).
- Defined a structured intermediate artifact per step and three human-review checkpoints.
- Abbreviation collision rule and a clutter-resolution policy for braille labels.
- Scale bar is recomputed for the output scale, never copied from the source.

---

## Step 0 — Output specification (NEW)

Decide before anything else; these values are inputs to Steps 5–13.

- **Medium:** swell/microcapsule paper, braille embosser, or 3D print. Determines
  pattern rendering (line-based vs. dot-based), minimum line width, and resolution.
- **Page size** (e.g., A4 / 11.5×11 in) and orientation.
- **Physical constants** (defaults below; confirm against BANA / local tactile guidelines):

| Constant | Default | Used in |
|---|---|---|
| Braille cell footprint (incl. spacing) | ~6.2 × 10 mm | Steps 8, 13, 14 |
| Min. area for an identifiable texture | ~13 × 13 mm | Steps 5, 6, 7 |
| Min. gap between distinct raised elements | ~3 mm | Steps 5, 8, 9 |
| Min. tactile line width / length | ~1 mm / ~13 mm | Steps 5, 7, 8 |
| Max. distinct area textures per map | Exactly 5 available slots (a ceiling, not a target) | Steps 6, 7 |

The output scale (map units → page mm) is fixed here and never changes downstream.
The pipeline never invents thematic classes to fill unused texture slots: a map
with fewer classes simply uses fewer patterns.

## Step 1 — Semantic interpretation (VLM)

A multimodal model reads the full source image and emits **structured JSON** (schema-validated):

- Map type (chorochromatic / area-class and isopleth are in scope; choropleth
  and other map types are out of scope).
- **Data ordering:** qualitative (unordered classes) vs. ordered (e.g., temperature bands).
  This selects the symbol-assignment strategy in Step 7.
- What the map shows; brief detailed description (feeds the reading guide, Step 15).
- Title (if present), legend presence and entries (labels only — see below).
- Non-thematic content and its priority (water is always priority 1; others ranked by
  relevance to this map).
- Thematic class priorities (tie-break: smaller total area → lower priority).
- Line features and their meaning (borders / rivers / roads / graticule).
- Overlay text expected on the map (city names, river names, region labels) and the capital city if identifiable.

**Rule:** the VLM supplies *semantics only*. Exact colors, coordinates, and bounding
boxes always come from CV/pixel operations (Steps 2–5), because VLMs are unreliable at
pixel-exact work (e.g., near-identical legend blues on the Iran sample).

**Required input:** the source map must contain a visible legend. Step 1 records
whether one was detected; when none is present, the entire tactile-map pipeline
is blocked before Step 2 because class and color analysis cannot be grounded.

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

## Step 5 — Class aggregation

Step 5 reads every surviving category directly from Step 4's immutable
`label_map.png` and `classes_final.json`. It builds a complete aggregation
proposal that fits the tactile texture capacity: **4 thematic slots when water
is present, otherwise 5**. The limit is a ceiling, never a target.

- **Qualitative maps:** propose semantically coherent category merges.
- **Ordered maps:** re-bin adjacent legend classes into contiguous groups.
- Every surviving Step 4 category is assigned exactly once.
- A human review is required only when a proposed group contains more than one
  source category; the decision is stored in `aggregation_review.json`.

The approved result changes category identities only. It writes the grouped
raster and an audit proving that no geographic pixel was moved or erased.
Step 6 and later steps remain blocked until every multi-class merge is approved.

## Step 6 — Simplify for touch

Step 6 takes the approved aggregated raster from Step 5 and applies the same
area-generalization algorithm and five physical presets previously used by
canonical Step 5:

- handle islands using the configured tactile minimum area;
- dissolve undersized components into neighboring final categories;
- smooth shared category boundaries;
- run a second small-component pass;
- preserve significant or explicitly protected groups; and
- retain, reconnect, simplify, and filter the selected line classes using the
  same physical thresholds.

The five presets differ only in their physical detail parameters. Selecting a
preset activates its cached `label_map_gen.png`, `classes_gen.json`,
`regions_gen.geojson`, and `lines_gen.geojson` artifacts. The untouched Step 4
raster remains the audit source, and `step6_transitions.json` records any pixel
that ends in a neighboring final group during simplification.

> **Checkpoint B (human review):** compare the simplified approved-category
> raster with the original and select the appropriate physical detail preset
> before symbolization.
## Step 7 — Tactile symbols & final master render

**Areas** (patterns from the 5-group pattern library):

- Regular fills use the exact Illustrator-exported SVG `<pattern>` definitions in
  `pattern_library/`: two noise patterns, two grids, two line patterns, and two
  waves. Their original geometry, tile dimensions, transforms, and carrier opacity
  are retained. `plain` (no pattern fill) and `solid_black` (pure black fill) are
  the two additional non-SVG fill choices.
- A map uses at most one regular pattern from each semantic family (noise/dots,
  grids, lines, waves, or solids); variants from the same family are never paired.

- **Water present:** water gets the wavy pattern — always, regardless of the thematic
  count. The wave group is then closed; the remaining ≤ 4 thematic classes draw one
  pattern each from the other four groups.
- **No water:** ≤ 5 thematic classes get patterns; the wave group contributes only the
  triangular wave.
- **Qualitative data:** group chosen by the meaning of the class (e.g., desert →
  granular, mountains → solid black). After all families are fixed, Step 7 enumerates
  every valid SVG-variant combination and chooses the complete assignment that
  **maximizes the minimum Euclidean haptic distance over every adjacent patterned
  region pair**, using `pattern_library/embedding.csv`. This is a global,
  order-independent optimization. Adjacencies involving plain, pure black, or the map
  exterior are excluded because those fills may neighbour anything. Mean eligible-edge
  distance, then configured candidate order, breaks ties.
- **Ordered data:** use a **texture ramp** with perceived ordering (e.g., increasing dot
  density), not max-distance assignment — the reader must feel "more/less", matching the corrected bins from Step 5.
- Non-thematic areas beyond water: patterned only if slots remain, in priority order;
  otherwise plain white.
- The symbol-assignment raster is retained as an audit artifact while Step 7
  continues with boundary rendering and component-layer cleanup to produce the final master.

**Lines:** distinct tactile line styles for borders / rivers / roads (e.g., solid thick,
solid thin, dashed), respecting the minimum gap to area boundaries.

The Step 7 UI places a visual pattern legend beside the final tactile map. Selecting
an area opens a non-destructive transform editor for horizontal/vertical scale,
horizontal/vertical movement, and rotation. These per-area values are layered over
the complete Illustrator `patternTransform`; the SVG files in `pattern_library/`
are never modified. Changes are stored in `pattern_transforms.json` and locally
rerender the symbol, boundary, and cleanup passes without another model call.

Each legend area also has a **Change** action exposing every SVG library pattern,
plus no fill and pure black. The selected concrete pattern is held fixed. If its
family was already used, the displaced area takes the vacated or next unused
family; all unlocked variants are then globally re-optimized against map adjacency.
This preserves the one-area-per-pattern-family rule while maximizing the minimum
haptic distance between eligible adjacent areas. Dependent boundary, cleanup,
Braille-map, and existing Braille-legend artifacts are rerendered locally.

### Boundary rendering within Step 7

Evaluate adjacencies across the complete map before drawing any boundary:

- **Pattern next to pattern:** draw the boundary.
- **Pattern next to no pattern (plain/no fill):** no boundary.
- **Pattern next to pure black:** no boundary.
- **Global priority exception:** if a pattern participates in at least one
  pattern-pattern adjacency anywhere on the map, apply its boundary everywhere that
  pattern meets a different region, including no fill, plain, pure black, or the map
  outside. This is decided by pattern identity, so disconnected occurrences (such as
  water in the Iran example) behave consistently.
- **Exterior closure:** when that priority boundary meets a connected pure-black
  component, carry the compound outline around the complete perimeter of that black
  component, including its edge against the outside. This prevents the selected
  pattern-black boundary from visually ending at a three-way exterior junction;
  unrelated black components remain unaffected.

Each selected boundary is a **5 mm white stroke** with a **1 mm black stroke centered
on top of it**. The wide white stroke clears nearby texture marks; the black centerline
is the raised tactile boundary. The decision audit is saved in
`step8_boundaries.json`, and its intermediate raster is `step8_boundaries.png`.

The boundary pass rebuilds a boundary-free copy of the Step 7 pattern layer in memory;
the saved symbol-assignment raster and debug artifacts remain unchanged. Priority-pattern occurrences are
rendered as complete closed contours (including holes and canvas-edge contacts), not as
independent adjacency fragments, so the 1 mm centerline cannot end at the map outside.

### Component-layer cleanup within Step 7

Leave every earlier artifact unchanged and reproduce an SVG-style paint stack in the
final Step 7 output. Every non-black component, including plain areas and water/wave patterns, is
first rendered without a stroke, followed by its complete centered 5 mm white stroke
and centered 1 mm black stroke. Plain fill remains at the bottom, while still owning a
closed contour. Every solid-black component is then repainted at its exact geometry as a
top fill layer. The centered stroke still exists underneath, but the upper component
hides the portion extending into it.

This makes disconnected occurrences such as A and A2 inherit the same pattern ownership,
while a solid-black component such as C receives no stroke and is not consumed
by its neighbours' white strokes. Owner-owner boundaries retain the complete centered
compound stroke. The final Step 7 artifacts are `step8a_cleanup.png`,
`step8a_cleanup.json`, and `step8a_debug.png`.

## Step 8 — Editable Braille label overlay

- Start from every reviewed label and its original text coordinate in
  `overlay_labels.json`. The original text coordinate becomes the center of its pin.
- Convert editable Latin-script text locally to uncontracted Unified English Braille
  Grade 1. The supplied `assets/fonts/Braille SW 2024 INSEI.ttf` renders the cells at
  **24 pt**; Step 8 makes no model or external API call.
- Put the Braille text in a white box with **3 mm clear space on every side**. The
  selected left, right, top, or bottom position always describes the **pin's position
  relative to that text box**.
- Draw a **6 × 6 mm black pin** with a **2 mm white ring** at the label coordinate
  (10 × 10 mm complete visible footprint).
- The UI shows the map on the left and one editable Braille preview plus on/off switch
  per label on the right. Users can add labels even when no source labels were detected;
  text and visibility updates appear immediately.
- The top of the right panel also contains an editable Braille map title. Its detected
  Step 1 title is used when available (otherwise it starts blank for manual entry), and
  it starts in a full-width title box inset **5 mm from the configured paper margins**.
  Its 24 pt Braille can be left-, centre-, or right-aligned, wraps within that box, and
  the title box can be selected and dragged on the page.
- Pins are draggable directly on the map. A pin and its attached box move together;
  users can choose the side on which every pin attaches. Labels remain where the
  source or user placed them. Step 8 performs no automatic collision avoidance or
  rearrangement. A master switch can show or hide all labels at once.
- Editable state is saved in `braille_labels.json`; the export and audit are
  `step8_braille.png` and `step8_braille.json`. The export is a full page at
  the configured size (A4 portrait by default), rendered at 127 DPI / 5 px per
  mm with the tactile map centered inside the configured margins. The Step 8
  UI previews that complete page rather than a map-only crop.

## Step 9 — Editable Braille legend

- Create a separate A4 legend page so the map page keeps its intended physical scale.
- Render every Step 7 pattern assignment as a **40 × 20 mm** sample, using its exact
  SVG pattern transform. Samples retain a 1 mm black outer outline; patterns with the
  map's compound boundary also receive a 2 mm white inset before the pattern.
- Place the category name in 24 pt Grade 1 Braille beside every sample. The legend
  title and all category text are editable live in the UI and saved in
  `legend_labels.json`.
- Outputs are `step9_legend_base.png`, `step9_legend.png`, and `step9_legend.json`.

## Steps 10–13 — Frame and furniture layout

- **10. Map frame/border** placement.
- **11. Scale bar** — recomputed for the output scale from Step 0 (never copied from the
  source; after generalization it is approximate and the guide says so).
- **12. North sign** placement.
- **13. Map title** in braille (space budget: title consumes one braille line height + margin).

Furniture must respect the minimum-gap constant relative to map content.

## Step 14 — Legend creation

- Each used pattern → class name (post-aggregation names from Step 5).
- Each line style → meaning.
- Capital-city symbol and text-box symbol, if used.
- Abbreviation list (abbr → full text).
- **May overflow the map page: multi-page legend is expected and fine** (standard tactile practice).

## Step 15 — Reading guide creation

Merge the Step 1 detailed description with the adaptation record: what was aggregated
(Step 5), what was dropped or exaggerated (Steps 6, 9), what each pattern means, and how
to read the sheet. Output as braille-ready text and/or audio script.

> **Checkpoint C (human review):** final proof of the master + guide before production.

---

## Data model (cross-cutting)

Every step emits a schema-validated artifact (`classes.json`, `labels.json`,
`regions.geojson`, `lines.geojson`, final layout as SVG). Steps are independently
testable, and a human can correct any artifact at the checkpoints without re-running
earlier steps.

## Open questions

1. **Validate the supplied haptic embedding** — compare the coordinates in
   `pattern_library/embedding.csv` against published discriminability studies or a
   small user study as the pattern library evolves.
2. Exact physical constants per chosen medium (Step 0 table holds defaults only).
3. Whether classed-sequential maps (Iran temperature sample) are officially in scope —
   the pipeline now handles them via Steps 6/7, but scope should be explicit.
4. Braille standard/language and contraction grade for labels and legend.
5. Evaluation protocol with tactile readers (at minimum: texture identification,
   region tracing, label lookup tasks).
6. On the France map the model returned water_present: false — defensible, because the sea there is blank white, not a colored polygon. But it's a policy question for us: a tactile France map probably still wants the wavy pattern on the sea so readers can find the coastline. I'd resolve this in Step 6/7 policy (e.g., "coastal territory → treat water as present") rather than by prompt-tweaking; worth deciding before Step 2, and easy to change either way.
