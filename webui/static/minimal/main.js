"use strict";

import { $ } from "./api.js";
import { setNav, state, toast } from "./state.js";
import { loadMaps, loadModels, loadSpec, renderWorkspace, uploadMap } from "./workspace.js";

/* Entry point.  Everything below the shell is drawn from state, so start-up is
   only: wire the parts of the page that never get redrawn, then load. */

function bindStaticEvents() {
  $("nav-toggle").addEventListener("click",
    () => setNav(!document.body.classList.contains("nav-open")));
  $("nav-backdrop").addEventListener("click", () => setNav(false, true));
  $("nav-add-map").addEventListener("click", () => $("upload-input").click());
  $("upload-input").addEventListener("change", (event) => uploadMap(event.target.files?.[0]));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("nav-open")) setNav(false, true);
  });
}

async function init() {
  bindStaticEvents();
  setNav(document.body.classList.contains("nav-open"));
  try {
    await Promise.all([loadMaps(), loadModels(), loadSpec()]);
    renderWorkspace();  // no map yet: one column, one instruction
    if (!state.maps.length) setNav(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

init();
