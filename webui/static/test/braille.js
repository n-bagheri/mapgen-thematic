/* /test presentation only. Keep the shared label editor and saved layout intact. */

import { state } from "../minimal/state.js";

let observedOverlay = null;
let overlayObserver = null;

export function prepareTestBrailleControls(root) {
  root.querySelectorAll(".braille-delete").forEach((button) => button.remove());
  root.querySelectorAll(".braille-callout").forEach((input) => {
    input.parentElement.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) node.textContent = " Add pin";
    });
  });
}

function showWhiteBoxes(overlay) {
  overlay.querySelectorAll("[data-braille-pin]").forEach((object) => {
    const text = object.querySelector(".braille-rendered-text");
    if (!text || text.querySelector(".braille-white-box")) return;
    const selection = object.querySelector(":scope > .selection-box");
    if (!selection) return;
    // The shared editor's hit rectangle already tracks the label's geometry.
    // Place its opaque copy behind the text, leaving drag targets and pins alone.
    const box = selection.cloneNode(false);
    box.setAttribute("class", "braille-white-box test-braille-white-box");
    box.setAttribute("aria-hidden", "true");
    if (object.hasAttribute("data-braille-pin")) {
      const layout = state.data.braille || {};
      const padding = Number(layout.geometry?.padding_mm || 0) * Number(layout.render_px_per_mm || 5);
      box.setAttribute("x", Number(selection.getAttribute("x") || 0) - padding);
      box.setAttribute("y", Number(selection.getAttribute("y") || 0) - padding);
      box.setAttribute("width", Number(selection.getAttribute("width")) + 2 * padding);
      box.setAttribute("height", Number(selection.getAttribute("height")) + 2 * padding);
    }
    text.prepend(box);
  });
}

export function enhanceTestBrailleOverlay() {
  const overlay = document.getElementById("braille-overlay");
  if (overlay !== observedOverlay) {
    overlayObserver?.disconnect();
    observedOverlay = overlay;
    overlayObserver = overlay ? new MutationObserver(() => showWhiteBoxes(overlay)) : null;
    overlayObserver?.observe(overlay, { childList: true, subtree: true });
  }
  if (overlay) showWhiteBoxes(overlay);
}
