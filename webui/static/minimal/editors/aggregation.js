"use strict";

import { $, esc, previewAggregation, saveAggregationReview } from "../api.js";
import { state, toast, withBusy } from "../state.js";
import { loadMaps } from "../workspace.js";
import { continuePipeline, renderControls } from "../controls.js";

let previewTimer = null;
let previewGeneration = 0;

/* Step 5 fits the map to the number of textures a hand can tell apart.  The
   proposal arrives already filled in; this panel exists so a reader can move a
   layer to a different category, or split a merge back apart, before anything
   downstream is built from it. */

/** Slots the review may use: the proposal groups, plus any spare texture
 *  capacity, so a merge can be split apart here instead of only accepted. */
function aggregationSlots() {
  const data = state.data.aggregation;
  const effective = data?.effective_groups || [];
  const proposed = data?.proposal?.groups || [];
  const groups = effective.length ? effective : proposed;
  const slots = Math.max(groups.length, Number(data?.proposal?.slots) || 0);
  return Array.from({ length: slots }, (_, slot) => ({
    label: state.groupLabels[slot] ?? groups[slot]?.label ?? `Category ${slot + 1}`,
    members: (groups[slot]?.members || []).map(Number),
  }));
}

/** Which slot each source class currently sits in, including local edits. */
function aggregationAssignment() {
  if (state.groupEdit) return state.groupEdit;
  const assignment = {};
  aggregationSlots().forEach((slot, index) => {
    slot.members.forEach((member) => { assignment[member] = index; });
  });
  return assignment;
}

/** Category containers shown in the editor. The proposal starts prefilled;
 *  spare texture slots appear only when the reviewer asks for one. */
function visibleAggregationSlots() {
  const slots = aggregationSlots();
  const proposed = slots
    .map((slot, index) => slot.members.length ? index : null)
    .filter((index) => index !== null);
  if (!Array.isArray(state.visibleGroupSlots)
      || (!state.visibleGroupSlots.length && proposed.length)) {
    state.visibleGroupSlots = proposed;
    if (!state.visibleGroupSlots.length && slots.length) state.visibleGroupSlots = [0];
  }
  return state.visibleGroupSlots.filter((slot) => slot >= 0 && slot < slots.length);
}

/* Step 5 runs before simplification now, so the colours come from Step 4's
   classes_final.json; classes_gen.json only exists once Step 6 has run. */
function layerColour(cl) {
  const sources = [
    state.data.classesFinal?.classes,
    state.data.classesGen?.classes,
  ];
  const match = sources.reduce((found, list) => found
    || (list || []).find((item) => Number(item.index) === Number(cl.index)), null);
  const rgb = (match?.rgb || cl.rgb || [224, 228, 234]).map(
    (channel) => Math.max(0, Math.min(255, Number(channel) || 0)));
  const linear = rgb.map((channel) => {
    const value = channel / 255;
    return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
  });
  const luminance = .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
  return {
    background: `rgb(${rgb.join(", ")})`,
    foreground: luminance > .179 ? "#17202c" : "#fff",
  };
}

function layerChipHtml(cl) {
  const colour = layerColour(cl);
  return `<article class="layer-chip" draggable="true" data-class-index="${Number(cl.index)}"
      aria-label="${esc(cl.label)}. Drag to another tactile category."
      style="--layer-colour:${colour.background};--layer-ink:${colour.foreground}">
    <span class="layer-drag-handle" aria-hidden="true">&#8942;&#8942;</span>
    <span class="layer-name">${esc(cl.label)}</span>
  </article>`;
}

function absentLegendLayersHtml() {
  const classes = state.data.classesGen?.classes || [];
  const absent = classes.filter((cl) => cl.source === "legend" && Number(cl.area_share) <= 0);
  if (!absent.length) return "";
  const merged = absent.filter((cl) => Number(cl.area_share_before) > 0);
  const notFound = absent.filter((cl) => Number(cl.area_share_before) <= 0);
  const list = (items) => items.length
    ? `<ul>${items.map((cl) => `<li>${esc(cl.label)}</li>`).join("")}</ul>`
    : '<p class="absent-none">None</p>';
  return `<details class="absent-layers">
    <summary><span><strong>${absent.length} legend ${absent.length === 1 ? "layer is" : "layers are"} absent after simplification</strong>
      <small>${merged.length} merged during simplification · ${notFound.length} not found in the source map</small></span>
      <span class="absent-caret" aria-hidden="true"></span></summary>
    <div class="absent-layer-groups">
      <section><h4>Merged during simplification</h4>${list(merged)}</section>
      <section><h4>Not found in the source map</h4>${list(notFound)}</section>
    </div>
  </details>`;
}

