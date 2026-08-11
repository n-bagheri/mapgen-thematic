"use strict";

/* ------------------------------------------------------------------ state */

const STEPS = [
  { n: 1, title: "Step 1 — Semantic interpretation",
    desc: "The selected model reads the map: type, classes, priorities, expected text" },
  { n: 2, title: "Step 2 — Map & legend isolation",
    desc: "Layout boxes + pixel-exact mask; legend swatch colors sampled" },
  { n: 3, title: "Step 3 — Overlay text detection",
    desc: "Gemini reading fused with CRAFT/EasyOCR text localization" },
  { n: 4, title: "Step 4 — Segmentation & lines",
    desc: "Areas classified, text removed, halos dissolved, lines vectorized (no AI)" },
  { n: 5, title: "Step 5 — Simplify for touch",
    desc: "Choose how much map detail to keep, then compare the adjusted result with the original" },
  { n: 6, title: "Step 6 — Class aggregation",
    desc: "Classes merged into the available texture slots (review the plan here)" },
  { n: 7, title: "Step 7 — Tactile symbols & master render",
    desc: "Patterns assigned (water = waves, ordered = ramp) and the tactile master rendered" },
  { n: 8, title: "Step 8 — Adding boundaries",
    desc: "Selected area edges receive a 5 mm white stroke with a centered 1 mm black stroke" },
  { n: "8a", title: "Step 8A — Cleanup",
    desc: "SVG-style component layers repaint only solid-black fills above centered boundary strokes" },
];

const KIND_COLORS = { capital: "#d62728", city: "#e28a1b", river_label: "#1f77d0",
  region_label: "#1e9e5a", line_label: "#7a3fbf", other: "#8a8f98" };

let MAPS = [];
let SELECTED = null;
let POLL = null;
const OPEN_STEPS = new Set([1, 2, 3, 4, 5]);
const OPEN_ALT_STEPS = new Set();
const RIVER_REVIEW_DISPLAY = new Map();

/* ------------------------------------------------------------------ utils */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
const artifactUrl = (stem, name) =>
  `/api/artifact/${encodeURIComponent(stem)}/${encodeURIComponent(name)}?t=${Date.now()}`;

async function artifactJson(stem, name) {
  const r = await fetch(artifactUrl(stem, name));
  if (!r.ok) return null;
  return r.json();
}

async function step5PresetData(stem) {
  try {
    return { ...(await api(`/api/step5presets/${encodeURIComponent(stem)}`)), supported: true };
  } catch (_) {
    // A stale backend can serve the new static JS while not yet knowing the
    // preset route. Keep the canonical Step 5 result usable until restart.
    return { ready: false, active_level: null, variants: {}, supported: false };
  }
}

async function altStep6PresetData(stem) {
  try {
    return await api(`/api/alt-step6presets/${encodeURIComponent(stem)}`);
  } catch (_) {
    return { ready: false, active_level: null, variants: {} };
  }
}

/* ------------------------------------------------------------------ sidebar */

async function loadMaps() {
  const data = await api("/api/maps");
  MAPS = data.maps;
  renderSidebar();
  if (SELECTED && !MAPS.some((m) => m.stem === SELECTED)) SELECTED = null;
}

async function loadModels() {
  const select = $("model-select");
  try {
    const data = await api("/api/models");
    select.innerHTML = data.models.map((model) =>
      `<option value="${esc(model.id)}">${esc(model.label)}</option>`).join("");
    select.value = data.default;
  } catch (_) {
    // The HTML contains the same options, so selection still works if the
    // backend is stale and needs to be restarted.
  }
}

function renderSidebar() {
  const list = $("map-list");
  list.innerHTML = "";
  for (const m of MAPS) {
    const btn = document.createElement("button");
    btn.className = "map-item" + (m.stem === SELECTED ? " sel" : "");
    const running = m.job && m.job.status === "running";
    const pips = STEPS.map((s) => {
      const cls = running && m.job.current === s.n ? "running" : m.steps[s.n] ? "done" : "";
      return `<span class="pip ${cls}" title="step ${s.n}"></span>`;
    }).join("");
    btn.innerHTML = `<img src="/api/mapimg/${encodeURIComponent(m.name)}" alt="">
      <span class="nm">${esc(m.name)}<span class="pips">${pips}</span></span>`;
    btn.onclick = () => selectMap(m.stem);
    list.appendChild(btn);
  }
}

async function selectMap(stem) {
  if (SELECTED !== stem) OPEN_ALT_STEPS.clear();
  SELECTED = stem;
  $("empty").hidden = true;
  $("spec-panel").hidden = true;
  $("map-panel").hidden = false;
  renderSidebar();
  await renderMap();
  startPollingIfRunning();
}

/* ------------------------------------------------------------------ map panel */

function mapRec() { return MAPS.find((m) => m.stem === SELECTED); }

let PENDING_VIEWS = [];

function rememberRiverReviewDisplay() {
  if (!SELECTED) return;
  const toggle = document.querySelector(".river-review .river-layer-enabled");
  if (toggle) RIVER_REVIEW_DISPLAY.set(SELECTED, { includeRivers: toggle.checked });
}

async function renderMap() {
  const m = mapRec();
  if (!m) return;
  // A Step 5 regeneration finishes with a complete page rebuild. Keep this
  // purely display-level choice so an already closed river review stays closed.
  rememberRiverReviewDisplay();
  const scrollY = $("main").scrollTop; // full rebuilds must not jump the page
  PENDING_VIEWS = [];
  $("map-title").textContent = m.name;
  const stepsEl = $("steps");
  stepsEl.innerHTML = "";
  for (const s of STEPS) {
    stepsEl.appendChild(await stepCard(m, s));
  }
  if (m.steps[4]) {
    const altMapGen = document.createElement("section");
    altMapGen.className = "alt-mapgen-shell";
    stepsEl.appendChild(altMapGen);
    PENDING_VIEWS.push(renderAltMapGen(altMapGen, m).catch((err) => {
      altMapGen.innerHTML = `<p class="msg err">Alt MapGen failed to load: ${esc(err.message)}</p>`;
    }));
  }
  // restore twice: once now, once after async artifact views finish loading
  requestAnimationFrame(() => { $("main").scrollTop = scrollY; });
  Promise.allSettled(PENDING_VIEWS).then(() =>
    requestAnimationFrame(() => { $("main").scrollTop = scrollY; }));
  renderMapActions(m);
}

function renderMapActions(m) {
  let remaining = STEPS.filter((s) => !m.steps[s.n]).map((s) => s.n);
  if (remaining.includes(6)) remaining = remaining.filter((step) => step <= 6);
  else if (!m.step6_review_ready) remaining = remaining.filter((step) => step < 7);
  const runAll = $("run-all");
  runAll.textContent = remaining.length ? `Run remaining (${remaining.join(", ")})` : "All steps done";
  runAll.disabled = !remaining.length || (m.job && m.job.status === "running");
  $("delete-project").disabled = Boolean(m.job && m.job.status === "running");
  $("model-select").disabled = Boolean(m.job && m.job.status === "running");
  runAll.onclick = () => runSteps(remaining);
  renderJobBanner();
}

async function refreshCanonicalDownstreamCards(map, cardNumbers = [6, 7, 8, "8a"]) {
  // Switching among Step 5's already-generated presets only invalidates the
  // canonical outputs that consume it.  Replacing just these cards preserves
  // unsaved Step 4 review controls (especially the river inclusion toggle).
  for (const stepNumber of cardNumbers) {
    const oldCard = document.querySelector(`#steps .step-card[data-step="${stepNumber}"]`);
    const definition = STEPS.find((step) => step.n === stepNumber);
    if (oldCard && definition) oldCard.replaceWith(await stepCard(map, definition));
  }
  renderMapActions(map);
}

