"use strict";

import {
  $, addBrailleLabel, deleteBrailleLabel, esc, saveBrailleLabel,
  saveBrailleLayout, saveBrailleTitle, saveStep8Review,
} from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { renderControls } from "../controls.js";
import { loadMaps, refreshSelectedData } from "../workspace.js";
import { refreshStepImages } from "../visual.js";
import { snapToGrid } from "../viewer.js";

/* Step 8 is a page-layout decision, not just a text list. The browser draws
   the editable Braille layer over a label-free server render, while every
   accepted edit also regenerates the printable PNG. */

const SIDES = ["top", "bottom", "right", "left"];
const PIN_SHAPES = ["circle", "triangle", "square"];
const TITLE_ALIGNS = ["left", "center", "right"];
let saveChain = Promise.resolve();
const textTimers = new Map();
const pendingTextValues = new Map();

function queued(task) {
  saveChain = saveChain.then(task, task);
  return saveChain;
}

function option(value, selected, label = value) {
  return `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`;
}

function brailleRowHtml(label) {
  const callout = label.callout === true;
  return `<article class="braille-row${label.enabled === false ? " is-hidden" : ""}"
      data-braille-row="${esc(label.id)}">
    <div class="braille-row-heading">
      <input class="braille-text" type="text" value="${esc(label.text || "")}" maxlength="200"
             aria-label="Braille text for ${esc(label.original_text || label.text || label.id)}">
      <div class="braille-row-options">
        <label class="tiny-check"><input class="braille-enabled" type="checkbox"
          ${label.enabled === false ? "" : "checked"}> Display</label>
        <label class="tiny-check"><input class="braille-callout" type="checkbox"
          ${callout ? "checked" : ""}> White box + pin</label>
      </div>
      <button class="icon-button braille-delete" type="button" title="Delete this text"
              aria-label="Delete ${esc(label.text || "text")}">&times;</button>
    </div>
    <div class="braille-row-detail">
      <div class="braille-callout-options${callout ? "" : " is-disabled"}">
        <label><span>Pin</span><select class="braille-shape" ${callout ? "" : "disabled"}>
          ${PIN_SHAPES.map((shape) => option(shape, label.pin_shape || "circle")).join("")}
        </select></label>
        <label><span>Pin location</span><select class="braille-side" ${callout ? "" : "disabled"}>
          ${SIDES.map((side) => option(side, label.side || "right")).join("")}
        </select></label>
      </div>
      <span class="braille-preview">${esc(label.braille_text || "")}</span>
    </div>
  </article>`;
}

