"use strict";

/* Each step shows a short title (what is happening) with `blurb` underneath
   saying what that means in plain words, and `caption` naming the picture.

   This pipeline is not the one the older focused view described: category
   aggregation now runs at Step 5 and simplification at Step 6, symbols,
   boundaries and cleanup were merged into a single Step 7, and Steps 8 and 9
   add the Braille overlay and the Braille legend page. */
export const STEP_DEFS = [
  { key: "1", number: "01", title: "Reading the map",
    caption: "The source map, as you uploaded it",
    blurb: "Reading means understanding it the way a person would: what the map is about, what each legend entry says, and whether the categories are a scale from low to high or simply different kinds of thing." },
  { key: "2", number: "02", title: "Locating the map, legend, and captions", preview: "step2_debug.png",
    caption: "Detected map area, legend, and captions",
    blurb: "A map sheet is more than the map: it also carries a title, a legend, and credits. Each of those is found and marked, and the colour of every legend swatch is measured from the pixels so the map's colours can be matched to their meaning." },
  { key: "3", number: "03", title: "Finding the text printed over the map", preview: "step3_debug.png",
    caption: "Detected text on the map",
    blurb: "Place names, category labels, and grid numbers are printed on top of the map and hide the colours underneath. Each piece of text is located and read here so it can be lifted off in the next step." },
  { key: "4", number: "04", title: "Turning colours into regions", preview: "step4_debug.png",
    caption: "Source map beside the rebuilt regions",
    blurb: "Every pixel is matched to the legend colour it belongs to, so the map becomes a set of regions instead of an image. The lettering found above is erased and filled in from its surroundings, and drawn lines such as country borders and the coordinate grid are dissolved into the regions around them." },
  { key: "5", number: "05", title: "Fitting the categories to the textures", preview: "step5_aggregation_preview.png",
    caption: "Categories after fitting to the texture limit",
    blurb: "Only about five textures stay tellable apart under the hand. If the map has more categories than that, the closest ones are proposed for merging — and that proposal is yours to approve or change. Nothing moves on the map here; only the names change." },
  { key: "6", number: "06", title: "Simplifying for the fingertip", preview: "label_map_gen_preview.png",
    caption: "Source map beside the simplified version",
    blurb: "A finger reads far less detail than an eye. Regions too small to feel are merged into the neighbour they touch most, and the remaining edges are smoothed, so every shape left on the sheet is one that can actually be traced." },
  { key: "7", number: "07", title: "Giving each category a texture", preview: "step8a_cleanup.png",
    caption: "The finished tactile master",
    blurb: "Each category gets its own tactile language — dots, lines, or a solid fill — picked so that two areas which touch never feel the same. A raised line is then drawn between neighbouring areas, and a last cleanup pass tidies the sheet ready to emboss." },
  { key: "8", number: "08", title: "Adding the Braille labels", preview: "step8_braille.png",
    caption: "The tactile page with its Braille labels",
    blurb: "Place names become Grade 1 Braille on the printed page. Every label can be renamed, hidden, or dragged where a reading finger will actually meet it, and the page keeps 3 mm of clear space around each one." },
  { key: "9", number: "09", title: "Building the Braille legend", preview: "step9_legend.png",
    caption: "The separate legend page",
    blurb: "The legend moves to a page of its own: one tactile sample per category beside its Braille name, so a reader can learn the textures before meeting them on the map." },
];

/* Run all has five human gates: the geographic mask after Step 2, the
   category proposal after Step 5, the simplification level after Step 6,
   the tactile pattern assignment after Step 7, and the Braille/page toolbox
   after Step 8.
   Nothing consumes a decision before the reader has explicitly approved it. */
export const INITIAL_BATCH = ["1", "2"];
export const ANALYSIS_BATCH = ["3", "4", "5"];
export const SIMPLIFICATION_BATCH = ["6"];
export const PATTERN_BATCH = ["7"];
export const LABEL_BATCH = ["8"];
export const FINAL_BATCH = ["9"];

