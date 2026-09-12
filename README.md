# Tunisia Water Watch

[![Update readings](https://github.com/geminimir/tunisia-water-watch/actions/workflows/run.yml/badge.svg)](https://github.com/geminimir/tunisia-water-watch/actions/workflows/run.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An independent, satellite-based water and agriculture monitoring system for Tunisia. Built to run forever with zero maintenance, zero cost, and zero human intervention.

**Dashboard:** https://geminimir.github.io/tunisia-water-watch/

## What it does

Every 5 days, a GitHub Actions cron job queries a public STAC API for the most recent Sentinel-2 scene covering each of Tunisia's 37 major reservoirs. It downloads only the green and near-infrared pixels inside each dam's bounding box (via COG range requests, typically <1 MB per dam), computes the Normalized Difference Water Index (NDWI), counts water pixels, and appends a row to `data/readings.csv`. The static site under `site/` is regenerated from the CSV and served by GitHub Pages.

Total operating cost: **$0/month**.

## Architecture

- **No servers.** GitHub Actions runs the pipeline; GitHub Pages serves the site.
- **No paid services.** Copernicus data is free by EU policy; STAC APIs are public; GitHub Actions and Pages are free for public repos.
- **No expiring credentials.** All data sources are anonymous HTTP; workflow auth uses the built-in `GITHUB_TOKEN`.
- **No external state.** The Git repository IS the codebase, the database, the artifact, and the backup.
- **Graceful degradation.** A cloudy pass or a downed STAC endpoint skips one dam for one cycle; the next clear scene fills the gap.

See [`specs.doc`](specs.doc) for the full design document.

## Repository layout

```
config/dams.json           37 dams: id, name, bbox, NDWI threshold
scripts/fetch.py           STAC discovery + COG band download
scripts/compute.py         NDWI + water-pixel-area math
scripts/render.py          Jinja2 → static HTML
scripts/main.py            Orchestration
templates/                 HTML templates
data/readings.csv          Append-only surface area history (committed)
data/latest.json           Latest reading per dam (machine-readable)
site/                      Generated static site (served by Pages)
.github/workflows/run.yml  Cron trigger, every 5 days
tests/                     Unit tests
```

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run pipeline (network required; downloads Sentinel-2 tiles):
python scripts/main.py

# Limit to N dams for testing:
TWW_MAX_DAMS=2 python scripts/main.py

# Only regenerate the site from existing CSV:
python scripts/render.py

# Run unit tests:
python -m unittest discover tests -v
```

## Contributing

Fork, tune NDWI thresholds for individual dams (`config/dams.json`), add historical baseline calibration, or extend to NDVI-based agricultural monitoring per Phase 2 of the spec.

## License

MIT. Satellite data © ESA / Copernicus. Map tiles © OpenStreetMap contributors.
