"""One-off historical backfill for governorate NDVI/NDMI readings.

Mirrors scripts/backfill.py but reads red/NIR/SWIR + SCL over the
representative agricultural bbox of each governorate. Idempotent on
(gov_id, YYYY-MM).
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

from compute import read_governorate  # noqa: E402
from fetch import (  # noqa: E402
    STAC_ENDPOINTS, Scene, _sign_planetary_computer, fetch_bands,
)
from govs import GOV_CSV, GOV_CSV_HEADER, _ensure_csv  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill-govs")

GOV_JSON = ROOT / "config" / "governorates.json"
REQUEST_TIMEOUT = 30.0


def _month_iter(since: str, until: str):
    y, m = map(int, since.split("-"))
    ye, me = map(int, until.split("-"))
    while (y, m) <= (ye, me):
        yield y, m
        m = 1 if m == 12 else m + 1
        y = y + 1 if m == 1 else y


def _search_month(endpoint: dict, bbox: list[float], year: int, month: int) -> Scene | None:
    last_day = monthrange(year, month)[1]
    payload = {
        "collections": [endpoint["collection"]],
        "bbox": bbox,
        "datetime": f"{year:04d}-{month:02d}-01T00:00:00Z/{year:04d}-{month:02d}-{last_day:02d}T23:59:59Z",
        "limit": 20,
        "query": {"eo:cloud_cover": {"lt": 30}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    try:
        r = httpx.post(f"{endpoint['url']}/search", json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("STAC %s failed %d-%02d: %s", endpoint["name"], year, month, exc)
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
        red = assets.get(keys.get("red", ""), {}).get("href", "")
        swir = assets.get(keys.get("swir16", ""), {}).get("href", "")
        if sign:
            green = _sign_planetary_computer(green)
            nir = _sign_planetary_computer(nir)
            scl = _sign_planetary_computer(scl)
            if red: red = _sign_planetary_computer(red)
            if swir: swir = _sign_planetary_computer(swir)
        return Scene(
            endpoint=endpoint["name"],
            scene_id=feat.get("id", ""),
            datetime=feat.get("properties", {}).get("datetime", ""),
            cloud_cover=float(feat.get("properties", {}).get("eo:cloud_cover", 0.0)),
            green_href=green, nir_href=nir, scl_href=scl,
            red_href=red, swir16_href=swir,
        )
    return None


def find_month_scene(bbox: list[float], year: int, month: int) -> Scene | None:
    for endpoint in STAC_ENDPOINTS:
        s = _search_month(endpoint, bbox, year, month)
        if s is not None:
            return s
    return None


def _existing_months() -> set[tuple[str, str]]:
    if not GOV_CSV.exists():
        return set()
    seen: set[tuple[str, str]] = set()
    with GOV_CSV.open() as fh:
        for r in csv.DictReader(fh):
            d = r.get("date", "")
            if len(d) >= 7 and r.get("mean_ndvi"):
                seen.add((r["gov_id"], d[:7]))
    return seen


def _append(row: dict) -> None:
    with GOV_CSV.open("a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=GOV_CSV_HEADER).writerow(row)


def backfill(since: str, until: str, only: list[str] | None) -> None:
    _ensure_csv()
    with GOV_JSON.open() as fh:
        govs = json.load(fh)["governorates"]
    if only:
        govs = [g for g in govs if g["id"] in only]
    seen = _existing_months()
    added = 0
    skipped = 0
    for gov in govs:
        for year, month in _month_iter(since, until):
            key = (gov["id"], f"{year:04d}-{month:02d}")
            if key in seen:
                continue
            scene = find_month_scene(gov["bbox"], year, month)
            if scene is None:
                log.info("%s %04d-%02d: no scene", gov["id"], year, month)
                continue
            try:
                bands = fetch_bands(scene, gov["bbox"], want=("red", "nir", "swir16", "scl"))
                red = bands.get("red")
                nir = bands.get("nir")
                if red is None or nir is None:
                    continue
                reading = read_governorate(
                    red[0], red[1], nir[0],
                    bands["swir16"][0] if "swir16" in bands else None,
                    bands["scl"][0] if "scl" in bands else None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s %04d-%02d compute failed: %s", gov["id"], year, month, exc)
                continue
            date_s = scene.datetime[:10] if scene.datetime else f"{year:04d}-{month:02d}-15"
            row = {
                "date": date_s, "gov_id": gov["id"], "gov_name": gov["name"],
                "region": gov["region"],
                "mean_ndvi": f"{reading.mean_ndvi:.4f}" if reading.mean_ndvi is not None else "",
                "std_ndvi": f"{reading.std_ndvi:.4f}" if reading.std_ndvi is not None else "",
                "mean_ndmi": f"{reading.mean_ndmi:.4f}" if reading.mean_ndmi is not None else "",
                "healthy_pct": f"{reading.healthy_pct:.2f}" if reading.healthy_pct is not None else "",
                "cloud_pct": f"{reading.cloud_pct:.1f}",
                "confidence": f"{reading.confidence:.3f}" if reading.confidence is not None else "",
                "scene_id": scene.scene_id,
            }
            _append(row)
            seen.add(key)
            if row["mean_ndvi"]:
                added += 1
                log.info(
                    "%s %s: NDVI=%.3f NDMI=%s healthy=%.0f%% cloud=%.0f%%",
                    gov["id"], date_s, reading.mean_ndvi,
                    f"{reading.mean_ndmi:.3f}" if reading.mean_ndmi is not None else "—",
                    reading.healthy_pct or 0.0, reading.cloud_pct,
                )
            else:
                skipped += 1
    log.info("gov backfill: %d added, %d skipped", added, skipped)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2022-01")
    ap.add_argument("--until", default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    if args.until is None:
        now = datetime.now(timezone.utc)
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        args.until = f"{y:04d}-{m:02d}"
    only = args.only.split(",") if args.only else None
    backfill(args.since, args.until, only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
