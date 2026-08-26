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
  { n: 5, title: "Step 5 — Class aggregation",
    desc: "Build and review a complete category aggregation without moving or erasing geography" },
  { n: 6, title: "Step 6 — Simplify for touch",
    desc: "Simplify the approved aggregated raster using physical touch presets" },
  { n: 7, title: "Step 7 — Tactile symbols & master render",
    desc: "Patterns assigned, boundaries added, and component cleanup applied to the final tactile master" },
  { n: 8, title: "Step 8 — Editable Braille labels",
    desc: "Add, edit, show, hide, and manually position 24 pt Braille labels on the tactile map" },
  { n: 9, title: "Step 9 — Editable Braille legend",
    desc: "Create a separate A4 legend page with tactile samples and editable Braille labels" },
];

const KIND_COLORS = { capital: "#d62728", city: "#e28a1b", river_label: "#1f77d0",
  region_label: "#1e9e5a", line_label: "#7a3fbf", other: "#8a8f98" };

let MAPS = [];
let SELECTED = null;
let POLL = null;
const OPEN_STEPS = new Set([1, 2, 3, 4, 5]);
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

async function step6PresetData(stem) {
  try {
    return { ...(await api(`/api/step6presets/${encodeURIComponent(stem)}`)), supported: true };
  } catch (_) {
    // A stale backend can serve the new static JS while not yet knowing the
    // preset route. Keep the canonical Step 6 result usable until restart.
    return { ready: false, active_level: null, variants: {}, supported: false };
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
    const row = document.createElement("div");
    row.className = "map-item" + (m.stem === SELECTED ? " sel" : "");
    row.draggable = !(m.job && m.job.status === "running");
    row.dataset.mapStem = m.stem;
    const running = m.job && m.job.status === "running";
    const pips = STEPS.map((s) => {
      const cls = running && m.job.current === s.n ? "running" : m.steps[s.n] ? "done" : "";
      return `<span class="pip ${cls}" title="step ${s.n}"></span>`;
    }).join("");
    row.innerHTML = `<span class="project-drag" title="Drag to reorder" aria-hidden="true">&#8942;&#8942;</span>
      <button class="project-open" type="button"><img src="/api/mapimg/${encodeURIComponent(m.name)}" alt="">
        <span class="nm">${esc(m.name)}<span class="pips">${pips}</span></span></button>
      <span class="project-actions">
        <button class="project-icon project-rename" type="button" title="Rename ${esc(m.name)}" aria-label="Rename ${esc(m.name)}">&#9998;</button>
        <button class="project-icon project-delete" type="button" title="Delete ${esc(m.name)}" aria-label="Delete ${esc(m.name)}">&times;</button>
      </span>`;
    row.querySelector(".project-open").onclick = () => selectMap(m.stem);
    row.querySelector(".project-rename").onclick = () => renameProject(m);
    row.querySelector(".project-delete").onclick = () => deleteProject(m);
    row.ondragstart = (event) => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", m.stem);
      row.classList.add("dragging");
    };
    row.ondragend = () => row.classList.remove("dragging");
    row.ondragover = (event) => {
      event.preventDefault(); const rect = row.getBoundingClientRect();
      row.classList.toggle("drag-after", event.clientY > rect.top + rect.height / 2);
      row.classList.add("drag-over");
    };
    row.ondragleave = () => row.classList.remove("drag-over", "drag-after");
    row.ondrop = async (event) => {
      event.preventDefault();
      const after = row.classList.contains("drag-after");
      row.classList.remove("drag-over", "drag-after");
      const source = event.dataTransfer.getData("text/plain");
      if (!source || source === m.stem) return;
      const order = MAPS.map((project) => project.stem);
      const moving = order.splice(order.indexOf(source), 1)[0];
      let target = order.indexOf(m.stem); if (after) target += 1;
      order.splice(target, 0, moving);
      await api("/api/maps/order", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stems: order }) });
      await loadMaps();
    };
    list.appendChild(row);
  }
}

