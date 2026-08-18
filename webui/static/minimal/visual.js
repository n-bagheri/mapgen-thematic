"use strict";

import { $, artifactUrl, esc, mapUrl } from "./api.js";
import { STEP_DEFS, STEP_VIEWS, viewedStep } from "./steps.js";
import { currentStepKey, state } from "./state.js";
import { bindMaskCanvas } from "./editors/mask.js";
import { bindBrailleOverlay } from "./editors/braille.js";
import { bindLegendOverlay } from "./editors/legend.js";
import { bindViewer, viewerToolbarHtml } from "./viewer.js";

/* The left pane shows one step at a time: whichever step the reader opened on
   the right.  Each picture that step produces gets its own framed panel, in the
   order the detailed page shows them -- what went in, then what came out. */

function selectedFromState() {
  return state.maps.find((map) => map.stem === state.selected) || null;
}

export function activeStep(map) {
  return viewedStep(map, state.activeStep);
}

function stepStatus(map, step) {
  const key = String(step);
  if (currentStepKey(map) === key) return { label: "Building", className: "running" };
  if (map.steps?.[key]) return { label: "Ready", className: "" };
  return { label: "Waiting", className: "waiting" };
}

/** Step 6 caches a preview per level, so the slider can show a level that has
 *  not been applied yet.  The presets payload names the file for us. */
export function simplifiedArtifactName() {
  const level = Number(state.previewLevel);
  const variant = state.data.presets?.variants?.[String(level)];
  return variant?.preview_artifact || "label_map_gen_preview.png";
}

/** Resolve one STEP_VIEWS entry to the file the browser should request. */
function viewSource(map, view) {
  if (view.source) return { url: mapUrl(map.name), name: map.name };
  if (view.dynamic === "simplified") {
    const name = simplifiedArtifactName();
    return { url: artifactUrl(map.stem, name), name, fallback: artifactUrl(map.stem, "step6_debug.png") };
  }
  const name = state.colourView && view.hybrid ? view.hybrid : view.artifact;
  return { url: artifactUrl(map.stem, name), name };
}

function overlayMarkup(view) {
  if (view.overlay === "layers") {
    return '<svg class="map-overlay" id="map-overlay" aria-label="Editable map layers"></svg>';
  }
  if (view.overlay === "braille") {
    return '<svg class="map-overlay braille-overlay" id="braille-overlay" aria-label="Braille label layer"></svg>';
  }
  if (view.overlay === "legend") {
    return '<svg class="map-overlay braille-overlay" id="legend-overlay" aria-label="Legend entry layer"></svg>';
  }
  return "";
}

function layerButton(layer, label) {
  return `<button class="layer-toggle" type="button" data-layer="${layer}"
      aria-pressed="${state.layers[layer]}">${label}</button>`;
}

/** The layer switches only exist where there is an editable vector overlay. */
function layerToolbar(view) {
  if (view.overlay !== "layers") return "";
  return `<div class="layer-toolbar" aria-label="Simplified map layers">
      <span>Layers</span>
      ${layerButton("map", "Colors")}
      ${layerButton("labels", "Labels")}
      ${layerButton("lines", "Lines")}
      ${layerButton("boundaries", "Boundaries")}
    </div>`;
}

