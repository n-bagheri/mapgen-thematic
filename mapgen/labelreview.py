"""Human review layer for detected overlay-text occurrences.

``labels.json`` remains the immutable detector output.  Review decisions are
stored separately and materialized as ``approved_labels.json`` for later
steps.  This distinction is important: repeated names can be legitimate, so
review operates on physical occurrences (text + box), not unique words.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


REVIEW_VERSION = 2


def normalized_text(value: str) -> str:
    """Loose name key used only to display likely repeated readings."""
    decomposed = unicodedata.normalize("NFKD", str(value))
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_marks.casefold()).strip()


def labels_fingerprint(labels_json: dict) -> str:
    """Fingerprint the exact occurrence list that a review applies to."""
    payload = json.dumps(
        labels_json.get("labels", []), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def occurrence_id(label: dict, index: int) -> str:
    identity = json.dumps({
        "index": index,
        "kind": label.get("kind"),
        "text": label.get("text"),
        "box": label.get("box"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"label-{index}-{hashlib.sha1(identity).hexdigest()[:10]}"


def review_view(labels_json: dict, review_json: dict | None = None) -> dict:
    """Return detector occurrences plus valid saved review decisions."""
    labels = labels_json.get("labels", [])
    fingerprint = labels_fingerprint(labels_json)
    saved = bool(
        review_json
        and review_json.get("version") == REVIEW_VERSION
        and review_json.get("labels_fingerprint") == fingerprint
    )
    decisions = {
        d.get("id"): d for d in (review_json or {}).get("decisions", [])
    } if saved else {}

    keys = [normalized_text(label.get("text", "")) for label in labels]
    counts = {key: keys.count(key) for key in set(keys) if key}
    occurrences = []
    for index, label in enumerate(labels):
        item_id = occurrence_id(label, index)
        decision = decisions.get(item_id)
        duplicate_count = counts.get(keys[index], 0)
        unconfirmed = (
            label.get("recognition_status") in {"gemini-only", "geometry-only"}
            or (
                label.get("recognition_status") is None
                and label.get("localization") in {"gemini", "gemini-unverified"}
            )
        )
        occurrences.append({
            "id": item_id,
            "index": index,
            "original_text": label.get("text", ""),
            "review_text": decision.get("text", label.get("text", "")) if decision else label.get("text", ""),
            "include": bool(decision.get("include", True)) if decision else True,
            "remove": bool(decision.get("remove", True)) if decision else True,
            "reviewed": decision is not None,
            "needs_review": unconfirmed or duplicate_count > 1,
            "duplicate_key": keys[index],
            "duplicate_count": duplicate_count,
            "label": label,
        })
    return {
        "version": REVIEW_VERSION,
        "labels_fingerprint": fingerprint,
        "saved": saved,
        "saved_at": review_json.get("saved_at") if saved else None,
        "occurrences": occurrences,
    }


def apply_review(labels_json: dict, decisions: list[dict]) -> tuple[dict, dict]:
    """Validate decisions and build review + approved-label artifacts."""
    labels = labels_json.get("labels", [])
    expected = {occurrence_id(label, i): (i, label) for i, label in enumerate(labels)}
    supplied: dict[str, dict] = {}
    for decision in decisions:
        item_id = str(decision.get("id", ""))
        if item_id not in expected or item_id in supplied:
            raise ValueError("review contains an unknown or repeated label occurrence")
        text = str(decision.get("text", "")).strip()
        include = bool(decision.get("include", False))
        remove = bool(decision.get("remove", True))
        if include and not text:
            raise ValueError("included labels must have non-empty text")
        if len(text) > 200:
            raise ValueError("reviewed text must be 200 characters or fewer")
        supplied[item_id] = {
            "id": item_id, "text": text, "include": include, "remove": remove,
        }
    if set(supplied) != set(expected):
        raise ValueError("review must contain one decision for every detected occurrence")

    saved_at = datetime.now(timezone.utc).isoformat()
    ordered_decisions = []
    approved = []
    for item_id, (index, label) in expected.items():
        decision = supplied[item_id]
        ordered_decisions.append({
            "id": item_id,
            "index": index,
            "original_text": label.get("text", ""),
            "text": decision["text"],
            "include": decision["include"],
            "remove": decision["remove"],
        })
        if decision["include"]:
            reviewed_label = deepcopy(label)
            reviewed_label["original_text"] = label.get("text", "")
            reviewed_label["text"] = decision["text"]
            reviewed_label["review_id"] = item_id
            reviewed_label["review_status"] = "approved"
            approved.append(reviewed_label)

    fingerprint = labels_fingerprint(labels_json)
    review = {
        "version": REVIEW_VERSION,
        "labels_fingerprint": fingerprint,
        "saved_at": saved_at,
        "decisions": ordered_decisions,
    }
    approved_json = {key: deepcopy(value) for key, value in labels_json.items() if key != "labels"}
    approved_json["labels"] = approved
    approved_json["review"] = {
        "version": REVIEW_VERSION,
        "labels_fingerprint": fingerprint,
        "saved_at": saved_at,
        "approved": len(approved),
        "excluded": len(labels) - len(approved),
        "source": "label_review.json",
    }
    return review, approved_json


def removal_signature(review_json: dict | None, labels_json: dict) -> tuple | None:
    """Comparable removal choices, or None when no current review exists."""
    if (not review_json or review_json.get("version") != REVIEW_VERSION
            or review_json.get("labels_fingerprint") != labels_fingerprint(labels_json)):
        return None
    return tuple(
        (str(decision.get("id", "")), bool(decision.get("remove", True)))
        for decision in review_json.get("decisions", [])
    )


def build_text_removal_mask(labels_json: dict, stroke_mask: np.ndarray,
                            review_json: dict | None = None) -> tuple[np.ndarray, dict]:
    """Build the exact dilated mask Step 4 must exclude before segmentation.

    Without a saved review, retain the legacy conservative behavior: precise
    strokes are removed, and unresolved compact city/capital boxes are filled.
    Once reviewed, every occurrence explicitly marked ``remove`` is removed;
    unresolved occurrences use their detected box as the safe, visible fallback.
    """
    import cv2

    labels = labels_json.get("labels", [])
    current_signature = removal_signature(review_json, labels_json)
    reviewed = current_signature is not None
    decisions = {
        d.get("id"): bool(d.get("remove", True))
        for d in (review_json or {}).get("decisions", [])
    } if reviewed else {}
    height, width = stroke_mask.shape[:2]
    result = np.zeros((height, width), np.uint8)
    precise = box_fallback = kept = 0

    for index, label in enumerate(labels):
        item_id = occurrence_id(label, index)
        remove = decisions.get(item_id, False) if reviewed else (
            bool(label.get("mask_found")) or label.get("kind") in {"city", "capital"}
        )
        if not remove:
            kept += 1
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in label["box"]]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 <= x0 or y1 <= y0:
            kept += 1
            continue
        if label.get("mask_found"):
            pad = max(6, int(0.6 * (y1 - y0)))
            px0, py0 = max(0, x0 - pad), max(0, y0 - pad)
            px1, py1 = min(width, x1 + pad), min(height, y1 + pad)
            result[py0:py1, px0:px1] |= stroke_mask[py0:py1, px0:px1]
            precise += 1
        else:
            result[y0:y1, x0:x1] = 255
            box_fallback += 1

    # Letter antialiasing extends beyond both exact strokes and detected boxes.
    result = cv2.dilate(result, np.ones((5, 5), np.uint8))
    return result, {
        "mode": "reviewed" if reviewed else "automatic-safe",
        "precise_stroke_labels": precise,
        "whole_box_labels": box_fallback,
        "kept_labels": kept,
        "removed_pixels": int(np.count_nonzero(result)),
    }


def write_text_removal_mask(run_dir: Path) -> tuple[np.ndarray, dict]:
    """Materialize the exact Step 4 removal mask from current review state."""
    from .isolate import imread, imwrite

    labels_json = json.loads((run_dir / "labels.json").read_text(encoding="utf-8"))
    stroke_mask = imread(run_dir / "text_mask.png")[..., 0]
    review_path = run_dir / "label_review.json"
    review_json = (json.loads(review_path.read_text(encoding="utf-8"))
                   if review_path.exists() else None)
    mask, metadata = build_text_removal_mask(labels_json, stroke_mask, review_json)
    imwrite(run_dir / "text_removal_mask.png", mask)
    (run_dir / "text_removal_mask.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8",
    )
    return mask, metadata
