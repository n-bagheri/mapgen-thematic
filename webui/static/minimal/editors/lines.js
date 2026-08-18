"use strict";

import { $, esc, saveLineReview } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import { renderMapOverlay } from "../visual.js";

/* Step 4 pulled rivers, roads and borders out as separate paths.  Drawing and
   joining paths by hand stays in the detailed view; here they are kept or
   dropped, which is the decision that changes the geometry passed onward. */

export function lineEditorHtml() {
  const review = state.data.lines;
  const lines = review?.automatic_rivers || [];
  const body = review ? `
    <label class="line-master">
      <input id="include-lines" type="checkbox" ${review.include_rivers !== false ? "checked" : ""}>
      <span><strong>Include lines in the simplified map</strong>
        <small>Visibility above is only a view filter. This setting changes the geometry passed onward.</small></span>
    </label>
    <div class="line-list">
      ${lines.map((line, index) => `<label class="line-row">
        <input class="line-include" type="checkbox" data-line-id="${esc(line.id)}" ${line.include ? "checked" : ""}>
        <span>Line ${index + 1}<small> · ${line.points?.length || 0} points</small></span>
        <span class="line-swatch" aria-hidden="true"></span>
      </label>`).join("") || '<div class="empty-editor">No lines were extracted from this map.</div>'}
    </div>
    <div class="action-row"><a class="quiet-link" href="/">Draw or join paths in detailed view</a>
      <span><span class="status-copy" id="line-save-status"></span>
      <button class="button secondary small" id="save-lines" type="button">Save lines</button></span></div>`
    : '<div class="empty-editor">Run through Step 4 to edit the extracted lines.</div>';
  return editorDetails("lines", "3", "Lines", "Extracted linework", body);
}

export function bindLineEditor() {
  const master = $("include-lines");
  const update = () => {
    if (!state.data.lines) return;
    state.data.lines.include_rivers = master?.checked !== false;
    document.querySelectorAll(".line-include").forEach((checkbox) => {
      const line = state.data.lines.automatic_rivers.find((item) => String(item.id) === checkbox.dataset.lineId);
      if (line) line.include = checkbox.checked;
    });
    statusLine("line-save-status", "Unsaved changes");
    renderMapOverlay();
  };
  master?.addEventListener("change", update);
  document.querySelectorAll(".line-include").forEach((checkbox) => checkbox.addEventListener("change", update));
  $("save-lines")?.addEventListener("click", saveLines);
}

async function saveLines() {
  const review = state.data.lines;
  if (!review) return;
  const payload = {
    include_rivers: review.include_rivers !== false,
    include_auto_ids: (review.automatic_rivers || []).filter((river) => river.include).map((river) => river.id),
    manual_rivers: review.manual_rivers || [],
  };
  await withBusy($("save-lines"), "Saving…", async () => {
    await saveLineReview(state.selected, payload);
    await loadMaps();
    await refreshSelectedData();
    state.activeStep = 4;
    renderWorkspace(true);
    toast("Line edits saved. Step 5 onward was cleared for a clean rebuild.", "warning");
  });
}