export function renderVisual() {
  const map = selectedFromState();
  if (!map) return;
  const step = activeStep(map);
  const definition = STEP_DEFS.find((item) => item.key === String(step));
  const views = STEP_VIEWS[step] || [];
  const status = stepStatus(map, step);
  const ready = Boolean(map.steps?.[String(step)]) || step === 1;

  const panels = ready ? views.map((view, index) => {
    const source = viewSource(map, view);
    const frameId = view.overlay === "layers" ? ' id="simplified-frame"' : "";
    const canvasId = view.overlay ? ` id="${view.overlay}-target"` : "";
    const imageId = view.overlay === "layers" ? "simplified-image" : `step-image-${index}`;
    return `
      <article class="map-stage" data-view="${index}"${view.optional ? ' data-optional="1"' : ""}>
        <header class="stage-heading">
          <div><span class="stage-index">${String(index + 1).padStart(2, "0")}</span>
            <h2>${esc(view.caption)}</h2></div>
        </header>
        ${viewerToolbarHtml(view, index)}
        <div class="map-frame"${frameId}><div class="map-canvas"${canvasId}>
          <img id="${imageId}" src="${source.url}" alt="${esc(view.caption)} for ${esc(map.name)}"
               ${source.fallback ? `data-fallback="${source.fallback}"` : ""}>
          ${overlayMarkup(view)}
          <div class="page-grid" aria-hidden="true"></div>
        </div></div>
        ${layerToolbar(view)}
      </article>`;
  }).join("") : `
      <article class="map-stage is-locked">
        <div class="locked-preview">
          <div><span aria-hidden="true">${esc(definition?.number || "")}</span>
            <p>${esc(lockedMessage(map, step))}</p></div>
        </div>
      </article>`;

  $("visual-content").innerHTML = `
    <div class="visual-header">
      <p class="minimal-eyebrow">Step ${esc(definition?.number || step)}</p>
      <h2>${esc(definition?.title || "")}</h2>
      <span class="stage-badge ${status.className}">${status.label}</span>
    </div>
    ${panels}`;
  bindVisualEvents();
}

function lockedMessage(map, step) {
  if (step === 6 && map.steps?.["5"] && !map.step5_review_ready) {
    return "Approve the suggested tactile categories to continue.";
  }
  const previous = STEP_DEFS.find((item) => Number(item.key) === step - 1);
  return previous && !map.steps?.[previous.key]
    ? `Run step ${previous.number} first; this step builds on its result.`
    : "Run this step to see its result here.";
}

function bindVisualEvents() {
  // An optional artifact that a run never produced simply drops out.
  document.querySelectorAll('.map-stage[data-optional] img').forEach((image) => {
    image.addEventListener("error", () => {
      image.closest(".map-stage")?.remove();
    }, { once: true });
  });
  document.querySelectorAll(".layer-toggle[data-layer]").forEach((button) => {
    button.addEventListener("click", () => {
      const layer = button.dataset.layer;
      state.layers[layer] = !state.layers[layer];
      button.setAttribute("aria-pressed", String(state.layers[layer]));
      updateLayerVisibility();
    });
  });
  const simplifiedImage = $("simplified-image");
  if (simplifiedImage) {
    simplifiedImage.addEventListener("load", renderMapOverlay);
    simplifiedImage.addEventListener("error", () => {
      if (simplifiedImage.dataset.fallback && simplifiedImage.src !== simplifiedImage.dataset.fallback) {
        simplifiedImage.src = simplifiedImage.dataset.fallback;
        return;
      }
      simplifiedImage.dataset.overlayDisabled = "true";
    }, { once: true });
    if (simplifiedImage.complete && simplifiedImage.naturalWidth) renderMapOverlay();
  }
  document.querySelectorAll(".page-view-toolbar").forEach((bar) => {
    bindViewer(Number(bar.dataset.viewer), renderVisual);
  });
  bindMaskCanvas();
  bindBrailleOverlay();
  bindLegendOverlay();
}

/** Show one step's pictures without redrawing the pane around them, so an edit
 *  that only re-renders a PNG does not scroll the reader away. */
export async function refreshStepImages() {
  const map = selectedFromState();
  if (!map) return false;
  const views = STEP_VIEWS[activeStep(map)] || [];
  let refreshed = false;
  await Promise.all(views.map(async (view, index) => {
    const id = view.overlay === "layers" ? "simplified-image" : `step-image-${index}`;
    const image = $(id);
    if (!image) return;
    const next = viewSource(map, view).url;
    await new Promise((resolve) => {
      const preload = new Image();
      preload.addEventListener("load", resolve, { once: true });
      preload.addEventListener("error", resolve, { once: true });
      preload.src = next;
    });
    image.src = next;
    refreshed = true;
  }));
  return refreshed;
}

