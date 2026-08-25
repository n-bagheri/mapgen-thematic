"use strict";

import {
  $, assignPattern, esc, patternLibraryPreviewUrl, patternPreviewUrl,
  saveCategoryColors, savePatternTransform, saveStep7Review,
} from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import { clearPreviewCache, preloadPreviews } from "../preview-cache.js";
import { refreshStepImages } from "../visual.js";

const TRANSFORM_FIELDS = [
  { key: "scale_x_percent", label: "Horizontal scale", step: 1, unit: "%" },
  { key: "scale_y_percent", label: "Vertical scale", step: 1, unit: "%" },
  { key: "move_x_mm", label: "Horizontal move", step: .5, unit: " mm" },
  { key: "move_y_mm", label: "Vertical move", step: .5, unit: " mm" },
  { key: "rotate_deg", label: "Angle", step: 1, unit: "°" },
];

function reviewState() {
  return state.data.step7Review || {
    approved: false,
    preserve_haptic_distances: true,
    create_hybrid_map: false,
  };
}

function groupById(groupId) {
  return (state.data.patterns?.groups || []).find(
    (group) => Number(group.group_id) === Number(groupId)) || null;
}

function currentDialogGroup() {
  return groupById(state.patternDialog?.groupId);
}

function modeButton(id, pressed, label, help) {
  return `<button class="pattern-mode-button" id="${id}" type="button"
      aria-pressed="${pressed}">
      <span class="mode-switch" aria-hidden="true"></span>
      <span><strong>${label}</strong><small>${help}</small></span>
    </button>`;
}

function patternRowHtml(group) {
  const review = reviewState();
  const colours = state.data.colors?.colors || {};
  const colour = colours[group.label] || "#FFFFFF";
  return `<article class="pattern-decision-row" data-pattern-row="${group.group_id}">
    <img class="pattern-swatch" src="${patternPreviewUrl(state.selected, group.group_id)}"
         data-pattern-preview="${group.group_id}"
         alt="${esc(group.pattern_desc)} pattern">
    <div class="pattern-row-copy">
      <strong>${esc(group.label)}</strong>
      <small>${esc(group.pattern_desc)} · ${esc(group.pattern_family || "texture")}</small>
    </div>
    <div class="pattern-row-actions">
      <button class="button subtle small" type="button" data-edit-pattern="${group.group_id}"
        ${group.editable ? "" : "disabled"}>Edit</button>
      <button class="button subtle small" type="button"
        data-change-pattern="${group.group_id}">Change</button>
      <label class="pattern-colour" title="${review.create_hybrid_map
          ? `Choose a colour for ${esc(group.label)}` : "Turn on Create hybrid map to add colour"}">
        <span class="visually-hidden">Colour for ${esc(group.label)}</span>
        <input type="color" value="${esc(colour)}" data-pattern-colour="${group.group_id}"
          ${review.create_hybrid_map ? "" : "disabled"}>
      </label>
    </div>
  </article>`;
}

function transformFieldHtml(group, field) {
  const limits = state.data.patterns?.limits?.[field.key] || {};
  const value = Number(group.transform?.[field.key]
    ?? state.data.patterns?.defaults?.[field.key] ?? 0);
  return `<label class="transform-field">
    <span>${field.label}<output id="transform-${field.key}-value">${value}${field.unit}</output></span>
    <input type="range" name="pattern-transform" data-key="${field.key}"
      min="${limits.min ?? 0}" max="${limits.max ?? 100}" step="${field.step}" value="${value}">
  </label>`;
}

function mountedPatternPreviewUrl(groupId) {
  const mounted = document.querySelector(
    `.pattern-decision-list [data-pattern-preview="${Number(groupId)}"]`);
  return mounted?.currentSrc || mounted?.src || patternPreviewUrl(state.selected, groupId);
}

