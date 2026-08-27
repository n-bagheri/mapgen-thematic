"use strict";

import {
  $, artifactJson, getAggregationReview, getBrailleLayout, getCategoryColors,
  getJob, getLabelReview, getLegendLayout, getLegendReview, getLineReview, getMaps, getMaskReview,
  getModels, getPageLayout, getPatternData, getSpec, getStep6Params, getStep6Presets,
  getStep7Review, getStep8Review, getStep9Review,
  runSteps, saveStep6Params, staleServerAdvice, uploadFile,
} from "./api.js";
import { blockingReason, completedCount, viewedStep } from "./steps.js";
import {
  currentStepKey, individualModeFor, isRunning, navCoversWorkspace, rememberIndividualMode,
  selectedMap, serverNotice, setNav, state, toast,
} from "./state.js";
import { renderProjectList } from "./library.js";
import { continuePipeline, renderControls } from "./controls.js";
import { clearPreviewCache, preloadPreviews } from "./preview-cache.js";
import { renderVisual } from "./visual.js";

/* Loading, run control, and the polling loop.  Everything the panes draw comes
   from `state.data`, which is filled here in one parallel pass per map. */

export async function loadMaps() {
  const payload = await getMaps();
  state.maps = payload.maps || [];
  // Every list answer re-states the server's vintage, so the bar appears the
  // moment the checkout moves underneath a server that is still running.
  serverNotice(staleServerAdvice());
  renderProjectList();
}

export async function loadSpec() {
  try {
    state.spec = JSON.parse((await getSpec()).spec);
  } catch {
    state.spec = null;
  }
}

export async function loadModels() {
  try {
    const payload = await getModels();
    state.models = payload.models || [];
    state.defaultModel = payload.default || state.models[0]?.id || "";
    if (!state.model) state.model = state.defaultModel;
  } catch {
    state.models = [];
  }
}

export async function uploadMap(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    toast("Adding your map…");
    const result = await uploadFile(form);
    await loadMaps();
    const uploaded = state.maps.find((map) => map.name === result.name);
    if (uploaded) await selectMap(uploaded.stem);
    toast("Map added.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("upload-input").value = "";
  }
}

export async function selectMap(stem) {
  if (!state.maps.some((map) => map.stem === stem)) return;
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.selected = stem;
  clearPreviewCache();
  state.generation += 1;
  state.data = {};
  state.job = selectedMap()?.job || { status: "idle" };
  state.pollVisualSignature = "";
  state.pollControlSignature = "";
  state.previewLevel = null;
  state.viewStep = null;
  state.groupEdit = null;
  state.groupLabels = {};
  state.visibleGroupSlots = null;
  if (state.aggregationPreviewUrl) URL.revokeObjectURL(state.aggregationPreviewUrl);
  state.aggregationPreviewUrl = null;
  state.autorun = false;
  state.renaming = null;
  state.individualRun = individualModeFor(selectedMap());
  if (state.individualRun) rememberIndividualMode(stem, true);
  state.runSetupOpen = completedCount(selectedMap()) === 0 && !isRunning(selectedMap());
  state.runSetupDraft = null;
  state.runSetupModelDraft = state.model || state.defaultModel;
  state.runSetupDirty = false;
  state.customPage = false;
  state.patternGroup = 0;
  state.patternDialog = null;
  state.showOriginalMap = false;
  state.showFinalMap = false;
  state.panMode = false;
  state.colourView = false;
  state.maskBrush = { active: false, mode: "erase", radius: 12, strokes: [] };
  state.legendBox = null;
  state.lineDrawing = { active: false, draft: [], addedIds: [] };
  state.activeStep = null;   // follow the run until the reader opens a step
  if (navCoversWorkspace()) setNav(false);   // otherwise the library stays open
  renderProjectList();
  await refreshSelectedData();
  renderWorkspace(true);
  $("visual-pane").focus({ preventScroll: true });
  if (isRunning(selectedMap())) startPolling();
}

