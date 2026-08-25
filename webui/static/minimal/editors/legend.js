"use strict";

import {
  $, esc, legendSwatchUrl, saveLegendItem, saveLegendOrientation, saveStep9Review,
} from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails, renderControls } from "../controls.js";
import { loadMaps, refreshSelectedData, renderWorkspace } from "../workspace.js";
import { refreshStepImages, renderVisual } from "../visual.js";
import { snapToGrid } from "../viewer.js";
import { grade1Preview } from "./braille.js";

/* Step 9 gives the legend a page of its own: one tactile sample per category
   beside its Braille name.  Entries and the title are placed in page pixels,
   so both the list here and the drag overlay speak the same coordinates. */

const TITLE_ALIGNS = ["left", "center", "right"];
let saveChain = Promise.resolve();
const textTimers = new Map();
const pendingTextValues = new Map();
const liveTextValues = new Map();

function queued(task) {
  saveChain = saveChain.then(task, task);
  return saveChain;
}

function legendToolboxBody(includeApproval = false) {
  const layout = state.data.legend;
  if (!layout) return '<div class="empty-editor">Run Step 9 to build the legend page.</div>';
  const entries = layout.entries || [];
  const title = layout.title || {};
  const orientation = layout.page?.orientation || "portrait";
  const review = state.data.step9Review || {};
  return `
    <button class="button secondary full" id="legend-compare" type="button"
      aria-pressed="${state.showFinalMap}">${state.showFinalMap
        ? "Hide tactile map" : "Display tactile map next to legend"}</button>
    <p class="section-intro">Drag an entry on the legend page above to move it. Rename it here or hide
      it from the sheet.</p>
    <div class="form-grid">
      <label class="field"><span>Legend page orientation</span>
        <select id="legend-orientation">
          <option value="portrait" ${orientation === "portrait" ? "selected" : ""}>Portrait</option>
          <option value="landscape" ${orientation === "landscape" ? "selected" : ""}>Landscape</option>
        </select>
      </label>
    </div>
    <section class="braille-title-box">
      <h4>Legend title</h4>
      <div class="braille-row">
        <input id="legend-title-text" type="text" value="${esc(title.text || "")}" maxlength="200"
               aria-label="Legend page title">
        <span class="row-options">
          <label class="tiny-check"><input id="legend-title-enabled" type="checkbox"
            ${title.enabled === false ? "" : "checked"}> show</label>
          <select id="legend-title-align" aria-label="Legend title alignment">
            ${TITLE_ALIGNS.map((align) => `<option value="${align}"
              ${(title.align || "left") === align ? "selected" : ""}>${align}</option>`).join("")}
          </select>
        </span>
        <span class="braille-preview" id="legend-title-preview">${esc(title.braille_text || "")}</span>
      </div>
    </section>
    <div class="braille-list">
      ${entries.map((entry) => `
        <div class="legend-entry${entry.enabled === false ? " is-hidden" : ""}" data-legend-row="${esc(entry.id)}">
          <img src="${legendSwatchUrl(state.selected, entry.id, state.colourView)}"
               alt="Tactile sample for ${esc(entry.text || entry.id)}">
          <input type="text" value="${esc(entry.text || "")}" maxlength="200"
                 aria-label="Legend name for ${esc(entry.original_text || entry.id)}">
          <label class="tiny-check"><input class="legend-enabled" type="checkbox"
            ${entry.enabled === false ? "" : "checked"}> show</label>
          <span class="braille-preview">${esc(entry.braille_text || "")}</span>
          <span class="pattern-chip"><code>${esc(entry.pattern || "")}</code>
            ${entry.pattern_desc ? `&mdash; ${esc(entry.pattern_desc)}` : ""}</span>
        </div>`).join("") || '<div class="empty-editor">No categories are marked for the legend.</div>'}
    </div>
    <div class="action-row end"><span class="status-copy" id="legend-status"></span></div>
    <p class="status-copy" id="legend-review-status">${review.approved
      ? "This legend page is approved." : "Review the legend entries and page arrangement."}</p>
    ${includeApproval ? `<button class="button primary full" id="approve-legend" type="button">
      Approve legend &amp; enable export</button>` : ""}`;
}