function editDialogHtml(group) {
  if (!group) return "";
  return `<div class="pattern-dialog-backdrop" data-close-pattern-dialog></div>
    <section class="pattern-dialog" role="dialog" aria-modal="true"
      aria-labelledby="pattern-dialog-title">
      <header><div><h3 id="pattern-dialog-title">Edit pattern — ${esc(group.label)}</h3>
        <small>${esc(group.pattern)} · ${esc(group.pattern_desc)}</small></div>
        <button class="pattern-dialog-close" type="button" data-close-pattern-dialog
          aria-label="Close pattern editor">×</button></header>
      <div class="pattern-dialog-preview">
        <img src="${esc(mountedPatternPreviewUrl(group.group_id))}"
          data-pattern-preview="${group.group_id}" alt="">
      </div>
      ${group.editable ? `<div class="transform-grid">
        ${TRANSFORM_FIELDS.map((field) => transformFieldHtml(group, field)).join("")}
      </div>
      <div class="action-row end"><span class="status-copy" id="transform-status"></span>
        <button class="button subtle small" id="transform-reset" type="button">Reset transform</button>
        <button class="button primary small" data-close-pattern-dialog type="button">Done</button></div>`
        : '<p class="section-intro">Plain and solid fills have no repeating texture to transform.</p>'}
    </section>`;
}

function changeDialogHtml(group) {
  if (!group) return "";
  const preserve = reviewState().preserve_haptic_distances;
  const hasWater = (state.data.patterns?.groups || []).some((item) => item.is_water);
  const used = new Set((state.data.patterns?.groups || []).map((item) => item.pattern));
  const choices = (state.data.patterns?.library || []).map((item) => {
    const waterConflict = preserve && (
      group.is_water ? item.pattern !== "04_waves_sine"
        : item.water_only || hasWater && item.pattern_family === "waves"
    );
    const title = waterConflict
      ? (group.is_water ? "Water uses the sinusoidal wave while haptic preservation is on"
        : "The sinusoidal wave is reserved for water while haptic preservation is on")
      : item.pattern_desc;
    return `
    <button class="pattern-library-choice${item.pattern === group.pattern ? " is-selected" : ""}${
      used.has(item.pattern) ? " is-used" : ""}" type="button"
      data-choose-pattern="${esc(item.pattern)}" title="${esc(title)}"
      ${waterConflict ? "disabled" : ""}>
      <img src="${patternLibraryPreviewUrl(item.pattern)}" alt="">
      <span>${esc(item.pattern_desc)}</span>
    </button>`;
  }).join("");
  return `<div class="pattern-dialog-backdrop" data-close-pattern-dialog></div>
    <section class="pattern-dialog" role="dialog" aria-modal="true"
      aria-labelledby="pattern-dialog-title">
      <header><div><h3 id="pattern-dialog-title">Change pattern — ${esc(group.label)}</h3>
        <small>${preserve
          ? "Other assignments will be recalculated to preserve haptic distance."
          : "Only this category will change."}</small></div>
        <button class="pattern-dialog-close" type="button" data-close-pattern-dialog
          aria-label="Close pattern library">×</button></header>
      <div class="pattern-dialog-preview">
        <img src="${esc(mountedPatternPreviewUrl(group.group_id))}"
          data-pattern-preview="${group.group_id}" alt="">
      </div>
      <div class="pattern-library-grid" role="listbox" aria-label="Tactile pattern library">
        ${choices}
      </div>
      <div class="action-row end"><span class="status-copy" id="pattern-save-status"></span>
        <button class="button secondary small" data-close-pattern-dialog type="button">Close</button></div>
    </section>`;
}

function patternDialogHtml() {
  const group = currentDialogGroup();
  if (!group) return "";
  return state.patternDialog?.kind === "edit" ? editDialogHtml(group) : changeDialogHtml(group);
}

function patternDecisionBody(showApproval) {
  const groups = state.data.patterns?.groups || [];
  if (!groups.length) return '<div class="empty-editor">Finish Step 7 to review its patterns.</div>';
  const review = reviewState();
  return `
    <div class="pattern-mode-grid">
      ${modeButton("preserve-haptic-distances", review.preserve_haptic_distances,
        "Preserve haptic distances", "Reassign the other patterns when one changes")}
      ${modeButton("create-hybrid-map", review.create_hybrid_map,
        "Create hybrid map", "Enable a printable colour for every category")}
    </div>
    <p class="section-intro">Review every pattern used in the tactile master. Edit changes its
      scale or position; Change selects a different texture.</p>
    <div class="pattern-decision-list">${groups.map(patternRowHtml).join("")}</div>
    <p class="status-copy pattern-review-status" id="pattern-review-status"></p>
    ${showApproval ? `<button class="button primary full" id="approve-patterns" type="button">
      Approve patterns &amp; continue</button>` : ""}
    ${patternDialogHtml()}`;
}