async function stepCard(m, s) {
  const done = m.steps[s.n];
  const job = m.job;
  const running = job && job.status === "running" && job.current === s.n;
  const failed = job && job.status === "failed" && job.current === null &&
                 job.steps.includes(s.n) && !done;

  const card = document.createElement("div");
  card.className = "step-card";
  card.dataset.step = String(s.n);
  const dot = running ? "running" : done ? "done" : failed ? "failed" : "";
  card.innerHTML = `
    <div class="step-head">
      <span class="dot ${dot}"></span>
      <h3>${esc(s.title)}</h3>
      <span class="desc">${esc(s.desc)}</span>
    </div>
    <div class="step-body" ${OPEN_STEPS.has(s.n) ? "" : "hidden"}></div>`;
  const head = card.querySelector(".step-head");
  const body = card.querySelector(".step-body");
  head.onclick = () => {
    body.hidden = !body.hidden;
    body.hidden ? OPEN_STEPS.delete(s.n) : OPEN_STEPS.add(s.n);
  };

  const busy = job && job.status === "running";
  const actions = document.createElement("div");
  actions.className = "step-actions";
  const runLabel = s.n === 5
    ? (done ? "Regenerate instant previews" : "Generate instant previews")
    : `${done ? "Re-run" : "Run"} step ${s.n}`;
  const resetLabel = s.n === 5 ? "Clear this and later results" : "Reset from here";
  const reviewBlocked = [7, 8, "8a"].includes(s.n) && m.steps[6] && !m.step6_review_ready;
  actions.innerHTML = `
    <button class="btn primary" ${busy || reviewBlocked ? "disabled" : ""}>${runLabel}</button>
    ${done ? `<button class="btn ghost" ${busy ? "disabled" : ""}
        title="delete artifacts of this and later steps">${resetLabel}</button>` : ""}`;
  actions.querySelector(".btn.primary").onclick = (e) => {
    e.stopPropagation();
    runSteps([s.n]);
  };
  const resetBtn = actions.querySelector(".btn.ghost");
  if (resetBtn) resetBtn.onclick = async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete artifacts of step ${s.n} and every later step for this map?`)) return;
    await api("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stem: SELECTED, from_step: s.n }) });
    await loadMaps(); await renderMap();
  };
  body.appendChild(actions);
  if (reviewBlocked) body.insertAdjacentHTML("beforeend",
    '<p class="line-warning">Approve the Step 6 class aggregation before running Steps 7–8A.</p>');

  const inputView = document.createElement("div");
  body.appendChild(inputView);
  await renderStepInput(s.n, inputView, m);

  if (s.n === 5 && m.steps[4]) {
    const paramsBox = document.createElement("div");
    paramsBox.className = "p5-params-host";
    body.appendChild(paramsBox);
    renderStep5Params(paramsBox, m.stem).catch(() => {});
  }

  if (done) {
    const view = document.createElement("div");
    body.appendChild(view);
    PENDING_VIEWS.push(renderArtifacts(s.n, view).catch((err) => {
      view.innerHTML = `<p class="msg err">failed to load artifacts: ${esc(err.message)}</p>`;
    }));
  } else if (!running) {
    body.insertAdjacentHTML("beforeend",
      `<p class="hint">Not run yet. Running a later step runs missing earlier steps automatically.</p>`);
  }
  return card;
}

/* --------------------------------------------------- step 5 controls */

const STEP5_LEVELS = {
  1: { label: "Most detail", short: "More detail" },
  2: { label: "Detailed", short: "Detailed" },
  3: { label: "Balanced", short: "Balanced" },
  4: { label: "Simple", short: "Simple" },
  5: { label: "Simplest", short: "Simplest" },
};

const LINE_KIND_LABELS = {
  river: "Rivers", road: "Roads", border: "Borders",
  border_or_coast: "Borders or coastlines", coastline: "Coastlines",
  frame: "Frames", line: "Other lines",
};

const LINE_KIND_COLORS = {
  river: "#2269cc", road: "#f59120", border: "#373737",
  border_or_coast: "#373737", coastline: "#30705c",
  frame: "#878787", line: "#7e57a8",
};

async function renderStep5Params(el, stem) {
  const [data, presetData] = await Promise.all([
    api(`/api/step5params/${encodeURIComponent(stem)}`),
    step5PresetData(stem),
  ]);
  const p = data.params;
  const KINDS = ["river", "road", "border", "border_or_coast", "line"];
  let selectedLevel = Number(presetData.active_level || p.simplification_level) || 3;
  el.innerHTML = `<section class="p5-controls" aria-labelledby="p5-control-title">
    <div class="p5-control-heading">
      <h4 id="p5-control-title">Map detail</h4>
      <output id="p5-level" class="p5-level" for="p5-slider"></output>
    </div>
    <input id="p5-slider" class="p5-slider" type="range" min="1" max="5" step="1"
      value="${selectedLevel}" aria-label="Level of map simplification"
      ${presetData.ready ? "" : "disabled"}>
    <div class="p5-scale" aria-hidden="true">
      ${Object.values(STEP5_LEVELS).map((v) => `<span>${v.short}</span>`).join("")}
    </div>
    ${presetData.ready ? "" : `<p class="p5-generate-note">${presetData.supported
      ? "Generate previews to enable the slider."
      : "Restart MapGen, then generate previews to enable the slider."}</p>`}
  </section>

  <details class="p5-advanced">
      <summary>Advanced controls</summary>
      <fieldset class="p5-options">
        <legend>Lines to keep</legend>
        <div class="p5-checks">${KINDS.map((k) => `<label><input type="checkbox" class="p5-kind" value="${k}"
          ${p.keep_line_kinds.includes(k) ? "checked" : ""}> ${LINE_KIND_LABELS[k]}</label>`).join("")}</div>
      </fieldset>
      <fieldset class="p5-options">
        <legend>Categories to always keep</legend>
        <div class="p5-checks">${data.classes.map((c) => `<label title="Covers ${pct(c.share)} of the map"><input type="checkbox"
          class="p5-prot" value="${c.index}"
          ${p.protected_classes.includes(c.index) ? "checked" : ""}> ${esc(c.label)}</label>`).join("")}</div>
      </fieldset>
      <div class="p5-pending" id="p5-pending" hidden>
        <span>Advanced changes not applied</span>
        <button type="button" class="btn primary" id="p5-update">Update previews</button>
      </div>
  </details>`;

  const slider = el.querySelector("#p5-slider");
  const showLevel = () => {
    el.querySelector("#p5-level").textContent = STEP5_LEVELS[+slider.value].label;
  };
  const advancedState = () => JSON.stringify({
    keep_line_kinds: [...el.querySelectorAll(".p5-kind:checked")].map((i) => i.value).sort(),
    protected_classes: [...el.querySelectorAll(".p5-prot:checked")].map((i) => +i.value).sort((a, b) => a - b),
  });
  const baseline = advancedState();
  const updatePending = () => {
    el.querySelector("#p5-pending").hidden = advancedState() === baseline;
  };
  const applyAdvanced = async () => {
    const body = {
      simplification_level: selectedLevel,
      min_texture_area_side_mm: null,
      smooth_mm: .5,
      preserve_share: .01,
      keep_line_kinds: [...el.querySelectorAll(".p5-kind:checked")].map((i) => i.value),
      protected_classes: [...el.querySelectorAll(".p5-prot:checked")].map((i) => +i.value),
    };
    const button = el.querySelector("#p5-update");
    button.disabled = true;
    try {
      await api(`/api/step5params/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      runSteps([5]);
    } catch (err) {
      button.disabled = false;
      alert(err.message);
    }
  };
  slider.oninput = () => {
    selectedLevel = +slider.value;
    showLevel();
    const body = el.closest(".step-body");
    body._step5PreviewLevel = selectedLevel;
    if (body._showStep5Preset) body._showStep5Preset(selectedLevel);
  };
  slider.onchange = async () => {
    const main = $("main");
    const scrollY = main.scrollTop;
    try {
      await api(`/api/step5preset/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: selectedLevel }) });
      await loadMaps();
      const current = mapRec();
      if (current) {
        renderSidebar();
        await refreshCanonicalDownstreamCards(current);
      }
      requestAnimationFrame(() => { main.scrollTop = scrollY; });
    } catch (err) { alert(err.message); }
  };
  el.querySelectorAll(".p5-advanced input").forEach((inp) => { inp.onchange = updatePending; });
  el.querySelector("#p5-update").onclick = applyAdvanced;
  const stepBody = el.closest(".step-body");
  stepBody._step5PreviewLevel = selectedLevel;
  if (stepBody._showStep5Preset) stepBody._showStep5Preset(selectedLevel);
  showLevel();
}

/* ------------------------------------------------------------------ artifact views */

const chip = (c) => `<span class="chip" style="background:${esc(c)}"></span>`;
const pct = (v) => (v * 100).toFixed(1) + "%";

const RECOGNITION_STATUS = {
  "text-confirmed": ["readings agree", "confirmed"],
  "partial-text-match": ["partial text match", "partial"],
  "geometry-only": ["location match only", "geometry"],
  "gemini-only": ["Gemini only", "single"],
  "easyocr-only": ["EasyOCR only", "local-only"],
  "legacy-match": ["legacy fused result", "legacy"],
};

function recognitionStatus(status, similarity) {
  const [label, cls] = RECOGNITION_STATUS[status] || [status || "unknown", "legacy"];
  const score = Number.isFinite(similarity)
    ? `<span class="subvalue">${Math.round(similarity * 100)}% text similarity</span>` : "";
  return `<span class="status-pill ${cls}">${esc(label)}</span>${score}`;
}

function boxSource(localization) {
  if (localization === "craft" || localization === "craft-only") return "CRAFT box";
  if (localization === "gemini-unverified") return "Gemini box (unconfirmed)";
  return "Gemini box";
}

function removalResult(label, occurrence) {
  if (occurrence?.reviewed && !occurrence.remove) {
    return '<span class="cross">kept by review</span>';
  }
  if (label.mask_found) return '<span class="tick">precise stroke mask</span>';
  if (occurrence?.reviewed && occurrence.remove) {
    return '<span class="tick">whole detected box</span>';
  }
  if (label.kind === "city" || label.kind === "capital") {
    return '<span class="tick">automatic city box</span>';
  }
  return '<span class="cross">save review to remove box</span>';
}

function smallHash(value) {
  let hash = 0;
  for (const ch of String(value)) hash = ((hash << 5) - hash + ch.charCodeAt(0)) | 0;
  return Math.abs(hash);
}

function reviewState(occurrence) {
  if (occurrence.reviewed && occurrence.include) {
    return '<span class="status-pill confirmed">approved</span>';
  }
  if (occurrence.reviewed && !occurrence.include) {
    return '<span class="status-pill excluded">excluded</span>';
  }
  if (occurrence.needs_review) {
    return '<span class="status-pill needs-review">needs review</span>';
  }
  return '<span class="status-pill pending">not reviewed</span>';
}

const mapImageUrl = (name) => `/api/mapimg/${encodeURIComponent(name)}`;

function sourceImage(name) {
  const url = mapImageUrl(name);
  return `<a class="imglink" href="${url}" target="_blank">open full size ↗</a>
          <img class="artifact-img" src="${url}" alt="Original uploaded map">`;
}

function inputFiles(names) {
  return `<div class="input-files" aria-label="Exact input files">
    ${names.map((name) => `<code class="input-file">${esc(name)}</code>`).join("")}
  </div>`;
}

async function artifactExists(stem, name) {
  try {
    return (await fetch(artifactUrl(stem, name), { method: "HEAD" })).ok;
  } catch (_) {
    return false;
  }
}

async function renderStepInput(step, el, map) {
  const stem = map.stem;
  let content = "";
  let description = "";
  let files = [];

  if (step === 1) {
    description = "The original uploaded image is sent unchanged to semantic interpretation.";
    files = [map.name];
    content = sourceImage(map.name);
  } else if (step === 2) {
    description = "Layout isolation receives the original image plus the structured semantics from Step 1.";
    files = [map.name, "step1_semantics.json"];
    content = sourceImage(map.name);
  } else if (step === 3) {
    if (!map.steps[2]) {
      content = '<p class="hint">Available after Step 2 prepares the map-only image.</p>';
    } else {
      description = "This furniture-blanked raster is sent to Gemini for text reading and to CRAFT/EasyOCR for independent text localization and recognition. The retained white margin preserves labels that cross a coastline.";
      files = ["map_text_input.png", "step1_semantics.json", "geometry.json"];
      content = debugImage(stem, "map_text_input.png");
    }
  } else if (step === 4) {
    if (!map.steps[3]) {
      content = '<p class="hint">Available after Step 3 creates the overlay-text mask.</p>';
    } else {
      const [hasRemovalMask, hasExcludedPreview] = await Promise.all([
        artifactExists(stem, "text_removal_mask.png"),
        artifactExists(stem, "step4_text_removed_input.png"),
      ]);
      description = "Step 4 classifies the map raster only where both masks allow it. White pixels in the exact removal mask are excluded, then filled from the nearest segmented map region.";
      files = ["map_area.png", "map_mask.png", "text_mask.png",
        ...(hasRemovalMask ? ["text_removal_mask.png"] : []), "classes.json",
        "labels.json", "geometry.json", "step1_semantics.json"];
      content = `<div class="input-grid input-grid-three">
        <div><h5>Map raster</h5>${debugImage(stem, "map_area.png")}</div>
        <div><h5>Geographic mask</h5>${debugImage(stem, "map_mask.png")}</div>
        <div><h5>Precise text strokes detected</h5>${debugImage(stem, "text_mask.png")}</div>
        <div><h5>Exact text-removal mask used</h5>${debugImage(stem,
          hasRemovalMask ? "text_removal_mask.png" : "text_mask.png")}</div>
        ${hasExcludedPreview ? `<div><h5>Map after excluded pixels are blanked</h5>
          ${debugImage(stem, "step4_text_removed_input.png")}</div>` : ""}
      </div>`;
    }
  } else if (step === 5) {
    if (!map.steps[4]) {
      content = '<p class="hint">Available after Step 4 creates the indexed segmentation.</p>';
    } else {
      const hasCleanPreview = await artifactExists(stem, "label_map_preview.png");
      description = hasCleanPreview
        ? "A color rendering of the exact class-index raster passed to simplification; colors aid inspection but do not alter any pixels or geometry."
        : "Legacy result: the right half of this comparison renders the class-index raster. Re-run Step 4 to create the standalone input preview.";
      files = ["label_map.png", "classes_final.json", "lines.geojson", "config/output_spec.json"];
      content = debugImage(stem, hasCleanPreview ? "label_map_preview.png" : "step4_debug.png");
    }
  } else if (step === 6) {
    if (!map.steps[5]) {
      content = '<p class="hint">Available after Step 5 creates the generalized class map.</p>';
    } else {
      const hasCleanPreview = await artifactExists(stem, "label_map_gen_preview.png");
      description = hasCleanPreview
        ? "Visual reference for the surviving classes. Step 6 reads their exact area statistics from classes_gen.json rather than reading image pixels."
        : "Legacy visual reference: the right half renders the generalized classes. Step 6 itself reads their exact area statistics from classes_gen.json.";
      files = ["classes_gen.json", "step1_semantics.json", "config/output_spec.json"];
      content = debugImage(stem, hasCleanPreview ? "label_map_gen_preview.png" : "step5_debug.png");
    }
  } else if (step === 7) {
    if (!map.steps[6]) {
      content = '<p class="hint">Available after Steps 5–6 create the generalized map and aggregation plan.</p>';
    } else {
      const [hasCleanPreview, hasApprovedLabels] = await Promise.all([
        artifactExists(stem, "label_map_gen_preview.png"),
        artifactExists(stem, "approved_labels.json"),
      ]);
      description = hasCleanPreview
        ? `This generalized class raster is combined with the aggregation plan and generalized lines to assign tactile patterns. Final text coordinates use ${hasApprovedLabels ? "the reviewed label selections" : "the raw detections because no review has been saved yet"}.`
        : "Legacy result: the right half renders the generalized class raster. Re-run Step 5 to create the standalone input preview.";
      files = ["label_map_gen.png", "classes_gen.json", "lines_gen.geojson",
        "step5_summary.json", "aggregation.json", hasApprovedLabels ? "approved_labels.json" : "labels.json",
        "step1_semantics.json", "config/output_spec.json"];
      content = debugImage(stem, hasCleanPreview ? "label_map_gen_preview.png" : "step5_debug.png");
    }
  } else if (step === 8) {
    if (!map.steps[7]) {
      content = '<p class="hint">Available after Step 7 assigns patterns and renders its default-boundary master.</p>';
    } else {
      description = "Step 8 preserves the Step 7 artifact unchanged. It reads the same assignments and generalized class raster, rebuilds a clean pattern layer internally, evaluates every adjacency, and draws complete selected contours.";
      files = ["step7_tactile.png", "symbols.json", "label_map_gen.png"];
      content = debugImage(stem, "step7_tactile.png");
    }
  } else if (step === "8a") {
    if (!map.steps[8]) {
      content = '<p class="hint">Available after Step 8 creates the selective-boundary render.</p>';
    } else {
      description = "Step 8A leaves Step 8 unchanged. It gives every non-black component, including plain and waves, a complete closed boundary, then repaints only solid-black fills on top. Plain fill remains at the bottom.";
      files = ["step8_boundaries.png", "step8_boundaries.json", "symbols.json", "label_map_gen.png"];
      content = debugImage(stem, "step8_boundaries.png");
    }
  }

  el.innerHTML = `<details class="step-input">
    <summary class="step-input-heading">
      <span class="step-input-title">Input to this step</span>
      <span class="badge neutral">click to preview${files.length ? ` · ${files.length} file${files.length === 1 ? "" : "s"}` : ""}</span>
    </summary>
    <div class="step-input-content">
      ${description ? `<p class="hint">${esc(description)}</p>` : ""}
      ${files.length ? inputFiles(files) : ""}
      ${content}
    </div>
  </details>`;
}

async function renderRiverReview(el, stem, review) {
  const noAutomaticRivers = review.automatic_rivers.length === 0;
  const noRiverPaths = noAutomaticRivers && review.manual_rivers.length === 0;
  const remembered = RIVER_REVIEW_DISPLAY.get(stem);
  const includeRivers = typeof remembered?.includeRivers === "boolean"
    ? remembered.includeRivers : review.include_rivers;
  const fixedPaths = review.fixed_features.map((feature) => {
    const points = feature.geometry?.coordinates ?? [];
    return `<polyline class="river-fixed" points="${points.map((p) => p.join(",")).join(" ")}" />`;
  }).join("");
  const autoRows = review.automatic_rivers.map((river, index) => `<tr data-river-id="${esc(river.id)}">
    <td><input class="river-keep" type="checkbox" ${river.include ? "checked" : ""}></td>
    <td><input class="river-join-choice" type="checkbox"></td>
    <td>Automatic ${index + 1}</td>
    <td>${esc((river.properties.label_evidence ?? []).join(", ") || "unlabelled")}</td>
    <td>${river.points.length} vertices</td>
  </tr>`).join("");
  el.insertAdjacentHTML("beforeend", `
    <section class="river-review">
      <div class="river-master-bar">
        <label class="river-master-toggle">
          <input class="river-layer-enabled" type="checkbox" ${includeRivers ? "checked" : ""}>
          <span><strong>Include rivers in later steps</strong>
            <small>Check this to review, correct, and include river paths. Coastlines remain independent.</small></span>
        </label>
        <button class="btn primary river-review-save">Save river settings</button>
      </div>
      <div class="river-review-content" ${includeRivers ? "" : "hidden"}>
       <div class="river-review-heading">
        <div><h4>Review extracted rivers</h4>
          <p class="hint">Coastlines are locked. Uncheck incorrect segments, join related fragments, or draw a replacement over the map.</p></div>
      </div>
      <div class="review-summary">
        <span>${noRiverPaths
          ? '<span class="tick">Detection completed: 0 automatic river paths.</span>'
          : review.saved
          ? '<span class="tick">Saved river review is supplying <code>lines.geojson</code>.</span>'
          : '<span class="cross">Automatic paths currently supply <code>lines.geojson</code>.</span>'}</span>
        <span class="river-review-message" aria-live="polite"></span>
      </div>
      ${noAutomaticRivers ? `<div class="detection-empty-state" role="status">
        <strong>${noRiverPaths ? "No rivers detected." : "No automatic rivers detected."}</strong>
        <span>${noRiverPaths
          ? "Step 4 completed and found no automatic river paths. Nothing will be saved as a river unless you draw a path below."
          : `${review.manual_rivers.length} manually reviewed river path${review.manual_rivers.length === 1 ? " remains" : "s remain"}; no automatic paths were found in this run.`}</span>
      </div>` : ""}
      <div class="river-editor-layout">
        <div class="river-canvas-wrap">
          <svg class="river-editor" viewBox="0 0 ${review.width} ${review.height}"
               aria-label="Editable river paths over the map">
            <image href="${artifactUrl(stem, "map_area.png")}" x="0" y="0"
                   width="${review.width}" height="${review.height}" />
            <g class="river-fixed-layer">${fixedPaths}</g>
            <g class="river-auto-layer"></g>
            <g class="river-manual-layer"></g>
            <polyline class="river-draft" points="" />
          </svg>
          <p class="hint">In drawing mode, click points along the river and then finish the path.</p>
        </div>
        <div class="river-editor-controls">
          <div class="river-toolbar">
            <button class="btn ghost river-join">Join selected fragments</button>
            <button class="btn ghost river-draw">Draw replacement</button>
            <button class="btn ghost river-draw-undo" disabled>Undo point</button>
            <button class="btn ghost river-draw-finish" disabled>Finish path</button>
            <button class="btn ghost river-draw-cancel" disabled>Cancel</button>
          </div>
          <div class="table-scroll"><table class="data river-segment-table">
            <tr><th>keep</th><th>join</th><th>segment</th><th>label evidence</th><th>geometry</th></tr>
            ${autoRows || '<tr><td colspan="5">No automatic river segments.</td></tr>'}
          </table></div>
          <div class="river-manual-list"></div>
        </div>
      </div>
      </div>
    </section>`);

  const section = el.querySelector(".river-review");
  const svg = section.querySelector(".river-editor");
  const autoLayer = section.querySelector(".river-auto-layer");
  const manualLayer = section.querySelector(".river-manual-layer");
  const draftLine = section.querySelector(".river-draft");
  const message = section.querySelector(".river-review-message");
  const layerToggle = section.querySelector(".river-layer-enabled");
  const reviewContent = section.querySelector(".river-review-content");
  let manualRivers = review.manual_rivers.map((river) => ({
    ...river, points: river.points.map((point) => [...point]),
  }));
  let drawing = false;
  let draft = [];
  const rowFor = (id) => [...section.querySelectorAll(".river-segment-table tr")]
    .find((row) => row.dataset.riverId === id);
  const keepBox = (id) => rowFor(id)?.querySelector(".river-keep");
  const joinBox = (id) => rowFor(id)?.querySelector(".river-join-choice");
  const svgPoints = (points) => points.map((point) => point.join(",")).join(" ");
  const markDirty = () => {
    message.textContent = "Unsaved river changes";
    message.className = "river-review-message pending-change";
  };

  const nearestAutomaticPoint = (point) => {
    let best = null;
    review.automatic_rivers.filter((river) => keepBox(river.id)?.checked).forEach((river) => {
      for (let index = 0; index < river.points.length - 1; index += 1) {
        const start = river.points[index];
        const end = river.points[index + 1];
        const vx = end[0] - start[0];
        const vy = end[1] - start[1];
        const denominator = vx * vx + vy * vy;
        const t = denominator
          ? Math.max(0, Math.min(1, ((point[0] - start[0]) * vx + (point[1] - start[1]) * vy) / denominator))
          : 0;
        const projected = [start[0] + t * vx, start[1] + t * vy];
        const distance = Math.hypot(projected[0] - point[0], projected[1] - point[1]);
        if (!best || distance < best.distance) best = { point: projected, distance };
      }
    });
    return best && best.distance <= review.snap_tolerance_px ? best : null;
  };

  const snapEndpoints = (points) => {
    let snapped = 0;
    if (points.length < 2) return snapped;
    [0, points.length - 1].forEach((index) => {
      const match = nearestAutomaticPoint(points[index]);
      if (match) {
        points[index] = match.point.map((value) => Math.round(value * 100) / 100);
        snapped += 1;
      }
    });
    return snapped;
  };

  const renderEditor = () => {
    reviewContent.hidden = !layerToggle.checked;
    section.classList.toggle("rivers-disabled", !layerToggle.checked);
    section.querySelectorAll(".river-keep, .river-join-choice").forEach((checkbox) => {
      checkbox.disabled = !layerToggle.checked;
    });
    autoLayer.innerHTML = review.automatic_rivers.map((river) =>
      `<polyline class="river-auto ${keepBox(river.id)?.checked ? "kept" : "excluded"} ${joinBox(river.id)?.checked ? "joining" : ""}"
        data-river-id="${esc(river.id)}" points="${svgPoints(river.points)}" />`).join("");
    manualLayer.innerHTML = manualRivers.map((river) =>
      `<polyline class="river-manual" points="${svgPoints(river.points)}" />`).join("");
    draftLine.setAttribute("points", svgPoints(draft));
    section.querySelector(".river-manual-list").innerHTML = manualRivers.length
      ? `<h5>Reviewed paths</h5>${manualRivers.map((river, index) => `<div class="river-manual-row" data-manual-id="${esc(river.id)}">
          <span>${river.edit_kind === "joined" ? "Joined" : "Drawn"} path ${index + 1}</span>
          <input class="river-manual-label" type="text" maxlength="100"
                 value="${esc(river.label ?? "")}" placeholder="River name (optional)">
          <button class="btn ghost river-manual-delete">Delete</button>
        </div>`).join("")}` : '<p class="hint">No reviewed paths have been added.</p>';
    section.querySelectorAll(".river-manual-label, .river-manual-delete").forEach((control) => {
      control.disabled = !layerToggle.checked;
    });
    autoLayer.querySelectorAll(".river-auto").forEach((path) => {
      path.onclick = (event) => {
        if (drawing) return;
        event.stopPropagation();
        const checkbox = joinBox(path.dataset.riverId);
        if (checkbox) checkbox.checked = !checkbox.checked;
        renderEditor();
      };
    });
    section.querySelectorAll(".river-manual-row").forEach((row) => {
      row.querySelector(".river-manual-label").oninput = (event) => {
        const river = manualRivers.find((item) => item.id === row.dataset.manualId);
        if (river) river.label = event.target.value;
        markDirty();
      };
      row.querySelector(".river-manual-delete").onclick = () => {
        manualRivers = manualRivers.filter((item) => item.id !== row.dataset.manualId);
        markDirty(); renderEditor();
      };
    });
  };
  section.querySelectorAll(".river-keep, .river-join-choice").forEach((checkbox) => {
    checkbox.onchange = () => { markDirty(); renderEditor(); };
  });
  layerToggle.onchange = () => {
    if (!layerToggle.checked && drawing) {
      draft = []; drawing = false;
    }
    RIVER_REVIEW_DISPLAY.set(stem, { includeRivers: layerToggle.checked });
    markDirty(); setDrawState(false); renderEditor();
  };

  const joinCoordinates = (rivers) => {
    const remaining = rivers.map((river) => river.points.map((point) => [...point]));
    let joined = remaining.shift() ?? [];
    while (remaining.length) {
      const end = joined[joined.length - 1];
      let best = null;
      remaining.forEach((points, index) => {
        [[false, points[0]], [true, points[points.length - 1]]].forEach(([reverse, point]) => {
          const distance = (point[0] - end[0]) ** 2 + (point[1] - end[1]) ** 2;
          if (!best || distance < best.distance) best = { index, reverse, distance };
        });
      });
      let next = remaining.splice(best.index, 1)[0];
      if (best.reverse) next.reverse();
      joined = joined.concat(next);
    }
    return joined;
  };
  section.querySelector(".river-join").onclick = () => {
    if (!layerToggle.checked) return;
    const chosen = review.automatic_rivers.filter((river) => joinBox(river.id)?.checked);
    if (chosen.length < 2) {
      message.textContent = "Select at least two fragments in the Join column.";
      message.className = "river-review-message error";
      return;
    }
    manualRivers.push({
      id: `manual-${Date.now().toString(36)}`, label: "", edit_kind: "joined",
      points: joinCoordinates(chosen),
    });
    chosen.forEach((river) => {
      keepBox(river.id).checked = false;
      joinBox(river.id).checked = false;
    });
    markDirty(); renderEditor();
  };

  const setDrawState = (active) => {
    drawing = active && layerToggle.checked;
    section.querySelector(".river-join").disabled = !layerToggle.checked || drawing;
    section.querySelector(".river-draw").disabled = !layerToggle.checked || drawing;
    section.querySelector(".river-draw-undo").disabled = !active || !draft.length;
    section.querySelector(".river-draw-finish").disabled = !active || draft.length < 2;
    section.querySelector(".river-draw-cancel").disabled = !active;
    svg.classList.toggle("drawing", active);
  };
  const cancelDrawing = () => { draft = []; setDrawState(false); renderEditor(); };
  const finishDrawing = () => {
    if (draft.length < 2) return;
    const snapped = snapEndpoints(draft);
    manualRivers.push({
      id: `manual-${Date.now().toString(36)}`, label: "", edit_kind: "drawn",
      points: draft.map((point) => [...point]),
    });
    draft = []; setDrawState(false); markDirty(); renderEditor();
    if (snapped) {
      message.textContent = `${snapped} endpoint${snapped === 1 ? "" : "s"} snapped to a kept automatic river. Save to confirm.`;
      message.className = "river-review-message snapped";
    }
  };
  section.querySelector(".river-draw").onclick = () => {
    draft = []; setDrawState(true); renderEditor();
  };
  section.querySelector(".river-draw-undo").onclick = () => {
    draft.pop(); setDrawState(true); renderEditor();
  };
  section.querySelector(".river-draw-cancel").onclick = cancelDrawing;
  section.querySelector(".river-draw-finish").onclick = finishDrawing;
  svg.onclick = (event) => {
    if (!drawing) return;
    const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
    const mapped = point.matrixTransform(svg.getScreenCTM().inverse());
    draft.push([Math.max(0, Math.min(review.width - 1, mapped.x)),
                Math.max(0, Math.min(review.height - 1, mapped.y))]);
    setDrawState(true); renderEditor();
  };
  svg.ondblclick = (event) => {
    if (drawing) { event.preventDefault(); finishDrawing(); }
  };

  section.querySelector(".river-review-save").onclick = async (event) => {
    const button = event.currentTarget;
    button.disabled = true; button.textContent = "Saving...";
    const includeAutoIds = review.automatic_rivers
      .filter((river) => keepBox(river.id)?.checked).map((river) => river.id);
    try {
      const result = await api(`/api/linereview/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ include_rivers: layerToggle.checked,
          include_auto_ids: includeAutoIds, manual_rivers: manualRivers }),
      });
      if (result.downstream_invalidated) {
        alert("Reviewed rivers were saved. Step 5 and later results were cleared because they used the previous lines.");
      }
      await renderMap();
    } catch (error) {
      button.disabled = false; button.textContent = "Save river settings";
      message.textContent = error.message; message.className = "river-review-message error";
    }
  };
  setDrawState(false);
  renderEditor();
}


