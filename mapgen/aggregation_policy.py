"""Deterministic semantic aggregation proposal policy for canonical Step 5.

The policy produces a complete grouping that fits the tactile texture
capacity. Every multi-class group remains subject to the concrete Step 5
review before Step 6 may simplify its shared geography.
"""

from __future__ import annotations

import re

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

