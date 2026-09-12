"""Entry point: discover scenes, compute NDWI per dam, append CSV, render site."""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute import read_dam  # noqa: E402
from fetch import discover_latest_scene, fetch_bands  # noqa: E402
from render import render_site  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tww")

READINGS_CSV = ROOT / "data" / "readings.csv"
DAMS_JSON = ROOT / "config" / "dams.json"

CSV_HEADER = [
    "date", "dam_id", "dam_name", "surface_area_km2", "cloud_pct",
    "scene_id", "threshold_used", "historical_avg_km2", "pct_of_avg",
]


def _ensure_csv() -> None:
    READINGS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not READINGS_CSV.exists():
        with READINGS_CSV.open("w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADER)


def _already_read_today(dam_id: str, today: str) -> bool:
    if not READINGS_CSV.exists():
        return False
    with READINGS_CSV.open() as fh:
        for r in csv.DictReader(fh):
            if r["dam_id"] == dam_id and r["date"] == today:
                return True
    return False


def _append(row: dict) -> None:
    with READINGS_CSV.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        w.writerow(row)


def process_dam(dam: dict, today: str) -> dict:
    """Process one dam. Returns the CSV row (may have empty surface_area_km2)."""
    row = {
        "date": today, "dam_id": dam["id"], "dam_name": dam["name"],
        "surface_area_km2": "", "cloud_pct": "", "scene_id": "",
        "threshold_used": dam["ndwi_threshold"],
        "historical_avg_km2": dam["historical_avg_km2"],
        "pct_of_avg": "",
    }
    scene = discover_latest_scene(dam["bbox"])
    if scene is None:
        log.warning("no scene for %s", dam["id"])
        return row
    row["scene_id"] = scene.scene_id
    try:
        bands = fetch_bands(scene, dam["bbox"])
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch failed for %s: %s", dam["id"], exc)
        return row
    green, pxa = bands["green"]
    nir, _ = bands["nir"]
    scl, _ = bands["scl"]
    reading = read_dam(green, pxa, nir, scl, dam["ndwi_threshold"])
    row["cloud_pct"] = f"{reading.cloud_pct:.1f}"
    if reading.surface_area_km2 is None:
        log.info("%s skipped: cloud %.0f%%", dam["id"], reading.cloud_pct)
        return row
    row["surface_area_km2"] = f"{reading.surface_area_km2:.3f}"
    if dam["historical_avg_km2"]:
        row["pct_of_avg"] = f"{100.0 * reading.surface_area_km2 / dam['historical_avg_km2']:.1f}"
    log.info(
        "%s: %.2f km² (cloud %.0f%%, scene %s)",
        dam["id"], reading.surface_area_km2, reading.cloud_pct, scene.scene_id,
    )
    return row


def main() -> int:
    _ensure_csv()
    with DAMS_JSON.open() as fh:
        dams = json.load(fh)["dams"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    max_dams = int(os.environ.get("TWW_MAX_DAMS", "0") or 0)
    if max_dams:
        dams = dams[:max_dams]
    read = 0
    skipped = 0
    for dam in dams:
        if _already_read_today(dam["id"], today):
            log.info("%s already read today, skipping", dam["id"])
            continue
        try:
            row = process_dam(dam, today)
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error for %s: %s", dam["id"], exc)
            row = {
                "date": today, "dam_id": dam["id"], "dam_name": dam["name"],
                "surface_area_km2": "", "cloud_pct": "", "scene_id": "",
                "threshold_used": dam["ndwi_threshold"],
                "historical_avg_km2": dam["historical_avg_km2"], "pct_of_avg": "",
            }
        _append(row)
        if row["surface_area_km2"]:
            read += 1
        else:
            skipped += 1
    log.info("run complete: %d read, %d skipped", read, skipped)
    render_site(os.environ.get("GITHUB_REPOSITORY", "geminimir/water-watch"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
