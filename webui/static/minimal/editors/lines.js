"use strict";

import { $, esc, saveLineReview } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import {
  renderMapOverlay, renderSegmentedLinesOverlay, setLineDrawingActive,
} from "../visual.js";

/* Step 4 pulls rivers, roads and borders out as separate paths. Detected paths
   can be kept here, and missing paths can be drawn over the segmented map. */

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
    <div class="action-row end"><span><span class="status-copy" id="line-save-status"></span>
      <button class="button secondary small" id="save-lines" type="button">Save lines</button></span></div>`
    : '<div class="empty-editor">Run through Step 4 to edit the extracted lines.</div>';
  return editorDetails("lines", "4", "Lines", "Approve or edit extracted linework", body);
}

/** Manual strokes stay beside the detected-line settings that own the reviewed
 * line geometry supplied to every downstream step. */
export function lineDrawingEditorHtml() {
  const review = state.data.lines;
  const added = state.lineDrawing.addedIds.length;
  const body = review ? `
    <p class="section-intro">Draw missing overlaying lines directly on the map. Each pointer stroke becomes one line.</p>
    <div class="line-drawing-actions">
      <button class="button secondary small" id="draw-new-line" type="button"
              aria-pressed="${state.lineDrawing.active}">${state.lineDrawing.active ? "Stop drawing" : "Draw new lines"}</button>
      <button class="button secondary small" id="undo-drawn-line" type="button" ${added ? "" : "disabled"}>Undo stroke</button>
      <button class="button secondary small" id="discard-drawn-lines" type="button"
              ${added || state.lineDrawing.draft.length ? "" : "disabled"}>Discard changes</button>
    </div>
    <p class="status-copy line-drawing-status" id="line-drawing-status">${added
      ? `${added} unsaved drawn stroke${added === 1 ? "" : "s"}`
      : "Select Draw new lines, then drag on the map."}</p>
    <div class="action-row end">
      <button class="button primary small" id="save-drawn-lines" type="button" ${added ? "" : "disabled"}>Apply drawn lines</button>
    </div>`
    : '<div class="empty-editor">Run through line detection before drawing new lines.</div>';
  return editorDetails("line-drawing", "4", "Draw missing lines", "Add, undo, or discard strokes", body);
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
    renderSegmentedLinesOverlay();
  };
  master?.addEventListener("change", update);
  document.querySelectorAll(".line-include").forEach((checkbox) => checkbox.addEventListener("change", update));
  $("save-lines")?.addEventListener("click", (event) => saveLines(event.currentTarget));
  $("draw-new-line")?.addEventListener("click", () => {
    if (state.data.lines) state.data.lines.include_rivers = true;
    setLineDrawingActive(!state.lineDrawing.active);
  });
  $("undo-drawn-line")?.addEventListener("click", undoDrawnLine);
  $("discard-drawn-lines")?.addEventListener("click", discardDrawnLines);
  $("save-drawn-lines")?.addEventListener("click", (event) => saveLines(event.currentTarget));
}

function undoDrawnLine() {
  const id = state.lineDrawing.addedIds.pop();
  if (!id || !state.data.lines) return;
  state.data.lines.manual_rivers = (state.data.lines.manual_rivers || [])
    .filter((line) => line.id !== id);
  renderMapOverlay();
  renderSegmentedLinesOverlay();
  setLineDrawingActive(state.lineDrawing.active);
}

function discardDrawnLines() {
  const added = new Set(state.lineDrawing.addedIds);
  if (state.data.lines) {
    state.data.lines.manual_rivers = (state.data.lines.manual_rivers || [])
      .filter((line) => !added.has(line.id));
  }
  state.lineDrawing.addedIds = [];
  state.lineDrawing.draft = [];
  setLineDrawingActive(false);
  renderMapOverlay();
  renderSegmentedLinesOverlay();
}

async function saveLines(button = $("save-lines")) {
  const review = state.data.lines;
  if (!review) return;
  const payload = {
    include_rivers: review.include_rivers !== false,
    include_auto_ids: (review.automatic_rivers || []).filter((river) => river.include).map((river) => river.id),
    manual_rivers: review.manual_rivers || [],
  };
  await withBusy(button, "Saving…", async () => {
    await saveLineReview(state.selected, payload);
    state.lineDrawing = { active: false, draft: [], addedIds: [] };
    await loadMaps();
    await refreshSelectedData();
    state.activeStep = 4;
    renderWorkspace(true);
    toast("Line edits saved. Step 5 onward was cleared for a clean rebuild.", "warning");
  });
}