function toolboxBody(includeApproval = false) {
  const layout = state.data.braille || {};
  const labels = layout.labels || [];
  const title = layout.title || {};
  const toolbox = layout.toolbox || {};
  const furniture = layout.page?.furniture || {};
  const review = state.data.step8Review || {};
  return `
    <div class="braille-tool-actions">
      <button class="button primary small" id="braille-add" type="button">Add text</button>
      <button class="button secondary small" id="braille-all-off" type="button">Turn all text off</button>
    </div>
    <div class="braille-mode-grid">
      <button class="pattern-mode-button" id="fix-text-to-map" type="button"
          aria-pressed="${toolbox.fix_text_to_map === true}">
        <span class="mode-switch" aria-hidden="true"></span>
        <span><strong>Fix text to the map</strong><small>Labels follow when the map moves</small></span>
      </button>
      <button class="pattern-mode-button" id="group-map-elements" type="button"
          aria-pressed="${toolbox.group_map_elements === true}">
        <span class="mode-switch" aria-hidden="true"></span>
        <span><strong>Group map elements</strong><small>Move map, border, and north sign together</small></span>
      </button>
    </div>
    <section class="braille-title-box">
      <div class="braille-subheading"><h4>Title</h4><span>Move and resize it on the page</span></div>
      <textarea id="braille-title-text" maxlength="200" rows="2"
                aria-label="Braille page title">${esc(title.text || "")}</textarea>
      <div class="braille-row-options">
        <label class="tiny-check"><input id="braille-title-enabled" type="checkbox"
          ${title.enabled === false ? "" : "checked"}> Display</label>
        <label class="compact-select"><span>Align</span><select id="braille-title-align">
          ${TITLE_ALIGNS.map((align) => option(align, title.align || "center")).join("")}
        </select></label>
      </div>
      <span class="braille-preview" id="braille-title-preview">${esc(title.braille_text || "")}</span>
    </section>
    <div class="braille-list" aria-label="Detected and added text">
      ${labels.map(brailleRowHtml).join("")
        || '<div class="empty-editor">No detected labels remain. Use Add text to create one.</div>'}
    </div>
    <section class="braille-furniture-box">
      <div class="braille-subheading"><h4>Map elements</h4><span>Select and move them on the page</span></div>
      <div class="braille-furniture-actions">
        <button class="button secondary small" id="toggle-north" type="button"
          aria-pressed="${furniture.north?.enabled === true}">${furniture.north?.enabled
            ? "Remove north sign" : "Add north sign"}</button>
        <button class="button secondary small" id="toggle-border" type="button"
          aria-pressed="${furniture.border?.enabled === true}">${furniture.border?.enabled
            ? "Remove map border" : "Draw map border"}</button>
        <button class="button secondary small" type="button" disabled title="Map scale is not available yet">
          Map scale — unavailable</button>
      </div>
      <p class="field-note">Click the map, border, north sign, or any text on the page to move that object.</p>
    </section>
    <p class="status-copy braille-review-status" id="braille-status">${review.approved
      ? "This label and page layout is approved." : "Review the labels and page arrangement."}</p>
    ${includeApproval ? `<button class="button primary full" id="approve-braille" type="button">
      Approve labels &amp; page layout</button>` : ""}`;
}

export function brailleDecisionHtml() {
  if (!state.data.braille) return "";
  return `<section class="review-gate braille-decision" id="braille-decision"
      aria-labelledby="braille-decision-title">
    <h3 id="braille-decision-title">Arrange the tactile page.</h3>
    ${toolboxBody(true)}
  </section>`;
}

