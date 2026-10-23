"use strict";

import {
  $, approveLegendReview, approveMask, artifactJson, artifactUrl, deleteMap, esc, getJob, mapUrl,
  patternLibraryPreviewUrl, patternPreviewUrl, renameMap, resetMask, saveBrailleLayout, saveCategoryColors, saveMaskStrokes,
  saveLegendBox, saveStep7Review, uploadFile,
} from "../minimal/api.js";
import { isRunning, selectedMap, setNav, state, toast, withBusy } from "../minimal/state.js";
import { completedCount, STEP_DEFS, STEP_VIEWS } from "../minimal/steps.js";
import { activeStep, refreshStepImages, renderVisual } from "../minimal/visual.js";
import { continuePipeline, renderControls } from "../minimal/controls.js";
import { bindMaskCanvas, commitMaskStrokes, resetMaskSurface, undoMaskStroke } from "./mask.js";
import { bindLegendReviewOverlay } from "../minimal/editors/legendreview.js";
import { bindBrailleOverlay } from "../minimal/editors/braille.js";
import { enhanceTestBrailleOverlay, prepareTestBrailleControls } from "./braille.js";
import { bindViewer, viewerToolbarHtml } from "../minimal/viewer.js";
import { clearPreviewCache, preloadPreviews } from "../minimal/preview-cache.js";
import { patternChoiceBlock, patternChoiceWarnings } from "./pattern-policy.js";
import { bindCompletedReviewNavigation, completedReview } from "./review-navigation.js";
import {
  loadMaps, loadModels, loadSpec, refreshSelectedData, renderWorkspace, selectMap, uploadMap,
} from "../minimal/workspace.js";

/* /test adds project opening and a three-panel shell around the focused UI.
   The visual output, automatic run, reviews, and export controls still come
   from the same modules as /minimal. */

const FLOW_SCREENS = ["test-home-screen", "test-about-screen"];
const TEMPLATE_MAPS = [
  { file: "andhra_soil.png", label: "Andhra Pradesh soil" },
  { file: "australia_landuse.png", label: "Australia land use" },
  { file: "belgium_landuse.png", label: "Belgium land use" },
  { file: "cambodia_ethinc.png", label: "Cambodia ethnic groups" },
  { file: "finland_forestry.jpg", label: "Finland forestry" },
  { file: "france_env.jpg", label: "France environment" },
  { file: "iran_landuse.png", label: "Iran land use" },
  { file: "iran_temp.png", label: "Iran temperature" },
  { file: "japan_landuse.jpg", label: "Japan land use" },
  { file: "liberia_population.png", label: "Liberia population" },
  { file: "madagascar_vegetation.png", label: "Madagascar vegetation" },
  { file: "morocco_humidity.jpg", label: "Morocco humidity" },
  { file: "morocco_population.png", label: "Morocco population" },
  { file: "netherlands_landuse.png", label: "Netherlands land use" },
  { file: "norway_landuse.png", label: "Norway land use" },
  { file: "poland_vegetation.png", label: "Poland vegetation" },
  { file: "senegral_rainfall.png", label: "Senegal rainfall" },
  { file: "southafrica_agriculture.png", label: "South Africa agriculture" },
  { file: "spain_landuse.png", label: "Spain land use" },
  { file: "telangana_soil.png", label: "Telangana soil" },
  { file: "world_climate.png", label: "World climate" },
];
const PROJECT_DATE_FILES = {
  1: ["step1_semantics.json"],
  2: ["step2_debug.png", "map_mask_review.json", "legend_review.json"],
  3: ["labels.json", "label_review.json"],
  4: ["lines.geojson", "line_review.json"],
  5: ["aggregation.json", "aggregation_review.json"],
  6: ["step6_summary.json", "step6_review.json"],
  7: ["step7_tactile.png", "step7_review.json"],
  8: ["step8_braille.png", "braille_labels.json", "step8_review.json"],
  9: ["step9_legend.png", "step9_review.json"],
};
const THINKING_MESSAGES = {
  "1": "AI is interpreting the map…",
  "2": "Finding the map and separating it from the rest of the image…\nAlmost there — getting it ready for your review.",
  "3": "Reading labels, place names, titles, and other text…",
  "4": "Breaking the map into meaningful regions…",
  "5": "Bringing similar map classes together…",
  "6": "Cleaning up fine detail while keeping the map recognizable…",
  "7": "Giving each class a distinct tactile texture…",
  "8": "Placing Braille labels…",
  "9": "Building the legend for the final map…",
};
const REVIEW_MESSAGES = {
  "2": "Map found! Adjust the selection if needed.",
  "5": "Take a look at the classes and adjust anything that doesn’t look right.",
  "6": "Adjust the simplification level however you like.",
  "7": "Change the textures, swap them around, or add color if you’d like.",
  "8": "Move the labels around, edit them, or remove any you don’t need.",
  "9": "Give everything one final check.",
};
const TEST_REVIEW_PANELS = {
  reading: {
    selector: '[data-editor="reading"]',
    title: "Review the map interpretation", action: "[data-test-review-next]",
  },
  text: {
    selector: '[data-editor="text"]',
    title: "Review the detected text", action: "[data-test-review-next]",
  },
  segmentation: {
    selector: ".test-segmentation-review", title: "Review the segmented map",
    action: "[data-test-review-next]",
  },
  mask: {
    selector: ".mask-decision",
    title: "Review the detected mask",
    intro: "Use the brush to restore map areas that were mistakenly masked, and the eraser to remove unwanted areas.",
    action: null,
  },
  legend: {
    selector: ".legend-review",
    title: "Review the detected legend",
    intro: "Move or resize the highlighted box until it encloses the complete legend.",
    action: ".legend-review-actions",
  },
  savedLegend: {
    selector: ".legend-review", title: "Review the saved legend",
    intro: "This completed result uses its saved category reading. Continue to review the detected text.",
    action: null,
  },
  aggregation: {
    selector: '.step-section[data-step="5"] .review-gate',
    title: "Review the suggested tactile categories",
    intro: "This map has more than five legend categories, so some need to be grouped into broader categories, with a maximum of five in total. Drag each legend category into the broader category it belongs to, then approve the grouping.",
    action: ".action-row.end",
    primary: "#approve-groups",
    primaryText: "Approve categories & continue",
  },
  simplification: {
    selector: ".simplification-decision",
    title: "Choose the simplification level",
    intro: "Use the slider to choose how much the map should be simplified.",
    action: "#apply-preset",
  },
  patterns: {
    selector: ".pattern-decision",
    title: "Review the tactile patterns",
    intro: "Review the assigned tactile patterns and adjust them if needed. You can also add colors to create a hybrid tactile map.",
    action: "#approve-patterns",
    primary: "#approve-patterns",
    primaryText: "Approve textures & continue",
  },
  braille: {
    selector: ".braille-decision",
    title: "Review the Braille labels",
    intro: "Review the Braille labels and add or edit them as needed.",
    action: "#approve-braille",
    primary: "#approve-braille",
    primaryText: "Approve labels & continue",
  },
  finalLegend: {
    selector: ".legend-decision",
    title: "Review the tactile legend",
    intro: "Review the tactile legend, its labels, title, and page layout, and adjust them if needed.",
    action: "#approve-legend",
    primary: "#approve-legend",
    primaryText: "Approve legend & continue",
  },
  export: {
    selector: ".export-panel.is-ready",
    title: "Export tactile map",
    action: ".export-row",
  },
};
const STEP_LABELS = {
  "1": "Read image",
  "2": "Locate map",
  "3": "Read text",
  "4": "Segment map",
  "5": "Aggregate classes",
  "6": "Simplify",
  "7": "Assign textures",
  "8": "Add Braille labels",
  "9": "Generate legend",
};

let deleteStem = null;
let renameStem = null;
const STAGE_DWELL_MS = 2500;
const ANALYSIS_SEQUENCE = [3, 4, 5];
const TEST_PREVIEW_PIN = "__test_stable_preview__";
const EXTRACTED_PREVIEW_VIEW = 99;
const stagePresentation = { stem: null, step: 0, holdUntil: 0 };
const previewPresentation = { stem: null, step: 0, pendingUntil: 0, locked: false };
const forcedAnalysisPresentation = { active: false, index: 0, holdUntil: 0 };
const hybridStateByProject = new Map();
const suggestedPatternDefaultsByProject = new Map();
const reviewScrollByProject = new Map();
const finalStageDefaultsApplied = new Set();
let finishedJobRefresh = null;
let finishedJobRefreshAt = 0;
let titleEditSaving = false;
let pipelineStartPending = false;
let pipelineStartTimer = null;
let reopenedProjectReview = null;
let reviewResize = null;
let reviewPanelObserver = null;
let reviewPanelStabilizeQueued = false;
let visualPanelObserver = null;
let visualPanelStabilizeQueued = false;
let testMaskReviewStem = null;
let testMaskBrushMode = null;
let testPatternTransformTimer = null;
let pendingTestPatternTransform = null;
let hybridVisualTransition = null;
let testPatternTaskQueue = Promise.resolve();
const testPatternPendingByProject = new Map();
const testSuggestedIntentByProject = new Map();
const testPatternSwatchUrls = new Map();
const testPatternStatusByProject = new Map();
const testPatternErrorsByProject = new Map();
let pendingTestPatternColours = null;
let testPatternColourTimer = null;
let testPatternChoicePending = false;

const DELETE_ICON = `<svg viewBox="0 0 32 32" aria-hidden="true">
  <path d="M7.5 9.5h17M12 9.5V6.8h8v2.7M10 12.5l1 13h10l1-13M14 14.5v8M18 14.5v8"></path></svg>`;

/* ---------------------------------------------------------- navigation */

function leavePipelineShell() {
  completedReview.leave();
  reopenedProjectReview = null;
  clearPipelineStartStatus();
  finishPipelineTitleEdit(false);
  clearStablePreview();
  clearTestMaskToolSelection();
  document.body.classList.remove(
    "test-pipeline-open", "test-source-collapsed", "test-job-running",
    "test-review-active", "test-job-failed", "test-export-ready", "test-show-controls",
    "test-preview-frozen", "test-status-visible", "test-hybrid-enabled",
    "test-hybrid-view-visible",
  );
  $("test-pipeline-header").hidden = true;
}

function showFlowScreen(id) {
  leavePipelineShell();
  document.body.classList.add("test-flow-open");
  FLOW_SCREENS.forEach((screenId) => { $(screenId).hidden = screenId !== id; });
  const title = id === "test-about-screen" ? $("test-about-title") : $("test-home-title");
  title?.focus({ preventScroll: true });
}

function showHome() {
  showFlowScreen("test-home-screen");
  $("test-choice-status").textContent = "";
  $("test-project-status").textContent = "";
  renderProjectGallery();
}

function showAbout() {
  showFlowScreen("test-about-screen");
}

async function enterPipeline(stem) {
  if (stem && state.selected !== stem) await selectMap(stem);
  await completedReview.restoreAvailability(selectedMap());
  state.individualRun = false;
  resetStagePresentation();
  restoreProjectReview(selectedMap());
  applyTestVisualPreferences();
  renderWorkspace();
  FLOW_SCREENS.forEach((screenId) => { $(screenId).hidden = true; });
  document.body.classList.remove("test-flow-open");
  document.body.classList.add("test-pipeline-open");
  $("test-pipeline-header").hidden = false;
  $("test-start-status").textContent = "";
  setNav(false);
  syncPipelineChrome();
  $("visual-pane")?.focus({ preventScroll: true });
}

/* ----------------------------------------------------------- API key */

function askForApiKey() {
  $("test-key-input").value = "";
  $("test-key-status").textContent = "";
  $("test-key-dialog").showModal();
  $("test-key-input").focus();
}

