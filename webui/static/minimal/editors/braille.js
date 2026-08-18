"use strict";

import { $, addBrailleLabel, esc, saveBrailleLabel, saveBrailleTitle } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails, renderControls } from "../controls.js";
import { loadMaps, refreshSelectedData } from "../workspace.js";
import { refreshStepImages } from "../visual.js";
import { snapToGrid } from "../viewer.js";

/* Step 8 prints the place names in Grade 1 Braille.  Every edit re-renders the
   page on the server, so calls are queued rather than fired in parallel and
   text is debounced until typing stops. */

const SIDES = ["right", "left", "top", "bottom"];
const TITLE_ALIGNS = ["left", "center", "right"];
let saveChain = Promise.resolve();
const textTimers = new Map();

/** Queue a page-rendering call behind whatever is already in flight. */
function queued(task) {
  saveChain = saveChain.then(task, task);
  return saveChain;
}

export function brailleEditorHtml() {
  const layout = state.data.braille;
  if (!layout) {
    return editorDetails("braille", "8", "Braille labels", "Names on the printed page",
      '<div class="empty-editor">Run Step 8 to place the Braille labels.</div>');
  }
  const labels = layout.labels || [];
  const title = layout.title || {};
  const body = `
    <p class="section-intro">Drag a label on the page above to move it. Rename it here, hide it, or
      choose which side of its pin the text sits on. Each change redraws the page.</p>
    <div class="braille-list">
      ${labels.map((label) => `
        <div class="braille-row${label.enabled === false ? " is-hidden" : ""}" data-braille-row="${esc(label.id)}">
          <input type="text" value="${esc(label.text || "")}" maxlength="200"
                 aria-label="Braille text for ${esc(label.original_text || label.text || label.id)}">
          <span class="row-options">
            <label class="tiny-check"><input class="braille-enabled" type="checkbox"
              ${label.enabled === false ? "" : "checked"}> show</label>
            <select class="braille-side" aria-label="Which side of the pin the text sits on">
              ${SIDES.map((side) => `<option value="${side}"
                ${(label.side || "right") === side ? "selected" : ""}>${side}</option>`).join("")}
            </select>
          </span>
          <span class="braille-preview">${esc(label.braille_text || "")}</span>
        </div>`).join("") || '<div class="empty-editor">No labels were carried into Step 8 yet.</div>'}
    </div>
    <div class="action-row end"><span class="status-copy" id="braille-status"></span>
      <button class="button secondary small" id="braille-add" type="button">Add a label</button></div>
    <section class="braille-title-box">
      <h4>Page title</h4>
      <div class="braille-row">
        <input id="braille-title-text" type="text" value="${esc(title.text || "")}" maxlength="200"
               aria-label="Braille page title">
        <span class="row-options">
          <label class="tiny-check"><input id="braille-title-enabled" type="checkbox"
            ${title.enabled === false ? "" : "checked"}> show</label>
          <select id="braille-title-align" aria-label="Title alignment">
            ${TITLE_ALIGNS.map((align) => `<option value="${align}"
              ${(title.align || "center") === align ? "selected" : ""}>${align}</option>`).join("")}
          </select>
        </span>
        <span class="braille-preview">${esc(title.braille_text || "")}</span>
      </div>
    </section>`;
  return editorDetails("braille", "8", "Braille labels", "Names on the printed page", body);
}

export function bindBrailleEditor() {
  document.querySelectorAll("[data-braille-row]").forEach((row) => {
    const id = row.dataset.brailleRow;
    const field = row.querySelector('input[type="text"]');
    field?.addEventListener("input", () => {
      // Typing sends one call when the reader pauses, not one per keystroke.
      window.clearTimeout(textTimers.get(id));
      textTimers.set(id, window.setTimeout(() => patchLabel(id, { text: field.value }), 500));
    });
    row.querySelector(".braille-enabled")?.addEventListener("change", (event) => {
      row.classList.toggle("is-hidden", !event.target.checked);
      patchLabel(id, { enabled: event.target.checked });
    });
    row.querySelector(".braille-side")?.addEventListener("change", (event) => {
      patchLabel(id, { side: event.target.value });
    });
  });

  $("braille-add")?.addEventListener("click", async () => {
    await withBusy($("braille-add"), "Adding…", async () => {
      await addBrailleLabel(state.selected, "New label");
      await reloadBraille();
      renderControls();
      toast("Label added. Drag it into place on the page.");
    });
  });

  const titleText = $("braille-title-text");
  titleText?.addEventListener("input", () => {
    window.clearTimeout(textTimers.get("title"));
    textTimers.set("title", window.setTimeout(() => patchTitle({ text: titleText.value }), 500));
  });
  $("braille-title-enabled")?.addEventListener("change", (event) =>
    patchTitle({ enabled: event.target.checked }));
  $("braille-title-align")?.addEventListener("change", (event) =>
    patchTitle({ align: event.target.value }));
}

function patchLabel(labelId, patch) {
  return queued(async () => {
    statusLine("braille-status", "Redrawing the page…");
    try {
      const result = await saveBrailleLabel(state.selected, labelId, patch);
      applyLabel(result.label);
      await refreshStepImages();
      bindBrailleOverlay();
      statusLine("braille-status", `${result.enabled_labels} labels on the page.`, "success");
    } catch (error) {
      statusLine("braille-status", error.message, "error");
      toast(error.message, "error");
    }
  });
}