export function bindBrailleEditor(onApproved) {
  document.querySelectorAll("[data-braille-row]").forEach((row) => {
    const id = row.dataset.brailleRow;
    const label = (state.data.braille?.labels || []).find((item) => String(item.id) === id);
    const field = row.querySelector(".braille-text");
    field?.addEventListener("input", () => {
      if (label) {
        label.text = field.value;
        label.braille_text = grade1Preview(field.value);
      }
      const preview = row.querySelector(".braille-preview");
      if (preview) preview.textContent = label?.braille_text || "";
      bindBrailleOverlay();
      window.clearTimeout(textTimers.get(id));
      pendingTextValues.set(id, field.value);
      textTimers.set(id, window.setTimeout(() => {
        const value = pendingTextValues.get(id);
        pendingTextValues.delete(id);
        patchLabel(id, { text: value });
      }, 350));
    });
    row.querySelector(".braille-enabled")?.addEventListener("change", (event) => {
      row.classList.toggle("is-hidden", !event.target.checked);
      if (label) label.enabled = event.target.checked;
      bindBrailleOverlay();
      patchLabel(id, { enabled: event.target.checked });
    });
    row.querySelector(".braille-callout")?.addEventListener("change", (event) => {
      row.querySelectorAll(".braille-callout-options select").forEach((select) => {
        select.disabled = !event.target.checked;
      });
      row.querySelector(".braille-callout-options")?.classList.toggle("is-disabled", !event.target.checked);
      patchLabel(id, { callout: event.target.checked });
    });
    row.querySelector(".braille-side")?.addEventListener("change", (event) =>
      patchLabel(id, { side: event.target.value }));
    row.querySelector(".braille-shape")?.addEventListener("change", (event) =>
      patchLabel(id, { pin_shape: event.target.value }));
    row.querySelector(".braille-delete")?.addEventListener("click", (event) =>
      removeLabel(id, event.currentTarget));
  });

  $("braille-add")?.addEventListener("click", async () => {
    await withBusy($("braille-add"), "Adding…", async () => {
      await addBrailleLabel(state.selected, "New text");
      await reloadBraille();
      renderControls();
      toast("New text added at the top of the list. Drag it into place.");
    });
  });
  $("braille-all-off")?.addEventListener("click", (event) =>
    patchLayout({ all_text_enabled: false }, event.currentTarget).then(() => renderControls()));

  const titleText = $("braille-title-text");
  titleText?.addEventListener("input", () => {
    const title = state.data.braille?.title;
    if (title) {
      title.text = titleText.value;
      title.braille_text = grade1Preview(titleText.value, true);
    }
    const preview = $("braille-title-preview");
    if (preview) preview.textContent = title?.braille_text || "";
    bindBrailleOverlay();
    window.clearTimeout(textTimers.get("title"));
    pendingTextValues.set("title", titleText.value);
    textTimers.set("title", window.setTimeout(() => {
      const value = pendingTextValues.get("title");
      pendingTextValues.delete("title");
      patchTitle({ text: value });
    }, 350));
  });
  $("braille-title-enabled")?.addEventListener("change", (event) =>
    patchTitle({ enabled: event.target.checked }));
  $("braille-title-align")?.addEventListener("change", (event) =>
    patchTitle({ align: event.target.value }));
  $("fix-text-to-map")?.addEventListener("click", (event) => {
    const value = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(value));
    patchLayout({ fix_text_to_map: value });
  });
  $("group-map-elements")?.addEventListener("click", (event) => {
    const value = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(value));
    patchLayout({ group_map_elements: value });
  });
  $("toggle-north")?.addEventListener("click", (event) =>
    toggleFurniture("north", event.currentTarget));
  $("toggle-border")?.addEventListener("click", (event) =>
    toggleFurniture("border", event.currentTarget));
  $("approve-braille")?.addEventListener("click", async () => {
    await withBusy($("approve-braille"), "Approving…", async () => {
      await flushPendingText();
      await saveChain;
      state.data.step8Review = await saveStep8Review(state.selected, true);
      await loadMaps();
      await refreshSelectedData();
      toast("Braille labels and page layout approved.");
      await onApproved?.();
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
    saves.push(id === "title" ? patchTitle({ text: value }) : patchLabel(id, { text: value }));
  });
  await Promise.all(saves);
}

function grade1Preview(value, preserveNewlines = false) {
  const text = String(value || "").normalize("NFKD").replace(/\p{M}/gu, "").replace(/\r/g, "");
  let output = "";
  let numberMode = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (/\d/.test(char)) {
      if (!numberMode) output += "#";
      output += char === "0" ? "j" : "abcdefghij"[Number(char) - 1];
      numberMode = true;
      continue;
    }
    if (/[A-Za-z]/.test(char)) {
      numberMode = false;
      if (/[A-Z]/.test(char)) {
        let end = index;
        while (end < text.length && /[A-Z]/.test(text[end])) end += 1;
        if (end - index > 1) {
          output += "``" + text.slice(index, end).toLowerCase();
          index = end - 1;
          continue;
        }
        output += "`";
      }
      output += char.toLowerCase();
      continue;
    }
    numberMode = false;
    if (char === "\n") output += preserveNewlines ? "\n" : " ";
    else output += char;
  }
  return output;
}

function markReviewDirty() {
  if (state.data.step8Review) state.data.step8Review.approved = false;
}

function patchLabel(labelId, patch) {
  return queued(async () => {
    statusLine("braille-status", "Updating the printable page…");
    try {
      const result = await saveBrailleLabel(state.selected, labelId, patch);
      applyLabel(result.label);
      markReviewDirty();
      await refreshStepImages();
      bindBrailleOverlay();
      statusLine("braille-status", `${result.enabled_labels} labels displayed.`, "success");
    } catch (error) {
      statusLine("braille-status", error.message, "error");
      toast(error.message, "error");
    }
  });
}

function patchTitle(patch) {
  return queued(async () => {
    statusLine("braille-status", "Updating the printable page…");
    try {
      const result = await saveBrailleTitle(state.selected, patch);
      if (state.data.braille) state.data.braille.title = result.title;
      markReviewDirty();
      await refreshStepImages();
      bindBrailleOverlay();
      statusLine("braille-status", "Title updated.", "success");
    } catch (error) {
      statusLine("braille-status", error.message, "error");
      toast(error.message, "error");
    }
  });
}

