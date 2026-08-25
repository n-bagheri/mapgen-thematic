"use strict";

import { artifactUrl } from "./api.js";

/* Step 5 and Step 6 previews are generated before the reader reaches their
 * decision gates. Keep their small PNG payloads locally for this page session
 * so moving between those steps never replaces a ready map with an empty one. */
const urls = new Map();
const pending = new Map();
const generations = new Map();

const keyFor = (stem, name) => `${stem}/${name}`;
const generationFor = (stem) => generations.get(stem) || 0;

export function cachedPreviewUrl(stem, name) {
  return urls.get(keyFor(stem, name)) || artifactUrl(stem, name);
}

async function preloadPreview(stem, name) {
  const key = keyFor(stem, name);
  if (urls.has(key)) return urls.get(key);
  if (pending.has(key)) return pending.get(key);
  const generation = generationFor(stem);
  const request = fetch(artifactUrl(stem, name), { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`Preview request failed (${response.status})`);
      const url = URL.createObjectURL(await response.blob());
      if (generation !== generationFor(stem)) {
        URL.revokeObjectURL(url);
        return null;
      }
      urls.set(key, url);
      return url;
    })
    .catch(() => null)
    .finally(() => { pending.delete(key); });
  pending.set(key, request);
  return request;
}

export async function preloadPreviews(stem, names) {
  return Promise.all([...new Set(names.filter(Boolean))].map((name) => preloadPreview(stem, name)));
}

/** A pipeline rerun can overwrite an artifact under the same filename. */
export function clearPreviewCache(stem = null) {
  const stems = stem ? [stem] : [...new Set([...urls.keys(), ...pending.keys()]
    .map((key) => key.slice(0, key.indexOf("/"))))];
  stems.forEach((item) => generations.set(item, generationFor(item) + 1));
  [...urls.entries()].forEach(([key, url]) => {
    if (stem && !key.startsWith(`${stem}/`)) return;
    URL.revokeObjectURL(url);
    urls.delete(key);
  });
}
