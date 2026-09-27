"use strict";

import {
  $, deleteMap, esc, getJob, mapUrl, renameMap, resetMask, uploadFile,
} from "../minimal/api.js";
import { isRunning, selectedMap, setNav, state, toast } from "../minimal/state.js";
import { completedCount, STEP_DEFS, STEP_VIEWS } from "../minimal/steps.js";
import { renderVisual } from "../minimal/visual.js";
import { renderControls } from "../minimal/controls.js";
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
  { file: "Russia_districts.jpg", label: "Russia districts" },
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
  "1": "Taking a closer look with a vision-language model…",
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
const stagePresentation = { stem: null, step: 0, holdUntil: 0 };
const previewPresentation = { stem: null, step: 0, pendingUntil: 0 };
const forcedAnalysisPresentation = { active: false, index: 0, holdUntil: 0 };
let finishedJobRefresh = null;
let finishedJobRefreshAt = 0;
let reviewResize = null;

const DELETE_ICON = `<svg viewBox="0 0 32 32" aria-hidden="true">
  <path d="M7.5 9.5h17M12 9.5V6.8h8v2.7M10 12.5l1 13h10l1-13M14 14.5v8M18 14.5v8"></path></svg>`;

/* ---------------------------------------------------------- navigation */

function leavePipelineShell() {
  clearStablePreview();
  document.body.classList.remove(
    "test-pipeline-open", "test-source-collapsed", "test-job-running",
    "test-review-active", "test-job-failed", "test-export-ready", "test-show-controls",
    "test-preview-frozen",
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
  renderProjectGallery();
}

function showAbout() {
  showFlowScreen("test-about-screen");
}

async function enterPipeline(stem) {
  if (stem && state.selected !== stem) await selectMap(stem);
  state.individualRun = false;
  resetStagePresentation();
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

function pipelineReviewStep(map) {
  if (!map) return null;
  if (map.steps?.["2"] && !map.steps?.["3"]
      && (!state.data.mask?.approved || !state.data.legendReview?.approved)) return "2";
  if (map.steps?.["5"] && !map.step5_review_ready) return "5";
  if (map.steps?.["6"] && !map.steps?.["7"] && !map.step6_review_ready) return "6";
  if (map.steps?.["7"] && !map.steps?.["8"] && !map.step7_review_ready) return "7";
  if (map.steps?.["8"] && !map.steps?.["9"] && !map.step8_review_ready) return "8";
  if (map.steps?.["9"] && !map.step9_review_ready) return "9";
  return null;
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
}

function applyTestVisualPreferences() {
  state.layers = {
    ...state.layers,
    map: true,
    labels: false,
    lines: false,
    boundaries: false,
  };
  Object.values(STEP_VIEWS).flat().forEach((view) => {
    view.originalCompare = true;
  });
}

function setToolbarLabel(label, text) {
  const textNode = Array.from(label.childNodes).filter((node) => node.nodeType === 3).at(-1);
  if (textNode) textNode.textContent = ` ${text}`;
}

function enhanceTestViewers() {
  document.querySelectorAll("#visual-content .page-view-toolbar").forEach((toolbar) => {
    if (toolbar.dataset.testEnhanced === "true") return;
    toolbar.dataset.testEnhanced = "true";

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
      colours.innerHTML = '<input type="checkbox" disabled><span aria-hidden="true"></span> Display colors';
    } else {
      setToolbarLabel(colours, "Display colors");
    }

    const original = document.createElement("label");
    original.className = "page-grid-toggle";
    original.innerHTML = `<input type="checkbox" data-test-original-view
      ${state.showOriginalMap ? "checked" : ""}><span aria-hidden="true"></span> Display original map`;
    original.querySelector("input").addEventListener("change", (event) => {
      state.showOriginalMap = event.currentTarget.checked;
      if (Number(state.activeStep) === 9) state.showFinalMap = state.showOriginalMap;
      renderTestVisual();
    });

    const fullSize = toolbar.querySelector(".page-full-size");
    if (fullSize) fullSize.textContent = "Open full size ↗";
    toolbar.querySelector(".page-zoom-readout")?.remove();
    [
      toolbar.querySelector(".page-pan-toggle"),
      toolbar.querySelector(".page-zoom-out"),
      toolbar.querySelector(".page-zoom-range"),
      toolbar.querySelector(".page-zoom-in"),
      toolbar.querySelector(".page-zoom-100"),
      toolbar.querySelector(".page-zoom-fit"),
      fullSize, guides, snap, colours, original,
    ].filter(Boolean).forEach((control) => toolbar.appendChild(control));
  });
}

function enhanceTestReviewDock() {
  const decision = document.querySelector("#control-content .mask-decision");
  if (!decision) return;

  const intro = decision.querySelector(".section-intro");
  if (intro) {
    intro.textContent = "Use the brush to restore map areas that were mistakenly masked, and the eraser to remove unwanted areas.";
  }
  const title = decision.querySelector("#mask-decision-title");
  if (title) title.textContent = "Review the detected mask";
  decision.querySelector(".section-kicker")?.remove();

  if (decision.dataset.testLayout !== "true") {
    const copy = document.createElement("div");
    copy.className = "test-mask-copy";
    [title, intro]
      .filter(Boolean).forEach((node) => copy.appendChild(node));

    const tools = document.createElement("div");
    tools.className = "test-mask-tools";
    [decision.querySelector(".mask-decision-modes"), decision.querySelector(".mask-decision-brush"),
      decision.querySelector(".mask-decision-status")]
      .filter(Boolean).forEach((node) => tools.appendChild(node));

    const actions = document.createElement("div");
    actions.className = "test-mask-action-column";
    [decision.querySelector(".mask-decision-actions"), decision.querySelector(".approve-mask")]
      .filter(Boolean).forEach((node) => actions.appendChild(node));

    decision.replaceChildren(copy, tools, actions);
    decision.dataset.testLayout = "true";
  }

  const discard = decision.querySelector("#mask-discard");
  const canRestoreAutomatic = Boolean(
    state.maskBrush.strokes.length || state.data.mask?.reviewed,
  );
  if (discard) {
    discard.disabled = !canRestoreAutomatic;
    discard.title = canRestoreAutomatic
      ? "Discard all edits and restore the system-detected mask"
      : "There are no mask edits to discard";
  }
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
    const backendReady = !live && reviewStep === "5";
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
  const copies = Array.from(visual.childNodes, (node) => node.cloneNode(true));
  stable.replaceChildren(...copies);
  stable.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
  const sourceCanvases = visual.querySelectorAll("canvas");
  stable.querySelectorAll("canvas").forEach((canvas, index) => {
    const source = sourceCanvases[index];
    if (!source || !source.width || !source.height) return;
    try {
      canvas.getContext("2d")?.drawImage(source, 0, 0);
    } catch { /* A preview image can be cross-origin restricted; its clone still renders. */ }
  });
  stable.hidden = false;
}

function freezeCurrentPreview() {
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
  state.viewStep = TEST_PREVIEW_PIN;
  state.activeStep = currentStep;
  document.body.classList.add("test-preview-frozen");
}

function stablePreviewStep(map, visibleStep) {
  const limit = Math.max(1, visibleStep - 1);
  return STEP_DEFS.reduce((latest, step) => {
    const number = Number(step.key);
    return number <= limit && map.steps?.[step.key] ? number : latest;
  }, 1);
}

/* Keep a static copy over the live visual pane while the backend is polling.
   The hidden live pane may redraw as often as needed without flashing or
   replacing the last completed result that the user is looking at. */
function syncStablePreview(map, visibleStep, processing, reviewStep) {
  const visual = $("visual-content");
  const stable = $("test-stable-preview");
  if (!map || !visual || !stable) return;
  if (processing) {
    const desiredStep = stablePreviewStep(map, visibleStep);
    const changed = previewPresentation.stem !== map.stem
      || previewPresentation.step !== desiredStep || stable.hidden;
    state.viewStep = TEST_PREVIEW_PIN;
    state.activeStep = desiredStep;
    previewPresentation.pendingUntil = 0;
    document.body.classList.add("test-preview-frozen");
    if (changed) {
      renderTestVisual();
      copyStablePreview();
      previewPresentation.stem = map.stem;
      previewPresentation.step = desiredStep;
    }
    return;
  }
  const wasFrozen = document.body.classList.contains("test-preview-frozen");
  if (wasFrozen && Date.now() < previewPresentation.pendingUntil) return;
  if (state.viewStep === TEST_PREVIEW_PIN) state.viewStep = null;
  // Reveal the live pane before asking it to render the review. If a viewer
  // enhancement fails, the old frozen Step 2 clone must not remain on top.
  document.body.classList.remove("test-preview-frozen");
  stable.hidden = true;
  stable.replaceChildren();
  previewPresentation.stem = null;
  previewPresentation.step = 0;
  previewPresentation.pendingUntil = 0;
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
  const current = content.querySelector(
    `.step-section[data-step="${reviewStep}"][open] .review-gate`,
  );
  if (current) return;
  state.viewStep = null;
  state.activeStep = Number(reviewStep);
  content.hidden = false;
  renderControls();
}

function renderPipelineProgress(map, reviewStep, runningStep, visibleStep, failedStep) {
  const progress = $("test-pipeline-progress");
  const visibleCompleted = STEP_DEFS.filter(
    (step) => Number(step.key) <= visibleStep && map?.steps?.[step.key],
  ).length;
  progress.setAttribute("aria-valuenow", String(visibleCompleted));
  const segments = STEP_DEFS.map((step) => {
    const isVisible = Number(step.key) <= visibleStep;
    const className = step.key === failedStep ? "failed"
      : !isVisible ? ""
        : step.key === reviewStep ? "waiting-review"
          : step.key === runningStep ? "running"
            : map?.steps?.[step.key] ? "done" : "";
    return { step, className };
  });
  const signature = segments.map(({ className }) => className || "idle").join("|");
  if (progress.dataset.signature === signature) return;
  progress.dataset.signature = signature;
  progress.innerHTML = segments.map(({ step, className }) =>
    `<span class="test-progress-step ${className}"
      title="${esc(THINKING_MESSAGES[step.key])}">
      <span class="test-progress-node" aria-hidden="true">${className === "done" ? "✓" : step.key}</span>
      <span class="test-progress-label">${esc(STEP_LABELS[step.key])}</span>
    </span>`).join("");
}

/* The shared poll asks for /api/job and /api/maps concurrently. At the end of
   any job, an older maps response can arrive beside a newer "done" response.
   /test therefore performs an ordered watchdog read for every pipeline batch:
   job first, maps second. Once the job is done, that map read cannot be older
   than the completion it is describing. */
function reconcileFinishedJobSnapshot(map, job) {
  const requested = (job?.steps || []).map(String);
  const highestRequested = requested.reduce(
    (highest, step) => Math.max(highest, Number(step) || 0), 0,
  );
  const snapshotMissing = requested.some((step) => !map?.steps?.[step]);
  const presentationBehind = highestRequested > Number(stagePresentation.step || 0);
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
    if (state.selected !== stem) return;
    state.job = authoritativeJob || { status: "idle" };
    // While the job is live, the normal workspace poll remains authoritative.
    // The ordered map refresh is needed only after that job has stopped.
    if (state.job.status === "running") return;
    await loadMaps();
    if (state.selected !== stem) return;
    await refreshSelectedData();
    if (state.selected !== stem) return;
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
  const map = selectedMap();
  const live = Boolean(map && isRunning(map));
  const job = state.job || map?.job || { status: "idle" };
  reconcileFinishedJobSnapshot(map, job);
  if (job.status === "failed") forcedAnalysisPresentation.active = false;
  const fallbackStep = map ? STEP_DEFS.find((step) => !map.steps?.[step.key])?.key : "";
  const actualReviewStep = live ? null : pipelineReviewStep(map);
  const targetStep = actualPipelineStage(map, live, job, actualReviewStep)
    || Number(fallbackStep) || 0;
  const presentation = presentedPipelineStage(map, targetStep, live, actualReviewStep);
  const failed = job.status === "failed";
  const presentationBusy = Boolean(!failed && (live || presentation.catchingUp));
  const runningStep = presentationBusy && presentation.step ? String(presentation.step) : null;
  const reviewStep = presentation.catchingUp ? null : actualReviewStep;
  const completed = completedCount(map);
  const exportReady = Boolean(map?.steps?.["9"] && map.step9_review_ready);
  const failedStep = failed
    ? (job.steps || []).map(String).find((step) => !map?.steps?.[step])
      || String(presentation.step || targetStep || "")
    : null;
  const blocked = Boolean(map?.pipeline_error || map?.step1_error || map?.in_scope === false);
  const showControls = Boolean(reviewStep || exportReady || failed || blocked);
  const canStart = Boolean(map && !presentationBusy && !actualReviewStep
    && !exportReady && !failed && !blocked
    && (completed === 0 || !state.autorun));

  applyTestVisualPreferences();
  enhanceTestViewers();

  $("test-pipeline-title").textContent = map?.name || "New project";
  $("test-pipeline-delete").disabled = !map || presentationBusy;
  $("test-pipeline-rename").disabled = !map || presentationBusy;
  $("test-pipeline-start").textContent = completed ? "Continue" : "Start";
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

  $("test-start-panel").hidden = !canStart;
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
  enhanceTestReviewDock();

  const thinking = $("test-thinking");
  const thinkingMessage = $("test-thinking-message");
  if (failed) {
    thinking.hidden = false;
    thinkingMessage.textContent = job.error || "This step could not finish.";
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
  } else {
    thinking.hidden = true;
  }

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
  if (discard.disabled || !state.selected) return true;

  discard.disabled = true;
  const original = discard.textContent;
  discard.textContent = "Discarding...";
  try {
    state.maskBrush.strokes = [];
    await resetMask(state.selected);
    await loadMaps();
    await refreshSelectedData();
    renderWorkspace(true);
    toast("All mask edits were discarded. The system-detected mask was restored.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (discard.isConnected) {
      discard.disabled = false;
      discard.textContent = original;
    }
  }
  return true;
}

function reviewHeightBounds() {
  const workspace = $("workspace");
  const height = workspace?.getBoundingClientRect().height || window.innerHeight;
  return { min: 150, max: Math.max(180, height - 260) };
}

function setReviewDockHeight(height) {
  const bounds = reviewHeightBounds();
  const next = Math.min(bounds.max, Math.max(bounds.min, height));
  $("workspace")?.style.setProperty("--test-review-height", `${Math.round(next)}px`);
}

function bindReviewResizer() {
  const handle = $("test-review-resizer");
  const pane = $("control-pane");
  handle.addEventListener("pointerdown", (event) => {
    if (handle.offsetParent === null) return;
    reviewResize = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startHeight: pane.getBoundingClientRect().height,
    };
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("is-dragging");
    document.body.classList.add("test-resizing-review");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!reviewResize || reviewResize.pointerId !== event.pointerId) return;
    setReviewDockHeight(reviewResize.startHeight + reviewResize.startY - event.clientY);
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
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    const amount = event.shiftKey ? 40 : 16;
    const current = pane.getBoundingClientRect().height;
    setReviewDockHeight(current + (event.key === "ArrowUp" ? amount : -amount));
    event.preventDefault();
  });
}