export function aggregationGateHtml() {
  const proposal = state.data.aggregation?.proposal;
  const source = proposal?.source_classes || [];
  const slots = aggregationSlots();
  const assignment = aggregationAssignment();
  const visibleSlots = visibleAggregationSlots();
  const groups = visibleSlots.map((index) => ({
    ...slots[index],
    index,
    classes: source.filter((cl) => assignment[Number(cl.index)] === index),
  }));
  return `
    <section class="review-gate">
      <span class="section-kicker">One decision needed</span>
      <h3>Review the suggested tactile categories.</h3>
      <p class="section-intro">This map has more categories than there are textures a hand can tell apart,
        so some must share one. Drag layers between categories, then approve. The geography stays fixed
        while the fitted-category preview updates to show which regions now share a category.</p>
      <div class="category-toolbar">
        <p><strong>${groups.length} of ${slots.length}</strong> available tactile categories used. The limit is a maximum, not a target.</p>
        <span class="category-toolbar-actions">
          <button class="button subtle small" id="reset-groups" type="button">Reset suggestion</button>
          <button class="button secondary small" id="add-group" type="button"
                  ${visibleSlots.length >= slots.length ? "disabled" : ""}>+ Add category</button>
        </span>
      </div>
      <div class="tactile-group-grid">
        ${groups.map((group) => `<section class="tactile-group ${group.classes.length ? "" : "is-empty"}"
            data-group-slot="${group.index}">
          <header class="tactile-group-header">
            <label><span>Category name</span>
              <input class="group-label" type="text" value="${esc(group.label)}" maxlength="80"
                     data-group-slot="${group.index}"
                     aria-label="Name for tactile category ${group.index + 1}">
            </label>
            <button class="remove-group" type="button" data-group-slot="${group.index}"
                    ${group.classes.length || groups.length <= 1 ? "disabled" : ""}
                    title="${group.classes.length ? "Move its layers before removing this category" : "Remove this empty category"}"
                    aria-label="Remove ${esc(group.label)}">&times;</button>
          </header>
          <div class="tactile-drop-zone" data-group-slot="${group.index}"
               aria-label="Layers assigned to ${esc(group.label)}">
            ${group.classes.length
              ? group.classes.map((cl) => layerChipHtml(cl)).join("")
              : '<p class="empty-group-copy">Drop a layer here</p>'}
          </div>
        </section>`).join("")}
      </div>
      ${absentLegendLayersHtml()}
      <div class="action-row end">
        <button class="button primary" id="approve-groups" type="button"
          ${groups.length ? "" : "disabled"}>Approve grouping &amp; continue</button>
      </div>
    </section>`;
}

