"use strict";

import { $, esc, resetFrom, saveSpecText } from "./api.js";
import {
  ANALYSIS_BATCH, FINAL_BATCH, INITIAL_BATCH, LABEL_BATCH, PAGE_SIZES, PATTERN_BATCH,
  SIMPLIFICATION_BATCH, STEP_DEFS,
  allDone, blockingReason, completedCount, pageSizeKey,
} from "./steps.js";
import {
  currentStepKey, forgetIndividualMode, isRunning, rememberIndividualMode,
  selectedMap, state, statusFor, toast, withBusy,
} from "./state.js";
import {
  loadMaps, refreshSelectedData, renderWorkspace, savePreflight, startJob,
} from "./workspace.js";
import { aggregationGateHtml, bindAggregationEditor } from "./editors/aggregation.js";
import { bindMaskEditor, maskDecisionHtml } from "./editors/mask.js";
import { bindTextEditor, textEditorHtml } from "./editors/text.js";
import { bindLineEditor, lineEditorHtml } from "./editors/lines.js";
import {
  bindSimplificationEditor, simplificationDecisionHtml,
} from "./editors/simplification.js";
import {
  bindPatternEditor, patternDecisionHtml,
} from "./editors/patterns.js";
import { bindBrailleEditor, brailleDecisionHtml } from "./editors/braille.js";
import { bindLegendEditor, legendDecisionHtml } from "./editors/legend.js";
import { bindExportEditor, exportEditorHtml } from "./editors/exportpdf.js";
import { activeStep, setActiveStep } from "./visual.js";

/* The control pane shows exactly one body at a time — a run, a decision, the
   setup, or the editors — with the progress summary above and the reset row
   below.  Which one is decided here and nowhere else. */

export function renderControls() {
  const map = selectedMap();
  if (!map) return;
  const failed = state.job.status === "failed";
  const live = state.job.status === "running" || isRunning(map);
  const blocked = blockingReason(map);
  const started = live || failed || Boolean(blocked) || completedCount(map) > 0
    || state.individualRun;
  const preflight = !started;
  let body = started ? individualRunHtml(map) : setupHtml(true, true);
  if (blocked) body += scopeBlockHtml(map, blocked);
  if (failed) body += failurePanelHtml(map);
  $("control-content").innerHTML = `
    <div class="control-shell${preflight ? " is-preflight" : ""}">
      <header class="control-header">
        <div><h2>${esc(map.name)}</h2></div>
      </header>
      ${body}
      ${resetRowHtml(map, live)}
    </div>`;
  bindControlEvents();
}

/** An editor is a block inside its step, not a section of its own: the step
 *  list is the only numbering the reader sees. */
export function editorDetails(id, number, title, subtitle, body) {
  return `<section class="step-editor" data-editor="${id}">
    <h4>${title}${subtitle ? `<small>${subtitle}</small>` : ""}</h4>
    ${body}
  </section>`;
}

function readableMapType(value) {
  const labels = {
    area_class_chorochromatic: "Area-class thematic map",
    isopleth: "Isopleth map",
    classed_sequential: "Classed sequential map",
    choropleth: "Choropleth map",
  };
  return labels[value] || String(value || "Unknown map type").replaceAll("_", " ");
}

/** Step 1's structured reading is much more useful than an empty settings
 * message. Keep the main interpretation visible and tuck the long model prose
 * and complete class list into one disclosure. */
