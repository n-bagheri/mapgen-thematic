"use strict";

import { $, artifactUrl, esc, mapUrl, resetFrom, saveSpecText } from "./api.js";
import {
  FINAL_BATCH, FIRST_BATCH, PAGE_SIZES, STEP_DEFS,
  allDone, blockingReason, completedCount, pageSizeKey,
} from "./steps.js";
import { currentStepKey, isRunning, selectedMap, state, statusFor, toast, withBusy } from "./state.js";
import {
  loadMaps, refreshSelectedData, renderWorkspace, savePreflight, startJob,
} from "./workspace.js";
import { aggregationEditorHtml, aggregationGateHtml,
         bindAggregationEditor } from "./editors/aggregation.js";
import { bindMaskEditor, maskEditorHtml } from "./editors/mask.js";
import { bindTextEditor, textEditorHtml } from "./editors/text.js";
import { bindLineEditor, lineEditorHtml } from "./editors/lines.js";
import { bindSimplificationEditor, simplificationEditorHtml } from "./editors/simplification.js";
import { bindPatternEditor, patternEditorHtml } from "./editors/patterns.js";
import { bindPageEditor, pageEditorHtml } from "./editors/page.js";
import { bindBrailleEditor, brailleEditorHtml } from "./editors/braille.js";
import { bindLegendEditor, legendEditorHtml } from "./editors/legend.js";
import { bindExportEditor, exportEditorHtml } from "./editors/exportpdf.js";
import { activeStep, renderVisual, setActiveStep } from "./visual.js";

/* The control pane shows exactly one body at a time — a run, a decision, the
   setup, or the editors — with the progress summary above and the reset row
   below.  Which one is decided here and nowhere else. */

export function renderControls() {
  const map = selectedMap();
  if (!map) return;
  const failed = state.job.status === "failed";
  const live = state.job.status === "running" || isRunning(map);
  const blocked = blockingReason(map);
  // A paused part-way run shows the same step panel as a live one, so the
  // results already produced stay readable while the run is stopped.
  const paused = !live && !failed && !map.steps?.["6"] && completedCount(map) > 0;
  const preflight = !live && !failed && !blocked && !paused
    && !map.steps?.["6"] && !map.steps?.["5"] && !state.individualRun;
  document.body.classList.toggle("preflight-layout", preflight);
  let body = "";
  if (live) {
    body = stepPanelHtml(map);
  } else if (blocked) {
    body = scopeBlockHtml(map, blocked);
  } else if (map.steps?.["5"] && !map.step5_review_ready) {
    body = aggregationGateHtml() + stepStackHtml(map);
  } else if (!map.steps?.["6"]) {
    body = paused
      ? setupHtml(false) + stepPanelHtml(map) + stepStackHtml(map)
      : state.individualRun ? individualRunHtml(map) : setupHtml(true, true);
  } else {
    body = resultActionHtml(map) + stepStackHtml(map);
  }
  if (failed) body = stepPanelHtml(map) + failurePanelHtml(map);
  $("control-content").innerHTML = `
    <div class="control-shell${preflight ? " is-preflight" : ""}">
      <header class="control-header">
        <div><h2>${esc(map.name)}</h2></div>
      </header>
      ${live || failed || paused || blocked ? "" : progressHtml(map)}
      ${body}
      ${resetRowHtml(map, live)}
    </div>`;
  bindControlEvents();
}

/** An editor is a block inside its step, not a section of its own: the step
 *  list is the only numbering the reader sees. */
export function editorDetails(id, number, title, subtitle, body) {
  return `<section class="step-editor" data-editor="${id}">
    <h4>${title}<small>${subtitle}</small></h4>
    ${body}
  </section>`;
}

/* Which editors belong to which step.  Everything the focused view can change
   is reached by opening the step that owns it. */
