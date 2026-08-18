"use strict";

import { $, downloadUrl, esc } from "../api.js";
import { state } from "../state.js";
import { editorDetails } from "../controls.js";

/* The finished job is two pages: the tactile map and its legend.  The server
   binds them into one PDF, in relief (black and white) or the colour hybrid. */

export function exportEditorHtml(map) {
  const ready = Boolean(map.steps?.["8"] && map.steps?.["9"]);
  const body = ready ? `
    <p class="section-intro">Both pages are ready. The relief PDF is the one to emboss; the colour
      PDF carries the same geometry with the category colours printed on top.</p>
    <div class="export-row">
      <a class="button primary small" id="download-relief"
         href="${downloadUrl(map.stem)}" download>Download relief PDF</a>
      <a class="button secondary small" id="download-hybrid"
         href="${downloadUrl(map.stem, true)}" download>Download colour PDF</a>
    </div>
    <p class="field-note">Two pages at ${esc(String(state.data.braille?.page?.dpi ?? 127))} DPI:
      the tactile map, then the legend.</p>`
    : '<div class="empty-editor">Finish Steps 8 and 9 to download the combined PDF.</div>';
  return editorDetails("export", "10", "Export", "The finished two-page PDF", body);
}

export function bindExportEditor() {
  // Anchors do the work; the handler only keeps the filename honest when the
  // project was renamed after the panel was drawn.
  [$("download-relief"), $("download-hybrid")].forEach((link, index) => {
    if (!link || !state.selected) return;
    link.href = downloadUrl(state.selected, index === 1);
  });
}