export function bindAggregationEditor() {
  document.querySelectorAll(".group-label").forEach((input) => {
    input.addEventListener("input", () => {
      state.groupLabels[Number(input.dataset.groupSlot)] = input.value;
    });
  });
  document.querySelectorAll(".layer-chip").forEach((chip) => {
    chip.addEventListener("dragstart", (event) => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", chip.dataset.classIndex);
      chip.classList.add("is-dragging");
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("is-dragging");
      document.querySelectorAll(".tactile-drop-zone").forEach(
        (zone) => zone.classList.remove("is-drag-over"));
    });
  });
  document.querySelectorAll(".tactile-drop-zone").forEach((zone) => {
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      zone.classList.add("is-drag-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-drag-over"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      const dragged = event.dataTransfer.getData("text/plain");
      const classIndex = Number(dragged);
      const slotIndex = Number(zone.dataset.groupSlot);
      if (dragged !== "" && Number.isInteger(classIndex) && Number.isInteger(slotIndex)) {
        moveLayerToGroup(classIndex, slotIndex);
      }
    });
  });
  $("add-group")?.addEventListener("click", () => {
    const visible = visibleAggregationSlots();
    const next = aggregationSlots().findIndex((_, index) => !visible.includes(index));
    if (next < 0) return;
    state.visibleGroupSlots = [...visible, next];
    renderControls();
  });
  document.querySelectorAll(".remove-group").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      const slot = Number(button.dataset.groupSlot);
      state.visibleGroupSlots = visibleAggregationSlots().filter((index) => index !== slot);
      delete state.groupLabels[slot];
      renderControls();
    });
  });
  $("reset-groups")?.addEventListener("click", () => {
    state.groupEdit = null;
    state.groupLabels = {};
    state.visibleGroupSlots = null;
    renderControls();
    queueAggregationPreview();
  });
  $("approve-groups")?.addEventListener("click", approveAggregation);
}

function moveLayerToGroup(classIndex, slotIndex) {
  state.groupEdit = { ...aggregationAssignment(), [classIndex]: slotIndex };
  if (!visibleAggregationSlots().includes(slotIndex)) {
    state.visibleGroupSlots = [...visibleAggregationSlots(), slotIndex];
  }
  renderControls();
  queueAggregationPreview();
}

/** The groups exactly as the panel shows them, ready for the review API. */
function reviewedGroups() {
  const source = state.data.aggregation?.proposal?.source_classes || [];
  const assignment = aggregationAssignment();
  return aggregationSlots().map((slot, index) => ({
    label: String(slot.label || "").trim() || `Category ${index + 1}`,
    members: source.filter((cl) => assignment[Number(cl.index)] === index)
      .map((cl) => Number(cl.index)),
    approved: true,
    rationale: "reviewed in the focused view",
  })).filter((group) => group.members.length);
}

/** Recolour Step 5 from the unsaved assignment. The preview endpoint performs
 *  the same grouping as approval but never writes review or pipeline files. */
function queueAggregationPreview() {
  if (previewTimer) window.clearTimeout(previewTimer);
  const generation = ++previewGeneration;
  previewTimer = window.setTimeout(async () => {
    previewTimer = null;
    const image = document.querySelector(
      'img[data-artifact="step5_aggregation_preview.png"]');
    if (!image) return;
    image.classList.add("is-updating");
    try {
      const blob = await previewAggregation(state.selected, reviewedGroups());
      if (generation !== previewGeneration || !image.isConnected) return;
      if (state.aggregationPreviewUrl) URL.revokeObjectURL(state.aggregationPreviewUrl);
      state.aggregationPreviewUrl = URL.createObjectURL(blob);
      image.src = state.aggregationPreviewUrl;
      const fullSize = image.closest(".map-stage")?.querySelector("[data-full-size]");
      if (fullSize) fullSize.href = state.aggregationPreviewUrl;
    } catch (error) {
      if (generation === previewGeneration) {
        toast(`The category preview could not be refreshed: ${error.message}`, "error");
      }
    } finally {
      if (generation === previewGeneration && image.isConnected) {
        image.classList.remove("is-updating");
      }
    }
  }, 120);
}

function cancelAggregationPreview() {
  if (previewTimer) window.clearTimeout(previewTimer);
  previewTimer = null;
  previewGeneration += 1;
}

function clearAggregationPreviewUrl() {
  if (state.aggregationPreviewUrl) URL.revokeObjectURL(state.aggregationPreviewUrl);
  state.aggregationPreviewUrl = null;
}

async function approveAggregation() {
  const groups = reviewedGroups();
  if (!groups.length) return;
  cancelAggregationPreview();
  await withBusy($("approve-groups"), "Approving…", async () => {
    await saveAggregationReview(state.selected, groups);
    clearAggregationPreviewUrl();
    state.groupEdit = null;
    state.groupLabels = {};
    state.visibleGroupSlots = null;
    state.autorun = true;
    await loadMaps();
    await continuePipeline();
    toast("Categories approved. Building the simplified map and the tactile result.");
  });
}