function readingEditorHtml() {
  const sem = state.data.semantics;
  if (!sem) {
    return editorDetails("reading", "1", "What the system read", "",
      '<div class="empty-editor">The Step 1 reading is not available.</div>');
  }
  const classes = Array.isArray(sem.thematic_classes) ? sem.thematic_classes : [];
  const visibleClasses = classes.slice(0, 6);
  const more = Math.max(0, classes.length - visibleClasses.length);
  const detailRows = classes.map((item) => `<li><span>${esc(item.label)}</span>
    ${Number.isFinite(Number(item.approx_area_share_percent))
      ? `<small>${esc(item.approx_area_share_percent)}% estimated area</small>` : ""}</li>`).join("");
  const body = `
    <section class="reading-summary" aria-label="Step 1 map interpretation">
      <p class="reading-subject">${esc(sem.subject || sem.title || "Map subject not identified")}</p>
      <div class="reading-facts">
        <span>${esc(readableMapType(sem.map_type))}</span>
        <span>${esc(String(sem.data_ordering || "unknown"))} data</span>
        <span>${esc(sem.map_language || "Unknown language")}</span>
        <span>${sem.water_present ? "Separate water identified" : "No separate water layer"}</span>
        <span class="${sem.in_scope === false ? "is-warning" : ""}">${sem.in_scope === false
          ? "Outside tactile pipeline scope" : "Suitable for tactile conversion"}</span>
      </div>
      ${sem.title ? `<p class="reading-title"><span>Detected title</span>${esc(sem.title)}</p>` : ""}
      <div class="reading-class-preview">
        <span class="field-label">Legend categories read</span>
        <div>${visibleClasses.map((item) => `<span>${esc(item.label)}</span>`).join("")
          || '<small>No thematic categories were returned.</small>'}
          ${more ? `<span class="reading-more-chip">+${more} more</span>` : ""}</div>
      </div>
      ${(sem.description || detailRows) ? `<details class="reading-details">
        <summary>See full reading</summary>
        ${sem.description ? `<p>${esc(sem.description)}</p>` : ""}
        ${detailRows ? `<ol>${detailRows}</ol>` : ""}
      </details>` : ""}
    </section>`;
  return editorDetails("reading", "1", "What the system read", "", body);
}

/* Which editors belong to which step.  Everything the focused view can change
   is reached by opening the step that owns it. */
const STEP_EDITORS = {
  1: [readingEditorHtml],
  2: [maskDecisionHtml],
  3: [textEditorHtml],
  4: [lineEditorHtml],
  5: [aggregationGateHtml],
  6: [simplificationDecisionHtml],
  7: [patternDecisionHtml],
  8: [brailleDecisionHtml],
  9: [legendDecisionHtml],
};

/** The right pane mirrors the detailed page: one row per pipeline step, in
 *  order, carrying that step's status, its controls, and its rerun button.
 *  Opening a row is what points the left pane at that step's pictures. */
function stepStackHtml(map, completedOnly = false) {
  const open = activeStep(map);
  const listedSteps = completedOnly
    ? STEP_DEFS.filter((step) => Boolean(map.steps?.[step.key]))
    : STEP_DEFS;
  const rows = listedSteps.map((step) => {
    const number = Number(step.key);
    const done = Boolean(map.steps?.[step.key]);
    const running = currentStepKey(map) === step.key && state.job.status === "running";
    const statusText = running ? "Running" : done ? "Ready" : "Waiting";
    const statusClass = running ? "running" : done ? "done" : "";
    const body = done
      ? (STEP_EDITORS[number] || []).map((build) => build(map)).join("")
        || '<p class="section-intro">This step has no settings to review.</p>'
      : `<p class="section-intro">${esc(step.blurb)}</p>`;
    return `<details class="step-section" data-step="${step.key}" ${number === open ? "open" : ""}>
      <summary>
        <span class="editor-number">${esc(step.number)}</span>
        <span class="editor-title"><strong>${esc(step.title)}</strong>
          <small>${esc(step.caption)}</small></span>
        <span class="step-state ${statusClass}">${statusText}</span>
        <span class="editor-caret" aria-hidden="true"></span>
      </summary>
      <div class="editor-body">
        ${body}
        ${stepRunRowHtml(map, step, done, running)}
      </div>
    </details>`;
  }).join("");
  const exportPanel = map.steps?.["9"] ? exportEditorHtml(map) : "";
  return `<div class="step-stack">${rows}</div>${exportPanel}`;
}

/** Per-step rerun, as on the detailed page.  Rerunning a step the pipeline has
 *  already passed clears what was built from it, so the label says so. */
function stepRunRowHtml(map, step, done, running) {
  if (running) return '<p class="status-copy">This step is running…</p>';
  const number = Number(step.key);
  const previous = STEP_DEFS.find((item) => Number(item.key) === number - 1);
  const blocked = previous && !map.steps?.[previous.key];
  if (blocked) {
    return `<p class="status-copy">Run step ${esc(previous.number)} first.</p>`;
  }
  return `<div class="action-row end">
      <span class="status-copy">${done ? "Rerunning clears the steps built from this one." : ""}</span>
      <button class="button ${done ? "subtle" : "primary"} small" data-run-step="${step.key}" type="button">
        ${done ? "Rerun" : "Run"} step ${esc(step.number)}</button>
    </div>`;
}

