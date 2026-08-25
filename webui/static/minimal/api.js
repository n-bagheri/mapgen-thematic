"use strict";

/* Every request the focused view makes.  Keeping them in one place means the
   step renumbering that separates this pipeline from the older one is visible
   as a list of paths rather than scattered through the editors. */

export const $ = (id) => document.getElementById(id);

export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
})[char]);

function enc(value) {
  return encodeURIComponent(String(value));
}

function cacheToken() {
  return Date.now().toString(36);
}

export function artifactUrl(stem, name) {
  return `/api/artifact/${enc(stem)}/${enc(name)}?t=${cacheToken()}`;
}

export function mapUrl(name) {
  return `/api/mapimg/${enc(name)}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) throw new Error(errorMessage(payload, response.status));
  return payload;
}

async function apiBlob(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (response.ok) return response.blob();
  const text = await response.text();
  let payload = text;
  try { payload = JSON.parse(text); } catch { /* Flask errors may be HTML. */ }
  throw new Error(errorMessage(payload, response.status));
}

/* This view and webui/server.py ship together: the page posts actions and reads
   review flags that a server from an earlier checkout does not have.  Keep this
   equal to UI_CONTRACT in webui/server.py. */
export const UI_CONTRACT = 2;

const RESTART_ADVICE = "The server is running older code than this page. In a PowerShell "
  + "terminal at the Thematic folder, run: .\\restart-mapgen-ui.ps1 — then refresh this page.";

let serverContract = null;
let serverRestartRequired = false;

/** The advice a version mismatch calls for, or "" while the two sides agree.
 *  A server older than this contract reports no contract at all, which reads as
 *  0 -- so the very first /api/maps answer is enough to recognise one. */
export function staleServerAdvice() {
  if (serverRestartRequired) return RESTART_ADVICE;
  return serverContract !== null && serverContract < UI_CONTRACT ? RESTART_ADVICE : "";
}

/** Endpoints report a refusal either as {error} or, when the server aborts,
 *  as one of Flask's HTML pages.  Both have to read as a plain sentence,
 *  because these messages are shown to the reader verbatim. */
function errorMessage(payload, status) {
  let text = "";
  if (payload && typeof payload === "object" && payload.error) {
    text = String(payload.error);
  } else if (typeof payload === "string" && payload) {
    const paragraph = payload.match(/<p>([\s\S]*?)<\/p>/i);
    text = (paragraph ? paragraph[1] : payload).replace(/<[^>]*>/g, "").trim();
  }
  if (!text) text = `Request failed (${status})`;
  // An outdated server refuses in terms of the actions it knows, so its own
  // wording never points at the real fix.
  const advice = staleServerAdvice();
  return advice ? `${text} — ${advice}` : text;
}

export async function artifactJson(stem, name) {
  try {
    return await api(`/api/artifact/${enc(stem)}/${enc(name)}?t=${cacheToken()}`);
  } catch {
    return null;
  }
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/* -- projects ---------------------------------------------------------- */
export const getMaps = async () => {
  const payload = await api("/api/maps");
  serverContract = Number(payload?.contract) || 0;
  serverRestartRequired = Boolean(payload?.restart_required);
  return payload;
};
export const getModels = () => api("/api/models");
export const getSpec = () => api("/api/spec");
export const saveSpecText = (spec) => api("/api/spec", json("POST", { spec: JSON.stringify(spec) }));
export const uploadFile = (form) => api("/api/upload", { method: "POST", body: form });
export const renameMap = (stem, name) => api(`/api/maps/${enc(stem)}`, json("PATCH", { name }));
export const reorderMaps = (stems) => api("/api/maps/order", json("PUT", { stems }));
export const deleteMap = (stem) => api(`/api/maps/${enc(stem)}`, { method: "DELETE" });

/* -- run control ------------------------------------------------------- */
export const getJob = (stem) => api(`/api/job/${enc(stem)}`);
export const runSteps = (stem, steps, model) =>
  api("/api/run", json("POST", { stem, steps: steps.map(Number), model }));
export const resetFrom = (stem, fromStep) =>
  api("/api/reset", json("POST", { stem, from_step: Number(fromStep) }));

/* -- Step 2: geographic mask ------------------------------------------- */
export const getMaskReview = (stem) => api(`/api/maskreview/${enc(stem)}`);
export const saveMaskStrokes = (stem, strokes) =>
  api(`/api/maskreview/${enc(stem)}`, json("POST", { strokes }));
export const resetMask = (stem) => api(`/api/maskreview/${enc(stem)}`, json("POST", { reset: true }));
export const approveMask = (stem) => api(`/api/maskreview/${enc(stem)}`, json("POST", { approve: true }));

/* -- Step 3: overlay text ---------------------------------------------- */
export const getLabelReview = (stem) => api(`/api/labelreview/${enc(stem)}`);
export const saveLabelReview = (stem, decisions) =>
  api(`/api/labelreview/${enc(stem)}`, json("POST", { decisions }));
export const labelCropUrl = (stem, index) =>
  `/api/labelcrop/${enc(stem)}/${Number(index)}?t=${cacheToken()}`;

/* -- Step 4: linework -------------------------------------------------- */
export const getLineReview = (stem) => api(`/api/linereview/${enc(stem)}`);
export const saveLineReview = (stem, payload) =>
  api(`/api/linereview/${enc(stem)}`, json("POST", payload));

/* -- Step 5: category aggregation (was Step 6 in the older pipeline) ---- */
export const getAggregationReview = (stem) => api(`/api/aggregation-review/${enc(stem)}`);
export const previewAggregation = (stem, groups) =>
  apiBlob(`/api/aggregation-preview/${enc(stem)}`, json("POST", { groups }));
export const saveAggregationReview = (stem, groups) =>
  api(`/api/aggregation-review/${enc(stem)}`, json("POST", { groups }));

/* -- Step 6: simplification (was Step 5 in the older pipeline) ---------- */
export const getStep6Params = (stem) => api(`/api/step6params/${enc(stem)}`);
export const saveStep6Params = (stem, params) =>
  api(`/api/step6params/${enc(stem)}`, json("POST", params));
export const getStep6Presets = (stem) => api(`/api/step6presets/${enc(stem)}`);
export const activateStep6Preset = (stem, level) =>
  api(`/api/step6preset/${enc(stem)}`, json("POST", { level: Number(level) }));

/* -- Step 7: pattern decision, transforms, colours, page layout -------- */
export const getStep7Review = (stem) => api(`/api/step7-review/${enc(stem)}`);
export const saveStep7Review = (stem, patch) =>
  api(`/api/step7-review/${enc(stem)}`, json("POST", patch));
export const getPatternData = (stem) => api(`/api/pattern-transforms/${enc(stem)}`);
export const assignPattern = (stem, groupId, pattern, preserveHapticDistances = true) =>
  api(`/api/pattern-assignments/${enc(stem)}/${Number(groupId)}`, json("POST", {
    pattern, preserve_haptic_distances: Boolean(preserveHapticDistances),
  }));
export const savePatternTransform = (stem, groupId, transform) =>
  api(`/api/pattern-transforms/${enc(stem)}/${Number(groupId)}`, json("POST", transform));
export const patternLibraryPreviewUrl = (patternId) =>
  `/api/pattern-library-preview/${enc(patternId)}`;
export const patternPreviewUrl = (stem, groupId) =>
  `/api/pattern-preview/${enc(stem)}/${Number(groupId)}?t=${cacheToken()}`;
export const getCategoryColors = (stem) => api(`/api/category-colors/${enc(stem)}`);
export const saveCategoryColors = (stem, colors) =>
  api(`/api/category-colors/${enc(stem)}`, json("POST", { colors }));
export const getPageLayout = (stem) => api(`/api/page-layout/${enc(stem)}`);
export const savePageLayout = (stem, patch) =>
  api(`/api/page-layout/${enc(stem)}`, json("POST", patch));
export const NORTH_MARKER_URL = "/api/north-marker.svg";

/* -- Step 8: Braille labels -------------------------------------------- */
export const getBrailleLayout = (stem) => api(`/api/braille-labels/${enc(stem)}`);
export const getStep8Review = (stem) => api(`/api/step8-review/${enc(stem)}`);
export const saveStep8Review = (stem, approve) =>
  api(`/api/step8-review/${enc(stem)}`, json("POST", { approve: Boolean(approve) }));
export const addBrailleLabel = (stem, text) =>
  api(`/api/braille-labels/${enc(stem)}`, json("POST", text ? { text } : {}));
export const saveBrailleLabel = (stem, labelId, patch) =>
  api(`/api/braille-labels/${enc(stem)}/${enc(labelId)}`, json("POST", patch));
export const deleteBrailleLabel = (stem, labelId) =>
  api(`/api/braille-labels/${enc(stem)}/${enc(labelId)}`, { method: "DELETE" });
export const saveBrailleTitle = (stem, patch) =>
  api(`/api/braille-labels/${enc(stem)}/title`, json("POST", patch));
export const saveBrailleLayout = (stem, patch) =>
  api(`/api/braille-layout/${enc(stem)}`, json("POST", patch));

/* -- Step 9: Braille legend page --------------------------------------- */
export const getLegendLayout = (stem) => api(`/api/legend/${enc(stem)}`);
export const getStep9Review = (stem) => api(`/api/step9-review/${enc(stem)}`);
export const saveStep9Review = (stem, approve) =>
  api(`/api/step9-review/${enc(stem)}`, json("POST", { approve: Boolean(approve) }));
export const saveLegendItem = (stem, target, patch) =>
  api(`/api/legend/${enc(stem)}/${enc(target)}`, json("POST", patch));
export const saveLegendOrientation = (stem, orientation) =>
  api(`/api/legend-page/${enc(stem)}`, json("POST", { orientation }));
export const legendSwatchUrl = (stem, target, hybrid = false) =>
  `/api/legend-swatch/${enc(stem)}/${enc(target)}${hybrid ? "?variant=hybrid" : ""}`;

/* -- Export ------------------------------------------------------------- */
export const downloadUrl = (stem, hybrid = false) =>
  `/api/download/${enc(stem)}${hybrid ? "?variant=hybrid" : ""}`;