async function renameProject(project) {
  const current = project.name.replace(/\.[^.]+$/, "");
  const name = prompt("Project name", current);
  if (name === null || !name.trim() || name.trim() === current) return;
  try {
    const result = await api(`/api/maps/${encodeURIComponent(project.stem)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (SELECTED === project.stem) SELECTED = result.stem;
    await loadMaps();
    if (SELECTED === result.stem) await renderMap();
  } catch (error) { alert("rename failed: " + error.message); }
}

async function deleteProject(project) {
  if (!confirm(`Delete "${project.name}" and all of its processed files?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/api/maps/${encodeURIComponent(project.stem)}`, { method: "DELETE" });
    if (SELECTED === project.stem) {
      SELECTED = null; $("map-panel").hidden = true; $("empty").hidden = false;
    }
    await loadMaps();
    if (!SELECTED && MAPS.length) await selectMap(MAPS[0].stem);
  } catch (error) { alert("delete failed: " + error.message); }
}

async function selectMap(stem) {
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
  // A Step 6 regeneration finishes with a complete page rebuild. Keep this
  // purely display-level choice so an already closed river review stays closed.
  rememberRiverReviewDisplay();
  const scrollY = $("main").scrollTop; // full rebuilds must not jump the page
  PENDING_VIEWS = [];
  $("map-title").textContent = m.name;
  const stepsEl = $("steps");
  stepsEl.innerHTML = `<section class="original-map-preview" aria-labelledby="original-map-title">
    <div class="step7-pane-heading"><h3 id="original-map-title">Original image</h3>
      <span class="badge neutral">pipeline input</span></div>
    <img src="/api/mapimg/${encodeURIComponent(m.name)}" alt="Original uploaded map: ${esc(m.name)}">
  </section>`;
  for (const s of STEPS) {
    stepsEl.appendChild(await stepCard(m, s));
  }
  // restore twice: once now, once after async artifact views finish loading
  requestAnimationFrame(() => { $("main").scrollTop = scrollY; });
  Promise.allSettled(PENDING_VIEWS).then(() =>
    requestAnimationFrame(() => { $("main").scrollTop = scrollY; }));
  renderMapActions(m);
}

function renderMapActions(m) {
  let remaining = STEPS.filter((s) => !m.steps[s.n]).map((s) => s.n);
  if (m.in_scope === false) remaining = [];
  else if (m.pipeline_error) remaining = [];
  else if (m.step1_error) remaining = remaining.filter((step) => step === 1);
  else if (remaining.includes(5)) remaining = remaining.filter((step) => step <= 5);
  else if (!m.step5_review_ready) remaining = remaining.filter((step) => step < 6);
  const runAll = $("run-all");
  runAll.textContent = m.in_scope === false
    ? "Map out of scope"
    : m.pipeline_error ? "Pipeline blocked: no legend"
    : m.step1_error ? "Retry Step 1"
    : remaining.length ? `Run remaining (${remaining.join(", ")})` : "All steps done";
  runAll.disabled = !remaining.length || (m.job && m.job.status === "running");
  $("delete-project").disabled = Boolean(m.job && m.job.status === "running");
  $("model-select").disabled = Boolean(m.job && m.job.status === "running");
  runAll.onclick = () => runSteps(remaining);
  renderJobBanner();
}

async function refreshCanonicalDownstreamCards(map, cardNumbers = [7, 8]) {
  // Switching among Step 6's already-generated presets only invalidates the
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
  const failed = (job && job.status === "failed" && job.current === null &&
                  job.steps.includes(s.n) && !done) ||
                 (s.n === 1 && Boolean(m.step1_error));

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
  const runLabel = s.n === 6
    ? (done ? "Regenerate instant previews" : "Generate instant previews")
    : `${done ? "Re-run" : "Run"} step ${s.n}`;
  const resetLabel = s.n === 6 ? "Clear this and later results" : "Reset from here";
  const reviewBlocked = [6, 7, 8, 9].includes(s.n) && m.steps[5] && !m.step5_review_ready;
  const scopeBlocked = s.n !== 1 && m.in_scope === false;
  const step1Blocked = s.n !== 1 && Boolean(m.step1_error);
  const pipelineBlocked = s.n !== 1 && Boolean(m.pipeline_error);
  actions.innerHTML = `
    <button class="btn primary" ${busy || reviewBlocked || scopeBlocked || step1Blocked || pipelineBlocked ? "disabled" : ""}>${runLabel}</button>
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
  if (s.n === 1 && m.step1_error) body.insertAdjacentHTML("beforeend",
    `<p class="msg err">${esc(m.step1_error)}</p>`);
  if (s.n === 1 && m.pipeline_error) body.insertAdjacentHTML("beforeend",
    `<p class="msg err">${esc(m.pipeline_error)}</p>`);
  if (s.n === 1 && done && m.in_scope === false) body.insertAdjacentHTML("beforeend",
    '<p class="msg ok">Step 1 is complete. No rerun is required: this map is classified as out of scope, and all later steps are disabled.</p>');
  if (step1Blocked) body.insertAdjacentHTML("beforeend",
    '<p class="line-warning">Step 1 must succeed before this step can run.</p>');
  if (pipelineBlocked) body.insertAdjacentHTML("beforeend",
    '<p class="line-warning">The tactile-map pipeline is blocked because the source map has no legend.</p>');
  if (scopeBlocked) body.insertAdjacentHTML("beforeend",
    '<p class="line-warning">Step 1 classified this map as out of scope. Only chorochromatic and isopleth maps can continue.</p>');
  if (reviewBlocked) body.insertAdjacentHTML("beforeend",
    '<p class="line-warning">Approve the Step 5 class aggregation before running Steps 6–8.</p>');

  const inputView = document.createElement("div");
  body.appendChild(inputView);
  await renderStepInput(s.n, inputView, m);

  if (s.n === 6 && m.steps[5] && m.step5_review_ready) {
    const paramsBox = document.createElement("div");
    paramsBox.className = "p5-params-host";
    body.appendChild(paramsBox);
    renderStep6Params(paramsBox, m.stem).catch(() => {});
  }

  if (done) {
    const view = document.createElement("div");
    body.appendChild(view);
    PENDING_VIEWS.push(renderArtifacts(s.n, view).catch((err) => {
      view.innerHTML = `<p class="msg err">failed to load artifacts: ${esc(err.message)}</p>`;
    }));
  } else if (!running && !(s.n === 1 && m.step1_error)) {
    body.insertAdjacentHTML("beforeend",
      `<p class="hint">Not run yet. Running a later step runs missing earlier steps automatically.</p>`);
  }
  return card;
}

/* --------------------------------------------------- step 6 controls */

const STEP5_LEVELS = {
  1: { label: "Most detail", short: "More detail" },
  2: { label: "Detailed", short: "Detailed" },
  3: { label: "Balanced", short: "Balanced" },
  4: { label: "Simple", short: "Simple" },
  5: { label: "Simplest", short: "Simplest" },
};

const LINE_KIND_LABELS = {
  river: "Overlaying lines", road: "Roads", border: "Borders",
  border_or_coast: "Borders or coastlines", coastline: "Coastlines",
  graticule: "Graticules", frame: "Frames", line: "Other lines",
};

const LINE_KIND_COLORS = {
  river: "#2269cc", road: "#f59120", border: "#373737",
  border_or_coast: "#373737", coastline: "#30705c",
  graticule: "#9a9a9a", frame: "#878787", line: "#7e57a8",
};

async function renderStep6Params(el, stem) {
  const [data, presetData] = await Promise.all([
    api(`/api/step6params/${encodeURIComponent(stem)}`),
    step6PresetData(stem),
  ]);
  const p = data.params;
  const KINDS = ["river", "road", "border", "border_or_coast", "coastline", "graticule", "line"];
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
      await api(`/api/step6params/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      runSteps([6]);
    } catch (err) {
      button.disabled = false;
      alert(err.message);
    }
  };
  slider.oninput = () => {
    selectedLevel = +slider.value;
    showLevel();
    const body = el.closest(".step-body");
    body._step6PreviewLevel = selectedLevel;
    if (body._showStep6Preset) body._showStep6Preset(selectedLevel);
  };
  slider.onchange = async () => {
    const main = $("main");
    const scrollY = main.scrollTop;
    try {
      await api(`/api/step6preset/${encodeURIComponent(stem)}`, {
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
  stepBody._step6PreviewLevel = selectedLevel;
  if (stepBody._showStep6Preset) stepBody._showStep6Preset(selectedLevel);
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
        ? "The exact Step 4 class-index raster used to build a reviewed aggregation proposal. Step 5 changes category identities only; no geographic pixel is moved or erased."
        : "The Step 4 class-index raster used to build a reviewed aggregation proposal.";
      files = ["label_map.png", "classes_final.json", "lines.geojson", "config/output_spec.json"];
      content = debugImage(stem, hasCleanPreview ? "label_map_preview.png" : "step4_debug.png");
    }
  } else if (step === 6) {
    if (!map.steps[5]) {
      content = '<p class="hint">Available after Step 5 creates the category aggregation proposal.</p>';
    } else if (!map.step5_review_ready) {
      content = '<p class="hint">Approve the Step 5 category merges before simplifying their combined geography.</p>';
    } else {
      description = "The approved aggregated Step 4 raster is simplified with the same area algorithm and physical presets previously used by canonical Step 5.";
      files = ["group_map_source.png", "groups.json", "aggregation.json",
        "lines.geojson", "config/output_spec.json"];
      content = debugImage(stem, "step5_aggregation_preview.png");
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
        ? `This generalized class raster is combined with the aggregation plan and generalized lines to assign tactile patterns, add selected boundaries, and create the cleaned final tactile master. Final text coordinates use ${hasApprovedLabels ? "the reviewed label selections" : "the raw detections because no review has been saved yet"}.`
        : "Legacy result: the right half renders the generalized class raster. Re-run Step 6 to create the standalone input preview.";
      files = ["label_map_gen.png", "classes_gen.json", "lines_gen.geojson",
        "step6_summary.json", "aggregation.json", hasApprovedLabels ? "approved_labels.json" : "labels.json",
        "step1_semantics.json", "config/output_spec.json"];
      content = debugImage(stem, hasCleanPreview ? "label_map_gen_preview.png" : "step6_debug.png");
    }
  } else if (step === 8) {
    if (!map.steps[7]) {
      content = '<p class="hint">Available after Step 7 creates the cleaned tactile master and exports the original text coordinates.</p>';
    } else {
      description = "The cleaned Step 7 tactile master and the reviewed original text coordinates are combined locally. No AI or external API is used.";
      files = ["step8a_cleanup.png", "overlay_labels.json", "assets/fonts/Braille SW 2024 INSEI.ttf",
        "config/output_spec.json"];
      content = debugImage(stem, "step8a_cleanup.png");
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
          <span><strong>Include overlaying lines in later steps</strong>
            <small>Review lines drawn over thematic areas, including rivers, roads, or railways. Coastlines remain independent.</small></span>
        </label>
        <button class="btn primary river-review-save">Save overlaying-line settings</button>
      </div>
      <div class="river-review-content" ${includeRivers ? "" : "hidden"}>
       <div class="river-review-heading">
        <div><h4>Review extracted overlaying lines</h4>
          <p class="hint">Coastlines are locked. Uncheck incorrect segments, join related fragments, or draw a replacement over the map.</p></div>
      </div>
      <div class="review-summary">
        <span>${noRiverPaths
          ? '<span class="tick">Detection completed: 0 automatic overlaying lines.</span>'
          : review.saved
          ? '<span class="tick">Saved line review is supplying <code>lines.geojson</code>.</span>'
          : '<span class="cross">Automatic paths currently supply <code>lines.geojson</code>.</span>'}</span>
        <span class="river-review-message" aria-live="polite"></span>
      </div>
      ${noAutomaticRivers ? `<div class="detection-empty-state" role="status">
        <strong>${noRiverPaths ? "No overlaying lines detected." : "No automatic overlaying lines detected."}</strong>
        <span>${noRiverPaths
          ? "Step 4 completed and found no automatic overlaying lines. Nothing will be included unless you draw a path below."
          : `${review.manual_rivers.length} manually reviewed overlaying line${review.manual_rivers.length === 1 ? " remains" : "s remain"}; no automatic paths were found in this run.`}</span>
      </div>` : ""}
      <div class="river-editor-layout">
        <div class="river-canvas-wrap">
          <svg class="river-editor" viewBox="0 0 ${review.width} ${review.height}"
               aria-label="Editable overlaying lines over the map">
            <image href="${artifactUrl(stem, "map_area.png")}" x="0" y="0"
                   width="${review.width}" height="${review.height}" />
            <g class="river-fixed-layer">${fixedPaths}</g>
            <g class="river-auto-layer"></g>
            <g class="river-manual-layer"></g>
            <polyline class="river-draft" points="" />
          </svg>
          <p class="hint">In drawing mode, click points along the overlaying line and then finish the path.</p>
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
            ${autoRows || '<tr><td colspan="5">No automatic overlaying-line segments.</td></tr>'}
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
    message.textContent = "Unsaved overlaying-line changes";
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
                 value="${esc(river.label ?? "")}" placeholder="Line name or type (optional)">
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
      message.textContent = `${snapped} endpoint${snapped === 1 ? "" : "s"} snapped to a kept automatic line. Save to confirm.`;
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
        alert("Reviewed overlaying lines were saved. Step 5 and later results were cleared because they used the previous lines.");
      }
      await renderMap();
    } catch (error) {
      button.disabled = false; button.textContent = "Save overlaying-line settings";
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
    const inScope = ["area_class_chorochromatic", "isopleth",
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
    let maskReview = null;
    try { maskReview = await api(`/api/maskreview/${encodeURIComponent(stem)}`); } catch (_) { /* stale server */ }
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
      </section>
      ${maskReview ? `<section class="substage mask-review" aria-labelledby="mask-review-title">
        <div class="mask-review-heading"><div><h4 id="mask-review-title">2.4 — Remove non-map marks</h4>
          <p class="hint">Work directly on the map image. Greyed areas will be excluded from text detection and segmentation. Remove scale bars, frames, north arrows, and other non-map marks; Restore map area can also recover geography omitted by the automatic mask.</p></div>
          <span class="badge ${maskReview.reviewed ? "" : "neutral"}">${maskReview.reviewed ? "reviewed" : "automatic mask"}</span></div>
        <div class="mask-review-toolbar">
          <div class="mask-review-mode" role="group" aria-label="Mask review mode">
            <button type="button" class="btn primary" data-mask-mode="erase">Remove from map</button>
            <button type="button" class="btn ghost" data-mask-mode="restore">Restore map area</button>
          </div>
          <label>Brush size <input class="mask-review-radius" type="range" min="2" max="40" value="12"> <output>12 px</output></label>
          <button type="button" class="btn ghost mask-review-undo" disabled>Undo stroke</button>
          <button type="button" class="btn ghost mask-review-reset">Discard edits</button>
          <button type="button" class="btn primary mask-review-save" disabled>Apply removal to later steps</button>
        </div>
        <div class="mask-review-canvas-wrap"><canvas class="mask-review-canvas" aria-label="Editable geographic mask over map"></canvas></div>
        <p class="mask-review-status" aria-live="polite">Normal-colour pixels are kept; greyed pixels are removed. Your edits appear immediately. Click “Apply removal to later steps” to make the pipeline use them.</p>
      </section>` : ""}`);
    if (maskReview) setupMaskReview(el, stem, maskReview);
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
            <span class="badge neutral">${cleanup?.pixels ?? "?"} pixels excluded · click to preview</span>
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
            <span class="line-preview-title">Overlaying-line ink cleanup</span>
            <span class="badge neutral">${cleanup?.pixels ?? "?"} pixels excluded · click to preview</span>
          </summary>
          <div class="line-preview-content">
            <p class="hint">White pixels are automatic image-supported overlaying-line centerlines plus nearby dark or neutral source ink within ${cleanup?.fringe_radius_px ?? 2} pixels. Reviewed paths, manual drawings, and graph-only bridges are never added to this segmentation mask.</p>
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
            <p class="hint">Only saved centerlines are colored. Coastlines come from the geographic mask. Overlaying lines follow dark pixel ridges selected near relevant labels; short label-covered gaps are joined by a local least-cost image path. Labels guide the search but never supply coordinates. All coordinates and evidence are saved in <code>lines.geojson</code>.</p>
            ${lineExtraction?.unmatched_river_labels?.length ? `<p class="line-warning"><strong>${lineExtraction.unmatched_river_labels.length} overlaying-line label occurrence(s) had no reliable nearby pixel ridge.</strong> They were left unmatched rather than converted into invented linework.</p>` : ""}
            ${lineExtraction ? `<div class="badges">
              <span class="badge neutral">${lineExtraction.boundary_features ?? 0} mask-derived boundaries</span>
              <span class="badge neutral">${lineExtraction.river_label_seeds ?? 0} overlaying-line label seeds</span>
              <span class="badge neutral">${lineExtraction.river_features ?? 0} pixel-derived overlaying lines</span>
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

  if (step === 6) await renderStep6(el, stem);

  if (step === 5) {
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
    let reviewData = null;
    if (agg.review_required && agg.source_classes?.length) {
      reviewData = await api(`/api/aggregation-review/${encodeURIComponent(stem)}`);
    }
    const approvedGroups = reviewData?.review?.approved ? reviewData.effective_groups
      : (!agg.review_required ? agg.groups : []);
    let colorState = { colors: {} };
    try { colorState = await api(`/api/category-colors/${encodeURIComponent(stem)}`); } catch (_) { /* backend restart pending */ }
    const colorSection = document.createElement("section");
    colorSection.className = "hybrid-colors";
    colorSection.innerHTML = `<h5>Hybrid map colours <span class="badge neutral">optional</span></h5>
      <p class="hint">Choose a printed colour under a tactile category. Leave it off to keep the relief map black and white.</p>
      <div class="hybrid-color-grid">${approvedGroups.map((group) => {
        const color = colorState.colors?.[group.label] || "#59F7FF";
        const enabled = Boolean(colorState.colors?.[group.label]);
        return `<label class="hybrid-color-card${enabled ? " color-enabled" : ""}"><strong>${esc(group.label)}</strong>
          <span class="hybrid-use-color"><input type="checkbox" data-color-enable="${esc(group.label)}" ${enabled ? "checked" : ""}> Use color</span>
          <span class="hybrid-color-swatch" style="--category-color:${color}"></span>
          <input type="color" data-category-color="${esc(group.label)}" value="${color}" ${enabled ? "" : "disabled"}>
          <code>${color}</code></label>`;
      }).join("")}</div><p class="hybrid-color-status" aria-live="polite"></p>`;
    const selectedColors = () => {
      const colors = {};
      colorSection.querySelectorAll("[data-color-enable]").forEach((toggle) => {
        if (toggle.checked) colors[toggle.dataset.colorEnable] = colorSection.querySelector(
          `[data-category-color="${CSS.escape(toggle.dataset.colorEnable)}"]`).value.toUpperCase();
      });
      return colors;
    };
    let colorSaveRequested = 0;
    let colorSaveCompleted = 0;
    let colorSaveActive = false;
    const saveColors = async () => {
      colorSaveRequested += 1;
      if (colorSaveActive) return;
      colorSaveActive = true;
      const status = colorSection.querySelector(".hybrid-color-status");
      status.textContent = "Saving colours…";
      let savedSuccessfully = false;
      try {
        while (colorSaveCompleted < colorSaveRequested) {
          const version = colorSaveRequested;
          await api(`/api/category-colors/${encodeURIComponent(stem)}`, { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ colors: selectedColors() }) });
          colorSaveCompleted = version;
        }
        savedSuccessfully = true;
        status.textContent = "Colours saved. Step 7 will show the hybrid preview.";
        const main = $("main");
        const scrollY = main.scrollTop;
        const current = mapRec();
        if (current) await refreshCanonicalDownstreamCards(current, [7, 8, 9]);
        requestAnimationFrame(() => { main.scrollTop = scrollY; });
      } catch (error) {
        colorSaveCompleted = colorSaveRequested;
        status.textContent = `Could not save colours: ${error.message}`;
        status.classList.add("error");
      } finally {
        colorSaveActive = false;
        if (savedSuccessfully) status.classList.remove("error");
        if (colorSaveCompleted < colorSaveRequested) saveColors();
      }
    };
    colorSection.querySelectorAll("[data-color-enable]").forEach((toggle) => {
      toggle.onchange = () => {
        const card = toggle.closest(".hybrid-color-card");
        card.classList.toggle("color-enabled", toggle.checked);
        card.querySelector("[data-category-color]").disabled = !toggle.checked;
        saveColors();
      };
    });
    colorSection.querySelectorAll("[data-category-color]").forEach((input) => {
      input.oninput = () => {
        const card = input.closest(".hybrid-color-card");
        card.querySelector(".hybrid-color-swatch").style.setProperty("--category-color", input.value);
        card.querySelector("code").textContent = input.value.toUpperCase();
      };
      input.onchange = () => {
        colorSection.querySelector(`[data-color-enable="${CSS.escape(input.dataset.categoryColor)}"]`).checked = true;
        saveColors();
      };
    });
    if (agg.review_required) {
      if (!agg.source_classes?.length) {
        el.insertAdjacentHTML("beforeend", '<p class="line-warning">This is a legacy aggregation result. Re-run Step 5 once to create its integrated review.</p>');
      } else {
        await renderAggregationReviewEditor(
          el, stem, agg, reviewData, `/api/aggregation-review/${encodeURIComponent(stem)}`,
          "Review Step 5 final categories", "canonical-aggregation-review", Number(agg.slots));
      }
    } else {
      el.insertAdjacentHTML("beforeend", '<p class="msg ok">No class merge is proposed, so no approval is required.</p>');
    }
    if (approvedGroups.length) el.appendChild(colorSection);
  }

  if (step === 7) {
    const [sym, overlayLabels, cleanup, patternData, pageLayout] = await Promise.all([
      artifactJson(stem, "symbols.json"),
      artifactJson(stem, "overlay_labels.json"),
      artifactJson(stem, "step8a_cleanup.json"),
      api(`/api/pattern-transforms/${encodeURIComponent(stem)}`),
      api(`/api/page-layout/${encodeURIComponent(stem)}`),
    ]);
    const finalUrl = artifactUrl(stem, "step8a_cleanup.png");
    const hybridUrl = artifactUrl(stem, "step8a_hybrid.png");
    const hasHybrid = await artifactExists(stem, "step8a_hybrid.png");
    const pageWidth = Number(pageLayout.canvas_px[0]);
    const pageHeight = Number(pageLayout.canvas_px[1]);
    const mapOrigin = pageLayout.map_origin_px;
    const mapSize = pageLayout.map_size_px;
    const allowedPageOrientations = new Set(pageLayout.allowed_orientations || ["portrait", "landscape"]);
    el.insertAdjacentHTML("beforeend", `
      <p class="hint">Final tactile master: symbol assignment, selected boundaries, and component-layer cleanup are completed together in this step. Use Edit to transform a repeating pattern, or Change to choose another fill and re-optimize the rest of the map.</p>
      <div class="step7-live-layout">
        <div class="step7-pattern-workspace">
          <div class="step7-map-pane">
            <div class="step7-pane-heading"><h5>Tactile map</h5>
              <a class="imglink step7-final-link" href="${finalUrl}" target="_blank">open full size ↗</a></div>
            ${pageViewToolbar("step7-hybrid-toggle", hasHybrid)}
            <label class="page-orientation-control">Paper orientation
              <select class="step7-orientation"><option value="portrait" ${pageLayout.orientation === "portrait" ? "selected" : ""} ${allowedPageOrientations.has("portrait") ? "" : "disabled"}>A4 portrait</option>
                <option value="landscape" ${pageLayout.orientation === "landscape" ? "selected" : ""} ${allowedPageOrientations.has("landscape") ? "" : "disabled"}>A4 landscape</option></select>
            </label>
            <div class="page-viewport" data-page-view data-page-width-mm="${pageLayout.size_mm[0]}"
                 data-page-height-mm="${pageLayout.size_mm[1]}">
              <div class="page-stage" style="aspect-ratio:${pageWidth}/${pageHeight}">
                <div class="step7-page-map" tabindex="0" aria-label="Move tactile map on A4 page"
                     style="left:${mapOrigin[0] / pageWidth * 100}%;top:${mapOrigin[1] / pageHeight * 100}%;width:${mapSize[0] / pageWidth * 100}%;height:${mapSize[1] / pageHeight * 100}%">
                  <img class="step7-live-map" src="${finalUrl}" alt="Final tactile map" draggable="false">
                </div>
                ${mapFurnitureSvg(pageLayout)}
                <div class="page-grid" aria-hidden="true"></div>
              </div>
            </div>
            <p class="step7-page-status braille-save-status">Drag the map to position it on the ${pageLayout.orientation} A4 page. Changing orientation retains real A4 dimensions and updates Step 8.</p>
          </div>
          <div class="step7-side-panels">
          <aside class="pattern-legend" aria-label="Editable tactile pattern legend">
            <div class="step7-pane-heading"><h5>Pattern legend</h5><span class="badge neutral">edit or change</span></div>
            <div class="pattern-legend-list">
              ${patternData.groups.map((group) => `
                <article class="pattern-legend-item" data-pattern-group="${group.group_id}">
                  <img src="/api/pattern-preview/${encodeURIComponent(stem)}/${group.group_id}?t=${Date.now()}"
                    alt="${esc(group.pattern_desc)} swatch">
                  <span class="pattern-legend-copy"><strong>${esc(group.label)}</strong>
                    <small><code>${esc(group.pattern)}</code> — ${esc(group.pattern_desc)}</small></span>
                  <span class="pattern-legend-actions">
                    <button type="button" class="pattern-edit-action" ${group.editable ? "" : "disabled"}
                      aria-label="Edit pattern transform for ${esc(group.label)}">Edit</button>
                    <button type="button" class="pattern-change-action"
                      aria-label="Change pattern for ${esc(group.label)}">Change</button>
                  </span>
                </article>`).join("")}
            </div>
          </aside>
          <aside class="map-furniture-panel" aria-label="Map frame, north marker, and scale controls">
            <div class="step7-pane-heading"><h5>Map furniture</h5><span class="badge neutral">page-level</span></div>
            <label class="map-furniture-control"><input type="checkbox" class="map-border-toggle"
              ${mapFurniture(pageLayout).border.enabled ? "checked" : ""}> <span><strong>Map border</strong><small>3 mm black stroke</small></span></label>
            <button type="button" class="btn ghost map-border-draw">Draw border rectangle</button>
            <label class="map-furniture-control"><input type="checkbox" class="north-marker-toggle"
              ${mapFurniture(pageLayout).north.enabled ? "checked" : ""}> <span><strong>North marker</strong><small>Uses N.svg</small></span></label>
            <div class="map-scale-placeholder"><strong>Scale</strong><small>Geographic calibration will be added later.</small><button type="button" class="btn ghost" disabled>Scale unavailable</button></div>
            <p class="map-furniture-status braille-save-status" aria-live="polite">Draw a frame on the page, then add the north marker if needed.</p>
          </aside>
          </div>
        </div>
        <aside class="pattern-editor-panel" aria-label="Pattern transform editor" hidden></aside>
      </div>`);
    const hybridToggle = el.querySelector(".step7-hybrid-toggle");
    if (hybridToggle) hybridToggle.onchange = () => {
      const show = hybridToggle.checked;
      const image = el.querySelector(".step7-live-map");
      const link = el.querySelector(".step7-final-link");
      image.src = artifactUrl(stem, show ? "step8a_hybrid.png" : "step8a_cleanup.png");
      image.alt = show ? "Hybrid tactile map" : "Final tactile map";
      link.href = show ? hybridUrl : finalUrl;
    };
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
    if (cleanup) el.insertAdjacentHTML("beforeend", `
      <div class="badges">
        <span class="badge">${cleanup.owner_groups?.length ?? 0} boundary-owner group(s)</span>
        <span class="badge neutral">${cleanup.repainted_components ?? 0} top component layer(s)</span>
        <span class="badge neutral">${cleanup.restored_pixels ?? 0} pixels restored</span>
      </div>`);
    setupPatternTransformEditor(el, stem, patternData);
    setupPageViewport(el.querySelector(".step7-map-pane"));
    setupStep7MapPlacement(el, stem, pageLayout);
    setupMapFurnitureEditor(el, stem, pageLayout);
  }

  if (step === 8) {
    const [layout, report] = await Promise.all([
      api(`/api/braille-labels/${encodeURIComponent(stem)}`),
      artifactJson(stem, "step8_braille.json"),
    ]);
    const baseUrl = artifactUrl(stem, "step8_braille_base.png");
    const mapBaseUrl = artifactUrl(stem, "step8a_cleanup.png");
    const hybridMapBaseUrl = artifactUrl(stem, "step8a_hybrid.png");
    const finalUrl = artifactUrl(stem, "step8_braille.png");
    const hybridFinalUrl = artifactUrl(stem, "step8_hybrid.png");
    const hasHybrid = await artifactExists(stem, "step8a_hybrid.png");
    const mapWidth = Number(layout.canvas_px?.[0]) || 1;
    const mapHeight = Number(layout.canvas_px?.[1]) || 1;
    const width = Number(layout.page?.canvas_px?.[0]) || mapWidth;
    const height = Number(layout.page?.canvas_px?.[1]) || mapHeight;
    const mapOrigin = layout.page?.map_origin_px || [0, 0];
    const pxPerMm = Number(layout.render_px_per_mm) || 5;
    const furnitureBounds = mapFurnitureGroupBounds({
      render_px_per_mm: pxPerMm, canvas_px: [width, height], map_origin_px: mapOrigin,
      map_size_px: [mapWidth, mapHeight], furniture: layout.page?.furniture,
    });
    const title = layout.title || { text: "", braille_text: "", enabled: true,
      position_page_px: [width / 2, 0], render_metrics: null };
    el.insertAdjacentHTML("beforeend", `
      <p class="hint">Edit the source wording or use its switch to show and hide a label. Drag the black pin directly on the map to move the pin and its attached Braille box together. Labels remain where you put them and are never rearranged automatically.</p>
      <div class="braille-workspace" style="--braille-font-px:${24 * 25.4 / 72 * pxPerMm}px">
        <section class="braille-map-pane">
          <div class="step7-pane-heading"><h5>Braille-labelled tactile map</h5>
            <a class="imglink braille-final-link" href="${finalUrl}" target="_blank">open full size ↗</a></div>
          ${pageViewToolbar("step8-hybrid-toggle", hasHybrid)}
          <div class="page-viewport" data-page-view data-page-width-mm="${layout.page?.size_mm?.[0]}"
               data-page-height-mm="${layout.page?.size_mm?.[1]}">
          <div class="braille-map-canvas page-stage" style="aspect-ratio:${width} / ${height}">
            <img class="braille-map-page-base" src="${baseUrl}" alt="A4 tactile-map page below editable Braille labels" draggable="false">
            <div class="step8-page-map" data-map-furniture-group
                 style="left:${Number(mapOrigin[0]) / width * 100}%;top:${Number(mapOrigin[1]) / height * 100}%;width:${mapWidth / width * 100}%;height:${mapHeight / height * 100}%">
              <img src="${mapBaseUrl}" alt="Movable tactile-map group" draggable="false">
            </div>
            <svg class="braille-map-overlay" viewBox="0 0 ${width} ${height}"
                 aria-label="Editable Braille label overlay">
              <g class="braille-map-furniture-art" data-page-furniture>${mapFurnitureSvgContents({
                render_px_per_mm: pxPerMm, canvas_px: [width, height], map_origin_px: mapOrigin,
                map_size_px: [mapWidth, mapHeight], furniture: layout.page?.furniture,
              })}</g>
              <g class="braille-map-furniture-group" data-map-furniture-group-hit>
                <rect class="braille-map-furniture-selection" x="${furnitureBounds[0]}" y="${furnitureBounds[1]}"
                    width="${furnitureBounds[2] - furnitureBounds[0]}" height="${furnitureBounds[3] - furnitureBounds[1]}"></rect>
                <rect class="braille-map-furniture-hit" x="${furnitureBounds[0]}" y="${furnitureBounds[1]}"
                    width="${furnitureBounds[2] - furnitureBounds[0]}" height="${furnitureBounds[3] - furnitureBounds[1]}"></rect>
              </g>
              ${layout.labels.map((label) => `
                <g class="braille-map-label${label.enabled ? "" : " disabled"}"
                   data-braille-label="${esc(label.id)}"
                   transform="translate(${Number(mapOrigin[0]) + Number(label.position_px[0])} ${Number(mapOrigin[1]) + Number(label.position_px[1])})">
                  <rect class="braille-label-box"></rect>
                  <text class="braille-map-text"></text>
                  <circle class="braille-pin-halo" cx="0" cy="0"></circle>
                  <circle class="braille-pin" cx="0" cy="0" tabindex="0"
                    aria-label="Move ${esc(label.text)} label pin"></circle>
                </g>`).join("")}
              <g class="braille-map-title${title.enabled && title.braille_text ? "" : " disabled"}"
                 data-braille-title="true"
                 transform="translate(${Number(title.position_page_px?.[0] ?? width / 2)} ${Number(title.position_page_px?.[1] ?? 0)})">
                <rect class="braille-title-box"></rect>
                <text class="braille-map-text braille-title-text"></text>
                <rect class="title-resize-handle" tabindex="0" aria-label="Resize map title box"></rect>
              </g>
            </svg>
            <div class="page-grid" aria-hidden="true"></div>
          </div>
          </div>
          <p class="braille-save-status" aria-live="polite">All changes are saved with this map.</p>
        </section>
        <aside class="braille-label-panel" aria-label="Braille labels">
          <div class="step7-pane-heading"><h5>Braille labels</h5>
            <span class="badge neutral">${layout.labels.length} labels</span></div>
          <section class="braille-title-editor" aria-label="Map title in Braille">
            <strong>Map title</strong>
            <label class="braille-switch" title="Show or hide the title">
              <input type="checkbox" ${title.enabled ? "checked" : ""}>
              <span aria-hidden="true"></span>
            </label>
            <textarea class="braille-title-input" maxlength="200" rows="3"
                   placeholder="Enter a map title" aria-label="Editable map title">${esc(title.text)}</textarea>
            <div class="braille-preview braille-title-preview" aria-label="Braille map title"></div>
            <label class="braille-title-align">Text alignment
              <select aria-label="Map title alignment">
                ${["left", "center", "right"].map((align) =>
                  `<option value="${align}" ${title.align === align ? "selected" : ""}>${align}</option>`).join("")}
              </select>
            </label>
            <small>Drag the title to move it; drag its lower-right handle to resize. Text wraps automatically and Enter creates a new line.</small>
          </section>
          <label class="braille-master-switch">
            <input type="checkbox" ${layout.labels.length && layout.labels.every((label) => label.enabled) ? "checked" : ""}>
            <span aria-hidden="true"></span><strong>Show all labels</strong>
          </label>
          <button type="button" class="btn ghost braille-add-label">+ Add label</button>
          <div class="braille-label-list">
            ${layout.labels.map((label) => `
              <article class="braille-label-row${label.enabled ? "" : " disabled"}"
                       data-braille-row="${esc(label.id)}">
                <label class="braille-switch" title="Show or hide this label">
                  <input type="checkbox" ${label.enabled ? "checked" : ""}>
                  <span aria-hidden="true"></span>
                </label>
                <div class="braille-label-fields">
                  <input class="braille-source-input" value="${esc(label.text)}"
                         maxlength="200" aria-label="Editable text for ${esc(label.original_text)}">
                  <div class="braille-preview" aria-label="Braille for ${esc(label.text)}"></div>
                  <label class="braille-side-choice">Pin position relative to text box
                    <select aria-label="Pin location for ${esc(label.text)}">
                      ${["left", "right", "top", "bottom"].map((side) =>
                        `<option value="${side}" ${label.side === side ? "selected" : ""}>${side} of box</option>`).join("")}
                    </select>
                  </label>
                  <small>${esc(label.kind)} · drag its black pin to move</small>
                </div>
              </article>`).join("")}
          </div>
        </aside>
      </div>
      <div class="badges">
        <span class="badge">${report?.enabled_labels ?? layout.labels.filter((label) => label.enabled).length} labels enabled</span>
        <span class="badge">A4 ${esc(layout.page?.orientation ?? "portrait")} · ${layout.page?.size_mm?.[0] ?? 210} × ${layout.page?.size_mm?.[1] ?? 297} mm</span>
        <span class="badge neutral">map ${layout.page?.map_size_mm?.[0] ?? (mapWidth / pxPerMm).toFixed(1)} × ${layout.page?.map_size_mm?.[1] ?? (mapHeight / pxPerMm).toFixed(1)} mm</span>
        <span class="badge neutral">24 pt · 3 mm box padding</span>
        <span class="badge neutral">6 mm black pin · 2 mm white ring</span>
        <span class="badge neutral">no API calls</span>
      </div>`);
    const step8ColorToggle = el.querySelector(".step8-hybrid-toggle");
    if (step8ColorToggle) step8ColorToggle.onchange = () => {
      const show = step8ColorToggle.checked;
      const image = el.querySelector(".step8-page-map img");
      image.src = artifactUrl(stem, show ? "step8a_hybrid.png" : "step8a_cleanup.png");
      image.alt = show ? "Movable hybrid tactile-map group" : "Movable tactile-map group";
      el.querySelector(".braille-final-link").href = show ? hybridFinalUrl : finalUrl;
    };
    setupBrailleEditor(el, stem, layout);
    setupPageViewport(el.querySelector(".braille-map-pane"));
  }

  if (step === 9) {
    const layout = await api(`/api/legend/${encodeURIComponent(stem)}`);
    const width = Number(layout.page?.canvas_px?.[0]) || 1050;
    const height = Number(layout.page?.canvas_px?.[1]) || 1485;
    const baseUrl = artifactUrl(stem, "step9_legend_base.png");
    const finalUrl = artifactUrl(stem, "step9_legend.png");
    const hybridFinalUrl = artifactUrl(stem, "step9_legend_hybrid.png");
    const hasHybrid = layout.entries.some((entry) => Boolean(entry.color));
    const title = layout.title || {};
    el.insertAdjacentHTML("beforeend", `
      <p class="hint">This is a separate A4 legend page. Select and drag a title or an entire sample-and-text entry. Switching an entry off removes both its sample and its text from the printable output.</p>
      <div class="legend-workspace" style="--braille-font-px:${24 * 25.4 / 72 * (layout.render_px_per_mm || 5)}px">
        <section class="legend-page-pane"><div class="step7-pane-heading"><h5>Braille legend</h5>
          <a class="imglink legend-final-link" href="${finalUrl}" target="_blank">open full size ↗</a></div>
          ${pageViewToolbar("step9-hybrid-toggle", hasHybrid)}
          <label class="page-orientation-control">Paper orientation
            <select class="legend-orientation"><option value="portrait" ${layout.page?.orientation === "portrait" ? "selected" : ""}>A4 portrait</option>
              <option value="landscape" ${layout.page?.orientation === "landscape" ? "selected" : ""}>A4 landscape</option></select>
          </label>
          <div class="page-viewport" data-page-view data-page-width-mm="${layout.page?.size_mm?.[0]}"
               data-page-height-mm="${layout.page?.size_mm?.[1]}">
            <div class="braille-map-canvas legend-page-canvas page-stage" style="aspect-ratio:${width} / ${height}">
              <img class="legend-rendered-page" src="${baseUrl}" alt="Editable A4 tactile legend page" draggable="false">
              <svg class="legend-overlay braille-map-overlay" viewBox="0 0 ${width} ${height}" aria-label="Movable legend objects">
                <g class="legend-object legend-title-object${title.enabled ? "" : " disabled"}" data-legend-object="title">
                  <rect class="legend-object-hit"></rect><text class="legend-map-text"></text>
                  <rect class="title-resize-handle" tabindex="0" aria-label="Resize legend title box"></rect>
                </g>
                ${layout.entries.map((entry) => `<g class="legend-object legend-entry-object${entry.enabled ? "" : " disabled"}"
                    data-legend-object="${esc(entry.id)}">
                  <rect class="legend-object-hit"></rect>
                  <image data-legend-swatch="${esc(entry.id)}" href="/api/legend-swatch/${encodeURIComponent(stem)}/${encodeURIComponent(entry.id)}?t=${Date.now()}"
                    width="${layout.swatch.size_px[0]}" height="${layout.swatch.size_px[1]}"></image>
                  <text class="legend-map-text"></text>
                </g>`).join("")}
              </svg>
              <div class="page-grid" aria-hidden="true"></div>
            </div>
          </div>
          <p class="legend-save-status braille-save-status" aria-live="polite">All changes are saved with this legend.</p>
        </section>
        <aside class="legend-label-panel braille-label-panel"><div class="step7-pane-heading"><h5>Legend text</h5>
          <span class="badge neutral">${layout.entries.length} patterns</span></div>
          <section class="braille-title-editor"><strong>Legend title</strong>
            <label class="braille-switch"><input type="checkbox" ${title.enabled ? "checked" : ""}><span aria-hidden="true"></span></label>
            <textarea class="legend-title-input braille-title-input" maxlength="200" rows="3" aria-label="Editable legend title">${esc(title.text)}</textarea>
            <div class="braille-preview legend-title-preview"></div>
            <label class="braille-title-align">Text alignment<select aria-label="Legend title alignment">
              ${["left", "center", "right"].map((align) => `<option value="${align}" ${title.align === align ? "selected" : ""}>${align}</option>`).join("")}
            </select></label></section>
          <div class="braille-label-list">${layout.entries.map((entry) => `<article class="braille-label-row${entry.enabled ? "" : " disabled"}" data-legend-row="${esc(entry.id)}">
            <label class="braille-switch"><input type="checkbox" ${entry.enabled ? "checked" : ""}><span aria-hidden="true"></span></label>
            <div class="braille-label-fields"><input class="legend-entry-input braille-source-input" value="${esc(entry.text)}" maxlength="200" aria-label="Editable legend text for ${esc(entry.original_text)}">
              <div class="braille-preview"></div><small><code>${esc(entry.pattern)}</code> — ${esc(entry.pattern_desc)}</small></div></article>`).join("")}</div>
        </aside></div>
      <div class="badges"><span class="badge">40 × 20 mm pattern samples</span><span class="badge">24 pt Braille</span><span class="badge neutral">local render · no API calls</span>
        <a class="btn primary legend-download-pdf" href="/api/download/${encodeURIComponent(stem)}">Download Relief Map</a>
        <a class="btn ghost legend-download-pdf" href="/api/download/${encodeURIComponent(stem)}?variant=hybrid">Download Hybrid Map</a></div>`);
    const step9ColorToggle = el.querySelector(".step9-hybrid-toggle");
    if (step9ColorToggle) step9ColorToggle.onchange = () => {
      const show = step9ColorToggle.checked;
      el.querySelectorAll("[data-legend-swatch]").forEach((image) => {
        const target = image.dataset.legendSwatch;
        image.setAttribute("href", `/api/legend-swatch/${encodeURIComponent(stem)}/${encodeURIComponent(target)}?variant=${show ? "hybrid" : "relief"}&t=${Date.now()}`);
      });
      el.querySelector(".legend-final-link").href = show ? hybridFinalUrl : finalUrl;
    };
    setupLegendEditor(el, stem, layout);
    setupPageViewport(el.querySelector(".legend-page-pane"));
  }
}

function mapFurniture(layout) {
  const px = Number(layout.render_px_per_mm) || 5;
  const pageW = Number(layout.canvas_px?.[0]) || 1;
  const pageH = Number(layout.canvas_px?.[1]) || 1;
  const mapOrigin = layout.map_origin_px || [0, 0];
  const mapSize = layout.map_size_px || [0, 0];
  const northSize = 24 * px;
  const fallback = {
    border: { enabled: false, stroke_mm: 3,
      rect_page_px: [mapOrigin[0], mapOrigin[1], mapOrigin[0] + mapSize[0], mapOrigin[1] + mapSize[1]] },
    north: { enabled: false, size_mm: 24,
      position_page_px: [Math.max(0, mapOrigin[0] + mapSize[0] - northSize - 4 * px),
        Math.max(0, mapOrigin[1] + 4 * px)] },
    scale: { enabled: false, status: "placeholder" },
  };
  const source = layout.furniture || {};
  return {
    border: { ...fallback.border, ...(source.border || {}) },
    north: { ...fallback.north, ...(source.north || {}) },
    scale: { ...fallback.scale, ...(source.scale || {}) },
    pageW, pageH, px, mapOrigin, mapSize,
  };
}

function mapFurnitureSvgContents(layout) {
  const furniture = mapFurniture(layout);
  const rect = furniture.border.rect_page_px.map(Number);
  const north = furniture.north.position_page_px.map(Number);
  const northSize = Number(furniture.north.size_mm) * furniture.px;
  const handles = (corner) => {
    const size = 14;
    const x = corner.includes("e") ? rect[2] - size / 2 : rect[0] - size / 2;
    const y = corner.includes("s") ? rect[3] - size / 2 : rect[1] - size / 2;
    return `<rect class="map-furniture-resize-handle" data-border-resize="${corner}"
      x="${x}" y="${y}" width="${size}" height="${size}" />`;
  };
  return `${furniture.border.enabled ? `<rect class="map-furniture-border" data-map-furniture-border x="${rect[0]}" y="${rect[1]}"
      width="${Math.max(1, rect[2] - rect[0])}" height="${Math.max(1, rect[3] - rect[1])}"
      stroke-width="${3 * furniture.px}" />${["nw", "ne", "se", "sw"].map(handles).join("")}` : ""}
    ${furniture.north.enabled ? `<image class="map-furniture-north" data-map-furniture-north href="/api/north-marker.svg"
      x="${north[0]}" y="${north[1]}" width="${northSize}" height="${northSize}" />` : ""}
  `;
}

function mapFurnitureSvg(layout) {
  const furniture = mapFurniture(layout);
  return `<svg class="map-furniture-overlay" data-map-furniture viewBox="0 0 ${furniture.pageW} ${furniture.pageH}"
      aria-label="Map frame and north marker">${mapFurnitureSvgContents(layout)}</svg>`;
}

function mapFurnitureGroupBounds(layout) {
  const furniture = mapFurniture(layout);
  const [mapX, mapY] = furniture.mapOrigin;
  const [mapW, mapH] = furniture.mapSize;
  let left = mapX; let top = mapY; let right = mapX + mapW; let bottom = mapY + mapH;
  if (furniture.border.enabled) {
    const rect = furniture.border.rect_page_px;
    left = Math.min(left, rect[0]); top = Math.min(top, rect[1]);
    right = Math.max(right, rect[2]); bottom = Math.max(bottom, rect[3]);
  }
  if (furniture.north.enabled) {
    const [x, y] = furniture.north.position_page_px;
    const size = Number(furniture.north.size_mm) * furniture.px;
    left = Math.min(left, x); top = Math.min(top, y);
    right = Math.max(right, x + size); bottom = Math.max(bottom, y + size);
  }
  return [left, top, right, bottom];
}

function pageViewToolbar(colorToggleClass = "", colorAvailable = true) {
  const colorToggle = colorToggleClass
    ? `<label class="page-grid-toggle hybrid-display-toggle${colorAvailable ? "" : " unavailable"}" title="${colorAvailable ? "Show or hide the saved category colors" : "Choose and save a category color in Step 5 first"}">
        <input type="checkbox" class="${colorToggleClass}" ${colorAvailable ? "" : "disabled"}><span aria-hidden="true"></span>
        ${colorAvailable ? "Display colors" : "Display colors (none selected)"}
      </label>`
    : "";
  return `<div class="page-view-toolbar" aria-label="Page zoom and guides">
    <button type="button" class="page-zoom-out" aria-label="Zoom out">&minus;</button>
    <input class="page-zoom-range" type="range" min="25" max="200" step="5" value="100" aria-label="Page zoom">
    <button type="button" class="page-zoom-in" aria-label="Zoom in">+</button>
    <button type="button" class="page-zoom-100">100%</button>
    <button type="button" class="page-zoom-fit">Fit</button>
    <span class="page-zoom-readout">Fit</span>
    ${colorToggle}
    <label class="page-grid-toggle page-guides-toggle"><input type="checkbox"><span aria-hidden="true"></span> Grid &amp; guides</label>
    <label class="page-grid-toggle"><input class="page-snap-toggle" type="checkbox"><span aria-hidden="true"></span> Snap to 6 mm grid</label>
  </div>`;
}

function snapToPageGrid(position, root, pxPerMm = 5) {
  if (!root?.querySelector(".page-snap-toggle")?.checked) return position;
  const interval = 6 * pxPerMm;
  return position.map((value) => Math.round(value / interval) * interval);
}

function setupPageViewport(root) {
  if (!root) return;
  const viewport = root.querySelector("[data-page-view]");
  const stage = viewport?.querySelector(".page-stage");
  const toolbar = root.querySelector(".page-view-toolbar");
  if (!viewport || !stage || !toolbar) return;
  const widthMm = Number(viewport.dataset.pageWidthMm) || 210;
  const heightMm = Number(viewport.dataset.pageHeightMm) || 297;
  const cssPxPerMm = 96 / 25.4;
  stage.style.setProperty("--grid-x", `${6 / widthMm * 100}%`);
  stage.style.setProperty("--grid-y", `${6 / heightMm * 100}%`);
  stage.style.setProperty("--guide-x", `${30 / widthMm * 100}%`);
  stage.style.setProperty("--guide-y", `${30 / heightMm * 100}%`);
  const range = toolbar.querySelector(".page-zoom-range");
  const readout = toolbar.querySelector(".page-zoom-readout");
  let zoom = 100;
  const apply = (value, label = null) => {
    zoom = Math.min(200, Math.max(25, Number(value)));
    stage.style.width = `${widthMm * cssPxPerMm * zoom / 100}px`;
    stage.style.height = `${heightMm * cssPxPerMm * zoom / 100}px`;
    range.value = String(Math.round(zoom / 5) * 5);
    readout.textContent = label || `${Math.round(zoom)}%`;
  };
  const fit = () => {
    const availableW = Math.max(220, viewport.clientWidth - 28);
    const availableH = Math.min(560, Math.max(320, window.innerHeight - 250));
    apply(Math.min(100, availableW / (widthMm * cssPxPerMm) * 100,
      availableH / (heightMm * cssPxPerMm) * 100), "Fit");
  };
  toolbar.querySelector(".page-zoom-out").onclick = () => apply(zoom - 10);
  toolbar.querySelector(".page-zoom-in").onclick = () => apply(zoom + 10);
  toolbar.querySelector(".page-zoom-100").onclick = () => apply(100);
  toolbar.querySelector(".page-zoom-fit").onclick = fit;
  range.oninput = () => apply(Number(range.value));
  toolbar.querySelector(".page-guides-toggle input").onchange = (event) =>
    stage.classList.toggle("show-grid", event.target.checked);
  requestAnimationFrame(fit);
}

function setupStep7MapPlacement(stepOutput, stem, layout) {
  const map = stepOutput.querySelector(".step7-page-map");
  const stage = map?.closest(".page-stage");
  const status = stepOutput.querySelector(".step7-page-status");
  if (!map || !stage) return;
  const pageW = Number(layout.canvas_px[0]); const pageH = Number(layout.canvas_px[1]);
  const mapW = Number(layout.map_size_px[0]); const mapH = Number(layout.map_size_px[1]);
  let active = null;
  const place = () => {
    map.style.left = `${layout.map_origin_px[0] / pageW * 100}%`;
    map.style.top = `${layout.map_origin_px[1] / pageH * 100}%`;
  };
  map.onpointerdown = (event) => {
    event.preventDefault(); map.setPointerCapture(event.pointerId); map.classList.add("selected", "dragging");
    stage.querySelectorAll("[data-map-furniture-border], [data-map-furniture-north]").forEach((element) =>
      element.classList.remove("selected"));
    active = { id: event.pointerId, x: event.clientX, y: event.clientY,
      origin: [...layout.map_origin_px] };
  };
  map.onpointermove = (event) => {
    if (!active || active.id !== event.pointerId) return;
    const bounds = stage.getBoundingClientRect();
    layout.map_origin_px = [
      Math.min(pageW - mapW, Math.max(0, active.origin[0] + (event.clientX - active.x) * pageW / bounds.width)),
      Math.min(pageH - mapH, Math.max(0, active.origin[1] + (event.clientY - active.y) * pageH / bounds.height)),
    ];
    layout.map_origin_px = snapToPageGrid(layout.map_origin_px, stepOutput, Number(layout.render_px_per_mm || 5));
    layout.map_origin_px = [
      Math.min(pageW - mapW, Math.max(0, layout.map_origin_px[0])),
      Math.min(pageH - mapH, Math.max(0, layout.map_origin_px[1])),
    ];
    place();
  };
  const finish = async (event) => {
    if (!active || active.id !== event.pointerId) return;
    active = null; map.classList.remove("dragging");
    if (map.hasPointerCapture(event.pointerId)) map.releasePointerCapture(event.pointerId);
    status.textContent = "Saving map placement..."; status.className = "step7-page-status braille-save-status saving";
    try {
      const result = await api(`/api/page-layout/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ map_origin_px: layout.map_origin_px }),
      });
      Object.assign(layout, result.layout); place();
      status.textContent = "Map placement saved for Step 8.";
      status.className = "step7-page-status braille-save-status saved";
    } catch (error) {
      status.textContent = error.message; status.className = "step7-page-status braille-save-status error";
    }
  };
  map.onpointerup = finish; map.onpointercancel = finish;
  const orientation = stepOutput.querySelector(".step7-orientation");
  orientation.onchange = async () => {
    orientation.disabled = true;
    status.textContent = "Changing A4 orientation..."; status.className = "step7-page-status braille-save-status saving";
    try {
      await api(`/api/page-layout/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orientation: orientation.value }),
      });
      await loadMaps(); await renderMap();
    } catch (error) {
      orientation.value = layout.orientation; orientation.disabled = false;
      status.textContent = error.message; status.className = "step7-page-status braille-save-status error";
    }
  };
}

function setupMapFurnitureEditor(stepOutput, stem, layout) {
  const panel = stepOutput.querySelector(".map-furniture-panel");
  const stage = stepOutput.querySelector(".page-stage");
  const status = panel?.querySelector(".map-furniture-status");
  if (!panel || !stage || !status) return;
  const borderToggle = panel.querySelector(".map-border-toggle");
  const northToggle = panel.querySelector(".north-marker-toggle");
  const drawButton = panel.querySelector(".map-border-draw");
  let drawing = null;
  let moving = null;
  let overlay = stage.querySelector("[data-map-furniture]");

  const payload = () => {
    const furniture = mapFurniture(layout);
    return {
      border: { enabled: Boolean(furniture.border.enabled),
        rect_page_px: furniture.border.rect_page_px.map(Number), stroke_mm: 3 },
      north: { enabled: Boolean(furniture.north.enabled),
        position_page_px: furniture.north.position_page_px.map(Number), size_mm: 24,
        asset: "pattern_library/N.svg" },
      scale: { enabled: false, status: "placeholder" },
    };
  };
  const refresh = () => {
    const fresh = document.createRange().createContextualFragment(mapFurnitureSvg(layout));
    overlay.replaceWith(fresh);
    overlay = stage.querySelector("[data-map-furniture]");
    borderToggle.checked = Boolean(mapFurniture(layout).border.enabled);
    northToggle.checked = Boolean(mapFurniture(layout).north.enabled);
    bindDrawing();
  };
  const save = async (message) => {
    status.textContent = "Saving map furniture...";
    status.className = "map-furniture-status braille-save-status saving";
    try {
      const result = await api(`/api/page-layout/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ furniture: payload() }),
      });
      Object.assign(layout, result.layout);
      refresh();
      status.textContent = message;
      status.className = "map-furniture-status braille-save-status saved";
    } catch (error) {
      status.textContent = error.message;
      status.className = "map-furniture-status braille-save-status error";
    }
  };
  const pagePoint = (event) => {
    const bounds = stage.getBoundingClientRect();
    return [
      Math.min(Number(layout.canvas_px[0]), Math.max(0,
        (event.clientX - bounds.left) * Number(layout.canvas_px[0]) / bounds.width)),
      Math.min(Number(layout.canvas_px[1]), Math.max(0,
        (event.clientY - bounds.top) * Number(layout.canvas_px[1]) / bounds.height)),
    ];
  };
  const bindDrawing = () => {
    overlay.onpointerdown = (event) => {
      if (!drawing) return;
      event.preventDefault();
      overlay.setPointerCapture(event.pointerId);
      drawing.pointerId = event.pointerId;
      drawing.start = pagePoint(event);
      drawing.preview = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      drawing.preview.setAttribute("class", "map-furniture-draft-border");
      overlay.appendChild(drawing.preview);
    };
    overlay.onpointermove = (event) => {
      if (!drawing?.start || drawing.pointerId !== event.pointerId) return;
      const [x, y] = pagePoint(event); const [x0, y0] = drawing.start;
      drawing.preview.setAttribute("x", String(Math.min(x0, x)));
      drawing.preview.setAttribute("y", String(Math.min(y0, y)));
      drawing.preview.setAttribute("width", String(Math.abs(x - x0)));
      drawing.preview.setAttribute("height", String(Math.abs(y - y0)));
    };
    const finish = async (event) => {
      if (!drawing?.start || drawing.pointerId !== event.pointerId) return;
      const [x, y] = pagePoint(event); const [x0, y0] = drawing.start;
      if (overlay.hasPointerCapture(event.pointerId)) overlay.releasePointerCapture(event.pointerId);
      const furniture = payload();
      furniture.border.enabled = true;
      furniture.border.rect_page_px = [Math.min(x0, x), Math.min(y0, y),
        Math.max(x0, x), Math.max(y0, y)];
      layout.furniture = furniture;
      drawing = null;
      overlay.classList.remove("drawing");
      await save("Map border saved. It will be rendered with a 3 mm black stroke.");
    };
    overlay.onpointerup = finish;
    overlay.onpointercancel = () => {
      if (!drawing) return;
      drawing = null; overlay.classList.remove("drawing"); refresh();
      status.textContent = "Border drawing cancelled.";
      status.className = "map-furniture-status braille-save-status";
    };

    const setSelected = (kind) => {
      overlay.querySelectorAll("[data-map-furniture-border], [data-map-furniture-north]").forEach((element) =>
        element.classList.toggle("selected", element.dataset.mapFurnitureBorder !== undefined
          ? kind === "border" : kind === "north"));
    };
    const updateFurniturePreview = () => {
      const current = mapFurniture(layout);
      const border = overlay.querySelector("[data-map-furniture-border]");
      const north = overlay.querySelector("[data-map-furniture-north]");
      if (border) {
        const rect = current.border.rect_page_px;
        border.setAttribute("x", String(rect[0])); border.setAttribute("y", String(rect[1]));
        border.setAttribute("width", String(rect[2] - rect[0])); border.setAttribute("height", String(rect[3] - rect[1]));
        const size = 14;
        overlay.querySelectorAll("[data-border-resize]").forEach((handle) => {
          const corner = handle.dataset.borderResize;
          handle.setAttribute("x", String((corner.includes("e") ? rect[2] : rect[0]) - size / 2));
          handle.setAttribute("y", String((corner.includes("s") ? rect[3] : rect[1]) - size / 2));
        });
      }
      if (north) {
        const pos = current.north.position_page_px;
        north.setAttribute("x", String(pos[0])); north.setAttribute("y", String(pos[1]));
      }
    };
    const moveFurniture = (kind, event, resizeCorner = null) => {
      if (drawing) return;
      const target = event.currentTarget;
      event.preventDefault(); event.stopPropagation();
      target.setPointerCapture(event.pointerId);
      moving = {
        pointerId: event.pointerId, kind, resizeCorner, start: pagePoint(event),
        furniture: JSON.parse(JSON.stringify(payload())),
      };
      setSelected(kind); target.classList.add("dragging");
      stage.querySelector(".step7-page-map")?.classList.remove("selected");
    };
    const dragFurniture = (event) => {
      if (!moving || moving.pointerId !== event.pointerId) return;
      const point = pagePoint(event);
      const dx = point[0] - moving.start[0]; const dy = point[1] - moving.start[1];
      const next = JSON.parse(JSON.stringify(moving.furniture));
      const pageW = Number(layout.canvas_px[0]); const pageH = Number(layout.canvas_px[1]);
      if (moving.resizeCorner) {
        const rect = next.border.rect_page_px;
        const snapped = snapToPageGrid(point, stepOutput, Number(layout.render_px_per_mm || 5));
        const x = Math.min(pageW, Math.max(0, snapped[0]));
        const y = Math.min(pageH, Math.max(0, snapped[1]));
        const minSize = 3 * Number(layout.render_px_per_mm || 5);
        let left = moving.resizeCorner.includes("w") ? x : rect[0];
        let right = moving.resizeCorner.includes("e") ? x : rect[2];
        let top = moving.resizeCorner.includes("n") ? y : rect[1];
        let bottom = moving.resizeCorner.includes("s") ? y : rect[3];
        if (right - left < minSize) {
          if (moving.resizeCorner.includes("w")) left = right - minSize;
          else right = left + minSize;
        }
        if (bottom - top < minSize) {
          if (moving.resizeCorner.includes("n")) top = bottom - minSize;
          else bottom = top + minSize;
        }
        next.border.rect_page_px = [Math.max(0, left), Math.max(0, top),
          Math.min(pageW, right), Math.min(pageH, bottom)];
      } else if (moving.kind === "border") {
        const rect = next.border.rect_page_px;
        const rectW = rect[2] - rect[0]; const rectH = rect[3] - rect[1];
        const snapped = snapToPageGrid([rect[0] + dx, rect[1] + dy], stepOutput,
          Number(layout.render_px_per_mm || 5));
        const x = Math.min(pageW - rectW, Math.max(0, snapped[0]));
        const y = Math.min(pageH - rectH, Math.max(0, snapped[1]));
        next.border.rect_page_px = [x, y, x + rectW, y + rectH];
      } else {
        const size = Number(next.north.size_mm || 24) * Number(layout.render_px_per_mm || 5);
        const snapped = snapToPageGrid([next.north.position_page_px[0] + dx,
          next.north.position_page_px[1] + dy], stepOutput, Number(layout.render_px_per_mm || 5));
        next.north.position_page_px = [
          Math.min(pageW - size, Math.max(0, snapped[0])),
          Math.min(pageH - size, Math.max(0, snapped[1])),
        ];
      }
      layout.furniture = next;
      updateFurniturePreview();
    };
    const finishFurnitureMove = async (event) => {
      if (!moving || moving.pointerId !== event.pointerId) return;
      const target = event.currentTarget;
      moving = null; target.classList.remove("dragging");
      if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId);
      await save("Map furniture position saved for Step 8.");
    };
    const border = overlay.querySelector("[data-map-furniture-border]");
    const north = overlay.querySelector("[data-map-furniture-north]");
    for (const [element, kind] of [[border, "border"], [north, "north"]]) {
      if (!element) continue;
      element.onpointerdown = (event) => moveFurniture(kind, event);
      element.onpointermove = dragFurniture;
      element.onpointerup = finishFurnitureMove;
      element.onpointercancel = finishFurnitureMove;
    }
    overlay.querySelectorAll("[data-border-resize]").forEach((handle) => {
      handle.onpointerdown = (event) => moveFurniture("border", event, handle.dataset.borderResize);
      handle.onpointermove = dragFurniture;
      handle.onpointerup = finishFurnitureMove;
      handle.onpointercancel = finishFurnitureMove;
    });
  };
  bindDrawing();
  borderToggle.onchange = () => {
    const furniture = payload(); furniture.border.enabled = borderToggle.checked;
    layout.furniture = furniture;
    save(borderToggle.checked ? "Map border added." : "Map border removed.");
  };
  northToggle.onchange = () => {
    const furniture = payload(); furniture.north.enabled = northToggle.checked;
    layout.furniture = furniture;
    save(northToggle.checked ? "North marker added from N.svg." : "North marker removed.");
  };
  drawButton.onclick = () => {
    drawing = {};
    overlay.classList.add("drawing");
    status.textContent = "Drag on the page to draw the border rectangle.";
    status.className = "map-furniture-status braille-save-status saving";
  };
}