function progressHtml(map) {
  const current = currentStepKey(map);
  const jobStatus = state.job.status;
  const status = statusFor(map);
  const requested = (state.job.steps || []).map(String);
  const failedKey = jobStatus === "failed"
    ? requested.find((step) => !map.steps?.[step]) || requested.at(-1) : null;
  const classFor = (key) => current === key && jobStatus === "running"
    ? "running"
    : map.steps?.[key] ? "done" : failedKey === key ? "failed" : "";
  const pips = STEP_DEFS.map((step) =>
    `<span class="step-pip ${classFor(step.key)}" title="${esc(step.title)}"></span>`).join("");
  const rows = STEP_DEFS.map((step) => {
    const className = classFor(step.key);
    const rowStatus = className === "running" ? "In progress"
      : className === "done" ? "Ready"
        : className === "failed" ? "Failed" : "Waiting";
    return `<div class="progress-row">
      <span class="progress-number">${step.number}</span>
      <span>${esc(step.title)}</span>
      <small>${rowStatus}</small><span class="progress-status ${className}" hidden></span>
    </div>`;
  }).join("");
  const total = STEP_DEFS.length;
  return `
    <details class="progress-disclosure">
      <summary>
        <span class="progress-copy"><strong>${esc(status.label)}</strong>
          <small>${completedCount(map)} of ${total} stages · expand for details</small></span>
        <span class="progress-track" aria-label="${completedCount(map)} of ${total} stages complete">${pips}</span>
      </summary>
      <div class="progress-details">${rows}</div>
    </details>`;
}

/** Bottom of the control pane: clear the map back to setup.  There is no way
 *  to stop a run part-way, so a live run only explains itself. */
function resetRowHtml(map, live) {
  if (!live && !completedCount(map)) return "";
  return `
    <div class="reset-row">
      <button class="button danger small" id="reset-map" type="button" ${live ? "disabled" : ""}>Start over</button>
      ${live ? "<span>A step cannot be interrupted; the run stops on its own if a step fails.</span>" : ""}
    </div>`;
}

async function resetMap() {
  const map = selectedMap();
  if (!map) return;
  if (!window.confirm(`Delete every result for ${map.name} and return to the run setup?`)) return;
  await withBusy($("reset-map"), "Resetting…", async () => {
    await resetFrom(state.selected, 1);
    forgetIndividualMode(state.selected);
    state.job = { status: "idle" };
    state.viewStep = null;
    state.activeStep = 1;
    state.individualRun = false;
    state.previewLevel = null;
    state.groupEdit = null;
    state.groupLabels = {};
    state.visibleGroupSlots = null;
    if (state.aggregationPreviewUrl) URL.revokeObjectURL(state.aggregationPreviewUrl);
    state.aggregationPreviewUrl = null;
    state.maskBrush.strokes = [];
    state.maskBrush.active = false;
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
  });
}

/** Step 1 decided this map cannot go further.  Only Step 1 may be rerun, so
 *  that is the only action offered. */
function scopeBlockHtml(map, reason) {
  return `
    <section class="review-gate scope-block" role="alert">
      <span class="section-kicker">Pipeline blocked</span>
      <h3>This map cannot continue past Step 1.</h3>
      <p class="section-intro">${esc(reason)}</p>
      <div class="action-row">
        <a class="quiet-link" href="/">Open detailed diagnostics</a>
        <button class="button primary small" id="rerun-step1" type="button">Re-read the map</button>
      </div>
    </section>`;
}

/* ------------------------------------------------------------ run setup --- */

const PAGE_SIZE_LABELS = { a4: "A4 · 210 × 297 mm", a3: "A3 · 297 × 420 mm", custom: "Custom" };
const MEDIUM_LABELS = {
  swell_paper: "Swell paper",
  embosser: "Embosser",
  print_3d: "3D print",
};
const BRAILLE_LABELS = {
  "unified-english-grade1": "Unified English Braille · Grade 1",
};

let specSaveChain = Promise.resolve();

function modelOptions() {
  if (!state.models.length) return '<option value="">Default pipeline model</option>';
  return state.models.map((model) => `<option value="${esc(model.id)}"
    ${model.id === (state.model || state.defaultModel) ? "selected" : ""}>${esc(model.label)}</option>`).join("");
}

