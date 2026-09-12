"""Phase 2: governorate vegetation pipeline (NDVI + NDMI cross-referenced).

For each governorate we sample a representative agricultural bbox on the most
recent low-cloud Sentinel-2 L2A scene. We compute NDVI (greenness) and NDMI
(canopy moisture) simultaneously; healthy cropland is defined as pixels where
BOTH NDVI > 0.3 and NDMI > 0.0, which catches the "still green but drying"
early-drought signal that NDVI alone misses.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute import read_governorate  # noqa: E402
from fetch import discover_latest_scene, fetch_bands  # noqa: E402

log = logging.getLogger("tww.govs")

GOV_JSON = ROOT / "config" / "governorates.json"
GOV_CSV = ROOT / "data" / "gov_readings.csv"

GOV_CSV_HEADER = [
    "date", "gov_id", "gov_name", "region",
    "mean_ndvi", "std_ndvi", "mean_ndmi", "healthy_pct",
    "cloud_pct", "confidence", "scene_id",
]


def _ensure_csv() -> None:
    GOV_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not GOV_CSV.exists():
        with GOV_CSV.open("w", newline="") as fh:
            csv.writer(fh).writerow(GOV_CSV_HEADER)


def _already_read_today(gov_id: str, today: str) -> bool:
    if not GOV_CSV.exists():
        return False
    with GOV_CSV.open() as fh:
        for r in csv.DictReader(fh):
            if r["gov_id"] == gov_id and r["date"] == today:
                return True
    return False


def _append(row: dict) -> None:
    with GOV_CSV.open("a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=GOV_CSV_HEADER).writerow(row)


def _empty_row(g: dict, today: str) -> dict:
    return {
        "date": today, "gov_id": g["id"], "gov_name": g["name"],
        "region": g["region"], "mean_ndvi": "", "std_ndvi": "", "mean_ndmi": "",
        "healthy_pct": "", "cloud_pct": "", "confidence": "", "scene_id": "",
    }


def process_gov(gov: dict, today: str) -> dict:
    row = _empty_row(gov, today)
    scene = discover_latest_scene(gov["bbox"])
    if scene is None:
        log.warning("no scene for gov %s", gov["id"])
        return row
    row["scene_id"] = scene.scene_id
    try:
        bands = fetch_bands(scene, gov["bbox"], want=("red", "nir", "swir16", "scl"))
    except Exception as exc:  # noqa: BLE001
        log.warning("gov fetch failed %s: %s", gov["id"], exc)
        return row
    red_t = bands.get("red")
    nir_t = bands.get("nir")
    if red_t is None or nir_t is None:
        log.warning("missing red/nir for gov %s", gov["id"])
        return row
    red, pxa = red_t
    nir, _ = nir_t
    scl = bands["scl"][0] if "scl" in bands else None
    swir = bands["swir16"][0] if "swir16" in bands else None
    reading = read_governorate(red, pxa, nir, swir, scl)
    row["cloud_pct"] = f"{reading.cloud_pct:.1f}"
    if reading.confidence is not None:
        row["confidence"] = f"{reading.confidence:.3f}"
    if reading.mean_ndvi is None:
        log.info("%s skipped: cloud %.0f%%", gov["id"], reading.cloud_pct)
        return row
    row["mean_ndvi"] = f"{reading.mean_ndvi:.4f}"
    row["std_ndvi"] = f"{reading.std_ndvi:.4f}"
    if reading.mean_ndmi is not None:
        row["mean_ndmi"] = f"{reading.mean_ndmi:.4f}"
    if reading.healthy_pct is not None:
        row["healthy_pct"] = f"{reading.healthy_pct:.2f}"
    log.info(
        "%s: NDVI=%.3f NDMI=%s healthy=%.0f%% conf=%.2f cloud=%.0f%%",
        gov["id"], reading.mean_ndvi,
        f"{reading.mean_ndmi:.3f}" if reading.mean_ndmi is not None else "—",
        reading.healthy_pct or 0.0,
        reading.confidence or 0.0,
        reading.cloud_pct,
    )
    return row


def run_governorates(today: str | None = None) -> None:
    _ensure_csv()
    with GOV_JSON.open() as fh:
        govs = json.load(fh)["governorates"]
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    read = 0
    skipped = 0
    for gov in govs:
        if _already_read_today(gov["id"], today):
            continue
        try:
            row = process_gov(gov, today)
        except Exception as exc:  # noqa: BLE001
            log.exception("gov %s failed: %s", gov["id"], exc)
            row = _empty_row(gov, today)
        _append(row)
        if row["mean_ndvi"]:
            read += 1
        else:
            skipped += 1
    log.info("governorates complete: %d read, %d skipped", read, skipped)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_governorates()
