"use strict";

import { $, downloadUrl } from "../api.js";
import { state } from "../state.js";

/* The finished job is two pages: the tactile map and its legend.  The server
   binds them into one PDF, in relief (black and white) or the colour hybrid. */

export function exportEditorHtml(map) {
  const ready = Boolean(map.steps?.["8"] && map.steps?.["9"] && map.step9_review_ready);
  const body = ready ? `
    <div class="export-row">
      <a class="button primary export-button" id="download-relief"
         href="${downloadUrl(map.stem)}" download>Download relief PDF</a>
      <a class="button secondary export-button" id="download-hybrid"
         href="${downloadUrl(map.stem, true)}" download>Download colour PDF</a>
    </div>`
    : `<div class="empty-editor">Approve the Step 9 legend page to activate export.</div>
       <div class="export-row">
         <button class="button primary export-button" type="button" disabled>Download relief PDF</button>
         <button class="button secondary export-button" type="button" disabled>Download colour PDF</button>
       </div>`;
  return `<section class="step-editor export-panel${ready ? " is-ready" : ""}" data-editor="export"
      data-export-preview tabindex="0" aria-label="Preview the printable map and legend pages">
    <div class="export-heading">
      <h3>Export</h3>
      <label class="tiny-check export-input-toggle"><input id="export-show-input" type="checkbox"
        ${state.showOriginalMap ? "checked" : ""}> Show input map too</label>
    </div>
    ${body}
  </section>`;
}

export function bindExportEditor(onPreview) {
  // Anchors do the work; the handler only keeps the filename honest when the
  // project was renamed after the panel was drawn.
  [$("download-relief"), $("download-hybrid")].forEach((link, index) => {
    if (!link || !state.selected) return;
    link.href = downloadUrl(state.selected, index === 1);
  });

  const showPreview = () => {
    state.showFinalMap = true;
    if (onPreview) onPreview();
  };
  $("export-show-input")?.addEventListener("change", (event) => {
    state.showOriginalMap = event.target.checked;
    showPreview();
  });
  const panel = document.querySelector("[data-export-preview]");
  panel?.addEventListener("click", (event) => {
    if (event.target.closest("a, button, input, label")) return;
    showPreview();
  });
  panel?.addEventListener("keydown", (event) => {
    if (event.target !== panel || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    showPreview();
  });
}