function setupHtml(showRun = true, showIndividual = false) {
  const spec = state.spec;
  const constants = spec?.constants || {};
  const size = state.customPage ? "custom" : pageSizeKey(spec);
  const orientation = spec?.orientation || "portrait";
  const disabled = spec ? "" : "disabled";
  const option = (value, current, label) =>
    `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`;
  const options = (labels, current) => Object.entries(labels)
    .map(([value, label]) => option(value, current, label)).join("");
  const value = (candidate) => Number.isFinite(Number(candidate)) ? Number(candidate) : "";
  return `
    <section class="setup-card featured">
      <div class="setup-heading"><h3>Run setup</h3>
        <span>Model per run · output specification shared by every map</span></div>
      <div class="setup-fields"><div class="form-grid" id="preflight-form">
        <label class="field">
          <span>Model</span>
          <select id="model-select">${modelOptions()}</select>
        </label>
        <label class="field">
          <span>Output medium</span>
          <select id="output-medium" ${disabled}>
            ${options(MEDIUM_LABELS, spec?.medium || "swell_paper")}
          </select>
        </label>
        <label class="field">
          <span>Page size</span>
          <select id="page-size" ${disabled}>
            ${option("a4", size, PAGE_SIZE_LABELS.a4)}
            ${option("a3", size, PAGE_SIZE_LABELS.a3)}
            ${option("custom", size, PAGE_SIZE_LABELS.custom)}
          </select>
        </label>
        <label class="field">
          <span>Orientation</span>
          <select id="page-orientation" ${disabled}>
            ${option("portrait", orientation, "Portrait")}
            ${option("landscape", orientation, "Landscape")}
          </select>
        </label>
        ${size === "custom" && spec ? `
        <div class="field">
          <span>Custom page (mm)</span>
          <div class="size-row">
            <input id="page-width" type="number" min="40" step="1"
                   value="${Number(spec.page_width_mm)}" aria-label="Page width in millimetres">
            <span aria-hidden="true">×</span>
            <input id="page-height" type="number" min="40" step="1"
                   value="${Number(spec.page_height_mm)}" aria-label="Page height in millimetres">
          </div>
        </div>` : ""}
        <label class="field">
          <span>Page margin</span>
          <input id="page-margin" type="number" min="0" step="0.5"
                 value="${value(spec?.margin_mm)}" ${disabled}>
          <small class="field-note">Millimetres on every side.</small>
        </label>
        <label class="field">
          <span>Braille standard</span>
          <select id="braille-standard" ${disabled}>
            ${options(BRAILLE_LABELS, spec?.braille_standard || "unified-english-grade1")}
          </select>
        </label>
      </div>
      <details class="output-spec-advanced" id="advanced-output-spec"
               ${state.specAdvancedOpen ? "open" : ""}>
        <summary><span>Advanced tactile settings</span>
          <small>Physical minimums used by every pipeline step</small></summary>
        <div class="output-spec-grid">
          <label class="field"><span>Braille cell width</span>
            <input type="number" min="0.1" step="0.1" value="${value(constants.braille_cell_width_mm)}"
                   data-spec-constant="braille_cell_width_mm" ${disabled}>
            <small class="field-note">Millimetres, including cell spacing.</small>
          </label>
          <label class="field"><span>Braille cell height</span>
            <input type="number" min="0.1" step="0.1" value="${value(constants.braille_cell_height_mm)}"
                   data-spec-constant="braille_cell_height_mm" ${disabled}>
            <small class="field-note">Millimetres, including line spacing.</small>
          </label>
          <label class="field"><span>Minimum texture side</span>
            <input type="number" min="0.1" step="0.5" value="${value(constants.min_texture_area_side_mm)}"
                   data-spec-constant="min_texture_area_side_mm" ${disabled}>
            <small class="field-note">Smallest textured square, in millimetres.</small>
          </label>
          <label class="field"><span>Minimum element gap</span>
            <input type="number" min="0.1" step="0.5" value="${value(constants.min_element_gap_mm)}"
                   data-spec-constant="min_element_gap_mm" ${disabled}>
            <small class="field-note">Clear tactile separation, in millimetres.</small>
          </label>
          <label class="field"><span>Minimum line width</span>
            <input type="number" min="0.1" step="0.1" value="${value(constants.min_line_width_mm)}"
                   data-spec-constant="min_line_width_mm" ${disabled}>
          </label>
          <label class="field"><span>Minimum line length</span>
            <input type="number" min="0.1" step="0.5" value="${value(constants.min_line_length_mm)}"
                   data-spec-constant="min_line_length_mm" ${disabled}>
          </label>
          <label class="field fixed-spec-field"><span>Maximum area textures</span>
            <input id="max-area-textures" type="number" value="${value(constants.max_area_textures)}"
                   readonly aria-readonly="true">
            <small class="field-note">Fixed at five by the current pipeline.</small>
          </label>
        </div>
      </details></div>
      ${showRun ? `<div class="action-row end${showIndividual ? " preflight-actions" : ""}">
        ${showIndividual ? `<button class="button primary" id="show-step-controls" type="button">
          Run each step individually</button>` : ""}
        <button class="button primary run-button" id="run-all" type="button">Run all</button>
      </div>` : ""}
    </section>`;
}

