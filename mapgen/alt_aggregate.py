"""Reviewable semantic aggregation helpers used by Alt MapGen Step 5.

The alternate aggregator consumes Alt Step 5 artifacts and the semantic
relationship policy created before generalization.  It proposes a complete
grouping that fits the tactile texture capacity, but a proposal containing any
multi-class group must be explicitly reviewed before Alt Step 7 may render it.
Local Step 5 replacement permissions and global Step 6 aggregation remain
separate decisions.
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from .aggregate import rebin_ordered
from .output_spec import OutputSpec
from .semantics import MapSemantics


AGGREGATION_REVIEW_VERSION = 1
AGGREGATION_REVIEW_ARTIFACT = "alt_aggregation_review.json"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "and", "with", "often", "primarily", "the", "of", "or", "mixed",
    "land", "areas", "area", "category", "other",
}
_FAMILIES = {
    "forest": {"forest", "wood", "woodland", "trees", "tree"},
    "grassland": {"grass", "grassland", "pasture", "meadow", "grazing"},
    "field_crops": {
        "crop", "crops", "cropland", "cereal", "cereals", "wheat", "corn",
        "maize", "beet", "beets", "field", "arable", "grain",
    },
    "specialty_crops": {
        "vine", "vines", "vineyard", "vineyards", "olive", "olives",
        "garden", "gardening", "fruit", "fruits", "vegetable", "vegetables",
        "flower", "flowers", "orchard", "orchards", "horticulture",
    },
}


def _aggregate_lookup(relationships: dict) -> dict[frozenset[int], bool]:
    if not relationships.get("reviewed"):
        return {}
    return {
        frozenset((int(pair["a_index"]), int(pair["b_index"]))):
            bool(pair.get("aggregation_compatible"))
        for pair in relationships.get("pairs", [])
    }


def _groups_compatible(a: dict, b: dict, lookup: dict[frozenset[int], bool]) -> bool:
    return all(lookup.get(frozenset((left, right)), False)
               for left in a["members"] for right in b["members"] if left != right)


def _group_label(labels: list[str]) -> str:
    lower = [label.lower() for label in labels]
    if all("grass" in label for label in lower):
        return "Grasslands"
    tokens = [_semantic_tokens(label) for label in labels]
    if all(parts & _FAMILIES["field_crops"] for parts in tokens):
        return "Field Crops"
    if all(parts & _FAMILIES["specialty_crops"] for parts in tokens):
        return "Specialty Crops"
    if all(any(word in label for word in ("crop", "garden", "vine", "olive"))
           for label in lower):
        return "Cultivated land"
    words = labels[0].split()
    common = [word for word in words
              if all(word.lower().strip("(),") in label for label in lower[1:])]
    if common:
        return " ".join(common[:3]).strip("(),")
    return " / ".join(label.split(" (")[0] for label in labels)


def _semantic_tokens(label: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(label.lower()) if token not in _STOPWORDS}


def _family(label: str) -> str | None:
    tokens = _semantic_tokens(label)
    scores = {name: len(tokens & words) for name, words in _FAMILIES.items()}
    family, score = max(scores.items(), key=lambda item: item[1])
    return family if score else None


def _proposal_pair_score(a: dict, b: dict,
                         lookup: dict[frozenset[int], bool]) -> tuple[int, str]:
    pairs = [frozenset((left, right))
             for left in a["members"] for right in b["members"] if left != right]
    if pairs and all(lookup.get(pair, False) for pair in pairs):
        return 100, "proposed by the semantic compatibility policy"

    labels = a["member_labels"] + b["member_labels"]
    families = [_family(label) for label in labels]
    known = [family for family in families if family]
    if known and len(known) == len(families) and len(set(known)) == 1:
        return 40, f"shared semantic family: {known[0].replace('_', ' ')}"
    if known and set(known) <= {"field_crops", "specialty_crops"}:
        return 15, "both describe cultivated land"

    left_tokens = set().union(*(_semantic_tokens(label) for label in a["member_labels"]))
    right_tokens = set().union(*(_semantic_tokens(label) for label in b["member_labels"]))
    overlap = len(left_tokens & right_tokens)
    if overlap:
        return 10 + overlap, "shared category wording"
    return 0, "capacity-only fallback; requires careful human review"


def propose_complete_aggregation(thematic: list[dict], slots: int,
                                 relationships: dict) -> tuple[list[dict], list[dict]]:
    """Propose at most ``slots`` groups without changing the Step 5 raster.

    Reviewed/model-proposed compatibility is preferred.  A deterministic
    lexical fallback completes the proposal when semantic services are
    unavailable.  Because this is only a proposal, every resulting multi-class
    group is still gated by the concrete Step 6 review.
    """
    lookup = {
        frozenset((int(pair["a_index"]), int(pair["b_index"]))):
            bool(pair.get("aggregation_compatible"))
        for pair in relationships.get("pairs", [])
    }
    groups = [{
        "label": cl["label"], "members": [int(cl["index"])],
        "member_labels": [cl["label"]], "area": int(cl["area_px"]),
        "priority": cl.get("priority"), "rationale": "kept as its original class",
    } for cl in thematic]
    merge_log: list[dict] = []

    # The ceiling is a maximum, not a target.  First merge enough groups to fit
    # it; after that, continue only for strongly compatible semantic families.
    while len(groups) > 1:
        candidates = []
        for left in range(len(groups)):
            for right in range(left + 1, len(groups)):
                score, reason = _proposal_pair_score(groups[left], groups[right], lookup)
                combined_area = groups[left]["area"] + groups[right]["area"]
                priority = min((value for value in (
                    groups[left]["priority"], groups[right]["priority"])
                    if value is not None), default=10_000)
                candidates.append((-score, combined_area, priority, left, right, reason))
        best = min(candidates)
        semantic_score = -best[0]
        if len(groups) <= slots and semantic_score < 40:
            break
        _, _, _, left, right, reason = best
        a, b = groups[left], groups[right]
        labels = a["member_labels"] + b["member_labels"]
        merged = {
            "label": _group_label(labels),
            "members": a["members"] + b["members"],
            "member_labels": labels,
            "area": a["area"] + b["area"],
            "priority": min((value for value in (a["priority"], b["priority"])
                             if value is not None), default=None),
            "rationale": reason,
        }
        merge_log.append({
            "left": a["member_labels"], "right": b["member_labels"],
            "result": merged["label"], "members": merged["member_labels"],
            "rationale": reason,
        })
        groups[left] = merged
        groups.pop(right)

    groups.sort(key=lambda group: (
        group["priority"] is None,
        group["priority"] if group["priority"] is not None else 10_000,
        -group["area"],
    ))
    for group in groups:
        group.pop("area", None)
        group.pop("priority", None)
    return groups, merge_log


def aggregation_fingerprint(aggregation: dict) -> str:
    payload = {
        "version": AGGREGATION_REVIEW_VERSION,
        "slots": aggregation["slots"],
        "source_classes": aggregation.get("source_classes", []),
        "groups": [{"members": sorted(group["members"]), "label": group["label"]}
                   for group in aggregation["groups"]],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    """Validate and persist the concrete grouping chosen in the Step 6 UI."""
    source = {int(item["index"]): item["label"]
              for item in aggregation.get("source_classes", [])}
    if not source:
        raise ValueError("aggregation proposal has no source classes")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("at least one final group is required")
    if len(decisions) > int(aggregation["slots"]):
        raise ValueError(f"at most {aggregation['slots']} final groups are allowed")

    seen: set[int] = set()
    groups: list[dict] = []
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
        if unknown:
            raise ValueError(f"group {position} contains unknown classes: {unknown}")
        duplicate = [member for member in members if member in seen]
        if duplicate:
            raise ValueError(f"classes assigned more than once: {duplicate}")
        seen.update(members)
        groups.append({
            "label": label,
            "members": members,
            "member_labels": [source[member] for member in members],
            "rationale": str(decision.get("rationale", "human-reviewed grouping")).strip(),
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
    """Return the approved grouping or raise before tactile rendering."""
    if not aggregation.get("review_required"):
        return aggregation
    review = load_aggregation_review(out_dir, aggregation)
    if not review or not review.get("approved"):
        raise RuntimeError(
            "Alt Step 5 aggregation proposal requires approval before Alt Step 6")
    result = dict(aggregation)
    result["groups"] = [{key: value for key, value in group.items() if key != "approved"}
                        for group in review["groups"]]
    result["plain_thematic"] = []
    result["review_status"] = "approved"
    return result


def aggregate_reviewed(thematic: list[dict], slots: int,
                       relationships: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Return patterned groups, plain groups, and an auditable merge log."""
    lookup = _aggregate_lookup(relationships)
    groups = [{
        "label": cl["label"],
        "members": [cl["index"]],
        "member_labels": [cl["label"]],
        "area": cl["area_px"],
        "priority": cl.get("priority"),
        "rationale": "kept as its original class",
    } for cl in thematic]
    merge_log = []

    while len(groups) > slots:
        candidates = []
        for left in range(len(groups)):
            for right in range(left + 1, len(groups)):
                if not _groups_compatible(groups[left], groups[right], lookup):
                    continue
                combined_area = groups[left]["area"] + groups[right]["area"]
                combined_priority = min(
                    value for value in (groups[left]["priority"], groups[right]["priority"])
                    if value is not None
                ) if any(value is not None for value in (
                    groups[left]["priority"], groups[right]["priority"])) else 10_000
                candidates.append((combined_area, combined_priority, left, right))
        if not candidates:
            break
        _, _, left, right = min(candidates)
        a, b = groups[left], groups[right]
        labels = a["member_labels"] + b["member_labels"]
        merged = {
            "label": _group_label(labels),
            "members": a["members"] + b["members"],
            "member_labels": labels,
            "area": a["area"] + b["area"],
            "priority": min(
                (value for value in (a["priority"], b["priority"]) if value is not None),
                default=None,
            ),
            "rationale": "merged through reviewed aggregation-compatible relationships",
        }
        merge_log.append({
            "left": a["member_labels"], "right": b["member_labels"],
            "result": merged["label"], "members": merged["member_labels"],
        })
        groups[left] = merged
        groups.pop(right)

    # If semantics cannot honestly reach the texture ceiling, preserve the
    # remaining geography but leave lower-priority groups plain.
    ranked = sorted(groups, key=lambda group: (
        group["priority"] is None,
        group["priority"] if group["priority"] is not None else 10_000,
        -group["area"],
    ))
    patterned = ranked[:slots]
    plain = ranked[slots:]
    for collection in (patterned, plain):
        for group in collection:
            group.pop("area", None)
            group.pop("priority", None)
    return patterned, plain, merge_log


