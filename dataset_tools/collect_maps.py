"""Collect provenance-first Gold and Silver thematic-map images.

This collector deliberately does *not* call the MapGen semantic/VLM pipeline.
It creates two independent pools:

* ``dataset/gold``: the legacy maps accepted by the project owner, except for
  explicit exclusions.  "Gold" here means scope-verified; these maps are not
  yet fully annotated ground truth.
* ``dataset/silver``: unverified candidates gathered from curated Wikimedia
  Commons category trees.  They are intended for a later automatic and human
  filtering pass.

Every stored image has source/revision metadata, license metadata, original
and stored dimensions, a SHA-256 digest, discovery categories, and the exact
download URL.  The script is resume-safe and never modifies or removes the
legacy ``dataset/images`` collection.

Usage:
    python dataset_tools/collect_maps.py --gold --silver --silver-max 250
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests
from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset"
LEGACY_MANIFEST = DATASET / "manifest.json"
GOLD_DIR = DATASET / "gold"
SILVER_DIR = DATASET / "silver"

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "MapGenTactileDataset/0.2 (arsalan77x@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT}

# 1280 is a Wikimedia-recommended thumbnail size and is sufficient for the
# candidate/screening stage. Gold maps already present locally keep their
# higher native resolution.
THUMB_WIDTH = 1280
MIN_LONG_SIDE = 900
MIN_SHORT_SIDE = 600
MAX_DOWNLOAD_BYTES = 40_000_000
REQUEST_PAUSE_S = 2.0
DOWNLOAD_PAUSE_S = 1.25

EXCLUDED_GOLD_TITLES = {
    "File:10 Districts of Yangon City.jpg": "Rejected by project owner on 2026-07-15",
}

# Broad enough to build a useful Silver pool; later filtering determines
# whether an individual image is an in-scope area-class or sequential map.
SILVER_CATEGORY_SEEDS = (
    "Category:Köppen climate classification maps",
    "Category:Weather and climate maps",
    "Category:Land use maps",
    "Category:Land cover",
    "Category:CORINE Land Cover",
    "Category:Vegetation maps",
    "Category:Biome maps",
    "Category:Geological maps",
    "Category:Precipitation maps",
    "Category:Temperature maps",
    "Category:Ecological maps",
    "Category:Maps of ecoregions",
    "Category:Linguistic maps",
    "Category:Ethnographic maps",
)

SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/svg+xml"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_html(value: str | None, limit: int = 1000) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def meta_value(metadata: dict[str, Any], name: str) -> str:
    raw = metadata.get(name, {})
    return clean_html(raw.get("value") if isinstance(raw, dict) else str(raw))


def api_get(session: requests.Session, **params: Any) -> dict[str, Any]:
    params |= {"action": "query", "format": "json", "formatversion": "2"}
    response: requests.Response | None = None
    for delay in (0, 8, 25, 60):
        if delay:
            time.sleep(delay)
        response = session.get(API, params=params, timeout=90)
        if response.status_code not in {429, 503}:
            break
        print(f"API THROTTLED ({response.status_code}); backing off", flush=True)
    assert response is not None
    response.raise_for_status()
    time.sleep(REQUEST_PAUSE_S)
    return response.json()


def category_members(
    session: requests.Session,
    category: str,
    file_limit: int,
) -> tuple[list[str], list[str]]:
    files: list[str] = []
    subcats: list[str] = []
    continuation: dict[str, Any] = {}
    while True:
        data = api_get(
            session,
            list="categorymembers",
            cmtitle=category,
            cmtype="file|subcat",
            cmlimit="500",
            **continuation,
        )
        for member in data.get("query", {}).get("categorymembers", []):
            if member.get("ns") == 6:
                files.append(member["title"])
            elif member.get("ns") == 14:
                subcats.append(member["title"])
        if len(files) >= file_limit:
            break
        if "continue" not in data:
            break
        continuation = data["continue"]
    return files, subcats


def discover_titles(
    session: requests.Session,
    seeds: tuple[str, ...],
    per_seed: int,
    depth: int,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    discoveries: dict[str, set[str]] = {}
    discovery_seeds: dict[str, set[str]] = {}
    for seed in seeds:
        found = 0
        queue: deque[tuple[str, int]] = deque([(seed, 0)])
        seen_categories: set[str] = set()
        while queue and found < per_seed:
            category, level = queue.popleft()
            if category in seen_categories:
                continue
            seen_categories.add(category)
            try:
                files, subcats = category_members(
                    session, category, max(1, per_seed - found)
                )
            except requests.RequestException as exc:
                print(f"WARN category {category}: {exc}", flush=True)
                continue
            for title in files:
                before = len(discoveries.get(title, set()))
                discoveries.setdefault(title, set()).add(category)
                discovery_seeds.setdefault(title, set()).add(seed)
                if before == 0:
                    found += 1
                if found >= per_seed:
                    break
            if level < depth:
                queue.extend((subcat, level + 1) for subcat in subcats)
        print(f"DISCOVER {seed}: {found} candidates", flush=True)
    return discoveries, discovery_seeds


def file_info(session: requests.Session, titles: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # Keep batches small because the revisions module and long historical file
    # titles can otherwise exceed MediaWiki's practical query limits.
    for start in range(0, len(titles), 20):
        data = api_get(
            session,
            titles="|".join(titles[start : start + 40]),
            prop="imageinfo|info",
            iiprop="url|size|mime|extmetadata|sha1|timestamp",
            iiurlwidth=str(THUMB_WIDTH),
        )
        if data.get("error"):
            print(f"WARN metadata batch: {data['error']}", flush=True)
            continue
        pages = data.get("query", {}).get("pages", [])
        if isinstance(pages, dict):
            pages = list(pages.values())
        for page in pages:
            imageinfo = page.get("imageinfo") or []
            if not imageinfo or page.get("missing"):
                continue
            info = imageinfo[0]
            metadata = info.get("extmetadata") or {}
            results.append(
                {
                    "title": page["title"],
                    "page_id": page.get("pageid"),
                    "revision_id": page.get("lastrevid"),
                    "revision_timestamp": page.get("touched"),
                    "source_page": info.get("descriptionurl", ""),
                    "original_url": info.get("url", ""),
                    "thumbnail_url": info.get("thumburl", ""),
                    "mime": info.get("mime", ""),
                    "original_width": int(info.get("width", 0)),
                    "original_height": int(info.get("height", 0)),
                    "commons_sha1": info.get("sha1", ""),
                    "commons_timestamp": info.get("timestamp"),
                    "license": meta_value(metadata, "LicenseShortName"),
                    "license_url": meta_value(metadata, "LicenseUrl"),
                    "usage_terms": meta_value(metadata, "UsageTerms"),
                    "artist": meta_value(metadata, "Artist"),
                    "credit": meta_value(metadata, "Credit"),
                    "attribution_required": meta_value(metadata, "AttributionRequired"),
                }
            )
    # Defensive de-duplication: normalized/redirected Commons titles can appear
    # more than once when a batch contains aliases of the same file.
    return list({item["title"]: item for item in results}.values())


def license_is_dataset_friendly(info: dict[str, Any]) -> bool:
    value = f"{info.get('license', '')} {info.get('usage_terms', '')}".lower()
    allowed = ("public domain", "cc0", "cc by", "creative commons attribution")
    return any(token in value for token in allowed)


def dimension_is_usable(info: dict[str, Any]) -> bool:
    width = info["original_width"]
    height = info["original_height"]
    return max(width, height) >= MIN_LONG_SIDE and min(width, height) >= MIN_SHORT_SIDE


def stable_filename(title: str, mime: str) -> str:
    base = title.removeprefix("File:").rsplit(".", 1)[0]
    ascii_base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_base).strip("_")[:72] or "map"
    key = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    extension = ".png" if mime == "image/svg+xml" else ".jpg" if mime == "image/jpeg" else ".png"
    return f"{slug}__{key}{extension}"


def load_collection_manifest(directory: Path, tier: str) -> dict[str, Any]:
    path = directory / "manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema_version": "2.0",
        "tier": tier,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "items": [],
        "failures": [],
    }


def save_collection_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = utc_now()
    path = directory / "manifest.json"
    temporary = directory / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def download_image(
    session: requests.Session,
    info: dict[str, Any],
    destination: Path,
) -> tuple[str, int, int, int, str]:
    use_thumbnail = info["mime"] == "image/svg+xml" or max(
        info["original_width"], info["original_height"]
    ) > THUMB_WIDTH
    url = info["thumbnail_url"] if use_thumbnail and info["thumbnail_url"] else info["original_url"]
    response: requests.Response | None = None
    for attempt, delay in enumerate((0, 15, 45)):
        if delay:
            time.sleep(delay)
        response = session.get(url, timeout=180)
        if response.status_code != 429:
            break
        print(f"THROTTLED; retrying {info['title']} (attempt {attempt + 1})", flush=True)
    assert response is not None
    response.raise_for_status()
    content = response.content
    if not content or len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"download size {len(content)} is outside allowed range")
    digest = hashlib.sha256(content).hexdigest()
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
    with Image.open(io.BytesIO(content)) as image:
        stored_width, stored_height = image.size
        stored_format = image.format or ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    time.sleep(DOWNLOAD_PAUSE_S)
    return digest, stored_width, stored_height, len(content), url


def canonical_title_from_source(item: dict[str, Any]) -> str:
    source = item.get("source", "")
    name = unquote(urlsplit(source).path.rsplit("/", 1)[-1]).replace("_", " ")
    return name if name.startswith("File:") else f"File:{name}"


def find_legacy_image(item: dict[str, Any]) -> Path | None:
    relative = item.get("file")
    if relative:
        direct = DATASET / relative
        if direct.exists():
            return direct
    canonical = canonical_title_from_source(item).removeprefix("File:").replace(" ", "_")
    candidate = DATASET / "images" / canonical
    if candidate.exists():
        return candidate
    sanitized = re.sub(r"[^\w.\-]", "_", canonical)[:100]
    candidate = DATASET / "images" / sanitized
    return candidate if candidate.exists() else None


def add_local_gold_items(
    directory: Path,
    accepted: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    verification: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    manifest = load_collection_manifest(directory, "gold")
    existing_titles = {item["title"] for item in manifest["items"]}
    existing_hashes = {item["sha256"] for item in manifest["items"]}
    missing: list[dict[str, Any]] = []
    added = 0
    for legacy in accepted:
        title = canonical_title_from_source(legacy)
        if title in existing_titles:
            continue
        source_path = find_legacy_image(legacy)
        info = metadata.get(title)
        if source_path is None or info is None:
            missing.append(legacy)
            continue
        content = source_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in existing_hashes:
            continue
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image_format = (image.format or "PNG").upper()
        mime = "image/jpeg" if image_format in {"JPEG", "JPG"} else "image/png"
        filename = stable_filename(title, mime)
        destination = directory / "images" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        record = {
            **info,
            "file": f"images/{filename}",
            "stored_width": width,
            "stored_height": height,
            "stored_bytes": len(content),
            "sha256": digest,
            "download_url": "legacy_local_copy",
            "downloaded_at": utc_now(),
            "discovery_categories": [legacy.get("category", "legacy collection")],
            "tier": "gold",
            "verification": verification,
            "annotation_status": "unannotated",
            "license_verification": "metadata_only_not_legally_verified",
            "legacy_screening": legacy.get("screening"),
        }
        manifest["items"].append(record)
        existing_titles.add(title)
        existing_hashes.add(digest)
        added += 1
        print(f"GOLD LOCAL {added:3d} {title[:100]}", flush=True)
    save_collection_manifest(directory, manifest)
    return added, missing


def add_items(
    session: requests.Session,
    directory: Path,
    tier: str,
    infos: list[dict[str, Any]],
    discoveries: dict[str, set[str]],
    discovery_seeds: dict[str, set[str]],
    maximum: int | None,
    verification: dict[str, Any],
    enforce_license: bool,
) -> tuple[int, int]:
    manifest = load_collection_manifest(directory, tier)
    images_dir = directory / "images"
    existing_titles = {item["title"] for item in manifest["items"]}
    existing_hashes = {item["sha256"] for item in manifest["items"]}
    added = skipped = 0
    for info in infos:
        if maximum is not None and added >= maximum:
            break
        title = info["title"]
        if title in existing_titles:
            skipped += 1
            continue
        if info["mime"] not in SUPPORTED_MIMES or not dimension_is_usable(info):
            skipped += 1
            continue
        if enforce_license and not license_is_dataset_friendly(info):
            skipped += 1
            continue
        filename = stable_filename(title, info["mime"])
        try:
            digest, width, height, size, download_url = download_image(
                session, info, images_dir / filename
            )
            if digest in existing_hashes:
                (images_dir / filename).unlink(missing_ok=True)
                skipped += 1
                continue
            item = {
                **info,
                "file": f"images/{filename}",
                "stored_width": width,
                "stored_height": height,
                "stored_bytes": size,
                "sha256": digest,
                "download_url": download_url,
                "downloaded_at": utc_now(),
                "discovery_categories": sorted(discoveries.get(title, set())),
                "discovery_seeds": sorted(discovery_seeds.get(title, set())),
                "tier": tier,
                "verification": verification,
                "annotation_status": "unannotated",
                "license_verification": "metadata_only_not_legally_verified",
            }
            manifest["items"].append(item)
            existing_titles.add(title)
            existing_hashes.add(digest)
            added += 1
            print(f"{tier.upper():6} {added:4d} {title[:100]}", flush=True)
            if added % 10 == 0:
                save_collection_manifest(directory, manifest)
        except Exception as exc:  # noqa: BLE001 - item-level collection boundary
            manifest["failures"].append(
                {"title": title, "at": utc_now(), "error": str(exc)[:500]}
            )
            print(f"WARN download {title}: {exc}", flush=True)
    save_collection_manifest(directory, manifest)
    return added, skipped


def collect_gold(session: requests.Session) -> tuple[int, int]:
    legacy = json.loads(LEGACY_MANIFEST.read_text(encoding="utf-8"))
    accepted = [
        item
        for item in legacy.get("items", [])
        if item.get("screening", {}).get("accepted")
        and item.get("title") not in EXCLUDED_GOLD_TITLES
    ]
    print(f"GOLD SOURCE: {len(accepted)} owner-approved legacy records", flush=True)
    canonical_titles = [canonical_title_from_source(item) for item in accepted]
    infos = file_info(session, canonical_titles)
    print(f"GOLD METADATA: resolved {len(infos)} Wikimedia file records", flush=True)
    metadata = {info["title"]: info for info in infos}
    verification = {
        "level": "scope_verified_by_project_owner",
        "verified_at": "2026-07-15",
        "note": "Map accepted as suitable; detailed annotations still pending",
    }
    local_added, missing = add_local_gold_items(
        GOLD_DIR, accepted, metadata, verification
    )
    print(f"GOLD LOCAL: {local_added} added; {len(missing)} require download", flush=True)
    missing_titles = {canonical_title_from_source(item) for item in missing}
    missing_infos = [info for info in infos if info["title"] in missing_titles]
    discoveries = {
        canonical_title_from_source(item): {item.get("category", "legacy collection")}
        for item in accepted
    }
    remote_added, skipped = add_items(
        session=session,
        directory=GOLD_DIR,
        tier="gold",
        infos=missing_infos,
        discoveries=discoveries,
        discovery_seeds={title: {"legacy manifest"} for title in discoveries},
        maximum=None,
        verification=verification,
        enforce_license=False,
    )
    curation = {
        "schema_version": "1.0",
        "recorded_at": utc_now(),
        "legacy_accepted_as_gold": [canonical_title_from_source(item) for item in accepted],
        "excluded": EXCLUDED_GOLD_TITLES,
        "note": "No legacy source images were deleted or modified.",
    }
    (DATASET / "curation.json").write_text(
        json.dumps(curation, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return local_added + remote_added, skipped


def collect_silver(
    session: requests.Session,
    maximum: int,
    per_seed: int,
    depth: int,
    refresh_discovery: bool,
) -> tuple[int, int]:
    discovery_path = SILVER_DIR / "discovered_candidates.json"
    if discovery_path.exists() and not refresh_discovery:
        cached = json.loads(discovery_path.read_text(encoding="utf-8"))
        discoveries = {key: set(value) for key, value in cached["categories"].items()}
        discovery_seeds = {key: set(value) for key, value in cached["seeds"].items()}
        print(f"DISCOVERY CACHE: {len(discoveries)} candidates", flush=True)
    else:
        discoveries, discovery_seeds = discover_titles(
            session, SILVER_CATEGORY_SEEDS, per_seed, depth
        )
        SILVER_DIR.mkdir(parents=True, exist_ok=True)
        discovery_path.write_text(
            json.dumps(
                {
                    "created_at": utc_now(),
                    "categories": {key: sorted(value) for key, value in discoveries.items()},
                    "seeds": {key: sorted(value) for key, value in discovery_seeds.items()},
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    excluded_titles: set[str] = set(EXCLUDED_GOLD_TITLES)
    if LEGACY_MANIFEST.exists():
        legacy = json.loads(LEGACY_MANIFEST.read_text(encoding="utf-8"))
        excluded_titles.update(item["title"] for item in legacy.get("items", []))
    if (GOLD_DIR / "manifest.json").exists():
        gold = json.loads((GOLD_DIR / "manifest.json").read_text(encoding="utf-8"))
        excluded_titles.update(item["title"] for item in gold.get("items", []))
    by_seed = {
        seed: sorted(
            title
            for title, seeds in discovery_seeds.items()
            if seed in seeds and title not in excluded_titles
        )
        for seed in SILVER_CATEGORY_SEEDS
    }
    # Round-robin the seed queues so broad climate/geology categories cannot
    # consume the complete first batch.
    ordered_titles: list[str] = []
    seen_ordered: set[str] = set()
    longest = max((len(items) for items in by_seed.values()), default=0)
    for index in range(longest):
        for seed in SILVER_CATEGORY_SEEDS:
            if index < len(by_seed[seed]):
                title = by_seed[seed][index]
                if title not in seen_ordered:
                    ordered_titles.append(title)
                    seen_ordered.add(title)
    # Query metadata for a balanced oversample only. This is enough to replace
    # candidates rejected for dimensions/license while avoiding hundreds of
    # unnecessary API calls.
    candidate_budget = min(len(ordered_titles), max(maximum * 2, maximum + 80))
    infos = file_info(session, ordered_titles[:candidate_budget])
    info_by_title = {item["title"]: item for item in infos}
    infos = [info_by_title[title] for title in ordered_titles if title in info_by_title]
    return add_items(
        session=session,
        directory=SILVER_DIR,
        tier="silver",
        infos=infos,
        discoveries=discoveries,
        discovery_seeds=discovery_seeds,
        maximum=maximum,
        verification={
            "level": "unverified_candidate",
            "note": "No semantic/VLM screening was run during collection",
        },
        enforce_license=True,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", action="store_true", help="rebuild the owner-verified Gold pool")
    parser.add_argument("--silver", action="store_true", help="collect unverified Silver candidates")
    parser.add_argument("--silver-max", type=int, default=250)
    parser.add_argument("--per-seed", type=int, default=80)
    parser.add_argument("--category-depth", type=int, default=2)
    parser.add_argument(
        "--refresh-discovery", action="store_true", help="ignore the cached Silver category crawl"
    )
    args = parser.parse_args()
    if not args.gold and not args.silver:
        parser.error("choose --gold, --silver, or both")
    if args.silver_max < 1 or args.per_seed < 1 or args.category_depth < 0:
        parser.error("collection limits must be positive and depth non-negative")

    session = requests.Session()
    session.headers.update(HEADERS)
    if args.gold:
        added, skipped = collect_gold(session)
        print(f"GOLD COMPLETE: {added} added, {skipped} skipped", flush=True)
    if args.silver:
        added, skipped = collect_silver(
            session,
            args.silver_max,
            args.per_seed,
            args.category_depth,
            args.refresh_discovery,
        )
        print(f"SILVER COMPLETE: {added} added, {skipped} skipped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
