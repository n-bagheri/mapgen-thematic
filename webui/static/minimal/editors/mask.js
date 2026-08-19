"use strict";

import { $, approveMask, artifactUrl, saveMaskStrokes } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";

/* Step 2 decides which pixels are the map and which are page furniture.  The
   brush paints corrections straight onto the input stage: erase takes pixels
   out of the map, restore puts them back. */

/** Run all stops here after Step 2. The map itself remains in the middle pane;
 *  this card contains decisions and brush controls only. */
export function maskDecisionHtml() {
  const review = state.data.mask;
  const brush = state.maskBrush;
  const pending = brush.strokes.length;
  const needsApproval = Boolean(review?.reviewed && !review?.approved && !pending);
  return `
    <section class="review-gate mask-decision" aria-labelledby="mask-decision-title">
      <span class="section-kicker">One decision needed</span>
      <h3 id="mask-decision-title">Review the detected mask.</h3>
      <p class="section-intro">The detected map area will be adapted to a tactile map. Remove parts
        that should not be included, restore areas that were excluded, or approve the detected mask unchanged.</p>
      <div class="mask-decision-modes" role="radiogroup" aria-label="Mask brush mode">
        <label><input type="radio" name="mask-mode" value="erase" ${brush.mode === "erase" ? "checked" : ""}>
          <span class="mask-mode-icon" aria-hidden="true">
            <svg viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M5.5 24.5 20.1 9.9a3 3 0 0 1 4.2 0l5.8 5.8a3 3 0 0 1 0 4.2L20 30H11l-5.5-5.5Z"/>
              <path d="m15.2 14.8 10 10M5 30h26"/>
            </svg>
          </span><strong>Remove from map</strong></label>
        <label><input type="radio" name="mask-mode" value="restore" ${brush.mode === "restore" ? "checked" : ""}>
          <span class="mask-mode-icon" aria-hidden="true">
            <img src="/images/brush.png" alt="">
          </span><strong>Restore map area</strong></label>
      </div>
      <label class="mask-decision-brush"><span>Brush size</span>
        <input id="mask-radius" type="range" min="1" max="100" step="1" value="${brush.radius}">
        <output id="mask-radius-value">${brush.radius} px</output></label>
      <p class="status-copy mask-decision-status" id="mask-save-status">${pending
        ? `${pending} unapplied stroke${pending === 1 ? "" : "s"}`
        : review?.reviewed && !review?.approved ? "Changes applied. Approval is still required." : ""}</p>
      <div class="mask-decision-actions">
        <button class="button secondary small" id="mask-undo" type="button" ${pending ? "" : "disabled"}>Undo stroke</button>
        <button class="button secondary small" id="mask-discard" type="button" ${pending ? "" : "disabled"}>Discard edits</button>
        <button class="button primary small" id="mask-save" type="button" ${pending ? "" : "disabled"}>Apply changes</button>
      </div>
      <button class="button primary full approve-mask${needsApproval ? " needs-attention" : ""}"
              id="approve-mask" type="button" ${pending ? "disabled" : ""}>Approve mask &amp; continue</button>
    </section>`;
}

export function bindMaskEditor(onApproved) {
  document.querySelectorAll('input[name="mask-mode"]').forEach((radio) => {
    radio.addEventListener("change", () => { state.maskBrush.mode = radio.value; });
  });
  const radius = $("mask-radius");
  radius?.addEventListener("input", () => {
    state.maskBrush.radius = Number(radius.value);
    $("mask-radius-value").textContent = `${radius.value}px`;
  });
  $("mask-save")?.addEventListener("click", saveMask);
  $("mask-undo")?.addEventListener("click", undoMaskStroke);
  $("mask-discard")?.addEventListener("click", discardMaskEdits);
  $("approve-mask")?.addEventListener("click", () => approveMaskAndContinue(onApproved));
}

/** Draw the persisted geographic mask over the map and apply unsaved strokes
 * in memory. Excluded pixels are greyed immediately, matching the detailed
 * view; the server receives the same image-coordinate strokes on Apply. */