function setupMaskReview(stepOutput, stem, review) {
  const canvas = stepOutput.querySelector(".mask-review-canvas");
  const status = stepOutput.querySelector(".mask-review-status");
  const radiusInput = stepOutput.querySelector(".mask-review-radius");
  const radiusOutput = stepOutput.querySelector(".mask-review-radius + output");
  const saveButton = stepOutput.querySelector(".mask-review-save");
  const undoButton = stepOutput.querySelector(".mask-review-undo");
  const modeButtons = [...stepOutput.querySelectorAll("[data-mask-mode]")];
  if (!canvas) return;
  let mode = "erase";
  let strokes = [];
  let active = null;
  const source = new Image(); const mask = new Image(); const automatic = new Image();
  source.src = mapImageUrl(review.source_name);
  mask.src = artifactUrl(stem, "map_mask_full.png");
  automatic.src = artifactUrl(stem, "map_mask_full_auto.png");
  const load = (image) => new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; });
  Promise.all([load(source), load(mask), load(automatic)]).then(() => {
    canvas.width = review.width; canvas.height = review.height;
    const context = canvas.getContext("2d");
    const overlayCanvas = document.createElement("canvas");
    overlayCanvas.width = review.width; overlayCanvas.height = review.height;
    const overlayContext = overlayCanvas.getContext("2d");
    const maskCanvas = document.createElement("canvas");
    maskCanvas.width = review.width; maskCanvas.height = review.height;
    const maskContext = maskCanvas.getContext("2d", { willReadFrequently: true });
    maskContext.drawImage(mask, 0, 0, review.width, review.height);
    const automaticCanvas = document.createElement("canvas");
    automaticCanvas.width = review.width; automaticCanvas.height = review.height;
    const automaticContext = automaticCanvas.getContext("2d", { willReadFrequently: true });
    automaticContext.drawImage(automatic, 0, 0, review.width, review.height);
    const strokePixels = (stroke) => {
      const draft = document.createElement("canvas"); draft.width = canvas.width; draft.height = canvas.height;
      const draftContext = draft.getContext("2d");
      draftContext.strokeStyle = "#fff"; draftContext.fillStyle = "#fff";
      draftContext.lineWidth = stroke.radius * 2; draftContext.lineCap = "round"; draftContext.lineJoin = "round";
      draftContext.beginPath(); stroke.points.forEach(([x, y], index) => index ? draftContext.lineTo(x, y) : draftContext.moveTo(x, y)); draftContext.stroke();
      if (stroke.points.length === 1) { const [x, y] = stroke.points[0]; draftContext.beginPath(); draftContext.arc(x, y, stroke.radius, 0, Math.PI * 2); draftContext.fill(); }
      return draftContext.getImageData(0, 0, canvas.width, canvas.height).data;
    };
    const paint = () => {
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(source, 0, 0, canvas.width, canvas.height);
      const pixels = maskContext.getImageData(0, 0, canvas.width, canvas.height);
      const automaticPixels = automaticContext.getImageData(0, 0, canvas.width, canvas.height);
      const effective = new Uint8ClampedArray(pixels.data);
      for (const stroke of [...strokes, ...(active ? [active] : [])]) {
        const coverage = strokePixels(stroke);
        for (let offset = 0; offset < effective.length; offset += 4) {
          if (coverage[offset + 3] === 0) continue;
          if (stroke.mode === "erase") effective[offset] = effective[offset + 1] = effective[offset + 2] = 0;
          else {
            effective[offset] = effective[offset + 1] = effective[offset + 2] = 255;
          }
        }
      }
      const overlay = context.createImageData(canvas.width, canvas.height);
      for (let offset = 0; offset < effective.length; offset += 4) {
        if (effective[offset] < 128) {
          overlay.data[offset] = 95; overlay.data[offset + 1] = 100;
          overlay.data[offset + 2] = 110; overlay.data[offset + 3] = 165;
        }
      }
      // Composite the translucent exclusion layer over the map.  Writing the
      // ImageData directly to the review canvas replaces the map with the
      // mask, which makes approval needlessly hard to judge.
      overlayContext.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
      overlayContext.putImageData(overlay, 0, 0);
      context.drawImage(overlayCanvas, 0, 0);
    };
    const point = (event) => {
      const bounds = canvas.getBoundingClientRect();
      return [Math.max(0, Math.min(canvas.width - 1, (event.clientX - bounds.left) * canvas.width / bounds.width)),
        Math.max(0, Math.min(canvas.height - 1, (event.clientY - bounds.top) * canvas.height / bounds.height))];
    };
    const syncControls = () => {
      saveButton.disabled = !strokes.length; undoButton.disabled = !strokes.length;
      modeButtons.forEach((button) => {
        const selected = button.dataset.maskMode === mode;
        button.classList.toggle("primary", selected); button.classList.toggle("ghost", !selected);
      });
    };
    modeButtons.forEach((button) => { button.onclick = () => { mode = button.dataset.maskMode; syncControls(); }; });
    radiusInput.oninput = () => { radiusOutput.value = `${radiusInput.value} px`; };
    canvas.onpointerdown = (event) => {
      event.preventDefault(); canvas.setPointerCapture(event.pointerId);
      active = { mode, radius: Number(radiusInput.value), points: [point(event)] };
      paint();
    };
    canvas.onpointermove = (event) => {
      if (!active || !canvas.hasPointerCapture(event.pointerId)) return;
      const next = point(event); const last = active.points[active.points.length - 1];
      if (Math.hypot(next[0] - last[0], next[1] - last[1]) >= 2) { active.points.push(next); paint(); }
    };
    const finish = (event) => {
      if (!active) return;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      strokes.push(active); active = null; paint(); syncControls();
    };
    canvas.onpointerup = finish; canvas.onpointercancel = finish;
    undoButton.onclick = () => { strokes.pop(); paint(); syncControls(); };
    saveButton.onclick = async () => {
      status.textContent = "Saving mask edits..."; status.className = "mask-review-status saving";
      try {
        await api(`/api/maskreview/${encodeURIComponent(stem)}`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ strokes }),
        });
        await loadMaps(); await renderMap();
      } catch (error) { status.textContent = error.message; status.className = "mask-review-status error"; }
    };
    stepOutput.querySelector(".mask-review-reset").onclick = async () => {
      if (!confirm("Restore the automatic Step 2 geographic mask? This discards saved mask-review edits and invalidates Steps 3–9.")) return;
      status.textContent = "Restoring automatic mask..."; status.className = "mask-review-status saving";
      try {
        await api(`/api/maskreview/${encodeURIComponent(stem)}`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reset: true }),
        });
        await loadMaps(); await renderMap();
      } catch (error) { status.textContent = error.message; status.className = "mask-review-status error"; }
    };
    radiusOutput.value = `${radiusInput.value} px`; paint(); syncControls();
  }).catch(() => { status.textContent = "Could not load the Step 2 mask for review."; status.className = "mask-review-status error"; });
}