async function storeSessionKey(key) {
  const response = await fetch("/api/session-key", {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "The API key could not be saved.");
}

async function confirmApiKey(event) {
  event.preventDefault();
  const input = $("test-key-input");
  const button = $("test-confirm-key");
  const key = input.value.trim();
  if (!key) {
    $("test-key-status").textContent = "Please enter an API key.";
    input.focus();
    return;
  }
  button.disabled = true;
  $("test-key-status").textContent = "Saving…";
  try {
    await storeSessionKey(key);
    input.value = "";
    $("test-key-dialog").close();
    showHome();
    await refreshHomeProjects();
  } catch (error) {
    $("test-key-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/* ----------------------------------------------- templates and projects */

function renderTemplateGallery() {
  const strip = $("test-template-strip");
  strip.querySelectorAll("[data-template-file]").forEach((card) => card.remove());
  strip.insertAdjacentHTML("beforeend", TEMPLATE_MAPS.map((template) => `
    <button class="test-template-card" type="button"
            data-template-file="${esc(template.file)}" aria-label="Create project from ${esc(template.label)}">
      <span class="test-template-preview">
        <img src="/test/map-library/${encodeURIComponent(template.file)}"
             alt="" loading="lazy" decoding="async">
      </span>
      <span class="test-template-name" title="${esc(template.label)}">${esc(template.label)}</span>
    </button>`).join(""));
  window.requestAnimationFrame(updateTemplateScrollButtons);
}

function updateTemplateScrollButtons() {
  const strip = $("test-template-strip");
  const maximum = Math.max(0, strip.scrollWidth - strip.clientWidth);
  $("test-template-previous").disabled = strip.scrollLeft <= 2;
  $("test-template-next").disabled = strip.scrollLeft >= maximum - 2;
}

function scrollTemplates(direction) {
  const strip = $("test-template-strip");
  strip.scrollBy({ left: direction * Math.max(240, strip.clientWidth * .72), behavior: "smooth" });
}

function setHomeBusy(busy) {
  $("test-add-map").disabled = busy;
  $("test-refresh-projects").disabled = busy;
  $("test-template-strip").querySelectorAll("[data-template-file]")
    .forEach((button) => { button.disabled = busy; });
}

async function createFromTemplate(fileName) {
  const template = TEMPLATE_MAPS.find((item) => item.file === fileName);
  if (!template) return;
  setHomeBusy(true);
  $("test-choice-status").textContent = `Creating ${template.label} project…`;
  try {
    const response = await fetch(`/test/map-library/${encodeURIComponent(template.file)}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("This map could not be loaded from the library.");
    const blob = await response.blob();
    await createProject(new File([blob], template.file, { type: blob.type || "image/png" }));
  } catch (error) {
    $("test-choice-status").textContent = error.message;
    setHomeBusy(false);
  }
}

function formatModifiedDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Recently";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(date);
}

async function hydrateProjectDates() {
  const records = Array.from($("test-project-grid").querySelectorAll("[data-project-date]"));
  await Promise.all(records.map(async (element) => {
    const map = state.maps.find((item) => item.stem === element.dataset.projectDate);
    if (!map) return;
    try {
      const latestStep = Object.entries(map.steps || {}).reduce(
        (latest, [step, complete]) => complete ? Math.max(latest, Number(step)) : latest, 0,
      );
      const artifactUrls = (PROJECT_DATE_FILES[latestStep] || []).map((name) =>
        `/api/artifact/${encodeURIComponent(map.stem)}/${encodeURIComponent(name)}`);
      const responses = await Promise.all([mapUrl(map.name), ...artifactUrls].map((url) =>
        fetch(url, { method: "HEAD", cache: "no-store" }).catch(() => null)));
      const modified = responses.reduce((latest, response) => {
        if (!response?.ok) return latest;
        const date = new Date(response.headers.get("Last-Modified") || "");
        return Number.isNaN(date.getTime()) || date <= latest ? latest : date;
      }, new Date(0));
      if (!modified.getTime()) return;
      element.dateTime = modified.toISOString();
      element.textContent = formatModifiedDate(modified);
    } catch { /* The project remains usable even if its date is unavailable. */ }
  }));
}

function renderProjectGallery() {
  const grid = $("test-project-grid");
  $("test-project-empty").hidden = state.maps.length > 0;
  grid.hidden = state.maps.length === 0;
  grid.innerHTML = state.maps.map((map) => {
    const locked = isRunning(map);
    const name = esc(map.stem);
    const stem = esc(map.stem);
    return `<article class="test-project-card" data-project-stem="${stem}">
      <button class="test-project-action delete" type="button" data-action="delete"
              aria-label="Delete ${name}" title="${locked ? "Available when processing finishes" : "Delete project"}"
              ${locked ? "disabled" : ""}>${DELETE_ICON}</button>
      <button class="test-project-open" type="button" data-action="open" aria-label="Open ${name}">
        <img class="test-project-image" src="${mapUrl(map.name)}" alt="">
        <span class="test-project-name" title="${name}">${name}</span>
        <time class="test-project-date" data-project-date="${stem}">Recently</time>
      </button>
    </article>`;
  }).join("");

  grid.querySelectorAll("[data-project-stem]").forEach((card) => {
    const stem = card.dataset.projectStem;
    card.querySelector('[data-action="open"]').addEventListener("click", () => openProject(stem));
    card.querySelector('[data-action="delete"]').addEventListener("click", () => askToDelete(stem));
  });
  void hydrateProjectDates();
}

async function refreshHomeProjects() {
  const button = $("test-refresh-projects");
  button.disabled = true;
  $("test-project-status").textContent = "Loading recent projects…";
  try {
    await loadMaps();
    renderProjectGallery();
    $("test-project-status").textContent = "";
  } catch (error) {
    $("test-project-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function openProject(stem) {
  $("test-project-status").textContent = "Opening project…";
  try {
    await enterPipeline(stem);
    $("test-project-status").textContent = "";
  } catch (error) {
    $("test-project-status").textContent = error.message;
  }
}

async function createProject(file) {
  if (!file) return;
  setHomeBusy(true);
  const form = new FormData();
  form.append("file", file);
  $("test-choice-status").textContent = "Creating project…";
  try {
    const result = await uploadFile(form);
    await loadMaps();
    const uploaded = state.maps.find((map) => map.name === result.name);
    if (!uploaded) throw new Error("The project was created but could not be opened.");
    await enterPipeline(uploaded.stem);
    $("test-choice-status").textContent = "";
    toast("Project created.");
  } catch (error) {
    $("test-choice-status").textContent = error.message;
    renderProjectGallery();
  } finally {
    setHomeBusy(false);
    $("test-new-project-input").value = "";
  }
}

/* ----------------------------------------------------- project actions */

function askToDelete(stem) {
  deleteStem = stem;
  $("test-delete-dialog").showModal();
}

async function confirmDelete() {
  const button = $("test-confirm-delete");
  if (!deleteStem || button.disabled) return;
  const fromPipeline = document.body.classList.contains("test-pipeline-open");
  button.disabled = true;
  try {
    await deleteMap(deleteStem);
    if (state.selected === deleteStem) {
      state.selected = null;
      state.data = {};
      state.job = { status: "idle" };
      renderWorkspace();
    }
    deleteStem = null;
    $("test-delete-dialog").close();
    await loadMaps();
    renderProjectGallery();
    $("test-project-status").textContent = "Project deleted.";
    if (fromPipeline) showHome();
  } catch (error) {
    $("test-delete-dialog").close();
    const target = fromPipeline ? $("test-start-status") : $("test-project-status");
    target.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function askToRename(stem) {
  const map = state.maps.find((item) => item.stem === stem);
  if (!map) return;
  renameStem = stem;
  $("test-rename-input").value = map.stem;
  $("test-rename-status").textContent = "";
  $("test-rename-dialog").showModal();
  $("test-rename-input").select();
}

async function confirmRename(event) {
  event.preventDefault();
  const button = $("test-confirm-rename");
  if (!renameStem || button.disabled) return;
  const name = $("test-rename-input").value.trim();
  if (!name) return;
  button.disabled = true;
  $("test-rename-status").textContent = "Saving…";
  try {
    const result = await renameMap(renameStem, name);
    if (state.selected === renameStem) state.selected = result.stem;
    renameStem = null;
    $("test-rename-dialog").close();
    await loadMaps();
    renderProjectGallery();
    $("test-project-status").textContent = "Project renamed.";
    syncPipelineChrome();
  } catch (error) {
    $("test-rename-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------ pipeline shell */

function latestCompletedStep(map) {
  return STEP_DEFS.filter((step) => map?.steps?.[step.key]).at(-1)?.key || null;
}

function restoreProjectReview(map) {
  const step = latestCompletedStep(map);
  reopenedProjectReview = map && !isRunning(map) && state.job?.status !== "failed"
    && !map.pipeline_error && !map.step1_error && map.in_scope !== false && REVIEW_MESSAGES[step]
    ? { stem: map.stem, step } : null;
  if (reopenedProjectReview) {
    state.viewStep = null;
    state.activeStep = Number(pipelineReviewStep(map));
  }
}

function reopenedReviewStep(map) {
  if (!reopenedProjectReview || reopenedProjectReview.stem !== map?.stem) return null;
  if (isRunning(map) || latestCompletedStep(map) !== reopenedProjectReview.step) {
    reopenedProjectReview = null;
    return null;
  }
  return reopenedProjectReview.step;
}

function watchReopenedLegendApproval() {
  const review = reopenedProjectReview;
  if (!review || review.step !== "9" || state.busy) return;
  const previous = state.data.step9Review;
  const check = () => {
    if (reopenedProjectReview !== review || state.selected !== review.stem) return;
    if (state.busy) {
      window.setTimeout(check, 80);
      return;
    }
    // The existing editor replaces this response only after saving approval.
    // Keep the restored panel available if approval failed.
    if (state.data.step9Review !== previous && state.data.step9Review?.approved) {
      reopenedProjectReview = null;
      renderWorkspace(true);
      syncPipelineChrome();
    }
  };
  window.setTimeout(check, 0);
}

function pipelineReviewStep(map) {
  if (!map) return null;
  if (completedReview.active) return String(completedReview.step);
  if (map.steps?.["2"] && !map.steps?.["3"]
      && (!state.data.mask?.approved || !state.data.legendReview?.approved)) return "2";
  // /test always asks the user to review Step 5. The shared backend can mark
  // an identity proposal ready automatically, but that must not bypass this
  // page's category naming and optional grouping review.
  if (map.steps?.["5"] && !state.data.aggregation?.review?.approved) return "5";
  if (map.steps?.["6"] && !map.steps?.["7"] && !map.step6_review_ready) return "6";
  if (map.steps?.["7"] && !map.steps?.["8"] && !map.step7_review_ready) return "7";
  if (map.steps?.["8"] && !map.steps?.["9"] && !map.step8_review_ready) return "8";
  if (map.steps?.["9"] && !map.step9_review_ready) return "9";
  return reopenedReviewStep(map);
}

function resetStagePresentation() {
  stagePresentation.stem = null;
  stagePresentation.step = 0;
  stagePresentation.holdUntil = 0;
  forcedAnalysisPresentation.active = false;
  forcedAnalysisPresentation.index = 0;
  forcedAnalysisPresentation.holdUntil = 0;
  clearStablePreview();
}

function beginAnalysisPresentation() {
  forcedAnalysisPresentation.active = true;
  forcedAnalysisPresentation.index = 0;
  forcedAnalysisPresentation.holdUntil = Date.now() + STAGE_DWELL_MS;
  stagePresentation.step = ANALYSIS_SEQUENCE[0];
  stagePresentation.holdUntil = forcedAnalysisPresentation.holdUntil;
}

function clearStablePreview() {
  document.body.classList.remove("test-preview-frozen");
  const stable = $("test-stable-preview");
  if (stable) {
    stable.hidden = true;
    stable.replaceChildren();
  }
  if (state.viewStep === TEST_PREVIEW_PIN) state.viewStep = null;
  previewPresentation.stem = null;
  previewPresentation.step = 0;
  previewPresentation.pendingUntil = 0;
  previewPresentation.locked = false;
}

function applyTestVisualPreferences() {
  state.layers = {
    ...state.layers,
    map: true,
    labels: false,
    lines: false,
    boundaries: false,
    segmentedLines: false,
  };
  Object.entries(STEP_VIEWS).forEach(([step, views]) => {
    views.forEach((view) => {
      // Step 1 is itself the source image. Later primary outputs can be
      // compared with that source without ever replacing the working view.
      view.originalCompare = Number(step) >= 2 && !view.intermediate;
    });
  });
}

function setToolbarLabel(label, text) {
  const textNode = Array.from(label.childNodes).filter((node) => node.nodeType === 3).at(-1);
  if (textNode) textNode.textContent = ` ${text}`;
}

function hybridMapEnabled() {
  return state.data.step7Review?.create_hybrid_map === true;
}

function activeHybridVisualTransition() {
  return hybridVisualTransition?.stem === state.selected ? hybridVisualTransition : null;
}

function applyTestHybridColourPreference() {
  const stem = state.selected;
  const transition = activeHybridVisualTransition();
  // The review setting is saved before its alternate image has necessarily
  // reached the browser. During that short window polling may observe the new
  // setting. Keep presenting the old, already-loaded view until the alternate
  // has been fetched and decoded, so the middle pane can never flash blank.
  const enabled = transition ? transition.previousEnabled : hybridMapEnabled();
  document.body.classList.toggle("test-hybrid-enabled", enabled);
  if (!stem) return false;

  const previousHybridState = hybridStateByProject.get(stem);
  const previousColourView = state.colourView;
  if (transition) {
    state.colourView = transition.previousColourView;
  } else if (!enabled) {
    state.colourView = false;
  } else if (previousHybridState !== true) {
    // A newly enabled hybrid map starts in colour. Once enabled, a user's
    // manual Display hybrid map choice remains untouched by subsequent polling.
    state.colourView = true;
  }
  hybridStateByProject.set(stem, enabled);
  document.body.classList.toggle(
    "test-hybrid-view-visible", enabled && state.colourView,
  );
  return previousColourView !== state.colourView;
}

function syncTestColourControl(toolbar) {
  const input = toolbar.querySelector("[data-colour-view]");
  if (!input) return;
  const transition = activeHybridVisualTransition();
  const enabled = transition ? transition.previousEnabled : hybridMapEnabled();
  const label = input.closest("label");
  input.disabled = !enabled;
  input.checked = enabled && state.colourView;
  label?.classList.toggle("is-disabled", !enabled);
  if (label) {
    label.title = enabled
      ? "Show the hybrid tactile map"
      : "Turn on Create hybrid map in Step 7 first";
    setToolbarLabel(label, "Display hybrid map");
  }
}

function testHybridPreviewNames(step, enabled) {
  if (step === 7) return [enabled ? "step8a_hybrid.png" : "step8a_cleanup.png"];
  if (step === 8) return [enabled ? "step8_hybrid_base.png" : "step8_braille_base.png"];
  return [];
}

function waitForTestImage(url) {
  return new Promise((resolve) => {
    const image = new Image();
    image.addEventListener("load", () => resolve(true), { once: true });
    image.addEventListener("error", () => resolve(false), { once: true });
    image.src = url;
  });
}

async function prepareTestHybridVisual(stem, step, enabled) {
  const names = testHybridPreviewNames(step, enabled);
  if (names.length) {
    const urls = await preloadPreviews(stem, names);
    if (state.selected !== stem || urls.some((url) => !url)) return false;
    const decoded = await Promise.all(urls.map(waitForTestImage));
    if (!decoded.every(Boolean)) return false;
  }
  if (step === 9) {
    const finalName = enabled ? "step8_hybrid.png" : "step8_braille.png";
    return waitForTestImage(artifactUrl(stem, finalName));
  }
  return true;
}

async function refreshTestHybridVisual(enabled, prepared = false) {
  const stem = state.selected;
  const step = activeStep(selectedMap());
  if (!stem) return false;
  if (!prepared && !await prepareTestHybridVisual(stem, step, enabled)) return false;

  if (step === 9) {
    const finalName = enabled ? "step8_hybrid.png" : "step8_braille.png";
    const url = artifactUrl(stem, finalName);
    const loaded = await waitForTestImage(url);
    if (!loaded || state.selected !== stem) return false;
    const finalMap = document.querySelector(".step9-map-sheet img");
    if (finalMap) finalMap.src = url;
  }
  const refreshed = await refreshStepImages();
  if (!refreshed && [7, 8, 9].includes(step)) {
    // Never rebuild the pane as part of a colour-only change. Rebuilding can
    // discard the valid tactile image; a failed alternate must leave it alone.
    return false;
  }
  document.body.classList.toggle("test-hybrid-view-visible", enabled);
  enhanceTestViewers();
  return refreshed || step === 9;
}

async function setTestHybridView(checkbox) {
  const enabled = hybridMapEnabled();
  if (!enabled) {
    checkbox.checked = false;
    return;
  }
  const desired = checkbox.checked;
  const previous = state.colourView;
  const transition = {
    stem: state.selected,
    previousEnabled: true,
    previousColourView: previous,
  };
  hybridVisualTransition = transition;
  checkbox.disabled = true;
  try {
    const prepared = await prepareTestHybridVisual(state.selected, activeStep(selectedMap()), desired);
    if (!prepared) throw new Error("The selected hybrid preview could not be loaded.");
    state.colourView = desired;
    hybridVisualTransition = null;
    const changed = await refreshTestHybridVisual(desired, true);
    if (!changed) throw new Error("The selected hybrid preview could not be loaded.");
  } catch (error) {
    state.colourView = previous;
    checkbox.checked = previous;
    document.body.classList.toggle("test-hybrid-view-visible", previous && enabled);
    toast(error.message, "error");
  } finally {
    if (hybridVisualTransition === transition) hybridVisualTransition = null;
    if (checkbox.isConnected) checkbox.disabled = false;
  }
}

async function toggleTestHybridMode(button) {
  if (!state.selected) return;
  const stem = state.selected;
  const enabled = button.getAttribute("aria-pressed") !== "true";
  const oldText = button.querySelector("strong")?.textContent || "Create hybrid map";
  const previousEnabled = hybridMapEnabled();
  const previousColourView = state.colourView;
  const transition = { stem, previousEnabled, previousColourView };
  let review = null;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  return runTestPatternTask(stem, async () => {
    hybridVisualTransition = transition;
    try {
      review = await saveStep7Review(stem, { create_hybrid_map: enabled });
      if (enabled) {
        // Saving the mode alone does not create a colour artifact. The existing
        // colour endpoint composites the unchanged tactile raster without a run.
        const colours = await saveCategoryColors(stem, testPatternColours());
        if (state.selected !== stem) return;
        state.data.colors = colours;
      }
      clearPreviewCache(stem);
      // Keep the current tactile image mounted while its alternate is fetched
      // and decoded. Only then expose the new mode to the polling renderer.
      const prepared = await prepareTestHybridVisual(stem, activeStep(selectedMap()), enabled);
      if (!prepared) throw new Error("The hybrid preview could not be loaded.");
      if (state.selected !== stem) return;
      state.data.step7Review = review;
      state.colourView = enabled;
      hybridStateByProject.set(stem, enabled);
      hybridVisualTransition = null;
      document.body.classList.toggle("test-hybrid-enabled", enabled);
      button.setAttribute("aria-pressed", String(enabled));
      const changed = await refreshTestHybridVisual(enabled, true);
      if (!changed) throw new Error("The hybrid preview could not be displayed.");
      document.querySelectorAll("[data-pattern-colour]").forEach((picker) => {
        picker.disabled = !enabled;
      });
    } catch (error) {
      if (review && state.selected === stem) {
        try {
          state.data.step7Review = await saveStep7Review(
            stem, { create_hybrid_map: previousEnabled },
          );
        } catch { /* Keep the original preview-loading error. */ }
      }
      state.colourView = previousColourView;
      hybridStateByProject.set(stem, previousEnabled);
      document.body.classList.toggle("test-hybrid-enabled", previousEnabled);
      document.body.classList.toggle(
        "test-hybrid-view-visible", previousEnabled && previousColourView,
      );
      button.setAttribute("aria-pressed", String(previousEnabled));
      toast(error.message, "error");
      const label = button.querySelector("strong");
      if (label) label.textContent = oldText;
    } finally {
      if (hybridVisualTransition === transition) hybridVisualTransition = null;
      if (button.isConnected) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    }
  });
}

function bindTestViewerFit(toolbar) {
  const frame = toolbar.closest(".map-stage")?.querySelector(".map-frame");
  const readout = toolbar.querySelector(".page-zoom-readout");
  if (!frame || !readout) return;
  // Cached images can be fitted while the home screen still hides the
  // workspace. Refit as its visible size settles, unless the user zooms.
  const refit = () => {
    if (!toolbar.isConnected) {
      observer.disconnect();
      return;
    }
    if (frame.clientWidth > 0 && frame.clientHeight > 0
        && readout.textContent === "Fit") {
      toolbar.querySelector(".page-zoom-fit")?.click();
    }
  };
  const observer = new ResizeObserver(refit);
  observer.observe(frame, { box: "border-box" });
  refit();
}

function addTestLegendOriginalComparison() {
  const map = selectedMap();
  if (!map || activeStep(map) !== 9 || !state.showOriginalMap) return;
  // The shared renderer already handles original + tactile map + legend.
  // Extend its legend-only view here so /test can also show original + legend.
  const canvas = document.querySelector("#visual-content .step9-page-canvas");
  if (!canvas) return;
  const [legendW, legendH] = (state.data.legend?.page?.canvas_px || []).map(Number);
  const [mapW, mapH] = state.data.braille?.page?.canvas_px || [legendW, legendH];
  if (![legendW, legendH, Number(mapW), Number(mapH)].every((size) => Number.isFinite(size) && size > 0)) return;
  const height = Math.max(mapH, legendH);
  const originalWidth = mapW * height / mapH;
  const legendWidth = legendW * height / legendH;
  const gap = Math.max(30, Math.round(height * .04));
  const grid = canvas.querySelector(":scope > .page-grid");
  const legend = document.createElement("div");
  legend.className = "step9-legend-sheet";
  legend.style.aspectRatio = `${legendW} / ${legendH}`;
  [...canvas.children].filter((node) => node !== grid).forEach((node) => legend.appendChild(node));
  const original = document.createElement("div");
  original.className = "step9-input-sheet";
  original.style.aspectRatio = `${mapW} / ${mapH}`;
  original.innerHTML = `<img src="${mapUrl(map.name)}" alt="Original map for ${esc(map.name)}">`;
  canvas.prepend(original, legend);
  canvas.classList.remove("step9-page-canvas");
  canvas.classList.add("step7-comparison-canvas", "step9-comparison-canvas");
  canvas.dataset.naturalWidth = String(originalWidth + legendWidth + gap);
  canvas.dataset.naturalHeight = String(height);
  canvas.style.aspectRatio = `${canvas.dataset.naturalWidth} / ${height}`;
  canvas.style.setProperty("--compare-gap", `${gap}px`);
  canvas.style.gridTemplateColumns = `${originalWidth}fr ${legendWidth}fr`;
  canvas.closest(".map-stage")?.querySelector(".page-zoom-fit")?.click();
}

function enhanceTestViewers() {
  addTestLegendOriginalComparison();
  let maskComparison = null;
  let legendComparison = null;
  document.querySelectorAll("#visual-content .page-view-toolbar").forEach((toolbar) => {
    const originalEnabled = activeStep(selectedMap()) >= 2;
    const originalInput = toolbar.querySelector("[data-test-original-view]");
    if (originalInput) {
      originalInput.disabled = !originalEnabled;
      originalInput.checked = originalEnabled && state.showOriginalMap;
      originalInput.closest("label")?.classList.toggle("is-disabled", !originalEnabled);
    }
    if (toolbar.dataset.testEnhanced === "true") {
      syncTestColourControl(toolbar);
      return;
    }
    toolbar.dataset.testEnhanced = "true";

    const pan = toolbar.querySelector(".page-pan-toggle");
    if (pan) {
      pan.title = "Pan";
      pan.setAttribute("aria-label", "Pan");
      pan.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24"
        fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
        stroke-linejoin="round" aria-hidden="true" style="vertical-align: middle">
        <path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11m0-5.5V3.5a1.5 1.5 0 0 1 3 0V11
          m0-5.5a1.5 1.5 0 0 1 3 0V12m0-3.5a1.5 1.5 0 0 1 3 0V15
          a7 7 0 0 1-7 7h-1a7 7 0 0 1-5.6-2.8L3 14.7a1.5 1.5 0 0 1 2.4-1.8L8 16V13"/>
      </svg>`;
    }

    toolbar.querySelector("[data-original-view]")?.remove();
    toolbar.querySelector("[data-final-map-view]")?.remove();

    const guides = toolbar.querySelector(".page-guides-toggle")?.closest("label");
    const snap = toolbar.querySelector(".page-snap-toggle")?.closest("label");
    if (guides) setToolbarLabel(guides, "Gridlines");
    if (snap) setToolbarLabel(snap, "Snap to grid");

    let colours = toolbar.querySelector("[data-colour-view]")?.closest("label");
    if (!colours) {
      colours = document.createElement("label");
      colours.className = "page-grid-toggle is-disabled";
      colours.title = "Colors become available with the tactile texture preview.";
      colours.innerHTML = '<input type="checkbox" disabled><span aria-hidden="true"></span> Display hybrid map';
    } else {
      setToolbarLabel(colours, "Display hybrid map");
    }
    syncTestColourControl(toolbar);

    const original = document.createElement("label");
    original.className = `page-grid-toggle${originalEnabled ? "" : " is-disabled"}`;
    original.innerHTML = `<input type="checkbox" data-test-original-view
      ${originalEnabled && state.showOriginalMap ? "checked" : ""}
      ${originalEnabled ? "" : "disabled"}><span aria-hidden="true"></span> Display original map`;
    original.querySelector("input").addEventListener("change", (event) => {
      state.showOriginalMap = event.currentTarget.checked;
      // The extracted map/legend comparison is already a two-result view.
      // Keep its standard toolbar stable without replacing that composite.
      if (toolbar.closest(".test-extracted-stage")) return;
      renderTestVisual();
    });

    const fullSize = toolbar.querySelector(".page-full-size");
    if (fullSize) fullSize.textContent = "Open full size ↗";
    // bindViewer requires this node during image-load and fit callbacks. Keep
    // it in the toolbar DOM and hide it visually instead of removing it while
    // a stage transition may still be finishing.
    const readout = toolbar.querySelector(".page-zoom-readout");
    if (readout) {
      readout.hidden = true;
      readout.setAttribute("aria-hidden", "true");
    }
    const navigationGroup = document.createElement("div");
    navigationGroup.className = "test-toolbar-group test-toolbar-navigation";
    [
      toolbar.querySelector(".page-pan-toggle"),
      toolbar.querySelector(".page-zoom-out"),
      toolbar.querySelector(".page-zoom-range"),
      toolbar.querySelector(".page-zoom-in"),
      toolbar.querySelector(".page-zoom-100"),
      toolbar.querySelector(".page-zoom-fit"),
      fullSize,
      readout,
    ].filter(Boolean).forEach((control) => navigationGroup.appendChild(control));

    const displayGroup = document.createElement("div");
    displayGroup.className = "test-toolbar-group test-toolbar-display";
    [guides, snap, colours, original]
      .filter(Boolean).forEach((control) => displayGroup.appendChild(control));
    toolbar.replaceChildren(navigationGroup, displayGroup);
    bindTestViewerFit(toolbar);
  });

  document.querySelectorAll("#visual-content .step7-comparison-canvas").forEach((comparison) => {
    comparison.classList.add("test-output-comparison");
    const originalSheet = comparison.querySelector(".step7-original-sheet");
    const outputSheet = comparison.querySelector(".step7-output-sheet");
    if (originalSheet) originalSheet.setAttribute("aria-label", "Original map");
    if (outputSheet) outputSheet.setAttribute("aria-label", "Current output");
    if (activeStep(selectedMap()) === 2 && outputSheet) {
      // The shared comparison renderer is also used by later stages. In Step 2
      // its output sheet must become the mask target so the editable mask is
      // drawn over the second image rather than showing two source images.
      const needsMaskBinding = outputSheet.id !== "mask-target"
        || !outputSheet.querySelector(".mask-canvas");
      outputSheet.id = "mask-target";
      outputSheet.dataset.testCurrentOutput = "detected-mask";
      if (needsMaskBinding) maskComparison = outputSheet;
      if (state.data.mask?.approved
          && !outputSheet.querySelector(".legend-review-box")) {
        legendComparison = outputSheet;
      }
    }
  });
  if (maskComparison) bindMaskCanvas();
  if (legendComparison) bindLegendReviewOverlay();
  // Also replace the shared editor in the ordinary (non-comparison) Step 2
  // view. Rebinding reuses the /test surface rather than fetching its mask.
  bindMaskCanvas();
  enhanceTestBrailleOverlay();
  completedReview.showSavedImages();
}

function scheduleTestViewerStabilization() {
  if (visualPanelStabilizeQueued) return;
  visualPanelStabilizeQueued = true;
  queueMicrotask(() => {
    visualPanelStabilizeQueued = false;
    if (!document.body.classList.contains("test-pipeline-open")) return;
    // Shared polling and ordered snapshot recovery can redraw the visual pane
    // directly. Normalize that fresh toolbar before the browser paints its
    // native control order, instead of waiting for the 350 ms shell poll.
    enhanceTestViewers();
  });
}

function bindTestViewerStabilizer() {
  const visual = $("visual-content");
  if (!visual || visualPanelObserver) return;
  visualPanelObserver = new MutationObserver(scheduleTestViewerStabilization);
  visualPanelObserver.observe(visual, { childList: true });
}

function aggregationReviewIntro() {
  const proposal = state.data.aggregation?.proposal;
  const count = proposal?.source_classes?.length || proposal?.groups?.length || 0;
  const maximum = Number(proposal?.slots) || 5;
  if (count <= maximum) {
    return "This map already has five tactile categories, the maximum allowed. You may still regroup the map by dragging any colored class and dropping it under a different category; you can also rename the categories before continuing.";
  }
  return "This map has more than five tactile categories, so some classes need to be grouped together. Drag any colored class and drop it under another category until there are no more than five categories; you can also rename the categories before continuing.";
}

function emphasizeAggregationInstructions(intro) {
  const phrase = intro.textContent.includes("more than five")
    ? "Drag any colored class and drop it under another category"
    : "dragging any colored class and dropping it under a different category";
  const text = intro.textContent;
  const offset = text.indexOf(phrase);
  if (offset < 0) return;
  const strong = document.createElement("strong");
  strong.textContent = phrase;
  intro.replaceChildren(text.slice(0, offset), strong, text.slice(offset + phrase.length));
}

function testReviewPanelDefinition(reviewStep, exportReady, failed, blocked) {
  if (exportReady) return TEST_REVIEW_PANELS.export;
  if (completedReview.active) {
    if (reviewStep === "1") return TEST_REVIEW_PANELS.reading;
    if (reviewStep === "3") return TEST_REVIEW_PANELS.text;
    if (reviewStep === "4") return TEST_REVIEW_PANELS.segmentation;
  }
  if (reviewStep === "2") {
    if (completedReview.active && completedReview.maskPhase === "legend"
        && ["legacy", "derived"].includes(state.data.legendReview?.status)) return TEST_REVIEW_PANELS.savedLegend;
    return (completedReview.active ? completedReview.maskPhase === "legend" : state.data.mask?.approved)
      ? TEST_REVIEW_PANELS.legend : TEST_REVIEW_PANELS.mask;
  }
  if (reviewStep === "5") {
    TEST_REVIEW_PANELS.aggregation.intro = aggregationReviewIntro();
    return TEST_REVIEW_PANELS.aggregation;
  }
  if (reviewStep === "6") return TEST_REVIEW_PANELS.simplification;
  if (reviewStep === "7") return TEST_REVIEW_PANELS.patterns;
  if (reviewStep === "8") return TEST_REVIEW_PANELS.braille;
  if (reviewStep === "9") return TEST_REVIEW_PANELS.finalLegend;
  if (failed) return { selector: ".failure-card", title: "Pipeline issue", action: ".action-row" };
  if (blocked) return { selector: ".scope-block", title: "Project review", action: ".action-row" };
  return null;
}

function testReviewPanelKey(definition) {
  return Object.entries(TEST_REVIEW_PANELS)
    .find(([, panel]) => panel === definition)?.[0]
    || definition?.selector || "review";
}

function testReviewScrollKey(panel) {
  return `${state.selected || ""}:${panel || "review"}`;
}

function rememberTestReviewScroll(event) {
  const body = event.target.closest?.(".test-review-shell-body");
  const root = body?.closest(".test-review-shell");
  if (!body || !root?.dataset.testReviewPanel) return;
  reviewScrollByProject.set(
    testReviewScrollKey(root.dataset.testReviewPanel), body.scrollTop,
  );
}

function restoreTestReviewScroll(root) {
  const body = root.querySelector(":scope > .test-review-shell-body");
  const saved = reviewScrollByProject.get(
    testReviewScrollKey(root.dataset.testReviewPanel),
  );
  if (!body || !Number.isFinite(saved)) return;
  body.scrollTop = saved;
  queueMicrotask(() => {
    if (body.isConnected) body.scrollTop = saved;
  });
}

function updateTestMaskActions(root) {
  const status = root.querySelector("#mask-save-status");
  if (status) status.textContent = "";
  const reset = root.querySelector("#mask-discard");
  const canReset = Boolean(state.maskBrush.strokes.length || state.data.mask?.reviewed);
  if (reset) {
    reset.textContent = "Reset to detected result";
    reset.disabled = state.busy || !canReset;
    reset.title = canReset
      ? "Restore the mask originally detected by the pipeline"
      : "The detected mask is already shown";
  }
  const finish = root.querySelector("#test-mask-continue");
  const undo = root.querySelector("#mask-undo");
  if (undo) undo.disabled = state.busy || !state.maskBrush.strokes.length;
  if (finish) {
    finish.textContent = "Approve mask and continue";
    finish.disabled = Boolean(state.busy);
  }
}

function clearTestMaskToolSelection() {
  testMaskReviewStem = null;
  testMaskBrushMode = null;
  document.body.classList.remove("test-mask-tool-unselected");
}

function syncTestMaskToolSelection(root) {
  const stem = state.selected || null;
  if (testMaskReviewStem !== stem) {
    testMaskReviewStem = stem;
    testMaskBrushMode = null;
    // The shared editor starts with erase selected. The /test review requires
    // an explicit tool choice before a stroke can be painted.
    state.maskBrush.mode = "";
  }
  root.querySelectorAll('input[name="mask-mode"]').forEach((radio) => {
    radio.checked = radio.value === testMaskBrushMode;
  });
  document.body.classList.toggle("test-mask-tool-unselected", !testMaskBrushMode);
}

function selectTestMaskTool(event) {
  const option = event.target.closest(".mask-decision-modes label");
  const radio = option?.querySelector('input[name="mask-mode"]');
  if (!radio) return false;
  testMaskReviewStem = state.selected || null;
  testMaskBrushMode = radio.value;
  state.maskBrush.mode = radio.value;
  radio.checked = true;
  document.body.classList.remove("test-mask-tool-unselected");
  return true;
}

function refreshTestMaskSurface() {
  bindMaskCanvas();
  const root = $("control-content")?.querySelector(".mask-decision");
  if (root) updateTestMaskActions(root);
}

function undoTestMaskStroke(event) {
  const undo = event.target.closest("#mask-undo");
  if (!undo) return false;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (undo.disabled || !state.maskBrush.strokes.length) return true;
  undoMaskStroke();
  refreshTestMaskSurface();
  return true;
}

function waitForTestCondition(predicate, timeout = 20000,
  failureMessage = "The mask changes could not be saved. Please try again.") {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const check = () => {
      if (predicate()) {
        resolve();
      } else if (Date.now() - started >= timeout) {
        reject(new Error(failureMessage));
      } else {
        window.setTimeout(check, 80);
      }
    };
    check();
  });
}

async function finishTestMaskReview(button) {
  if (button.disabled || state.busy || !state.selected) return;
  if (completedReview.active) return finishCompletedStep2Review(button);
  const stem = state.selected;
  const originalLabel = button.textContent;
  state.busy = true;
  button.disabled = true;
  button.textContent = "Approving...";
  try {
    if (state.maskBrush.strokes.length) {
      await saveMaskStrokes(stem, state.maskBrush.strokes);
      if (state.selected !== stem) return;
      commitMaskStrokes();
      state.maskBrush.strokes = [];
      state.data.mask = { ...state.data.mask, reviewed: true, approved: false };
    }
    await approveMask(stem);
    if (state.selected !== stem) return;
    state.maskBrush.active = false;
    if (!state.individualRun) {
      state.autorun = true;
      await continuePipeline();
    } else {
      state.autorun = false;
      state.viewStep = null;
      await loadMaps();
      await refreshSelectedData();
      if (state.selected === stem) renderWorkspace(true);
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy = false;
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }
}

function buildTestMaskFooter(root, body, footer) {
  const actions = body.querySelector(".mask-decision-actions");
  const undo = body.querySelector("#mask-undo");
  const reset = body.querySelector("#mask-discard");
  const save = body.querySelector("#mask-save");
  const approve = body.querySelector("#approve-mask");
  const secondary = document.createElement("div");
  secondary.className = "test-review-secondary-actions";
  if (undo) {
    undo.textContent = "Undo stroke";
    secondary.appendChild(undo);
  }
  if (reset) secondary.appendChild(reset);

  const nativeActions = document.createElement("div");
  nativeActions.className = "test-review-native-actions";
  nativeActions.hidden = true;
  [save, approve].filter(Boolean).forEach((node) => node.remove());
  if (actions) actions.remove();
  root.appendChild(nativeActions);

  const finish = document.createElement("button");
  finish.className = "button primary test-review-primary";
  finish.id = "test-mask-continue";
  finish.type = "button";
  finish.addEventListener("click", () => void finishTestMaskReview(finish));
  body.appendChild(secondary);
  footer.appendChild(finish);
}

async function finishTestLegendReview(button) {
  if (button.disabled || state.busy) return;
  if (completedReview.active) return finishCompletedStep2Review(button);
  const apply = $("apply-legend-box");
  if (!apply) return;
  const stem = state.selected;
  button.disabled = true;
  try {
    const review = state.data.legendReview;
    const box = state.legendBox || review?.box;
    const approve = $("approve-legend-box");
    const sameBox = (left, right) => Array.isArray(left) && left.length === 4
      && Array.isArray(right) && right.length === 4
      && left.every((coordinate, index) => coordinate === right[index]);
    let alreadyValidated = review?.status === "ready" && approve && !approve.disabled
      && sameBox(box, review.box);
    if (alreadyValidated) {
      // Reuse only when this exact legend exclusion was already used to build
      // the text input. A recovered legend can still need its first Apply.
      const geometry = await artifactJson(stem, "geometry.json");
      if (state.selected !== stem) return;
      const legends = Array.isArray(geometry?.furniture)
        ? geometry.furniture.filter((item) => item?.name === "legend") : [];
      alreadyValidated = sameBox(geometry?.legend_crop, review.box)
        && legends.length === 1 && sameBox(legends[0].box, review.box)
        && state.data.legendReview?.status === "ready"
        && sameBox(state.legendBox || state.data.legendReview?.box, review.box);
    }
    if (!alreadyValidated) {
      apply.click();
      await waitForTestCondition(() => {
        const approve = $("approve-legend-box");
        return Boolean(state.data.legendReview?.status === "ready"
          && approve && !approve.disabled && !state.busy);
      }, 20000, "The legend box could not be applied. Adjust it and try again.");
    }
    if (state.selected !== stem) return;
    await withBusy(button, "Approving...", async () => {
      beginAnalysisPresentation();
      freezeCurrentPreview();
      await approveLegendReview(stem);
      if (state.selected !== stem) return;
      state.legendBox = null;
      if (!state.individualRun) {
        // Continuation refreshes the map list and reviews before starting Step 3.
        // Avoid the shared approval handler's additional pre-continuation refresh.
        state.autorun = true;
        await continuePipeline();
      } else {
        state.autorun = false;
        state.viewStep = null;
        await loadMaps();
        await refreshSelectedData();
        if (state.selected === stem) renderWorkspace(true);
      }
    });
  } catch (error) {
    toast(error.message, "error");
    if (button.isConnected) button.disabled = false;
  }
}

function buildTestLegendFooter(root, footer) {
  const actions = footer.querySelector(".legend-review-actions");
  const apply = actions?.querySelector("#apply-legend-box");
  const approve = actions?.querySelector("#approve-legend-box");
  if (!apply || !approve) {
    if (completedReview.active && ["legacy", "derived"].includes(state.data.legendReview?.status)) {
      const finish = document.createElement("button");
      finish.id = "test-legend-continue";
      finish.type = "button";
      finish.className = "button primary test-review-primary";
      finish.textContent = "Approve legend & continue";
      finish.addEventListener("click", () => void finishCompletedStep2Review(finish));
      footer.appendChild(finish);
    }
    return;
  }

  const nativeActions = document.createElement("div");
  nativeActions.className = "test-review-native-actions";
  nativeActions.hidden = true;
  nativeActions.append(apply, approve);
  actions.remove();
  root.appendChild(nativeActions);

  const finish = document.createElement("button");
  finish.className = "button primary test-review-primary";
  finish.id = "test-legend-continue";
  finish.type = "button";
  finish.textContent = "Apply legend box and continue";
  finish.addEventListener("click", () => void finishTestLegendReview(finish));
  footer.appendChild(finish);
}

async function finishCompletedStep2Review(button) {
  if (button.disabled || state.busy) return;
  const stem = state.selected;
  await withBusy(button, "Approving…", async () => {
    if (state.maskBrush.strokes.length) {
      completedReview.markDirty();
      await saveMaskStrokes(stem, state.maskBrush.strokes);
      commitMaskStrokes();
      state.maskBrush.strokes = [];
      if (state.data.mask) state.data.mask.approved = false;
    }
    if (!state.data.mask?.approved) {
      await approveMask(stem);
      if (state.data.mask) state.data.mask.approved = true;
    }
    const review = state.data.legendReview;
    if (state.legendBox) {
      completedReview.markDirty();
      state.data.legendReview = await saveLegendBox(stem, state.legendBox);
      state.legendBox = null;
    }
    const current = state.data.legendReview || review;
    if (!current || !["ready", "derived", "legacy"].includes(current.status)) {
      completedReview.setMaskPhase("legend");
      throw new Error("Review the legend box before continuing.");
    }
    if (current.status !== "legacy" && !current.approved) {
      await approveLegendReview(stem);
      current.approved = true;
    }
    if (state.selected === stem) await completedReview.advance();
  });
}

function buildTestLegendCompare(root) {
  const native = root.querySelector("#legend-compare");
  if (!native || root.querySelector(".test-legend-compare")) return;
  native.setAttribute("aria-pressed", String(state.showFinalMap));
  const label = document.createElement("label");
  label.className = "tiny-check test-legend-compare";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.showFinalMap;
  checkbox.addEventListener("change", () => {
    state.showFinalMap = checkbox.checked;
    native.setAttribute("aria-pressed", String(state.showFinalMap));
    renderTestVisual();
  });
  label.append(checkbox, document.createTextNode(" Display tactile map next to legend"));
  native.hidden = true;
  native.before(label);
}

function syncTestLegendCompare(root) {
  const checkbox = root.querySelector(".test-legend-compare input");
  if (checkbox) checkbox.checked = state.showFinalMap;
}

function disableTestExportPanelPreview(root) {
  root.removeAttribute("tabindex");
  root.removeAttribute("aria-label");
  const stopPanelPreview = (event) => {
    if (event.target.closest("a, button, input, label")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  root.addEventListener("click", stopPanelPreview, true);
  root.addEventListener("keydown", stopPanelPreview, true);
}

function removeTestPatternDescriptions(root) {
  root.querySelectorAll(".pattern-row-copy small, .pattern-dialog header small, .pattern-library-choice span")
    .forEach((node) => node.remove());
}

function relocateTestPatternDialog() {
  const nestedDialog = document.querySelector("#control-content .pattern-dialog");
  const nestedBackdrop = document.querySelector("#control-content .pattern-dialog-backdrop");
  if (!nestedDialog) return;
  document.querySelectorAll("body > .pattern-dialog, body > .pattern-dialog-backdrop")
    .forEach((node) => node.remove());
  removeTestPatternDescriptions(nestedDialog);
  if (nestedBackdrop) document.body.appendChild(nestedBackdrop);
  document.body.appendChild(nestedDialog);
}

function testPatternUpdating(stem = state.selected) {
  return Boolean(testPatternPendingByProject.get(stem)
    || pendingTestPatternTransform?.stem === stem || pendingTestPatternColours?.stem === stem
    || stem === state.selected && testPatternChoicePending);
}

function runTestPatternTask(stem, action, label = "Rendering pattern changes…") {
  if (completedReview.active && state.selected === stem) completedReview.markDirty();
  const hadStep7 = state.selected === stem && selectedMap()?.steps?.["7"];
  if (state.selected === stem) state.generation += 1; // Discard any older review-data fetch.
  testPatternPendingByProject.set(stem, (testPatternPendingByProject.get(stem) || 0) + 1);
  testPatternStatusByProject.set(stem, label);
  if (state.selected === stem) syncTestPatternControls();
  // A pattern save rewrites symbols before cleanup. During that interval the
  // server can report Step 7 as incomplete; that must never launch another run.
  if (state.selected === stem) state.autorun = false;
  const task = testPatternTaskQueue.then(async () => {
    if (state.selected !== stem) return;
    state.busy = true;
    try {
      await action();
      if (state.selected === stem) {
        const map = selectedMap();
        if (map) {
          map.step7_review_ready = false;
          if (hadStep7) map.steps["7"] = true;
        }
      }
    } catch (error) {
      testPatternErrorsByProject.set(stem, error.message);
      if (state.selected === stem) toast(error.message, "error");
    } finally {
      state.busy = false;
    }
  }).finally(() => {
    const remaining = (testPatternPendingByProject.get(stem) || 1) - 1;
    if (remaining) testPatternPendingByProject.set(stem, remaining);
    else testPatternPendingByProject.delete(stem);
    if (!remaining) testPatternStatusByProject.delete(stem);
    if (state.selected === stem) syncTestPatternControls();
  });
  testPatternTaskQueue = task.catch(() => {});
  return task;
}

function syncTestPatternControls() {
  const suggested = testSuggestedIntentByProject.get(state.selected)?.enabled
    ?? state.data.step7Review?.preserve_haptic_distances ?? true;
  const hybrid = hybridMapEnabled();
  const busy = testPatternUpdating();
  const mode = $("preserve-haptic-distances");
  mode?.setAttribute("aria-pressed", String(suggested));
  if (mode) mode.disabled = false; // A newer toggle intent is queued, never discarded.
  $("create-hybrid-map")?.setAttribute("aria-pressed", String(hybrid));
  document.querySelectorAll("[data-edit-pattern]").forEach((button) => {
    const group = state.data.patterns?.groups?.find((item) => Number(item.group_id) === Number(button.dataset.editPattern));
    button.disabled = suggested || busy || !group?.editable;
  });
  document.querySelectorAll("[data-change-pattern]").forEach((button) => {
    button.disabled = busy;
    button.title = suggested ? "Choose one pattern; the remaining categories will be re-optimized around it" : "Choose a pattern for this category";
  });
  document.querySelectorAll("[data-pattern-colour]").forEach((picker) => {
    picker.disabled = !hybrid;
  });
  const approval = $("approve-patterns");
  if (approval) {
    const failed = testPatternErrorsByProject.has(state.selected);
    approval.disabled = busy || failed;
    approval.textContent = busy ? "Updating preview…" : "Approve textures & continue";
    const root = $("pattern-decision");
    if (root) {
      root.setAttribute("aria-busy", String(busy));
      let status = root.querySelector("#test-pattern-render-status");
      if (!status) {
        status = document.createElement("p");
        status.id = "test-pattern-render-status";
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        (root.querySelector(".test-review-shell-footer") || approval.parentElement).prepend(status);
      }
      status.hidden = !busy && !failed;
      status.classList.toggle("is-error", failed && !busy);
      status.textContent = busy ? testPatternStatusByProject.get(state.selected) || "Preparing preview…"
        : `Preview could not be updated: ${testPatternErrorsByProject.get(state.selected)} Retry the change before approving.`;
    }
  }
  document.querySelectorAll('input[name="pattern-transform"]').forEach((slider) => {
    slider.disabled = suggested;
  });
  document.querySelectorAll(".page-view-toolbar").forEach(syncTestColourControl);
}

function syncTestPatternChoices() {
  const data = state.data.patterns || {};
  const group = data.groups?.find((item) => Number(item.group_id) === Number(state.patternDialog?.groupId));
  document.querySelectorAll("[data-choose-pattern]").forEach((button) => {
    const reason = patternChoiceBlock(group, button.dataset.choosePattern, data.groups || [],
      data.library || [], state.data.step7Review?.preserve_haptic_distances !== false);
    button.disabled = Boolean(reason);
    if (reason) button.title = reason;
  });
}

function confirmTestPatternChoice(message) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "test-pattern-warning";
    dialog.setAttribute("aria-label", message);
    dialog.innerHTML = `<form method="dialog"><p></p><div class="test-pattern-warning-actions">
      <button class="button secondary" value="cancel" autofocus>Cancel</button>
      <button class="button primary" value="yes">Yes</button></div></form>`;
    dialog.querySelector("p").textContent = message;
    let finished = false;
    const finish = (accepted) => {
      if (finished) return;
      finished = true;
      dialog.remove();
      resolve(accepted);
    };
    dialog.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      finish(event.submitter?.value === "yes");
    });
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    dialog.addEventListener("close", () => finish(dialog.returnValue === "yes"), { once: true });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function rememberSuggestedPatternDefaults() {
  const stem = state.selected;
  const groups = state.data.patterns?.groups || [];
  if (!stem || !groups.length || suggestedPatternDefaultsByProject.has(stem)) return;
  const key = `mapgen:test:initial-patterns:${stem}`;
  const signature = JSON.stringify([state.previewLevel,
    groups.map((group) => [group.group_id, group.label])]);
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || "null");
    if (saved?.signature === signature) {
      suggestedPatternDefaultsByProject.set(stem, saved.groups);
      return;
    }
  } catch { /* Session storage can be unavailable. Keep the in-memory baseline. */ }
  const defaults = groups.map((group) => ({
    group_id: Number(group.group_id),
    pattern: group.pattern,
    transform: JSON.parse(JSON.stringify(group.transform || {})),
  }));
  suggestedPatternDefaultsByProject.set(stem, defaults);
  try { sessionStorage.setItem(key, JSON.stringify({ signature, groups: defaults })); } catch { /* optional cache */ }
}

