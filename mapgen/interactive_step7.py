"""Exact-render fast path used only by the /test Step 7 editor."""

from time import perf_counter

from .patterns import PATTERNS, normalize_pattern_transform
from .render_cache import PatternRasterCache, reuse_step7_rasters


RASTER_CACHE = PatternRasterCache()


def apply_edit(run_dir, symbols, data):
    """Validate and apply all changes in memory before writing any artifacts."""
    from .symbols import reassign_step7_pattern

    assignments = symbols["area_assignments"]
    action = data.get("action")
    allowed = {
        "assign": {"action", "group_id", "pattern", "preserve_haptic_distances"},
        "transform": {"action", "group_id", "transform"},
        "reset": {"action", "groups"},
    }
    if action not in allowed or set(data) != allowed[action]:
        raise ValueError("Choose one Step 7 assignment, transform, or reset action")

    def group_id(value):
        if type(value) is not int or not 0 <= value < len(assignments):
            raise ValueError("Unknown Step 7 area")
        return value

    def pattern(value):
        if not isinstance(value, str) or value not in PATTERNS:
            raise ValueError("Select a pattern from the Step 7 library")
        return value

    before = [(item["pattern"], normalize_pattern_transform(item.get("transform")))
              for item in assignments]
    if action == "assign":
        index = group_id(data["group_id"])
        selected = pattern(data["pattern"])
        preserve = data["preserve_haptic_distances"]
        if type(preserve) is not bool:
            raise ValueError("preserve_haptic_distances must be true or false")
        if selected != assignments[index]["pattern"]:
            reassign_step7_pattern(run_dir, symbols, index, selected, preserve)
    elif action == "transform":
        index = group_id(data["group_id"])
        if assignments[index]["pattern"] in {"plain", "solid_black"}:
            raise ValueError("Plain and solid fills have no repeating pattern to transform")
        assignments[index]["transform"] = normalize_pattern_transform(data["transform"])
    else:
        groups = data["groups"]
        if not isinstance(groups, list) or len(groups) != len(assignments):
            raise ValueError("Reset must include the initial patterns for every area")
        validated = []
        for item in groups:
            if not isinstance(item, dict) or set(item) != {"group_id", "pattern", "transform"}:
                raise ValueError("Reset requires a pattern and transform for every area")
            validated.append((group_id(item["group_id"]), pattern(item["pattern"]),
                              normalize_pattern_transform(item["transform"])))
        if len({index for index, _, _ in validated}) != len(assignments):
            raise ValueError("Reset area IDs must be unique")
        # Use the same independent assignment operation as the old sequence of
        # reset requests, but postpone rendering until the entire reset is done.
        for index, selected, transform in validated:
            if assignments[index]["pattern"] != selected:
                reassign_step7_pattern(run_dir, symbols, index, selected, False)
            assignments[index]["transform"] = transform
    after = [(item["pattern"], normalize_pattern_transform(item.get("transform")))
             for item in assignments]
    return before != after


def render_edit(image, run_dir, symbols, include_legend=False):
    """Run the original renderers, reusing only identical intermediate rasters."""
    from .boundaries import run_step8
    from .cleanup import run_step8a
    from .symbols import rerender_step7_artifacts
    from .braille import load_step7_page_layout

    start = perf_counter()
    with reuse_step7_rasters(RASTER_CACHE) as reuse:
        rerender_step7_artifacts(run_dir, symbols)
        run_step8(image, runs_dir=run_dir.parent)
        run_step8a(image, runs_dir=run_dir.parent)
    if (run_dir / "step8a_cleanup.png").exists():
        load_step7_page_layout(run_dir)
    if (run_dir / "braille_labels.json").exists():
        from .braille import run_step8 as run_braille
        run_braille(image, runs_dir=run_dir.parent)
    if include_legend and (run_dir / "legend_labels.json").exists():
        from .legend import run_step9
        run_step9(image, runs_dir=run_dir.parent)
    return {"seconds": round(perf_counter() - start, 3),
            "pattern_cache_hits": reuse.pattern_hits,
            "pattern_renders": reuse.pattern_misses,
            "base_cache_hits": reuse.base_hits}