function toGrade1FontText(value, preserveNewlines = false) {
  let text = String(value ?? "").replace(/\r\n?/g, "\n");
  text = preserveNewlines
    ? text.split("\n").map((line) => line.trim().replace(/[ \t]+/g, " ")).join("\n").trim()
    : text.replace(/\n+/g, " ").trim().replace(/\s+/g, " ");
  const digitCells = { "1": "a", "2": "b", "3": "c", "4": "d", "5": "e",
    "6": "f", "7": "g", "8": "h", "9": "i", "0": "j" };
  const asciiLetter = (char) => {
    const base = char.normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
    return /^[A-Za-z]$/.test(base[0] || "") ? base[0].toLowerCase() : null;
  };
  let output = "";
  let numberMode = false;
  for (let i = 0; i < text.length;) {
    const char = text[i];
    if (digitCells[char]) {
      if (!numberMode) output += "#";
      output += digitCells[char];
      numberMode = true;
      i += 1;
      continue;
    }
    const letter = /[^\W\d_]/u.test(char) ? asciiLetter(char) : null;
    if (letter) {
      numberMode = false;
      if (char === char.toUpperCase() && char !== char.toLowerCase()) {
        let end = i;
        while (end < text.length && /[^\W\d_]/u.test(text[end]) &&
               text[end] === text[end].toUpperCase()) end += 1;
        if (end - i > 1) {
          output += "``";
          for (const upper of text.slice(i, end)) output += asciiLetter(upper) || "?";
          i = end;
          continue;
        }
        output += "`";
      }
      output += letter;
      i += 1;
      continue;
    }
    if (char === "\n" && preserveNewlines) {
      output += "\n";
      numberMode = false;
    } else if (/\s/.test(char)) {
      output += " ";
      numberMode = false;
    } else {
      output += ({ "–": "-", "—": "-", "−": "-", "’": "'", "‘": "'",
        "“": '"', "”": '"', "…": "...", "°": "?" })[char] ||
        (char.charCodeAt(0) >= 32 && char.charCodeAt(0) <= 126 ? char : "?");
      if (![".", ","].includes(char)) numberMode = false;
    }
    i += 1;
  }
  return output;
}

