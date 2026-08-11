"""Occurrence-level review for Step 4 river centerlines.

``lines_auto.geojson`` is immutable automatic output for a Step 4 run.
Review decisions live in ``line_review.json`` and are materialized into both
``approved_lines.geojson`` and the authoritative downstream ``lines.geojson``.
Coastlines/borders are never editable here; the review affects rivers only.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone

import numpy as np


REVIEW_VERSION = 2
MANUAL_ID = re.compile(r"^manual-[A-Za-z0-9_-]{1,80}$")


def auto_lines_fingerprint(lines_json: dict) -> str:
    payload = json.dumps(
        lines_json.get("features", []), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def automatic_feature_id(feature: dict, index: int) -> str:
    identity = json.dumps({
        "index": index,
        "properties": feature.get("properties", {}),
        "geometry": feature.get("geometry", {}),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"river-{index}-{hashlib.sha1(identity).hexdigest()[:10]}"


def _automatic_rivers(lines_json: dict) -> list[tuple[str, dict]]:
    return [
        (automatic_feature_id(feature, index), feature)
        for index, feature in enumerate(lines_json.get("features", []))
        if feature.get("properties", {}).get("kind") == "river"
    ]


def review_is_current(lines_json: dict, review_json: dict | None) -> bool:
    return bool(
        review_json
        and review_json.get("version") == REVIEW_VERSION
        and review_json.get("auto_lines_fingerprint") == auto_lines_fingerprint(lines_json)
    )


def _manual_feature(item: dict) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "kind": "river",
            "source_class": "reviewed river path",
            "source": "manual_review",
            "confidence": "reviewed",
            "label_evidence": [item["label"]] if item.get("label") else [],
            "review_id": item["id"],
            "edit_kind": item.get("edit_kind", "drawn"),
            "connections": deepcopy(item.get("connections", {})),
        },
        "geometry": {"type": "LineString", "coordinates": deepcopy(item["points"])},
    }


def materialize_review(lines_json: dict, review_json: dict | None) -> dict:
    """Return the authoritative collection, applying only a current review."""
    if not review_is_current(lines_json, review_json):
        return deepcopy(lines_json)
    include_rivers = bool(review_json.get("include_rivers", True))
    include_ids = set(review_json.get("include_auto_ids", []))
    auto_ids = dict(_automatic_rivers(lines_json))
    features = []
    for feature in lines_json.get("features", []):
        if feature.get("properties", {}).get("kind") != "river":
            features.append(deepcopy(feature))
    connections_by_target: dict[str, list[dict]] = {}
    for manual in review_json.get("manual_rivers", []):
        for connection in manual.get("connections", {}).values():
            connections_by_target.setdefault(connection["target_id"], []).append(connection)
    for item_id, feature in auto_ids.items():
        if include_rivers and item_id in include_ids:
            kept = deepcopy(feature)
            kept.setdefault("properties", {})["review_id"] = item_id
            kept["properties"]["review_status"] = "approved-automatic"
            points = kept.get("geometry", {}).get("coordinates", [])
            inserts = connections_by_target.get(item_id, [])
            if len(points) >= 2 and inserts:
                by_segment: dict[int, list[dict]] = {}
                for connection in inserts:
                    by_segment.setdefault(int(connection["segment_index"]), []).append(connection)
                rebuilt = []
                for segment_index, point in enumerate(points[:-1]):
                    rebuilt.append(point)
                    for connection in sorted(
                            by_segment.get(segment_index, []), key=lambda item: item["t"]):
                        snapped = connection["point"]
                        if snapped != rebuilt[-1] and snapped != points[segment_index + 1]:
                            rebuilt.append(snapped)
                rebuilt.append(points[-1])
                kept["geometry"]["coordinates"] = rebuilt
            features.append(kept)
    if include_rivers:
        features.extend(_manual_feature(item) for item in review_json.get("manual_rivers", []))
    result = {key: deepcopy(value) for key, value in lines_json.items() if key != "features"}
    result["review"] = {
        "source": "line_review.json",
        "version": REVIEW_VERSION,
        "saved_at": review_json.get("saved_at"),
        "automatic_rivers_kept": len(include_ids),
        "manual_rivers": len(review_json.get("manual_rivers", [])),
        "include_rivers": include_rivers,
    }
    result["features"] = features
    return result


def review_view(lines_json: dict, review_json: dict | None,
                width: int, height: int) -> dict:
    saved = review_is_current(lines_json, review_json)
    include_ids = set(review_json.get("include_auto_ids", [])) if saved else {
        item_id for item_id, _ in _automatic_rivers(lines_json)
    }
    automatic = []
    for item_id, feature in _automatic_rivers(lines_json):
        automatic.append({
            "id": item_id,
            "include": item_id in include_ids,
            "properties": feature.get("properties", {}),
            "points": feature.get("geometry", {}).get("coordinates", []),
        })
    fixed = [feature for feature in lines_json.get("features", [])
             if feature.get("properties", {}).get("kind") != "river"]
    return {
        "version": REVIEW_VERSION,
        "auto_lines_fingerprint": auto_lines_fingerprint(lines_json),
        "saved": saved,
        "saved_at": review_json.get("saved_at") if saved else None,
        "include_rivers": bool(review_json.get("include_rivers", True)) if saved else True,
        "snap_tolerance_px": round(max(6.0, min(20.0, 0.015 * np.hypot(width, height))), 2),
        "width": width,
        "height": height,
        "automatic_rivers": automatic,
        "manual_rivers": deepcopy(review_json.get("manual_rivers", [])) if saved else [],
        "fixed_features": fixed,
    }


def apply_review(lines_json: dict, payload: dict,
                 width: int, height: int) -> tuple[dict, dict]:
    auto = dict(_automatic_rivers(lines_json))
    include_rivers = bool(payload.get("include_rivers", True))
    include_ids = [str(value) for value in payload.get("include_auto_ids", [])]
    if len(include_ids) != len(set(include_ids)) or not set(include_ids) <= set(auto):
        raise ValueError("review contains an unknown or repeated automatic river")

    manual_rivers = []
    supplied_manual_ids: set[str] = set()
    for raw in payload.get("manual_rivers", []):
        item_id = str(raw.get("id") or f"manual-{uuid.uuid4().hex[:12]}")
        if not MANUAL_ID.match(item_id) or item_id in supplied_manual_ids:
            raise ValueError("manual river identifiers must be unique and valid")
        supplied_manual_ids.add(item_id)
        raw_points = raw.get("points")
        if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= 5000:
            raise ValueError("each manual river needs between 2 and 5000 points")
        points = []
        for point in raw_points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("manual river points must be [x, y] pairs")
            x, y = float(point[0]), float(point[1])
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError("manual river points must stay inside map_area.png")
            points.append([round(x, 2), round(y, 2)])
        label = str(raw.get("label", "")).strip()
        if len(label) > 100:
            raise ValueError("manual river labels must be 100 characters or fewer")
        edit_kind = str(raw.get("edit_kind", "drawn"))
        if edit_kind not in {"drawn", "joined"}:
            raise ValueError("unknown manual river edit kind")
        manual_rivers.append({
            "id": item_id, "label": label, "edit_kind": edit_kind, "points": points,
        })

    # Snap both endpoints to the closest point on a kept automatic river. The
    # connection record also inserts that exact point into the target path,
    # preserving network topology for later simplification.
    tolerance = max(6.0, min(20.0, 0.015 * float(np.hypot(width, height))))
    reference_segments = []
    for target_id in include_ids:
        points = auto[target_id].get("geometry", {}).get("coordinates", [])
        for segment_index, (start, end) in enumerate(zip(points[:-1], points[1:])):
            reference_segments.append((target_id, segment_index,
                                       np.asarray(start, np.float64),
                                       np.asarray(end, np.float64)))
    for manual in manual_rivers:
        connections = {}
        for endpoint_name, point_index in (("start", 0), ("end", -1)):
            point = np.asarray(manual["points"][point_index], np.float64)
            best = None
            for target_id, segment_index, start, end in reference_segments:
                vector = end - start
                denom = float(vector @ vector)
                t = float(np.clip(((point - start) @ vector) / denom, 0, 1)) if denom else 0.0
                projected = start + t * vector
                distance = float(np.linalg.norm(projected - point))
                if best is None or distance < best[0]:
                    best = (distance, target_id, segment_index, t, projected)
            if best is not None and best[0] <= tolerance:
                distance, target_id, segment_index, t, projected = best
                snapped = [round(float(projected[0]), 2), round(float(projected[1]), 2)]
                manual["points"][point_index] = snapped
                connections[endpoint_name] = {
                    "target_id": target_id,
                    "segment_index": segment_index,
                    "t": round(t, 6),
                    "point": snapped,
                    "distance_px": round(distance, 2),
                }
        manual["connections"] = connections

    review = {
        "version": REVIEW_VERSION,
        "auto_lines_fingerprint": auto_lines_fingerprint(lines_json),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "include_rivers": include_rivers,
        "include_auto_ids": include_ids,
        "manual_rivers": manual_rivers,
    }
    return review, materialize_review(lines_json, review)