async function renderArtifacts(step, el) {
  const stem = SELECTED;
  el.insertAdjacentHTML("beforeend", `
    <div class="step-output-heading">
      <h4>Output from this step</h4>
      <span class="badge neutral">generated artifacts</span>
    </div>`);
  if (step === 1) {
    const sem = await artifactJson(stem, "step1_semantics.json");
    if (!sem) return;
    const inScope = ["area_class_chorochromatic", "choropleth", "isopleth",
      "classed_sequential"].includes(sem.map_type);
    el.insertAdjacentHTML("beforeend", `
      <div class="badges">
        <span class="badge">${esc(sem.map_type)}</span>
        <span class="badge">${esc(sem.data_ordering)}</span>
        <span class="badge">${inScope ? "in scope" : "OUT OF SCOPE"}</span>
        <span class="badge neutral">language: ${esc(sem.map_language || "not recorded (legacy result) — rerun Step 1")}</span>
        <span class="badge ${sem.water_present ? "" : "neutral"}">water: ${sem.water_present ? "yes" : "no"}</span>
        ${sem.title ? `<span class="badge neutral">title: ${esc(sem.title)}</span>` : ""}
      </div>
      <p><strong>${esc(sem.subject)}</strong></p>
      <details class="desc-box"><summary>full description (reading-guide source)</summary>
        <p>${esc(sem.description)}</p></details>
      <table class="data"><tr><th>#</th><th>thematic class</th><th>est. share</th></tr>
        ${sem.thematic_classes.map((c) =>
          `<tr><td>${c.priority}</td><td>${esc(c.label)}</td><td>${esc(c.approx_area_share_percent)}%</td></tr>`).join("")}
      </table>
      ${sem.non_thematic.length ? `<table class="data"><tr><th>#</th><th>non-thematic</th><th>why</th></tr>
        ${sem.non_thematic.map((f) =>
          `<tr><td>${f.priority}</td><td>${esc(f.name)}</td><td>${esc(f.reason)}</td></tr>`).join("")}</table>` : ""}
      <div class="badges">${sem.lines.map((l) => `<span class="badge neutral">${esc(l.kind)}</span>`).join("")}
        ${sem.overlay_text.capital_city ? `<span class="badge">capital: ${esc(sem.overlay_text.capital_city)}</span>` : ""}
      </div>`);
  }

  if (step === 2) {
    const cls = await artifactJson(stem, "classes.json");
    const layout = await artifactJson(stem, "step2_layout.json");
    const layoutRows = layout ? [
      ...((layout.map_areas || []).map((b, i) => [`map_area ${i + 1}: ${b.label}`, b])),
      ["legend", layout.legend], ["title", layout.title],
      ["scale_bar", layout.scale_bar], ["north_arrow", layout.north_arrow],
      ...((layout.other || []).map((b) => [`other: ${b.label}`, b])),
    ].filter(([, b]) => b).map(([name, b]) => `<tr><td>${esc(name)}</td><td><code>${esc(JSON.stringify(b.box_2d))}</code></td></tr>`).join("") : "";
    el.insertAdjacentHTML("beforeend", `
      <section class="substage">
        <h4>2.1 — Raw AI layout</h4>
        <p class="hint">Direct model output before Python changes the crop or removes furniture. Blue marks map_area; orange marks other components.</p>
        ${debugImage(stem, "step2_layout_debug.png")}
        ${layoutRows ? `<table class="data"><tr><th>AI component</th><th>normalized box [y0, x0, y1, x1]</th></tr>${layoutRows}</table>` : ""}
      </section>
      <section class="substage">
        <h4>2.2 — Python-refined map and legend</h4>
        <p class="hint">Yellow is the pixel mask; green is its final rectangular crop; orange regions are excluded furniture.</p>
        ${debugImage(stem, "step2_debug.png")}
      </section>
      <section class="substage">
        <h4>2.3 — Extracted inputs</h4>
        <p class="hint">The map preview is the exact furniture-blanked image used for overlay-text detection. The legend is extracted separately from the original image.</p>
        <div class="extracted-grid">
          <div>
            <h5>Map sent to text detection</h5>
            ${debugImage(stem, "map_text_input.png")}
          </div>
          <div>
            <h5>Legend used for colour sampling</h5>
            ${layout && layout.legend ? debugImage(stem, "legend.png")
              : '<p class="hint">No legend was detected.</p>'}
          </div>
        </div>
      </section>`);
    if (cls) {
      el.insertAdjacentHTML("beforeend", `
        <table class="data"><tr><th>color</th><th>legend entry</th><th>thematic</th><th>priority</th><th>hex</th></tr>
        ${cls.classes.map((c) => `<tr>
          <td>${c.hex ? chip(c.hex) : "—"}</td>
          <td>${esc(c.label)}</td>
          <td>${c.is_thematic ? '<span class="tick">✔</span>' : "—"}</td>
          <td>${c.priority ?? "—"}</td><td><code>${esc(c.hex ?? "—")}</code></td></tr>`).join("")}
        </table>
        ${warnList(cls.warnings)}`);
    }
  }

  if (step === 3) {
    const [lj, easyocrDetections, review] = await Promise.all([
      artifactJson(stem, "labels.json"),
      artifactJson(stem, "step3_craft.json"),
      api(`/api/labelreview/${encodeURIComponent(stem)}`).catch(() => null),
    ]);
    el.insertAdjacentHTML("beforeend", debugImage(stem, "step3_debug.png"));
    if (lj) {
      const localDetections = Array.isArray(easyocrDetections) ? easyocrDetections : [];
      const occurrences = review?.occurrences ?? lj.labels.map((label, index) => ({
        id: `legacy-${index}`, index, label, original_text: label.text,
        review_text: label.text, include: true, remove: true, reviewed: false,
        needs_review: label.recognition_status === "gemini-only",
        duplicate_key: String(label.text || "").toLocaleLowerCase(), duplicate_count: 1,
      }));
      const noTextDetected = occurrences.length === 0;
      const repeated = new Map();
      for (const occurrence of occurrences) {
        if (occurrence.duplicate_count > 1) {
          repeated.set(occurrence.duplicate_key,
            { text: occurrence.original_text, count: occurrence.duplicate_count });
        }
      }
      const rows = occurrences.map((occurrence) => {
        const l = occurrence.label;
        // Older labels.json files predate the comparison fields. Their CRAFT
        // box is exact, so join the cached EasyOCR row by box for display.
        const legacyLocal = localDetections.find((d) =>
          Array.isArray(d.box) && Array.isArray(l.box) && d.box.length === l.box.length &&
          d.box.every((v, i) => Math.abs(v - l.box[i]) <= 1));
        const geminiText = l.gemini_text !== undefined
          ? l.gemini_text
          : (l.localization === "craft-only" ? null : l.text);
        const easyocrText = l.easyocr_text !== undefined
          ? l.easyocr_text
          : (legacyLocal?.text ?? (l.localization === "craft-only" ? l.text : null));
        const easyocrConf = l.easyocr_conf ?? legacyLocal?.conf ?? null;
        const status = l.recognition_status ??
          (l.localization === "craft-only" ? "easyocr-only" :
           l.localization === "craft" ? "legacy-match" : "gemini-only");
        const duplicateClass = occurrence.duplicate_count > 1
          ? ` duplicate-row duplicate-group-${smallHash(occurrence.duplicate_key) % 6}` : "";
        const duplicateBadge = occurrence.duplicate_count > 1
          ? `<span class="duplicate-badge">${occurrence.duplicate_count} occurrences</span>` : "";
        return `<tr class="label-review-row${duplicateClass} ${occurrence.include ? "" : "review-excluded"}"
            data-review-id="${esc(occurrence.id)}" data-index="${occurrence.index}">
          <td class="review-choice">
            <label><input class="label-review-include" type="checkbox"
              ${occurrence.include ? "checked" : ""}> final overlay</label>
            <label><input class="label-review-remove" type="checkbox"
              ${occurrence.remove ? "checked" : ""}> remove text</label>
            <span class="review-state">${reviewState(occurrence)}</span>
          </td>
          <td><img class="label-crop" loading="lazy"
            src="/api/labelcrop/${encodeURIComponent(stem)}/${occurrence.index}?t=${Date.now()}"
            alt="Image crop for ${esc(occurrence.original_text)}">
            <span class="subvalue">box ${esc(JSON.stringify(l.box))}</span></td>
          <td>${chip(KIND_COLORS[l.kind] || "#999")}${esc(l.kind)}</td>
          <td><input class="label-review-text" type="text" maxlength="200"
            value="${esc(occurrence.review_text)}" aria-label="Final overlay text">
            ${occurrence.review_text !== occurrence.original_text
              ? `<span class="subvalue">detected as ${esc(occurrence.original_text)}</span>` : ""}
            ${duplicateBadge}</td>
          <td>${geminiText ? esc(geminiText) : "—"}</td>
          <td>${easyocrText ? esc(easyocrText) : "—"}
            ${Number.isFinite(easyocrConf) ? `<span class="subvalue">confidence ${Math.round(easyocrConf * 100)}%</span>` : ""}</td>
          <td>${recognitionStatus(status, l.text_similarity)}</td>
          <td>${esc(boxSource(l.localization ?? "gemini"))}</td>
          <td>${esc((l.anchor_source || "box_center").replaceAll("_", " "))}</td>
          <td>${removalResult(l, occurrence)}</td>
          <td>${l.matches_step1 ? '<span class="tick">yes</span>' : '<span class="cross">not mentioned</span>'}</td>
        </tr>`;
      }).join("");
      const repeatedSummary = [...repeated.values()].map((item) =>
        `<span class="badge duplicate-summary">${esc(item.text)} × ${item.count}</span>`).join("");
      const approvedCount = occurrences.filter((o) => o.reviewed && o.include).length;
      const excludedCount = occurrences.filter((o) => o.reviewed && !o.include).length;
      const removalCount = occurrences.filter((o) => o.reviewed && o.remove).length;
      el.insertAdjacentHTML("beforeend", `
        <section class="label-review" aria-labelledby="label-review-title">
        <div class="label-review-heading">
          <div><h4 id="label-review-title">Review text for the final overlay</h4>
            <p class="hint">Each row is one physical occurrence. “Remove text” controls segmentation; “Final overlay” controls whether the corrected text is placed back later. Saving never changes raw <code>labels.json</code>.</p></div>
          <button class="btn primary label-review-save" ${review ? "" : "disabled"}>Save reviewed overlay labels</button>
        </div>
        ${noTextDetected ? `<div class="detection-empty-state" role="status">
          <strong>No overlaying text detected.</strong>
          <span>Step 3 completed and returned zero overlay-text occurrences. There is nothing to review or save for the final overlay.</span>
        </div>` : ""}
        <div class="review-summary">
          ${noTextDetected
            ? '<span class="tick">Detection completed: 0 overlay-text occurrences.</span>'
            : review?.saved
            ? `<span class="tick">Saved review: ${removalCount} removed from segmentation; ${approvedCount} kept for final overlay; ${excludedCount} excluded from overlay</span>`
            : '<span class="cross">Not reviewed yet. Gemini-only and repeated readings need attention.</span>'}
          <span class="review-save-message" aria-live="polite"></span>
        </div>
        ${repeatedSummary ? `<div class="duplicate-summaries"><strong>Repeated readings:</strong> ${repeatedSummary}</div>` : ""}
        <p class="hint">Gemini supplies classification and its preferred reading. EasyOCR combines CRAFT localization with local character recognition. Agreement describes the readings; box source describes geometry. Repeated names are grouped visually but never deleted automatically.</p>
        <div class="table-scroll"><table class="data recognition-table">
          <tr><th>review actions</th><th>image crop</th><th>kind</th><th>editable text</th><th>Gemini read</th><th>EasyOCR read</th>
            <th>recognition agreement</th><th>box source</th><th>anchor</th><th>text removal</th><th>Step 1 context</th></tr>
          ${rows || `<tr><td colspan="11">no overlay text on this map</td></tr>`}
        </table></div>
        ${warnList(lj.warnings)}</section>`);

      const reviewSection = el.querySelector(".label-review");
      const saveButton = reviewSection.querySelector(".label-review-save");
      const saveMessage = reviewSection.querySelector(".review-save-message");
      const markDirty = () => {
        saveMessage.textContent = "Unsaved changes";
        saveMessage.className = "review-save-message pending-change";
      };
      reviewSection.querySelectorAll(".label-review-row").forEach((row) => {
        const checkbox = row.querySelector(".label-review-include");
        checkbox.onchange = () => {
          row.classList.toggle("review-excluded", !checkbox.checked);
          markDirty();
        };
        row.querySelector(".label-review-remove").onchange = markDirty;
        row.querySelector(".label-review-text").oninput = markDirty;
      });
      if (review) saveButton.onclick = async () => {
        const decisions = [...reviewSection.querySelectorAll(".label-review-row")].map((row) => ({
          id: row.dataset.reviewId,
          include: row.querySelector(".label-review-include").checked,
          remove: row.querySelector(".label-review-remove").checked,
          text: row.querySelector(".label-review-text").value.trim(),
        }));
        saveButton.disabled = true;
        saveButton.textContent = "Saving…";
        try {
          const result = await api(`/api/labelreview/${encodeURIComponent(stem)}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ decisions }),
          });
          if (result.warning) alert(result.warning);
          if (result.segmentation_invalidated) {
            alert("Removal choices changed. Step 4 and later results were cleared so segmentation cannot use an old text mask. Run the remaining steps again.");
          }
          await renderMap();
        } catch (err) {
          saveButton.disabled = false;
          saveButton.textContent = "Save reviewed overlay labels";
          saveMessage.textContent = err.message;
          saveMessage.className = "review-save-message error";
        }
      };
    }
  }

  if (step === 4) {
    const [cf, counts, lineExtraction, lineReview] = await Promise.all([
      artifactJson(stem, "classes_final.json"),
      api(`/api/geocounts/${encodeURIComponent(stem)}`),
      artifactJson(stem, "line_extraction.json"),
      api(`/api/linereview/${encodeURIComponent(stem)}`).catch(() => null),
    ]);
    const [hasLinesPreview, hasCoastlineCleanup, hasRiverCleanup] = await Promise.all([
      artifactExists(stem, "step4_lines_preview.png"),
      artifactExists(stem, "coastline_cleanup_mask.png"),
      artifactExists(stem, "river_cleanup_mask.png"),
    ]);
    el.insertAdjacentHTML("beforeend", `
      <div class="p5-compare-labels"><span>Original map (text still visible)</span><span>Segmented reconstruction (text areas filled)</span></div>
      <p class="hint">The left side is only a reference. The right side is the actual class reconstruction sent onward; removed text pixels have been assigned from surrounding segmented regions.</p>`);
    el.insertAdjacentHTML("beforeend", debugImage(stem, "step4_debug.png"));
    if (lineReview) await renderRiverReview(el, stem, lineReview);
    if (hasCoastlineCleanup) {
      const cleanup = lineExtraction?.coastline_cleanup;
      el.insertAdjacentHTML("beforeend", `
        <details class="line-preview-details coastline-cleanup-details">
          <summary class="line-preview-heading">
            <span class="line-preview-title">Coastline ink cleanup</span>
            <span class="badge neutral">${cleanup?.pixels ?? "?"} pixels excluded Â· click to preview</span>
          </summary>
          <div class="line-preview-content">
            <p class="hint">White pixels are dark printed outline pixels removed before colour assignment. Candidates must touch the geographic silhouette and stay within an adaptive ${cleanup?.band_width_px ?? "?"} px inward band. The final fill uses surviving interior area classes.</p>
            ${debugImage(stem, "coastline_cleanup_mask.png")}
          </div>
        </details>`);
    }
    if (hasRiverCleanup) {
      const cleanup = lineExtraction?.river_cleanup;
      el.insertAdjacentHTML("beforeend", `
        <details class="line-preview-details river-cleanup-details">
          <summary class="line-preview-heading">
            <span class="line-preview-title">River ink cleanup</span>
            <span class="badge neutral">${cleanup?.pixels ?? "?"} pixels excluded Â· click to preview</span>
          </summary>
          <div class="line-preview-content">
            <p class="hint">White pixels are the automatic image-supported river centerlines plus dark or neutral source ink within ${cleanup?.fringe_radius_px ?? 2} pixels. Reviewed paths, manual drawings, and graph-only bridges are never added to this segmentation mask.</p>
            <div class="badges">
              <span class="badge neutral">${cleanup?.centerline_pixels ?? 0} centerline pixels</span>
              <span class="badge neutral">${cleanup?.fringe_pixels ?? 0} dark fringe pixels</span>
            </div>
            ${debugImage(stem, "river_cleanup_mask.png")}
          </div>
        </details>`);
    }
    if (hasLinesPreview) {
      const lineKey = Object.entries(counts.line_kinds).map(([kind, count]) => `
        <span class="line-kind-key">
          <span class="line-kind-swatch" style="background:${LINE_KIND_COLORS[kind] || LINE_KIND_COLORS.line}"></span>
          ${esc(LINE_KIND_LABELS[kind] || kind)}: ${count}
        </span>`).join("");
      el.insertAdjacentHTML("beforeend", `
        <details class="line-preview-details">
          <summary class="line-preview-heading">
            <span class="line-preview-title">Extracted line layer</span>
            <span class="badge neutral">${counts.polylines ?? "?"} polylines · click to preview</span>
          </summary>
          <div class="line-preview-content">
            <p class="hint">Only saved centerlines are colored. Coastlines come from the geographic mask. Rivers follow dark pixel ridges selected near reviewed river labels; short label-covered gaps are joined by a local least-cost image path. Labels guide the search but never supply coordinates. All coordinates and evidence are saved in <code>lines.geojson</code>.</p>
            ${lineExtraction?.unmatched_river_labels?.length ? `<p class="line-warning"><strong>${lineExtraction.unmatched_river_labels.length} river-label occurrence(s) had no reliable nearby pixel ridge.</strong> They were left unmatched rather than converted into invented linework.</p>` : ""}
            ${lineExtraction ? `<div class="badges">
              <span class="badge neutral">${lineExtraction.boundary_features ?? 0} mask-derived boundaries</span>
              <span class="badge neutral">${lineExtraction.river_label_seeds ?? 0} river-label seeds</span>
              <span class="badge neutral">${lineExtraction.river_features ?? 0} pixel-derived river segments</span>
            </div>` : ""}
            ${lineKey ? `<div class="line-kind-legend">${lineKey}</div>` : ""}
            ${debugImage(stem, "step4_lines_preview.png")}
          </div>
        </details>`);
    }
    if (cf) {
      const rows = cf.classes.filter((c) => c.area_share >= 0.001)
        .sort((a, b) => b.area_share - a.area_share)
        .map((c) => `<tr>
          <td>${chip(`rgb(${c.rgb.join(",")})`)}${esc(c.label)}</td>
          <td>${pct(c.area_share)}</td>
          <td>${c.is_thematic ? '<span class="tick">thematic</span>' : esc(c.source)}</td>
        </tr>`).join("");
      const kinds = Object.entries(counts.line_kinds).map(([k, v]) =>
        `<span class="badge neutral">${esc(k)}: ${v}</span>`).join("");
      el.insertAdjacentHTML("beforeend", `
        <div class="badges">
          <span class="badge">${counts.polygons ?? "?"} polygons</span>
          <span class="badge">${counts.polylines ?? "?"} polylines</span>${kinds}
        </div>
        <table class="data"><tr><th>class</th><th>area</th><th>origin</th></tr>${rows}</table>
        ${cf.notes.length ? `<ul class="notelist">${cf.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`);
    }
  }

  if (step === 5) await renderStep5(el, stem);

  if (step === 6) {
    const agg = await artifactJson(stem, "aggregation.json");
    if (!agg) return;
    el.insertAdjacentHTML("beforeend", `
      <div class="badges">
        <span class="badge">${esc(agg.mode)}</span>
        <span class="badge">${agg.groups.length} groups / ${agg.slots} slots</span>
        <span class="badge ${agg.water ? "" : "neutral"}">water: ${agg.water ? esc(agg.water.label) : "none"}</span>
      </div>
      <table class="data"><tr><th>group</th><th>merged classes</th><th>rationale</th></tr>
      ${agg.groups.map((g) => `<tr><td><strong>${esc(g.label)}</strong></td>
        <td>${g.member_labels.map(esc).join(", ")}</td><td>${esc(g.rationale)}</td></tr>`).join("")}
      </table>
      ${agg.non_thematic_extra.length ? `<p class="hint">non-thematic without a slot claim:
        ${agg.non_thematic_extra.map((e) => esc(e.label)).join(", ")}</p>` : ""}
      ${warnList(agg.notes)}`);
    if (agg.review_required) {
      if (!agg.source_classes?.length) {
        el.insertAdjacentHTML("beforeend", '<p class="line-warning">This is a legacy Step 6 result. Re-run Step 6 once to create its integrated review.</p>');
      } else {
        const reviewData = await api(`/api/aggregation-review/${encodeURIComponent(stem)}`);
        await renderAggregationReviewEditor(
          el, stem, agg, reviewData, `/api/aggregation-review/${encodeURIComponent(stem)}`,
          "Review Step 6 final categories", "canonical-aggregation-review");
      }
    } else {
      el.insertAdjacentHTML("beforeend", '<p class="msg ok">No class merge is proposed, so no approval is required.</p>');
    }
  }

  if (step === 7) {
    const [sym, overlayLabels] = await Promise.all([
      artifactJson(stem, "symbols.json"),
      artifactJson(stem, "overlay_labels.json"),
    ]);
    el.insertAdjacentHTML("beforeend", debugImage(stem, "step7_tactile.png"));
    if (!sym) return;
    el.insertAdjacentHTML("beforeend", `
      ${overlayLabels ? `<div class="badges">
        <span class="badge">${overlayLabels.labels?.length ?? 0} overlay labels saved</span>
        <span class="badge ${overlayLabels.review_source === "approved_labels.json" ? "" : "neutral"}">
          ${overlayLabels.review_source === "approved_labels.json" ? "reviewed selections" : "raw detections — review not saved"}
        </span></div>` : ""}
      <table class="data"><tr><th>area</th><th>tactile pattern</th><th>why</th></tr>
      ${sym.area_assignments.map((a) => `<tr>
        <td>${esc(a.label)}</td><td><code>${esc(a.pattern)}</code> — ${esc(a.pattern_desc)}</td>
        <td>${esc(a.rationale)}</td></tr>`).join("")}
      </table>
      <div class="badges">${Object.entries(sym.line_styles).map(([k, v]) =>
        `<span class="badge neutral">${esc(k)}: ${esc(v.desc)}</span>`).join("")}</div>
      ${warnList(sym.notes)}`);
  }

  if (step === 8) {
    const boundaries = await artifactJson(stem, "step8_boundaries.json");
    el.insertAdjacentHTML("beforeend", `
      <div class="p5-compare-labels"><span>Step 7 default boundaries</span><span>Step 8 selective boundaries</span></div>
      ${debugImage(stem, "step8_debug.png")}`);
    if (!boundaries) return;
    const priority = boundaries.active_priority_patterns || [];
    const drawn = (boundaries.adjacencies || []).filter((edge) => edge.boundary_drawn);
    el.insertAdjacentHTML("beforeend", `
      <div class="badges">
        <span class="badge">white stroke: ${esc(boundaries.white_stroke_mm)} mm</span>
        <span class="badge">black stroke: ${esc(boundaries.black_stroke_mm)} mm</span>
        <span class="badge neutral">${drawn.length} selected adjacency type(s)</span>
        <span class="badge neutral">${boundaries.black_closure_components ?? 0} black closure component(s)</span>
      </div>
      <p class="hint">Patterns with map-wide priority: ${priority.length
        ? priority.map((pattern) => `<code>${esc(pattern)}</code>`).join(", ")
        : "none"}.</p>
      <table class="data"><tr><th>side A</th><th>side B</th><th>decision</th></tr>
      ${(boundaries.adjacencies || []).map((edge) => `<tr>
        <td>${esc(edge.side_a.label)} <code>${esc(edge.side_a.pattern)}</code></td>
        <td>${esc(edge.side_b.label)} <code>${esc(edge.side_b.pattern)}</code></td>
        <td>${edge.boundary_drawn ? '<span class="tick">boundary</span>' : 'none'} — ${esc(edge.reason)}</td>
      </tr>`).join("")}</table>`);
  }

  if (step === "8a") {
    const cleanup = await artifactJson(stem, "step8a_cleanup.json");
    el.insertAdjacentHTML("beforeend", `
      <div class="p5-compare-labels"><span>Step 8 boundaries</span><span>Step 8A component cleanup</span></div>
      ${debugImage(stem, "step8a_debug.png")}`);
    if (!cleanup) return;
    el.insertAdjacentHTML("beforeend", `
      <div class="badges">
        <span class="badge">${cleanup.owner_groups?.length ?? 0} boundary-owner group(s)</span>
        <span class="badge neutral">${cleanup.repainted_components ?? 0} top component layer(s)</span>
        <span class="badge neutral">${cleanup.restored_pixels ?? 0} pixels restored</span>
      </div>
      <p class="hint">Every non-black component, including plain and waves, receives a complete outside contour. Only solid-black fills are repainted above it; plain fill remains underneath, without changing any Step 8 artifact.</p>
      <table class="data"><tr><th>top component group</th><th>fill</th><th>components</th><th>restored pixels</th></tr>
      ${(cleanup.repainted_groups || []).map((group) => `<tr>
        <td>${esc(group.label)}</td><td><code>${esc(group.pattern)}</code></td>
        <td>${group.components}</td><td>${group.restored_pixels}</td>
      </tr>`).join("")}</table>`);
  }
}

function step5ClassRows(classes) {
  return classes.filter((c) => c.area_share > 0 || c.area_share_before >= 0.001)
    .sort((a, b) => b.area_share - a.area_share)
    .map((c) => `<tr>
      <td>${chip(`rgb(${c.rgb.join(",")})`)}${esc(c.label)}</td>
      <td>${pct(c.area_share_before)}</td>
      <td>${c.area_share > 0 ? pct(c.area_share) : '<span class="cross">gone</span>'}</td>
      <td>${c.is_thematic ? '<span class="tick">thematic</span>' : esc(c.source)}</td>
    </tr>`).join("");
}

async function renderStep5(el, stem) {
  const [sum, cg, counts, presetData] = await Promise.all([
    artifactJson(stem, "step5_summary.json"),
    artifactJson(stem, "classes_gen.json"),
    api(`/api/geocounts/${encodeURIComponent(stem)}?gen=1`),
    step5PresetData(stem),
  ]);
  if (!sum || !cg) return;
  const rows = step5ClassRows(cg.classes);
  el.insertAdjacentHTML("beforeend", `
    <section class="p5-review" aria-labelledby="p5-review-title">
      <h4 id="p5-review-title">Review the adjusted map</h4>
      <div class="p5-compare-labels"><span>Original map</span><span>Adjusted map preview</span></div>
      <a class="imglink p5-preview-link" href="${artifactUrl(stem, "step5_debug.png")}" target="_blank">open full size ↗</a>
      <img class="artifact-img p5-preview-image" src="${artifactUrl(stem, "step5_debug.png")}" alt="Adjusted map preview">
      <div class="p5-result-stats">
        <div><strong data-p5-stat="merged">${sum.dissolved_components}</strong><span>small regions merged</span></div>
        <div><strong data-p5-stat="dropped">${sum.islands.dropped}</strong><span>tiny islands removed</span></div>
        <div><strong data-p5-stat="enlarged">${sum.islands.exaggerated}</strong><span>small islands enlarged</span></div>
        <div><strong data-p5-stat="polygons">${counts.polygons ?? "?"}</strong><span>regions in preview</span></div>
      </div>
    </section>
    <details class="p5-production-details">
      <summary>Production details</summary>
      <div class="badges">
        <span class="badge" data-p5-detail="scale">${sum.scale_mm_per_px} mm/px</span>
        <span class="badge" data-p5-detail="orientation">${esc(sum.orientation)}</span>
        <span class="badge" data-p5-detail="size">map ${sum.map_size_mm[0]}×${sum.map_size_mm[1]} mm</span>
        <span class="badge neutral" data-p5-detail="texture">smallest texture ≈ ${sum.min_texture_area_px} px²</span>
        <span class="badge" data-p5-detail="lines">${counts.polylines ?? "?"} lines (${sum.line_joins} joins, ${sum.lines_dropped_short} dropped short)</span>
      </div>
      <table class="data"><thead><tr><th>Map category</th><th>Area before</th><th>Area after</th><th>Type</th></tr></thead>
        <tbody class="p5-class-rows">${rows}</tbody></table>
      <div class="p5-vanished">${sum.classes_vanished.length
        ? `<ul class="notelist"><li>Categories removed: ${sum.classes_vanished.map(esc).join(", ")}</li></ul>` : ""}</div>
    </details>`);

  const body = el.closest(".step-body");
  body._showStep5Preset = (level) => {
    const variant = presetData.variants[String(level)];
    if (!variant) return;
    const s = variant.summary;
    const url = artifactUrl(stem, variant.debug_artifact);
    el.querySelector(".p5-preview-link").href = url;
    el.querySelector(".p5-preview-image").src = url;
    el.querySelector('[data-p5-stat="merged"]').textContent = s.dissolved_components;
    el.querySelector('[data-p5-stat="dropped"]').textContent = s.islands.dropped;
    el.querySelector('[data-p5-stat="enlarged"]').textContent = s.islands.exaggerated;
    el.querySelector('[data-p5-stat="polygons"]').textContent = variant.polygons;
    el.querySelector('[data-p5-detail="scale"]').textContent = `${s.scale_mm_per_px} mm/px`;
    el.querySelector('[data-p5-detail="orientation"]').textContent = s.orientation;
    el.querySelector('[data-p5-detail="size"]').textContent = `map ${s.map_size_mm[0]}×${s.map_size_mm[1]} mm`;
    el.querySelector('[data-p5-detail="texture"]').textContent = `smallest texture ≈ ${s.min_texture_area_px} px²`;
    el.querySelector('[data-p5-detail="lines"]').textContent =
      `${variant.polylines} lines (${s.line_joins} joins, ${s.lines_dropped_short} dropped short)`;
    el.querySelector(".p5-class-rows").innerHTML = step5ClassRows(variant.classes);
    el.querySelector(".p5-vanished").innerHTML = s.classes_vanished.length
      ? `<ul class="notelist"><li>Categories removed: ${s.classes_vanished.map(esc).join(", ")}</li></ul>`
      : "";
  };
  if (body._step5PreviewLevel) body._showStep5Preset(body._step5PreviewLevel);
}

/* ------------------------------------------------ alternate Steps 5-7 */

async function renderAltMapGen(el, map) {
  el.innerHTML = `<header class="alt-mapgen-header">
    <div><span class="alt-kicker">Independent comparison pipeline</span>
      <h2>Alt MapGen</h2>
      <p>Aggregates the complete Step 4 classification first, then simplifies those approved final categories for touch with canonical Step 5's algorithm. Canonical Steps 5–7 remain unchanged.</p></div>
  </header><div class="alt-mapgen-steps"></div>`;
  const host = el.querySelector(".alt-mapgen-steps");
  for (const step of [5, 6, 7]) {
    const panel = document.createElement("div");
    panel.className = "alt-host";
    panel.dataset.altStep = String(step);
    host.appendChild(panel);
    await renderAltBranch(step, panel, map);
  }
}

async function renderAltBranch(step, el, map) {
  const done = Boolean(map.alt_steps?.[step]);
  const busy = Boolean(map.job && map.job.status === "running");
  const prerequisiteReady = step === 5 ? Boolean(map.steps?.[4])
    : step === 6 ? Boolean(map.alt_steps?.[5] && map.alt_step5_review_ready)
    : Boolean(map.alt_steps?.[6]);
  const open = OPEN_ALT_STEPS.has(step);
  el.innerHTML = `<details class="alt-branch" data-alt-step="${step}" ${open ? "open" : ""}>
    <summary class="alt-summary">
      <div class="alt-heading">
        <div><span class="alt-kicker">Experimental comparison branch</span>
          <h4 id="alt-${step}-title">Alt Step ${step}</h4></div>
        <div class="step-actions alt-actions">
          <button class="btn primary alt-run" ${busy || !prerequisiteReady ? "disabled" : ""}>${done ? "Re-run" : "Run"} Alt Step ${step}</button>
          ${done ? `<button class="btn ghost alt-reset" ${busy ? "disabled" : ""}>Clear Alt ${step} and later</button>` : ""}
        </div>
      </div>
    </summary>
    <div class="alt-panel-body">
      <p class="hint">${step === 5
      ? "Builds a reviewed aggregation proposal from every surviving Step 4 category. It changes category identities only: no geographic pixel is moved or erased."
      : step === 6
      ? "Takes Alt Step 5's approved aggregated Step 4 raster and simplifies it with the same area algorithm and physical presets as canonical Step 5."
      : "Assigns textures and renders the simplified final-category raster from Alt Step 6. Alt Step 7 performs no further geographic simplification."}</p>
      <div class="alt-content"></div>
    </div>
  </details>`;
  const panel = el.querySelector(".alt-branch");
  const content = el.querySelector(".alt-content");
  const loadContent = async () => {
    if (content.dataset.loaded) return;
    content.dataset.loaded = "true";
    if (step === 5) await renderAltAggregationStep5(content, map, done);
    if (step === 6) await renderAltSimplificationStep6(content, map, done);
    if (step === 7) await renderAltStep7(content, map, done);
  };
  panel.ontoggle = () => {
    panel.open ? OPEN_ALT_STEPS.add(step) : OPEN_ALT_STEPS.delete(step);
    if (panel.open) loadContent().catch((error) => {
      content.innerHTML = `<p class="msg err">alternate branch failed to load: ${esc(error.message)}</p>`;
    });
  };
  el.querySelector(".alt-run").onclick = (event) => {
    event.preventDefault(); event.stopPropagation(); runAltSteps([step]);
  };
  const reset = el.querySelector(".alt-reset");
  if (reset) reset.onclick = async (event) => {
    event.preventDefault(); event.stopPropagation();
    if (!confirm(`Delete alternate artifacts from Alt Step ${step} onward?`)) return;
    await api("/api/reset-alt", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stem: map.stem, from_step: step }) });
    await loadMaps(); await renderMap();
  };
  if (panel.open) await loadContent();
}