function setupLegendEditor(stepOutput, stem, layout) {
  const svg = stepOutput.querySelector(".legend-overlay");
  const status = stepOutput.querySelector(".legend-save-status");
  const finalLink = stepOutput.querySelector(".legend-final-link");
  const timers = new Map();
  const width = Number(layout.page.canvas_px[0]); const height = Number(layout.page.canvas_px[1]);
  const objectFor = (key) => svg.querySelector(`[data-legend-object="${CSS.escape(key)}"]`);
  const renderLines = (text, metrics, offset = [0, 0]) => {
    text.replaceChildren();
    (metrics.lines || [""]).forEach((line, index) => {
      const lineOffset = metrics.line_offsets_px?.[index] || [0, 0];
      const tspan = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
      tspan.textContent = line;
      tspan.setAttribute("x", String(offset[0] + lineOffset[0]));
      tspan.setAttribute("y", String(offset[1] + lineOffset[1]));
      text.appendChild(tspan);
    });
  };
  const renderObject = (item, key) => {
    const object = objectFor(key); if (!object || !item.render_metrics) return;
    object.classList.toggle("disabled", !item.enabled);
    object.setAttribute("transform", `translate(${item.position_page_px[0]} ${item.position_page_px[1]})`);
    renderLines(object.querySelector(".legend-map-text"), item.render_metrics,
      key === "title" ? [0, 0] : (item.text_offset_px || [0, 0]));
    const size = key === "title" ? [item.box_width_px, item.render_metrics.height_px]
      : (item.group_size_px || layout.swatch.size_px);
    const hit = object.querySelector(".legend-object-hit");
    hit.setAttribute("x", "0"); hit.setAttribute("y", "0");
    hit.setAttribute("width", String(size[0])); hit.setAttribute("height", String(size[1]));
    const handle = object.querySelector(".title-resize-handle");
    if (handle) {
      handle.setAttribute("x", String(size[0] - 12.5)); handle.setAttribute("y", String(size[1] - 12.5));
      handle.setAttribute("width", "25"); handle.setAttribute("height", "25");
    }
  };
  const save = (key, item, patch) => {
    status.textContent = "Saving change…"; status.className = "legend-save-status braille-save-status saving";
    return api(`/api/legend/${encodeURIComponent(stem)}/${encodeURIComponent(key)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
    }).then((result) => {
      Object.assign(item, result.item); renderObject(item, key);
      finalLink.href = artifactUrl(stem, "step9_legend.png");
      status.textContent = "Applied to the legend and saved.";
      status.className = "legend-save-status braille-save-status saved";
    }).catch((error) => {
      status.textContent = error.message; status.className = "legend-save-status braille-save-status error";
    });
  };
  const wire = (key, item, row, input, preview, toggle) => {
    preview.textContent = item.braille_text; renderObject(item, key);
    input.oninput = () => {
      item.text = input.value; item.braille_text = toGrade1FontText(item.text, key === "title");
      preview.textContent = item.braille_text; clearTimeout(timers.get(key));
      timers.set(key, setTimeout(() => save(key, item, { text: item.text }), 280));
    };
    toggle.onchange = () => {
      item.enabled = toggle.checked; row?.classList.toggle("disabled", !item.enabled);
      renderObject(item, key); save(key, item, { enabled: item.enabled });
    };
  };
  wire("title", layout.title, null, stepOutput.querySelector(".legend-title-input"),
       stepOutput.querySelector(".legend-title-preview"),
       stepOutput.querySelector(".braille-title-editor .braille-switch input"));
  layout.entries.forEach((entry) => {
    const row = stepOutput.querySelector(`[data-legend-row="${CSS.escape(entry.id)}"]`);
    wire(entry.id, entry, row, row.querySelector(".legend-entry-input"),
      row.querySelector(".braille-preview"), row.querySelector('.braille-switch input'));
  });
  const select = (key) => stepOutput.querySelectorAll(".legend-object, [data-legend-row]").forEach((element) =>
    element.classList.toggle("selected", element.dataset.legendObject === key || element.dataset.legendRow === key));
  const pagePoint = (event) => {
    const bounds = svg.getBoundingClientRect();
    return [(event.clientX - bounds.left) * width / bounds.width,
      (event.clientY - bounds.top) * height / bounds.height];
  };
  const allItems = [["title", layout.title], ...layout.entries.map((entry) => [entry.id, entry])];
  for (const [key, item] of allItems) {
    const object = objectFor(key); let drag = null;
    object.onpointerdown = (event) => {
      if (!item.enabled || event.target.classList.contains("title-resize-handle")) return;
      event.preventDefault(); select(key); object.setPointerCapture(event.pointerId);
      drag = { id: event.pointerId, point: pagePoint(event), position: [...item.position_page_px] };
      object.classList.add("dragging");
    };
    object.onpointermove = (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      const now = pagePoint(event);
      item.position_page_px = [Math.min(width, Math.max(0, drag.position[0] + now[0] - drag.point[0])),
        Math.min(height, Math.max(0, drag.position[1] + now[1] - drag.point[1]))];
      item.position_page_px = snapToPageGrid(item.position_page_px, stepOutput,
        Number(layout.render_px_per_mm) || 5);
      renderObject(item, key);
    };
    const finish = (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      drag = null; object.classList.remove("dragging");
      if (object.hasPointerCapture(event.pointerId)) object.releasePointerCapture(event.pointerId);
      save(key, item, { position_page_px: item.position_page_px });
    };
    object.onpointerup = finish; object.onpointercancel = finish; object.onclick = () => select(key);
    stepOutput.querySelector(`[data-legend-row="${CSS.escape(key)}"]`)?.addEventListener("click", () => select(key));
  }
  const align = stepOutput.querySelector(".braille-title-align select");
  align.onchange = () => save("title", layout.title, { align: align.value });
  const titleObject = objectFor("title"); const handle = titleObject.querySelector(".title-resize-handle");
  let resize = null;
  handle.onpointerdown = (event) => {
    event.preventDefault(); event.stopPropagation(); select("title");
    handle.setPointerCapture(event.pointerId); resize = event.pointerId;
  };
  handle.onpointermove = (event) => {
    if (resize !== event.pointerId) return;
    layout.title.box_width_px = Math.max(150, pagePoint(event)[0] - layout.title.position_page_px[0]);
    renderObject(layout.title, "title");
  };
  const finishResize = (event) => {
    if (resize !== event.pointerId) return; resize = null;
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    save("title", layout.title, { box_width_px: layout.title.box_width_px });
  };
  handle.onpointerup = finishResize; handle.onpointercancel = finishResize;
  const orientation = stepOutput.querySelector(".legend-orientation");
  orientation.onchange = async () => {
    orientation.disabled = true;
    status.textContent = "Changing A4 orientation..."; status.className = "legend-save-status braille-save-status saving";
    try {
      await api(`/api/legend-page/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orientation: orientation.value }),
      });
      await loadMaps(); await renderMap();
    } catch (error) {
      orientation.value = layout.page.orientation; orientation.disabled = false;
      status.textContent = error.message; status.className = "legend-save-status braille-save-status error";
    }
  };
}