export function bindMaskCanvas() {
  // The review works in map_area.png coordinates, so the brush has to sit on
  // that picture -- Step 2's first panel -- and not on the uploaded source.
  const holder = $("mask-target");
  if (!holder) return;
  holder.querySelector(".mask-canvas")?.remove();
  holder.classList.toggle("is-masking", state.maskBrush.active);
  const review = state.data.mask;
  if (!review) return;

  const canvas = document.createElement("canvas");
  canvas.className = `mask-canvas${state.maskBrush.active ? "" : " is-readonly"}`;
  canvas.width = Number(review.width) || 1;
  canvas.height = Number(review.height) || 1;
  canvas.setAttribute("aria-label", "Current editable geographic mask over the map");
  holder.appendChild(canvas);
  const context = canvas.getContext("2d");
  const maskImage = new Image();
  let maskReady = false;
  maskImage.addEventListener("load", () => {
    maskReady = true;
    redrawMask(context, maskImage);
  }, { once: true });
  maskImage.addEventListener("error", () => {
    statusLine("mask-save-status", "The current mask could not be displayed.", "error");
  }, { once: true });
  maskImage.src = artifactUrl(state.selected, "map_mask.png");

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
    if (!state.maskBrush.active || !maskReady) return;
    if (state.maskBrush.strokes.length >= 300) {
      toast("Save the 300 strokes already painted before adding more.", "warning");
      return;
    }
    const point = toImagePoint(event);
    if (!point) return;
    canvas.setPointerCapture(event.pointerId);
    current = { mode: state.maskBrush.mode, radius: state.maskBrush.radius, points: [point] };
    redrawMask(context, maskImage, current);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!current) return;
    const point = toImagePoint(event);
    // 5000 points is the server ceiling for one stroke; stop well inside it.
    if (point && current.points.length < 4000) {
      current.points.push(point);
      redrawMask(context, maskImage, current);
    }
  });
  const finish = () => {
    if (!current) return;
    state.maskBrush.strokes.push(current);
    current = null;
    redrawMask(context, maskImage);
    const save = $("mask-save");
    if (save) save.disabled = false;
    const undo = $("mask-undo");
    if (undo) undo.disabled = false;
    const discard = $("mask-discard");
    if (discard) discard.disabled = false;
    const approve = $("approve-mask");
    if (approve) approve.disabled = true;
    const count = state.maskBrush.strokes.length;
    statusLine("mask-save-status", `${count} unapplied stroke${count === 1 ? "" : "s"}`);
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
  canvas.addEventListener("pointerleave", finish);
}

function drawMaskStroke(context, stroke) {
  context.strokeStyle = stroke.mode === "erase" ? "#000" : "#fff";
  context.fillStyle = context.strokeStyle;
  context.lineWidth = stroke.radius * 2;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.beginPath();
  stroke.points.forEach((point, index) => {
    if (index) context.lineTo(point[0], point[1]);
    else context.moveTo(point[0], point[1]);
  });
  if (stroke.points.length > 1) context.stroke();
  else if (stroke.points.length === 1) {
    context.beginPath();
    context.arc(stroke.points[0][0], stroke.points[0][1], stroke.radius, 0, Math.PI * 2);
    context.fill();
  }
}

function redrawMask(context, maskImage, pending = null) {
  const { canvas } = context;
  if (!maskImage.complete || !maskImage.naturalWidth) return;
  const effectiveCanvas = document.createElement("canvas");
  effectiveCanvas.width = canvas.width;
  effectiveCanvas.height = canvas.height;
  const effective = effectiveCanvas.getContext("2d", { willReadFrequently: true });
  effective.drawImage(maskImage, 0, 0, canvas.width, canvas.height);
  [...state.maskBrush.strokes, ...(pending ? [pending] : [])].forEach((stroke) => {
    drawMaskStroke(effective, stroke);
  });
  const maskPixels = effective.getImageData(0, 0, canvas.width, canvas.height);
  const overlay = context.createImageData(canvas.width, canvas.height);
  for (let offset = 0; offset < maskPixels.data.length; offset += 4) {
    if (maskPixels.data[offset] >= 128) continue;
    overlay.data[offset] = 95;
    overlay.data[offset + 1] = 100;
    overlay.data[offset + 2] = 110;
    overlay.data[offset + 3] = 165;
  }
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.putImageData(overlay, 0, 0);
}

async function saveMask() {
  const strokes = state.maskBrush.strokes;
  if (!strokes.length) return;
  const decisionGate = Boolean($("approve-mask"));
  await withBusy($("mask-save"), "Saving…", async () => {
    const result = await saveMaskStrokes(state.selected, strokes);
    state.maskBrush.strokes = [];
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
    toast(decisionGate
      ? "Mask changes applied. Approve the mask to continue."
      : result.downstream_invalidated
      ? "Mask saved. Step 3 onward was cleared so it can be rebuilt."
      : "Mask saved.", decisionGate || result.downstream_invalidated ? "warning" : "");
  });
}

function undoMaskStroke() {
  if (!state.maskBrush.strokes.length) return;
  state.maskBrush.strokes.pop();
  renderWorkspace();
}

function discardMaskEdits() {
  if (!state.maskBrush.strokes.length) return;
  state.maskBrush.strokes = [];
  renderWorkspace();
}

async function approveMaskAndContinue(onApproved) {
  if (state.maskBrush.strokes.length) return;
  await withBusy($("approve-mask"), "Approving…", async () => {
    await approveMask(state.selected);
    state.maskBrush.active = false;
    await loadMaps();
    await refreshSelectedData();
    toast("Mask approved. Continuing the run.");
    await onApproved?.();
  });
}