/** Every per-map fetch at once.  A generation counter discards a slow response
 *  that lands after the reader has already moved to another map. */
export async function refreshSelectedData() {
  const map = selectedMap();
  if (!map) return;
  const stem = map.stem;
  const generation = ++state.generation;
  const safe = (promise) => promise.catch(() => null);
  const when = (condition, factory) => condition ? safe(factory()) : Promise.resolve(null);
  const tasks = {
    step6: safe(getStep6Params(stem)),
    semantics: when(map.steps?.["1"], () => artifactJson(stem, "step1_semantics.json")),
    mask: when(map.steps?.["2"], () => getMaskReview(stem)),
    legendReview: when(map.steps?.["2"], () => getLegendReview(stem)),
    labels: when(map.steps?.["3"], () => getLabelReview(stem)),
    lines: when(map.steps?.["4"], () => getLineReview(stem)),
    classesFinal: when(map.steps?.["4"], () => artifactJson(stem, "classes_final.json")),
    aggregation: when(map.steps?.["5"], () => getAggregationReview(stem)),
    presets: when(map.steps?.["6"], () => getStep6Presets(stem)),
    lineGeo: when(map.steps?.["6"], () => artifactJson(stem, "lines_gen.geojson")),
    classesGen: when(map.steps?.["6"], () => artifactJson(stem, "classes_gen.json")),
    patterns: when(map.steps?.["7"], () => getPatternData(stem)),
    colors: when(map.steps?.["7"], () => getCategoryColors(stem)),
    pageLayout: when(map.steps?.["7"], () => getPageLayout(stem)),
    step7Review: when(map.steps?.["7"], () => getStep7Review(stem)),
    braille: when(map.steps?.["8"], () => getBrailleLayout(stem)),
    step8Review: when(map.steps?.["8"], () => getStep8Review(stem)),
    legend: when(map.steps?.["9"], () => getLegendLayout(stem)),
    step9Review: when(map.steps?.["9"], () => getStep9Review(stem)),
    job: safe(getJob(stem)),
  };
  const entries = await Promise.all(
    Object.entries(tasks).map(async ([key, promise]) => [key, await promise]));
  if (generation !== state.generation || state.selected !== stem) return;
  state.data = Object.fromEntries(entries);
  if (state.data.job) state.job = state.data.job;
  state.previewLevel = state.previewLevel
    || Number(state.data.presets?.active_level)
    || Number(state.data.step6?.params?.simplification_level)
    || 3;
  if (!state.model) state.model = state.defaultModel;
  // Prime only the picture the reader is about to see. Waiting for every
  // slider variant here held the entire workspace behind five independent
  // image requests. The remaining variants warm in the background so slider
  // movement is still instant once the Step 6 decision appears.
  const previews = [];
  const foreground = [];
  const visibleStep = viewedStep(map, state.activeStep);
  if (map.steps?.["5"]) {
    previews.push("step5_aggregation_preview.png");
    if (visibleStep === 5 || visibleStep === 6 && !map.steps?.["6"]) {
      foreground.push("step5_aggregation_preview.png");
    }
  }
  const activeVariant = state.data.presets?.variants?.[String(state.previewLevel)];
  Object.values(state.data.presets?.variants || {}).forEach((variant) => {
    previews.push(variant?.preview_artifact);
  });
  if (visibleStep === 6 && activeVariant?.preview_artifact) {
    foreground.push(activeVariant.preview_artifact);
  }
  if (map.steps?.["7"]) {
    previews.push("step8a_cleanup.png");
    if (state.data.step7Review?.create_hybrid_map) previews.push("step8a_hybrid.png");
  }
  await preloadPreviews(stem, foreground);
  // preloadPreviews absorbs individual request failures. Deliberately do not
  // await this warm-up: it must never delay rendering the foreground image.
  void preloadPreviews(stem, previews.filter((name) => !foreground.includes(name)));
}

