"use strict";

import { $, artifactUrl } from "../minimal/api.js";
import { state, toast } from "../minimal/state.js";
import { MaskSurface } from "./mask-surface.js";

// One active review only: switching projects or rerunning Step 2 drops caches.
let session = null;

function loadImage(stem, name) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("The current mask could not be displayed. Please retry."));
    image.src = artifactUrl(stem, name);
  });
}

function isCurrent(editor) {
  return session === editor && state.selected === editor.stem && state.maskBrush === editor.brush;
}

function editable(editor) {
  return isCurrent(editor) && state.maskBrush.active && !state.busy && !editor.resetting
    && ["erase", "restore"].includes(state.maskBrush.mode);
}

function updateActions() {
  const pending = state.maskBrush.strokes.length;
  const undo = $("mask-undo");
  const reset = $("mask-discard");
  const finish = $("test-mask-continue");
  if (undo) undo.disabled = state.busy || !pending;
  if (reset) reset.disabled = state.busy || !(pending || state.data.mask?.reviewed);
  if (finish) finish.disabled = Boolean(state.busy);
}

function finishStroke(editor) {
  const stroke = editor.surface?.finish();
  if (stroke && isCurrent(editor)) editor.brush.strokes.push(stroke);
  updateActions();
}

function cursorSize(editor) {
  const canvas = editor.surface.canvas;
  const scale = Math.min((canvas.clientWidth || canvas.width) / canvas.width,
    (canvas.clientHeight || canvas.height) / canvas.height);
  const size = `${editor.brush.radius * 2 * scale}px`;
  if (editor.cursor.style.width !== size) {
    editor.cursor.style.width = size;
    editor.cursor.style.height = size;
  }
}