function individualStepDotsHtml(map) {
  const selected = activeStep(map);
  const dots = STEP_DEFS.map((step) => {
    const itemState = stepState(map, step);
    const reachable = itemState === "done" || itemState === "running" || itemState === "failed";
    return `<button class="step-dot ${itemState}" type="button" data-individual-step="${step.key}"
        aria-current="${Number(step.key) === selected}" ${reachable ? "" : "disabled"}
        aria-label="Step ${esc(step.number)} · ${esc(step.title)}"
        title="Step ${esc(step.number)} · ${esc(step.title)}">${esc(step.number)}</button>`;
  }).join("");
  return `<div class="step-dots individual-step-dots" role="group" aria-label="All pipeline steps">${dots}</div>`;
}

function individualRunHtml(map) {
  const completed = completedCount(map);
  const candidate = STEP_DEFS.find((step) => !map.steps?.[step.key]);
  const waitingForApproval = candidate?.key === "3" && !state.data.mask?.approved
    || candidate?.key === "6" && !map.step5_review_ready
    || candidate?.key === "7" && !map.step6_review_ready
    || candidate?.key === "8" && !map.step7_review_ready
    || candidate?.key === "9" && !map.step8_review_ready;
  const next = waitingForApproval ? null : candidate;
  const live = state.job.status === "running";
  const modeLabel = state.individualRun ? "Individual run" : "Run all";
  const nextAction = !live && next
    ? state.individualRun
      ? `<div class="action-row end individual-next-step">
          <button class="button primary" data-run-step="${next.key}" type="button">
            Run step ${esc(next.number)}</button>
        </div>`
      : !state.autorun
        ? `<div class="action-row end individual-next-step">
            <button class="button primary" id="continue-run" type="button">Continue run</button>
          </div>`
        : ""
    : "";
  return `
    <section class="individual-run-heading">
      ${individualStepDotsHtml(map)}
      <div><span class="section-kicker">${modeLabel}</span>
        <h3>${completed ? `${completed} completed step${completed === 1 ? "" : "s"}` : "No steps completed yet"}</h3></div>
    </section>
    ${completed ? stepStackHtml(map, true)
      : `<div class="empty-editor individual-empty">${live
        ? "The first step is running. Completed steps will appear here."
        : "Run Step 1 to begin. Completed steps will appear here."}</div>`}
    ${nextAction}`;
}

/** Re-rendering mid-edit would replace the inputs the user is still typing in,
 *  so only a change of page-size mode asks for one. */
function saveSpec(patch, rerender = false) {
  const save = async () => {
    if (!state.spec) {
      toast("The output spec could not be read; reload the page.", "error");
      return;
    }
    // Constants are nested in the JSON file. Merge a changed constant instead
    // of replacing its siblings, then serialize writes so quick edits cannot
    // land out of order and silently restore an older value.
    const next = {
      ...state.spec,
      ...patch,
      ...(patch.constants ? {
        constants: { ...(state.spec.constants || {}), ...patch.constants },
      } : {}),
    };
    try {
      await saveSpecText(next);
      state.spec = next;
      if (rerender) renderControls();
      toast("Output specification saved.");
    } catch (error) {
      toast(error.message, "error");
      renderControls();  // restore every field from the last valid spec
    }
  };
  const operation = specSaveChain.then(save, save);
  specSaveChain = operation.catch(() => {});
  return operation;
}

function bindPageSetup() {
  $("advanced-output-spec")?.addEventListener("toggle", (event) => {
    state.specAdvancedOpen = event.currentTarget.open;
  });
  $("output-medium")?.addEventListener("change", (event) => {
    saveSpec({ medium: event.target.value });
  });
  $("page-size")?.addEventListener("change", (event) => {
    const size = PAGE_SIZES[event.target.value];
    state.customPage = !size;
    if (!size) {
      renderControls();  // reveal the width/height fields; nothing saved yet
      return;
    }
    saveSpec({ page_width_mm: size[0], page_height_mm: size[1] }, true);
  });
  $("page-orientation")?.addEventListener("change", (event) => {
    saveSpec({ orientation: event.target.value });
  });
  $("page-margin")?.addEventListener("change", (event) => {
    saveSpec({ margin_mm: Number(event.target.value) });
  });
  $("braille-standard")?.addEventListener("change", (event) => {
    saveSpec({ braille_standard: event.target.value });
  });
  const saveCustom = () => saveSpec({
    page_width_mm: Number($("page-width").value),
    page_height_mm: Number($("page-height").value),
  });
  $("page-width")?.addEventListener("change", saveCustom);
  $("page-height")?.addEventListener("change", saveCustom);
  document.querySelectorAll("[data-spec-constant]").forEach((input) => {
    input.addEventListener("change", () => {
      saveSpec({ constants: { [input.dataset.specConstant]: Number(input.value) } });
    });
  });
}