export function renderWorkspace(revealActive = false) {
  const map = selectedMap();
  document.body.classList.toggle("no-map", !map);
  $("welcome-card").hidden = Boolean(map);
  $("visual-content").hidden = !map;
  $("control-content").hidden = !map;
  renderProjectList();
  if (!map) return;
  renderVisual();
  renderControls();
  if (revealActive) {
    window.requestAnimationFrame(() => $("visual-pane")?.scrollTo({ top: 0 }));
  }
}

/* ---------------------------------------------------------- run control --- */

/** Step 6 owns the simplification inputs, so the setup card only carries
 *  whatever was saved before into this run. */
export async function savePreflight() {
  const current = state.data.step6?.params || {};
  const level = Number(current.simplification_level) || Number(state.previewLevel) || 3;
  state.model = $("model-select")?.value || state.model || state.defaultModel;
  await saveStep6Params(state.selected, {
    simplification_level: level,
    keep_line_kinds: current.keep_line_kinds || [],
    protected_classes: current.protected_classes || [],
  });
  state.previewLevel = level;
}

export async function startJob(steps) {
  if (!steps?.length) return;
  clearPreviewCache(state.selected);
  await runSteps(state.selected, steps, state.model || state.defaultModel);
  state.job = { status: "running", steps, current: null, log: [] };
  // /api/run has already invalidated the requested Step 6 output, while the
  // local map snapshot still describes the pre-run files until the first poll.
  // Reflect that immediately so the middle panel uses its ready Step 5 input
  // as a stable placeholder instead of requesting just-deleted Step 6 PNGs.
  if (steps.map(String).includes("6")) {
    const map = selectedMap();
    if (map?.steps) map.steps["6"] = false;
  }
  state.viewStep = null;  // a fresh run follows itself again
  state.activeStep = Number(steps[0]);
  renderWorkspace(true);
  state.pollVisualSignature = pollingVisualSignature(selectedMap());
  state.pollControlSignature = pollingControlSignature(selectedMap());
  startPolling(true);
}

/* ------------------------------------------------------------- polling --- */

function completedSignature(map) {
  return ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    .map((key) => map?.steps?.[key] ? "1" : "0").join("");
}

function pollingVisualSignature(map) {
  return [
    map?.stem || "",
    completedSignature(map),
    currentStepKey(map) || "",
    state.previewLevel || "",
    String(state.colourView),
  ].join("|");
}

function pollingControlSignature(map) {
  return [
    state.job.status || "idle",
    map?.job?.current ?? "",
    state.job.current ?? "",
    state.job.error || "",
    (state.job.steps || []).join(","),
    completedSignature(map),
    String(state.data.mask?.approved),
    String(state.data.legendReview?.approved),
    state.data.legendReview?.status || "",
    String(map?.step5_review_ready),
    String(map?.step6_review_ready),
    String(map?.step7_review_ready),
    String(map?.step8_review_ready),
    String(map?.step9_review_ready),
    String(map?.in_scope),
    map?.pipeline_error || "",
    map?.step1_error || "",
    state.viewStep || "auto",
  ].join("|");
}

/** Redraw only the pane whose inputs actually changed, keeping the reader's
 *  scroll position and the disclosure they left open. */
function renderPollingState(map) {
  if (!map) return;
  if (state.job.status === "running" && !state.viewStep) {
    const current = currentStepKey(map);
    if (current) state.activeStep = Number(current);
  }
  const visualSignature = pollingVisualSignature(map);
  const controlSignature = pollingControlSignature(map);
  const visualChanged = visualSignature !== state.pollVisualSignature;
  const controlChanged = controlSignature !== state.pollControlSignature;

  if (visualChanged) {
    renderVisual();
    state.pollVisualSignature = visualSignature;
  }
  if (controlChanged) {
    const pane = $("control-pane");
    const scrollTop = pane?.scrollTop || 0;
    const progressOpen = Boolean(document.querySelector(".progress-disclosure")?.open);
    renderControls();
    const disclosure = document.querySelector(".progress-disclosure");
    if (progressOpen && disclosure) disclosure.open = true;
    if (pane) pane.scrollTop = scrollTop;
    state.pollControlSignature = controlSignature;
  }
  if (visualChanged || controlChanged) renderProjectList();
}