const STEP_EDITORS = {
  2: [maskEditorHtml],
  3: [textEditorHtml],
  4: [lineEditorHtml],
  5: [aggregationEditorHtml],
  6: [simplificationEditorHtml],
  7: [patternEditorHtml, pageEditorHtml],
  8: [brailleEditorHtml],
  9: [legendEditorHtml],
};

/** The right pane mirrors the detailed page: one row per pipeline step, in
 *  order, carrying that step's status, its controls, and its rerun button.
 *  Opening a row is what points the left pane at that step's pictures. */
function stepStackHtml(map) {
  const open = activeStep(map);
  const rows = STEP_DEFS.map((step) => {
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
  const exportPanel = map.steps?.["8"] && map.steps?.["9"] ? exportEditorHtml(map) : "";
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
      <span>${live
        ? "A step cannot be interrupted; the run stops on its own if a step fails."
        : "Deletes every result for this map and returns to the run setup."}</span>
    </div>`;
}

async function resetMap() {
  const map = selectedMap();
  if (!map) return;
  if (!window.confirm(`Delete every result for ${map.name} and return to the run setup?`)) return;
  await withBusy($("reset-map"), "Resetting…", async () => {
    await resetFrom(state.selected, 1);
    state.job = { status: "idle" };
    state.viewStep = null;
    state.activeStep = 1;
    state.individualRun = false;
    state.previewLevel = null;
    state.maskBrush.strokes = [];
    state.maskBrush.active = false;
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
    toast("Map reset. The run setup is ready again.");
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

function modelOptions() {
  if (!state.models.length) return '<option value="">Default pipeline model</option>';
  return state.models.map((model) => `<option value="${esc(model.id)}"
    ${model.id === (state.model || state.defaultModel) ? "selected" : ""}>${esc(model.label)}</option>`).join("");
}

function setupHtml(showRun = true, showIndividual = false) {
  const spec = state.spec;
  const size = state.customPage ? "custom" : pageSizeKey(spec);
  const orientation = spec?.orientation || "auto";
  const option = (value, current, label) =>
    `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`;
  return `
    <section class="setup-card featured">
      <h3>Run setup</h3>
      <div class="form-grid" id="preflight-form">
        <label class="field">
          <span>Model</span>
          <select id="model-select">${modelOptions()}</select>
        </label>
        <label class="field">
          <span>Page size</span>
          <select id="page-size" ${spec ? "" : "disabled"}>
            ${option("a4", size, PAGE_SIZE_LABELS.a4)}
            ${option("a3", size, PAGE_SIZE_LABELS.a3)}
            ${option("custom", size, PAGE_SIZE_LABELS.custom)}
          </select>
          <small class="field-note">Shared by every map.</small>
        </label>
        <label class="field">
          <span>Orientation</span>
          <select id="page-orientation" ${spec ? "" : "disabled"}>
            ${option("auto", orientation, "Fit the sheet")}
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
      </div>
      ${showRun ? `<div class="action-row end${showIndividual ? " preflight-actions" : ""}">
        ${showIndividual ? `<button class="button primary" id="show-step-controls" type="button">
          Run each step individually</button>` : ""}
        <button class="button primary run-button" id="run-all" type="button">Run all</button>
      </div>` : ""}
    </section>`;
}

function individualRunHtml(map) {
  return `
    <section class="individual-run-heading">
      <div><span class="section-kicker">Individual run</span>
        <h3>Choose the step you want to run.</h3></div>
      <button class="button subtle small" id="show-run-setup" type="button">Back to run setup</button>
    </section>
    ${stepStackHtml(map)}`;
}

/** Re-rendering mid-edit would replace the inputs the user is still typing in,
 *  so only a change of page-size mode asks for one. */
async function saveSpec(patch, rerender = false) {
  if (!state.spec) {
    toast("The output spec could not be read; reload the page.", "error");
    return;
  }
  const next = { ...state.spec, ...patch };
  try {
    await saveSpecText(next);
    state.spec = next;
    if (rerender) renderControls();
    toast("Page setup saved.");
  } catch (error) {
    toast(error.message, "error");
    renderControls();  // put the rejected field back to the stored value
  }
}

function bindPageSetup() {
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
  const saveCustom = () => saveSpec({
    page_width_mm: Number($("page-width").value),
    page_height_mm: Number($("page-height").value),
  });
  $("page-width")?.addEventListener("change", saveCustom);
  $("page-height")?.addEventListener("change", saveCustom);
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

function stepImageSrc(map, step) {
  if (step.key === "1") return mapUrl(map.name);
  return step.preview ? artifactUrl(map.stem, step.preview) : "";
}

function stepPanelHtml(map) {
  const running = STEP_DEFS.find((step) => step.key === currentStepKey(map));
  const step = viewedStep(map);
  const status = stepState(map, step);
  const ready = status === "done" || (step.key === "1" && status !== "");
  const source = ready ? stepImageSrc(map, step) : "";
  const dots = STEP_DEFS.map((item) => {
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
  // Paused runs keep the same panel, so the results stay readable between
  // steps; the button picks the run back up from the first missing result.
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
      <figure class="step-figure${source ? "" : " is-waiting"}">
        ${source ? `<img src="${source}" alt="Result of ${esc(step.title)}">` : ""}
        <figcaption>${esc(source ? step.caption : `${step.title} — result appears here as soon as this step finishes.`)}</figcaption>
      </figure>
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
  const batch = map.steps?.["5"] ? FINAL_BATCH : FIRST_BATCH;
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
    state.autorun = true;
    const map = selectedMap();
    const missing = FIRST_BATCH.filter((step) => !map.steps?.[step]);
    if (missing.length) await startJob(missing);
    else await continuePipeline();
  });
}

function showIndividualSteps() {
  state.individualRun = true;
  state.activeStep = 1;
  renderControls();
  renderVisual();
  $("control-pane")?.scrollTo({ top: 0 });
}

function showRunSetup() {
  state.individualRun = false;
  renderControls();
  renderVisual();
  $("control-pane")?.scrollTo({ top: 0 });
}

/** Resume a paused run, saving the settings still on screen first. */
async function resumeRun() {
  await withBusy($("continue-run"), "Starting…", async () => {
    if ($("model-select")) await savePreflight();
    await continuePipeline();
  });
}

/** The run is in two halves with the Step 5 category review between them. */
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
    if (!map.steps?.["5"]) {
      const missing = FIRST_BATCH.filter((step) => !map.steps?.[step]);
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
    const missing = FINAL_BATCH.filter((step) => !map.steps?.[step]);
    if (missing.length) {
      state.autorun = true;
      await startJob(missing);
    } else {
      state.autorun = false;
      state.activeStep = 9;
      await refreshSelectedData();
      renderWorkspace(true);
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

function bindControlEvents() {
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
      setActiveStep(details.dataset.step);
    });
  });
  document.querySelectorAll("[data-run-step]").forEach((button) => {
    button.addEventListener("click", () => runSingleStep(button.dataset.runStep, button));
  });
  $("show-step-controls")?.addEventListener("click", showIndividualSteps);
  $("show-run-setup")?.addEventListener("click", showRunSetup);
  $("model-select")?.addEventListener("change", (event) => { state.model = event.target.value; });
  $("run-all")?.addEventListener("click", runAll);
  $("continue-run")?.addEventListener("click", resumeRun);
  $("continue-pipeline")?.addEventListener("click", continuePipeline);
  $("retry-failed")?.addEventListener("click", retryFailedJob);
  $("rerun-step1")?.addEventListener("click", rerunStep1);
  $("reset-map")?.addEventListener("click", resetMap);
  bindPageSetup();
  bindAggregationEditor();
  bindMaskEditor();
  bindTextEditor();
  bindLineEditor();
  bindSimplificationEditor();
  bindPatternEditor();
  bindPageEditor();
  bindBrailleEditor();
  bindLegendEditor();
  bindExportEditor();
}