export function setActiveStep(step, shouldScroll = false) {
  state.activeStep = Number(step);
  renderVisual();
  if (shouldScroll) {
    $("visual-pane")?.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function linePath(points) {
  if (!Array.isArray(points) || points.length < 2) return "";
  return points.map((point, index) =>
    `${index ? "L" : "M"} ${Number(point[0]).toFixed(2)} ${Number(point[1]).toFixed(2)}`).join(" ");
}

function geometryLines(feature) {
  const geometry = feature?.geometry || {};
  if (geometry.type === "LineString") return [geometry.coordinates || []];
  if (geometry.type === "MultiLineString") return geometry.coordinates || [];
  return [];
}

function labelPosition(label) {
  if (Array.isArray(label?.text_position)) return label.text_position;
  if (Array.isArray(label?.quad) && label.quad.length) {
    const x = label.quad.reduce((sum, point) => sum + Number(point[0]), 0) / label.quad.length;
    const y = label.quad.reduce((sum, point) => sum + Number(point[1]), 0) / label.quad.length;
    return [x, y];
  }
  const box = label?.box;
  return Array.isArray(box) && box.length === 4
    ? [(Number(box[0]) + Number(box[2])) / 2, (Number(box[1]) + Number(box[3])) / 2]
    : null;
}

export function renderMapOverlay() {
  const image = $("simplified-image");
  const overlay = $("map-overlay");
  if (!image || !overlay || image.dataset.overlayDisabled === "true") return;
  const review = state.data.lines;
  const width = Number(review?.width) || image.naturalWidth;
  const height = Number(review?.height) || image.naturalHeight;
  if (!width || !height) return;
  overlay.setAttribute("viewBox", `0 0 ${width} ${height}`);
  overlay.setAttribute("preserveAspectRatio", "none");

  const fixedFeatures = state.data.lineGeo?.features || review?.fixed_features || [];
  const boundaryPaths = fixedFeatures.flatMap((feature) => {
    const kind = feature?.properties?.kind;
    if (!["border", "border_or_coast"].includes(kind)) return [];
    return geometryLines(feature).map((points) =>
      `<path class="boundary-path" d="${linePath(points)}"></path>`);
  }).join("");

  const extracted = review?.automatic_rivers || [];
  const includeLines = review?.include_rivers !== false;
  const linePaths = extracted.map((line) => `
    <path class="line-path${includeLines && line.include ? "" : " is-excluded"}"
      d="${linePath(line.points)}"></path>`).join("");
  const manualPaths = (review?.manual_rivers || []).map((line) => `
    <path class="line-path${includeLines ? "" : " is-excluded"}" d="${linePath(line.points)}"></path>`).join("");

  const occurrences = state.data.labels?.occurrences || [];
  const fontSize = Math.max(9, Math.min(width, height) / 42);
  const labels = occurrences.filter((item) => item.include !== false).map((item) => {
    const position = labelPosition(item.label);
    if (!position) return "";
    const text = item.review_text || item.original_text || item.label?.text || "";
    return `<text x="${Number(position[0]).toFixed(2)}" y="${Number(position[1]).toFixed(2)}"
      font-size="${fontSize.toFixed(2)}" text-anchor="middle" dominant-baseline="middle">${esc(text)}</text>`;
  }).join("");

  overlay.innerHTML = `
    <g class="boundary-layer">${boundaryPaths}</g>
    <g class="line-layer">${linePaths}${manualPaths}</g>
    <g class="label-layer">${labels}</g>`;
  updateLayerVisibility();
}

function updateLayerVisibility() {
  const overlay = $("map-overlay");
  const frame = $("simplified-frame");
  if (!overlay || !frame) return;
  frame.classList.toggle("map-layer-off", !state.layers.map);
  overlay.classList.toggle("hide-labels", !state.layers.labels);
  overlay.classList.toggle("hide-lines", !state.layers.lines);
  overlay.classList.toggle("hide-boundaries", !state.layers.boundaries);
}
