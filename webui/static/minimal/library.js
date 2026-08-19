"use strict";

import { $, deleteMap, esc, mapUrl, renameMap, reorderMaps } from "./api.js";
import { STEP_DEFS, completedCount } from "./steps.js";
import { isRunning, state, statusFor, toast } from "./state.js";
import { loadMaps, refreshSelectedData, renderWorkspace, selectMap } from "./workspace.js";

/* The drawer is the only place a project is created, renamed, reordered or
   deleted, so the workspace never has to think about more than one map.  Each
   of those actions sits on the project's own row: the row is the project. */

const PENCIL_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
  <path d="M12.1 1.7a1.15 1.15 0 0 1 1.6 0l.6.6a1.15 1.15 0 0 1 0 1.6l-8.1 8.1-2.8.6.6-2.8z"></path>
  <path d="M11 2.8 13.2 5"></path></svg>`;

const CROSS_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"
  stroke-linecap="round" aria-hidden="true" focusable="false">
  <path d="M4.4 4.4 11.6 11.6M11.6 4.4 4.4 11.6"></path></svg>`;

/* The stem being dragged.  `dataTransfer` refuses to be read during dragover,
   so the row under the pointer needs another way to know what is coming. */
let dragging = null;

export function renderProjectList() {
  const list = $("project-list");
  const editing = renameFieldSnapshot();
  $("nav-empty").hidden = state.maps.length > 0;
  list.innerHTML = state.maps.map(projectRow).join("");
  bindProjectList(editing);
}

function projectRow(map) {
  const progress = Math.round((completedCount(map) / STEP_DEFS.length) * 100);
  const status = statusFor(map);
  const stem = esc(map.stem);
  const name = esc(map.name);
  const meter = `<span class="mini-progress" style="--progress:${progress}%"
                       role="img" aria-label="${progress}% complete"></span>`;

  // Renaming happens in place: the row keeps its picture, its status line and
  // its progress ring, and only the name turns into a field.
  if (state.renaming === map.stem) {
    return `
      <div class="project-item is-renaming" data-stem="${stem}">
        <span class="project-thumb"><img src="${mapUrl(map.name)}" alt=""></span>
        <span class="project-copy">
          <input class="project-rename" id="rename-input" type="text" value="${name}"
                 maxlength="120" autocomplete="off" spellcheck="false"
                 aria-label="New name for ${name}">
          <small>${esc(status.label)}</small>
        </span>
        ${meter}
      </div>`;
  }

  // Renaming and deleting are both refused by the server while a job runs, so
  // the row says so up front rather than letting the click fail.
  const running = isRunning(map);
  const held = running ? "Available once the run finishes" : "";
  const selected = map.stem === state.selected;
  return `
    <div class="project-item${running ? " is-running" : ""}${selected ? " is-selected" : ""}"
         data-stem="${stem}" draggable="true">
      <button class="project-open" type="button" aria-current="${selected ? "true" : "false"}">
        <span class="project-thumb"><img src="${mapUrl(map.name)}" alt=""></span>
        <span class="project-copy">
          <strong>${name}</strong>
          <small>${esc(status.label)}</small>
        </span>
      </button>
      ${meter}
      <span class="project-actions">
        <button class="row-icon" type="button" data-action="rename" ${running ? "disabled" : ""}
                title="${held || `Rename ${name}`}" aria-label="Rename ${name}">${PENCIL_ICON}</button>
        <button class="row-icon is-danger" type="button" data-action="delete" ${running ? "disabled" : ""}
                title="${held || `Delete ${name}`}" aria-label="Delete ${name}">${CROSS_ICON}</button>
      </span>
    </div>`;
}

function bindProjectList(editing) {
  $("project-list").querySelectorAll(".project-item[data-stem]").forEach((item) => {
    const stem = item.dataset.stem;
    item.querySelector(".project-open")?.addEventListener("click", () => selectMap(stem));
    item.querySelector('[data-action="rename"]')?.addEventListener("click", () => {
      state.renaming = stem;
      renderProjectList();
    });
    item.querySelector('[data-action="delete"]')?.addEventListener("click", () => removeProject(item));
    if (item.draggable) bindRowDrag(item);
  });
  bindRenameField(editing);
}

/* ------------------------------------------------------------ reorder --- */