export function patternDecisionHtml() {
  return `<section class="review-gate pattern-decision" id="pattern-decision">
    <span class="section-kicker">One decision needed</span>
    <h3>Review the tactile patterns.</h3>
    ${patternDecisionBody(true)}
  </section>`;
}

export function bindPatternEditor(continuePipeline = null) {
  $("preserve-haptic-distances")?.addEventListener("click", (event) => {
    const value = event.currentTarget.getAttribute("aria-pressed") !== "true";
    updateReviewMode(event.currentTarget, { preserve_haptic_distances: value });
  });
  $("create-hybrid-map")?.addEventListener("click", (event) => {
    const value = event.currentTarget.getAttribute("aria-pressed") !== "true";
    updateReviewMode(event.currentTarget, { create_hybrid_map: value });
  });
  bindPatternRowControls();
  bindPatternDialogControls();
  $("approve-patterns")?.addEventListener("click", () => approveAndContinue(continuePipeline));
}

function bindPatternRowControls() {
  document.querySelectorAll("[data-edit-pattern]").forEach((button) => {
    button.addEventListener("click", () => openPatternDialog("edit", button.dataset.editPattern));
  });
  document.querySelectorAll("[data-change-pattern]").forEach((button) => {
    button.addEventListener("click", () => openPatternDialog("change", button.dataset.changePattern));
  });
  document.querySelectorAll("[data-pattern-colour]").forEach((picker) => {
    picker.addEventListener("change", saveColours);
  });
}

function bindPatternDialogControls() {
  document.querySelectorAll("[data-close-pattern-dialog]").forEach((button) => {
    button.addEventListener("click", closePatternDialog);
  });
  document.querySelectorAll("[data-choose-pattern]").forEach((button) => {
    button.addEventListener("click", () => choosePattern(button));
  });
  document.querySelectorAll('input[name="pattern-transform"]').forEach((slider) => {
    slider.addEventListener("input", () => {
      const field = TRANSFORM_FIELDS.find((item) => item.key === slider.dataset.key);
      const output = $(`transform-${slider.dataset.key}-value`);
      if (output) output.textContent = `${slider.value}${field?.unit || ""}`;
    });
    slider.addEventListener("change", saveTransform);
  });
  $("transform-reset")?.addEventListener("click", resetTransform);
}

function openPatternDialog(kind, groupId) {
  state.patternGroup = Number(groupId);
  state.patternDialog = { kind, groupId: Number(groupId) };
  removePatternDialog();
  $("pattern-decision")?.insertAdjacentHTML("beforeend", patternDialogHtml());
  bindPatternDialogControls();
}

function closePatternDialog() {
  state.patternDialog = null;
  removePatternDialog();
}

function removePatternDialog() {
  document.querySelectorAll(".pattern-dialog-backdrop, .pattern-dialog").forEach(
    (element) => element.remove());
}