function patchLayout(patch, button = null) {
  return queued(async () => {
    if (button) button.disabled = true;
    statusLine("braille-status", "Updating the printable page…");
    try {
      const result = await saveBrailleLayout(state.selected, patch);
      state.data.braille = result.layout;
      state.data.pageLayout = {
        ...(state.data.pageLayout || {}),
        map_origin_px: result.layout.page?.map_origin_px,
        furniture: result.layout.page?.furniture,
      };
      markReviewDirty();
      await refreshStepImages();
      bindBrailleOverlay();
      statusLine("braille-status", "Page arrangement updated.", "success");
    } catch (error) {
      statusLine("braille-status", error.message, "error");
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  });
}

async function removeLabel(labelId, button) {
  await withBusy(button, "…", async () => {
    try {
      await deleteBrailleLabel(state.selected, labelId);
      markReviewDirty();
      await reloadBraille();
      renderControls();
      toast("Text deleted.");
    } catch (error) {
      toast(error.message, "error");
    }
  });
}

function toggleFurniture(kind, button) {
  const furniture = structuredClone(state.data.braille?.page?.furniture || {});
  furniture[kind] ||= {};
  furniture[kind].enabled = !furniture[kind].enabled;
  patchLayout({ furniture }, button).then(() => renderControls());
}

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
  bindBrailleOverlay();
}

/* -------------------------------------------------------- page overlay --- */

function pinShape(shape, radius, className) {
  if (shape === "triangle") {
    return `<polygon class="${className}" points="0,${-radius} ${radius},${radius} ${-radius},${radius}"></polygon>`;
  }
  if (shape === "square") {
    return `<rect class="${className}" x="${-radius}" y="${-radius}"
      width="${radius * 2}" height="${radius * 2}"></rect>`;
  }
  return `<circle class="${className}" r="${radius}"></circle>`;
}

function labelMarkup(label, origin) {
  const metrics = label.render_metrics || {};
  const [boxX, boxY] = metrics.box_offset_px || [0, 0];
  const [boxW, boxH] = metrics.box_size_px || [1, 1];
  const [textX, textY] = metrics.text_offset_px || [boxX, boxY];
  const textBaseline = textY + (Number(metrics.font_ascent_px) || 0);
  const x = Number(label.position_px?.[0] || 0) + origin[0];
  const y = Number(label.position_px?.[1] || 0) + origin[1];
  const callout = label.callout === true;
  const outer = Number(metrics.pin_outer_radius_px) || 20;
  const inner = Number(metrics.pin_black_radius_px) || 10;
  return `<g class="braille-pin${label.enabled === false ? " is-hidden" : ""}"
      data-braille-pin="${esc(label.id)}" transform="translate(${x} ${y})"
      role="button" tabindex="0" aria-label="Move ${esc(label.text || "text")}">
    <g class="braille-rendered-text">
      ${callout ? `<rect class="braille-white-box" x="${boxX}" y="${boxY}"
        width="${boxW}" height="${boxH}"></rect>` : ""}
      <text x="${textX}" y="${textBaseline}" font-size="${Number(metrics.font_size_px) || 42}">${esc(label.braille_text || "")}</text>
      ${callout ? pinShape(label.pin_shape || "circle", outer, "pin-outer")
        + pinShape(label.pin_shape || "circle", inner, "pin-inner") : ""}
    </g>
    <rect class="selection-box" x="${boxX}" y="${boxY}" width="${boxW}" height="${boxH}"></rect>
    ${callout ? pinShape(label.pin_shape || "circle", outer, "selection-pin") : ""}
  </g>`;
}