export function legendDecisionHtml() {
  if (!state.data.legend) return "";
  return `<section class="review-gate legend-decision" id="legend-decision"
      aria-labelledby="legend-decision-title">
    <span class="section-kicker">One final decision needed</span>
    <h3 id="legend-decision-title">Review the separate legend page.</h3>
    <p class="section-intro">Check the tactile samples, Braille names, title, and page orientation.
      You can display the finished tactile map beside it before enabling export.</p>
    ${legendToolboxBody(true)}
  </section>`;
}

export function legendEditorHtml() {
  return editorDetails("legend", "9", "Legend page", "Samples and their Braille names",
    legendToolboxBody(true));
}

export function bindLegendEditor(onApproved) {
  document.querySelectorAll("[data-legend-row]").forEach((row) => {
    const id = row.dataset.legendRow;
    const entry = (state.data.legend?.entries || [])
      .find((item) => String(item.id) === id);
    const field = row.querySelector('input[type="text"]');
    field?.addEventListener("input", () => {
      if (entry) {
        entry.text = field.value;
        entry.braille_text = grade1Preview(field.value);
      }
      liveTextValues.set(id, field.value);
      const preview = row.querySelector(".braille-preview");
      if (preview) preview.textContent = entry?.braille_text || "";
      bindLegendOverlay();
      window.clearTimeout(textTimers.get(id));
      pendingTextValues.set(id, field.value);
      textTimers.set(id, window.setTimeout(() => {
        const value = pendingTextValues.get(id);
        pendingTextValues.delete(id);
        patchItem(id, { text: value });
      }, 500));
    });
    row.querySelector(".legend-enabled")?.addEventListener("change", (event) => {
      row.classList.toggle("is-hidden", !event.target.checked);
      if (entry) entry.enabled = event.target.checked;
      bindLegendOverlay();
      patchItem(id, { enabled: event.target.checked });
    });
  });

  const titleText = $("legend-title-text");
  titleText?.addEventListener("input", () => {
    const title = state.data.legend?.title;
    if (title) {
      title.text = titleText.value;
      title.braille_text = grade1Preview(titleText.value, true);
    }
    liveTextValues.set("title", titleText.value);
    const preview = $("legend-title-preview");
    if (preview) preview.textContent = title?.braille_text || "";
    bindLegendOverlay();
    window.clearTimeout(textTimers.get("title"));
    pendingTextValues.set("title", titleText.value);
    textTimers.set("title", window.setTimeout(() => {
      const value = pendingTextValues.get("title");
      pendingTextValues.delete("title");
      patchItem("title", { text: value });
    }, 500));
  });
  $("legend-title-enabled")?.addEventListener("change", (event) => {
    if (state.data.legend?.title) state.data.legend.title.enabled = event.target.checked;
    bindLegendOverlay();
    patchItem("title", { enabled: event.target.checked });
  });
  $("legend-title-align")?.addEventListener("change", (event) =>
    patchItem("title", { align: event.target.value }));

  $("legend-orientation")?.addEventListener("change", async (event) => {
    await withBusy(null, "", async () => {
      statusLine("legend-status", "Rebuilding the legend page…");
      const result = await saveLegendOrientation(state.selected, event.target.value);
      state.data.legend = result.layout;
      markReviewDirty();
      await loadMaps();
      await refreshStepImages();
      renderControls();
      toast("Legend page orientation changed.");
    });
  });
  $("legend-compare")?.addEventListener("click", (event) => {
    state.showFinalMap = !state.showFinalMap;
    event.currentTarget.setAttribute("aria-pressed", String(state.showFinalMap));
    event.currentTarget.textContent = state.showFinalMap
      ? "Hide tactile map" : "Display tactile map next to legend";
    renderVisual();
  });
  $("approve-legend")?.addEventListener("click", async () => {
    await withBusy($("approve-legend"), "Approving…", async () => {
      await flushPendingText();
      await saveChain;
      state.data.step9Review = await saveStep9Review(state.selected, true);
      await loadMaps();
      await refreshSelectedData();
      toast("Legend page approved. Export is now available.");
      if (onApproved) await onApproved();
      else renderWorkspace(true);
    });
  });
}

