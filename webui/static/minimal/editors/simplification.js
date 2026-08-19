"use strict";

import { $, activateStep6Preset, artifactUrl, esc, resetFrom, saveStep6Params } from "../api.js";
import { DETAIL_NAMES, LINE_KINDS } from "../steps.js";
import { state, toast, withBusy } from "../state.js";
import { editorDetails } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace, startJob } from "../workspace.js";
import { simplifiedArtifactName } from "../visual.js";

/* Step 6 pre-builds five levels of detail and caches each one, so the slider
   is a preview rather than a re-run.  Applying a level swaps the active
   artifacts; rebuilding is only needed when the advanced inputs change. */

function simplificationControlsHtml(decision = false) {
  const presets = state.data.presets;
  const paramsData = state.data.step6 || {};
  const params = paramsData.params || {};
  const level = Number(state.previewLevel) || Number(presets?.active_level) || 3;
  const variant = presets?.variants?.[String(level)];
  const summary = variant?.summary || {};
  const classes = paramsData.classes || [];
  const kept = new Set(params.keep_line_kinds || []);
  const protectedClasses = new Set((params.protected_classes || []).map(Number));
  const body = presets ? `
    <div class="preset-hero">
      <span class="range-heading"><span class="field-label">Simplification preview</span>
        <output class="range-value" id="preset-value">${DETAIL_NAMES[level]}</output></span>
      <input id="preset-slider" type="range" min="1" max="5" value="${level}" step="1"
             aria-label="Simplification preview level">
      <div class="preset-labels"><span>More detail</span><span>More space</span></div>
      <div class="preset-stats">
        <div class="preset-stat"><strong id="stat-polygons">${variant?.polygons ?? "—"}</strong><span>regions</span></div>
        <div class="preset-stat"><strong id="stat-merged">${summary.dissolved_components ?? "—"}</strong><span>small regions merged</span></div>
        <div class="preset-stat"><strong id="stat-islands">${summary.islands?.dropped ?? "—"}</strong><span>tiny islands removed</span></div>
      </div>
      <div class="action-row"><span class="status-copy">Previews and generated tactile results are cached per level during this session.</span>
        <button class="button primary small" id="apply-preset" type="button">${decision
          ? "Use this level &amp; continue" : "Use this level"}</button></div>
    </div>
    <details class="advanced-box">
      <summary>Advanced simplification controls</summary>
      <div class="form-grid">
        <div class="field range-field"><span>Keep linework</span><div class="choice-row">
          ${LINE_KINDS.map((kind) => `<label class="check-chip"><input type="checkbox" name="advanced-line"
            value="${kind.id}" ${kept.has(kind.id) ? "checked" : ""}><span>${kind.label}</span></label>`).join("")}
        </div></div>
        ${classes.length ? `<div class="field range-field"><span>Protect categories from merging</span><div class="choice-row">
          ${classes.map((item) => `<label class="check-chip"><input type="checkbox" name="protected-class"
            value="${Number(item.index)}" ${protectedClasses.has(Number(item.index)) ? "checked" : ""}>
            <span>${esc(item.label)}</span></label>`).join("")}
        </div></div>` : ""}
      </div>
      <div class="action-row end"><button class="button secondary small" id="rebuild-simplification" type="button">Rebuild five levels</button></div>
    </details>`
    : '<div class="empty-editor">Step 6 previews are not available yet.</div>';
  return body;
}

export function simplificationEditorHtml() {
  return editorDetails("simplification", "5", "Simplification",
                       "Detail, lines, and protected categories",
                       simplificationControlsHtml());
}

export function simplificationDecisionHtml() {
  return `
    <section class="review-gate simplification-decision" id="simplification-decision"
             aria-labelledby="simplification-decision-title">
      <span class="section-kicker">One decision needed</span>
      <h3 id="simplification-decision-title">Choose the simplification level.</h3>
      <p class="section-intro">Move the slider to compare the five prepared versions in the
        middle panel. Choose the amount of geographic detail that remains clear to touch,
        then continue to tactile patterns.</p>
      ${simplificationControlsHtml(true)}
    </section>`;
}

export function bindSimplificationEditor(onApproved) {
  $("preset-slider")?.addEventListener("input", (event) => {
    const level = Number(event.target.value);
    state.previewLevel = level;
    $("preset-value").textContent = DETAIL_NAMES[level];
    updatePresetStats(level);
    const image = $("simplified-image");
    if (image) {
      image.dataset.overlayDisabled = "";
      const source = artifactUrl(state.selected, simplifiedArtifactName());
      image.src = source;
      const fullSize = image.closest(".map-stage")?.querySelector("[data-full-size]");
      if (fullSize) fullSize.href = source;
    }
  });
  $("apply-preset")?.addEventListener("click", () => applyPreset(onApproved));
  $("rebuild-simplification")?.addEventListener("click", rebuildSimplification);
}

function updatePresetStats(level) {
  const variant = state.data.presets?.variants?.[String(level)];
  if (!variant) return;
  $("stat-polygons").textContent = variant.polygons ?? "—";
  $("stat-merged").textContent = variant.summary?.dissolved_components ?? "—";
  $("stat-islands").textContent = variant.summary?.islands?.dropped ?? "—";
}

async function applyPreset(onApproved) {
  const level = Number($("preset-slider")?.value || state.previewLevel || 3);
  const decisionGate = Boolean($("simplification-decision"));
  await withBusy($("apply-preset"), "Applying…", async () => {
    const result = await activateStep6Preset(state.selected, level);
    await loadMaps();
    await refreshSelectedData();
    state.previewLevel = level;
    state.activeStep = 6;
    renderWorkspace(true);
    if (decisionGate) {
      toast(state.individualRun
        ? `Level set to ${DETAIL_NAMES[level]}.`
        : `Level set to ${DETAIL_NAMES[level]}. Continuing the run.`);
      await onApproved?.();
    } else {
      toast(result.invalidated?.length
        ? `Level set to ${DETAIL_NAMES[level]}. Later steps were cleared; continue to rebuild them.`
        : `Level set to ${DETAIL_NAMES[level]}.`,
      result.invalidated?.length ? "warning" : "");
    }
  });
}

/** The advanced inputs change how the five levels are built, so they are saved
 *  and Step 6 is run again from scratch. */
async function rebuildSimplification() {
  const payload = {
    simplification_level: Number(state.previewLevel) || 3,
    keep_line_kinds: [...document.querySelectorAll('input[name="advanced-line"]:checked')].map((input) => input.value),
    protected_classes: [...document.querySelectorAll('input[name="protected-class"]:checked')].map((input) => Number(input.value)),
  };
  await withBusy($("rebuild-simplification"), "Starting…", async () => {
    await saveStep6Params(state.selected, payload);
    await resetFrom(state.selected, 6);
    state.autorun = false;
    await startJob(["6"]);
    toast("Rebuilding the five simplification previews.");
  });
}