async function renderAltStep5(el, map, done) {
  const stem = map.stem;
  const [paramsData, presets] = await Promise.all([
    api(`/api/alt-step5params/${encodeURIComponent(stem)}`),
    altStep5PresetData(stem),
  ]);
  const params = paramsData.params;
  let selectedLevel = Number(presets.active_level || params.simplification_level) || 3;
  const active = presets.variants[String(selectedLevel)];
  const summary = done ? await artifactJson(stem, "alt_step5_summary.json") : null;

  el.insertAdjacentHTML("beforeend", `<div class="alt-controls">
    <div class="p5-control-heading"><h5>Alternate map detail</h5>
      <output class="alt-level">${esc(STEP5_LEVELS[selectedLevel].label)}</output></div>
    <input class="p5-slider alt-slider" type="range" min="1" max="5" step="1"
      value="${selectedLevel}" ${presets.ready ? "" : "disabled"}>
    <div class="p5-scale" aria-hidden="true">${Object.values(STEP5_LEVELS).map((v) => `<span>${v.short}</span>`).join("")}</div>
    <p class="hint">This controls geographic cleanup before categories are grouped. It is intentionally gentler than the final tactile check: Alt Step 7 later applies the larger minimum required by each actual texture.</p>
    ${presets.ready ? "" : '<p class="p5-generate-note">Run Alt Step 5 to generate its five independent previews.</p>'}
  </div>`);

  if (!done || !summary) return;
  el.insertAdjacentHTML("beforeend", `<div class="alt-preview-grid">
    <div class="alt-main-preview"><h5>Alternate Step 5</h5>${debugImage(stem, "alt_label_map_gen_preview.png")}</div>
  </div>
  <div class="p5-result-stats alt-stats">
    <div><strong data-alt-stat="threshold">${summary.tactile_min_feature_mm} × ${summary.tactile_min_feature_mm} mm</strong><span>pre-aggregation cleanup target</span></div>
    <div><strong data-alt-stat="smoothing">${summary.boundary_smoothing_mm} mm</strong><span>boundary smoothing</span></div>
    <div><strong data-alt-stat="merged">${summary.whole_component_merges}</strong><span>undersized whole regions merged</span></div>
    <div><strong data-alt-stat="retained">${summary.unresolved_small_components}</strong><span>small regions retained for semantic safety</span></div>
  </div>`);

  const slider = el.querySelector(".alt-slider");
  const showVariant = (level) => {
    const variant = presets.variants[String(level)];
    if (!variant) return;
    el.querySelector(".alt-level").textContent = STEP5_LEVELS[level].label;
    const image = el.querySelector(".alt-main-preview img");
    const previewArtifact = `alt_step5_preset_${level}_label_map_gen_preview.png`;
    if (image) image.src = artifactUrl(stem, previewArtifact);
    const link = el.querySelector(".alt-main-preview .imglink");
    if (link) link.href = artifactUrl(stem, previewArtifact);
    el.querySelector('[data-alt-stat="threshold"]').textContent = `${variant.summary.tactile_min_feature_mm} × ${variant.summary.tactile_min_feature_mm} mm`;
    el.querySelector('[data-alt-stat="smoothing"]').textContent = `${variant.summary.boundary_smoothing_mm} mm`;
    el.querySelector('[data-alt-stat="merged"]').textContent = variant.summary.whole_component_merges;
    el.querySelector('[data-alt-stat="retained"]').textContent = variant.summary.unresolved_small_components;
  };
  slider.oninput = () => { selectedLevel = +slider.value; showVariant(selectedLevel); };
  slider.onchange = async () => {
    const main = $("main");
    const scrollY = main.scrollTop;
    await api(`/api/alt-step5preset/${encodeURIComponent(stem)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: selectedLevel }),
    });
    await loadMaps();
    const current = mapRec();
    for (const laterStep of [6, 7]) {
      const host = document.querySelector(`.alt-host[data-alt-step="${laterStep}"]`);
      if (host && current) await renderAltBranch(laterStep, host, current);
    }
    requestAnimationFrame(() => { main.scrollTop = scrollY; });
  };
  if (active) showVariant(selectedLevel);
}

async function renderAltAggregationStep5(el, map, done) {
  if (!done) {
    el.insertAdjacentHTML("beforeend", '<p class="hint">Run Alt Step 5 manually to propose final tactile categories from the complete Step 4 classification.</p>');
    return;
  }
  const [proposal, reviewData] = await Promise.all([
    artifactJson(map.stem, "alt_aggregation.json"),
    api(`/api/alt-aggregation-review/${encodeURIComponent(map.stem)}`),
  ]);
  const aggregationApproved = !proposal.review_required || Boolean(reviewData.review?.approved);
  el.insertAdjacentHTML("beforeend", `<div class="alt-step5-layout">
    <div><h5>${aggregationApproved ? "Approved aggregated Step 4 map" : "Proposed grouped geography"} — no simplification</h5>
      ${debugImage(map.stem, "alt_step5_aggregation_preview.png")}</div>
    <div><h5>Aggregation proposal</h5>
      <p class="hint">${esc(proposal.mode)}; ${proposal.groups.length} thematic group(s), using ${proposal.proposed_texture_count} of at most ${proposal.texture_ceiling} textures.</p>
      <table class="data"><thead><tr><th>Final category</th><th>Step 4 categories</th><th>Reason</th></tr></thead>
        <tbody>${aggregationRows(proposal)}</tbody></table></div>
  </div>${warnList(proposal.notes)}`);
  if (!proposal.review_required) {
    el.insertAdjacentHTML("beforeend", '<p class="msg ok">No approval is required. This aggregated Step 4 raster is now the input to Alt Step 6.</p>');
    return;
  }
  await renderAggregationReviewEditor(
    el, map.stem, proposal, reviewData,
    `/api/alt-aggregation-review/${encodeURIComponent(map.stem)}`,
    "Review merged categories before simplification", "alt-step5-review",
    Number(proposal.slots));
}

function aggregationRows(aggregation) {
  return (aggregation?.groups || []).map((group) => `<tr><td><strong>${esc(group.label)}</strong></td>
    <td>${group.member_labels.map(esc).join(", ")}</td><td>${esc(group.rationale || "")}</td></tr>`).join("");
}

async function renderAggregationReviewEditor(el, stem, proposal, reviewData,
                                              endpoint, title, extraClass = "",
                                              availableSlots = null) {
  const groups = (reviewData.effective_groups || proposal.groups).map(
    (group) => ({ ...group, members: [...(group.members || [])] }));
  const slotCount = Math.max(groups.length, Number(availableSlots) || 0);
  while (groups.length < slotCount) {
    const position = groups.length + 1;
    groups.push({
      label: `Final category ${position}`, members: [], member_labels: [],
      rationale: "created during review", approved: true,
    });
  }
  const source = proposal.source_classes || [];
  const assigned = new Map();
  groups.forEach((group, slot) => (group.members || []).forEach(
    (member) => assigned.set(Number(member), slot)));
  const status = reviewData.review?.status || "needs_review";
  const editors = groups.map((group, slot) => `<div class="alt-group-editor" data-group-slot="${slot}">
    <label>Final category ${slot + 1}<input class="alt-group-label" value="${esc(group.label || `Final category ${slot + 1}`)}"></label>
    <p class="alt-group-members"></p>
    <label class="alt-group-approval"><input type="checkbox" class="alt-group-approved" ${group.approved ? "checked" : ""}>
      Approve this multi-class merge</label>
    <input class="alt-group-rationale" value="${esc(group.rationale || "human-reviewed grouping")}" aria-label="Grouping rationale">
  </div>`).join("");
  const classRows = source.map((cl) => `<tr><td>${esc(cl.label)}</td><td><select class="alt-group-select" data-class-index="${cl.index}">
    ${groups.map((_, slot) => `<option value="${slot}" ${assigned.get(Number(cl.index)) === slot ? "selected" : ""}>Final category ${slot + 1}</option>`).join("")}
  </select></td></tr>`).join("");
  el.insertAdjacentHTML("beforeend", `<section class="alt-aggregation-review ${extraClass}">
    <div class="alt-review-heading"><div><h5>${esc(title)}</h5>
      <p class="hint">Review the actual merged categories before their shared geography is simplified. The texture limit is a maximum, not a target.</p></div>
      <span class="badge ${status === "approved" ? "" : "neutral"}">${esc(status)}</span></div>
    <div class="alt-group-editors">${editors}</div>
    <details><summary>Adjust which source classes belong together</summary>
      <table class="data alt-class-assignment"><thead><tr><th>Source category</th><th>Final tactile category</th></tr></thead><tbody>${classRows}</tbody></table>
    </details>
    <button class="btn primary aggregation-review-save">Save aggregation review</button>
    <p class="hint">Every multi-class merge must be approved before Alt Step 6 can run.</p>
  </section>`);

  const section = el.lastElementChild;
  const refresh = (changed = false) => {
    const selections = [...section.querySelectorAll(".alt-group-select")];
    groups.forEach((_, slot) => {
      const editor = section.querySelector(`[data-group-slot="${slot}"]`);
      const members = selections.filter((select) => Number(select.value) === slot)
        .map((select) => source.find(
          (cl) => Number(cl.index) === Number(select.dataset.classIndex))).filter(Boolean);
      editor.querySelector(".alt-group-members").textContent = members.length
        ? members.map((member) => member.label).join(" + ") : "Unused category slot";
      const approval = editor.querySelector(".alt-group-approved");
      approval.disabled = members.length <= 1;
      if (members.length <= 1) approval.checked = true;
      else if (changed) approval.checked = false;
      editor.classList.toggle("unused", members.length === 0);
    });
  };
  section.querySelectorAll(".alt-group-select").forEach((select) => {
    select.onchange = () => refresh(true);
  });
  refresh(false);
  section.querySelector(".aggregation-review-save").onclick = async () => {
    const selections = [...section.querySelectorAll(".alt-group-select")];
    const decisions = groups.map((_, slot) => {
      const editor = section.querySelector(`[data-group-slot="${slot}"]`);
      return {
        label: editor.querySelector(".alt-group-label").value,
        members: selections.filter((select) => Number(select.value) === slot)
          .map((select) => Number(select.dataset.classIndex)),
        approved: editor.querySelector(".alt-group-approved").checked,
        rationale: editor.querySelector(".alt-group-rationale").value,
      };
    }).filter((group) => group.members.length);
    try {
      await api(endpoint, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groups: decisions }) });
      if (extraClass === "canonical-aggregation-review") {
        // Step 6 review invalidates Steps 7 and 8. Rebuild these cards in
        // place rather than the complete page, which would move the viewport
        // into the Alt MapGen section appended at the bottom.
        const main = $("main");
        const scrollY = main.scrollTop;
        await loadMaps();
        const current = mapRec();
        if (current) {
          renderSidebar();
          await refreshCanonicalDownstreamCards(current);
        }
        requestAnimationFrame(() => { main.scrollTop = scrollY; });
      } else {
        const main = $("main");
        const scrollY = main.scrollTop;
        await loadMaps();
        const current = mapRec();
        renderSidebar();
        if (current) {
          for (const step of [5, 6, 7]) {
            const host = document.querySelector(`.alt-host[data-alt-step="${step}"]`);
            if (host) await renderAltBranch(step, host, current);
          }
        }
        requestAnimationFrame(() => { main.scrollTop = scrollY; });
      }
    } catch (error) { alert(error.message); }
  };
}

async function renderAltSimplificationStep6(el, map, done) {
  if (!map.alt_steps?.[5]) {
    const button = el.closest(".alt-branch")?.querySelector(".alt-run");
    if (button) button.disabled = true;
    el.insertAdjacentHTML("beforeend", '<p class="line-warning">Run Alt Step 5 first.</p>');
    return;
  }
  const reviewData = await api(`/api/alt-aggregation-review/${encodeURIComponent(map.stem)}`);
  if (reviewData.proposal.review_required && !reviewData.review?.approved) {
    const button = el.closest(".alt-branch")?.querySelector(".alt-run");
    if (button) button.disabled = true;
    el.insertAdjacentHTML("beforeend", '<p class="line-warning">Approve the Alt Step 5 category merges before simplifying their combined geography.</p>');
    return;
  }
  el.insertAdjacentHTML("beforeend", `<details class="production-details alt-step6-input">
    <summary>Input from Alt Step 5: approved aggregated Step 4 map</summary>
    <p class="hint">Categories assigned to the same final group now share one class identity. No geography has been simplified yet.</p>
    ${debugImage(map.stem, "alt_step5_aggregation_preview.png")}
  </details>`);
  if (!done) {
    el.insertAdjacentHTML("beforeend", '<p class="hint">Ready. Run Alt Step 6 manually to simplify this aggregated raster at all five canonical detail levels.</p>');
    return;
  }

  const stem = map.stem;
  const [paramsData, presets, summary, transitions] = await Promise.all([
    api(`/api/alt-step6params/${encodeURIComponent(stem)}`),
    altStep6PresetData(stem),
    artifactJson(stem, "alt_step6_summary.json"),
    artifactJson(stem, "alt_step6_transitions.json"),
  ]);
  let selectedLevel = Number(presets.active_level ||
    paramsData.params.simplification_level) || 3;
  el.insertAdjacentHTML("beforeend", `<div class="alt-controls">
    <div class="p5-control-heading"><h5>Map detail after aggregation</h5>
      <output class="alt-level">${esc(STEP5_LEVELS[selectedLevel].label)}</output></div>
    <input class="p5-slider alt-slider" type="range" min="1" max="5" step="1"
      value="${selectedLevel}" ${presets.ready ? "" : "disabled"}>
    <div class="p5-scale" aria-hidden="true">${Object.values(STEP5_LEVELS).map((value) => `<span>${value.short}</span>`).join("")}</div>
    <p class="hint">This runs canonical Step 5's area algorithm on the aggregated raster: islands, undersized components, boundary smoothing, a second small-component pass, and significant-group preservation.</p>
  </div>
  <div class="alt-main-preview"><h5>Simplified approved groups</h5>${debugImage(stem, "alt_label_map_gen_preview.png")}</div>
  <div class="p5-result-stats alt-stats">
    <div><strong data-alt6-stat="changed">${pct(summary.changed_share)}</strong><span>geographic pixels changed</span></div>
    <div><strong data-alt6-stat="dissolved">${summary.dissolved_components}</strong><span>canonical small regions dissolved</span></div>
    <div><strong data-alt6-stat="smoothing">${summary.smoothing_mm} mm</strong><span>canonical boundary smoothing</span></div>
    <div><strong data-alt6-stat="area-change">${pct(summary.largest_group_gain_or_loss_share)}</strong><span>largest group gain or loss (audit)</span></div>
  </div>
  <details class="production-details"><summary>Source-to-final transition report</summary>
    <p class="hint">${transitions.geographically_reassigned_pixels} pixels (${pct(transitions.geographically_reassigned_share)}) ended in a neighbouring final group during Alt Step 6 simplification. The untouched Step 4 raster remains the audit source.</p>
    <table class="data"><thead><tr><th>Step 4 category</th><th>Intended final group</th><th>Pixels moved elsewhere</th></tr></thead><tbody>
      ${(transitions.source_to_final_groups || []).map((row) => `<tr><td>${esc(row.source_label)}</td><td>${esc(row.intended_group_label)}</td><td>${row.geographically_reassigned_px}</td></tr>`).join("")}
    </tbody></table>${debugImage(stem, "alt_step6_changes.png")}
  </details>`);

  const slider = el.querySelector(".alt-slider");
  const showVariant = (level) => {
    const variant = presets.variants[String(level)];
    if (!variant) return;
    const variantSummary = variant.summary;
    el.querySelector(".alt-level").textContent = STEP5_LEVELS[level].label;
    const image = el.querySelector(".alt-main-preview img");
    if (image) image.src = artifactUrl(stem, variant.preview_artifact);
    const link = el.querySelector(".alt-main-preview .imglink");
    if (link) link.href = artifactUrl(stem, variant.preview_artifact);
    el.querySelector('[data-alt6-stat="changed"]').textContent = pct(variantSummary.changed_share);
    el.querySelector('[data-alt6-stat="dissolved"]').textContent = variantSummary.dissolved_components;
    el.querySelector('[data-alt6-stat="smoothing"]').textContent = `${variantSummary.smoothing_mm} mm`;
    el.querySelector('[data-alt6-stat="area-change"]').textContent = pct(variantSummary.largest_group_gain_or_loss_share);
  };
  slider.oninput = () => { selectedLevel = +slider.value; showVariant(selectedLevel); };
  slider.onchange = async () => {
    const main = $("main");
    const scrollY = main.scrollTop;
    await api(`/api/alt-step6preset/${encodeURIComponent(stem)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: selectedLevel }),
    });
    await loadMaps();
    const current = mapRec();
    // The current Alt Step 6 preview was already switched by showVariant().
    // Rebuilding it here would discard the slider's DOM and can move the page.
    // Only Alt Step 7 was invalidated by choosing a different preset.
    renderSidebar();
    const step7Host = document.querySelector('.alt-host[data-alt-step="7"]');
    if (step7Host && current) await renderAltBranch(7, step7Host, current);
    requestAnimationFrame(() => { main.scrollTop = scrollY; });
  };
  showVariant(selectedLevel);
}

