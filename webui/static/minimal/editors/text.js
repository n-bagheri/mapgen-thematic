"use strict";

import { $, esc, labelCropUrl, saveLabelReview } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import { renderMapOverlay } from "../visual.js";

/* Step 3 found the words printed over the map.  Here the reader fixes the
   wording, hides an entry, or decides whether its ink is lifted off before the
   colours underneath are read. */

export function textEditorHtml() {
  const occurrences = state.data.labels?.occurrences || [];
  const body = occurrences.length ? `
    <p class="section-intro">Edit the final wording, hide labels, or decide whether the printed text is removed before segmentation.</p>
    <div class="label-list">
      ${occurrences.map((item) => `
        <div class="label-row${item.include === false ? " is-excluded" : ""}" data-label-id="${esc(item.id)}">
          <img class="label-crop" src="${labelCropUrl(state.selected, item.index)}" alt="">
          <input class="label-text" type="text" value="${esc(item.review_text || item.original_text)}"
                 maxlength="200" aria-label="Overlay text for ${esc(item.original_text)}">
          <span class="row-options">
            <label class="tiny-check"><input class="label-include" type="checkbox" ${item.include !== false ? "checked" : ""}> show</label>
            <label class="tiny-check"><input class="label-remove" type="checkbox" ${item.remove !== false ? "checked" : ""}> remove ink</label>
          </span>
        </div>`).join("")}
    </div>
    <div class="action-row end"><span class="status-copy" id="text-save-status"></span>
      <button class="button secondary small" id="save-text" type="button">Save text</button></div>`
    : '<div class="empty-editor">No overlay text was detected on this map.</div>';
  return editorDetails("text", "2", "Text", "Wording and visibility overlays", body);
}

export function bindTextEditor() {
  const section = document.querySelector('[data-editor="text"]');
  if (!section) return;
  section.querySelectorAll(".label-row").forEach((row) => {
    const item = state.data.labels?.occurrences?.find((entry) => String(entry.id) === row.dataset.labelId);
    const update = () => {
      if (!item) return;
      item.review_text = row.querySelector(".label-text").value;
      item.include = row.querySelector(".label-include").checked;
      item.remove = row.querySelector(".label-remove").checked;
      row.classList.toggle("is-excluded", !item.include);
      statusLine("text-save-status", "Unsaved changes");
      renderMapOverlay();
    };
    row.querySelector(".label-text")?.addEventListener("input", update);
    row.querySelector(".label-include")?.addEventListener("change", update);
    row.querySelector(".label-remove")?.addEventListener("change", update);
  });
  $("save-text")?.addEventListener("click", saveText);
}

async function saveText() {
  const occurrences = state.data.labels?.occurrences || [];
  // The server wants one decision per detected occurrence, not just the edits.
  const decisions = occurrences.map((item) => ({
    id: item.id,
    include: item.include !== false,
    remove: item.remove !== false,
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