function patchTitle(patch) {
  return queued(async () => {
    statusLine("braille-status", "Redrawing the page…");
    try {
      const result = await saveBrailleTitle(state.selected, patch);
      if (state.data.braille) state.data.braille.title = result.title;
      await refreshStepImages();
      bindBrailleOverlay();
      statusLine("braille-status", "Title updated.", "success");
    } catch (error) {
      statusLine("braille-status", error.message, "error");
      toast(error.message, "error");
    }
  });
}

/** Replace one label in place so the overlay and the list stay in step without
 *  redrawing the panel the reader is still typing into. */
function applyLabel(label) {
  const labels = state.data.braille?.labels;
  if (!labels || !label) return;
  const index = labels.findIndex((item) => String(item.id) === String(label.id));
  if (index >= 0) labels[index] = label;
  const row = document.querySelector(`[data-braille-row="${CSS.escape(String(label.id))}"]`);
  const preview = row?.querySelector(".braille-preview");
  if (preview) preview.textContent = label.braille_text || "";
}

async function reloadBraille() {
  await loadMaps();
  await refreshSelectedData();
  await refreshStepImages();
}

/* -------------------------------------------------------- page overlay --- */

/** Label positions are stored relative to the map, while the page also carries
 *  the title and the furniture, so drawing adds the map origin back on. */
export function bindBrailleOverlay() {
  const overlay = $("braille-overlay");
  const layout = state.data.braille;
  if (!overlay || !layout) return;
  const page = layout.page || {};
  const [pageW, pageH] = page.canvas_px || layout.canvas_px || [1, 1];
  const [originX, originY] = page.map_origin_px || [0, 0];
  const [mapW, mapH] = layout.canvas_px || [pageW, pageH];
  overlay.setAttribute("viewBox", `0 0 ${pageW} ${pageH}`);
  overlay.setAttribute("preserveAspectRatio", "none");
  overlay.style.pointerEvents = "auto";

  overlay.innerHTML = (layout.labels || []).map((label) => {
    const metrics = label.render_metrics || {};
    const [boxX, boxY] = metrics.box_offset_px || [0, 0];
    const [boxW, boxH] = metrics.box_size_px || [0, 0];
    const outer = Number(metrics.pin_outer_radius_px) || 6;
    const x = Number(label.position_px?.[0] || 0) + originX;
    const y = Number(label.position_px?.[1] || 0) + originY;
    // The PNG already draws the box, its braille and the pin; this layer only
    // outlines what can be grabbed and provides the hit area for dragging.
    return `<g class="braille-pin${label.enabled === false ? " is-hidden" : ""}"
        data-braille-pin="${esc(label.id)}" transform="translate(${x} ${y})"
        role="button" tabindex="0" aria-label="Move the label ${esc(label.text || label.id)}">
      <rect x="${boxX}" y="${boxY}" width="${boxW}" height="${boxH}"></rect>
      <circle r="${outer}"></circle>
    </g>`;
  }).join("");

  overlay.querySelectorAll("[data-braille-pin]").forEach((pin) => {
    bindPinDrag(overlay, pin, layout, [originX, originY], [pageW, pageH], [mapW, mapH]);
  });
}

function bindPinDrag(overlay, pin, layout, origin, pageSize, mapSize) {
  const id = pin.dataset.braillePin;
  const label = (layout.labels || []).find((item) => String(item.id) === id);
  if (!label) return;
  let start = null;

  const place = (x, y) => {
    label.position_px = [x, y];
    pin.setAttribute("transform", `translate(${x + origin[0]} ${y + origin[1]})`);
  };

  pin.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    pin.setPointerCapture(event.pointerId);
    pin.classList.add("is-dragging");
    start = {
      x: event.clientX, y: event.clientY,
      position: [...(label.position_px || [0, 0])],
      box: overlay.getBoundingClientRect(),
    };
  });
  pin.addEventListener("pointermove", (event) => {
    if (!start || !start.box.width || !start.box.height) return;
    const dx = (event.clientX - start.x) / start.box.width * pageSize[0];
    const dy = (event.clientY - start.y) / start.box.height * pageSize[1];
    const [sx, sy] = snapToGrid([start.position[0] + dx, start.position[1] + dy],
                                Number(layout.render_px_per_mm) || 5);
    place(Math.min(Math.max(sx, 0), mapSize[0]), Math.min(Math.max(sy, 0), mapSize[1]));
  });
  const drop = () => {
    if (!start) return;
    start = null;
    pin.classList.remove("is-dragging");
    patchLabel(id, { position_px: label.position_px });
  };
  pin.addEventListener("pointerup", drop);
  pin.addEventListener("pointercancel", drop);

  pin.addEventListener("keydown", (event) => {
    const nudge = { ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5] }[event.key];
    if (!nudge) return;
    event.preventDefault();
    place(
      Math.min(Math.max(label.position_px[0] + nudge[0], 0), mapSize[0]),
      Math.min(Math.max(label.position_px[1] + nudge[1], 0), mapSize[1]),
    );
    patchLabel(id, { position_px: label.position_px });
  });
}