async function renderAltStep6(el, map, done) {
  if (!done) {
    el.insertAdjacentHTML("beforeend", map.alt_steps?.[5]
      ? '<p class="hint">Ready. Run Alt Step 6 manually to create its proposal.</p>'
      : '<p class="line-warning">Run Alt Step 5 manually first.</p>');
    return;
  }
  const [alternate, canonical, reviewData] = await Promise.all([
    artifactJson(map.stem, "alt_aggregation.json"),
    map.steps[6] ? artifactJson(map.stem, "aggregation.json") : Promise.resolve(null),
    api(`/api/alt-aggregation-review/${encodeURIComponent(map.stem)}`),
  ]);
  el.insertAdjacentHTML("beforeend", `<div class="alt-aggregation-grid ${canonical ? "two" : ""}">
    ${canonical ? `<div><h5>Canonical Step 6</h5><p class="hint">${esc(canonical.mode)}; ${canonical.groups.length} groups</p>
      <table class="data"><thead><tr><th>Group</th><th>Members</th><th>Reason</th></tr></thead><tbody>${aggregationRows(canonical)}</tbody></table></div>` : ""}
    <div><h5>Alternate Step 6 proposal</h5><p class="hint">${esc(alternate.mode)}; ${alternate.groups.length} proposed tactile categories; review ${esc(alternate.review_status || "not required")}</p>
      <table class="data"><thead><tr><th>Group</th><th>Members</th><th>Reason</th></tr></thead><tbody>${aggregationRows(alternate)}</tbody></table>
      </div>
  </div>${warnList(alternate.notes)}`);
  if (!alternate.review_required) {
    el.insertAdjacentHTML("beforeend", '<p class="msg ok">No class merge is needed; the identity grouping is automatically usable by Alt Step 7.</p>');
    return;
  }

  const currentGroups = reviewData.effective_groups || alternate.groups;
  const source = alternate.source_classes || [];
  const slots = currentGroups.map((group) => group);
  const assigned = new Map();
  slots.forEach((group, slot) => (group.members || []).forEach((member) => assigned.set(Number(member), slot)));
  const status = reviewData.review?.status || "needs_review";
  const groupEditors = slots.map((group, slot) => `<div class="alt-group-editor" data-group-slot="${slot}">
    <label>Final category ${slot + 1}<input class="alt-group-label" value="${esc(group.label || `Group ${slot + 1}`)}"></label>
    <p class="alt-group-members"></p>
    <label class="alt-group-approval"><input type="checkbox" class="alt-group-approved" ${group.approved ? "checked" : ""}>
      Approve this multi-class merge</label>
    <input class="alt-group-rationale" value="${esc(group.rationale || "human-reviewed grouping")}" aria-label="Grouping rationale">
  </div>`).join("");
  const classRows = source.map((cl) => `<tr><td>${esc(cl.label)}</td><td><select class="alt-group-select" data-class-index="${cl.index}">
    ${slots.map((_, slot) => `<option value="${slot}" ${assigned.get(Number(cl.index)) === slot ? "selected" : ""}>Final category ${slot + 1}</option>`).join("")}
  </select></td></tr>`).join("");
  el.insertAdjacentHTML("beforeend", `<section class="alt-aggregation-review">
    <div class="alt-review-heading"><div><h5>Review the actual final categories</h5>
      <p class="hint">This proposal uses ${alternate.proposed_texture_count ?? currentGroups.length} of at most ${alternate.texture_ceiling ?? 5} textures. Unused texture capacity stays unused. Rename categories if needed and approve each proposed merge.</p></div>
      <span class="badge ${status === "approved" ? "" : "neutral"}">${esc(status)}</span></div>
    <div class="alt-group-editors">${groupEditors}</div>
    <details><summary>Adjust which source classes belong together</summary>
      <table class="data alt-class-assignment"><thead><tr><th>Source category</th><th>Final tactile category</th></tr></thead><tbody>${classRows}</tbody></table>
    </details>
    <button class="btn primary alt-save-aggregation">Save aggregation review</button>
    <p class="hint">Saving with a merge left unapproved records a rejection and keeps Alt Step 7 blocked.</p>
  </section>`);

  const refreshGroups = (changed = false) => {
    const selections = [...el.querySelectorAll(".alt-group-select")];
    slots.forEach((_, slot) => {
      const editor = el.querySelector(`[data-group-slot="${slot}"]`);
      const members = selections.filter((select) => Number(select.value) === slot)
        .map((select) => source.find((cl) => Number(cl.index) === Number(select.dataset.classIndex)))
        .filter(Boolean);
      editor.querySelector(".alt-group-members").textContent = members.length
        ? members.map((member) => member.label).join(" + ") : "Unused category slot";
      const approval = editor.querySelector(".alt-group-approved");
      approval.disabled = members.length <= 1;
      if (members.length <= 1) approval.checked = true;
      else if (changed) approval.checked = false;
      editor.classList.toggle("unused", members.length === 0);
    });
  };
  el.querySelectorAll(".alt-group-select").forEach((select) => {
    select.onchange = () => refreshGroups(true);
  });
  refreshGroups(false);
  el.querySelector(".alt-save-aggregation").onclick = async () => {
    const selections = [...el.querySelectorAll(".alt-group-select")];
    const groups = slots.map((_, slot) => {
      const editor = el.querySelector(`[data-group-slot="${slot}"]`);
      return {
        label: editor.querySelector(".alt-group-label").value,
        members: selections.filter((select) => Number(select.value) === slot)
          .map((select) => Number(select.dataset.classIndex)),
        approved: editor.querySelector(".alt-group-approved").checked,
        rationale: editor.querySelector(".alt-group-rationale").value,
      };
    }).filter((group) => group.members.length);
    try {
      await api(`/api/alt-aggregation-review/${encodeURIComponent(map.stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groups }),
      });
      await loadMaps(); await renderMap();
    } catch (error) { alert(error.message); }
  };
}

async function renderAltStep7(el, map, done) {
  if (!done) {
    if (!map.alt_steps?.[6]) {
      const button = el.closest(".alt-branch")?.querySelector(".alt-run");
      if (button) button.disabled = true;
      el.insertAdjacentHTML("beforeend", '<p class="hint">Run Alt Step 6 first to create the simplified approved group raster.</p>');
      return;
    }
    el.insertAdjacentHTML("beforeend", '<p class="hint">Ready. Run Alt Step 7 manually. It assigns textures without changing Alt Step 6 geography.</p>');
    return;
  }
  const [symbols, geometry, step6Summary] = await Promise.all([
    artifactJson(map.stem, "alt_symbols.json"),
    artifactJson(map.stem, "alt_step7_generalization.json"),
    artifactJson(map.stem, "alt_step6_summary.json"),
  ]);
  const hasComparison = await artifactExists(map.stem, "step7_comparison.png");
  if (hasComparison) {
    el.insertAdjacentHTML("beforeend", '<div class="p5-compare-labels"><span>Canonical tactile render</span><span>Alternate tactile render</span></div>' + debugImage(map.stem, "step7_comparison.png"));
  } else {
    el.insertAdjacentHTML("beforeend", debugImage(map.stem, "alt_step7_tactile.png"));
  }
  el.insertAdjacentHTML("beforeend", `<div class="p5-result-stats alt-stats">
    <div><strong>${symbols.texture_count ?? 0}</strong><span>textures used (maximum ${symbols.texture_ceiling ?? 5})</span></div>
    <div><strong>${step6Summary.dissolved_components ?? 0}</strong><span>regions simplified in Alt Step 6</span></div>
    <div><strong>${step6Summary.smoothing_mm ?? 0} mm</strong><span>boundary smoothing in Alt Step 6</span></div>
    <div><strong>${geometry.remaining_below_pattern_minimum ?? 0}</strong><span>pattern-size warnings (no Step 7 changes)</span></div>
  </div>
  <details class="production-details"><summary>See exact final regions before textures</summary>
    <p class="hint">This is the exact Alt Step 6 geometry sent unchanged to the tactile renderer.</p>
    ${debugImage(map.stem, "alt_step7_regions_preview.png")}
  </details>
  <table class="data"><thead><tr><th>Final area</th><th>Pattern</th><th>Required footprint</th><th>Result</th></tr></thead><tbody>
    ${(symbols?.area_assignments || []).map((assignment) => `<tr><td>${esc(assignment.label)}</td><td>${esc(assignment.pattern_desc)}</td><td>at least ${esc(assignment.minimum_width_mm)} mm wide and ${esc(assignment.minimum_area_mm2)} mm²</td><td>${assignment.remaining_subminimum_regions ? `${assignment.remaining_subminimum_regions} retained exception(s)` : "passes or safely merged"}</td></tr>`).join("")}
  </tbody></table>${warnList(symbols?.notes)}`);
}

function debugImage(stem, name) {
  const url = artifactUrl(stem, name);
  return `<a class="imglink" href="${url}" target="_blank">open full size ↗</a>
          <img class="artifact-img" src="${url}" alt="${esc(name)}">`;
}
const warnList = (ws) => ws && ws.length
  ? `<ul class="warnlist">${ws.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";

/* ------------------------------------------------------------------ jobs */

async function runAltSteps(steps) {
  if (!steps.length) return;
  try {
    await api("/api/run-alt", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stem: SELECTED, steps, model: $("model-select").value }) });
  } catch (e) { alert(e.message); return; }
  const m = mapRec();
  if (m) {
    const model = $("model-select").value;
    const names = steps.map((step) => `alt${step}`);
    m.job = { status: "running", current: names[0], steps: names, model };
    m.jobDetail = { status: "running", current: null, steps: names, model, log: [] };
  }
  document.querySelectorAll("#steps .btn").forEach((button) => { button.disabled = true; });
  $("run-all").disabled = true;
  $("model-select").disabled = true;
  renderSidebar();
  renderJobBanner();
  startPollingIfRunning();
}

async function runSteps(steps) {
  if (!steps.length) return;
  try {
    await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stem: SELECTED, steps, model: $("model-select").value }) });
  } catch (e) { alert(e.message); return; }
  // NO full rebuild here (it would jump the scroll position): just freeze the
  // buttons and show the banner; the poll loop rebuilds once the job is done
  const m = mapRec();
  if (m) {
    const model = $("model-select").value;
    m.job = { status: "running", current: steps[0], steps, model };
    m.jobDetail = { status: "running", current: null, steps, model, log: [] };
  }
  document.querySelectorAll("#steps .btn").forEach((b) => { b.disabled = true; });
  $("run-all").disabled = true;
  $("model-select").disabled = true;
  renderSidebar();
  renderJobBanner();
  startPollingIfRunning();
}

