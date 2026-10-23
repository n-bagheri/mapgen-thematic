/* Completed-project navigation belongs exclusively to /test. */
import { artifactUrl, getJob, runSteps, saveLabelReview } from "../minimal/api.js";
import { selectedMap, state, toast } from "../minimal/state.js";
import { STEP_VIEWS } from "../minimal/steps.js";
import { clearPreviewCache } from "../minimal/preview-cache.js";
import { loadMaps, refreshSelectedData } from "../minimal/workspace.js";
import { bindAggregationEditor } from "../minimal/editors/aggregation.js";
import { bindSimplificationEditor } from "../minimal/editors/simplification.js";
import { bindPatternEditor } from "../minimal/editors/patterns.js";
import { bindBrailleEditor } from "../minimal/editors/braille.js";
import { bindLegendEditor } from "../minimal/editors/legend.js";
import { bindLegendReviewEditor } from "../minimal/editors/legendreview.js";
import { bindTextEditor } from "../minimal/editors/text.js";

const projects = new Map();
let hooks = null;
const clone = (value) => structuredClone(value);
const pause = () => new Promise((resolve) => window.setTimeout(resolve, 300));
let transport = null;
let pendingSaves = 0;
const ownerOf = (name) => name.startsWith("step8a_") ? 7
  : Number(name.match(/^step([1-9])[_\.]/)?.[1])
    || (name === "label_map_gen_preview.png" ? 6 : name === "label_map_preview.png" ? 4 : 2);
const ownedData = { 1: ["semantics"], 2: ["mask", "legendReview"], 3: ["labels"], 4: ["lines", "classesFinal"],
  5: ["aggregation"], 6: ["step6", "presets"], 7: ["patterns", "colors", "step7Review", "pageLayout"],
  8: ["braille", "step8Review"], 9: ["legend", "step9Review"] };
const localDraft = () => clone({ groupEdit: state.groupEdit, groupLabels: state.groupLabels,
  visibleGroupSlots: state.visibleGroupSlots, previewLevel: state.previewLevel,
  legendBox: state.legendBox, maskBrush: state.maskBrush });

async function sessionAction(project, operation, step, keep = false, invalidate = false) {
  const response = await transport(`/api/test/review-session/${project.session}/checkpoint`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operation, step, keep, invalidate }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The saved review could not be opened.");
}