function bindRowDrag(item) {
  item.addEventListener("dragstart", (event) => {
    dragging = item.dataset.stem;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", dragging);
    item.classList.add("is-dragging");
  });
  item.addEventListener("dragend", () => {
    dragging = null;
    item.classList.remove("is-dragging");
    clearDropMarks();
  });
  item.addEventListener("dragover", (event) => {
    if (!dragging || dragging === item.dataset.stem) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    // Which half of the row the pointer is over decides whether the project
    // lands above or below it, so the drop never has to be guessed at.
    const box = item.getBoundingClientRect();
    const below = event.clientY > box.top + box.height / 2;
    clearDropMarks();
    item.classList.add(below ? "drop-after" : "drop-before");
  });
  item.addEventListener("dragleave", (event) => {
    if (!item.contains(event.relatedTarget)) {
      item.classList.remove("drop-before", "drop-after");
    }
  });
  item.addEventListener("drop", (event) => {
    event.preventDefault();
    const below = item.classList.contains("drop-after");
    const dragged = dragging || event.dataTransfer.getData("text/plain");
    clearDropMarks();
    if (dragged && dragged !== item.dataset.stem) moveProject(dragged, item.dataset.stem, below);
  });
}

function clearDropMarks() {
  $("project-list").querySelectorAll(".drop-before, .drop-after")
    .forEach((row) => row.classList.remove("drop-before", "drop-after"));
}

/** The server insists on an exact permutation, so the whole order is sent.
 *  The list is resorted first: the drop should look instant rather than wait
 *  on a round trip that would snap the row back for a frame. */
async function moveProject(draggedStem, targetStem, below) {
  const stems = state.maps.map((map) => map.stem);
  const from = stems.indexOf(draggedStem);
  if (from < 0) return;
  stems.splice(from, 1);
  const target = stems.indexOf(targetStem);
  if (target < 0) return;
  stems.splice(below ? target + 1 : target, 0, draggedStem);

  const rank = new Map(stems.map((stem, index) => [stem, index]));
  state.maps = [...state.maps].sort((a, b) => rank.get(a.stem) - rank.get(b.stem));
  renderProjectList();
  try {
    await reorderMaps(stems);
  } catch (error) {
    toast(error.message, "error");
  }
  await loadMaps();
}

/* ------------------------------------------------------------- rename --- */

/** A poll can redraw the list mid-edit, so the field's own text and caret are
 *  carried across the redraw rather than reset to the stored name. */
function renameFieldSnapshot() {
  const field = document.activeElement;
  if (!field || field.id !== "rename-input") return null;
  return { value: field.value, start: field.selectionStart, end: field.selectionEnd };
}

function bindRenameField(editing) {
  const field = $("rename-input");
  if (!field) return;
  field.focus();
  if (editing) {
    field.value = editing.value;
    field.setSelectionRange(editing.start, editing.end);
  } else {
    field.select();
  }
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commitRename(field.value);
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();   // Escape abandons the edit; it does not close the drawer
      state.renaming = null;
      renderProjectList();
    }
  });
  field.addEventListener("blur", () => commitRename(field.value));
}

async function commitRename(value) {
  const stem = state.renaming;
  if (!stem) return;
  state.renaming = null;
  const map = state.maps.find((item) => item.stem === stem);
  const wanted = String(value || "").trim();
  if (!map || !wanted || wanted === map.name) {
    renderProjectList();
    return;
  }
  try {
    const result = await renameMap(stem, wanted);
    const wasSelected = state.selected === stem;
    if (wasSelected) state.selected = result.stem;
    await loadMaps();
    if (wasSelected) {
      await refreshSelectedData();
      renderWorkspace();
    }
    toast("Project renamed.");
  } catch (error) {
    toast(error.message, "error");
    renderProjectList();
  }
}

/* ------------------------------------------------------------- delete --- */

/** `withBusy` writes a label into the button it locks, which would wipe an
 *  icon, so the row carries the waiting state instead. */
async function removeProject(item) {
  const stem = item.dataset.stem;
  const map = state.maps.find((entry) => entry.stem === stem);
  if (!map || state.busy) return;
  if (!window.confirm(`Delete ${map.name} and every result generated from it?`)) return;
  state.busy = true;
  item.classList.add("is-busy");
  try {
    await deleteMap(stem);
    if (state.selected === stem) {
      state.selected = null;
      state.data = {};
      state.job = { status: "idle" };
    }
    await loadMaps();
    renderWorkspace();
    toast("Project deleted.");
  } catch (error) {
    toast(error.message, "error");
    item.classList.remove("is-busy");
  } finally {
    state.busy = false;
  }
}
