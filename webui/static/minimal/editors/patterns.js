"use strict";

import {
  $, assignPattern, esc, patternLibraryPreviewUrl, patternPreviewUrl,
  saveCategoryColors, savePatternTransform,
} from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails, renderControls } from "../controls.js";
import { loadMaps, refreshSelectedData } from "../workspace.js";
import { refreshStepImages } from "../visual.js";

/* Step 7 assigns one tactile texture per category, then draws the boundaries
   and cleans the sheet.  Every control here saves the moment it is used: the
   server re-renders the master (and the Braille page, when it exists) in the
   same request, so there is no separate apply step to forget. */

const TRANSFORM_FIELDS = [
  { key: "scale_x_percent", label: "Width", step: 1, unit: "%" },
  { key: "scale_y_percent", label: "Height", step: 1, unit: "%" },
  { key: "move_x_mm", label: "Move across", step: .5, unit: "mm" },
  { key: "move_y_mm", label: "Move down", step: .5, unit: "mm" },
  { key: "rotate_deg", label: "Rotate", step: 1, unit: "deg" },
];

function currentGroup() {
  const groups = state.data.patterns?.groups || [];
  return groups.find((group) => Number(group.group_id) === Number(state.patternGroup))
    || groups[0] || null;
}

function transformFieldHtml(group, field) {
  const limits = state.data.patterns?.limits?.[field.key] || {};
  const value = Number(group.transform?.[field.key]
    ?? state.data.patterns?.defaults?.[field.key] ?? 0);
  return `<label class="transform-field">
    <span>${field.label}<output id="transform-${field.key}-value">${value}${field.unit}</output></span>
    <input type="range" name="pattern-transform" data-key="${field.key}"
           min="${limits.min ?? 0}" max="${limits.max ?? 100}" step="${field.step}" value="${value}"
           aria-label="${field.label} of the ${esc(group.label)} texture">
  </label>`;
}

function patternRowHtml(group) {
  const library = state.data.patterns?.library || [];
  const pickerId = `pattern-picker-${group.group_id}`;
  const options = library.map((item) => `<button class="pattern-picker-option${
    item.pattern === group.pattern ? " is-selected" : ""}"
    type="button" role="option" aria-selected="${item.pattern === group.pattern ? "true" : "false"}"
    data-pattern="${esc(item.pattern)}" data-label="${esc(item.pattern_desc)}"
    aria-label="${esc(item.pattern_desc)}" title="${esc(item.pattern_desc)}">
      <img src="${patternLibraryPreviewUrl(item.pattern)}" alt="">
    </button>`).join("");
  return `<div class="pattern-row" data-pattern-row="${group.group_id}">
    <button class="pattern-picker-trigger" type="button" role="combobox"
            aria-expanded="false" aria-controls="${pickerId}"
            aria-label="Choose tactile pattern for ${esc(group.label)}"
            data-group="${group.group_id}" data-pattern="${esc(group.pattern)}">
      <img src="${patternPreviewUrl(state.selected, group.group_id)}" alt="">
    </button>
    <div class="pattern-row-copy">
      <div class="pattern-row-heading"><strong>${esc(group.label)}</strong>
        <span class="pattern-family">${esc(group.pattern_family || "texture")}</span></div>
    </div>
    <div class="pattern-picker-menu" id="${pickerId}" role="listbox"
         aria-label="Patterns for ${esc(group.label)}" hidden>${options}</div>
  </div>`;
}

