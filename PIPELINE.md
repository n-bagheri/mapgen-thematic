# MapGen Tactile Pipeline — v2

Converts area-class / chorochromatic thematic maps (raster images, often scans) into
tactile map masters with braille labeling, a tactile legend, and a reading guide.

**Changes vs. v1:**

- Added **Step 0** (output specification) — the physical medium and page size drive every later decision.
- Added **Step 6** (class aggregation) — merging classes is the primary strategy when a map has more than the available texture slots; dropping to white is the last resort.
- Merged text removal into segmentation (old Steps 4+5): text pixels are filled by growing the surrounding regions, not by generic inpainting.
- Fixed the water contradiction: **water always gets the wavy pattern when present**, so there are 4 thematic slots with water, 5 without.
- Added a second symbol-assignment strategy for **ordered (classed-sequential) data**: texture ramps instead of max-haptic-distance.
- Added **Step 8** for selective compound boundaries, including the map-wide
  pattern-priority exception.
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
| Braille cell footprint (incl. spacing) | ~6.2 × 10 mm | Steps 9, 13, 14 |
| Min. area for an identifiable texture | ~13 × 13 mm | Steps 5, 6, 7 |
| Min. gap between distinct raised elements | ~3 mm | Steps 5, 8, 9 |
| Min. tactile line width / length | ~1 mm / ~13 mm | Steps 5, 7, 8 |
| Max. distinct area textures per map | Exactly 5 available slots (a ceiling, not a target) | Steps 6, 7 |

The output scale (map units → page mm) is fixed here and never changes downstream.
The pipeline never invents thematic classes to fill unused texture slots: a map
with fewer classes simply uses fewer patterns.

## Step 1 — Semantic interpretation (VLM)

A multimodal model reads the full source image and emits **structured JSON** (schema-validated):

- Map type (chorochromatic / area-class, classed-sequential, other → out of scope flag).
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

### Alt MapGen: aggregate before simplifying

The alternate branch can run beside the canonical Steps 5–7 without
overwriting them. Each alternate step is manual: Alt Step 6 will not generate
Alt Step 5, and Alt Step 7 will not generate Alt Step 6.

```text
python -m mapgen alt-step5 MAP
python -m mapgen alt-step6 MAP
python -m mapgen alt-step7 MAP
```

- **Alt Step 5** reads every surviving class directly from Step 4's immutable
  `label_map.png` and `classes_final.json`. It proposes at most five tactile
  groups. Five is a ceiling, never a target. A review is required only when a
  group contains more than one source class. Creating the proposed group
  raster changes class identities but moves or erases zero geographic pixels.
- **Alt Step 6** takes the actual cached raster from each canonical Step 5
  preset, so its island handling, minimum-area removal, and boundary smoothing
  are pixel-for-pixel the canonical result. It then changes only each
  simplified source class identity into its approved final group. This removes
  internal boundaries between an approved pair but cannot retain extra small
  patches or jagged boundaries. The complete source-class-to-final-group
  transition report and changed-pixel overlay are saved for audit.
- **Alt Step 7** assigns textures and renders the exact Alt Step 6 group raster.
  It performs no additional geographic simplification. Pattern footprint
  checks are reported as warnings only. Non-thematic extras remain plain, so
  unused texture capacity stays unused. If the canonical Step 7 render exists,
  a side-by-side `step7_comparison.png` is also written.

Alternate artifacts use an `alt_` prefix. The web UI places the three manual,
expandable panels in a separate **Alt MapGen** section after canonical Step 8.
The concrete Alt Step 5 decision is persisted in
`alt_aggregation_review.json`.

## Step 6 — Class aggregation (NEW)

Compute available texture slots: **4 if water is present, else 5** (see Step 7).
If thematic classes exceed the slots:

- **Qualitative maps:** the VLM proposes semantic merges (e.g., "Grassland" +
  "Grassland and crops"; specialty crops → "other agriculture"), human confirms.