function titleMarkup(title) {
  const metrics = title.render_metrics || {};
  const [x, y] = title.position_page_px || [0, 0];
  const [boxW, boxH] = metrics.box_size_px || [title.box_width_px || 100, 40];
  const lines = metrics.lines || String(title.braille_text || "").split("\n");
  const offsets = metrics.line_offsets_px || [metrics.text_offset_px || [0, 0]];
  return `<g class="braille-title-object${title.enabled === false ? " is-hidden" : ""}"
      data-braille-title transform="translate(${x} ${y})" role="button" tabindex="0"
      aria-label="Move or resize the page title">
    <g class="braille-rendered-text">${lines.map((line, index) => {
      const offset = offsets[index] || offsets.at(-1) || [0, 0];
      return `<text x="${offset[0]}" y="${Number(offset[1]) + (Number(metrics.font_ascent_px) || 0)}"
        font-size="${Number(metrics.font_size_px) || 42}">${esc(line)}</text>`;
    }).join("")}</g>
    <rect class="selection-box title-selection" width="${boxW}" height="${boxH}"></rect>
    <rect class="resize-handle title-resize" x="${boxW - 7}" y="${Math.max(0, boxH - 7)}"
      width="14" height="14" data-title-resize></rect>
  </g>`;
}

export function bindBrailleOverlay() {
  const overlay = $("braille-overlay");
  const layout = state.data.braille;
  if (!overlay || !layout) return;
  const page = layout.page || {};
  const [pageW, pageH] = page.canvas_px || layout.canvas_px || [1, 1];
  const origin = [...(page.map_origin_px || [0, 0])].map(Number);
  const [mapW, mapH] = (layout.canvas_px || [pageW, pageH]).map(Number);
  const furniture = page.furniture || {};
  const border = furniture.border || {};
  const north = furniture.north || {};
  const pxPerMm = Number(layout.render_px_per_mm) || 5;
  const northSize = Number(north.size_mm || 24) * pxPerMm;
  const borderRect = border.rect_page_px || [origin[0], origin[1], origin[0] + mapW, origin[1] + mapH];
  const northPosition = north.position_page_px || [origin[0], origin[1]];

  overlay.setAttribute("viewBox", `0 0 ${pageW} ${pageH}`);
  overlay.setAttribute("preserveAspectRatio", "none");
  overlay.style.pointerEvents = "auto";
  overlay.innerHTML = `
    <rect class="map-object-hit" data-page-map x="${origin[0]}" y="${origin[1]}"
      width="${mapW}" height="${mapH}" tabindex="0" role="button" aria-label="Move the tactile map"></rect>
    ${border.enabled ? `<g class="border-object" data-page-border tabindex="0" role="button"
        aria-label="Move or resize the map border">
      <rect class="border-object-hit" x="${borderRect[0]}" y="${borderRect[1]}"
        width="${borderRect[2] - borderRect[0]}" height="${borderRect[3] - borderRect[1]}"></rect>
      <rect class="resize-handle border-resize" x="${borderRect[2] - 7}" y="${borderRect[3] - 7}"
        width="14" height="14" data-border-resize></rect>
    </g>` : ""}
    ${north.enabled ? `<g class="north-object" data-page-north tabindex="0" role="button"
        aria-label="Move the north sign">
      <rect class="north-object-hit" x="${northPosition[0]}" y="${northPosition[1]}"
        width="${northSize}" height="${northSize}"></rect>
    </g>` : ""}
    ${titleMarkup(layout.title || {})}
    ${(layout.labels || []).map((label) => labelMarkup(label, origin)).join("")}`;

  bindMapDrag(overlay, layout, [pageW, pageH], [mapW, mapH]);
  bindFurnitureDrag(overlay, layout, [pageW, pageH], northSize);
  bindTitleDrag(overlay, layout, [pageW, pageH]);
  overlay.querySelectorAll("[data-braille-pin]").forEach((pin) =>
    bindPinDrag(overlay, pin, layout, origin, [pageW, pageH]));
}