function renderJobBanner() {
  const m = mapRec();
  const banner = $("job-banner");
  if (!m || !m.jobDetail || m.jobDetail.status === "idle") { banner.hidden = true; return; }
  const j = m.jobDetail;
  banner.hidden = false;
  banner.querySelector(".dot").className =
    "dot " + (j.status === "running" ? "running" : j.status === "done" ? "done" : "failed");
  $("job-text").textContent =
    j.status === "running" ? `running step ${j.current ?? "…"} of [${j.steps.join(", ")}] with ${j.model}`
    : j.status === "done" ? "job finished" : `job failed: ${j.error ?? ""}`;
  const log = $("job-log");
  log.textContent = (j.log || []).join("\n");
  log.scrollTop = log.scrollHeight;
}

function startPollingIfRunning() {
  clearInterval(POLL);
  POLL = setInterval(async () => {
    if (!SELECTED) return;
    const j = await api(`/api/job/${encodeURIComponent(SELECTED)}`);
    const m = mapRec();
    if (m) m.jobDetail = j;
    renderJobBanner();
    if (j.status !== "running") {
      clearInterval(POLL);
      await loadMaps();
      await renderMap();
      const m2 = mapRec();
      if (m2) m2.jobDetail = j;
      renderJobBanner();
    }
  }, 1500);
}

