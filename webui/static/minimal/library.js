"use strict";

import { $, deleteMap, esc, mapUrl, renameMap, reorderMaps } from "./api.js";
import { STEP_DEFS, completedCount } from "./steps.js";
import { isRunning, selectedMap, state, statusFor, toast, withBusy } from "./state.js";
import { loadMaps, refreshSelectedData, renderWorkspace, selectMap } from "./workspace.js";

/* The drawer is the only place a project is created, renamed, reordered or
   deleted, so the workspace never has to think about more than one map. */
export function renderProjectList() {
  const list = $("project-list");
  $("nav-empty").hidden = state.maps.length > 0;
  $("project-tools").hidden = !state.selected;
  list.innerHTML = state.maps.map((map) => {
    const count = completedCount(map);
    const progress = Math.round((count / STEP_DEFS.length) * 100);
    const status = statusFor(map);
    const selected = map.stem === state.selected;
    if (selected && state.renaming) {
      return `
        <div class="project-item is-renaming" data-stem="${esc(map.stem)}">
          <input class="project-rename" id="rename-input" type="text" value="${esc(map.name)}"
                 maxlength="120" aria-label="New name for ${esc(map.name)}">
        </div>`;
    }
    return `
      <button class="project-item${isRunning(map) ? " is-running" : ""}" type="button" draggable="true"
              data-stem="${esc(map.stem)}" aria-current="${selected ? "true" : "false"}">
        <span class="project-thumb"><img src="${mapUrl(map.name)}" alt=""></span>
        <span class="project-copy">
          <strong>${esc(map.name)}</strong>
          <small>${esc(status.label)}</small>
        </span>
        <span class="mini-progress" style="--progress:${progress}%" aria-label="${progress}% complete"></span>
      </button>`;
  }).join("");
  bindProjectList();
}

function bindProjectList() {
  const list = $("project-list");
  list.querySelectorAll(".project-item[data-stem]").forEach((item) => {
    if (item.classList.contains("is-renaming")) return;
    item.addEventListener("click", () => selectMap(item.dataset.stem));
    item.addEventListener("dragstart", (event) => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", item.dataset.stem);
      item.classList.add("is-dragging");
    });
    item.addEventListener("dragend", () => {
      item.classList.remove("is-dragging");
      list.querySelectorAll(".project-item").forEach((row) => row.classList.remove("is-drag-over"));
    });
    item.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      item.classList.add("is-drag-over");
    });
    item.addEventListener("dragleave", () => item.classList.remove("is-drag-over"));
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      const dragged = event.dataTransfer.getData("text/plain");
      if (dragged && dragged !== item.dataset.stem) moveProject(dragged, item.dataset.stem);
    });
  });

  const field = $("rename-input");
  if (field) {
    field.focus();
    field.select();
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter") commitRename(field.value);
      if (event.key === "Escape") {
        state.renaming = false;
        renderProjectList();
      }
    });
    field.addEventListener("blur", () => commitRename(field.value));
  }
}

/** The server insists on an exact permutation, so the whole order is sent. */
async function moveProject(draggedStem, targetStem) {
  const stems = state.maps.map((map) => map.stem);
  const from = stems.indexOf(draggedStem);
  const to = stems.indexOf(targetStem);
  if (from < 0 || to < 0) return;
  stems.splice(to, 0, ...stems.splice(from, 1));
  try {
    await reorderMaps(stems);
    await loadMaps();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function commitRename(value) {
  if (!state.renaming) return;
  state.renaming = false;
  const map = selectedMap();
  const wanted = String(value || "").trim();
  if (!map || !wanted || wanted === map.name) {
    renderProjectList();
    return;
  }
  try {
    const result = await renameMap(map.stem, wanted);
    state.selected = result.stem;
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace();
    toast("Project renamed.");
  } catch (error) {
    toast(error.message, "error");
    renderProjectList();
  }
}

export function bindLibraryTools() {
  $("rename-project").addEventListener("click", () => {
    if (!state.selected) return;
    state.renaming = true;
    renderProjectList();
  });
  $("delete-project").addEventListener("click", async () => {
    const map = selectedMap();
    if (!map) return;
    if (!window.confirm(`Delete ${map.name} and every result generated from it?`)) return;
    await withBusy($("delete-project"), "Deleting…", async () => {
      await deleteMap(map.stem);
      state.selected = null;
      state.data = {};
      state.job = { status: "idle" };
      await loadMaps();
      renderWorkspace();
      toast("Project deleted.");
    });
  });
}
