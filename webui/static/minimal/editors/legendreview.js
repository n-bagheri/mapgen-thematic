"use strict";

import { $, approveLegendReview, deriveLegendReview, saveLegendBox, stopForMissingLegend } from "../api.js";
import { state, toast, withBusy } from "../state.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";

function warningHtml(review) {
  if (review?.status === "missing") {
    return `<section class="legend-warning legend-warning-missing" role="alert">
      <div class="legend-warning-title"><span class="legend-warning-sign" aria-hidden="true">!</span><h3>No legend detected</h3></div>
      <p>Choose how to continue. Categories will not be derived unless you explicitly request it.</p>
      <div class="legend-warning-actions">
        <button class="button primary full" id="derive-map-categories" type="button">Derive categories from map</button>
        <button class="button secondary full" type="button" disabled>Create legend</button>
        <button class="button danger full" id="stop-no-legend" type="button">Stop and use a different map</button>
      </div>
    </section>`;
  }
  if (review?.status === "mismatch") {
    return `<section class="legend-warning legend-warning-mismatch" role="alert">
      <div class="legend-warning-title"><span class="legend-warning-sign" aria-hidden="true">!</span><h3>Legend categories do not match</h3></div>
      <p>Step 1 read ${review.expected_area_fills} thematic area ${review.expected_area_fills === 1 ? "category" : "categories"};
        this box contains ${review.detected_swatches} matching swatch${review.detected_swatches === 1 ? "" : "es"}.
        Adjust the box and apply it, or create a legend.</p>
      <button class="button secondary full" type="button" disabled>Create legend</button>
    </section>`;
  }
  return "";
}

export function legendReviewHtml() {
  const review = state.data.legendReview;
  if (!review || review.status === "legacy") return "";
  const ready = review.status === "ready";
  const hasBox = Array.isArray(review.box);
  return `
    <section class="review-gate legend-review" aria-labelledby="legend-review-title">
      <span class="section-kicker">One decision needed</span>
      <h3 id="legend-review-title">Review the detected legend.</h3>
      <p class="section-intro">Move or resize the highlighted box on the map until it encloses the complete legend.
        Apply the box to check its swatches, then approve it to continue.</p>
      ${hasBox ? `<div class="legend-review-actions">
        <button class="button secondary full" id="apply-legend-box" type="button">Apply legend box</button>
        <button class="button primary full" id="approve-legend-box" type="button" ${ready ? "" : "disabled"}>Approve legend &amp; continue</button>
      </div>` : ""}
      ${warningHtml(review)}
    </section>`;
}

export function bindLegendReviewEditor(onApproved) {
  $("apply-legend-box")?.addEventListener("click", applyBox);
  $("approve-legend-box")?.addEventListener("click", () => approve(onApproved));
  $("derive-map-categories")?.addEventListener("click", () => derive(onApproved));
  $("stop-no-legend")?.addEventListener("click", stop);
}

/** The legend rectangle is local until Apply is pressed. It uses full source
 * coordinates, just like the mask canvas, so moving it remains accurate at any
 * viewer zoom. */
export function bindLegendReviewOverlay() {
  const holder = $("mask-target");
  const review = state.data.legendReview;
  // In Run all, the legend box is introduced only after mask approval. When
  // Step 2 is reopened individually, both review cards and the box are shown.
  if (!state.individualRun && !state.data.mask?.approved) return;
  if (!holder || !review || review.status === "missing" || review.status === "stopped") return;
  const box = Array.isArray(state.legendBox) ? state.legendBox : review.box;
  if (!Array.isArray(box) || box.length !== 4 || !review.width || !review.height) return;
  holder.querySelector(".legend-review-box")?.remove();
  const node = document.createElement("div");
  node.className = "legend-review-box";
  node.innerHTML = `<span class="legend-review-label">Legend</span>
    <i data-handle="nw"></i><i data-handle="ne"></i><i data-handle="se"></i><i data-handle="sw"></i>`;
  holder.appendChild(node);

  let current = box.map(Number);
  const redraw = () => {
    const [x0, y0, x1, y1] = current;
    node.style.left = `${x0 / review.width * 100}%`;
    node.style.top = `${y0 / review.height * 100}%`;
    node.style.width = `${(x1 - x0) / review.width * 100}%`;
    node.style.height = `${(y1 - y0) / review.height * 100}%`;
  };
  redraw();
  node.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const start = current.slice();
    const handle = event.target.dataset.handle || "move";
    const viewport = holder.getBoundingClientRect();
    if (!viewport.width || !viewport.height) return;
    const sourcePoint = (pointer) => [
      (pointer.clientX - viewport.left) / viewport.width * review.width,
      (pointer.clientY - viewport.top) / viewport.height * review.height,
    ];
    const [startX, startY] = sourcePoint(event);
    node.setPointerCapture(event.pointerId);
    const move = (pointer) => {
      const [x, y] = sourcePoint(pointer);
      const dx = x - startX;
      const dy = y - startY;
      let [x0, y0, x1, y1] = start;
      if (handle === "move") {
        const boxW = x1 - x0;
        const boxH = y1 - y0;
        x0 = Math.max(0, Math.min(review.width - boxW, x0 + dx));
        y0 = Math.max(0, Math.min(review.height - boxH, y0 + dy));
        x1 = x0 + boxW;
        y1 = y0 + boxH;
      } else {
        if (handle.includes("w")) x0 += dx;
        if (handle.includes("e")) x1 += dx;
        if (handle.includes("n")) y0 += dy;
        if (handle.includes("s")) y1 += dy;
        x0 = Math.max(0, Math.min(x0, x1 - 16));
        x1 = Math.min(review.width, Math.max(x1, x0 + 16));
        y0 = Math.max(0, Math.min(y0, y1 - 16));
        y1 = Math.min(review.height, Math.max(y1, y0 + 16));
      }
      current = [Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)];
      state.legendBox = current;
      redraw();
    };
    const finish = () => {
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", finish);
      node.removeEventListener("pointercancel", finish);
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", finish, { once: true });
    node.addEventListener("pointercancel", finish, { once: true });
  });
}

async function refresh(message = "") {
  await loadMaps();
  await refreshSelectedData();
  state.legendBox = null;
  renderWorkspace(true);
  if (message) toast(message, "warning");
}

async function applyBox() {
  const review = state.data.legendReview;
  const box = state.legendBox || review?.box;
  if (!Array.isArray(box)) return;
  await withBusy($("apply-legend-box"), "Checking…", async () => {
    const result = await saveLegendBox(state.selected, box);
    await refresh(result.status === "ready"
      ? "Legend box updated. Approve it to continue."
      : "The adjusted legend still does not match the Step 1 categories.");
  });
}

async function approve(onApproved) {
  await withBusy($("approve-legend-box"), "Approving…", async () => {
    await approveLegendReview(state.selected);
    await refresh();
    await onApproved?.();
  });
}

async function derive(onApproved) {
  await withBusy($("derive-map-categories"), "Deriving…", async () => {
    await deriveLegendReview(state.selected);
    await refresh();
    await onApproved?.();
  });
}

async function stop() {
  await withBusy($("stop-no-legend"), "Stopping…", async () => {
    await stopForMissingLegend(state.selected);
    await refresh();
  });
}