async function updateReviewMode(button, patch) {
  if (state.busy) return;
  state.busy = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  statusLine("pattern-review-status", "Saving pattern settings…");
  try {
    const wasColourView = state.colourView;
    const hybridWasEnabled = reviewState().create_hybrid_map;
    state.data.step7Review = await saveStep7Review(state.selected, patch);
    if (!state.data.step7Review.create_hybrid_map) {
      state.colourView = false;
    }
    if (!hybridWasEnabled && state.data.step7Review.create_hybrid_map) {
      await preloadPreviews(state.selected, ["step8a_hybrid.png"]);
    }
    // These modes affect future assignments or enable colour editing; they do
    // not require rebuilding the currently visible relief map. If hybrid view
    // is being disabled, refreshStepImages retains it until relief is ready.
    if (wasColourView && !state.colourView) await refreshStepImages();
    syncPatternModeControls();
    statusLine("pattern-review-status", "Pattern settings saved.", "success");
  } catch (error) {
    statusLine("pattern-review-status", error.message, "error");
    toast(error.message, "error");
  } finally {
    state.busy = false;
    if (button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

function syncPatternModeControls() {
  const review = reviewState();
  $("preserve-haptic-distances")?.setAttribute(
    "aria-pressed", String(review.preserve_haptic_distances));
  $("create-hybrid-map")?.setAttribute("aria-pressed", String(review.create_hybrid_map));
  document.querySelectorAll("[data-pattern-colour]").forEach((picker) => {
    picker.disabled = !review.create_hybrid_map;
    const label = picker.closest(".pattern-colour");
    const group = groupById(picker.dataset.patternColour);
    if (label) label.title = review.create_hybrid_map
      ? `Choose a colour for ${group?.label || "this category"}`
      : "Turn on Create hybrid map to add colour";
  });
  syncPatternViewerControls();
}

function syncPatternViewerControls() {
  const hybrid = reviewState().create_hybrid_map;
  document.querySelectorAll("[data-colour-view]").forEach((checkbox) => {
    checkbox.disabled = !hybrid;
    checkbox.checked = state.colourView;
    const label = checkbox.closest("label");
    if (label) label.title = hybrid
      ? "Show the saved category colors"
      : "Turn on Create hybrid map in Step 7 first";
  });
}

async function choosePattern(button) {
  const group = currentDialogGroup();
  const pattern = button.dataset.choosePattern;
  if (!group || !pattern) return;
  if (pattern === group.pattern) {
    closePatternDialog();
    return;
  }
  await withBusy(button, "Applying…", async () => {
    const result = await assignPattern(
      state.selected, group.group_id, pattern,
      reviewState().preserve_haptic_distances,
    );
    state.patternDialog = null;
    if (result.pattern_data) state.data.patterns = result.pattern_data;
    if (result.review) state.data.step7Review = result.review;
    removePatternDialog();
    await refreshAfterRender({ patternGroups: "all" });
    toast(reviewState().preserve_haptic_distances
      ? "Pattern applied and haptic distances recalculated."
      : "Pattern applied to this category.");
  });
}

async function saveTransform() {
  const group = currentDialogGroup();
  if (!group) return;
  const transform = {};
  document.querySelectorAll('input[name="pattern-transform"]').forEach((slider) => {
    transform[slider.dataset.key] = Number(slider.value);
  });
  statusLine("transform-status", "Re-rendering…");
  try {
    const result = await savePatternTransform(state.selected, group.group_id, transform);
    group.transform = result.transform;
    state.data.step7Review = { ...reviewState(), approved: false };
    await refreshAfterRender({ patternGroups: [group.group_id] });
    statusLine("transform-status", "Updated.", "success");
  } catch (error) {
    statusLine("transform-status", error.message, "error");
    toast(error.message, "error");
  }
}

async function resetTransform() {
  const group = currentDialogGroup();
  if (!group) return;
  await withBusy($("transform-reset"), "Resetting…", async () => {
    const result = await savePatternTransform(
      state.selected, group.group_id, state.data.patterns?.defaults || {});
    group.transform = result.transform;
    state.data.step7Review = { ...reviewState(), approved: false };
    await refreshAfterRender({ patternGroups: [group.group_id] });
  });
}

let pendingColourSave = null;
let colourSaveTimer = null;
let colourSaveInFlight = false;

function saveColours() {
  const colors = {};
  (state.data.patterns?.groups || []).forEach((group) => {
    const picker = document.querySelector(`[data-pattern-colour="${group.group_id}"]`);
    colors[group.label] = (picker?.value || state.data.colors?.colors?.[group.label]
      || "#FFFFFF").toUpperCase();
  });
  state.data.colors = { colors };
  pendingColourSave = { stem: state.selected, colors };
  window.clearTimeout(colourSaveTimer);
  colourSaveTimer = window.setTimeout(flushColourSave, 180);
  statusLine("pattern-review-status", "Saving hybrid colour…");
}

async function flushColourSave() {
  if (colourSaveInFlight || !pendingColourSave) return;
  const request = pendingColourSave;
  pendingColourSave = null;
  colourSaveInFlight = true;
  if (state.selected === request.stem) {
    statusLine("pattern-review-status", "Saving hybrid colour…");
  }
  try {
    const saved = await saveCategoryColors(request.stem, request.colors);
    // If the reader picked another colour while this request was running,
    // immediately save that newer snapshot. Do not reload an intermediate
    // artifact or announce success while the final colour is still queued.
    if (!pendingColourSave && state.selected === request.stem) {
      state.data.colors = saved;
      state.data.step7Review = { ...reviewState(), approved: false };
      clearPreviewCache(request.stem);
      // Keep both sides of Display colors resident after an edited colour
      // invalidates the old blobs. The later toggle is then a decoded local
      // image swap rather than another request competing with editor images.
      await preloadPreviews(request.stem, ["step8a_cleanup.png", "step8a_hybrid.png"]);
      if (state.colourView) await refreshStepImages();
      statusLine("pattern-review-status", pendingColourSave
        ? "Saving hybrid colour…" : "Hybrid colour saved.",
      pendingColourSave ? "" : "success");
    }
    if (!pendingColourSave) loadMaps().catch(() => {});
  } catch (error) {
    if (state.selected === request.stem) {
      statusLine("pattern-review-status", error.message, "error");
      toast(error.message, "error");
    }
  } finally {
    colourSaveInFlight = false;
    if (pendingColourSave) {
      window.clearTimeout(colourSaveTimer);
      colourSaveTimer = window.setTimeout(flushColourSave, 0);
    }
  }
}

async function approveAndContinue(continuePipeline) {
  await withBusy($("approve-patterns"), "Approving…", async () => {
    state.data.step7Review = await saveStep7Review(state.selected, { approve: true });
    await loadMaps();
    await refreshSelectedData();
    state.patternDialog = null;
    renderWorkspace(true);
    toast("Tactile patterns approved.");
    if (continuePipeline) await continuePipeline();
  });
}

async function refreshAfterRender({ patternGroups = [] } = {}) {
  const stem = state.selected;
  clearPreviewCache(stem);
  const groups = patternGroups === "all"
    ? (state.data.patterns?.groups || []).map((group) => group.group_id)
    : patternGroups;
  await Promise.all([loadMaps(), refreshStepImages(), refreshPatternRows(groups)]);
  syncPatternModeControls();
}

async function refreshPatternRows(groupIds) {
  const groups = state.data.patterns?.groups || [];
  const rows = [...document.querySelectorAll("[data-pattern-row]")];
  if (rows.length !== groups.length) {
    const list = document.querySelector(".pattern-decision-list");
    if (!list) return;
    list.innerHTML = groups.map(patternRowHtml).join("");
    bindPatternRowControls();
  }
  groups.forEach((group) => {
    const row = document.querySelector(`[data-pattern-row="${group.group_id}"]`);
    if (!row) return;
    const name = row.querySelector(".pattern-row-copy strong");
    const description = row.querySelector(".pattern-row-copy small");
    const edit = row.querySelector("[data-edit-pattern]");
    if (name) name.textContent = group.label;
    if (description) {
      description.textContent = `${group.pattern_desc} · ${group.pattern_family || "texture"}`;
    }
    if (edit) edit.disabled = !group.editable;
  });
  await Promise.all(groupIds.map(refreshPatternPreview));
}

async function refreshPatternPreview(groupId) {
  const source = patternPreviewUrl(state.selected, groupId);
  const loaded = await new Promise((resolve) => {
    const image = new Image();
    image.addEventListener("load", () => resolve(true), { once: true });
    image.addEventListener("error", () => resolve(false), { once: true });
    image.src = source;
  });
  if (!loaded) return;
  document.querySelectorAll(`[data-pattern-preview="${groupId}"]`).forEach((image) => {
    image.src = source;
  });
}
