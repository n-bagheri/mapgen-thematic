"""Step 5 -- Class aggregation from the untouched Step 4 classification.

Texture slots: 4 when a water area exists in the generalized map (water always
claims the wavy pattern), else 5. If more thematic classes survive Step 4 than
slots exist, merge them: contiguous re-binning for ordered data (deterministic),
Gemini-proposed semantic merges for qualitative data (Checkpoint: human reviews
the plan in the UI / aggregation.json before Step 6 consumes it). Dropping to
plain white is the last resort and only happens in the fallback path.

Artifact: runs/<name>/aggregation.json
"""

from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .output_spec import OutputSpec
from .semantics import DEFAULT_MODEL, MapSemantics, _ensure_api_key, require_pipeline_eligible


AGGREGATION_REVIEW_VERSION = 1
AGGREGATION_REVIEW_ARTIFACT = "aggregation_review.json"


def aggregation_fingerprint(aggregation: dict) -> str:
    payload = {
        "version": AGGREGATION_REVIEW_VERSION,
        "slots": aggregation["slots"],
        "source_classes": aggregation.get("source_classes", []),
        "groups": [{"members": sorted(group["members"]), "label": group["label"]}
                   for group in aggregation["groups"]],
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def load_aggregation_review(out_dir: Path, aggregation: dict) -> dict | None:
    path = out_dir / AGGREGATION_REVIEW_ARTIFACT
    if not path.exists():
        return None
    review = json.loads(path.read_text(encoding="utf-8"))
    if (review.get("version") != AGGREGATION_REVIEW_VERSION
            or review.get("proposal_fingerprint") != aggregation_fingerprint(aggregation)):
        return None
    return review


def save_aggregation_review(out_dir: Path, aggregation: dict,
                            decisions: list[dict]) -> dict:
    """Save the reviewed canonical Step 5 grouping."""
    source = {int(item["index"]): item["label"]
              for item in aggregation.get("source_classes", [])}
    if not source:
        raise ValueError("aggregation proposal has no source classes; rerun Step 5")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("at least one final group is required")
    if len(decisions) > int(aggregation["slots"]):
        raise ValueError(f"at most {aggregation['slots']} final groups are allowed")

    seen: set[int] = set()
    groups = []
    for position, decision in enumerate(decisions, start=1):
        try:
            members = [int(member) for member in decision.get("members", [])]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"group {position} has invalid members") from exc
        if not members:
            continue
        label = str(decision.get("label", "")).strip()
        if not label:
            raise ValueError(f"group {position} needs a label")
        if len(set(members)) != len(members):
            raise ValueError(f"group {position} contains a duplicate class")
        unknown = [member for member in members if member not in source]
        duplicate = [member for member in members if member in seen]
        if unknown:
            raise ValueError(f"group {position} contains unknown classes: {unknown}")
        if duplicate:
            raise ValueError(f"classes assigned more than once: {duplicate}")
        seen.update(members)
        groups.append({
            "label": label,
            "members": members,
            "member_labels": [source[member] for member in members],
            "rationale": str(decision.get(
                "rationale", "human-reviewed grouping")).strip(),
            "approved": bool(decision.get("approved", len(members) == 1)),
        })
    missing = sorted(set(source) - seen)
    if missing:
        raise ValueError(f"every source class must be assigned exactly once; missing {missing}")
    rejected = [group["label"] for group in groups
                if len(group["members"]) > 1 and not group["approved"]]
    status = "rejected" if rejected else "approved"
    review = {
        "version": AGGREGATION_REVIEW_VERSION,
        "proposal_fingerprint": aggregation_fingerprint(aggregation),
        "status": status,
        "approved": status == "approved",
        "groups": groups,
        "rejected_groups": rejected,
    }
    (out_dir / AGGREGATION_REVIEW_ARTIFACT).write_text(
        json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
    return review


def effective_aggregation(out_dir: Path, aggregation: dict) -> dict:
    if not aggregation.get("review_required"):
        return aggregation
    review = load_aggregation_review(out_dir, aggregation)
    if not review or not review.get("approved"):
        raise RuntimeError(
            "Step 5 aggregation proposal requires approval before Step 6")
    result = dict(aggregation)
    result["groups"] = [{key: value for key, value in group.items()
                         if key != "approved"} for group in review["groups"]]
    result["review_status"] = "approved"
    return result


class MergeGroup(BaseModel):
    label: str = Field(description="Short legend-ready name for the merged group (<= 3 words)")
    members: list[str] = Field(description="Original class labels merged into this group")
    rationale: str


class AggregationProposal(BaseModel):
    groups: list[MergeGroup]


MERGE_PROMPT = """\
You aggregate thematic map classes for a tactile map, which supports at most
{slots} distinct area textures. Merge the {n} classes below into AT MOST
{slots} groups by SEMANTIC similarity (e.g. two grassland variants belong
together; specialty crops can become one group). Rules:

- Every input class appears in exactly one group, spelled exactly as given.
- Prefer balanced, meaningful groups over dumping everything into one.
- Group labels must be short (<= 3 words) and legend-ready.

Map subject: {subject}

Classes (label -- share of map area):
{listing}
"""


def _range_label(a: str, b: str) -> str:
    """'8-10' + '10-12' -> '8-12'; falls back to 'a / b'."""
    na = re.findall(r"-?\d+\.?\d*", a)
    nb = re.findall(r"-?\d+\.?\d*", b)
    if na and nb:
        a_compact = re.sub(r"\s+", "", a)
        b_compact = re.sub(r"\s+", "", b)
        if a_compact.startswith(("<=", "≤")):
            return f"≤ {nb[-1]}"
        if a_compact.startswith("<"):
            return f"< {nb[-1]}"
        if b_compact.startswith((">=", "≥")):
            return f"≥ {na[0]}"
        if b_compact.startswith(">"):
            return f"> {na[0]}"
        return f"{na[0]}–{nb[-1]}"
    return f"{a} / {b}"


def rebin_ordered(thematic: list[dict], slots: int) -> list[dict]:
    """Merge adjacent bins (legend order) until <= slots, smallest pairs first."""
    bins = [{"label": c["label"], "members": [c["index"]], "member_labels": [c["label"]],
             "area": c["area_px"], "rationale": ""} for c in thematic]
    while len(bins) > slots:
        pair = min(range(len(bins) - 1), key=lambda i: bins[i]["area"] + bins[i + 1]["area"])
        a, b = bins[pair], bins[pair + 1]
        bins[pair:pair + 2] = [{
            "label": _range_label(a["label"], b["label"]),
            "members": a["members"] + b["members"],
            "member_labels": a["member_labels"] + b["member_labels"],
            "area": a["area"] + b["area"],
            "rationale": "adjacent bins merged (ordered data)",
        }]
    return bins


def propose_semantic(thematic: list[dict], slots: int, sem: MapSemantics,
                     model: str | None) -> AggregationProposal:
    from .semantics import generate_json

    listing = "\n".join(f"- {c['label']} -- {c['area_share'] * 100:.1f}%" for c in thematic)
    prompt = MERGE_PROMPT.format(slots=slots, n=len(thematic), subject=sem.subject, listing=listing)
    return generate_json([prompt], AggregationProposal, model=model)


def validate_proposal(plan: AggregationProposal, thematic: list[dict], slots: int) -> list[dict] | None:
    by_label = {c["label"]: c for c in thematic}
    seen: set[str] = set()
    groups = []
    for g in plan.groups:
        members = [m for m in g.members if m in by_label and m not in seen]
        seen.update(members)
        if members:
            groups.append({"label": g.label, "members": [by_label[m]["index"] for m in members],
                           "member_labels": members, "rationale": g.rationale})
    if len(seen) != len(thematic) or not groups or len(groups) > slots:
        return None
    return groups


def fallback_merge(thematic: list[dict], slots: int) -> list[dict]:
    """Deterministic last resort: top priority classes stay, the rest -> 'other'."""
    ordered = sorted(thematic, key=lambda c: (c["priority"] is None, c["priority"], -c["area_px"]))
    keep, rest = ordered[:slots - 1], ordered[slots - 1:]
    groups = [{"label": c["label"], "members": [c["index"]], "member_labels": [c["label"]],
               "rationale": "kept (highest priority)"} for c in keep]
    groups.append({"label": "other", "members": [c["index"] for c in rest],
                   "member_labels": [c["label"] for c in rest],
                   "rationale": "fallback merge of remaining classes"})
    return groups


def run_step5(image_path: Path, model: str | None = None,
              runs_dir: Path = Path("runs")) -> dict:
    """Build the reviewed aggregation proposal without changing geography."""
    from .postprocess import run_step5 as run_aggregation_first

    return run_aggregation_first(image_path, model=model, runs_dir=runs_dir)