function toggleSourcePane() {
  const collapsed = document.body.classList.toggle("test-source-collapsed");
  const button = $("test-source-toggle");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "Expand original map panel" : "Collapse original map panel");
  button.title = collapsed ? "Expand original map panel" : "Collapse original map panel";
}

function startPipeline() {
  const button = $("test-pipeline-start");
  const action = $("run-all") || $("continue-run") || $("continue-pipeline");
  if (!action) {
    $("test-start-status").textContent = "The pipeline is not ready yet. Please try again.";
    return;
  }
  button.disabled = true;
  $("test-start-status").textContent = "Starting…";
  const map = selectedMap();
  if (map?.steps?.["2"] && !map.steps?.["5"]) beginAnalysisPresentation();
  freezeCurrentPreview();
  action.click();
  window.setTimeout(() => {
    button.disabled = false;
    syncPipelineChrome();
  }, 600);
}

/* -------------------------------------------------------------- events */

function bindFlowEvents() {
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

  $("test-pipeline-home").addEventListener("click", showHome);
  $("test-pipeline-back").addEventListener("click", showHome);
  $("test-pipeline-delete").addEventListener("click", () => {
    if (state.selected) askToDelete(state.selected);
  });
  $("test-pipeline-rename").addEventListener("click", () => {
    if (state.selected) askToRename(state.selected);
  });
  $("test-source-toggle").addEventListener("click", toggleSourcePane);
  $("test-pipeline-start").addEventListener("click", startPipeline);
  $("control-content").addEventListener("click", (event) => {
    if (event.target.closest("#mask-discard")) {
      void discardTestMaskEdits(event);
      return;
    }
    const analysisAction = event.target.closest("#approve-legend-box, #derive-map-categories");
    if (analysisAction) beginAnalysisPresentation();
    if (analysisAction || event.target.closest(
      "#approve-groups, #apply-preset, #approve-patterns, #approve-braille",
    )) freezeCurrentPreview();
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