function dragGesture(overlay, element, startValue, moveValue, commit) {
  let start = null;
  element?.addEventListener("pointerdown", (event) => {
    if (event.target.closest("[data-title-resize], [data-border-resize]")) return;
    event.preventDefault();
    element.setPointerCapture(event.pointerId);
    element.classList.add("is-dragging");
    start = { client: [event.clientX, event.clientY], value: startValue(),
      box: overlay.getBoundingClientRect() };
  });
  element?.addEventListener("pointermove", (event) => {
    if (!start || !start.box.width || !start.box.height) return;
    const view = overlay.viewBox.baseVal;
    const delta = [(event.clientX - start.client[0]) / start.box.width * view.width,
      (event.clientY - start.client[1]) / start.box.height * view.height];
    moveValue(start.value, delta);
  });
  const drop = () => {
    if (!start) return;
    start = null;
    element.classList.remove("is-dragging");
    commit();
  };
  element?.addEventListener("pointerup", drop);
  element?.addEventListener("pointercancel", drop);
}

function bindMapDrag(overlay, layout, pageSize, mapSize) {
  const map = overlay.querySelector("[data-page-map]");
  let position = [...(layout.page?.map_origin_px || [0, 0])].map(Number);
  dragGesture(overlay, map, () => [...position], (start, delta) => {
    position = snapToGrid([start[0] + delta[0], start[1] + delta[1]], layout.render_px_per_mm);
    position = [Math.min(Math.max(position[0], 0), pageSize[0] - mapSize[0]),
      Math.min(Math.max(position[1], 0), pageSize[1] - mapSize[1])];
    map.setAttribute("x", position[0]);
    map.setAttribute("y", position[1]);
  }, () => patchLayout({ map_origin_px: position }));
}

function furnitureCopy(layout) {
  return structuredClone(layout.page?.furniture || {});
}

function bindFurnitureDrag(overlay, layout, pageSize, northSize) {
  const grouped = layout.toolbox?.group_map_elements === true;
  const mapOrigin = [...(layout.page?.map_origin_px || [0, 0])].map(Number);
  const borderGroup = overlay.querySelector("[data-page-border]");
  const borderHit = borderGroup?.querySelector(".border-object-hit");
  let borderRect = [...(layout.page?.furniture?.border?.rect_page_px || [0, 0, 1, 1])].map(Number);
  const savedBorderRect = [...borderRect];
  dragGesture(overlay, borderGroup, () => [...borderRect], (start, delta) => {
    const width = start[2] - start[0];
    const height = start[3] - start[1];
    let [x, y] = snapToGrid([start[0] + delta[0], start[1] + delta[1]], layout.render_px_per_mm);
    x = Math.min(Math.max(x, 0), pageSize[0] - width);
    y = Math.min(Math.max(y, 0), pageSize[1] - height);
    borderRect = [x, y, x + width, y + height];
    borderHit?.setAttribute("x", x); borderHit?.setAttribute("y", y);
    borderGroup?.querySelector("[data-border-resize]")?.setAttribute("x", x + width - 7);
    borderGroup?.querySelector("[data-border-resize]")?.setAttribute("y", y + height - 7);
  }, () => {
    if (grouped) {
      patchLayout({ map_origin_px: [mapOrigin[0] + borderRect[0] - savedBorderRect[0],
        mapOrigin[1] + borderRect[1] - savedBorderRect[1]] });
      return;
    }
    const furniture = furnitureCopy(layout);
    furniture.border.rect_page_px = borderRect;
    patchLayout({ furniture });
  });
  bindResizeHandle(overlay, borderGroup?.querySelector("[data-border-resize]"),
    () => [borderRect[2], borderRect[3]], (point) => {
      const [x, y] = snapToGrid(point, layout.render_px_per_mm);
      borderRect[2] = Math.min(Math.max(x, borderRect[0] + 20), pageSize[0]);
      borderRect[3] = Math.min(Math.max(y, borderRect[1] + 20), pageSize[1]);
      borderHit?.setAttribute("width", borderRect[2] - borderRect[0]);
      borderHit?.setAttribute("height", borderRect[3] - borderRect[1]);
      borderGroup?.querySelector("[data-border-resize]")?.setAttribute("x", borderRect[2] - 7);
      borderGroup?.querySelector("[data-border-resize]")?.setAttribute("y", borderRect[3] - 7);
    }, () => {
      const furniture = furnitureCopy(layout);
      furniture.border.rect_page_px = borderRect;
      patchLayout({ furniture });
    });

  const north = overlay.querySelector("[data-page-north]");
  const northHit = north?.querySelector(".north-object-hit");
  let northPosition = [...(layout.page?.furniture?.north?.position_page_px || [0, 0])].map(Number);
  const savedNorthPosition = [...northPosition];
  dragGesture(overlay, north, () => [...northPosition], (start, delta) => {
    northPosition = snapToGrid([start[0] + delta[0], start[1] + delta[1]], layout.render_px_per_mm);
    northPosition = [Math.min(Math.max(northPosition[0], 0), pageSize[0] - northSize),
      Math.min(Math.max(northPosition[1], 0), pageSize[1] - northSize)];
    northHit?.setAttribute("x", northPosition[0]); northHit?.setAttribute("y", northPosition[1]);
  }, () => {
    if (grouped) {
      patchLayout({ map_origin_px: [mapOrigin[0] + northPosition[0] - savedNorthPosition[0],
        mapOrigin[1] + northPosition[1] - savedNorthPosition[1]] });
      return;
    }
    const furniture = furnitureCopy(layout);
    furniture.north.position_page_px = northPosition;
    patchLayout({ furniture });
  });
}

