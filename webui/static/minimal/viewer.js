"use strict";

import { $ } from "./api.js";
import { state } from "./state.js";

/* The picture tools the detailed page puts above every page render: zoom in
   steps or by slider, snap back to 100% or to a fit, a 6 mm grid with 30 mm
   guides, and the snap that makes dragging land on that grid.  Every step in
   the focused view gets the same toolbar, so the controls do not move around
   as the reader walks the pipeline. */

const MIN_ZOOM = 25;
const MAX_ZOOM = 200;

export function viewerToolbarHtml(view, index) {
  const colour = view.hybrid ? `
    <label class="page-grid-toggle" title="Show the saved category colours">
      <input type="checkbox" data-colour-view ${state.colourView ? "checked" : ""}>
      <span aria-hidden="true"></span> Display colours
    </label>` : "";
  return `<div class="page-view-toolbar" data-viewer="${index}" aria-label="Page zoom and guides">
      <button class="page-zoom-out" type="button" aria-label="Zoom out">&minus;</button>
      <input class="page-zoom-range" type="range" min="${MIN_ZOOM}" max="${MAX_ZOOM}" step="5"
             value="100" aria-label="Page zoom">
      <button class="page-zoom-in" type="button" aria-label="Zoom in">+</button>
      <button class="page-zoom-100" type="button">100%</button>
      <button class="page-zoom-fit" type="button">Fit</button>
      <span class="page-zoom-readout">Fit</span>
      ${colour}
      <label class="page-grid-toggle"><input class="page-guides-toggle" type="checkbox">
        <span aria-hidden="true"></span> Grid &amp; guides</label>
      <label class="page-grid-toggle"><input class="page-snap-toggle" type="checkbox"
        ${state.snapToGrid ? "checked" : ""}><span aria-hidden="true"></span> Snap to 6 mm grid</label>
      <a class="quiet-link page-full-size" data-full-size="${index}" target="_blank" rel="noopener">open full size &#8599;</a>
    </div>`;
}

/** True while the reader has asked for drags to land on the 6 mm grid. */
export function snapToGrid(position, pxPerMm = 5) {
  if (!state.snapToGrid) return position;
  const interval = 6 * pxPerMm;
  return position.map((value) => Math.round(value / interval) * interval);
}

/** Wire one toolbar to the frame it sits under.  Zoom drives the frame width,
 *  so the picture and every overlay on it scale together. */
export function bindViewer(index, onColourChange) {
  const toolbar = document.querySelector(`.page-view-toolbar[data-viewer="${index}"]`);
  const frame = document.querySelector(`.map-stage[data-view="${index}"] .map-frame`);
  const sheet = frame?.querySelector(".map-canvas");
  const image = sheet?.querySelector("img");
  if (!toolbar || !frame || !sheet || !image) return;

  const range = toolbar.querySelector(".page-zoom-range");
  const readout = toolbar.querySelector(".page-zoom-readout");
  let zoom = 100;

  const apply = (value, label = null) => {
    zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(value) || 100));
    // Natural width is the page at 100%; the sheet carries the overlay too.
    const natural = image.naturalWidth || sheet.clientWidth || 1;
    sheet.style.width = `${natural * zoom / 100}px`;
    range.value = String(Math.round(zoom / 5) * 5);
    readout.textContent = label || `${Math.round(zoom)}%`;
  };
  const fit = () => {
    // Collapse the sheet first: once it has been zoomed wider than its
    // surround, measuring would just report the width the sheet itself forced.
    sheet.style.width = "";
    // Fit means the whole sheet is visible, so height constrains it as much as
    // width does -- a portrait page is otherwise cut off below the fold.
    const availableW = Math.max(200, frame.clientWidth - 30);
    const availableH = Math.max(200, frame.clientHeight - 30);
    const naturalW = image.naturalWidth || availableW;
    const naturalH = image.naturalHeight || availableH;
    apply(Math.min(100, availableW / naturalW * 100, availableH / naturalH * 100), "Fit");
  };

  toolbar.querySelector(".page-zoom-out").addEventListener("click", () => apply(zoom - 10));
  toolbar.querySelector(".page-zoom-in").addEventListener("click", () => apply(zoom + 10));
  toolbar.querySelector(".page-zoom-100").addEventListener("click", () => apply(100));
  toolbar.querySelector(".page-zoom-fit").addEventListener("click", fit);
  range.addEventListener("input", () => apply(Number(range.value)));

  toolbar.querySelector(".page-guides-toggle").addEventListener("change", (event) => {
    sheet.classList.toggle("show-grid", event.target.checked);
  });
  toolbar.querySelector(".page-snap-toggle").addEventListener("change", (event) => {
    state.snapToGrid = event.target.checked;
    document.querySelectorAll(".page-snap-toggle").forEach((box) => { box.checked = state.snapToGrid; });
  });
  toolbar.querySelector("[data-colour-view]")?.addEventListener("change", () => {
    state.colourView = !state.colourView;
    onColourChange?.();
  });
  const fullSize = toolbar.querySelector("[data-full-size]");
  if (fullSize) fullSize.href = image.src;

  // A 6 mm grid with 30 mm guides, expressed against the real page size.
  const widthMm = (image.naturalWidth || 1050) / 5;
  const heightMm = (image.naturalHeight || 1485) / 5;
  sheet.style.setProperty("--grid-x", `${6 / widthMm * 100}%`);
  sheet.style.setProperty("--grid-y", `${6 / heightMm * 100}%`);
  sheet.style.setProperty("--guide-x", `${30 / widthMm * 100}%`);
  sheet.style.setProperty("--guide-y", `${30 / heightMm * 100}%`);

  if (image.complete && image.naturalWidth) fit();
  else image.addEventListener("load", fit, { once: true });
}