function enableTestSuggestedPatternChanges(root) {
  rememberSuggestedPatternDefaults();
  syncTestPatternControls();
}

function samePatternTransform(left, right) {
  const defaults = state.data.patterns?.defaults || {};
  return [...new Set([...Object.keys(left || {}), ...Object.keys(right || {})])]
    .every((key) => Number(left?.[key] ?? defaults[key]) === Number(right?.[key] ?? defaults[key]));
}

async function saveTestPatternEdit(stem, body) {
  const response = await fetch(`/api/test/step7-edit/${encodeURIComponent(stem)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), cache: "no-store",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "The pattern preview could not be rendered.");
  return payload;
}

async function refreshTestPatternArtifacts(stem, groups = []) {
  testPatternStatusByProject.set(stem, "Loading updated preview…");
  syncTestPatternControls();
  clearPreviewCache(stem);
  const names = testHybridPreviewNames(7, state.colourView && hybridMapEnabled());
  const [urls] = await Promise.all([preloadPreviews(stem, names), ...groups.map(updateTestPatternPreview)]);
  if (state.selected !== stem) return;
  if (urls.some((url) => !url) || !(await Promise.all(urls.map(waitForTestImage))).every(Boolean)) {
    throw new Error("The updated map image could not be loaded.");
  }
  if (!await refreshStepImages()) throw new Error("The updated map preview is not available.");
  const image = $("step-image-0");
  if (image?.decode) await image.decode();
  testPatternErrorsByProject.delete(stem);
  syncTestPatternControls();
}

async function toggleTestSuggestedPatterns(button) {
  if (!state.selected) return;
  if (pendingTestPatternTransform) void flushTestPatternTransform();
  if (pendingTestPatternColours) void flushTestPatternColours();
  const stem = state.selected;
  const enable = !(testSuggestedIntentByProject.get(stem)?.enabled
    ?? state.data.step7Review?.preserve_haptic_distances ?? true);
  rememberSuggestedPatternDefaults();
  const intent = { enabled: enable };
  testSuggestedIntentByProject.set(stem, intent);
  if (enable) {
    state.patternDialog = null;
    document.querySelectorAll(".pattern-dialog, .pattern-dialog-backdrop").forEach((node) => node.remove());
  }
  syncTestPatternControls();
  return runTestPatternTask(stem, async () => {
    try {
      if (!enable) {
        const review = await saveStep7Review(stem, { preserve_haptic_distances: false });
        if (state.selected === stem) state.data.step7Review = review;
        return;
      }
      const defaults = suggestedPatternDefaultsByProject.get(stem) || [];
      const result = await saveTestPatternEdit(stem, { action: "reset", groups: defaults });
      if (state.selected !== stem) return;
      state.data.patterns = result.pattern_data;
      state.data.step7Review = result.review;
      await refreshTestPatternArtifacts(stem, defaults.map((group) => group.group_id));
    } finally {
      if (testSuggestedIntentByProject.get(stem) === intent) testSuggestedIntentByProject.delete(stem);
    }
  }, enable ? "Restoring suggested patterns…" : "Saving pattern settings…");
}

function openTestPatternChange(button) {
  if (button.disabled || testPatternUpdating() || state.busy) return;
  const data = state.data.patterns;
  const groupId = Number(button.dataset.changePattern);
  const group = data?.groups?.find((item) => Number(item.group_id) === groupId);
  if (!group) return;
  // The shared dialog forbids opening while suggestions are enabled. Build
  // the /test picker directly so event timing never changes the saved mode.
  state.patternGroup = groupId;
  state.patternDialog = { kind: "change", groupId };
  document.querySelectorAll(".pattern-dialog, .pattern-dialog-backdrop").forEach((node) => node.remove());
  const mounted = document.querySelector(`.pattern-decision-list [data-pattern-preview="${groupId}"]`);
  const preview = mounted?.currentSrc || mounted?.src || patternPreviewUrl(state.selected, groupId);
  const used = new Set(data.groups.map((item) => item.pattern));
  const backdrop = document.createElement("div");
  backdrop.className = "pattern-dialog-backdrop";
  backdrop.dataset.closePatternDialog = "";
  backdrop.dataset.testPatternChange = "true";
  const dialog = document.createElement("section");
  dialog.className = "pattern-dialog";
  dialog.dataset.testPatternChange = "true";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "test-pattern-change-title");
  dialog.innerHTML = `
    <header><h3 id="test-pattern-change-title">Change pattern — ${esc(group.label)}</h3>
      <button class="pattern-dialog-close" type="button" data-close-pattern-dialog
        aria-label="Close pattern library">&times;</button></header>
    <div class="pattern-dialog-preview">
      <img src="${esc(preview)}" data-pattern-preview="${groupId}" alt="Current pattern">
    </div>
    <div class="pattern-library-grid" role="group" aria-label="Tactile pattern library">
      ${(data.library || []).map((item) => `
        <button class="pattern-library-choice${item.pattern === group.pattern ? " is-selected" : ""}${used.has(item.pattern) ? " is-used" : ""}"
            type="button" data-choose-pattern="${esc(item.pattern)}" title="${esc(item.pattern_desc)}"
            aria-label="${esc(item.pattern_desc)}" aria-pressed="${item.pattern === group.pattern}">
          <img src="${patternLibraryPreviewUrl(item.pattern)}" alt="">
        </button>`).join("")}
    </div>
    <div class="action-row end"><button class="button secondary small" type="button"
      data-close-pattern-dialog>Close</button></div>`;
  document.body.append(backdrop, dialog);
  syncTestPatternChoices();
  dialog.querySelector("[data-close-pattern-dialog]")?.focus();
}

async function chooseTestOptimizedPattern(button) {
  if (testPatternChoicePending || testPatternUpdating() || !state.selected) return;
  const stem = state.selected;
  const groupId = Number(state.patternDialog?.groupId);
  const pattern = button.dataset.choosePattern;
  if (!Number.isInteger(groupId) || !pattern) return;
  const current = (state.data.patterns?.groups || []).find(
    (group) => Number(group.group_id) === groupId,
  );
  if (!current) return;
  const suggested = state.data.step7Review?.preserve_haptic_distances !== false;
  const data = state.data.patterns;
  const reason = patternChoiceBlock(current, pattern, data.groups, data.library, suggested);
  if (reason) { toast(reason, "warning"); return; }
  if (current?.pattern === pattern && !testPatternErrorsByProject.has(stem)) {
    state.patternDialog = null;
    document.querySelectorAll(".pattern-dialog, .pattern-dialog-backdrop")
      .forEach((node) => node.remove());
    return;
  }

  testPatternChoicePending = true;
  button.disabled = true;
  let applied = false;
  const swatches = [];
  try {
    for (const warning of patternChoiceWarnings(current, pattern, data.groups, data.library, suggested)) {
      if (!await confirmTestPatternChoice(warning)) return;
    }
    if (state.selected !== stem) return;
    // The selected library tile is already cached. Show the choice immediately
    // while retaining the last valid full map until its replacement is ready.
    document.querySelectorAll(`[data-pattern-preview="${groupId}"]`).forEach((image) => {
      swatches.push([image, image.src]);
      image.src = patternLibraryPreviewUrl(pattern);
    });
    await runTestPatternTask(stem, async () => {
      const result = await saveTestPatternEdit(stem, {
        action: "assign", group_id: groupId, pattern, preserve_haptic_distances: suggested,
      });
      if (state.selected !== stem) return;
      applied = true;
      if (result.pattern_data) state.data.patterns = result.pattern_data;
      if (result.review) state.data.step7Review = result.review;
      state.patternDialog = null;
      document.querySelectorAll(".pattern-dialog, .pattern-dialog-backdrop")
        .forEach((node) => node.remove());
      await refreshTestPatternArtifacts(stem, suggested
        ? state.data.patterns.groups.map((item) => item.group_id) : [groupId]);
      toast(suggested ? "Pattern changed and the remaining categories were re-optimized."
        : "Pattern changed for this category.");
    });
  } finally {
    if (!applied) swatches.forEach(([image, source]) => { if (image.isConnected) image.src = source; });
    testPatternChoicePending = false;
    if (button.isConnected) button.disabled = false;
    syncTestPatternControls();
  }
}

async function updateTestPatternPreview(groupId) {
  const stem = state.selected;
  const group = state.data.patterns?.groups?.find((item) => Number(item.group_id) === Number(groupId));
  if (!group) return;
  const defaults = state.data.patterns.defaults || {};
  const transformed = Object.entries(defaults).some(([key, value]) => Number(group.transform?.[key] ?? value) !== Number(value));
  let source = patternLibraryPreviewUrl(group.pattern);
  if (transformed) {
    const response = await fetch(patternPreviewUrl(stem, groupId), { cache: "no-store" });
    if (!response.ok) return;
    source = URL.createObjectURL(await response.blob());
  }
  const loaded = await waitForTestImage(source);
  if (!loaded || state.selected !== stem) {
    if (source.startsWith("blob:")) URL.revokeObjectURL(source);
    return;
  }
  const key = `${stem}:${groupId}`;
  const previous = testPatternSwatchUrls.get(key);
  testPatternSwatchUrls.set(key, source);
  document.querySelectorAll(`[data-pattern-preview="${groupId}"]`).forEach((image) => {
    image.src = source;
  });
  if (previous?.startsWith("blob:")) URL.revokeObjectURL(previous);
}

function queueTestPatternTransform(slider) {
  if ((testSuggestedIntentByProject.get(state.selected)?.enabled
      ?? state.data.step7Review?.preserve_haptic_distances) !== false) return;
  const dialog = slider.closest(".pattern-dialog");
  const groupId = Number(state.patternDialog?.groupId);
  if (!dialog || !Number.isInteger(groupId) || !state.selected) return;
  const transform = {};
  dialog.querySelectorAll('input[name="pattern-transform"]').forEach((input) => {
    transform[input.dataset.key] = Number(input.value);
  });
  pendingTestPatternTransform = { stem: state.selected, groupId, transform };
  syncTestPatternControls();
  const status = dialog.querySelector("#transform-status");
  if (status) status.textContent = "Updating preview…";
  window.clearTimeout(testPatternTransformTimer);
  testPatternTransformTimer = window.setTimeout(flushTestPatternTransform, 450);
}

async function flushTestPatternTransform() {
  if (!pendingTestPatternTransform) return;
  let request = pendingTestPatternTransform;
  pendingTestPatternTransform = null;
  return runTestPatternTask(request.stem, async () => {
    if (pendingTestPatternTransform?.stem === request.stem
        && pendingTestPatternTransform.groupId === request.groupId) {
      request = pendingTestPatternTransform;
      pendingTestPatternTransform = null;
      window.clearTimeout(testPatternTransformTimer);
    }
    const group = (state.data.patterns?.groups || []).find(
      (item) => Number(item.group_id) === request.groupId,
    );
    if (!group || samePatternTransform(group.transform, request.transform)
        && !testPatternErrorsByProject.has(request.stem)) return;
    const result = await saveTestPatternEdit(request.stem, {
      action: "transform", group_id: request.groupId, transform: request.transform,
    });
    if (state.selected !== request.stem) return;
    state.data.patterns = result.pattern_data;
    state.data.step7Review = result.review;
    if (!pendingTestPatternTransform) await refreshTestPatternArtifacts(request.stem, [request.groupId]);
    const status = document.querySelector(".pattern-dialog #transform-status");
    if (status) status.textContent = "Updated.";
  });
}

function testPatternColours() {
  return Object.fromEntries((state.data.patterns?.groups || []).map((group) => {
    const picker = document.querySelector(`[data-pattern-colour="${group.group_id}"]`);
    return [group.label, (picker?.value || state.data.colors?.colors?.[group.label] || "#FFFFFF").toUpperCase()];
  }));
}

function queueTestPatternColours() {
  if (!hybridMapEnabled() || !state.selected) return;
  const colors = testPatternColours();
  state.data.colors = { colors };
  pendingTestPatternColours = { stem: state.selected, colors };
  syncTestPatternControls();
  window.clearTimeout(testPatternColourTimer);
  testPatternColourTimer = window.setTimeout(flushTestPatternColours, 180);
}

function flushTestPatternColours() {
  if (!pendingTestPatternColours) return Promise.resolve();
  let request = pendingTestPatternColours;
  pendingTestPatternColours = null;
  return runTestPatternTask(request.stem, async () => {
    if (pendingTestPatternColours?.stem === request.stem) {
      request = pendingTestPatternColours;
      pendingTestPatternColours = null;
      window.clearTimeout(testPatternColourTimer);
    }
    const saved = await saveCategoryColors(request.stem, request.colors);
    if (state.selected !== request.stem) return;
    if (pendingTestPatternColours) return;
    state.data.colors = saved;
    state.colourView = true;
    if (state.data.step7Review) state.data.step7Review.approved = false;
    await refreshTestPatternArtifacts(request.stem);
    document.body.classList.add("test-hybrid-view-visible");
  });
}

function resetTestPatternTransform() {
  const defaults = state.data.patterns?.defaults;
  const dialog = document.querySelector(".pattern-dialog");
  if (!defaults || !dialog || state.data.step7Review?.preserve_haptic_distances !== false) return;
  dialog.querySelectorAll('input[name="pattern-transform"]').forEach((slider) => {
    slider.value = defaults[slider.dataset.key];
    slider.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const slider = dialog.querySelector('input[name="pattern-transform"]');
  if (slider) queueTestPatternTransform(slider);
  window.clearTimeout(testPatternTransformTimer);
  void flushTestPatternTransform();
}

function handleTestPatternClick(event) {
  if (!document.body.classList.contains("test-pipeline-open")) return;
  const close = event.target.closest("[data-close-pattern-dialog]");
  if (close?.closest('[data-test-pattern-change="true"]')) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const groupId = state.patternDialog?.groupId;
    state.patternDialog = null;
    document.querySelectorAll('[data-test-pattern-change="true"]').forEach((node) => node.remove());
    document.querySelector(`[data-change-pattern="${groupId}"]`)?.focus();
    return;
  }
  if (event.target.closest("#transform-reset")) {
    event.preventDefault();
    event.stopImmediatePropagation();
    resetTestPatternTransform();
    return;
  } else if (event.target.closest("[data-close-pattern-dialog]")
      && pendingTestPatternTransform) {
    window.clearTimeout(testPatternTransformTimer);
    void flushTestPatternTransform();
  }
  const hybrid = event.target.closest("#create-hybrid-map");
  if (hybrid) {
    event.preventDefault();
    event.stopImmediatePropagation();
    void toggleTestHybridMode(hybrid);
    return;
  }
  const suggested = event.target.closest("#preserve-haptic-distances");
  if (suggested) {
    event.preventDefault();
    event.stopImmediatePropagation();
    void toggleTestSuggestedPatterns(suggested);
    return;
  }
  const choice = event.target.closest("[data-choose-pattern]");
  if (choice) {
    event.preventDefault();
    event.stopImmediatePropagation();
    void chooseTestOptimizedPattern(choice);
    return;
  }
  const change = event.target.closest("[data-change-pattern]");
  if (change) {
    event.preventDefault();
    event.stopImmediatePropagation();
    openTestPatternChange(change);
    return;
  }
  if (event.target.closest("[data-edit-pattern]")
      && (state.data.step7Review?.preserve_haptic_distances !== false || testPatternUpdating())) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}

function allBrailleTextEnabled(layout = state.data.braille) {
  if (!layout) return false;
  return layout.title?.enabled !== false
    && (layout.labels || []).every((label) => label.enabled !== false);
}

function syncTestBrailleTextControls(root) {
  const layout = state.data.braille || {};
  const title = root.querySelector("#braille-title-enabled");
  if (title) title.checked = layout.title?.enabled !== false;
  root.querySelectorAll("[data-braille-row]").forEach((row) => {
    const label = (layout.labels || []).find(
      (item) => String(item.id) === row.dataset.brailleRow,
    );
    const enabled = label?.enabled !== false;
    const input = row.querySelector(".braille-enabled");
    if (input) input.checked = enabled;
    row.classList.toggle("is-hidden", !enabled);
  });
  const toggle = root.querySelector("#braille-all-off");
  if (toggle) toggle.setAttribute("aria-pressed", String(allBrailleTextEnabled(layout)));
}

function setLocalBrailleTextEnabled(enabled) {
  const layout = state.data.braille;
  if (!layout) return;
  if (layout.title) layout.title.enabled = enabled;
  (layout.labels || []).forEach((label) => { label.enabled = enabled; });
}

async function toggleTestBrailleText(event) {
  const button = event.target.closest("#braille-all-off");
  if (!button) return false;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (button.disabled || !state.selected || !state.data.braille) return true;

  const root = button.closest(".braille-decision");
  const layout = state.data.braille;
  const before = {
    title: layout.title?.enabled !== false,
    labels: (layout.labels || []).map((label) => [String(label.id), label.enabled !== false]),
  };
  const enabled = button.getAttribute("aria-pressed") !== "true";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  setLocalBrailleTextEnabled(enabled);
  syncTestBrailleTextControls(root);
  bindBrailleOverlay();
  try {
    const result = await saveBrailleLayout(state.selected, { all_text_enabled: enabled });
    state.data.braille = result.layout;
    state.data.pageLayout = {
      ...(state.data.pageLayout || {}),
      map_origin_px: result.layout.page?.map_origin_px,
      furniture: result.layout.page?.furniture,
    };
    if (state.data.step8Review) state.data.step8Review.approved = false;
    syncTestBrailleTextControls(root);
    bindBrailleOverlay();
  } catch (error) {
    if (layout.title) layout.title.enabled = before.title;
    const prior = new Map(before.labels);
    (layout.labels || []).forEach((label) => {
      label.enabled = prior.get(String(label.id)) !== false;
    });
    syncTestBrailleTextControls(root);
    bindBrailleOverlay();
    toast(error.message, "error");
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
  return true;
}

function prepareTestReviewContent(root, definition) {
  if (completedReview.step === 2 && !root.querySelector("[data-test-step2-parts]")) {
    const parts = document.createElement("div");
    parts.className = "action-row";
    parts.dataset.testStep2Parts = "true";
    for (const [part, label] of [["mask", "Map area"], ["legend", "Legend"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button secondary small";
      button.dataset.testStep2Part = part;
      button.setAttribute("aria-pressed", String(completedReview.maskPhase === part));
      button.textContent = label;
      button.addEventListener("click", () => completedReview.setMaskPhase(part));
      parts.appendChild(button);
    }
    root.querySelector("h3")?.after(parts);
  }
  if (definition === TEST_REVIEW_PANELS.simplification) {
    root.querySelector(".range-heading .field-label")?.remove();
    root.querySelector(".preset-labels")?.remove();
    root.querySelector(".preset-hero > .action-row .status-copy")?.remove();
  }
  if (definition === TEST_REVIEW_PANELS.patterns) {
    root.querySelector(".pattern-mode-grid + .section-intro")?.remove();
    root.querySelector("#pattern-review-status")?.remove();
    const suggested = root.querySelector("#preserve-haptic-distances strong");
    if (suggested) suggested.textContent = "Use suggested patterns";
    enableTestSuggestedPatternChanges(root);
    removeTestPatternDescriptions(root);
  }
  if (definition === TEST_REVIEW_PANELS.aggregation) {
    const intro = root.querySelector(":scope > .section-intro");
    if (intro) emphasizeAggregationInstructions(intro);
    const unused = state.data.aggregation?.proposal?.unused_legend_classes || [];
    if (unused.length && !root.querySelector("[data-unused-legend-classes]")) {
      const note = document.createElement("p");
      note.className = "status-copy";
      note.dataset.unusedLegendClasses = "true";
      note.textContent = `Legend entries with no detected map pixels: ${unused.map((item) => item.label).join(", ")}.`;
      root.appendChild(note);
    }
    const usage = root.querySelector(".category-toolbar p");
    usage?.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        node.textContent = node.textContent.replace(
          /\s*The limit is a maximum, not a target\./,
          "",
        );
      }
    });
    const reset = root.querySelector("#reset-groups");
    if (reset) reset.textContent = "Reset to suggested";
  }
  if (definition === TEST_REVIEW_PANELS.braille) {
    prepareTestBrailleControls(root);
    root.querySelector(".braille-review-status")?.remove();
    const allText = root.querySelector("#braille-all-off");
    if (allText) {
      allText.className = "pattern-mode-button test-all-text-toggle";
      allText.innerHTML = '<span class="mode-switch" aria-hidden="true"></span><span><strong>Display all text</strong></span>';
    }
    root.querySelector(".braille-furniture-box .braille-subheading span")?.remove();
    root.querySelector(".braille-furniture-box .field-note")?.remove();
    root.querySelector('.braille-furniture-actions button[title*="Map scale"]')?.remove();
    root.querySelectorAll(".braille-row-detail").forEach((detail) => {
      const preview = detail.querySelector(".braille-preview");
      if (preview) detail.prepend(preview);
    });
    syncTestBrailleTextControls(root);
  }
  if (definition === TEST_REVIEW_PANELS.finalLegend) {
    root.querySelector("#approve-legend")?.addEventListener("click", watchReopenedLegendApproval, { capture: true });
    buildTestLegendCompare(root);
    root.querySelectorAll(".pattern-chip").forEach((node) => node.remove());
    root.querySelectorAll(".section-intro").forEach((node) => {
      if (node !== root.querySelector(":scope > .section-intro")) node.remove();
    });
    root.querySelector("#legend-review-status")?.remove();
  }
}

function enhanceTestReviewPanel(reviewStep, exportReady, failed, blocked) {
  const definition = testReviewPanelDefinition(reviewStep, exportReady, failed, blocked);
  if (definition !== TEST_REVIEW_PANELS.mask) clearTestMaskToolSelection();
  if (!definition) return;
  const content = $("control-content");
  let root = content.querySelector(definition.selector);
  if (!root) return;
  root = completedReview.bind(root, Number(reviewStep));

  if (definition === TEST_REVIEW_PANELS.mask) syncTestMaskToolSelection(root);

  if (root.dataset.testReviewShell === "true") {
    if (definition === TEST_REVIEW_PANELS.patterns) syncTestPatternControls();
    if (definition === TEST_REVIEW_PANELS.mask) updateTestMaskActions(root);
    if (definition === TEST_REVIEW_PANELS.finalLegend) syncTestLegendCompare(root);
    return;
  }

  root.querySelectorAll(".section-kicker").forEach((node) => node.remove());
  root.querySelectorAll(".advanced-box").forEach((node) => node.remove());
  const title = root.querySelector("h3");
  let intro = root.querySelector(":scope > .section-intro");
  if (title) {
    title.textContent = definition.title;
    title.removeAttribute("id");
  }
  if (definition.intro) {
    if (!intro) {
      intro = document.createElement("p");
      intro.className = "section-intro";
    }
    intro.textContent = definition.intro;
  }
  prepareTestReviewContent(root, definition);
  if (definition.primary) {
    const primary = root.querySelector(definition.primary);
    if (primary && definition.primaryText) primary.textContent = definition.primaryText;
  }

  const action = definition.action ? root.querySelector(definition.action) : null;
  const actionParent = action?.parentElement;
  const header = document.createElement("header");
  header.className = "test-review-shell-header";
  if (title) header.appendChild(title);
  if (intro) header.appendChild(intro);

  const body = document.createElement("div");
  body.className = "test-review-shell-body";
  const footer = document.createElement("footer");
  footer.className = "test-review-shell-footer";

  if (action && definition !== TEST_REVIEW_PANELS.export) footer.appendChild(action);
  const exportHeading = root.querySelector(":scope > .export-heading");
  if (definition === TEST_REVIEW_PANELS.export) {
    exportHeading?.remove();
    const ready = document.createElement("p");
    ready.className = "test-export-ready-copy";
    ready.textContent = "Your tactile map is ready.";
    body.appendChild(ready);
    if (action) body.appendChild(action);
    const note = document.createElement("p");
    note.className = "test-export-step-note";
    note.textContent = "You can now click on each step to see the details.";
    body.appendChild(note);
  }
  if (actionParent && actionParent !== root && !actionParent.children.length
      && !actionParent.textContent.trim()) actionParent.remove();

  Array.from(root.children).forEach((child) => {
    if (child === header || child === body || child === footer || child === title
        || child === intro || child === action || child === exportHeading
        || child.classList.contains("test-review-native-actions")) return;
    body.appendChild(child);
  });

  if (definition === TEST_REVIEW_PANELS.mask) buildTestMaskFooter(root, body, footer);
  if (definition === TEST_REVIEW_PANELS.legend || definition === TEST_REVIEW_PANELS.savedLegend) buildTestLegendFooter(root, footer);
  root.replaceChildren(header, body, ...Array.from(root.querySelectorAll(":scope > .test-review-native-actions")), footer);
  root.classList.add("test-review-shell");
  root.dataset.testReviewShell = "true";
  root.dataset.testReviewPanel = testReviewPanelKey(definition);
  content.hidden = false;
  content.replaceChildren(root);
  restoreTestReviewScroll(root);
  if (definition === TEST_REVIEW_PANELS.export) disableTestExportPanelPreview(root);
  if (definition === TEST_REVIEW_PANELS.patterns) relocateTestPatternDialog();
  if (definition === TEST_REVIEW_PANELS.mask) updateTestMaskActions(root);
  if (definition === TEST_REVIEW_PANELS.finalLegend) syncTestLegendCompare(root);
}

function scheduleTestReviewStabilization() {
  if (reviewPanelStabilizeQueued) return;
  reviewPanelStabilizeQueued = true;
  queueMicrotask(() => {
    reviewPanelStabilizeQueued = false;
    if (!document.body.classList.contains("test-pipeline-open")) return;
    if (testPatternUpdating()) return;
    const map = selectedMap();
    if (!map || isRunning(map)) return;
    const job = state.job || map.job || { status: "idle" };
    const reviewStep = pipelineReviewStep(map);
    const exportReady = Boolean(!reviewStep && map.steps?.["9"] && map.step9_review_ready);
    const failed = job.status === "failed";
    const blocked = Boolean(map.pipeline_error || map.step1_error || map.in_scope === false);
    const definition = testReviewPanelDefinition(reviewStep, exportReady, failed, blocked);
    if (!definition) return;
    const content = $("control-content");
    const mounted = content.querySelector(":scope > .test-review-shell");
    if (mounted?.dataset.testReviewPanel === testReviewPanelKey(definition)) return;
    // Shared editors redraw themselves after add/reset/drag operations. Wrap
    // that new DOM in the /test shell in the same microtask, before a raw
    // alternate panel can be painted on screen.
    enhanceTestReviewPanel(reviewStep, exportReady, failed, blocked);
  });
}

function bindTestReviewStabilizer() {
  const content = $("control-content");
  if (!content || reviewPanelObserver) return;
  reviewPanelObserver = new MutationObserver(scheduleTestReviewStabilization);
  reviewPanelObserver.observe(content, { childList: true });
}

function renderTestVisual() {
  renderVisual();
  enhanceTestViewers();
}

function actualPipelineStage(map, live, job, reviewStep) {
  const jobStep = Number(job?.current);
  if (live && jobStep >= 1 && jobStep <= STEP_DEFS.length) return jobStep;
  if (reviewStep) return Number(reviewStep);
  return STEP_DEFS.reduce(
    (latest, step) => map?.steps?.[step.key] ? Number(step.key) : latest, 0,
  );
}

/* Backend stages can finish between two polling responses. Advance the
   visible stage one at a time so every message and progress segment appears. */
function presentedPipelineStage(map, targetStep, live, reviewStep) {
  if (completedReview.active && !live) {
    stagePresentation.stem = map?.stem;
    stagePresentation.step = targetStep;
    stagePresentation.holdUntil = 0;
    return { step: targetStep, catchingUp: false };
  }
  if (!map) {
    resetStagePresentation();
    return { step: 0, catchingUp: false };
  }
  if (stagePresentation.stem !== map.stem) {
    stagePresentation.stem = map.stem;
    stagePresentation.step = completedCount(map) ? targetStep : 0;
    stagePresentation.holdUntil = 0;
  }
  const now = Date.now();
  if (forcedAnalysisPresentation.active) {
    const currentForcedStep = ANALYSIS_SEQUENCE[forcedAnalysisPresentation.index];
    const currentForcedStepComplete = Boolean(map.steps?.[String(currentForcedStep)]);
    if (currentForcedStepComplete && now >= forcedAnalysisPresentation.holdUntil
        && forcedAnalysisPresentation.index < ANALYSIS_SEQUENCE.length - 1) {
      forcedAnalysisPresentation.index += 1;
      forcedAnalysisPresentation.holdUntil = now + STAGE_DWELL_MS;
    }
    const forcedStep = ANALYSIS_SEQUENCE[forcedAnalysisPresentation.index];
    stagePresentation.step = forcedStep;
    stagePresentation.holdUntil = forcedAnalysisPresentation.holdUntil;
    const sequenceDisplayed = forcedAnalysisPresentation.index === ANALYSIS_SEQUENCE.length - 1
      && Boolean(map.steps?.["5"]) && now >= forcedAnalysisPresentation.holdUntil;
    // Release the staged progress animation as soon as Step 5's backend work
    // is complete. /test then presents its mandatory category review, even
    // when the proposal is an identity grouping with five or fewer classes.
    const backendReady = !live && Boolean(map.steps?.["5"]);
    if (!sequenceDisplayed || !backendReady) {
      return { step: forcedStep, catchingUp: true };
    }
    forcedAnalysisPresentation.active = false;
    stagePresentation.holdUntil = 0;
    return { step: 5, catchingUp: false };
  }
  if (targetStep < stagePresentation.step) {
    stagePresentation.step = targetStep;
    stagePresentation.holdUntil = 0;
  } else if (targetStep > stagePresentation.step && now >= stagePresentation.holdUntil) {
    stagePresentation.step += 1;
    stagePresentation.holdUntil = now + STAGE_DWELL_MS;
  }
  const holdingForReview = Boolean(
    !live && reviewStep && stagePresentation.step === targetStep
      && now < stagePresentation.holdUntil,
  );
  return {
    step: stagePresentation.step,
    catchingUp: stagePresentation.step < targetStep || holdingForReview,
  };
}

function copyStablePreview() {
  const visual = $("visual-content");
  const stable = $("test-stable-preview");
  // Preserve the actual loaded viewer rather than cloning it. Moving these
  // nodes retains canvas pixels, image load state, fitted transforms and the
  // IDs that their stage-specific CSS needs while the live pane redraws.
  enhanceTestViewers();
  stable.replaceChildren(...Array.from(visual.childNodes));
  // Keep the scroll container interactive while the parked map and its
  // controls remain read-only during processing.
  stable.querySelectorAll(".page-view-toolbar, .map-canvas").forEach((node) => {
    node.inert = true;
  });
  stable.inert = false;
  stable.hidden = false;
}

function freezeCurrentPreview(lockUntilReview = false) {
  const map = selectedMap();
  const visual = $("visual-content");
  if (!map || !visual || visual.hidden || !visual.childNodes.length) return;
  const currentStep = Number(state.activeStep)
    || STEP_DEFS.reduce(
      (latest, step) => map.steps?.[step.key] ? Number(step.key) : latest, 1,
    );
  copyStablePreview();
  previewPresentation.stem = map.stem;
  previewPresentation.step = currentStep;
  previewPresentation.pendingUntil = Date.now() + 10000;
  previewPresentation.locked = lockUntilReview;
  state.viewStep = TEST_PREVIEW_PIN;
  state.activeStep = currentStep;
  document.body.classList.add("test-preview-frozen");
}

function stablePreviewSelection(map, visibleStep) {
  const limit = Math.max(1, visibleStep - 1);
  const step = STEP_DEFS.reduce((latest, definition) => {
    const number = Number(definition.key);
    return number <= limit && map.steps?.[definition.key] ? number : latest;
  }, 1);
  const extractedStep2 = step === 2 && visibleStep > 2;
  return {
    step,
    key: extractedStep2 ? "2-extracted-map-and-legend" : step,
    extractedStep2,
  };
}

function renderExtractedStep2Preview(map) {
  const visual = $("visual-content");
  visual.hidden = false;
  visual.innerHTML = `
    <article class="map-stage test-extracted-stage" data-view="${EXTRACTED_PREVIEW_VIEW}"
        aria-label="Extracted Step 2 results">
      ${viewerToolbarHtml({}, EXTRACTED_PREVIEW_VIEW)}
      <div class="map-frame">
        <div class="map-viewport-content"><div class="map-zoom-space">
          <div class="map-canvas test-extracted-comparison">
            <div class="test-extracted-preview-grid">
              <figure class="test-extracted-preview-card">
                <figcaption>Extracted map</figcaption>
                <div><img data-viewer-image data-artifact="map_text_input.png"
                  src="${artifactUrl(map.stem, "map_text_input.png")}" alt="Extracted map for ${esc(map.name)}"></div>
              </figure>
              <figure class="test-extracted-preview-card">
                <figcaption>Extracted legend</figcaption>
                <div><img data-artifact="legend.png"
                  src="${artifactUrl(map.stem, "legend.png")}" alt="Extracted legend for ${esc(map.name)}"></div>
              </figure>
            </div>
          </div>
        </div></div>
      </div>
    </article>`;
  void bindExtractedStep2Viewer();
}

function waitForPreviewImage(image) {
  if (image.complete) return Promise.resolve();
  return new Promise((resolve) => {
    image.addEventListener("load", resolve, { once: true });
    image.addEventListener("error", resolve, { once: true });
  });
}

async function bindExtractedStep2Viewer() {
  const stage = document.querySelector(".test-extracted-stage");
  const sheet = stage?.querySelector(".test-extracted-comparison");
  const grid = sheet?.querySelector(".test-extracted-preview-grid");
  const images = Array.from(grid?.querySelectorAll("img") || []);
  if (!stage || !sheet || images.length !== 2) return;
  await Promise.all(images.map(waitForPreviewImage));
  if (!stage.isConnected) return;
  const normalizedHeight = Math.max(...images.map((image) => image.naturalHeight || 600));
  const widths = images.map((image) => {
    const width = image.naturalWidth || 800;
    const height = image.naturalHeight || normalizedHeight;
    return width / height * normalizedHeight;
  });
  const gap = Math.max(24, Math.round(normalizedHeight * .025));
  const naturalWidth = Math.round(widths[0] + widths[1] + gap + 40);
  const naturalHeight = Math.round(normalizedHeight + 90);
  sheet.dataset.naturalWidth = String(naturalWidth);
  sheet.dataset.naturalHeight = String(naturalHeight);
  sheet.style.aspectRatio = `${naturalWidth} / ${naturalHeight}`;
  grid.style.gridTemplateColumns = `${widths[0]}fr ${widths[1]}fr`;
  enhanceTestViewers();
  bindViewer(EXTRACTED_PREVIEW_VIEW, async () => true);
  // The image-sized canvas is fitted and zoomed as a whole. Compensate only
  // its captions so high-resolution maps cannot shrink their UI text.
  const syncCaptionScale = () => {
    const scale = sheet.getBoundingClientRect().width / sheet.offsetWidth;
    if (scale > 0) grid.style.setProperty("--test-caption-scale", String(1 / scale));
  };
  new MutationObserver(syncCaptionScale).observe(sheet, {
    attributes: true, attributeFilter: ["style"],
  });
  syncCaptionScale();
}

/* Park the last valid visual over the live pane while the backend is polling.
   The hidden live pane may redraw as often as needed without flashing or
   replacing the completed result that the user is looking at. */
function syncStablePreview(map, visibleStep, processing, reviewStep) {
  const visual = $("visual-content");
  const stable = $("test-stable-preview");
  if (!map || !visual || !stable) return;
  if (processing) {
    if (previewPresentation.locked && previewPresentation.stem === map.stem
        && !stable.hidden) {
      state.viewStep = TEST_PREVIEW_PIN;
      state.activeStep = Number(previewPresentation.step) || 7;
      previewPresentation.pendingUntil = 0;
      document.body.classList.add("test-preview-frozen");
      return;
    }
    const desired = stablePreviewSelection(map, visibleStep);
    const changed = previewPresentation.stem !== map.stem
      || previewPresentation.step !== desired.key || stable.hidden;
    state.viewStep = TEST_PREVIEW_PIN;
    state.activeStep = desired.step;
    previewPresentation.pendingUntil = 0;
    document.body.classList.add("test-preview-frozen");
    if (changed) {
      if (desired.extractedStep2) renderExtractedStep2Preview(map);
      else renderTestVisual();
      copyStablePreview();
      previewPresentation.stem = map.stem;
      previewPresentation.step = desired.key;
    }
    return;
  }
  const awaitingBrailleRun = previewPresentation.locked
    && Number(previewPresentation.step) === 7
    && !map.steps?.["8"]
    && reviewStep !== "8"
    && state.job?.status !== "failed";
  if (awaitingBrailleRun && !stable.hidden) {
    state.viewStep = TEST_PREVIEW_PIN;
    state.activeStep = 7;
    document.body.classList.add("test-preview-frozen");
    return;
  }
  const wasFrozen = document.body.classList.contains("test-preview-frozen");
  if (previewPresentation.locked) {
    previewPresentation.locked = false;
    previewPresentation.pendingUntil = 0;
  }
  if (wasFrozen && Date.now() < previewPresentation.pendingUntil) return;
  if (state.viewStep === TEST_PREVIEW_PIN) state.viewStep = null;
  // Reveal the live pane before asking it to render the review. If a viewer
  // enhancement fails, the old parked Step 2 result must not remain on top.
  document.body.classList.remove("test-preview-frozen");
  stable.hidden = true;
  stable.replaceChildren();
  previewPresentation.stem = null;
  previewPresentation.step = 0;
  previewPresentation.pendingUntil = 0;
  previewPresentation.locked = false;
  if (wasFrozen) {
    state.activeStep = reviewStep ? Number(reviewStep)
      : STEP_DEFS.reduce(
        (latest, step) => map.steps?.[step.key] ? Number(step.key) : latest, 1,
      );
    renderTestVisual();
  }
}

function ensureTestReviewControls(reviewStep) {
  if (!reviewStep) return;
  const content = $("control-content");
  content.hidden = false;
  if (completedReview.active) {
    if (content.querySelector(`[data-test-completed-bound="${reviewStep}"]`)) return;
    state.viewStep = String(reviewStep);
    state.activeStep = Number(reviewStep);
    renderControls();
    if (reviewStep === "2" && completedReview.maskPhase === "legend" && !content.querySelector(".legend-review")) {
      const body = content.querySelector('.step-section[data-step="2"] .editor-body');
      if (body) body.innerHTML = '<section class="review-gate legend-review"><h3>Saved legend</h3></section>';
    }
    if (reviewStep === "4") {
      const body = content.querySelector('.step-section[data-step="4"] .editor-body');
      if (body) body.innerHTML = '<section class="review-gate test-segmentation-review"></section>';
    }
    return;
  }
  const definition = testReviewPanelDefinition(reviewStep, false, false, false);
  const mounted = content.querySelector(":scope > .test-review-shell");
  if (mounted?.dataset.testReviewPanel === testReviewPanelKey(definition)) return;
  const current = content.querySelector(
    `.step-section[data-step="${reviewStep}"][open] .review-gate`,
  );
  if (current) return;
  state.viewStep = null;
  state.activeStep = Number(reviewStep);
  renderControls();
}

function renderPipelineProgress(map, reviewStep, runningStep, visibleStep, failedStep) {
  const progress = $("test-pipeline-progress");
  const selectable = completedReview.available(map);
  progress.setAttribute("role", selectable ? "group" : "progressbar");
  progress.setAttribute("aria-label", selectable ? "Review pipeline steps" : "Pipeline progress");
  const visibleCompleted = STEP_DEFS.filter(
    (step) => selectable && completedReview.project?.snapshots.has(Number(step.key))
      || Number(step.key) <= visibleStep && map?.steps?.[step.key],
  ).length;
  progress.setAttribute("aria-valuenow", String(visibleCompleted));
  const segments = STEP_DEFS.map((step) => {
    const isVisible = selectable || Number(step.key) <= visibleStep;
    const className = step.key === failedStep ? "failed"
      : !isVisible ? ""
        : step.key === reviewStep ? "waiting-review"
          : step.key === runningStep ? "running"
            : map?.steps?.[step.key] || selectable && completedReview.project?.snapshots.has(Number(step.key)) ? "done" : "";
    return { step, className };
  });
  const signature = segments.map(({ className }) => className || "idle").join("|")
    + `:${selectable}:${completedReview.step}:${completedReview.busy || state.busy}`;
  if (progress.dataset.signature === signature) return;
  progress.dataset.signature = signature;
  progress.innerHTML = segments.map(({ step, className }) =>
    `<${selectable ? "button type=\"button\"" : "span"} class="test-progress-step ${className}"
      ${selectable ? `data-test-review-step="${step.key}" aria-current="${completedReview.step === Number(step.key) ? "step" : "false"}" ${completedReview.busy || state.busy ? "disabled" : ""}` : ""}
      title="${esc(selectable ? `Review step ${step.number}: ${STEP_LABELS[step.key]}` : THINKING_MESSAGES[step.key])}">
      <span class="test-progress-node" aria-hidden="true">${className === "done" ? "✓" : step.key}</span>
      <span class="test-progress-label">${esc(STEP_LABELS[step.key])}</span>
    </${selectable ? "button" : "span"}>`).join("");
}

function finishPipelineTitleEdit(restoreFocus = true) {
  const button = $("test-pipeline-title");
  const input = $("test-pipeline-title-input");
  if (!button || !input) return;
  input.hidden = true;
  button.hidden = false;
  titleEditSaving = false;
  if (restoreFocus && document.body.classList.contains("test-pipeline-open")) {
    button.focus({ preventScroll: true });
  }
}

function beginPipelineTitleEdit() {
  const map = selectedMap();
  const button = $("test-pipeline-title");
  const input = $("test-pipeline-title-input");
  if (!map || button.disabled || titleEditSaving) return;
  input.value = map.stem;
  button.hidden = true;
  input.hidden = false;
  input.focus();
  input.select();
}

function cancelPipelineTitleEdit() {
  finishPipelineTitleEdit();
  syncPipelineChrome();
}

async function commitPipelineTitleEdit() {
  if (titleEditSaving) return;
  const map = selectedMap();
  const input = $("test-pipeline-title-input");
  const name = input.value.trim();
  if (!map || !name || name === map.stem) {
    cancelPipelineTitleEdit();
    return;
  }

  titleEditSaving = true;
  input.disabled = true;
  $("test-start-status").textContent = "Saving project name…";
  try {
    const result = await renameMap(map.stem, name);
    if (state.selected === map.stem) state.selected = result.stem;
    await loadMaps();
    renderProjectGallery();
    finishPipelineTitleEdit(false);
    $("test-start-status").textContent = "";
    syncPipelineChrome();
    toast("Project renamed.");
  } catch (error) {
    titleEditSaving = false;
    $("test-start-status").textContent = error.message;
    input.focus();
    input.select();
  } finally {
    input.disabled = false;
  }
}

/* The shared poll asks for /api/job and /api/maps concurrently. At the end of
   any job, an older maps response can arrive beside a newer "done" response.
   /test therefore performs an ordered watchdog read for every pipeline batch:
   job first, maps second. Once the job is done, that map read cannot be older
   than the completion it is describing. */
function reconcileFinishedJobSnapshot(map, job) {
  if (completedReview.active) return;
  if (testPatternUpdating(map?.stem)) return;
  const requested = (job?.steps || []).map(String);
  const highestRequested = requested.reduce(
    (highest, step) => Math.max(highest, Number(step) || 0), 0,
  );
  const snapshotMissing = requested.some((step) => !map?.steps?.[step]);
  // A reopened run can pause at an earlier review than its last completed
  // job. That deliberate pause must not redraw the viewer on every poll.
  const reviewStep = pipelineReviewStep(map);
  const presentationTarget = reviewStep
    ? Math.min(highestRequested, Number(reviewStep)) : highestRequested;
  const presentationBehind = presentationTarget > Number(stagePresentation.step || 0)
    && (stagePresentation.stem === map?.stem || activeStep(map) !== 7);
  const needsOrderedRefresh = requested.length && job?.status !== "failed"
    && (job?.status === "running"
      || job?.status === "done" && (snapshotMissing || presentationBehind));
  const now = Date.now();
  if (!needsOrderedRefresh || finishedJobRefresh
      || now - finishedJobRefreshAt < 1000) return;

  const stem = map.stem;
  finishedJobRefreshAt = now;
  finishedJobRefresh = (async () => {
    const authoritativeJob = await getJob(stem);
    if (state.selected !== stem || testPatternUpdating(stem)) return;
    state.job = authoritativeJob || { status: "idle" };
    // While the job is live, the normal workspace poll remains authoritative.
    // The ordered map refresh is needed only after that job has stopped.
    if (state.job.status === "running") return;
    await loadMaps();
    if (state.selected !== stem || testPatternUpdating(stem)) return;
    await refreshSelectedData();
    if (state.selected !== stem || testPatternUpdating(stem)) return;
    renderWorkspace(true);
  })().catch((error) => {
    // This is a redundant recovery poll, not the pipeline itself. A brief
    // request interruption is retried on the next watchdog tick and must not
    // be presented as a pipeline failure to the user.
    console.warn("Test UI pipeline-status refresh will retry.", error);
  }).finally(() => {
    finishedJobRefresh = null;
  });
}

function syncPipelineChrome() {
  if (!document.body.classList.contains("test-pipeline-open")) return;
  completedReview.remember();
  completedReview.normalize();
  if (testPatternUpdating() && activeStep(selectedMap()) === 7) return;
  const map = selectedMap();
  const live = Boolean(map && isRunning(map));
  const job = state.job || map?.job || { status: "idle" };
  if (live && Number(job.current) === 7) {
    suggestedPatternDefaultsByProject.delete(map.stem);
    try { sessionStorage.removeItem(`mapgen:test:initial-patterns:${map.stem}`); } catch { /* optional cache */ }
  }
  if (pipelineStartPending && (live || job.status === "failed")) {
    clearPipelineStartStatus();
  }
  reconcileFinishedJobSnapshot(map, job);
  if (job.status === "failed") forcedAnalysisPresentation.active = false;
  const fallbackStep = map ? STEP_DEFS.find((step) => !map.steps?.[step.key])?.key : "";
  const actualReviewStep = live ? null : pipelineReviewStep(map);
  const targetStep = actualPipelineStage(map, live, job, actualReviewStep)
    || Number(fallbackStep) || 0;
  const presentation = presentedPipelineStage(map, targetStep, live, actualReviewStep);
  const failed = job.status === "failed";
  const startRequestPending = pipelineStartPending && Boolean(state.busy || state.continuing);
  const presentationBusy = Boolean(!failed
    && (live || presentation.catchingUp || startRequestPending));
  const runningStep = presentationBusy && presentation.step ? String(presentation.step) : null;
  const reviewStep = presentation.catchingUp ? null : actualReviewStep;
  const completed = completedCount(map);
  const exportReady = Boolean(!actualReviewStep && map?.steps?.["9"] && map.step9_review_ready);
  const failedStep = failed
    ? (job.steps || []).map(String).find((step) => !map?.steps?.[step])
      || String(presentation.step || targetStep || "")
    : null;
  const blocked = Boolean(map?.pipeline_error || map?.step1_error || map?.in_scope === false);
  const showControls = Boolean(reviewStep || exportReady || failed || blocked);
  const canStart = Boolean(map && !presentationBusy && !actualReviewStep
    && !exportReady && !failed && !blocked
    && (completed === 0 || !state.autorun));

  let visualPreferenceChanged = false;
  if (map && (reviewStep === "9" || exportReady)
      && !finalStageDefaultsApplied.has(map.stem)) {
    finalStageDefaultsApplied.add(map.stem);
    visualPreferenceChanged = Boolean(state.showOriginalMap || !state.showFinalMap);
    state.showOriginalMap = false;
    state.showFinalMap = true;
  }
  visualPreferenceChanged = applyTestHybridColourPreference() || visualPreferenceChanged;
  applyTestVisualPreferences();
  if (visualPreferenceChanged
      && !document.body.classList.contains("test-preview-frozen")) {
    renderTestVisual();
  } else {
    enhanceTestViewers();
  }

  const titleButton = $("test-pipeline-title");
  const titleInput = $("test-pipeline-title-input");
  if (titleInput.hidden) titleButton.textContent = map?.stem || "New project";
  titleButton.disabled = !map || presentationBusy;
  $("test-pipeline-delete").disabled = !map || presentationBusy;
  $("test-pipeline-start").textContent = "Start";
  $("test-pipeline-start").disabled = !canStart;
  renderPipelineProgress(map, reviewStep, runningStep, presentation.step, failedStep);

  const source = $("test-source-image");
  const empty = $("test-source-empty");
  if (map) {
    if (source.dataset.mapName !== map.name) {
      source.src = mapUrl(map.name);
      source.alt = `Original map for ${map.name}`;
      source.dataset.mapName = map.name;
    }
    source.hidden = false;
    empty.hidden = true;
  } else {
    source.hidden = true;
    source.removeAttribute("src");
    source.dataset.mapName = "";
    empty.hidden = false;
  }

  // This is always the pipeline Start control. Review continuation belongs
  // exclusively to the fixed action footer in the right panel.
  $("test-start-panel").hidden = !map;
  document.body.classList.toggle("test-job-running", presentationBusy);
  document.body.classList.toggle("test-review-active", Boolean(reviewStep));
  document.body.classList.toggle("test-job-failed", failed);
  document.body.classList.toggle("test-export-ready", exportReady);
  document.body.classList.toggle("test-show-controls", showControls);
  try {
    ensureTestReviewControls(reviewStep);
  } catch (error) {
    console.warn("Test UI review controls will retry.", error);
  }
  enhanceTestReviewPanel(reviewStep, exportReady, failed, blocked);

  const thinking = $("test-thinking");
  const thinkingMessage = $("test-thinking-message");
  const transientStatus = $("test-start-status");
  const transientMessage = transientStatus.textContent.trim();
  transientStatus.hidden = true;
  if (failed) {
    thinking.hidden = false;
    thinkingMessage.textContent = job.error || "This step could not finish.";
  } else if (completedReview.busy) {
    thinking.hidden = false;
    thinkingMessage.textContent = live ? THINKING_MESSAGES[String(job.current)] || "Updating the next step…" : "Opening the saved review…";
  } else if (presentationBusy) {
    thinking.hidden = false;
    thinkingMessage.textContent = THINKING_MESSAGES[runningStep] || "Starting the pipeline…";
  } else if (reviewStep) {
    thinking.hidden = false;
    thinkingMessage.textContent = REVIEW_MESSAGES[reviewStep]
      || "Your review is needed before the pipeline continues.";
  } else if (exportReady) {
    thinking.hidden = false;
    thinkingMessage.textContent = "Your tactile map is ready to export.";
  } else if (map && completed > 0 && !canStart) {
    thinking.hidden = false;
    thinkingMessage.textContent = "Preparing the next step…";
  } else if (transientMessage) {
    thinking.hidden = false;
    thinkingMessage.textContent = "";
    transientStatus.hidden = false;
  } else {
    thinking.hidden = true;
  }
  document.body.classList.toggle("test-status-visible", !thinking.hidden);

  try {
    syncStablePreview(map, presentation.step, presentationBusy, reviewStep);
  } catch (error) {
    // Status, review controls, and the thinking message are already current.
    // Never allow a preview enhancement to strand the whole workflow.
    clearStablePreview();
    state.activeStep = reviewStep ? Number(reviewStep) : state.activeStep;
    console.warn("Test UI preview transition will retry.", error);
  }
}

async function discardTestMaskEdits(event) {
  const discard = event.target.closest("#mask-discard");
  if (!discard) return false;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (discard.disabled || state.busy || !state.selected) return true;

  const stem = state.selected;
  state.busy = true;
  discard.disabled = true;
  const original = discard.textContent;
  discard.textContent = "Resetting...";
  try {
    const synchronized = await resetMaskSurface(() => resetMask(stem));
    if (state.selected !== stem) return true;
    if (synchronized) {
      await loadMaps();
      if (state.selected !== stem) return true;
      await refreshSelectedData();
    }
    refreshTestMaskSurface();
    toast("The system-detected mask was restored.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy = false;
    if (discard.isConnected) {
      discard.textContent = original;
    }
    if (state.selected === stem) refreshTestMaskSurface();
  }
  return true;
}

function toggleSourcePane() {
  const collapsed = document.body.classList.toggle("test-source-collapsed");
  const button = $("test-source-toggle");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "Expand original map panel" : "Collapse original map panel");
  button.title = collapsed ? "Expand original map panel" : "Collapse original map panel";
}

function reviewWidthBounds() {
  const workspace = $("workspace");
  const width = workspace?.getBoundingClientRect().width || window.innerWidth;
  return {
    min: 320,
    max: Math.max(320, Math.floor(Math.min(width * .62, width - 440))),
  };
}

function setReviewPaneWidth(width) {
  const workspace = $("workspace");
  const handle = $("test-review-resizer");
  if (!workspace || !handle) return;
  const bounds = reviewWidthBounds();
  const next = Math.min(bounds.max, Math.max(bounds.min, Number(width) || bounds.min));
  workspace.style.setProperty("--test-review-width", `${Math.round(next)}px`);
  handle.setAttribute("aria-valuemin", String(bounds.min));
  handle.setAttribute("aria-valuemax", String(bounds.max));
  handle.setAttribute("aria-valuenow", String(Math.round(next)));
}

function bindReviewResizer() {
  const handle = $("test-review-resizer");
  const control = $("control-pane");
  if (!handle || !control) return;

  handle.addEventListener("pointerdown", (event) => {
    if (handle.offsetParent === null) return;
    reviewResize = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: control.getBoundingClientRect().width,
    };
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("is-dragging");
    document.body.classList.add("test-resizing-review");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!reviewResize || reviewResize.pointerId !== event.pointerId) return;
    setReviewPaneWidth(reviewResize.startWidth + reviewResize.startX - event.clientX);
  });
  const finish = (event) => {
    if (!reviewResize || reviewResize.pointerId !== event.pointerId) return;
    reviewResize = null;
    handle.classList.remove("is-dragging");
    document.body.classList.remove("test-resizing-review");
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
  handle.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const bounds = reviewWidthBounds();
    const current = control.getBoundingClientRect().width;
    const amount = event.shiftKey ? 64 : 24;
    const next = event.key === "Home" ? bounds.min
      : event.key === "End" ? bounds.max
        : current + (event.key === "ArrowLeft" ? amount : -amount);
    setReviewPaneWidth(next);
    event.preventDefault();
  });
}

function clearPipelineStartStatus() {
  pipelineStartPending = false;
  if (pipelineStartTimer) window.clearTimeout(pipelineStartTimer);
  pipelineStartTimer = null;
  const status = $("test-start-status");
  if (status?.textContent === "Starting…") status.textContent = "";
}

function settlePipelineStartRequest() {
  pipelineStartTimer = null;
  if (!pipelineStartPending) return;
  if (state.busy || state.continuing) {
    pipelineStartTimer = window.setTimeout(settlePipelineStartRequest, 250);
    return;
  }
  clearPipelineStartStatus();
  syncPipelineChrome();
}

function startPipeline() {
  const button = $("test-pipeline-start");
  const action = $("run-all") || $("continue-run") || $("continue-pipeline");
  if (!action) {
    $("test-start-status").textContent = "The pipeline is not ready yet. Please try again.";
    return;
  }
  if (pipelineStartPending) return;
  pipelineStartPending = true;
  button.disabled = true;
  $("test-start-status").textContent = "Starting…";
  const map = selectedMap();
  if (map?.steps?.["2"] && !map.steps?.["5"]) beginAnalysisPresentation();
  freezeCurrentPreview();
  action.click();
  pipelineStartTimer = window.setTimeout(settlePipelineStartRequest, 50);
}

/* -------------------------------------------------------------- events */

function bindFlowEvents() {
  bindCompletedReviewNavigation({
    updating: () => testPatternUpdating(),
    sync: () => syncPipelineChrome(),
    select: (step) => {
      reopenedProjectReview = null;
      clearStablePreview();
      forcedAnalysisPresentation.active = false;
      state.activeStep = Number(step);
      state.viewStep = String(step);
      state.individualRun = true;
      state.autorun = false;
      stagePresentation.stem = state.selected;
      stagePresentation.step = Number(step);
      stagePresentation.holdUntil = 0;
      renderWorkspace(true);
      syncPipelineChrome();
    },
    finish: () => {
      reopenedProjectReview = null;
      renderWorkspace(true);
      syncPipelineChrome();
    },
  });
  document.addEventListener("click", handleTestPatternClick, true);
  document.addEventListener("scroll", rememberTestReviewScroll, true);
  document.addEventListener("change", (event) => {
    const colour = event.target.closest?.("[data-pattern-colour]");
    if (colour && document.body.classList.contains("test-pipeline-open")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      queueTestPatternColours();
      return;
    }
    const transform = event.target.closest?.('input[name="pattern-transform"]');
    if (transform) {
      event.preventDefault();
      event.stopImmediatePropagation();
      queueTestPatternTransform(transform);
      return;
    }
    const hybridView = event.target.closest?.("[data-colour-view]");
    if (hybridView && document.body.classList.contains("test-pipeline-open")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      void setTestHybridView(hybridView);
    }
  }, true);
  $("test-about-link").addEventListener("click", showAbout);
  $("test-about-home").addEventListener("click", showHome);
  $("test-add-map").addEventListener("click", () => $("test-new-project-input").click());
  $("test-new-project-input").addEventListener("change",
    (event) => createProject(event.target.files?.[0]));
  $("test-template-strip").addEventListener("click", (event) => {
    const template = event.target.closest("[data-template-file]");
    if (template) void createFromTemplate(template.dataset.templateFile);
  });
  $("test-template-strip").addEventListener("scroll", updateTemplateScrollButtons, { passive: true });
  $("test-template-previous").addEventListener("click", () => scrollTemplates(-1));
  $("test-template-next").addEventListener("click", () => scrollTemplates(1));
  $("test-refresh-projects").addEventListener("click", refreshHomeProjects);
  window.addEventListener("resize", updateTemplateScrollButtons);
  $("test-confirm-delete").addEventListener("click", confirmDelete);
  $("test-rename-form").addEventListener("submit", confirmRename);
  $("test-cancel-rename").addEventListener("click", () => $("test-rename-dialog").close());
  $("test-key-form").addEventListener("submit", confirmApiKey);
  $("test-key-dialog").addEventListener("cancel", (event) => event.preventDefault());

  $("test-pipeline-home").addEventListener("click", async () => {
    if (await completedReview.suspend()) showHome();
  });
  $("test-pipeline-delete").addEventListener("click", () => {
    if (state.selected) askToDelete(state.selected);
  });
  $("test-pipeline-title").addEventListener("click", beginPipelineTitleEdit);
  $("test-pipeline-title-input").addEventListener("blur", () => {
    void commitPipelineTitleEdit();
  });
  $("test-pipeline-title-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void commitPipelineTitleEdit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelPipelineTitleEdit();
    }
  });
  $("test-source-toggle").addEventListener("click", toggleSourcePane);
  $("test-pipeline-start").addEventListener("click", startPipeline);
  $("control-content").addEventListener("click", (event) => {
    if (selectTestMaskTool(event)) return;
    if (event.target.closest("[data-edit-pattern], [data-change-pattern]")) {
      window.setTimeout(relocateTestPatternDialog, 0);
    }
    if (event.target.closest("#mask-discard")) {
      void discardTestMaskEdits(event);
      return;
    }
    if (event.target.closest("#mask-undo")) {
      undoTestMaskStroke(event);
      return;
    }
    if (event.target.closest("#braille-all-off")) {
      void toggleTestBrailleText(event);
      return;
    }
    if (completedReview.active) return;
    const analysisAction = event.target.closest("#approve-legend-box, #derive-map-categories");
    if (analysisAction) beginAnalysisPresentation();
    const patternApproval = event.target.closest("#approve-patterns");
    if (patternApproval) freezeCurrentPreview(true);
    else if (analysisAction || event.target.closest(
      "#approve-groups, #apply-preset, #approve-braille",
    )) freezeCurrentPreview();
  }, true);
  $("control-content").addEventListener("change", (event) => {
    const orientation = event.target.closest("#legend-orientation");
    if (!orientation || !state.selected) return;
    const stem = state.selected;
    const expected = orientation.value;
    // The shared save updates the layout before it refreshes the PNG. Rebuild
    // the viewer at that point so its canvas adopts the new aspect ratio and
    // the image is never squeezed into the previous orientation.
    window.setTimeout(async () => {
      try {
        await waitForTestCondition(
          () => state.selected !== stem
            || state.data.legend?.page?.orientation === expected,
          20000,
          "The updated legend orientation could not be displayed.",
        );
        if (state.selected === stem && Number(state.activeStep) === 9) {
          renderTestVisual();
        }
      } catch (error) {
        console.warn("Test UI legend orientation refresh will retry.", error);
      }
    }, 0);
  }, true);
}

function bindMinimalEvents() {
  $("nav-toggle").addEventListener("click",
    () => setNav(!document.body.classList.contains("nav-open")));
  $("nav-backdrop").addEventListener("click", () => setNav(false, true));
  $("nav-add-map").addEventListener("click", () => $("upload-input").click());
  $("upload-input").addEventListener("change", (event) => uploadMap(event.target.files?.[0]));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("nav-open")
        && !document.body.classList.contains("test-flow-open")) setNav(false, true);
  });
}

async function init() {
  renderTemplateGallery();
  bindFlowEvents();
  bindMinimalEvents();
  bindReviewResizer();
  bindTestReviewStabilizer();
  bindTestViewerStabilizer();
  setNav(document.body.classList.contains("nav-open"));
  showFlowScreen("test-home-screen");
  askForApiKey();
  try {
    await Promise.all([loadMaps(), loadModels(), loadSpec()]);
    renderProjectGallery();
    renderWorkspace();
    if (!state.maps.length) setNav(true);
    window.setInterval(syncPipelineChrome, 350);
  } catch (error) {
    $("test-project-status").textContent = error.message;
  }
}

init();