def run_alt_step6(image_path: Path, model: str | None = None,
                  runs_dir: Path = Path("runs")) -> dict:
    out_dir = runs_dir / image_path.stem
    if not (out_dir / "alt_classes_gen.json").exists():
        raise FileNotFoundError("run Alt Step 5 manually before Alt Step 6")

    spec = OutputSpec.load_or_create()
    sem = MapSemantics.model_validate_json(
        (out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    classes = json.loads((out_dir / "alt_classes_gen.json").read_text(
        encoding="utf-8"))["classes"]
    # Alt Step 6 is deterministic and review-gated.  An old reviewed policy may
    # improve its initial proposal, but a failed/unreviewed model call is neither
    # required nor exposed as a pipeline warning.
    relationship_path = out_dir / "alt_class_relationships.json"
    saved_relationships = (json.loads(relationship_path.read_text(encoding="utf-8"))
                           if relationship_path.exists() else {})
    relationships = (saved_relationships if saved_relationships.get("reviewed")
                     else {"reviewed": False, "pairs": [], "notes": []})
    surviving = [cl for cl in classes if cl["area_px"] > 0]
    water = [cl for cl in surviving
             if cl.get("source") == "water-heuristic"
             or (not cl.get("is_thematic") and "water" in cl.get("label", "").lower())]
    slots = spec.texture_slots(water_present=bool(water))
    thematic = sorted((cl for cl in surviving if cl["is_thematic"]),
                      key=lambda cl: cl["index"])
    extras = [cl for cl in surviving if not cl["is_thematic"] and cl not in water]
    notes: list[str] = []
    merge_log = []

    if sem.data_ordering.value == "ordered" and len(thematic) <= slots:
        mode = "identity"
        groups = [{
            "label": cl["label"], "members": [cl["index"]],
            "member_labels": [cl["label"]], "rationale": "",
        } for cl in thematic]
        plain_thematic = []
    elif sem.data_ordering.value == "ordered":
        mode = "ordered_rebin_proposal"
        groups = rebin_ordered(thematic, slots)
        for group in groups:
            group.pop("area", None)
        plain_thematic = []
    else:
        groups, merge_log = propose_complete_aggregation(thematic, slots, relationships)
        plain_thematic = []
        mode = "semantic_merge_proposal" if merge_log else "identity"

    review_required = any(len(group["members"]) > 1 for group in groups)

    aggregation = {
        "branch": "alternate",
        "mode": mode,
        "slots": slots,
        "texture_ceiling": spec.constants.max_area_textures,
        "texture_ceiling_is_target": False,
        "proposed_texture_count": len(groups) + (1 if water else 0),
        "unused_texture_capacity": max(
            0, spec.constants.max_area_textures - len(groups) - (1 if water else 0)),
        "relationships_reviewed": bool(relationships.get("reviewed")),
        "review_required": review_required,
        "review_status": "needs_review" if review_required else "not_required",
        "water": ({"label": water[0]["label"],
                   "members": [cl["index"] for cl in water]} if water else None),
        "groups": groups,
        "plain_thematic": plain_thematic,
        "source_classes": [{"index": cl["index"], "label": cl["label"]}
                           for cl in thematic],
        "non_thematic_extra": [{
            "index": cl["index"], "label": cl["label"], "priority": cl.get("priority")
        } for cl in extras],
        "merge_log": merge_log,
        "notes": notes,
    }
    aggregation["proposal_fingerprint"] = aggregation_fingerprint(aggregation)
    review = load_aggregation_review(out_dir, aggregation)
    if review:
        aggregation["review_status"] = review["status"]
    (out_dir / "alt_aggregation.json").write_text(
        json.dumps(aggregation, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"out_dir": out_dir, "aggregation": aggregation}
