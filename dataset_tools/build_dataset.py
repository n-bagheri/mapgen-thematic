"""Build the thematic-map dataset: category crawl + pipeline screening.

Sources: Wikimedia Commons CATEGORY trees (curated by humans; far cleaner than
keyword search). Every candidate is screened by the pipeline's own Step 1
(semantic interpretation): accepted = in-scope map type AND legend present.
Every decision -- including rejections -- is recorded in dataset/manifest.json
with provenance and license, so the screening precision is itself measurable.

Usage:  .venv\\Scripts\\python.exe dataset_tools\\build_dataset.py [--max N]
Resume-safe: already-screened source URLs are skipped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mapgen.semantics import interpret_map  # noqa: E402

DATASET = ROOT / "dataset"
IMAGES = DATASET / "images"
STAGING = DATASET / "staging"
MANIFEST = DATASET / "manifest.json"

API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "MapGenTactileDataset/0.1 (arsalan77x@gmail.com)"}
SCREEN_MODEL = "gemini-2.5-flash"  # larger free-tier bucket; keep 3.5 for interactive work
PAUSE_S = 5.0                      # stay under free-tier requests-per-minute

# category search terms -> curated Commons category trees
CATEGORY_TERMS = [
    "Köppen climate classification maps",
    "land use maps",
    "vegetation maps",
    "precipitation maps",
    "soil maps",
    "climate maps",
]
PER_CATEGORY = 12
MIN_W, MIN_H = 900, 600


def api_get(**params) -> dict:
    params |= {"action": "query", "format": "json", "formatversion": "2"}
    r = requests.get(API, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    return r.json()


def find_categories(term: str, limit: int = 3) -> list[str]:
    data = api_get(list="search", srsearch=term, srnamespace=14, srlimit=limit)
    return [hit["title"] for hit in data.get("query", {}).get("search", [])
            if "maps" in hit["title"].lower()]


def category_files(cat: str, limit: int) -> list[str]:
    data = api_get(list="categorymembers", cmtitle=cat, cmtype="file", cmlimit=limit)
    files = [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
    if files:
        return files
    # container category (only subcategories): descend one level
    data = api_get(list="categorymembers", cmtitle=cat, cmtype="subcat", cmlimit=10)
    for sub in data.get("query", {}).get("categorymembers", []):
        files += category_files_flat(sub["title"], limit - len(files))
        if len(files) >= limit:
            break
    return files[:limit]


def category_files_flat(cat: str, limit: int) -> list[str]:
    if limit <= 0:
        return []
    data = api_get(list="categorymembers", cmtitle=cat, cmtype="file", cmlimit=limit)
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def file_info(titles: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(titles), 20):
        data = api_get(titles="|".join(titles[i:i + 20]),
                       prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=2200)
        for page in data.get("query", {}).get("pages", []):
            infos = page.get("imageinfo")
            if not infos:
                continue
            ii = infos[0]
            if ii.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if ii.get("width", 0) < MIN_W or ii.get("height", 0) < MIN_H:
                continue
            meta = ii.get("extmetadata", {}) or {}
            out.append({
                "title": page["title"],
                "url": ii["thumburl"] if ii["width"] > 2200 and ii.get("thumburl") else ii["url"],
                "source": ii.get("descriptionurl", ""),
                "license": (meta.get("LicenseShortName") or {}).get("value", ""),
                "artist": re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", ""))[:120],
                "width": ii["width"], "height": ii["height"],
            })
    return out


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"items": [], "screening_model": SCREEN_MODEL}


def save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def screen(path: Path) -> dict:
    sem = interpret_map(path, model=SCREEN_MODEL)
    accepted = bool(sem.in_scope and sem.legend_present)
    return {
        "accepted": accepted,
        "map_type": sem.map_type.value,
        "data_ordering": sem.data_ordering.value,
        "legend_present": sem.legend_present,
        "water_present": sem.water_present,
        "thematic_classes": len(sem.thematic_classes),
        "subject": sem.subject,
        "reason": "" if accepted else
                  ("out of scope" if not sem.in_scope else "no legend"),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # unicode filenames on Windows
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=45, help="max candidates to screen this run")
    args = ap.parse_args()

    IMAGES.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    seen = {it["source"] for it in manifest["items"]}

    candidates: list[dict] = []
    for term in CATEGORY_TERMS:
        cats = find_categories(term)
        print(f"[{term}] categories: {cats}")
        for cat in cats[:2]:
            for title in category_files(cat, PER_CATEGORY):
                candidates.append({"title": title, "category_query": term, "category": cat})
        time.sleep(1)
    # de-dup by title, keep first category
    uniq = {}
    for c in candidates:
        uniq.setdefault(c["title"], c)
    infos = file_info(list(uniq))
    by_title = {c["title"]: c for c in candidates}
    print(f"{len(infos)} downloadable candidates")

    screened = accepted = failures = 0
    for info in infos:
        if screened >= args.max:
            break
        if info["source"] in seen:
            continue
        name = re.sub(r"[^\w.\-]", "_", info["title"].removeprefix("File:"))[:100]
        staged = STAGING / name
        try:
            data = requests.get(info["url"], headers=UA, timeout=120).content
            staged.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            print(f"  download failed: {name} ({exc})")
            continue

        try:
            result = screen(staged)
            failures = 0
        except Exception as exc:  # noqa: BLE001
            print(f"  screening FAILED: {name} ({str(exc)[:140]})")
            staged.unlink(missing_ok=True)
            failures += 1
            is_quota = "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)
            if not is_quota:
                # deterministic failure (e.g. model output overflow): record it
                # so resume runs do not retry the same pathological item
                manifest["items"].append({
                    **{k: info[k] for k in ("title", "source", "license", "artist",
                                            "width", "height")},
                    "category_query": by_title[info["title"]]["category_query"],
                    "category": by_title[info["title"]]["category"],
                    "file": None,
                    "screening": {"accepted": False, "reason": f"screening error: {str(exc)[:140]}"},
                })
                save_manifest(manifest)
            if failures >= 3:
                print("three consecutive screening failures -- stopping (quota?); resume later")
                break
            continue

        screened += 1
        item = {k: info[k] for k in ("title", "source", "license", "artist", "width", "height")}
        item |= {"category_query": by_title[info["title"]]["category_query"],
                 "category": by_title[info["title"]]["category"],
                 "screening": result}
        if result["accepted"]:
            accepted += 1
            dest = IMAGES / name
            staged.replace(dest)
            item["file"] = str(dest.relative_to(DATASET)).replace("\\", "/")
        else:
            staged.unlink(missing_ok=True)
            item["file"] = None
        manifest["items"].append(item)
        save_manifest(manifest)
        mark = "ACCEPT" if result["accepted"] else f"reject ({result['reason']})"
        print(f"  [{screened:3d}] {mark:24} {result['map_type'][:28]:30} {name[:50]}")
        time.sleep(PAUSE_S)

    total_acc = sum(1 for it in manifest["items"] if it["screening"]["accepted"])
    print(f"\nthis run: {screened} screened, {accepted} accepted")
    print(f"dataset total: {len(manifest['items'])} screened, {total_acc} accepted -> dataset/images/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
