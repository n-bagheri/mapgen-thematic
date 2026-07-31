# MapGen Thematic Map Dataset (working name)

A curated corpus of modern classed thematic maps (area-class / chorochromatic and
classed-sequential), collected for research on automated tactile map production.
Companion to the MapGen pipeline in this repository; every accepted map can be
processed into the full annotation stack (semantics, legend colors, overlay text,
class segmentation, generalized vectors, tactile symbol assignment).

## Motivation

Existing map-understanding benchmarks (ICDAR MapText, ICDAR-21 MapSeg, USGS/DARPA
geologic corpora) target historical, cadastral, or geologic maps. No public dataset
covers modern classed thematic maps with legend/class annotations — and none pairs
source maps with tactile adaptations. This corpus addresses both gaps.

## Composition

- `gold/images/` and `gold/manifest.json` — maps accepted as suitable by the
  project owner, with normalized filenames, Wikimedia revision/provenance,
  stored dimensions, license metadata, and SHA-256 hashes. Gold currently
  means **scope-verified but unannotated**; it does not yet mean pixel-level
  ground truth.
- `silver/images/` and `silver/manifest.json` — broad, unverified candidates
  collected without semantic/VLM screening. Both likely positives and useful
  out-of-scope negatives are intentionally retained for the later filtering
  pass.
- `silver/discovered_candidates.json` — cached category-crawl results, making
  collection resume-safe without repeatedly traversing Commons.
- `curation.json` — owner review decisions and explicit exclusions.
- `images/` and `manifest.json` — the original legacy collection. These are
  retained unchanged for reproducibility.
- The legacy `manifest.json` contains one record per **screened** candidate:
  source URL, license, artist, Commons category, and the screening verdict
  (map type, data ordering, legend presence, class count, subject, reason).
  Rejected candidates keep their record (screening precision is measurable)
  but not their image file.
- Pipeline annotations for processed maps live under `../runs/<image-stem>/`
  (`step1_semantics.json`, `classes.json`, `labels.json`, `regions_gen.geojson`,
  `aggregation.json`, `symbols.json`, `step7_tactile.png`).

## Collection process

1. **Category crawl, not keyword search**: candidates come from human-curated
   Wikimedia Commons category trees (climate classification, land use, vegetation,
   biomes, geology, precipitation, temperature, ecology, ecoregions, linguistic,
   and ethnographic maps). Keyword search was evaluated and rejected — it returns
   photographs and non-maps. The provenance-first collector stores 1280 px
   Commons derivatives for Silver screening while retaining original URLs and
   dimensions in the manifest.
2. **Silver collection**: collection itself performs only technical and license
   metadata checks. It does not run the semantic model:

   `python dataset_tools/collect_maps.py --silver --silver-max 150 --per-seed 40 --category-depth 1`

3. **Automated screening (later pass)**: every candidate can be interpreted by the pipeline's
   Step 1 (VLM semantic interpretation, model recorded in the manifest).
   Acceptance criteria: in-scope map type (area-class chorochromatic or
   classed-sequential) AND a legend present.
4. **Human checkpoints**: maps processed through the pipeline receive human
   review at Checkpoints A/B (class list, segmentation), turning machine
   annotations into verified ground truth.

## Licensing

All items originate from Wikimedia Commons; the per-item license is recorded in
the manifest (`license`, `artist`, `source`). Only use items whose license terms
you can satisfy; public-domain and CC-BY(-SA) dominate.

## Intended tasks

1. Legend extraction (swatch geometry + color ↔ label pairing).
2. Overlay text detection & classification (comparable to MapText protocols).
3. Class segmentation against legend-seeded colors.
4. Minimum-size generalization / class aggregation quality.
5. **Tactile constraint audit** — validity of produced tactile masters against
   physical constants (novel task defined by this project).

## Status / TODO

- [x] Establish 28 owner-verified Gold maps; exclude the Yangon districts map.
- [x] Gather the first 150-map unverified Silver batch across category groups.
- [ ] Run the automatic verifier on Silver and promote accepted items to Gold.
- [ ] Synthetic ground-truth subset rendered from redistribution-friendly GIS
      data (Natural Earth / ESA WorldCover / USGS NLCD) with exact polygons
      and legends.
- [ ] Human verification pass over all accepted items (Checkpoint A).
- [ ] Train/val/test splits + baseline numbers from the MapGen pipeline.
- [ ] Full datasheet (Gebru et al.) before publication; DOI via Zenodo/HF.
