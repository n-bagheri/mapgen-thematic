"use strict";

/* ------------------------------------------------------------------ state */

const STEPS = [
  { n: 1, title: "Step 1 — Semantic interpretation",
    desc: "The selected model reads the map: type, classes, priorities, expected text" },
  { n: 2, title: "Step 2 — Map & legend isolation",
    desc: "Layout boxes + pixel-exact mask; legend swatch colors sampled" },
  { n: 3, title: "Step 3 — Overlay text detection",
    desc: "Labels transcribed, classified, anchored; removal mask built" },
  { n: 4, title: "Step 4 — Segmentation & lines",
    desc: "Areas classified, text removed, halos dissolved, lines vectorized (no AI)" },
  { n: 5, title: "Step 5 — Simplify for touch",
    desc: "Choose how much map detail to keep, then compare the adjusted result with the original" },
  { n: 6, title: "Step 6 — Class aggregation",
    desc: "Classes merged into the available texture slots (review the plan here)" },
  { n: 7, title: "Step 7 — Tactile symbols & master render",
    desc: "Patterns assigned (water = waves, ordered = ramp) and the tactile master rendered" },
];

const KIND_COLORS = { capital: "#d62728", city: "#e28a1b", river_label: "#1f77d0",
  region_label: "#1e9e5a", line_label: "#7a3fbf", other: "#8a8f98" };

let MAPS = [];
let SELECTED = null;
let POLL = null;
const OPEN_STEPS = new Set([1, 2, 3, 4, 5]);

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

async function renderMap() {
  const m = mapRec();
  if (!m) return;
  const scrollY = $("main").scrollTop; // full rebuilds must not jump the page
  PENDING_VIEWS = [];
  $("map-title").textContent = m.name;
  const stepsEl = $("steps");
  stepsEl.innerHTML = "";
  for (const s of STEPS) {
    stepsEl.appendChild(await stepCard(m, s));
  }
  // restore twice: once now, once after async artifact views finish loading
  requestAnimationFrame(() => { $("main").scrollTop = scrollY; });
  Promise.allSettled(PENDING_VIEWS).then(() =>
    requestAnimationFrame(() => { $("main").scrollTop = scrollY; }));
  const remaining = STEPS.filter((s) => !m.steps[s.n]).map((s) => s.n);
  const runAll = $("run-all");
  runAll.textContent = remaining.length ? `Run remaining (${remaining.join(", ")})` : "All steps done";
  runAll.disabled = !remaining.length || (m.job && m.job.status === "running");
  $("delete-project").disabled = Boolean(m.job && m.job.status === "running");
  $("model-select").disabled = Boolean(m.job && m.job.status === "running");
  runAll.onclick = () => runSteps(remaining);
  renderJobBanner();
}

async function stepCard(m, s) {
  const done = m.steps[s.n];
  const job = m.job;
  const running = job && job.status === "running" && job.current === s.n;
  const failed = job && job.status === "failed" && job.current === null &&
                 job.steps.includes(s.n) && !done;

  const card = document.createElement("div");
  card.className = "step-card";
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
  actions.innerHTML = `
    <button class="btn primary" ${busy ? "disabled" : ""}>${runLabel}</button>
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
  border_or_coast: "Borders or coastlines", line: "Other lines",
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
    try {
      await api(`/api/step5preset/${encodeURIComponent(stem)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: selectedLevel }) });
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

async function renderArtifacts(step, el) {
  const stem = SELECTED;
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
    const lj = await artifactJson(stem, "labels.json");
    el.insertAdjacentHTML("beforeend", debugImage(stem, "step3_debug.png"));
    if (lj) {
      const rows = lj.labels.map((l) => `<tr>
        <td>${chip(KIND_COLORS[l.kind] || "#999")}${esc(l.kind)}</td>
        <td>${esc(l.text)}</td>
        <td>${esc(l.localization ?? "gemini")}</td>
        <td>${esc(l.anchor_source)}</td>
        <td>${l.mask_found ? '<span class="tick">✔</span>' : '<span class="cross">box fill</span>'}</td>
        <td>${l.matches_step1 ? '<span class="tick">✔</span>' : '<span class="cross">✘</span>'}</td>
      </tr>`).join("");
      el.insertAdjacentHTML("beforeend", `
        <table class="data"><tr><th>kind</th><th>text</th><th>boxes</th><th>anchor</th><th>strokes</th><th>vocab</th></tr>
        ${rows || `<tr><td colspan="6">no overlay text on this map</td></tr>`}</table>
        ${warnList(lj.warnings)}`);
    }
  }

  if (step === 4) {
    const cf = await artifactJson(stem, "classes_final.json");
    const counts = await api(`/api/geocounts/${encodeURIComponent(stem)}`);
    el.insertAdjacentHTML("beforeend", debugImage(stem, "step4_debug.png"));
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
  }

  if (step === 7) {
    const sym = await artifactJson(stem, "symbols.json");
    el.insertAdjacentHTML("beforeend", debugImage(stem, "step7_tactile.png"));
    if (!sym) return;
    el.insertAdjacentHTML("beforeend", `
      <table class="data"><tr><th>area</th><th>tactile pattern</th><th>why</th></tr>
      ${sym.area_assignments.map((a) => `<tr>
        <td>${esc(a.label)}</td><td><code>${esc(a.pattern)}</code> — ${esc(a.pattern_desc)}</td>
        <td>${esc(a.rationale)}</td></tr>`).join("")}
      </table>
      <div class="badges">${Object.entries(sym.line_styles).map(([k, v]) =>
        `<span class="badge neutral">${esc(k)}: ${esc(v.desc)}</span>`).join("")}</div>
      ${warnList(sym.notes)}`);
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