- **Ordered maps:** re-bin into ≤ slots contiguous bins (e.g., 11 temperature bands → 4).
- **Only if aggregation is impossible:** keep the highest-priority classes and set the
  rest to plain white. Step 8 then applies only the selective boundary rule below;
  dropping classes to white remains the last resort, not the default.

Every canonical multi-class proposal is reviewed inside Step 6 and saved in
`aggregation_review.json`. Canonical Step 7 is blocked until every proposed
merge is approved; changing the reviewed grouping invalidates the old Step 7
render and its Step 8 boundary result.

## Step 7 — Tactile symbol assignment

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
  density), not max-distance assignment — the reader must feel "more/less", matching the corrected bins from Step 6.
- Non-thematic areas beyond water: patterned only if slots remain, in priority order;
  otherwise plain white.
- Step 7 retains its default embossed boundary rendering between every distinct
  region. Step 8 does not overwrite or otherwise alter this artifact.

**Lines:** distinct tactile line styles for borders / rivers / roads (e.g., solid thick,
solid thin, dashed), respecting the minimum gap to area boundaries.

## Step 8 — Adding boundaries

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
`step8_boundaries.json`, and the final raster is `step8_boundaries.png`.

Step 8 rebuilds a boundary-free copy of the Step 7 pattern layer in memory; the saved
Step 7 raster and debug artifacts remain unchanged. Priority-pattern occurrences are
rendered as complete closed contours (including holes and canvas-edge contacts), not as
independent adjacency fragments, so the 1 mm centerline cannot end at the map outside.

## Step 8A — Component-layer cleanup

Leave every Step 1–8 artifact unchanged and reproduce an SVG-style paint stack in a new
output. Every non-black component, including plain areas and water/wave patterns, is
first rendered without a stroke, followed by its complete centered 5 mm white stroke
and centered 1 mm black stroke. Plain fill remains at the bottom, while still owning a
closed contour. Every solid-black component is then repainted at its exact geometry as a
top fill layer. The centered stroke still exists underneath, but the upper component
hides the portion extending into it.

This makes disconnected occurrences such as A and A2 inherit the same pattern ownership,
while a solid-black component such as C receives no stroke and is not consumed
by its neighbours' white strokes. Owner-owner boundaries retain the complete centered
compound stroke. Artifacts are `step8a_cleanup.png`, `step8a_cleanup.json`, and
`step8a_debug.png`.

## Step 9 — Braille labels

- **Abbreviation rule:** first two letters; on collision, first letter + next
  distinguishing consonant; uniqueness enforced in one table; every abbreviation is
  listed in the legend (Step 14).
- Cities are omitted except the **capital**: its 2-letter abbreviation goes in the text-box
  icon at the Step 3 anchor.
- Other overlay labels: 2-letter abbreviation in the text-box icon at their anchors.
- **Clutter resolution** (braille is fixed-size and cannot be scaled): if boxes collide or
  a region is too small to host one, drop labels in ascending priority
  (site labels → region labels → … capital last) until the layout is clean. Avoid tactile
  leader lines — they read as map lines. Everything dropped here is still covered by the
  legend and reading guide.

## Steps 10–13 — Frame and furniture layout

- **10. Map frame/border** placement.
- **11. Scale bar** — recomputed for the output scale from Step 0 (never copied from the
  source; after generalization it is approximate and the guide says so).
- **12. North sign** placement.
- **13. Map title** in braille (space budget: title consumes one braille line height + margin).

Furniture must respect the minimum-gap constant relative to map content.

## Step 14 — Legend creation

- Each used pattern → class name (post-aggregation names from Step 6).
- Each line style → meaning.
- Capital-city symbol and text-box symbol, if used.
- Abbreviation list (abbr → full text).
- **May overflow the map page: multi-page legend is expected and fine** (standard tactile practice).

## Step 15 — Reading guide creation

Merge the Step 1 detailed description with the adaptation record: what was aggregated
(Step 6), what was dropped or exaggerated (Steps 5, 9), what each pattern means, and how
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