function setupBrailleEditor(stepOutput, stem, layout) {
  const svg = stepOutput.querySelector(".braille-map-overlay");
  const status = stepOutput.querySelector(".braille-save-status");
  const finalLink = stepOutput.querySelector(".braille-final-link");
  const masterToggle = stepOutput.querySelector(".braille-master-switch input");
  const title = layout.title || { text: "", braille_text: "", enabled: true };
  const titleGroup = stepOutput.querySelector(".braille-map-title");
  const titleInput = stepOutput.querySelector(".braille-title-input");
  const titlePreview = stepOutput.querySelector(".braille-title-preview");
  const titleToggle = stepOutput.querySelector(".braille-title-editor .braille-switch input");
  const titleAlign = stepOutput.querySelector(".braille-title-align select");
  const pxPerMm = Number(layout.render_px_per_mm) || 5;
  const mapWidth = Number(layout.canvas_px?.[0]) || 1;
  const mapHeight = Number(layout.canvas_px?.[1]) || 1;
  const width = Number(layout.page?.canvas_px?.[0]) || mapWidth;
  const height = Number(layout.page?.canvas_px?.[1]) || mapHeight;
  let mapOrigin = [...(layout.page?.map_origin_px || [0, 0])];
  const mapGroup = stepOutput.querySelector("[data-map-furniture-group]");
  const mapFurnitureArt = svg.querySelector("[data-page-furniture]");
  const mapFurnitureHit = svg.querySelector("[data-map-furniture-group-hit]");
  const labels = new Map(layout.labels.map((label) => [String(label.id), label]));
  let saveChain = Promise.resolve();
  const textTimers = new Map();

  const groupFor = (id) => svg.querySelector(`[data-braille-label="${CSS.escape(id)}"]`);
  const rowFor = (id) => stepOutput.querySelector(`[data-braille-row="${CSS.escape(id)}"]`);
  const positionGroup = (label) => {
    groupFor(String(label.id))?.setAttribute("transform",
      `translate(${Number(mapOrigin[0]) + label.position_px[0]} ${Number(mapOrigin[1]) + label.position_px[1]})`);
  };
  const furnitureLayout = () => ({
    render_px_per_mm: pxPerMm, canvas_px: [width, height], map_origin_px: mapOrigin,
    map_size_px: [mapWidth, mapHeight], furniture: layout.page?.furniture,
  });
  const redrawMapFurniture = () => {
    if (mapGroup) {
      mapGroup.style.left = `${mapOrigin[0] / width * 100}%`;
      mapGroup.style.top = `${mapOrigin[1] / height * 100}%`;
    }
    if (mapFurnitureArt) mapFurnitureArt.innerHTML = mapFurnitureSvgContents(furnitureLayout());
    const bounds = mapFurnitureGroupBounds(furnitureLayout());
    mapFurnitureHit?.querySelectorAll(".braille-map-furniture-selection, .braille-map-furniture-hit").forEach((element) => {
      element.setAttribute("x", String(bounds[0])); element.setAttribute("y", String(bounds[1]));
      element.setAttribute("width", String(bounds[2] - bounds[0]));
      element.setAttribute("height", String(bounds[3] - bounds[1]));
    });
    layout.labels.forEach(positionGroup);
  };
  // Move the box in the browser first.  The server then recalculates the
  // authoritative metrics and persists exactly the same placement.
  const setLocalBoxSide = (label, side) => {
    const metrics = label.render_metrics;
    if (!metrics) return;
    const [boxWidth, boxHeight] = metrics.box_size_px;
    const outerRadius = Number(metrics.pin_outer_radius_px);
    let boxOffset;
    if (side === "left") boxOffset = [outerRadius, -boxHeight / 2];
    else if (side === "top") boxOffset = [-boxWidth / 2, outerRadius];
    else if (side === "bottom") boxOffset = [-boxWidth / 2, -outerRadius - boxHeight];
    else boxOffset = [-outerRadius - boxWidth, -boxHeight / 2];
    const previous = metrics.box_offset_px;
    const textOffset = [
      metrics.text_offset_px[0] + boxOffset[0] - previous[0],
      metrics.text_offset_px[1] + boxOffset[1] - previous[1],
    ];
    label.render_metrics = {
      ...metrics, side, box_offset_px: boxOffset, text_offset_px: textOffset,
    };
  };
  const layoutGroup = (label) => {
    const group = groupFor(String(label.id));
    if (!group) return;
    const metrics = label.render_metrics;
    if (!metrics) return;
    const text = group.querySelector(".braille-map-text");
    text.textContent = label.braille_text;
    text.setAttribute("x", String(metrics.text_offset_px[0]));
    text.setAttribute("y", String(metrics.text_offset_px[1] + metrics.text_bbox_px[1]));
    text.setAttribute("dominant-baseline", "hanging");
    const rect = group.querySelector(".braille-label-box");
    rect.setAttribute("x", String(metrics.box_offset_px[0]));
    rect.setAttribute("y", String(metrics.box_offset_px[1]));
    rect.setAttribute("width", String(metrics.box_size_px[0]));
    rect.setAttribute("height", String(metrics.box_size_px[1]));
    group.querySelector(".braille-pin-halo").setAttribute(
      "r", String(metrics.pin_outer_radius_px));
    group.querySelector(".braille-pin").setAttribute(
      "r", String(metrics.pin_black_radius_px));
  };
  const layoutTitle = () => {
    if (!titleGroup || !title.render_metrics) return;
    const metrics = title.render_metrics;
    titleGroup.setAttribute("transform", `translate(${title.position_page_px[0]} ${title.position_page_px[1]})`);
    titleGroup.classList.toggle("disabled", !title.enabled || !title.braille_text);
    const text = titleGroup.querySelector(".braille-title-text");
    text.setAttribute("dominant-baseline", "hanging");
    text.replaceChildren();
    const lines = metrics.lines || [title.braille_text];
    const offsets = metrics.line_offsets_px || [metrics.text_offset_px];
    lines.forEach((line, index) => {
      const tspan = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
      const offset = offsets[index] || metrics.text_offset_px;
      tspan.textContent = line;
      tspan.setAttribute("x", String(offset[0]));
      tspan.setAttribute("y", String(offset[1] + metrics.text_bbox_px[1]));
      text.appendChild(tspan);
    });
    const rect = titleGroup.querySelector(".braille-title-box");
    rect.setAttribute("x", String(metrics.box_offset_px[0]));
    rect.setAttribute("y", String(metrics.box_offset_px[1]));
    rect.setAttribute("width", String(metrics.box_size_px[0]));
    rect.setAttribute("height", String(metrics.box_size_px[1]));
    const handle = titleGroup.querySelector(".title-resize-handle");
    const handleSize = 25;
    handle.setAttribute("x", String(metrics.box_size_px[0] - handleSize / 2));
    handle.setAttribute("y", String(metrics.box_size_px[1] - handleSize / 2));
    handle.setAttribute("width", String(handleSize));
    handle.setAttribute("height", String(handleSize));
  };
  const setSelected = (id) => {
    stepOutput.querySelectorAll(".braille-label-row, .braille-map-label").forEach((element) =>
      element.classList.toggle("selected",
        element.dataset.brailleRow === id || element.dataset.brailleLabel === id));
    titleGroup?.classList.remove("selected");
    mapGroup?.classList.remove("selected");
    mapFurnitureHit?.classList.remove("selected");
  };
  const syncMasterToggle = () => {
    masterToggle.checked = layout.labels.length > 0 && layout.labels.every((label) => label.enabled);
    masterToggle.indeterminate = layout.labels.some((label) => label.enabled) && !masterToggle.checked;
  };
  const setEnabled = (label, enabled) => {
    label.enabled = enabled;
    const row = rowFor(String(label.id));
    row.classList.toggle("disabled", !enabled);
    groupFor(String(label.id)).classList.toggle("disabled", !enabled);
    row.querySelector('.braille-switch input[type="checkbox"]').checked = enabled;
  };
  const save = (label, patch) => {
    status.textContent = "Saving change…";
    status.className = "braille-save-status saving";
    saveChain = saveChain.then(() => api(
      `/api/braille-labels/${encodeURIComponent(stem)}/${encodeURIComponent(label.id)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      })).then((result) => {
        Object.assign(label, result.label);
        layoutGroup(label);
        setEnabled(label, label.enabled);
        syncMasterToggle();
        finalLink.href = artifactUrl(stem, "step8_braille.png");
        status.textContent = "Applied to the map and saved.";
        status.className = "braille-save-status saved";
      }).catch((error) => {
        status.textContent = error.message;
        status.className = "braille-save-status error";
      });
    return saveChain;
  };
  const saveTitle = (patch) => {
    status.textContent = "Saving title...";
    status.className = "braille-save-status saving";
    saveChain = saveChain.then(() => api(`/api/braille-labels/${encodeURIComponent(stem)}/title`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
    })).then((result) => {
      Object.assign(title, result.title);
      titlePreview.textContent = title.braille_text;
      titleToggle.checked = title.enabled;
      titleAlign.value = title.align || "center";
      layoutTitle();
      finalLink.href = artifactUrl(stem, "step8_braille.png");
      status.textContent = "Title applied to the page and saved.";
      status.className = "braille-save-status saved";
    }).catch((error) => {
      status.textContent = error.message;
      status.className = "braille-save-status error";
    });
    return saveChain;
  };

  titlePreview.textContent = title.braille_text;
  layoutTitle();
  let titleTimer = null;
  titleInput.oninput = () => {
    title.text = titleInput.value;
    title.braille_text = toGrade1FontText(title.text, true);
    titlePreview.textContent = title.braille_text;
    if (titleGroup) titleGroup.querySelector(".braille-title-text").textContent = title.braille_text;
    clearTimeout(titleTimer);
    titleTimer = setTimeout(() => saveTitle({ text: title.text }), 280);
  };
  titleToggle.onchange = () => {
    title.enabled = titleToggle.checked;
    layoutTitle();
    saveTitle({ enabled: title.enabled });
  };
  titleAlign.onchange = () => {
    title.align = titleAlign.value;
    layoutTitle();
    saveTitle({ align: title.align });
  };
  const pagePoint = (event) => {
    const bounds = svg.getBoundingClientRect();
    return [(event.clientX - bounds.left) * width / bounds.width,
      (event.clientY - bounds.top) * height / bounds.height];
  };
  let mapFurnitureDragging = null;
  const clearMapFurnitureSelection = () => {
    mapGroup?.classList.remove("selected");
    mapFurnitureHit?.classList.remove("selected");
  };
  const translatedFurniture = (source, deltaX, deltaY) => {
    const next = JSON.parse(JSON.stringify(source || {}));
    if (Array.isArray(next.border?.rect_page_px) && next.border.rect_page_px.length >= 4) {
      next.border.rect_page_px = [next.border.rect_page_px[0] + deltaX,
        next.border.rect_page_px[1] + deltaY, next.border.rect_page_px[2] + deltaX,
        next.border.rect_page_px[3] + deltaY];
    }
    if (Array.isArray(next.north?.position_page_px) && next.north.position_page_px.length >= 2) {
      next.north.position_page_px = [next.north.position_page_px[0] + deltaX,
        next.north.position_page_px[1] + deltaY];
    }
    return next;
  };
  mapFurnitureHit?.addEventListener("pointerdown", (event) => {
    event.preventDefault(); event.stopPropagation();
    mapFurnitureHit.setPointerCapture(event.pointerId);
    mapFurnitureDragging = {
      pointerId: event.pointerId, pointerStart: pagePoint(event), originStart: [...mapOrigin],
      furnitureStart: JSON.parse(JSON.stringify(layout.page?.furniture || {})),
    };
    mapGroup?.classList.add("selected", "dragging");
    mapFurnitureHit.classList.add("selected", "dragging");
    titleGroup?.classList.remove("selected");
    stepOutput.querySelectorAll(".braille-label-row, .braille-map-label").forEach((element) =>
      element.classList.remove("selected"));
  });
  mapFurnitureHit?.addEventListener("pointermove", (event) => {
    if (mapFurnitureDragging?.pointerId !== event.pointerId) return;
    const point = pagePoint(event);
    const next = [
      Math.min(width - mapWidth, Math.max(0,
        mapFurnitureDragging.originStart[0] + point[0] - mapFurnitureDragging.pointerStart[0])),
      Math.min(height - mapHeight, Math.max(0,
        mapFurnitureDragging.originStart[1] + point[1] - mapFurnitureDragging.pointerStart[1])),
    ];
    mapOrigin = snapToPageGrid(next, stepOutput, pxPerMm);
    mapOrigin = [Math.min(width - mapWidth, Math.max(0, mapOrigin[0])),
      Math.min(height - mapHeight, Math.max(0, mapOrigin[1]))];
    layout.page.map_origin_px = mapOrigin;
    layout.page.furniture = translatedFurniture(mapFurnitureDragging.furnitureStart,
      mapOrigin[0] - mapFurnitureDragging.originStart[0],
      mapOrigin[1] - mapFurnitureDragging.originStart[1]);
    redrawMapFurniture();
  });
  const finishMapFurnitureDrag = async (event) => {
    if (mapFurnitureDragging?.pointerId !== event.pointerId) return;
    const furniture = layout.page.furniture;
    mapFurnitureDragging = null;
    mapGroup?.classList.remove("dragging");
    mapFurnitureHit.classList.remove("dragging");
    if (mapFurnitureHit.hasPointerCapture(event.pointerId)) mapFurnitureHit.releasePointerCapture(event.pointerId);
    status.textContent = "Saving map placement...";
    status.className = "braille-save-status saving";
    try {
      const result = await api(`/api/page-layout/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ map_origin_px: mapOrigin, furniture }),
      });
      mapOrigin = [...result.layout.map_origin_px];
      layout.page.map_origin_px = mapOrigin;
      layout.page.furniture = result.layout.furniture;
      redrawMapFurniture();
      finalLink.href = artifactUrl(stem, "step8_braille.png");
      status.textContent = "Map, border, and north marker moved together and saved.";
      status.className = "braille-save-status saved";
    } catch (error) {
      status.textContent = error.message;
      status.className = "braille-save-status error";
    }
  };
  mapFurnitureHit?.addEventListener("pointerup", finishMapFurnitureDrag);
  mapFurnitureHit?.addEventListener("pointercancel", finishMapFurnitureDrag);
  let titleDragging = null;
  titleGroup.onpointerdown = (event) => {
    if (!title.enabled || event.target.classList.contains("title-resize-handle")) return;
    event.preventDefault();
    titleGroup.setPointerCapture(event.pointerId);
    titleDragging = {
      pointerId: event.pointerId,
      pointerStart: pagePoint(event),
      titleStart: [...title.position_page_px],
    };
    titleGroup.classList.add("selected", "dragging");
    clearMapFurnitureSelection();
    stepOutput.querySelectorAll(".braille-label-row, .braille-map-label").forEach((element) =>
      element.classList.remove("selected"));
  };
  titleGroup.onpointermove = (event) => {
    if (titleDragging?.pointerId !== event.pointerId) return;
    const point = pagePoint(event);
    const metrics = title.render_metrics || {};
    const boxSize = metrics.box_size_px || [0, 0];
    const x = titleDragging.titleStart[0] + point[0] - titleDragging.pointerStart[0];
    const y = titleDragging.titleStart[1] + point[1] - titleDragging.pointerStart[1];
    title.position_page_px = [Math.round(Math.min(width - boxSize[0], Math.max(0, x)) * 1000) / 1000,
      Math.round(Math.min(height - boxSize[1], Math.max(0, y)) * 1000) / 1000];
    title.position_page_px = snapToPageGrid(title.position_page_px, stepOutput, pxPerMm);
    layoutTitle();
  };
  const finishTitleDrag = (event) => {
    if (titleDragging?.pointerId !== event.pointerId) return;
    titleDragging = null;
    titleGroup.classList.remove("dragging");
    if (titleGroup.hasPointerCapture(event.pointerId)) titleGroup.releasePointerCapture(event.pointerId);
    saveTitle({ position_page_px: title.position_page_px });
  };
  titleGroup.onpointerup = finishTitleDrag;
  titleGroup.onpointercancel = finishTitleDrag;
  const titleHandle = titleGroup.querySelector(".title-resize-handle");
  let titleResizing = null;
  titleHandle.onpointerdown = (event) => {
    if (!title.enabled) return;
    event.preventDefault(); event.stopPropagation();
    titleHandle.setPointerCapture(event.pointerId); titleResizing = event.pointerId;
    titleGroup.classList.add("selected", "resizing");
  };
  titleHandle.onpointermove = (event) => {
    if (titleResizing !== event.pointerId) return;
    const point = pagePoint(event);
    title.box_width_px = Math.min(width - title.position_page_px[0],
      Math.max(30 * pxPerMm, point[0] - title.position_page_px[0]));
    title.render_metrics.box_size_px[0] = title.box_width_px;
    layoutTitle();
  };
  const finishTitleResize = (event) => {
    if (titleResizing !== event.pointerId) return;
    titleResizing = null; titleGroup.classList.remove("resizing");
    if (titleHandle.hasPointerCapture(event.pointerId)) titleHandle.releasePointerCapture(event.pointerId);
    saveTitle({ box_width_px: title.box_width_px });
  };
  titleHandle.onpointerup = finishTitleResize;
  titleHandle.onpointercancel = finishTitleResize;
  stepOutput.querySelector(".braille-add-label").onclick = async () => {
    // Rendering the new control replaces this card.  Keep both the application
    // scroll position and the page viewport in exactly the same place.
    const main = $("main");
    const pageViewport = stepOutput.querySelector(".page-viewport");
    const view = {
      mainLeft: main?.scrollLeft || 0,
      mainTop: main?.scrollTop || 0,
      pageLeft: pageViewport?.scrollLeft || 0,
      pageTop: pageViewport?.scrollTop || 0,
    };
    const restoreView = () => {
      if (main) {
        main.scrollLeft = view.mainLeft;
        main.scrollTop = view.mainTop;
      }
      const refreshedViewport = $("step-output")?.querySelector(".page-viewport");
      if (refreshedViewport) {
        refreshedViewport.scrollLeft = view.pageLeft;
        refreshedViewport.scrollTop = view.pageTop;
      }
    };
    try {
      status.textContent = "Adding label...";
      status.className = "braille-save-status saving";
      await api(`/api/braille-labels/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: "" }),
      });
      await renderMap();
      // renderMap also restores its own scroll position.  Wait until its
      // asynchronous artifact views have rebuilt Step 8, then restore the
      // nested print-page viewport as well.
      Promise.allSettled(PENDING_VIEWS).then(() =>
        requestAnimationFrame(() => requestAnimationFrame(restoreView)));
    } catch (error) {
      status.textContent = error.message;
      status.className = "braille-save-status error";
    }
  };

  for (const label of layout.labels) {
    const id = String(label.id);
    const row = rowFor(id);
    const group = groupFor(id);
    const input = row.querySelector(".braille-source-input");
    const preview = row.querySelector(".braille-preview");
    const toggle = row.querySelector('.braille-switch input[type="checkbox"]');
    const sideChoice = row.querySelector(".braille-side-choice select");
    const pin = group.querySelector(".braille-pin");
    preview.textContent = label.braille_text;
    layoutGroup(label);
    row.onclick = () => setSelected(id);
    group.onclick = () => setSelected(id);

    input.oninput = () => {
      label.text = input.value;
      label.braille_text = toGrade1FontText(label.text);
      preview.textContent = label.braille_text;
      preview.setAttribute("aria-label", `Braille for ${label.text}`);
      layoutGroup(label);
      clearTimeout(textTimers.get(id));
      textTimers.set(id, setTimeout(() => save(label, { text: label.text }), 280));
    };
    toggle.onchange = () => {
      setEnabled(label, toggle.checked);
      save(label, { enabled: label.enabled });
    };
    sideChoice.onchange = () => {
      label.side = sideChoice.value;
      setLocalBoxSide(label, label.side);
      layoutGroup(label);
      save(label, { side: label.side });
    };

    let dragging = null;
    const eventPoint = (event) => {
      const bounds = svg.getBoundingClientRect();
      return [(event.clientX - bounds.left) * width / bounds.width - Number(mapOrigin[0]),
        (event.clientY - bounds.top) * height / bounds.height - Number(mapOrigin[1])];
    };
    pin.onpointerdown = (event) => {
      event.preventDefault();
      pin.setPointerCapture(event.pointerId);
      dragging = event.pointerId;
      group.classList.add("dragging");
      setSelected(id);
    };
    pin.onpointermove = (event) => {
      if (dragging !== event.pointerId) return;
      const point = eventPoint(event);
      label.position_px = [
        Math.round(Math.min(mapWidth, Math.max(0, point[0])) * 1000) / 1000,
        Math.round(Math.min(mapHeight, Math.max(0, point[1])) * 1000) / 1000,
      ];
      if (stepOutput.querySelector(".page-snap-toggle")?.checked) {
        const snapped = snapToPageGrid([mapOrigin[0] + label.position_px[0],
          mapOrigin[1] + label.position_px[1]], stepOutput, pxPerMm);
        label.position_px = [Math.min(mapWidth, Math.max(0, snapped[0] - mapOrigin[0])),
          Math.min(mapHeight, Math.max(0, snapped[1] - mapOrigin[1]))];
      }
      positionGroup(label);
    };
    const finishDrag = (event) => {
      if (dragging !== event.pointerId) return;
      dragging = null;
      group.classList.remove("dragging");
      if (pin.hasPointerCapture(event.pointerId)) pin.releasePointerCapture(event.pointerId);
      save(label, { position_px: label.position_px });
    };
    pin.onpointerup = finishDrag;
    pin.onpointercancel = finishDrag;
  }
  syncMasterToggle();
  masterToggle.onchange = () => {
    const enabled = masterToggle.checked;
    layout.labels.forEach((label) => {
      if (label.enabled === enabled) return;
      setEnabled(label, enabled);
      save(label, { enabled });
    });
    syncMasterToggle();
  };
}

function patternTransformControl(key, label, min, max, step, unit) {
  return `<label class="pattern-transform-row">
    <span>${label}</span>
    <input type="range" min="${min}" max="${max}" step="${step}" data-transform-key="${key}">
    <span class="pattern-number"><input type="number" min="${min}" max="${max}" step="${step}"
      data-transform-key="${key}"><span>${unit}</span></span>
  </label>`;
}

function setupPatternTransformEditor(stepOutput, stem, patternData) {
  const layout = stepOutput.querySelector(".step7-live-layout");
  const dialog = layout.querySelector(".pattern-editor-panel");
  dialog.innerHTML = `
    <div class="pattern-dialog-head">
      <div><h4>Edit pattern</h4><p class="pattern-dialog-area"></p></div>
      <button type="button" class="pattern-dialog-close" aria-label="Close pattern editor">×</button>
    </div>
    <div class="pattern-dialog-preview"><img alt="Selected tactile pattern preview"></div>
    <div class="pattern-change-picker" hidden>
      <p>Choose a fill for this area. The choice stays fixed while every other area is re-optimized.</p>
      <div class="pattern-library-grid">
        ${(patternData.library || []).length ? patternData.library.map((item) => `
          <button type="button" class="pattern-library-choice" data-pattern-id="${esc(item.pattern)}"
            data-pattern-family="${esc(item.pattern_family)}"
            data-water-only="${item.water_only ? "true" : "false"}">
            <img src="/api/pattern-library-preview/${encodeURIComponent(item.pattern)}"
              alt="${esc(item.pattern_desc)} swatch">
            <span><strong>${esc(item.pattern_desc)}</strong>
              <small><code>${esc(item.pattern)}</code> · ${esc(item.pattern_family)}</small></span>
          </button>`).join("") : `<p class="pattern-library-unavailable">
            The running MapGen server is out of date. Restart it, then reload this page.
          </p>`}
      </div>
    </div>
    <div class="pattern-transform-controls">
      <fieldset><legend>Scale</legend>
        ${patternTransformControl("scale_x_percent", "Horizontal", 10, 500, 1, "%")}
        ${patternTransformControl("scale_y_percent", "Vertical", 10, 500, 1, "%")}
        <label class="pattern-link-scale"><input type="checkbox" checked> Link horizontal and vertical scale</label>
      </fieldset>
      <fieldset><legend>Move</legend>
        ${patternTransformControl("move_x_mm", "Horizontal", -100, 100, .1, "mm")}
        ${patternTransformControl("move_y_mm", "Vertical", -100, 100, .1, "mm")}
      </fieldset>
      <fieldset><legend>Rotate</legend>
        ${patternTransformControl("rotate_deg", "Angle", -360, 360, 1, "°")}
      </fieldset>
    </div>
    <p class="pattern-transform-note">The original Illustrator transform remains intact. These values are layered over it and saved only with this map.</p>
    <p class="pattern-transform-status" aria-live="polite"></p>
    <div class="pattern-dialog-actions">
      <button type="button" class="btn ghost pattern-reset">Reset transform</button>
      <button type="button" class="btn primary pattern-done">Done</button>
    </div>`;
  let activeGroup = null;
  let saveTimer = null;
  let saving = false;
  let queuedTransform = null;
  let dirty = false;
  const status = dialog.querySelector(".pattern-transform-status");
  const preview = dialog.querySelector(".pattern-dialog-preview img");
  const previewBox = dialog.querySelector(".pattern-dialog-preview");
  const picker = dialog.querySelector(".pattern-change-picker");
  const transformControls = dialog.querySelector(".pattern-transform-controls");
  const transformNote = dialog.querySelector(".pattern-transform-note");
  const resetButton = dialog.querySelector(".pattern-reset");
  const doneButton = dialog.querySelector(".pattern-done");
  const controls = [...dialog.querySelectorAll("[data-transform-key]")];
  const linkScale = dialog.querySelector(".pattern-link-scale input");
  const hasWater = (patternData.groups || []).some((group) => group.is_water);

  const syncChoiceAvailability = (group) => {
    picker.querySelectorAll(".pattern-library-choice").forEach((choice) => {
      const waterConflict = group.is_water
        ? choice.dataset.patternId !== "04_waves_sine"
        : choice.dataset.waterOnly === "true"
          || hasWater && choice.dataset.patternFamily === "waves";
      choice.disabled = waterConflict;
      choice.title = waterConflict
        ? (group.is_water ? "Water uses the sinusoidal wave pattern"
          : "The sinusoidal wave pattern is reserved for water")
        : "";
      choice.classList.toggle("selected", choice.dataset.patternId === group.pattern);
    });
  };

  const valuesFromControls = () => Object.fromEntries(
    [...new Set(controls.map((input) => input.dataset.transformKey))].map((key) => [
      key, Number(dialog.querySelector(`input[type="number"][data-transform-key="${key}"]`).value),
    ]));
  const setControlValue = (key, value) => {
    dialog.querySelectorAll(`[data-transform-key="${key}"]`).forEach((input) => {
      input.value = value;
    });
  };
  const refreshRenderedViews = (groupId) => {
    const stamp = Date.now();
    const finalUrl = `/api/artifact/${encodeURIComponent(stem)}/step8a_cleanup.png?t=${stamp}`;
    const mapImage = stepOutput.querySelector(".step7-live-map");
    const mapLink = stepOutput.querySelector(".step7-final-link");
    if (mapImage) mapImage.src = finalUrl;
    if (mapLink) mapLink.href = finalUrl;
    stepOutput.querySelectorAll(`[data-pattern-group="${groupId}"] img`).forEach((image) => {
      image.src = `/api/pattern-preview/${encodeURIComponent(stem)}/${groupId}?t=${stamp}`;
    });
    preview.src = `/api/pattern-preview/${encodeURIComponent(stem)}/${groupId}?t=${stamp}`;
  };
  const saveTransform = async (transform) => {
    if (!activeGroup) return;
    if (saving) { queuedTransform = transform; return; }
    saving = true;
    status.textContent = "Updating the tactile map…";
    status.className = "pattern-transform-status saving";
    const group = activeGroup;
    try {
      const result = await api(`/api/pattern-transforms/${encodeURIComponent(stem)}/${group.group_id}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(transform),
      });
      group.transform = result.transform;
      refreshRenderedViews(group.group_id);
      status.textContent = "Applied to the map";
      status.className = "pattern-transform-status saved";
      if (!queuedTransform) dirty = false;
    } catch (error) {
      status.textContent = error.message;
      status.className = "pattern-transform-status error";
    } finally {
      saving = false;
      if (queuedTransform) {
        const next = queuedTransform;
        queuedTransform = null;
        saveTransform(next);
      }
    }
  };
  const scheduleSave = () => {
    if (!activeGroup?.editable) return;
    dirty = true;
    clearTimeout(saveTimer);
    status.textContent = "Change pending…";
    status.className = "pattern-transform-status pending";
    saveTimer = setTimeout(() => saveTransform(valuesFromControls()), 220);
  };

  controls.forEach((input) => {
    input.oninput = () => {
      setControlValue(input.dataset.transformKey, input.value);
      if (linkScale.checked && input.dataset.transformKey === "scale_x_percent") {
        setControlValue("scale_y_percent", input.value);
      } else if (linkScale.checked && input.dataset.transformKey === "scale_y_percent") {
        setControlValue("scale_x_percent", input.value);
      }
      scheduleSave();
    };
  });

  const openEditor = (group) => {
    activeGroup = group;
    dirty = false;
    stepOutput.querySelectorAll(".pattern-legend-item").forEach((button) => {
      button.classList.toggle("selected", Number(button.dataset.patternGroup) === group.group_id);
    });
    dialog.querySelector("h4").textContent = `Edit pattern — ${group.label}`;
    dialog.querySelector(".pattern-dialog-area").innerHTML =
      `<code>${esc(group.pattern)}</code> — ${esc(group.pattern_desc)}`;
    picker.hidden = true;
    previewBox.hidden = false;
    transformControls.hidden = false;
    transformNote.hidden = false;
    resetButton.hidden = false;
    doneButton.textContent = "Done";
    Object.entries(group.transform).forEach(([key, value]) => setControlValue(key, value));
    linkScale.checked = group.transform.scale_x_percent === group.transform.scale_y_percent;
    controls.forEach((input) => { input.disabled = !group.editable; });
    resetButton.disabled = !group.editable;
    status.textContent = group.editable
      ? "Adjust a value to update the pattern on the map."
      : "This is a solid fill, so it has no repeating pattern transform.";
    status.className = "pattern-transform-status";
    preview.src = `/api/pattern-preview/${encodeURIComponent(stem)}/${group.group_id}?t=${Date.now()}`;
    dialog.hidden = false;
    layout.classList.add("editor-open");
  };
  const openChanger = (group) => {
    activeGroup = group;
    dirty = false;
    clearTimeout(saveTimer);
    stepOutput.querySelectorAll(".pattern-legend-item").forEach((item) => {
      item.classList.toggle("selected", Number(item.dataset.patternGroup) === group.group_id);
    });
    dialog.querySelector("h4").textContent = `Change pattern — ${group.label}`;
    dialog.querySelector(".pattern-dialog-area").textContent =
      "Your selection is fixed; all remaining patterns are recalculated.";
    previewBox.hidden = false;
    preview.src = `/api/pattern-preview/${encodeURIComponent(stem)}/${group.group_id}?t=${Date.now()}`;
    transformControls.hidden = true;
    transformNote.hidden = true;
    resetButton.hidden = true;
    doneButton.textContent = "Close";
    picker.hidden = false;
    syncChoiceAvailability(group);
    status.textContent = "Choose a pattern; sinusoidal waves are reserved for water.";
    status.className = "pattern-transform-status";
    dialog.hidden = false;
    layout.classList.add("editor-open");
  };
  stepOutput.querySelectorAll(".pattern-legend-item").forEach((item) => {
    const group = patternData.groups.find(
      (candidate) => candidate.group_id === Number(item.dataset.patternGroup));
    item.querySelector(".pattern-edit-action").onclick = () => openEditor(group);
    item.querySelector(".pattern-change-action").onclick = () => openChanger(group);
  });
  picker.querySelectorAll(".pattern-library-choice").forEach((choice) => {
    choice.onclick = async () => {
      if (!activeGroup) return;
      const group = activeGroup;
      picker.querySelectorAll(".pattern-library-choice").forEach((button) => {
        button.disabled = true;
      });
      choice.classList.add("selected");
      status.textContent = "Re-optimizing adjacent pattern distances…";
      status.className = "pattern-transform-status saving";
      try {
        await api(`/api/pattern-assignments/${encodeURIComponent(stem)}/${group.group_id}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pattern: choice.dataset.patternId }),
        });
        status.textContent = "Pattern changed and the map was re-optimized.";
        status.className = "pattern-transform-status saved";
        await renderMap();
      } catch (error) {
        status.textContent = error.message;
        status.className = "pattern-transform-status error";
        syncChoiceAvailability(group);
      }
    };
  });
  resetButton.onclick = () => {
    Object.entries(patternData.defaults).forEach(([key, value]) => setControlValue(key, value));
    linkScale.checked = true;
    scheduleSave();
  };
  const close = () => {
    clearTimeout(saveTimer);
    if (activeGroup?.editable && dirty) saveTransform(valuesFromControls());
    dialog.hidden = true;
    layout.classList.remove("editor-open");
    stepOutput.querySelectorAll(".pattern-legend-item.selected").forEach(
      (button) => button.classList.remove("selected"));
  };
  dialog.querySelector(".pattern-dialog-close").onclick = close;
  doneButton.onclick = close;
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
  const editors = groups.map((group, slot) => `<div class="aggregation-group-editor" data-group-slot="${slot}">
    <label>Final category ${slot + 1}<input class="aggregation-group-label" value="${esc(group.label || `Final category ${slot + 1}`)}"></label>
    <p class="aggregation-group-members"></p>
    <label class="aggregation-group-approval"><input type="checkbox" class="aggregation-group-approved" ${group.approved ? "checked" : ""}>
      Approve this multi-class merge</label>
    <input class="aggregation-group-rationale" value="${esc(group.rationale || "human-reviewed grouping")}" aria-label="Grouping rationale">
  </div>`).join("");
  const classRows = source.map((cl) => `<tr><td>${esc(cl.label)}</td><td><select class="aggregation-group-select" data-class-index="${cl.index}">
    ${groups.map((_, slot) => `<option value="${slot}" ${assigned.get(Number(cl.index)) === slot ? "selected" : ""}>Final category ${slot + 1}</option>`).join("")}
  </select></td></tr>`).join("");
  el.insertAdjacentHTML("beforeend", `<section class="aggregation-review ${extraClass}">
    <div class="aggregation-review-heading"><div><h5>${esc(title)}</h5>
      <p class="hint">Review the actual merged categories before their shared geography is simplified. The texture limit is a maximum, not a target.</p></div>
      <span class="badge ${status === "approved" ? "" : "neutral"}">${esc(status)}</span></div>
    <div class="aggregation-group-editors">${editors}</div>
    <details><summary>Adjust which source classes belong together</summary>
      <table class="data aggregation-class-assignment"><thead><tr><th>Source category</th><th>Final tactile category</th></tr></thead><tbody>${classRows}</tbody></table>
    </details>
    <button class="btn primary aggregation-review-save">Save aggregation review</button>
    <p class="hint">Every multi-class merge must be approved before Step 6 can run.</p>
  </section>`);

  const section = el.lastElementChild;
  const refresh = (changed = false) => {
    const selections = [...section.querySelectorAll(".aggregation-group-select")];
    groups.forEach((_, slot) => {
      const editor = section.querySelector(`[data-group-slot="${slot}"]`);
      const members = selections.filter((select) => Number(select.value) === slot)
        .map((select) => source.find(
          (cl) => Number(cl.index) === Number(select.dataset.classIndex))).filter(Boolean);
      editor.querySelector(".aggregation-group-members").textContent = members.length
        ? members.map((member) => member.label).join(" + ") : "Unused category slot";
      const approval = editor.querySelector(".aggregation-group-approved");
      approval.disabled = members.length <= 1;
      if (members.length <= 1) approval.checked = true;
      else if (changed) approval.checked = false;
      editor.classList.toggle("unused", members.length === 0);
    });
  };
  section.querySelectorAll(".aggregation-group-select").forEach((select) => {
    select.onchange = () => refresh(true);
  });
  refresh(false);
  section.querySelector(".aggregation-review-save").onclick = async () => {
    const selections = [...section.querySelectorAll(".aggregation-group-select")];
    const decisions = groups.map((_, slot) => {
      const editor = section.querySelector(`[data-group-slot="${slot}"]`);
      return {
        label: editor.querySelector(".aggregation-group-label").value,
        members: selections.filter((select) => Number(select.value) === slot)
          .map((select) => Number(select.dataset.classIndex)),
        approved: editor.querySelector(".aggregation-group-approved").checked,
        rationale: editor.querySelector(".aggregation-group-rationale").value,
      };
    }).filter((group) => group.members.length);
    try {
      await api(endpoint, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groups: decisions }) });
      const main = $("main");
      const scrollY = main.scrollTop;
      await loadMaps();
      const current = mapRec();
      if (current) {
        renderSidebar();
        await refreshCanonicalDownstreamCards(current, [5, 6, 7]);
      }
      requestAnimationFrame(() => { main.scrollTop = scrollY; });
    } catch (error) { alert(error.message); }
  };
}

async function renderStep6(el, stem) {
  const [sum, cg, counts, presetData] = await Promise.all([
    artifactJson(stem, "step6_summary.json"),
    artifactJson(stem, "classes_gen.json"),
    api(`/api/geocounts/${encodeURIComponent(stem)}?gen=1`),
    step6PresetData(stem),
  ]);
  if (!sum || !cg) return;
  const rows = step5ClassRows(cg.classes);
  const vanished = cg.classes.filter((c) => !c.area_px).map((c) => c.label);
  el.insertAdjacentHTML("beforeend", `
    <section class="p5-review" aria-labelledby="p5-review-title">
      <h4 id="p5-review-title">Review the adjusted map</h4>
      <div class="p5-compare-labels"><span>Original map</span><span>Adjusted map preview</span></div>
      <a class="imglink p5-preview-link" href="${artifactUrl(stem, "step6_debug.png")}" target="_blank">open full size ↗</a>
      <img class="artifact-img p5-preview-image" src="${artifactUrl(stem, "step6_debug.png")}" alt="Adjusted map preview">
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
      <div class="p5-vanished">${vanished.length
        ? `<ul class="notelist"><li>Categories removed: ${vanished.map(esc).join(", ")}</li></ul>` : ""}</div>
    </details>`);

  const body = el.closest(".step-body");
  body._showStep6Preset = (level) => {
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
    const variantVanished = variant.classes.filter((c) => !c.area_px).map((c) => c.label);
    el.querySelector(".p5-vanished").innerHTML = variantVanished.length
      ? `<ul class="notelist"><li>Categories removed: ${variantVanished.map(esc).join(", ")}</li></ul>`
      : "";
  };
  if (body._step6PreviewLevel) body._showStep6Preset(body._step6PreviewLevel);
}

function debugImage(stem, name) {
  const url = artifactUrl(stem, name);
  return `<a class="imglink" href="${url}" target="_blank">open full size ↗</a>
          <img class="artifact-img" src="${url}" alt="${esc(name)}">`;
}
const warnList = (ws) => ws && ws.length
  ? `<ul class="warnlist">${ws.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";

/* ------------------------------------------------------------------ jobs */

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
  if (m) await deleteProject(m);
};

/* ------------------------------------------------------------------ boot */

loadModels().then(loadMaps).then(() => {
  if (MAPS.length) selectMap(MAPS[0].stem);
});