async function flushPendingText() {
  const pending = [...pendingTextValues.entries()];
  pendingTextValues.clear();
  const saves = [];
  pending.forEach(([id, value]) => {
    window.clearTimeout(textTimers.get(id));
    textTimers.delete(id);
    const target = id === "title" ? "title" : id;
    saves.push(patchItem(target, { text: value }));
  });
  await Promise.all(saves);
}

function markReviewDirty() {
  if (state.data.step9Review) state.data.step9Review.approved = false;
  const map = state.maps.find((item) => item.stem === state.selected);
  if (map) map.step9_review_ready = false;
}

function patchItem(target, patch) {
  return queued(async () => {
    statusLine("legend-status", "Redrawing the legend page…");
    try {
      const result = await saveLegendItem(state.selected, target, patch);
      applyItem(result.item);
      markReviewDirty();
      await refreshStepImages();
      bindLegendOverlay();
      statusLine("legend-status", `${result.entries} entries on the page.`, "success");
    } catch (error) {
      statusLine("legend-status", error.message, "error");
      toast(error.message, "error");
      await refreshSelectedData();
    }
  });
}

function applyItem(item) {
  const layout = state.data.legend;
  if (!layout || !item) return;
  const isTitle = item.id === layout.title?.id;
  let applied = item;
  if (isTitle) {
    if (layout.title) Object.assign(layout.title, item);
    else layout.title = item;
    applied = layout.title;
  } else {
    const index = (layout.entries || []).findIndex((entry) => entry.id === item.id);
    if (index >= 0) {
      Object.assign(layout.entries[index], item);
      applied = layout.entries[index];
    }
  }
  const liveKey = isTitle ? "title" : String(item.id);
  const liveValue = liveTextValues.get(liveKey);
  if (liveValue === item.text) {
    liveTextValues.delete(liveKey);
  } else if (liveValue !== undefined) {
    applied.text = liveValue;
    applied.braille_text = grade1Preview(liveValue, isTitle);
  }
  const row = document.querySelector(`[data-legend-row="${CSS.escape(String(item.id))}"] img`);
  if (row) row.src = legendSwatchUrl(state.selected, item.id, state.colourView);
  const preview = isTitle
    ? $("legend-title-preview")
    : document.querySelector(`[data-legend-row="${CSS.escape(String(item.id))}"] .braille-preview`);
  if (preview) preview.textContent = applied.braille_text || "";
}

/* -------------------------------------------------------- page overlay --- */

/** Every legend coordinate is already page-relative, so the overlay is a
 *  straight one-to-one map onto the rendered sheet. */