/* ----------------------------------------------------------- step panel --- */

function stepState(map, step) {
  const requested = (state.job.steps || []).map(String);
  const failedKey = state.job.status === "failed"
    ? requested.find((key) => !map.steps?.[key]) || requested.at(-1) : null;
  if (currentStepKey(map) === step.key && state.job.status === "running") return "running";
  if (map.steps?.[step.key]) return "done";
  if (failedKey === step.key) return "failed";
  return "";
}

/** The step whose result the single panel shows: pinned by a circle, or the
 *  latest one that has finished, so each completed step replaces the last. */
function viewedStep(map) {
  const pinned = STEP_DEFS.find((step) => step.key === state.viewStep);
  if (pinned) return pinned;
  const done = STEP_DEFS.filter((step) => map.steps?.[step.key]);
  return done.at(-1)
    || STEP_DEFS.find((step) => step.key === currentStepKey(map))
    || STEP_DEFS[0];
}

function stepPanelHtml(map) {
  const running = STEP_DEFS.find((step) => step.key === currentStepKey(map));
  const step = viewedStep(map);
  const visibleSteps = state.individualRun
    ? STEP_DEFS.filter((item) => Boolean(stepState(map, item)))
    : STEP_DEFS;
  const dots = visibleSteps.map((item) => {
    const itemState = stepState(map, item);
    const reachable = itemState === "done" || itemState === "running" || itemState === "failed";
    return `<button class="step-dot ${itemState}" type="button" data-step-view="${item.key}"
        aria-current="${item.key === step.key}" ${reachable ? "" : "disabled"}
        aria-label="Step ${esc(item.number)} · ${esc(item.title)}"
        title="Step ${esc(item.number)} · ${esc(item.title)}">${esc(item.number)}</button>`;
  }).join("");
  const live = state.job.status === "running";
  const next = STEP_DEFS.find((item) => !map.steps?.[item.key]);
  const failed = state.job.status === "failed";
  const headline = failed
    ? `Stopped at ${running ? `${running.number} · ${running.title}` : "this step"}`
    : live
      ? `Working on ${running ? `${running.number} · ${running.title}` : "the next step"}`
      : next
        ? `Paused · next up ${next.number} · ${next.title}`
        : "Every step finished";
  const mood = failed ? "is-failed" : live ? "is-running" : "is-idle";
  // Paused runs keep the same status panel between steps; the button picks the
  // run back up from the first missing result.
  const resume = !live && !failed && next
    ? `<div class="action-row end"><button class="button primary" id="continue-run" type="button">
         Continue from ${esc(next.number)} · ${esc(next.title)}</button></div>`
    : "";
  return `
    <section class="step-panel" aria-live="polite" aria-busy="${live}">
      <div class="step-dots" role="group" aria-label="Pipeline steps">${dots}</div>
      <p class="step-status ${mood}">${esc(headline)}</p>
      <h3>${esc(step.number)} · ${esc(step.title)}</h3>
      <p class="step-blurb">${esc(step.blurb)}</p>
      ${resume}
    </section>`;
}

function failurePanelHtml(map) {
  const requested = (state.job.steps || []).map(String);
  const failedStep = requested.find((step) => !map.steps?.[step])
    || requested.at(-1)
    || STEP_DEFS.find((step) => !map.steps?.[step.key])?.key;
  const definition = STEP_DEFS.find((step) => step.key === failedStep);
  return `
    <section class="review-gate failure-card" role="alert">
      <span class="section-kicker">Pipeline paused</span>
      <h3>${esc(definition ? `${definition.title} could not finish.` : "This run could not finish.")}</h3>
      <p class="section-intro">${esc(state.job.error || "Retry the missing stages, or open the detailed view for the full log.")}</p>
      <div class="action-row">
        <a class="quiet-link" href="/">Open detailed diagnostics</a>
        <button class="button danger small" id="retry-failed" type="button">Retry missing stages</button>
      </div>
    </section>`;
}

