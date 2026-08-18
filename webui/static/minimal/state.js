"use strict";

import { $ } from "./api.js";
import { STEP_DEFS, allDone, blockingReason, completedCount } from "./steps.js";

/* One flat mutable object holds everything the focused view knows.  Rendering
   is always a full redraw of a pane from this object, so there is no second
   copy of the truth to keep in step. */
export const state = {
  maps: [],
  models: [],
  defaultModel: "",
  spec: null,          // Step 0 output spec, shared by every map
  customPage: false,
  groupEdit: null,     // class index -> tactile category slot, while reviewing
  groupLabels: {},
  visibleGroupSlots: null,
  selected: null,
  model: "",
  data: {},
  job: { status: "idle" },
  pollTimer: null,
  generation: 0,
  activeStep: null,    // step whose pictures the left pane shows; null follows the run
  viewStep: null,      // step key pinned by the circles; null follows the run
  previewLevel: null,
  layers: { map: true, labels: true, lines: true, boundaries: true },
  colourView: false,   // relief master, or the hybrid colour render
  snapToGrid: false,   // drags land on the 6 mm braille grid
  autorun: false,
  busy: false,
  continuing: false,
  pollFailures: 0,
  pollVisualSignature: "",
  pollControlSignature: "",
  maskBrush: { active: false, mode: "erase", radius: 12, strokes: [] },
  patternGroup: 0,     // which Step 7 area the transform box is editing
  renaming: false,
};

export function selectedMap() {
  return state.maps.find((map) => map.stem === state.selected) || null;
}

export function isRunning(map) {
  return Boolean(map?.job?.status === "running"
    || (state.selected === map?.stem && state.job.status === "running"));
}

export function currentStepKey(map) {
  const value = state.selected === map?.stem && state.job.current !== undefined
    ? state.job.current
    : map?.job?.current;
  return value === null || value === undefined ? null : String(value);
}

export function statusFor(map) {
  if (!map) return { label: "Choose a map", className: "is-idle" };
  const job = state.selected === map.stem ? state.job : map.job;
  if (job?.status === "running") {
    const step = STEP_DEFS.find((item) => item.key === String(job.current));
    return { label: step ? `${step.title}…` : "Starting pipeline…", className: "is-running" };
  }
  if (job?.status === "failed") return { label: "Pipeline needs attention", className: "is-failed" };
  if (blockingReason(map)) return { label: "Out of scope", className: "is-failed" };
  if (map.steps?.["5"] && !map.step5_review_ready) {
    return { label: "Category review needed", className: "is-blocked" };
  }
  if (allDone(map)) return { label: "Tactile result ready", className: "is-ready" };
  const count = completedCount(map);
  return {
    label: count ? `${count} of ${STEP_DEFS.length} stages ready` : "Ready to run",
    className: count ? "is-ready" : "is-idle",
  };
}

export function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`.trim();
  node.textContent = message;
  $("toast-region").appendChild(node);
  window.setTimeout(() => node.remove(), 4200);
}

export function setNav(open, returnFocus = false) {
  document.body.classList.toggle("nav-open", open);
  const toggle = $("nav-toggle");
  const nav = $("project-nav");
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Close map library" : "Open map library");
  nav.setAttribute("aria-hidden", String(!open));
  nav.inert = !open;
  if (returnFocus) toggle.focus();
}

/** The universal async wrapper: one global busy lock, a button that says what
 *  it is doing, and an error surfaced as a toast rather than a dead click. */
export async function withBusy(button, busyLabel, action) {
  if (state.busy) return;
  state.busy = true;
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = busyLabel;
  }
  try {
    await action();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy = false;
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

export function statusLine(id, message, kind = "") {
  const node = $(id);
  if (!node) return;
  node.textContent = message;
  node.className = `status-copy ${kind}`.trim();
}
