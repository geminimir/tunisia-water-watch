"""One-off historical backfill.

For each dam and each month since --since (default 2020-01), query STAC for the
lowest-cloud Sentinel-2 L2A scene in that month, compute NDWI, and append a
reading to data/readings.csv. Idempotent on (dam_id, scene_id): a second run
adds nothing.

Usage:
    python scripts/backfill.py [--since 2020-01] [--until 2026-08]
                               [--only sidi-salem,sejnane]
                               [--per-month 1]

The script is designed to be run locally (typically ~1-2 hours for 5 years of
history across 37 dams) but is safe to run in CI as workflow_dispatch too.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute import read_dam  # noqa: E402
from fetch import STAC_ENDPOINTS, Scene, _sign_planetary_computer, fetch_bands  # noqa: E402
from main import CSV_HEADER, READINGS_CSV, _ensure_csv  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill")

DAMS_JSON = ROOT / "config" / "dams.json"
REQUEST_TIMEOUT = 30.0


def _month_iter(since: str, until: str):
    start = datetime.strptime(since, "%Y-%m")
    stop = datetime.strptime(until, "%Y-%m")
    y, m = start.year, start.month
    while (y, m) <= (stop.year, stop.month):
        yield y, m
        m += 1
        if m == 13:
            m, y = 1, y + 1


def _search_month(endpoint: dict, bbox: list[float], year: int, month: int) -> Scene | None:
    start = f"{year:04d}-{month:02d}-01T00:00:00Z"
    last_day = monthrange(year, month)[1]
    end = f"{year:04d}-{month:02d}-{last_day:02d}T23:59:59Z"
    payload = {
        "collections": [endpoint["collection"]],
        "bbox": bbox,
        "datetime": f"{start}/{end}",
        "limit": 20,
        "query": {"eo:cloud_cover": {"lt": 40}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    try:
        r = httpx.post(f"{endpoint['url']}/search", json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("STAC %s failed for %d-%02d: %s", endpoint["name"], year, month, exc)
        return None
    features = r.json().get("features", [])
    keys = endpoint["asset_keys"]
    sign = endpoint.get("sign", False)
    for feat in features:
        assets = feat.get("assets", {})
        try:
            green = assets[keys["green"]]["href"]
            nir = assets[keys["nir"]]["href"]
            scl = assets[keys["scl"]]["href"]
        except KeyError:
            continue
        if sign:
            green = _sign_planetary_computer(green)
            nir = _sign_planetary_computer(nir)
            scl = _sign_planetary_computer(scl)
        return Scene(
            endpoint=endpoint["name"],
            scene_id=feat.get("id", ""),
            datetime=feat.get("properties", {}).get("datetime", ""),
            cloud_cover=float(feat.get("properties", {}).get("eo:cloud_cover", 0.0)),
            green_href=green, nir_href=nir, scl_href=scl,
        )
    return None


def find_month_scene(bbox: list[float], year: int, month: int) -> Scene | None:
    for endpoint in STAC_ENDPOINTS:
        s = _search_month(endpoint, bbox, year, month)
        if s is not None:
            return s
    return None


def _existing_scene_ids() -> set[tuple[str, str]]:
    if not READINGS_CSV.exists():
        return set()
    seen: set[tuple[str, str]] = set()
    with READINGS_CSV.open() as fh:
        for r in csv.DictReader(fh):
            seen.add((r["dam_id"], r.get("scene_id", "")))
    return seen


def _existing_dam_dates() -> set[tuple[str, str]]:
    """Dam-id + YYYY-MM for dedup by month (in addition to scene_id dedup)."""
    if not READINGS_CSV.exists():
        return set()
    seen: set[tuple[str, str]] = set()
    with READINGS_CSV.open() as fh:
        for r in csv.DictReader(fh):
            d = r.get("date", "")
            if len(d) >= 7 and r.get("surface_area_km2"):
                seen.add((r["dam_id"], d[:7]))
    return seen


def _append_row(row: dict) -> None:
    with READINGS_CSV.open("a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=CSV_HEADER).writerow(row)


def backfill(since: str, until: str, only: list[str] | None, per_month: int = 1) -> None:
    _ensure_csv()
    with DAMS_JSON.open() as fh:
        dams = json.load(fh)["dams"]
    if only:
        dams = [d for d in dams if d["id"] in only]
    seen_scenes = _existing_scene_ids()
    seen_months = _existing_dam_dates()

    total = 0
    added = 0
    skipped = 0
    for dam in dams:
        for year, month in _month_iter(since, until):
            key = (dam["id"], f"{year:04d}-{month:02d}")
            if key in seen_months:
                continue
            scene = find_month_scene(dam["bbox"], year, month)
            total += 1
            if scene is None:
                log.info("%s %04d-%02d: no scene", dam["id"], year, month)
                continue
            scene_key = (dam["id"], scene.scene_id)
            if scene_key in seen_scenes:
                continue
            try:
                bands = fetch_bands(scene, dam["bbox"])
                green, pxa = bands["green"]
                nir, _ = bands["nir"]
                scl, _ = bands["scl"]
                reading = read_dam(green, pxa, nir, scl, dam["ndwi_threshold"])
            except Exception as exc:  # noqa: BLE001
                log.warning("%s %04d-%02d compute failed: %s", dam["id"], year, month, exc)
                continue
            reading_date = scene.datetime[:10] if scene.datetime else f"{year:04d}-{month:02d}-15"
            row = {
                "date": reading_date, "dam_id": dam["id"], "dam_name": dam["name"],
                "surface_area_km2": (
                    f"{reading.surface_area_km2:.3f}"
                    if reading.surface_area_km2 is not None else ""
                ),
                "cloud_pct": f"{reading.cloud_pct:.1f}",
                "scene_id": scene.scene_id,
                "threshold_used": dam["ndwi_threshold"],
                "historical_avg_km2": dam["historical_avg_km2"],
                "pct_of_avg": (
                    f"{100.0 * reading.surface_area_km2 / dam['historical_avg_km2']:.1f}"
                    if reading.surface_area_km2 is not None and dam["historical_avg_km2"] else ""
                ),
            }
            _append_row(row)
            seen_scenes.add(scene_key)
            seen_months.add(key)
            if row["surface_area_km2"]:
                added += 1
                log.info(
                    "%s %s: %.2f km² (cloud %.0f%%)",
                    dam["id"], reading_date, reading.surface_area_km2, reading.cloud_pct,
                )
            else:
                skipped += 1
                log.info("%s %s skipped (cloud %.0f%%)", dam["id"], reading_date, reading.cloud_pct)
    log.info("backfill complete: %d attempted, %d added, %d skipped", total, added, skipped)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2020-01", help="YYYY-MM inclusive")
    ap.add_argument("--until", default=None, help="YYYY-MM inclusive (default: last full month)")
    ap.add_argument("--only", default=None, help="Comma-separated dam ids to backfill")
    args = ap.parse_args()

    if args.until is None:
        now = datetime.now(timezone.utc)
        month = now.month - 1 if now.month > 1 else 12
        year = now.year if now.month > 1 else now.year - 1
        args.until = f"{year:04d}-{month:02d}"
    only = args.only.split(",") if args.only else None
    backfill(args.since, args.until, only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