export function patternEditorHtml() {
  const data = state.data.patterns;
  const groups = data?.groups || [];
  const group = currentGroup();
  const editable = Boolean(group?.editable);
  const body = groups.length ? `
    <p class="section-intro">Click a texture to choose another pattern for that area. Each change
      re-renders the tactile master, its boundaries, and the cleanup pass.</p>
    <div class="pattern-list" id="pattern-list">
      ${groups.map((item) => patternRowHtml(item)).join("")}
    </div>
    <div class="action-row end"><span class="status-copy" id="pattern-save-status"></span></div>
    <section class="transform-box">
      <h4>Adjust the ${esc(group?.label || "selected")} texture</h4>
      <p>${editable
        ? "Scale, move, or rotate the repeat without changing which pattern it is."
        : "Plain and solid fills have no repeat to adjust."}</p>
      <label class="field"><span>Area</span>
        <select id="transform-group" aria-label="Area whose texture is being adjusted">
          ${groups.map((item) => `<option value="${item.group_id}"
            ${Number(item.group_id) === Number(group?.group_id) ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
        </select>
      </label>
      ${editable ? `<div class="transform-grid">
        ${TRANSFORM_FIELDS.map((field) => transformFieldHtml(group, field)).join("")}
      </div>
      <div class="action-row end"><span class="status-copy" id="transform-status"></span>
        <button class="button subtle small" id="transform-reset" type="button">Reset texture</button></div>` : ""}
    </section>
    ${colourSectionHtml(groups)}`
    : '<div class="empty-editor">Finish Step 7 to select tactile patterns.</div>';
  return editorDetails("patterns", "6", "Tactile patterns", "Textures, adjustments, and colours", body);
}

/** Colours only affect the hybrid render; the relief master stays black. */
function colourSectionHtml(groups) {
  const colours = state.data.colors?.colors || {};
  return `<section class="transform-box">
    <h4>Category colours</h4>
    <p>Used by the colour view and the colour PDF. The relief master stays black and white.</p>
    <div class="colour-list">
      ${groups.map((group) => {
        const value = colours[group.label] || "#FFFFFF";
        return `<div class="colour-row" data-colour-label="${esc(group.label)}">
          <input type="color" value="${esc(value)}" aria-label="Colour for ${esc(group.label)}">
          <span>${esc(group.label)}</span>
          <code>${esc(value)}</code>
        </div>`;
      }).join("")}
    </div>
    <div class="action-row end"><span class="status-copy" id="colour-status"></span>
      <button class="button secondary small" id="save-colours" type="button">Save colours</button></div>
  </section>`;
}

export function bindPatternEditor() {
  document.querySelectorAll(".pattern-picker-trigger").forEach((trigger) => {
    trigger.addEventListener("click", () => togglePatternPicker(trigger));
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        openPatternPicker(trigger);
      } else if (event.key === "Escape") {
        closePatternPickers();
      }
    });
  });
  document.querySelectorAll(".pattern-picker-option").forEach((option) => {
    option.addEventListener("click", () => {
      const row = option.closest(".pattern-row");
      const trigger = row?.querySelector(".pattern-picker-trigger");
      if (!trigger || option.dataset.pattern === trigger.dataset.pattern) {
        closePatternPickers();
        return;
      }
      closePatternPickers();
      choosePattern(Number(trigger.dataset.group), option.dataset.pattern);
    });
  });
  $("transform-group")?.addEventListener("change", (event) => {
    state.patternGroup = Number(event.target.value);
    renderControls();
  });
  document.querySelectorAll('input[name="pattern-transform"]').forEach((slider) => {
    slider.addEventListener("input", () => {
      const field = TRANSFORM_FIELDS.find((item) => item.key === slider.dataset.key);
      const output = $(`transform-${slider.dataset.key}-value`);
      if (output) output.textContent = `${slider.value}${field?.unit || ""}`;
    });
    // Saving on change (not input) keeps one re-render per gesture, not one
    // per pixel the slider travels.
    slider.addEventListener("change", saveTransform);
  });
  $("transform-reset")?.addEventListener("click", resetTransform);
  document.querySelectorAll(".colour-row input[type=\"color\"]").forEach((picker) => {
    picker.addEventListener("input", () => {
      const row = picker.closest(".colour-row");
      const code = row?.querySelector("code");
      if (code) code.textContent = picker.value.toUpperCase();
      statusLine("colour-status", "Unsaved changes");
    });
  });
  $("save-colours")?.addEventListener("click", saveColours);
  refreshUsedPatternOverlays();
}

export function closePatternPickers() {
  document.querySelectorAll(".pattern-picker-trigger").forEach((trigger) => {
    trigger.setAttribute("aria-expanded", "false");
  });
  document.querySelectorAll(".pattern-picker-menu").forEach((menu) => { menu.hidden = true; });
}

function openPatternPicker(trigger) {
  const menu = $(trigger.getAttribute("aria-controls"));
  if (!menu) return;
  closePatternPickers();
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
}

function togglePatternPicker(trigger) {
  const menu = $(trigger.getAttribute("aria-controls"));
  if (!menu) return;
  if (menu.hidden) openPatternPicker(trigger);
  else closePatternPickers();
}

/** Mark textures already in use, so two areas are not given the same feel. */
function refreshUsedPatternOverlays() {
  const used = new Set((state.data.patterns?.groups || []).map((group) => group.pattern));
  document.querySelectorAll(".pattern-picker-option").forEach((option) => {
    const isUsed = used.has(option.dataset.pattern);
    const label = option.dataset.label || "Tactile pattern";
    option.classList.toggle("is-used", isUsed);
    option.setAttribute("aria-label", isUsed ? `${label}, already used` : label);
    option.title = isUsed ? `${label} — already used` : label;
  });
}

async function choosePattern(groupId, pattern) {
  statusLine("pattern-save-status", "Re-rendering the tactile master…");
  try {
    const result = await assignPattern(state.selected, groupId, pattern);
    if (result.pattern_data) state.data.patterns = result.pattern_data;
    await refreshAfterRender();
    statusLine("pattern-save-status", "Applied to the tactile result.", "success");
  } catch (error) {
    statusLine("pattern-save-status", error.message, "error");
    toast(error.message, "error");
  }
}

async function saveTransform() {
  const group = currentGroup();
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
  const group = currentGroup();
  if (!group) return;
  await withBusy($("transform-reset"), "Resetting…", async () => {
    const result = await savePatternTransform(
      state.selected, group.group_id, state.data.patterns?.defaults || {});
    group.transform = result.transform;
    await refreshAfterRender();
  });
}

async function saveColours() {
  const colors = {};
  document.querySelectorAll(".colour-row").forEach((row) => {
    const picker = row.querySelector('input[type="color"]');
    if (picker) colors[row.dataset.colourLabel] = picker.value.toUpperCase();
  });
  await withBusy($("save-colours"), "Saving…", async () => {
    state.data.colors = await saveCategoryColors(state.selected, colors);
    await refreshAfterRender();
    statusLine("colour-status", "Saved.", "success");
    toast("Category colours saved.");
  });
}

/** Every Step 7 edit rebuilds the master and whatever sits on top of it, so
 *  refresh exactly those pages rather than redrawing the whole workspace. */
async function refreshAfterRender() {
  await loadMaps();
  await refreshStepImages();
  // A reassignment can shuffle other areas too, so every thumbnail is reread
  // from the fresh payload rather than only the one that was clicked.
  await refreshSelectedData();
  renderControls();
}