function resultActionHtml(map) {
  if (allDone(map)) return "";
  const label = map.steps?.["8"] ? "Finish the legend page"
    : map.steps?.["7"] ? "Add the Braille labels"
      : "Continue to the tactile result";
  return `<section class="setup-card featured">
    <span class="section-kicker">Simplified map ready</span>
    <h3>Review it now, or continue to the tactile result.</h3>
    <p class="section-intro">Simplification, text, and linework remain editable below. Changes may clear later results so they cannot become stale.</p>
    <div class="action-row end"><button class="button primary" id="continue-pipeline" type="button">${label}</button></div>
  </section>`;
}

/* ------------------------------------------------------- run orchestration */

function retrySteps(map) {
  const requested = (state.job.steps || []).map(String);
  const missingRequested = requested.filter((step) => !map.steps?.[step]);
  if (missingRequested.length) return missingRequested;
  const batch = map.steps?.["5"]
    ? map.steps?.["6"] ? map.steps?.["7"]
      ? map.steps?.["8"] ? FINAL_BATCH : LABEL_BATCH
      : PATTERN_BATCH : SIMPLIFICATION_BATCH
    : map.steps?.["2"] ? ANALYSIS_BATCH : INITIAL_BATCH;
  return batch.filter((step) => !map.steps?.[step]);
}

async function retryFailedJob() {
  const map = selectedMap();
  if (!map) return;
  await withBusy($("retry-failed"), "Retrying…", async () => {
    const missing = retrySteps(map);
    state.autorun = true;
    if (missing.length) await startJob(missing);
    else await continuePipeline();
  });
}

async function runAll() {
  await withBusy($("run-all"), "Starting…", async () => {
    await savePreflight();
    rememberIndividualMode(state.selected, false);
    state.individualRun = false;
    state.autorun = true;
    const map = selectedMap();
    const missing = INITIAL_BATCH.filter((step) => !map.steps?.[step]);
    if (missing.length) await startJob(missing);
    else await continuePipeline();
  });
}

async function showIndividualSteps(event) {
  const button = event?.currentTarget || $("show-step-controls");
  await withBusy(button, "Starting Step 1…", async () => {
    await savePreflight();
    rememberIndividualMode(state.selected, true);
    state.individualRun = true;
    state.autorun = false;
    state.activeStep = 1;
    state.viewStep = null;
    await startJob(["1"]);
  });
}

/** Resume a paused run, saving the settings still on screen first. */
async function resumeRun() {
  await withBusy($("continue-run"), "Starting…", async () => {
    if ($("model-select")) await savePreflight();
    await continuePipeline();
  });
}

/** The automatic run has six human gates: Step 2 mask, Step 5 categories,
 *  Step 6 simplification, Step 7 patterns, Step 8 layout, and Step 9 legend. */
export async function continuePipeline() {
  if (state.continuing || state.job.status === "running") return;
  state.continuing = true;
  try {
    await loadMaps();
    const map = selectedMap();
    if (!map) return;
    if (blockingReason(map)) {
      await refreshSelectedData();
      renderWorkspace();
      return;
    }
    if (!map.steps?.["2"]) {
      const missing = INITIAL_BATCH.filter((step) => !map.steps?.[step]);
      state.autorun = true;
      await startJob(missing);
      return;
    }
    // Existing projects that have already passed Step 2 remain compatible;
    // the approval is required only before this run starts Step 3.
    if (!map.steps?.["3"]) {
      await refreshSelectedData();
      if (!state.data.mask?.approved) {
        state.autorun = true;
        state.activeStep = 2;
        state.maskBrush.active = true;
        renderWorkspace(true);
        toast("Review and approve the detected mask to continue.", "warning");
        return;
      }
    }
    if (!map.steps?.["5"]) {
      const missing = ANALYSIS_BATCH.filter((step) => !map.steps?.[step]);
      state.autorun = true;
      await startJob(missing);
      return;
    }
    if (!map.step5_review_ready) {
      await refreshSelectedData();
      renderWorkspace();
      toast("Review the suggested tactile categories to continue.", "warning");
      return;
    }
    if (!map.steps?.["6"]) {
      state.autorun = true;
      await startJob(SIMPLIFICATION_BATCH);
      return;
    }
    // Existing projects that already reached Step 7 remain compatible. New
    // runs require an explicit level choice before patterns consume Step 6.
    if (!map.steps?.["7"] && !map.step6_review_ready) {
      await refreshSelectedData();
      state.autorun = true;
      state.activeStep = 6;
      renderWorkspace(true);
      toast("Choose a simplification level to continue.", "warning");
      return;
    }
    if (!map.steps?.["7"]) {
      state.autorun = true;
      await startJob(PATTERN_BATCH);
      return;
    }
    if (!map.steps?.["8"] && !map.step7_review_ready) {
      await refreshSelectedData();
      state.autorun = true;
      state.activeStep = 7;
      renderWorkspace(true);
      toast("Review and approve the tactile patterns to continue.", "warning");
      return;
    }
    if (!map.steps?.["8"]) {
      state.autorun = true;
      await startJob(LABEL_BATCH);
      return;
    }
    if (!map.steps?.["9"] && !map.step8_review_ready) {
      await refreshSelectedData();
      state.autorun = true;
      state.activeStep = 8;
      renderWorkspace(true);
      toast("Review and approve the Braille labels and page layout to continue.", "warning");
      return;
    }
    const missing = FINAL_BATCH.filter((step) => !map.steps?.[step]);
    if (missing.length) {
      state.autorun = true;
      await startJob(missing);
    } else {
      state.autorun = !map.step9_review_ready;
      state.activeStep = 9;
      await refreshSelectedData();
      renderWorkspace(true);
      if (!map.step9_review_ready) {
        toast("Review and approve the legend page to enable export.", "warning");
      }
    }
  } finally {
    state.continuing = false;
  }
}