function reviewUrl(value) {
  const project = completedReview.project;
  if (!project?.session || !(project.step || project.busy || project.exporting)) return value;
  const url = new URL(value, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/test/review-session/")) return value;
  const parts = url.pathname.split("/");
  if (parts[2] === "maps" && url.pathname !== "/api/maps") return value;
  const belongs = parts[3] === encodeURIComponent(project.stem)
    || url.pathname === `/api/test/step7-edit/${encodeURIComponent(project.stem)}`;
  if (!belongs && !["/api/maps", "/api/run"].includes(url.pathname)) return value;
  return `/api/test/review-session/${project.session}${url.pathname}${url.search}`;
}

function installReviewTransport() {
  if (transport) return;
  transport = window.fetch.bind(window);
  window.fetch = async (url, options = {}) => {
    const target = reviewUrl(typeof url === "string" ? url : url.url);
    const save = target !== url && options.method && options.method !== "GET";
    if (save) pendingSaves += 1;
    try { return await transport(target, options); }
    finally { if (save) pendingSaves -= 1; }
  };
  // Image() loads used by the existing mask editor also need its isolated files.
  const source = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
  Object.defineProperty(HTMLImageElement.prototype, "src", { ...source,
    set(value) { source.set.call(this, reviewUrl(value)); } });
}

export const completedReview = {
  get project() { return projects.get(state.selected); },
  get step() { return this.project?.step || null; },
  get active() { return Boolean(this.step); },
  get busy() { return Boolean(this.project?.busy); },
  get maskPhase() { return this.project?.maskPhase || "mask"; },
  available(map = selectedMap()) {
    return Boolean(map && ![state.job?.status, map.job?.status].includes("running")
      && (projects.has(map.stem) || Array.from({ length: 9 }, (_, i) => map.steps?.[String(i + 1)]).every(Boolean)));
  },
  remember(map = selectedMap()) {
    if (!this.available(map) || projects.has(map.stem)) return;
    const project = { stem: map.stem, step: null, busy: false, maskPhase: "mask",
      snapshots: new Map(), locals: new Map(), images: new Map(), dirty: new Set(), stale: new Set(), map: clone(map) };
    for (let step = 1; step <= 9; step += 1) project.snapshots.set(step, clone(state.data));
    projects.set(map.stem, project);
    project.ready = this.captureImages(project);
  },
  async restoreAvailability(map) {
    if (!map || this.available(map) || state.job?.status === "running") return;
    try {
      const response = await transport(`/api/test/review-status/${encodeURIComponent(map.stem)}`, { cache: "no-store" });
      if (response.ok && (await response.json()).available) {
        const saved = clone(map);
        saved.steps = Object.fromEntries(Array.from({ length: 9 }, (_, index) => [String(index + 1), true]));
        this.remember(saved);
      }
    } catch { /* The normal unfinished-project workflow remains available. */ }
  },
  leave() {
    const project = this.project;
    if (project?.step) {
      project.snapshots.set(project.step, clone(state.data));
      project.locals.set(project.step, localDraft());
    }
    if (project) { project.step = null; project.exporting = false; }
  },
  async suspend() {
    if (this.busy || state.busy || hooks.updating()) return false;
    if (this.active) {
      await new Promise((resolve) => window.setTimeout(resolve, 450));
      while (pendingSaves) await pause();
    }
    this.leave();
    if (this.project?.session) await loadMaps();
    return true;
  },
  async captureImages(project, onlyStep = null) {
    const names = new Set();
    Object.entries(STEP_VIEWS).forEach(([step, views]) => {
      if (onlyStep && Number(step) !== onlyStep) return;
      views.forEach((view) => [view.artifact, view.hybrid].filter(Boolean).forEach((name) => names.add(name)));
    });
    if (!onlyStep || onlyStep === 6) {
      names.add("label_map_gen_preview.png");
      Object.values(state.data.presets?.variants || {}).forEach((variant) => {
        if (variant.preview_artifact) names.add(variant.preview_artifact);
      });
    }
    if (!onlyStep || onlyStep === 8) ["step8_braille.png", "step8_hybrid.png"].forEach((name) => names.add(name));
    if (!onlyStep || onlyStep === 9) ["step9_legend.png", "step9_legend_hybrid.png"].forEach((name) => names.add(name));
    await Promise.allSettled([...names].map(async (name) => {
      const source = project.session
        ? `/api/test/review-session/${project.session}/result/${ownerOf(name)}/${encodeURIComponent(name)}`
        : artifactUrl(project.stem, name);
      const response = await fetch(source, { cache: "no-store" });
      if (!response.ok) return;
      const url = URL.createObjectURL(await response.blob());
      const previous = project.images.get(name);
      project.images.set(name, url);
      if (previous) URL.revokeObjectURL(previous);
    }));
  },
  async select(step, afterApproval = false) {
    const project = this.project;
    if (!project || project.busy || state.busy && !afterApproval || hooks.updating()) return;
    project.busy = true;
    document.getElementById("control-content").inert = true;
    document.getElementById("visual-content").inert = true;
    hooks.sync();
    try {
      await project.ready;
      if (state.selected !== project.stem) return;
      // Let existing debounced label saves finish before changing their workspace.
      if (!afterApproval) await new Promise((resolve) => window.setTimeout(resolve, 450));
      while (pendingSaves) await pause();
      const previous = project.step;
      if (previous && !afterApproval) {
        project.snapshots.set(previous, clone(state.data));
        project.locals.set(previous, localDraft());
      }
      {
        const wasConnected = Boolean(project.session);
        const response = await transport(`/api/test/review-session/${encodeURIComponent(project.stem)}`, { method: "POST" });
        const result = await response.json();
        if (!response.ok || !result.session) throw new Error(result.error || "The saved review workspace could not be opened.");
        project.session = result.session;
        result.stale?.forEach((value) => project.stale.add(value));
        result.drafts?.forEach((value) => project.dirty.add(value));
        if (!wasConnected) await this.captureImages(project);
      }
      await sessionAction(project, "checkout", Number(step), previous && project.dirty.has(previous));
      project.step = Number(step);
      project.exporting = false;
      project.maskPhase = "mask";
      const draft = project.snapshots.get(project.step);
      await loadMaps();
      this.normalize();
      await refreshSelectedData();
      if (project.dirty.has(project.step) && project.locals.has(project.step)) {
        for (const key of ownedData[project.step]) state.data[key] = clone(draft[key]);
      }
      project.snapshots.set(project.step, clone(state.data));
      state.groupEdit = null;
      state.groupLabels = {};
      state.visibleGroupSlots = null;
      state.legendBox = null;
      state.patternDialog = null;
      state.maskBrush.strokes = [];
      state.previewLevel = Number(state.data.presets?.active_level) || 3;
      Object.assign(state, project.locals.get(project.step) || {});
      clearPreviewCache(project.stem);
      this.normalize();
      hooks.select(project.step);
    } catch (error) { toast(error.message, "error"); }
    finally {
      project.busy = false;
      document.getElementById("control-content").inert = false;
      document.getElementById("visual-content").inert = false;
      hooks.sync();
    }
  },
  normalize() {
    const project = this.project;
    if (!project?.step) return;
    const map = selectedMap();
    if (map) map.steps = { ...project.map.steps, ...map.steps,
      ...Object.fromEntries([...project.snapshots.keys()].map((step) => [String(step), true])) };
    state.activeStep = project.step;
    state.viewStep = String(project.step);
    state.individualRun = true;
    state.autorun = false;
  },
  markDirty() {
    if (this.active && !this.busy) this.project.dirty.add(this.step);
  },
  showSavedImages() {
    const project = this.project;
    if (!project?.session || !(project.step || project.exporting)) return;
    document.querySelectorAll("#visual-content img, #control-content img").forEach((image) => {
      const redirected = reviewUrl(image.src);
      if (redirected !== image.src) image.src = redirected;
      let name = image.dataset.artifact;
      if (!name) {
        try {
          const path = new URL(image.src, location.href).pathname;
          if (path.includes(`/api/artifact/${encodeURIComponent(project.stem)}/`)) name = decodeURIComponent(path.split("/").at(-1));
        } catch { return; }
      }
      if (!name) return;
      const owner = ownerOf(name);
      if (owner === project.step && project.dirty.has(owner)) return;
      const saved = project.images.get(name);
      if (saved && image.src !== saved) image.src = saved;
    });
    document.querySelectorAll('#control-content a[href*="/api/"], #visual-content a[href*="/api/"]')
      .forEach((link) => { link.href = reviewUrl(link.href); });
  },
  setMaskPhase(part) {
    const project = this.project;
    if (!project) return;
    project.maskPhase = part;
    hooks.select(2);
  },
  async advance() {
    const project = this.project;
    if (!project?.step || project.busy) return;
    const from = project.step;
    const next = from + 1;
    project.busy = true;
    state.autorun = false;
    document.getElementById("control-content").inert = true;
    document.getElementById("visual-content").inert = true;
    try {
      project.snapshots.set(from, clone(state.data));
      await sessionAction(project, "checkpoint", from, false, project.dirty.has(from));
      if (project.dirty.has(from)) {
        for (let step = next; step <= 9; step += 1) project.stale.add(step);
      }
      if (next <= 9 && project.stale.has(next)) {
        await runSteps(project.stem, [String(next)], state.model || state.defaultModel);
        do {
          await pause();
          state.job = await getJob(project.stem);
          hooks.sync();
        } while (state.job?.status === "running" && state.selected === project.stem);
        if (state.selected !== project.stem) return;
        if (state.job?.status === "failed") throw new Error(state.job.error || `Step ${next} could not finish.`);
        await loadMaps();
        await refreshSelectedData();
        await sessionAction(project, "checkpoint", next);
        project.snapshots.set(next, clone(state.data));
        await this.captureImages(project, next);
        project.stale.delete(next);
        project.dirty.delete(next);
      }
      project.dirty.delete(from);
      if (next > 9) {
        project.step = null;
        project.exporting = true;
        state.viewStep = null;
        state.activeStep = 9;
        state.individualRun = false;
        hooks.finish();
      } else {
        project.busy = false;
        await this.select(next, true);
      }
    } catch (error) { toast(error.message, "error"); }
    finally {
      project.busy = false;
      document.getElementById("control-content").inert = false;
      document.getElementById("visual-content").inert = false;
      hooks.sync();
    }
  },
  bind(root, step) {
    if (!this.active || root.dataset.testCompletedBound === String(step)) return root;
    const fresh = root.cloneNode(true);
    root.replaceWith(fresh);
    fresh.dataset.testCompletedBound = String(step);
    const next = () => this.advance();
    const binders = { 5: bindAggregationEditor, 6: bindSimplificationEditor,
      7: bindPatternEditor, 8: bindBrailleEditor, 9: bindLegendEditor };
    if (binders[step]) binders[step](next);
    if (step === 2 && this.maskPhase === "legend") bindLegendReviewEditor(next);
    if (step === 3) bindTextEditor();
    if ([1, 3, 4].includes(step)) {
      fresh.querySelector(":scope > h4")?.remove();
      const heading = document.createElement("h3");
      heading.textContent = ({ 1: "Map interpretation", 3: "Detected text", 4: "Segmented map" })[step];
      fresh.prepend(heading);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button primary full";
      button.dataset.testReviewNext = "true";
      button.textContent = step === 3 ? "Approve text & continue" : "Continue";
      button.addEventListener("click", async () => {
        if (state.busy || this.busy) return;
        button.disabled = true;
        try {
          if (step === 3 && this.project.dirty.has(3)) {
            const decisions = (state.data.labels?.occurrences || []).map((item) => ({ id: item.id,
              include: item.include !== false, remove: true,
              text: String(item.review_text || item.original_text || "").trim() }));
            await saveLabelReview(state.selected, decisions);
          }
          await next();
        } catch (error) { toast(error.message, "error"); }
        finally { if (button.isConnected) button.disabled = false; }
      });
      fresh.appendChild(button);
    }
    fresh.querySelectorAll(".reading-more-chip").forEach((button) => button.addEventListener("click", () => {
      fresh.querySelectorAll(".reading-extra-chip").forEach((chip) => { chip.hidden = false; });
      button.remove();
    }));
    return fresh;
  },
};

export function bindCompletedReviewNavigation(callbacks) {
  hooks = callbacks;
  installReviewTransport();
  document.getElementById("test-pipeline-progress").addEventListener("click", (event) => {
    const button = event.target.closest("[data-test-review-step]");
    if (button && !button.disabled) void completedReview.select(Number(button.dataset.testReviewStep));
  });
  const mark = (event) => {
    if (!completedReview.active || !event.target.closest("#control-content, #visual-content, .pattern-dialog")) return;
    if (event.target.closest(".page-view-toolbar, .test-legend-compare")) return;
    if (event.target.matches('#mask-radius, input[name="mask-mode"]')) return;
    completedReview.markDirty();
  };
  document.addEventListener("input", mark, true);
  document.addEventListener("change", mark, true);
  document.addEventListener("drop", mark, true);
  document.addEventListener("click", (event) => {
    if (!completedReview.active) return;
    const approval = event.target.closest("#approve-groups, #apply-preset, #approve-patterns, #approve-braille, #approve-legend");
    const approved = ({ 5: state.data.aggregation?.review?.approved, 6: selectedMap()?.step6_review_ready,
      7: state.data.step7Review?.approved, 8: state.data.step8Review?.approved, 9: state.data.step9Review?.approved })[completedReview.step];
    if (approval && approved && !completedReview.project.dirty.has(completedReview.step)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!approval.disabled && !state.busy && !hooks.updating()) void completedReview.advance();
      return;
    }
    if (event.target.closest("#braille-add, #braille-all-off, #toggle-north, #toggle-border, #fix-text-to-map, #group-map-elements, #toggle-all-text")) mark(event);
  }, true);
  let drag = null;
  document.addEventListener("pointerdown", (event) => {
    if (!completedReview.active || state.panMode || !event.target.closest("#visual-content .map-overlay, #visual-content .mask-canvas")) return;
    drag = { stem: state.selected, step: completedReview.step, x: event.clientX, y: event.clientY };
  }, true);
  document.addEventListener("pointerup", (event) => {
    if (drag && drag.stem === state.selected && drag.step === completedReview.step
        && (drag.x !== event.clientX || drag.y !== event.clientY)) completedReview.markDirty();
    drag = null;
  }, true);
}