function bindTitleDrag(overlay, layout, pageSize) {
  const titleGroup = overlay.querySelector("[data-braille-title]");
  const title = layout.title || {};
  const size = title.render_metrics?.box_size_px || [title.box_width_px || 100, 40];
  let position = [...(title.position_page_px || [0, 0])].map(Number);
  dragGesture(overlay, titleGroup, () => [...position], (start, delta) => {
    position = snapToGrid([start[0] + delta[0], start[1] + delta[1]], layout.render_px_per_mm);
    position = [Math.min(Math.max(position[0], 0), pageSize[0] - size[0]),
      Math.min(Math.max(position[1], 0), pageSize[1] - size[1])];
    titleGroup?.setAttribute("transform", `translate(${position[0]} ${position[1]})`);
  }, () => patchTitle({ position_page_px: position }));

  let width = Number(title.box_width_px || size[0]);
  bindResizeHandle(overlay, titleGroup?.querySelector("[data-title-resize]"),
    () => [position[0] + width, position[1]], (point) => {
      width = Math.min(Math.max(point[0] - position[0], 30 * (layout.render_px_per_mm || 5)),
        pageSize[0] - position[0]);
      titleGroup?.querySelector(".title-selection")?.setAttribute("width", width);
      titleGroup?.querySelector("[data-title-resize]")?.setAttribute("x", width - 7);
    }, () => patchTitle({ box_width_px: width }));
}

function bindResizeHandle(overlay, handle, startValue, moveValue, commit) {
  let start = null;
  handle?.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    handle.setPointerCapture(event.pointerId);
    start = { client: [event.clientX, event.clientY], value: startValue(),
      box: overlay.getBoundingClientRect() };
  });
  handle?.addEventListener("pointermove", (event) => {
    if (!start || !start.box.width || !start.box.height) return;
    const view = overlay.viewBox.baseVal;
    moveValue([start.value[0] + (event.clientX - start.client[0]) / start.box.width * view.width,
      start.value[1] + (event.clientY - start.client[1]) / start.box.height * view.height]);
  });
  const drop = () => { if (start) { start = null; commit(); } };
  handle?.addEventListener("pointerup", drop);
  handle?.addEventListener("pointercancel", drop);
}

function bindPinDrag(overlay, pin, layout, origin, pageSize) {
  const id = pin.dataset.braillePin;
  const label = (layout.labels || []).find((item) => String(item.id) === id);
  if (!label) return;
  let position = [...(label.position_px || [0, 0])].map(Number);
  dragGesture(overlay, pin, () => [...position], (start, delta) => {
    position = snapToGrid([start[0] + delta[0], start[1] + delta[1]], layout.render_px_per_mm);
    position = [Math.min(Math.max(position[0], -origin[0]), pageSize[0] - origin[0]),
      Math.min(Math.max(position[1], -origin[1]), pageSize[1] - origin[1])];
    pin.setAttribute("transform", `translate(${position[0] + origin[0]} ${position[1] + origin[1]})`);
  }, () => patchLabel(id, { position_px: position }));
}