/** Run exactly one step, as the detailed page's per-card button does. */
async function runSingleStep(stepKey, button) {
  await withBusy(button, "Starting…", async () => {
    state.autorun = false;
    setActiveStep(stepKey);
    await startJob([String(stepKey)]);
  });
}

async function rerunStep1() {
  await withBusy($("rerun-step1"), "Re-reading…", async () => {
    state.autorun = false;
    await startJob(["1"]);
  });
}

/** Every decision panel has the same post-approval behavior: Run all resumes
 * automatically, while an individual run exposes its next numbered button. */
async function continueAfterApproval() {
  if (!state.individualRun) {
    state.autorun = true;
    await continuePipeline();
    return;
  }
  state.autorun = false;
  state.viewStep = null;
  renderWorkspace(true);
}

async function continueAfterLegendApproval() {
  state.autorun = false;
  state.viewStep = null;
  renderWorkspace(true);
}

function bindControlEvents() {
  document.querySelectorAll("[data-individual-step]").forEach((dot) => {
    dot.addEventListener("click", () => {
      state.viewStep = dot.dataset.individualStep;
      setActiveStep(dot.dataset.individualStep, true);
      renderControls();
    });
  });
  document.querySelectorAll("[data-step-view]").forEach((dot) => {
    dot.addEventListener("click", () => {
      // Clicking the step the run is already on hands the panel back to the run.
      const key = dot.dataset.stepView;
      state.viewStep = state.viewStep === key ? null : key;
      renderControls();
    });
  });
  document.querySelectorAll(".step-section").forEach((details) => {
    details.addEventListener("toggle", () => {
      if (!details.open) return;
      // One step open at a time, so the left pane is never ambiguous.
      document.querySelectorAll(".step-section").forEach((other) => {
        if (other !== details) other.open = false;
      });
      state.viewStep = details.dataset.step;
      setActiveStep(details.dataset.step);
    });
  });
  document.querySelectorAll("[data-run-step]").forEach((button) => {
    button.addEventListener("click", () => runSingleStep(button.dataset.runStep, button));
  });
  $("show-step-controls")?.addEventListener("click", showIndividualSteps);
  $("model-select")?.addEventListener("change", (event) => { state.model = event.target.value; });
  $("run-all")?.addEventListener("click", runAll);
  $("continue-run")?.addEventListener("click", resumeRun);
  $("continue-pipeline")?.addEventListener("click", continuePipeline);
  $("retry-failed")?.addEventListener("click", retryFailedJob);
  $("rerun-step1")?.addEventListener("click", rerunStep1);
  $("reset-map")?.addEventListener("click", resetMap);
  bindPageSetup();
  bindAggregationEditor(continueAfterApproval);
  bindMaskEditor(continueAfterApproval);
  bindTextEditor();
  bindLineEditor();
  bindSimplificationEditor(continueAfterApproval);
  bindPatternEditor(continueAfterApproval);
  bindBrailleEditor(continueAfterApproval);
  bindLegendEditor(continueAfterLegendApproval);
  bindExportEditor(() => setActiveStep(9, true));
}

