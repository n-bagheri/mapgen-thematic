"use strict";

import {
  $, assignPattern, esc, patternLibraryPreviewUrl, patternPreviewUrl,
  saveCategoryColors, savePatternTransform, saveStep7Review,
} from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { renderControls } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import { refreshStepImages, renderVisual } from "../visual.js";

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
        <img src="${patternPreviewUrl(state.selected, group.group_id)}" alt="">
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
  const used = new Set((state.data.patterns?.groups || []).map((item) => item.pattern));
  const choices = (state.data.patterns?.library || []).map((item) => `
    <button class="pattern-library-choice${item.pattern === group.pattern ? " is-selected" : ""}${
      used.has(item.pattern) ? " is-used" : ""}" type="button"
      data-choose-pattern="${esc(item.pattern)}" title="${esc(item.pattern_desc)}">
      <img src="${patternLibraryPreviewUrl(item.pattern)}" alt="">
      <span>${esc(item.pattern_desc)}</span>
    </button>`).join("");
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
        <img src="${patternPreviewUrl(state.selected, group.group_id)}" alt="">
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
  document.querySelectorAll("[data-edit-pattern]").forEach((button) => {
    button.addEventListener("click", () => openPatternDialog("edit", button.dataset.editPattern));
  });
  document.querySelectorAll("[data-change-pattern]").forEach((button) => {
    button.addEventListener("click", () => openPatternDialog("change", button.dataset.changePattern));
  });
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
  document.querySelectorAll("[data-pattern-colour]").forEach((picker) => {
    picker.addEventListener("change", () => saveColours(picker));
  });
  $("approve-patterns")?.addEventListener("click", () => approveAndContinue(continuePipeline));
}

function openPatternDialog(kind, groupId) {
  state.patternGroup = Number(groupId);
  state.patternDialog = { kind, groupId: Number(groupId) };
  renderControls();
}

function closePatternDialog() {
  state.patternDialog = null;
  renderControls();
}

async function updateReviewMode(button, patch) {
  await withBusy(button, "Saving…", async () => {
    state.data.step7Review = await saveStep7Review(state.selected, patch);
    if (!state.data.step7Review.create_hybrid_map) {
      state.colourView = false;
    }
    renderVisual();
    renderControls();
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
    await refreshAfterRender();
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
    await refreshAfterRender();
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
    await refreshAfterRender();
  });
}

async function saveColours(changedPicker) {
  const colors = {};
  (state.data.patterns?.groups || []).forEach((group) => {
    const picker = document.querySelector(`[data-pattern-colour="${group.group_id}"]`);
    colors[group.label] = (picker?.value || state.data.colors?.colors?.[group.label]
      || "#FFFFFF").toUpperCase();
  });
  changedPicker.disabled = true;
  statusLine("pattern-review-status", "Saving hybrid colours…");
  try {
    state.data.colors = await saveCategoryColors(state.selected, colors);
    await refreshAfterRender();
    statusLine("pattern-review-status", "Hybrid colour saved.", "success");
  } catch (error) {
    changedPicker.disabled = false;
    statusLine("pattern-review-status", error.message, "error");
    toast(error.message, "error");
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

async function refreshAfterRender() {
  await loadMaps();
  await refreshStepImages();
  await refreshSelectedData();
  renderControls();
}