function startPolling(immediate = false) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollFailures = 0;
  const tick = async () => {
    const stem = state.selected;
    if (!stem) return;
    try {
      const [job, mapsPayload] = await Promise.all([getJob(stem), getMaps()]);
      if (state.selected !== stem) return;
      state.pollFailures = 0;
      state.job = job || { status: "idle" };
      state.maps = mapsPayload.maps || [];
      serverNotice(staleServerAdvice());
      renderPollingState(selectedMap());
      if (state.job.status === "running") {
        state.pollTimer = window.setTimeout(tick, 1200);
        return;
      }
      await refreshSelectedData();
      if (state.job.status === "failed") {
        state.autorun = false;
        renderWorkspace(true);
        toast(state.job.error || "The pipeline stopped. Review the failure panel to retry.", "error");
        return;
      }
      const map = selectedMap();
      const blocked = blockingReason(map);
      if (blocked) {
        state.autorun = false;
        renderWorkspace(true);
        toast(blocked, "error");
        return;
      }
      // Step 2 pauses first for map-mask confirmation, then for legend
      // confirmation on the shared middle-pane source image.
      if (state.autorun && map?.steps?.["2"] && !map.steps?.["3"]
          && (!state.data.mask?.approved || !state.data.legendReview?.approved)) {
        state.activeStep = 2;
        state.maskBrush.active = !state.data.mask?.approved;
        renderWorkspace(true);
        toast(state.data.mask?.approved
          ? "Review and approve the detected legend to continue."
          : "Review and approve the detected map area to continue.", "warning");
        return;
      }
      // The second gate is the Step 5 category review. Once each decision is
      // complete, continuePipeline starts the next automatic batch.
      if (state.autorun && map?.steps?.["5"] && !map.step5_review_ready) {
        state.activeStep = 5;
        renderWorkspace(true);
        return;
      }
      // Step 6 prepares all five variants, but patterns must wait until the
      // reader explicitly chooses which simplification level is active.
      if (state.autorun && map?.steps?.["6"] && !map.steps?.["7"]
          && !map.step6_review_ready) {
        state.activeStep = 6;
        renderWorkspace(true);
        return;
      }
      // Step 7 produces the finished tactile master, then pauses before
      // Braille so its pattern and optional hybrid-colour decisions are human.
      if (state.autorun && map?.steps?.["7"] && !map.steps?.["8"]
          && !map.step7_review_ready) {
        state.activeStep = 7;
        renderWorkspace(true);
        return;
      }
      if (state.autorun && map && !map.steps?.["9"]) {
        await continuePipeline();
        return;
      }
      if (map?.steps?.["9"]) {
        state.autorun = !map.step9_review_ready;
        state.activeStep = 9;
        toast(map.step9_review_ready
          ? "Your tactile map and its legend are ready to export."
          : "The legend page is ready for final review.");
      } else {
        // A single-step or manually resumed job does not enter the automatic
        // decision branches above. Follow the result it just completed instead
        // of falling back to Step 6 after every later job.
        const latest = ["8", "7", "6", "5", "4", "3", "2", "1"]
          .find((step) => map?.steps?.[step]);
        if (latest) state.activeStep = Number(latest);
      }
      renderWorkspace(true);
    } catch (error) {
      if (state.selected !== stem) return;
      state.pollFailures += 1;
      if (state.pollFailures === 1) {
        toast("Live updates were interrupted. Retrying automatically.", "warning");
      }
      if (state.pollFailures >= 6) {
        state.pollTimer = null;
        toast("Live updates paused. Reselect the map to reconnect.", "error");
        return;
      }
      const delay = Math.min(15000, 900 * (2 ** state.pollFailures));
      state.pollTimer = window.setTimeout(tick, delay);
    }
  };
  state.pollTimer = window.setTimeout(tick, immediate ? 0 : 900);
}
