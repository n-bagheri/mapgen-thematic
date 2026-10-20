"""Repair confirmed, haloed region lettering before colour segmentation.

The existing removal policy is retained for other label styles. This narrow
repair handles dark lettering with a pale halo on chromatic isopleth fills,
where both the letters and the halo otherwise become spurious area classes.
"""

from __future__ import annotations

import cv2
import numpy as np

from .isolate import to_lab
from .labelreview import occurrence_id, removal_signature


def fill_region_label_halos(image: np.ndarray, geographic_mask: np.ndarray,
                           labels_json: dict, classes_json: dict, sem,
                           review_json: dict | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Return cleaned colours, geography, repaired pixels, and occurrence count.

    No map names are special-cased. Changes are confined to confirmed text
    windows and small detached ink-only fragments without any thematic fill.
    Copies and distance fields are only allocated after a confirmed halo-style
    label has been identified.
    """
    repaired = np.zeros(image.shape[:2], np.uint8)
    if sem.map_type.value != "isopleth":
        return image, geographic_mask, repaired, 0
    palette = np.asarray([cl["lab"] for cl in classes_json.get("classes", [])
                          if cl.get("is_thematic") and cl.get("lab") is not None], np.float32)
    # Muted/neutral legend fills can be real geography; restrict this repair
    # to strongly chromatic palettes rather than reinterpret those as halos.
    if not len(palette) or np.any(np.linalg.norm(palette[:, 1:], axis=1) < 45):
        return image, geographic_mask, repaired, 0

    labels = labels_json.get("labels", [])
    reviewed = removal_signature(review_json, labels_json) is not None
    decisions = {entry["id"]: bool(entry.get("remove", True))
                 for entry in (review_json or {}).get("decisions", [])} if reviewed else {}
    height, width = image.shape[:2]
    regions = []
    extras = []
    protected = []
    for index, label in enumerate(labels):
        box = label.get("box", [])
        if len(box) != 4:
            continue
        x0, y0, x1, y1 = [int(round(value)) for value in box]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        pad = max(2, int(round((y1 - y0) * 0.15)))
        padded = (max(0, x0 - pad), max(0, y0 - pad),
                  min(width, x1 + pad), min(height, y1 + pad))
        if reviewed and not decisions.get(occurrence_id(label, index), False):
            protected.append(padded)
            continue
        recognized = str(label.get("easyocr_text") or label.get("text") or "")
        confirmed = label.get("recognition_status") == "text-confirmed" or (
            label.get("localization") in {"craft", "craft-only"}
            and float(label.get("easyocr_conf") or 0) >= 0.8)
        if not confirmed or sum(char.isalnum() for char in recognized) < 3:
            continue
        if label.get("kind") != "region_label" and not (
                label.get("kind") == "other" and label.get("localization") == "craft-only"):
            continue
        core = image[y0:y1, x0:x1].astype(np.int16)
        pale = (core.min(axis=2) >= 170) & (np.ptp(core, axis=2) <= 45)
        dark = core.max(axis=2) <= 110
        if pale.mean() < 0.05 or dark.mean() < 0.02:
            continue
        if label.get("kind") == "region_label":
            regions.append(padded)
        else:
            extras.append(padded)
    if not regions:
        return image, geographic_mask, repaired, 0
    # CRAFT-only occurrences on the same haloed region-label map may have no
    # model classification. Their high-confidence OCR still confirms lettering.
    boxes = regions + extras
    for x0, y0, x1, y1 in boxes:
        lab = to_lab(image[y0:y1, x0:x1])
        distance = np.full(lab.shape[:2], np.inf, np.float32)
        for colour in palette:
            np.minimum(distance, np.linalg.norm(lab - colour, axis=2), out=distance)
        # Preserve the original thematic pixels even inside the OCR window.
        # Reconstruct just the lettering/halo and its one-pixel antialias fringe,
        # rather than stamping a rectangular patch across a class boundary.
        candidate = cv2.dilate((distance >= 16).astype(np.uint8), np.ones((3, 3), np.uint8))
        repaired[y0:y1, x0:x1] |= candidate * 255
    for x0, y0, x1, y1 in protected:
        repaired[y0:y1, x0:x1] = 0
    if not repaired.any():
        return image, geographic_mask, repaired, 0

    # White halos can have cut holes into the automatic geographic mask, and
    # dark letters can protrude into its page background. Reconstruct ONLY the
    # confirmed text windows from surrounding geography so neither survives as
    # a letter-shaped cutout at a coastline. Preserve all other mask pixels.
    inferred = cv2.inpaint(geographic_mask, repaired, 3, cv2.INPAINT_TELEA)
    clean_geography = geographic_mask.copy()
    clean_geography[repaired > 0] = (inferred[repaired > 0] >= 128).astype(np.uint8) * 255
    # Match Step 4's existing interior-hole policy within the repaired pixels;
    # interpolation must not replace a white letter by a small enclosed hole.
    flooded = cv2.copyMakeBorder(clean_geography, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    cv2.floodFill(flooded, np.zeros((height + 4, width + 4), np.uint8), (0, 0), 255)
    interior = cv2.bitwise_not(flooded[1:-1, 1:-1])
    clean_geography[repaired > 0] |= interior[repaired > 0]
    cleaned = image.copy()
    cleaned[(repaired > 0) & (clean_geography == 0)] = 255
    for x0, y0, x1, y1 in boxes:
        # Use a surrounding window, excluding every label slated for removal.
        # Only pixels matching a known thematic colour may donate their colour:
        # pale halos, black borders and page background cannot be propagated.
        pad = max(8, (y1 - y0) * 2)
        rx0, ry0 = max(0, x0 - pad), max(0, y0 - pad)
        rx1, ry1 = min(width, x1 + pad), min(height, y1 + pad)
        excluded = repaired[ry0:ry1, rx0:rx1] > 0
        target = excluded & (clean_geography[ry0:ry1, rx0:rx1] > 0)
        if not target.any():
            continue
        source = image[ry0:ry1, rx0:rx1]
        lab = to_lab(source)
        distance = np.full(lab.shape[:2], np.inf, np.float32)
        for colour in palette:
            np.minimum(distance, np.linalg.norm(lab - colour, axis=2), out=distance)
        donors = (distance < 16) & ~excluded & (geographic_mask[ry0:ry1, rx0:rx1] > 0)
        if not donors.any():
            raise ValueError("Cannot remove a confirmed region label: no surrounding thematic fill found")
        _, nearest = cv2.distanceTransformWithLabels(
            (~donors).astype(np.uint8), cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
        donor_colours = source[donors]
        # Each rectangle is filled from the original surrounding pixels, never
        # from another rectangle's reconstruction.
        local_target = np.zeros(target.shape, bool)
        local_target[y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0] = True
        local_target &= target
        destination = cleaned[ry0:ry1, rx0:rx1]
        destination[local_target] = donor_colours[nearest[local_target] - 1]

    # Very short OCR fragments can be dropped from labels.json (one-letter
    # coordinate/furniture text on the page). In this same halo-repair path,
    # remove only small detached ink-only components. A genuine mapped island
    # with even one thematic-colour pixel is retained; reviewed keep boxes win.
    count, components, stats, _ = cv2.connectedComponentsWithStats(clean_geography, connectivity=8)
    largest = int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) + 1 if count > 1 else -1
    limit = max(64, int(0.001 * np.count_nonzero(clean_geography)))
    for index in range(1, count):
        x, y, cw, ch, area = stats[index]
        if index == largest or area > limit:
            continue
        if any(x < px1 and x + cw > px0 and y < py1 and y + ch > py0
               for px0, py0, px1, py1 in protected):
            continue
        component = components[y:y + ch, x:x + cw] == index
        colours = to_lab(image[y:y + ch, x:x + cw])[component]
        if any(np.any(np.linalg.norm(colours - colour, axis=1) < 16) for colour in palette):
            continue
        clean_geography[y:y + ch, x:x + cw][component] = 0
        cleaned[y:y + ch, x:x + cw][component] = 255
        repaired[y:y + ch, x:x + cw][component] = 255
    return cleaned, clean_geography, repaired, len(boxes)
