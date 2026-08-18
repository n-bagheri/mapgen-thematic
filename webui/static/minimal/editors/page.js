"use strict";

import { $, NORTH_MARKER_URL, esc, savePageLayout } from "../api.js";
import { state, statusLine, toast, withBusy } from "../state.js";
import { editorDetails, renderControls } from "../controls.js";
import { loadMaps, refreshSelectedData } from "../workspace.js";
import { refreshStepImages } from "../visual.js";
import { snapToGrid } from "../viewer.js";

/* Step 7 also decides where the map sits on the printed sheet and what else is
   printed beside it.  The map is drawn to scale inside a page rectangle so the
   margins stay visible while it is dragged; the sheet itself is only rendered
   once Step 8 composes the page. */

export function pageEditorHtml() {
  const layout = state.data.pageLayout;
  if (!layout) {
    return editorDetails("page", "7", "Page layout", "Where the map sits on the sheet",
      '<div class="empty-editor">Finish Step 7 to place the map on its page.</div>');
  }
  const [pageW, pageH] = layout.canvas_px || [1, 1];
  const [mapW, mapH] = layout.map_size_px || [1, 1];
  const [originX, originY] = layout.map_origin_px || [0, 0];
  const furniture = layout.furniture || {};
  const north = furniture.north || {};
  const border = furniture.border || {};
  const pxPerMm = Number(layout.render_px_per_mm) || 5;
  const northSize = Number(north.size_mm || 0) * pxPerMm;
  const allowed = layout.allowed_orientations || ["portrait", "landscape"];
  const pct = (value, total) => `${(Number(value) / Number(total) * 100).toFixed(3)}%`;
  const body = `
    <p class="section-intro">Drag the map to move it on the sheet. The page is
      ${esc(String(layout.size_mm?.[0] ?? "?"))} × ${esc(String(layout.size_mm?.[1] ?? "?"))} mm
      with a ${esc(String(layout.margin_mm ?? "?"))} mm margin.</p>
    <div class="page-frame" id="page-frame" style="aspect-ratio:${pageW} / ${pageH}">
      <div class="page-map" id="page-map" style="left:${pct(originX, pageW)};top:${pct(originY, pageH)};
           width:${pct(mapW, pageW)};height:${pct(mapH, pageH)}"
           role="button" tabindex="0" aria-label="Map position on the page"></div>
      ${border.enabled ? `<div class="page-border" style="left:${pct(originX, pageW)};top:${pct(originY, pageH)};
           width:${pct(mapW, pageW)};height:${pct(mapH, pageH)}"></div>` : ""}
      ${north.enabled ? `<div class="page-north" style="left:${pct(north.position_page_px?.[0] || 0, pageW)};
           top:${pct(north.position_page_px?.[1] || 0, pageH)};
           width:${pct(northSize, pageW)};height:${pct(northSize, pageH)}">
        <img src="${NORTH_MARKER_URL}" alt=""></div>` : ""}
    </div>
    <div class="furniture-row">
      <label class="tiny-check"><input id="furniture-border" type="checkbox"
        ${border.enabled ? "checked" : ""}> map border</label>
      <label class="tiny-check"><input id="furniture-north" type="checkbox"
        ${north.enabled ? "checked" : ""}> north marker</label>
      <label class="tiny-check"><input type="checkbox" disabled> scale bar (not built yet)</label>
    </div>
    <div class="form-grid">
      <label class="field"><span>Orientation</span>
        <select id="page-layout-orientation">
          <option value="portrait" ${layout.orientation === "portrait" ? "selected" : ""}
            ${allowed.includes("portrait") ? "" : "disabled"}>Portrait</option>
          <option value="landscape" ${layout.orientation === "landscape" ? "selected" : ""}
            ${allowed.includes("landscape") ? "" : "disabled"}>Landscape</option>
        </select>
        <small class="field-note">Only rotations the map still fits on are offered.</small>
      </label>
    </div>
    <div class="action-row end"><span class="status-copy" id="page-status"></span>
      <button class="button subtle small" id="page-centre" type="button">Centre on the page</button></div>`;
  return editorDetails("page", "7", "Page layout", "Where the map sits on the sheet", body);
}

export function bindPageEditor() {
  const frame = $("page-frame");
  const handle = $("page-map");
  const layout = state.data.pageLayout;
  if (frame && handle && layout) bindPageDrag(frame, handle, layout);

  $("furniture-border")?.addEventListener("change", (event) => {
    saveFurniture({ border: { ...(layout?.furniture?.border || {}), enabled: event.target.checked } });
  });
  $("furniture-north")?.addEventListener("change", (event) => {
    saveFurniture({ north: { ...(layout?.furniture?.north || {}), enabled: event.target.checked } });
  });
  $("page-layout-orientation")?.addEventListener("change", async (event) => {
    statusLine("page-status", "Rotating the page…");
    await commitLayout({ orientation: event.target.value });
  });
  $("page-centre")?.addEventListener("click", async () => {
    if (!layout) return;
    const [pageW, pageH] = layout.canvas_px;
    const [mapW, mapH] = layout.map_size_px;
    await withBusy($("page-centre"), "Centring…", () => commitLayout({
      map_origin_px: [(pageW - mapW) / 2, (pageH - mapH) / 2],
    }));
  });
}

/** Move the box in the browser first so dragging stays smooth, then let the
 *  server clamp and store the placement it actually accepted. */
function bindPageDrag(frame, handle, layout) {
  const [pageW, pageH] = layout.canvas_px;
  const [mapW, mapH] = layout.map_size_px;
  let start = null;

  handle.addEventListener("pointerdown", (event) => {
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("is-dragging");
    start = {
      x: event.clientX, y: event.clientY,
      origin: [...(layout.map_origin_px || [0, 0])],
      box: frame.getBoundingClientRect(),
    };
  });
  handle.addEventListener("pointermove", (event) => {
    if (!start || !start.box.width || !start.box.height) return;
    const dx = (event.clientX - start.x) / start.box.width * pageW;
    const dy = (event.clientY - start.y) / start.box.height * pageH;
    const [rawX, rawY] = snapToGrid([start.origin[0] + dx, start.origin[1] + dy],
                                    Number(layout.render_px_per_mm) || 5);
    const x = Math.min(Math.max(rawX, 0), pageW - mapW);
    const y = Math.min(Math.max(rawY, 0), pageH - mapH);
    layout.map_origin_px = [x, y];
    handle.style.left = `${x / pageW * 100}%`;
    handle.style.top = `${y / pageH * 100}%`;
  });
  const drop = async () => {
    if (!start) return;
    start = null;
    handle.classList.remove("is-dragging");
    statusLine("page-status", "Saving placement…");
    await commitLayout({ map_origin_px: layout.map_origin_px });
  };
  handle.addEventListener("pointerup", drop);
  handle.addEventListener("pointercancel", drop);

  handle.addEventListener("keydown", (event) => {
    const nudge = { ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5] }[event.key];
    if (!nudge) return;
    event.preventDefault();
    const x = Math.min(Math.max(layout.map_origin_px[0] + nudge[0], 0), pageW - mapW);
    const y = Math.min(Math.max(layout.map_origin_px[1] + nudge[1], 0), pageH - mapH);
    layout.map_origin_px = [x, y];
    handle.style.left = `${x / pageW * 100}%`;
    handle.style.top = `${y / pageH * 100}%`;
    commitLayout({ map_origin_px: [x, y] });
  });
}

function saveFurniture(patch) {
  const current = state.data.pageLayout?.furniture || {};
  return commitLayout({ furniture: { ...current, ...patch } });
}

async function commitLayout(patch) {
  try {
    const result = await savePageLayout(state.selected, patch);
    state.data.pageLayout = result.layout;
    await loadMaps();
    await refreshStepImages();
    statusLine("page-status", "Saved.", "success");
    renderControls();
    await refreshSelectedData();
  } catch (error) {
    statusLine("page-status", error.message, "error");
    toast(error.message, "error");
    await refreshSelectedData();
    renderControls();
  }
}
