"""Entry point: dams + governorates → CSV → static site."""

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
    "surface_area_mndwi_km2", "confidence",
]


def _ensure_csv() -> None:
    READINGS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not READINGS_CSV.exists():
        with READINGS_CSV.open("w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADER)
        return
    # Backfill missing columns on legacy CSVs.
    with READINGS_CSV.open() as fh:
        rdr = csv.reader(fh)
        header = next(rdr, [])
    missing = [c for c in CSV_HEADER if c not in header]
    if not missing:
        return
    log.info("adding new CSV columns: %s", missing)
    tmp = READINGS_CSV.with_suffix(".csv.tmp")
    with READINGS_CSV.open() as src, tmp.open("w", newline="") as dst:
        rdr = csv.DictReader(src)
        w = csv.DictWriter(dst, fieldnames=CSV_HEADER)
        w.writeheader()
        for row in rdr:
            for m in missing:
                row.setdefault(m, "")
            w.writerow({k: row.get(k, "") for k in CSV_HEADER})
    tmp.replace(READINGS_CSV)


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
        csv.DictWriter(fh, fieldnames=CSV_HEADER).writerow(row)


def _empty_row(dam: dict, today: str) -> dict:
    return {
        "date": today, "dam_id": dam["id"], "dam_name": dam["name"],
        "surface_area_km2": "", "cloud_pct": "", "scene_id": "",
        "threshold_used": dam["ndwi_threshold"],
        "historical_avg_km2": dam["historical_avg_km2"],
        "pct_of_avg": "", "surface_area_mndwi_km2": "", "confidence": "",
    }


def process_dam(dam: dict, today: str) -> dict:
    row = _empty_row(dam, today)
    scene = discover_latest_scene(dam["bbox"])
    if scene is None:
        log.warning("no scene for %s", dam["id"])
        return row
    row["scene_id"] = scene.scene_id
    try:
        bands = fetch_bands(scene, dam["bbox"], want=("green", "nir", "scl", "swir16"))
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch failed for %s: %s", dam["id"], exc)
        return row
    green_t = bands.get("green")
    nir_t = bands.get("nir")
    scl_t = bands.get("scl")
    swir_t = bands.get("swir16")
    if green_t is None or nir_t is None:
        log.warning("missing green/nir for %s", dam["id"])
        return row
    green, pxa = green_t
    nir, _ = nir_t
    scl = scl_t[0] if scl_t else None
    swir = swir_t[0] if swir_t else None
    reading = read_dam(green, pxa, nir, scl, dam["ndwi_threshold"], swir=swir)
    row["cloud_pct"] = f"{reading.cloud_pct:.1f}"
    if reading.confidence is not None:
        row["confidence"] = f"{reading.confidence:.3f}"
    if reading.surface_area_mndwi_km2 is not None:
        row["surface_area_mndwi_km2"] = f"{reading.surface_area_mndwi_km2:.3f}"
    if reading.surface_area_km2 is None:
        log.info("%s skipped: cloud %.0f%%", dam["id"], reading.cloud_pct)
        return row
    row["surface_area_km2"] = f"{reading.surface_area_km2:.3f}"
    if dam["historical_avg_km2"]:
        row["pct_of_avg"] = f"{100.0 * reading.surface_area_km2 / dam['historical_avg_km2']:.1f}"
    log.info(
        "%s: %.2f km² (mndwi %.2f, conf %.2f, cloud %.0f%%, scene %s)",
        dam["id"],
        reading.surface_area_km2,
        reading.surface_area_mndwi_km2 or 0.0,
        reading.confidence or 0.0,
        reading.cloud_pct,
        scene.scene_id,
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
            row = _empty_row(dam, today)
        _append(row)
        if row["surface_area_km2"]:
            read += 1
        else:
            skipped += 1
    log.info("dams complete: %d read, %d skipped", read, skipped)

    # Governorate pass (Phase 2). Import lazily so a broken gov config never
    # blocks the dam pipeline.
    try:
        from govs import run_governorates  # noqa: E402
        run_governorates(today)
    except Exception as exc:  # noqa: BLE001
        log.warning("governorate pass failed: %s", exc)

    render_site(os.environ.get("GITHUB_REPOSITORY", "geminimir/water-watch"))

    # Phase 3: opt-in Telegram alerts. Silent no-op if secrets aren't set.
    try:
        from telegram import maybe_alert  # noqa: E402
        maybe_alert()
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram alerting failed: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