function bindPointerEvents(editor) {
  const { surface, cursor } = editor;
  const canvas = surface.canvas;
  let pointerId = null;
  let cursorEvent = null;
  let cursorFrame = null;
  const point = (event) => {
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    return [Math.round((event.clientX - box.left) / box.width * canvas.width),
      Math.round((event.clientY - box.top) / box.height * canvas.height)];
  };
  const moveCursor = (event) => {
    cursorEvent = event;
    if (cursorFrame !== null) return;
    cursorFrame = requestAnimationFrame(() => {
      cursorFrame = null;
      if (!editable(editor)) return;
      const box = canvas.getBoundingClientRect();
      if (!box.width || !box.height) return;
      cursor.style.left = `${(cursorEvent.clientX - box.left) / box.width * 100}%`;
      cursor.style.top = `${(cursorEvent.clientY - box.top) / box.height * 100}%`;
      cursorSize(editor);
      cursor.classList.add("is-visible");
    });
  };
  canvas.addEventListener("pointerdown", (event) => {
    if (!editable(editor) || pointerId !== null || event.button !== 0) return;
    if (editor.brush.strokes.length >= 300) {
      toast("Save the 300 strokes already painted before adding more.", "warning");
      return;
    }
    const start = point(event);
    if (!start) return;
    pointerId = event.pointerId;
    canvas.setPointerCapture(pointerId);
    surface.begin({ mode: editor.brush.mode, radius: editor.brush.radius, points: [start] });
    moveCursor(event);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!editable(editor)) return;
    moveCursor(event);
    if (pointerId !== event.pointerId || !surface.current) return;
    const next = point(event);
    // Keep the existing stroke payload and point ceiling used by the server.
    if (next && surface.current.stroke.points.length < 4000) {
      surface.current.stroke.points.push(next);
      surface.schedule();
    }
  });
  const finish = (event) => {
    if (pointerId !== event.pointerId) return;
    finishStroke(editor);
    if (canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
    pointerId = null;
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
  canvas.addEventListener("lostpointercapture", finish);
  canvas.addEventListener("pointerleave", () => {
    if (cursorFrame !== null) cancelAnimationFrame(cursorFrame);
    cursorFrame = null;
    cursor.classList.remove("is-visible");
  });
}

function mount(editor) {
  const holder = $("mask-target");
  if (!holder || !isCurrent(editor) || !editor.surface) return;
  if (!holder.contains(editor.surface.canvas)) {
    holder.querySelector(".mask-canvas")?.remove();
    holder.querySelector(".mask-brush-cursor")?.remove();
    holder.append(editor.surface.canvas, editor.cursor);
  }
  holder.classList.toggle("is-masking", editor.brush.active);
  editor.surface.canvas.classList.toggle("is-readonly", !editor.brush.active || state.busy);
  cursorSize(editor);
}

async function automaticMask(editor) {
  if (!editor.automaticPromise) {
    editor.automaticPromise = loadImage(editor.stem, "map_mask_full_auto.png").then((image) => {
      // Prepare the reset overlay once, before the user needs it.
      const preview = new MaskSurface(image.naturalWidth, image.naturalHeight, image);
      editor.automatic = image;
      editor.automaticOverlay = preview.canvas;
      return image;
    }).catch((error) => {
      editor.automaticPromise = null;
      throw error;
    });
  }
  return editor.automaticPromise;
}

export function bindMaskCanvas() {
  if (Number(state.activeStep) !== 2 || state.job?.status === "running") {
    if (session) session.surface?.cancelFrame();
    session = null;
    return;
  }
  const review = state.data.mask;
  if (!$("mask-target") || !review) return;
  if (!session || !isCurrent(session) || session.width !== review.width || session.height !== review.height) {
    session?.surface?.cancelFrame();
    $("mask-target").querySelector(".mask-canvas")?.classList.add("is-readonly");
    const editor = { stem: state.selected, brush: state.maskBrush, width: review.width,
      height: review.height, surface: null, cursor: document.createElement("div"), resetting: false };
    editor.cursor.className = "mask-brush-cursor";
    editor.cursor.setAttribute("aria-hidden", "true");
    session = editor;
    editor.ready = loadImage(editor.stem, "map_mask_full.png").then((image) => {
      if (!isCurrent(editor)) return;
      editor.surface = new MaskSurface(review.width, review.height, image);
      if (editor.brush.strokes.length) editor.surface.replay(editor.brush.strokes);
      bindPointerEvents(editor);
      mount(editor);
    });
    editor.ready.catch((error) => {
      if (isCurrent(editor)) {
        session = null;
        toast(error.message, "error");
      }
    });
    // A failed prefetch is retried on Reset and must not disable brushing.
    void automaticMask(editor).catch(() => {});
  }
  mount(session);
  return session.ready;
}

export function undoMaskStroke() {
  const editor = session;
  if (!editor?.surface || !isCurrent(editor) || state.busy || editor.resetting) return;
  finishStroke(editor);
  const stroke = editor.brush.strokes.pop();
  if (stroke) editor.surface.undo(stroke, editor.brush.strokes);
  updateActions();
}

export function commitMaskStrokes() {
  if (session?.surface && isCurrent(session)) session.surface.commit();
}

export async function resetMaskSurface(synchronize) {
  const editor = session;
  if (!editor || !isCurrent(editor) || editor.resetting) {
    throw new Error("The mask is still loading. Please try resetting it again.");
  }
  editor.resetting = true;
  try {
    await editor.ready;
    if (!editor.automatic) await automaticMask(editor);
    if (!isCurrent(editor) || !editor.surface) return false;
    finishStroke(editor);
    const { surface, brush } = editor;
    // Only a persisted reset can fail. Keep its exact pixels and undo tiles
    // until the server acknowledges it, including antialiased stroke edges.
    const previous = { checkpoint: state.data.mask.reviewed ? surface.checkpoint() : null,
      strokes: brush.strokes,
      review: { ...state.data.mask } };
    surface.replace(editor.automatic, editor.automaticOverlay);
    brush.strokes = [];
    updateActions();
    if (!previous.review.reviewed) return false;
    try {
      await synchronize();
    } catch (error) {
      if (isCurrent(editor)) {
        surface.rollback(previous.checkpoint);
        brush.strokes = previous.strokes;
        state.data.mask = previous.review;
      }
      throw error;
    }
    if (isCurrent(editor)) {
      state.data.mask = { ...state.data.mask, reviewed: false, approved: false,
        kept_pixels: state.data.mask.automatic_pixels };
    }
    return true;
  } finally {
    editor.resetting = false;
    updateActions();
  }
}
