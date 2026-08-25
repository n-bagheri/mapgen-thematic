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
const MIN_FIT_ZOOM = .01;

export function viewerToolbarHtml(view, index) {
  const hybridEnabled = state.data.step7Review?.create_hybrid_map === true;
  const colour = view.hybrid ? `
    <label class="page-grid-toggle" title="${hybridEnabled
      ? "Show the saved category colors" : "Turn on Create hybrid map in Step 7 first"}">
      <input type="checkbox" data-colour-view ${state.colourView ? "checked" : ""}
        ${hybridEnabled ? "" : "disabled"}>
      <span aria-hidden="true"></span> Display colors
    </label>` : "";
  const original = view.originalCompare ? `
    <button class="page-original-toggle" type="button" data-original-view
      aria-pressed="${state.showOriginalMap}">${state.showOriginalMap
        ? "Hide original map" : "Display original map"}</button>` : "";
  const finalMap = view.mapLegendCompare ? `
    <button class="page-original-toggle" type="button" data-final-map-view
      aria-pressed="${state.showFinalMap}">${state.showFinalMap
        ? "Hide tactile map" : "Display tactile map"}</button>` : "";
  return `<div class="page-view-toolbar" data-viewer="${index}" aria-label="Page zoom and guides">
      ${original}${finalMap}
      <button class="page-pan-toggle" type="button" aria-pressed="${state.panMode}"
        title="Drag the zoomed map to move around it">Pan</button>
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

/** Wire one toolbar to the frame it sits under. Zoom changes only the inner
 *  map sheet; the stage and its scroll viewport retain their dimensions. */
export function bindViewer(index, onColourChange) {
  const toolbar = document.querySelector(`.page-view-toolbar[data-viewer="${index}"]`);
  const frame = document.querySelector(`.map-stage[data-view="${index}"] .map-frame`);
  const zoomSpace = frame?.querySelector(".map-zoom-space");
  const sheet = frame?.querySelector(".map-canvas");
  const image = sheet?.querySelector("img[data-viewer-image]") || sheet?.querySelector("img");
  if (!toolbar || !frame || !zoomSpace || !sheet || !image) return;

  const range = toolbar.querySelector(".page-zoom-range");
  const readout = toolbar.querySelector(".page-zoom-readout");
  const panButton = toolbar.querySelector(".page-pan-toggle");
  let zoom = 100;
  let panStart = null;

  const syncPanMode = () => {
    document.querySelectorAll(".page-pan-toggle").forEach((button) => {
      button.setAttribute("aria-pressed", String(state.panMode));
    });
    document.querySelectorAll(".map-frame").forEach((node) => {
      node.classList.toggle("is-pan-enabled", state.panMode);
      if (!state.panMode) node.classList.remove("is-panning");
    });
  };

  const apply = (value, label = null) => {
    const currentMin = Math.max(MIN_FIT_ZOOM, Number(range.min) || MIN_ZOOM);
    zoom = Math.min(MAX_ZOOM, Math.max(currentMin, Number(value) || 100));
    // Natural dimensions remain the immutable page/canvas coordinate system.
    // Only the visual transform changes; a separate space supplies the scaled
    // scroll extent without resizing or reflowing anything on the paper.
    const naturalW = Number(sheet.dataset.naturalWidth)
      || image.naturalWidth || sheet.clientWidth || 1;
    const naturalH = Number(sheet.dataset.naturalHeight)
      || image.naturalHeight || sheet.clientHeight || 1;
    const scale = zoom / 100;
    sheet.style.width = `${naturalW}px`;
    sheet.style.height = `${naturalH}px`;
    sheet.style.transform = `scale(${scale})`;
    zoomSpace.style.width = `${naturalW * scale}px`;
    zoomSpace.style.height = `${naturalH * scale}px`;
    range.value = String(zoom);
    readout.textContent = label || `${Math.round(zoom)}%`;
  };
  const fit = () => {
    // Collapse only the scroll extent while measuring. The paper itself keeps
    // its natural dimensions and is never reflowed by a Fit operation.
    sheet.style.transform = "scale(0)";
    zoomSpace.style.width = "0px";
    zoomSpace.style.height = "0px";
    // Fit means the whole sheet is visible, so height constrains it as much as
    // width does -- a portrait page is otherwise cut off below the fold.
    const availableW = Math.max(200, frame.clientWidth - 30);
    const availableH = Math.max(200, frame.clientHeight - 30);
    const naturalW = Number(sheet.dataset.naturalWidth) || image.naturalWidth || availableW;
    const naturalH = Number(sheet.dataset.naturalHeight) || image.naturalHeight || availableH;
    const fittedZoom = Math.min(100, availableW / naturalW * 100, availableH / naturalH * 100);
    // High-resolution scans can need far less than the normal 25% floor. Fit
    // establishes the smallest useful zoom for this image, so the whole sheet
    // remains visible and the slider accurately represents that fitted scale.
    range.min = String(Math.max(MIN_FIT_ZOOM, Math.min(MIN_ZOOM, fittedZoom)));
    range.step = "any";
    apply(fittedZoom, "Fit");
  };

  toolbar.querySelector(".page-zoom-out").addEventListener("click", () => apply(zoom - 10));
  toolbar.querySelector(".page-zoom-in").addEventListener("click", () => apply(zoom + 10));
  toolbar.querySelector(".page-zoom-100").addEventListener("click", () => apply(100));
  toolbar.querySelector(".page-zoom-fit").addEventListener("click", fit);
  range.addEventListener("input", () => apply(Number(range.value)));
  panButton.addEventListener("click", () => {
    state.panMode = !state.panMode;
    syncPanMode();
  });

  frame.addEventListener("pointerdown", (event) => {
    if (!state.panMode || event.button !== 0) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    frame.setPointerCapture(event.pointerId);
    panStart = {
      x: event.clientX,
      y: event.clientY,
      left: frame.scrollLeft,
      top: frame.scrollTop,
    };
    frame.classList.add("is-panning");
  }, { capture: true });
  frame.addEventListener("pointermove", (event) => {
    if (!panStart) return;
    event.preventDefault();
    frame.scrollLeft = panStart.left - (event.clientX - panStart.x);
    frame.scrollTop = panStart.top - (event.clientY - panStart.y);
  }, { capture: true });
  const stopPanning = (event) => {
    if (!panStart) return;
    panStart = null;
    frame.classList.remove("is-panning");
    if (frame.hasPointerCapture(event.pointerId)) frame.releasePointerCapture(event.pointerId);
  };
  frame.addEventListener("pointerup", stopPanning, { capture: true });
  frame.addEventListener("pointercancel", stopPanning, { capture: true });

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
  toolbar.querySelector("[data-original-view]")?.addEventListener("click", () => {
    state.showOriginalMap = !state.showOriginalMap;
    onColourChange?.();
  });
  toolbar.querySelector("[data-final-map-view]")?.addEventListener("click", () => {
    state.showFinalMap = !state.showFinalMap;
    onColourChange?.();
  });
  const fullSize = toolbar.querySelector("[data-full-size]");
  if (fullSize) fullSize.href = image.dataset.fullSizeUrl || image.src;

  // A 6 mm grid with 30 mm guides, expressed against the real page size.
  const widthMm = (Number(sheet.dataset.naturalWidth) || image.naturalWidth || 1050) / 5;
  const heightMm = (Number(sheet.dataset.naturalHeight) || image.naturalHeight || 1485) / 5;
  sheet.style.setProperty("--grid-x", `${6 / widthMm * 100}%`);
  sheet.style.setProperty("--grid-y", `${6 / heightMm * 100}%`);
  sheet.style.setProperty("--guide-x", `${30 / widthMm * 100}%`);
  sheet.style.setProperty("--guide-y", `${30 / heightMm * 100}%`);

  if (image.complete && image.naturalWidth) fit();
  else image.addEventListener("load", fit, { once: true });
  syncPanMode();
}