export const LINE_KINDS = [
  { id: "river", label: "Rivers" },
  { id: "road", label: "Roads" },
  { id: "border", label: "Borders" },
  { id: "border_or_coast", label: "Coasts" },
  { id: "line", label: "Other lines" },
];

export const DETAIL_NAMES = {
  1: "Most detail",
  2: "Detailed",
  3: "Balanced",
  4: "Simple",
  5: "Simplest",
};

export const PAGE_SIZES = {
  a4: [210, 297],
  a3: [297, 420],
};

export function completedCount(map) {
  return STEP_DEFS.filter((step) => Boolean(map?.steps?.[step.key])).length;
}

export function allDone(map) {
  return Boolean(map?.steps?.["9"] && map?.step9_review_ready);
}

/** The message the server will refuse a run with, or null when it will accept
 *  one.  Step 1 stays runnable in every case so the map can be re-read. */
export function blockingReason(map) {
  if (!map) return null;
  if (map.step1_error) return map.step1_error;
  if (map.pipeline_error) return map.pipeline_error;
  if (map.in_scope === false) {
    return "Step 1 classified this map as out of scope for the tactile pipeline. "
      + "Only Step 1 can be rerun.";
  }
  return null;
}

export function pageSizeKey(spec) {
  const width = Number(spec?.page_width_mm);
  const height = Number(spec?.page_height_mm);
  const match = Object.entries(PAGE_SIZES).find(([, [pw, ph]]) =>
    (pw === width && ph === height) || (pw === height && ph === width));
  return match ? match[0] : "custom";
}

/* What the left pane shows for each step: the same pictures the detailed page
   puts on that step's card, in the same order -- what went in, then what came
   out.  `optional` entries are skipped when the artifact is not on disk, which
   is how a step that ran with different inputs still renders cleanly.

   `overlay` names the editable layer drawn on top of that picture, and
   `hybrid` names the colour twin the View toggle swaps to. */
export const STEP_VIEWS = {
  1: [
    { source: true, caption: "The source map, as you uploaded it" },
  ],
  2: [
    { artifact: "map_area.png", caption: "Map area — paint here to correct the mask",
      overlay: "mask" },
    { artifact: "step2_layout_debug.png", caption: "Raw AI layout, before any refinement",
      optional: true, intermediate: true },
    { artifact: "step2_debug.png", caption: "Refined map area, legend, and captions",
      intermediate: true },
    { artifact: "map_text_input.png", caption: "Extracted map",
      optional: true, intermediate: true },
    { artifact: "legend.png", caption: "Extracted legend",
      optional: true, intermediate: true },
  ],
  3: [
    { artifact: "step3_debug.png", caption: "Detected text on the map" },
  ],
  4: [
    { artifact: "label_map_preview.png", caption: "Segmented map", overlay: "segmented-lines" },
  ],
  5: [
    { artifact: "step5_aggregation_preview.png", caption: "Categories after fitting to the texture limit" },
  ],
  6: [
    { dynamic: "simplified", caption: "Simplified map", overlay: "layers" },
  ],
  7: [
    { artifact: "step8a_cleanup.png", hybrid: "step8a_hybrid.png",
      caption: "The finished tactile master", pageLayout: true, originalCompare: true },
  ],
  8: [
    { artifact: "step8_braille_base.png", hybrid: "step8_hybrid_base.png",
      caption: "The tactile page with its Braille labels", overlay: "braille",
      pageRender: true, originalCompare: true },
  ],
  9: [
    { artifact: "step9_legend.png", hybrid: "step9_legend_hybrid.png",
      caption: "The separate legend page", overlay: "legend",
      legendPage: true, mapLegendCompare: true },
  ],
};

/** The step whose pictures the left pane is showing: the one the reader opened,
 *  else the newest finished step, so a fresh run follows itself. */
export function viewedStep(map, activeStep) {
  if (activeStep && STEP_VIEWS[activeStep]) return Number(activeStep);
  const done = STEP_DEFS.filter((step) => map?.steps?.[step.key]);
  return Number(done.at(-1)?.key || 1);
}
