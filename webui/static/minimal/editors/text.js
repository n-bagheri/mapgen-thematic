"use strict";

import { $, esc, labelCropUrl, saveLabelReview } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import { renderMapOverlay } from "../visual.js";

/* Step 3 found the words printed over the map. The only user decision is
   whether the corrected label is carried forward; source ink is always
   cleaned before segmentation so there are not two switches for one choice. */

export function textEditorHtml() {
  const occurrences = state.data.labels?.occurrences || [];
  const allOff = occurrences.length > 0 && occurrences.every((item) => item.include === false);
  const body = occurrences.length ? `
    <p class="section-intro">Edit the detected wording or turn off entries you do not want carried into the tactile map. Detected source ink is cleaned automatically before segmentation.</p>
    <div class="label-list-toolbar">
      <span>${occurrences.length} detected text entr${occurrences.length === 1 ? "y" : "ies"}</span>
      <button class="button secondary small" id="toggle-all-text" type="button">
        ${allOff ? "Turn all on" : "Turn all off"}</button>
    </div>
    <div class="label-list">
      ${occurrences.map((item) => `
        <div class="label-row${item.include === false ? " is-excluded" : ""}" data-label-id="${esc(item.id)}">
          <img class="label-crop" src="${labelCropUrl(state.selected, item.index)}" alt=""
               loading="lazy" decoding="async" fetchpriority="low">
          <input class="label-text" type="text" value="${esc(item.review_text || item.original_text)}"
                 maxlength="200" aria-label="Overlay text for ${esc(item.original_text)}">
          <span class="row-options">
            <label class="tiny-check"><input class="label-include" type="checkbox" ${item.include !== false ? "checked" : ""}> use this text</label>
          </span>
        </div>`).join("")}
    </div>
    <div class="action-row end"><span class="status-copy" id="text-save-status"></span>
      <button class="button secondary small" id="save-text" type="button">Save text</button></div>`
    : '<div class="empty-editor">No overlay text was detected on this map.</div>';
  return editorDetails("text", "3", "Detected text", "Edit wording or turn an entry off", body);
}

export function bindTextEditor() {
  const section = document.querySelector('[data-editor="text"]');
  if (!section) return;
  const rows = [...section.querySelectorAll(".label-row")];
  const bulkToggle = $("toggle-all-text");
  const syncBulkToggle = () => {
    if (!bulkToggle) return;
    const checkboxes = rows.map((row) => row.querySelector(".label-include"));
    const allOff = checkboxes.length > 0 && checkboxes.every((checkbox) => !checkbox.checked);
    bulkToggle.textContent = allOff ? "Turn all on" : "Turn all off";
  };
  rows.forEach((row) => {
    const item = state.data.labels?.occurrences?.find((entry) => String(entry.id) === row.dataset.labelId);
    const update = () => {
      if (!item) return;
      item.review_text = row.querySelector(".label-text").value;
      item.include = row.querySelector(".label-include").checked;
      item.remove = true;
      row.classList.toggle("is-excluded", !item.include);
      statusLine("text-save-status", "Unsaved changes");
      syncBulkToggle();
      renderMapOverlay();
    };
    row.querySelector(".label-text")?.addEventListener("input", update);
    row.querySelector(".label-include")?.addEventListener("change", update);
  });
  bulkToggle?.addEventListener("click", () => {
    const checkboxes = rows.map((row) => row.querySelector(".label-include"));
    const enableAll = checkboxes.every((checkbox) => !checkbox.checked);
    rows.forEach((row, index) => {
      const item = state.data.labels?.occurrences?.find(
        (entry) => String(entry.id) === row.dataset.labelId);
      checkboxes[index].checked = enableAll;
      if (item) {
        item.include = enableAll;
        item.remove = true;
      }
      row.classList.toggle("is-excluded", !enableAll);
    });
    statusLine("text-save-status", "Unsaved changes");
    syncBulkToggle();
    renderMapOverlay();
  });
  $("save-text")?.addEventListener("click", saveText);
}

async function saveText() {
  const occurrences = state.data.labels?.occurrences || [];
  // The server wants one decision per detected occurrence, not just the edits.
  const decisions = occurrences.map((item) => ({
    id: item.id,
    include: item.include !== false,
    remove: true,
    text: String(item.review_text || item.original_text || "").trim(),
  }));
  await withBusy($("save-text"), "Saving…", async () => {
    const result = await saveLabelReview(state.selected, decisions);
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
    if (result.segmentation_invalidated) {
      toast("Text-removal changes cleared Step 4 onward. Continue the pipeline to rebuild them.", "warning");
    } else {
      toast("Text overlay saved.");
    }
  });
}