/* ------------------------------------------------------------------ spec + upload */

$("spec-btn").onclick = async () => {
  $("map-panel").hidden = true;
  $("empty").hidden = true;
  $("spec-panel").hidden = false;
  const data = await api("/api/spec");
  $("spec-path").textContent = data.path;
  $("spec-text").value = data.spec;
  $("spec-msg").textContent = "";
};
$("spec-close").onclick = () => {
  $("spec-panel").hidden = true;
  (SELECTED ? $("map-panel") : $("empty")).hidden = false;
};
$("spec-save").onclick = async () => {
  const msg = $("spec-msg");
  try {
    JSON.parse($("spec-text").value); // fail fast client-side
    await api("/api/spec", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec: $("spec-text").value }) });
    msg.textContent = "saved ✔"; msg.className = "msg ok";
  } catch (e) { msg.textContent = e.message; msg.className = "msg err"; }
};

$("upload-input").onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    await loadMaps();
    selectMap(r.name.replace(/\.[^.]+$/, ""));
  } catch (e) { alert("upload failed: " + e.message); }
  ev.target.value = "";
};

$("delete-project").onclick = async () => {
  const m = mapRec();
  if (!m) return;
  if (!confirm(`Delete "${m.name}" and all of its processed files?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/api/maps/${encodeURIComponent(m.stem)}`, { method: "DELETE" });
    SELECTED = null;
    $("map-panel").hidden = true;
    $("empty").hidden = false;
    await loadMaps();
    if (MAPS.length) await selectMap(MAPS[0].stem);
  } catch (e) { alert("delete failed: " + e.message); }
};

/* ------------------------------------------------------------------ boot */

loadModels().then(loadMaps).then(() => {
  if (MAPS.length) selectMap(MAPS[0].stem);
});
