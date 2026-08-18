"use strict";

import { $, esc, resetMask, saveMaskStrokes } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";

/* Step 2 decides which pixels are the map and which are page furniture.  The
   brush paints corrections straight onto the input stage: erase takes pixels
   out of the map, restore puts them back. */

export function maskEditorHtml() {
  const review = state.data.mask;
  const brush = state.maskBrush;
  const body = review ? `
    <p class="section-intro">The map area was found automatically. Paint over anything it kept by
      mistake, or restore anything it dropped, then save.</p>
    <div class="mask-toolbar">
      <button class="button ${brush.active ? "primary" : "secondary"} small" id="mask-toggle" type="button">
        ${brush.active ? "Stop painting" : "Paint on the map"}</button>
      <span class="choice-row">
        <label class="check-chip"><input type="radio" name="mask-mode" value="erase"
          ${brush.mode === "erase" ? "checked" : ""}><span>Erase</span></label>
        <label class="check-chip"><input type="radio" name="mask-mode" value="restore"
          ${brush.mode === "restore" ? "checked" : ""}><span>Restore</span></label>
      </span>
      <span class="brush-size">Brush
        <input id="mask-radius" type="range" min="1" max="100" step="1" value="${brush.radius}"
               aria-label="Brush radius in pixels">
        <output id="mask-radius-value">${brush.radius}px</output></span>
    </div>
    <p class="mask-stats">
      <span><strong>${Number(review.kept_pixels).toLocaleString()}</strong> pixels kept</span>
      <span><strong>${Number(review.automatic_pixels).toLocaleString()}</strong> found automatically</span>
      <span>${review.reviewed ? "Edited by hand" : "Automatic only"}</span>
    </p>
    <div class="action-row">
      <span class="status-copy" id="mask-save-status">${esc(brush.strokes.length
        ? `${brush.strokes.length} unsaved stroke${brush.strokes.length === 1 ? "" : "s"}`
        : "")}</span>
      <span class="choice-row">
        <button class="button subtle small" id="mask-reset" type="button">Reset to automatic</button>
        <button class="button secondary small" id="mask-save" type="button"
                ${brush.strokes.length ? "" : "disabled"}>Save mask</button>
      </span>
    </div>`
    : '<div class="empty-editor">Run through Step 2 to correct the map area.</div>';
  return editorDetails("mask", "1", "Geographic mask", "Which pixels count as the map", body);
}

export function bindMaskEditor() {
  $("mask-toggle")?.addEventListener("click", () => {
    state.maskBrush.active = !state.maskBrush.active;
    renderWorkspace();
  });
  document.querySelectorAll('input[name="mask-mode"]').forEach((radio) => {
    radio.addEventListener("change", () => { state.maskBrush.mode = radio.value; });
  });
  const radius = $("mask-radius");
  radius?.addEventListener("input", () => {
    state.maskBrush.radius = Number(radius.value);
    $("mask-radius-value").textContent = `${radius.value}px`;
  });
  $("mask-save")?.addEventListener("click", saveMask);
  $("mask-reset")?.addEventListener("click", resetMaskToAutomatic);
}

/** The canvas only exists while the brush is on, so an idle map keeps a plain
 *  picture that can still be selected and dragged like any other image. */
export function bindMaskCanvas() {
  // The review works in map_area.png coordinates, so the brush has to sit on
  // that picture -- Step 2's first panel -- and not on the uploaded source.
  const holder = $("mask-target");
  if (!holder) return;
  holder.querySelector(".mask-canvas")?.remove();
  holder.classList.toggle("is-masking", state.maskBrush.active);
  const review = state.data.mask;
  if (!state.maskBrush.active || !review) return;

  const image = holder.querySelector("img");
  const canvas = document.createElement("canvas");
  canvas.className = "mask-canvas";
  canvas.width = Number(review.width) || 1;
  canvas.height = Number(review.height) || 1;
  holder.appendChild(canvas);
  const context = canvas.getContext("2d");
  redrawStrokes(context);

  let current = null;
  const toImagePoint = (event) => {
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    return [
      Math.round((event.clientX - box.left) / box.width * canvas.width),
      Math.round((event.clientY - box.top) / box.height * canvas.height),
    ];
  };

  canvas.addEventListener("pointerdown", (event) => {
    if (state.maskBrush.strokes.length >= 300) {
      toast("Save the 300 strokes already painted before adding more.", "warning");
      return;
    }
    const point = toImagePoint(event);
    if (!point) return;
    canvas.setPointerCapture(event.pointerId);
    current = { mode: state.maskBrush.mode, radius: state.maskBrush.radius, points: [point] };
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!current) return;
    const point = toImagePoint(event);
    // 5000 points is the server ceiling for one stroke; stop well inside it.
    if (point && current.points.length < 4000) {
      current.points.push(point);
      redrawStrokes(context, current);
    }
  });
  const finish = () => {
    if (!current) return;
    state.maskBrush.strokes.push(current);
    current = null;
    redrawStrokes(context);
    const save = $("mask-save");
    if (save) save.disabled = false;
    const count = state.maskBrush.strokes.length;
    statusLine("mask-save-status", `${count} unsaved stroke${count === 1 ? "" : "s"}`);
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
  canvas.addEventListener("pointerleave", finish);
  if (image && !image.complete) image.addEventListener("load", () => redrawStrokes(context), { once: true });
}

function redrawStrokes(context, pending = null) {
  const { canvas } = context;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.lineCap = "round";
  context.lineJoin = "round";
  [...state.maskBrush.strokes, ...(pending ? [pending] : [])].forEach((stroke) => {
    context.strokeStyle = stroke.mode === "erase"
      ? "rgba(197, 52, 52, .55)" : "rgba(30, 158, 90, .55)";
    context.lineWidth = stroke.radius * 2;
    context.beginPath();
    stroke.points.forEach((point, index) => {
      if (index) context.lineTo(point[0], point[1]);
      else context.moveTo(point[0], point[1]);
    });
    if (stroke.points.length === 1) context.lineTo(stroke.points[0][0] + .01, stroke.points[0][1]);
    context.stroke();
  });
}

async function saveMask() {
  const strokes = state.maskBrush.strokes;
  if (!strokes.length) return;
  await withBusy($("mask-save"), "Saving…", async () => {
    const result = await saveMaskStrokes(state.selected, strokes);
    state.maskBrush.strokes = [];
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
    toast(result.downstream_invalidated
      ? "Mask saved. Step 3 onward was cleared so it can be rebuilt."
      : "Mask saved.", result.downstream_invalidated ? "warning" : "");
  });
}

async function resetMaskToAutomatic() {
  await withBusy($("mask-reset"), "Resetting…", async () => {
    await resetMask(state.selected);
    state.maskBrush.strokes = [];
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
    toast("Mask returned to the automatic result.");
  });
}