export function bindLegendOverlay() {
  const overlay = $("legend-overlay");
  const layout = state.data.legend;
  if (!overlay || !layout) return;
  const [pageW, pageH] = layout.page?.canvas_px || [1, 1];
  overlay.setAttribute("viewBox", `0 0 ${pageW} ${pageH}`);
  overlay.setAttribute("preserveAspectRatio", "none");
  overlay.style.pointerEvents = "auto";
  const pxPerMm = Number(layout.render_px_per_mm) || 5;
  const fontPx = Number(layout.font?.size_pt || 24) * 25.4 / 72 * pxPerMm;
  const swatchSize = (layout.swatch?.size_px || [1, 1]).map(Number);

  const textMarkup = (item, origin = [0, 0], preserveNewlines = false) => {
    const metrics = item.render_metrics || {};
    const braille = String(item.braille_text || "");
    const metricsAreCurrent = String(metrics.braille_text || "") === braille;
    const lines = metricsAreCurrent && Array.isArray(metrics.lines)
      ? metrics.lines
      : (preserveNewlines ? braille.split("\n") : [braille]);
    const offsets = metrics.line_offsets_px || [[0, 0]];
    const lineAdvance = Number(metrics.line_spacing_px) || fontPx * 1.5;
    const bboxTop = Number(metrics.bbox_px?.[1]) || 0;
    return lines.map((line, index) => {
      const offset = offsets[index] || [offsets[0]?.[0] || 0, index * lineAdvance];
      return `<text x="${origin[0] + Number(offset[0] || 0)}"
        y="${origin[1] + Number(offset[1] || 0) + bboxTop}" font-size="${fontPx}"
        dominant-baseline="hanging">${esc(line)}</text>`;
    }).join("");
  };

  const boxes = (layout.entries || []).map((entry) => {
    const [x, y] = entry.position_page_px || [0, 0];
    const [w, h] = entry.group_size_px || [0, 0];
    return { id: entry.id, x, y, w, h, item: entry, hidden: entry.enabled === false,
      title: false };
  });
  const title = layout.title;
  if (title) {
    const [x, y] = title.position_page_px || [0, 0];
    boxes.push({
      id: title.id, x, y,
      w: Number(title.box_width_px) || 0,
      h: Number(title.render_metrics?.height_px) || 0,
      item: title, hidden: title.enabled === false, title: true, target: "title",
    });
  }

  overlay.innerHTML = boxes.map((box) => `
    <g class="braille-pin legend-object${box.title ? " legend-title-object" : ""}${box.hidden ? " is-hidden" : ""}"
       data-legend-box="${esc(box.id)}"
       transform="translate(${box.x} ${box.y})"
       role="button" tabindex="0" aria-label="Move ${esc(box.item.text || box.id)}">
      ${box.title ? "" : `<image href="${esc(legendSwatchUrl(state.selected, box.id, state.colourView))}"
        width="${swatchSize[0]}" height="${swatchSize[1]}"></image>`}
      <g class="braille-rendered-text">${textMarkup(box.item,
        box.title ? [0, 0] : (box.item.text_offset_px || [0, 0]), box.title)}</g>
      <rect class="selection-box" x="0" y="0" width="${box.w}" height="${box.h}"></rect>
    </g>`).join("");

  boxes.forEach((box) => {
    const node = overlay.querySelector(`[data-legend-box="${CSS.escape(String(box.id))}"]`);
    if (node) bindBoxDrag(overlay, node, box, [pageW, pageH]);
  });
}

function bindBoxDrag(overlay, node, box, pageSize) {
  let start = null;
  const place = (x, y) => {
    box.item.position_page_px = [x, y];
    node.setAttribute("transform", `translate(${x} ${y})`);
  };
  const clampX = (value) => Math.min(Math.max(value, 0), Math.max(0, pageSize[0] - box.w));
  const clampY = (value) => Math.min(Math.max(value, 0), Math.max(0, pageSize[1] - box.h));

  node.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    node.setPointerCapture(event.pointerId);
    node.classList.add("is-dragging");
    start = {
      x: event.clientX, y: event.clientY,
      position: [...(box.item.position_page_px || [0, 0])],
      rect: overlay.getBoundingClientRect(),
    };
  });
  node.addEventListener("pointermove", (event) => {
    if (!start || !start.rect.width || !start.rect.height) return;
    const dx = (event.clientX - start.x) / start.rect.width * pageSize[0];
    const dy = (event.clientY - start.y) / start.rect.height * pageSize[1];
    const [sx, sy] = snapToGrid([start.position[0] + dx, start.position[1] + dy],
                                Number(state.data.legend?.render_px_per_mm) || 5);
    place(clampX(sx), clampY(sy));
  });
  const drop = () => {
    if (!start) return;
    start = null;
    node.classList.remove("is-dragging");
    patchItem(box.target || box.id, { position_page_px: box.item.position_page_px });
  };
  node.addEventListener("pointerup", drop);
  node.addEventListener("pointercancel", drop);

  node.addEventListener("keydown", (event) => {
    const nudge = { ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5] }[event.key];
    if (!nudge) return;
    event.preventDefault();
    const [x, y] = box.item.position_page_px || [0, 0];
    place(clampX(x + nudge[0]), clampY(y + nudge[1]));
    patchItem(box.target || box.id, { position_page_px: box.item.position_page_px });
  });
}
