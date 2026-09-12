"""Bbox recalibration via OpenStreetMap Overpass API.

For each dam in config/dams.json, query OSM for `natural=water` or
`landuse=reservoir` polygons within N kilometers of the configured lat/lon,
then compute the union bbox of those polygons with a 200 m buffer. Prints a
diff so an operator can decide whether to update the config.

Overpass is public, free, and requires no auth. Rate limit: keep queries
sequential with a 1-second sleep. The script writes suggestions to
config/dams.recalibrated.json (a proposal file) so the operator has final say.

Usage:
    python scripts/recalibrate.py [--only sejnane,joumine] [--radius-km 8]
    # inspect config/dams.recalibrated.json, then merge if it looks right.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("recalibrate")

ROOT = Path(__file__).resolve().parent.parent
DAMS_JSON = ROOT / "config" / "dams.json"
OUTPUT_JSON = ROOT / "config" / "dams.recalibrated.json"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
BUFFER_DEG = 0.002  # ~200 m


def _km_to_deg_lat(km: float) -> float:
    return km / 111.32


def _km_to_deg_lon(km: float, lat: float) -> float:
    return km / (111.32 * math.cos(math.radians(lat)))


def _query(overpass: str, lat: float, lon: float, radius_km: float) -> dict | None:
    dlat = _km_to_deg_lat(radius_km)
    dlon = _km_to_deg_lon(radius_km, lat)
    bbox = f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"
    query = f"""
    [out:json][timeout:30];
    (
      way[natural=water]({bbox});
      way[landuse=reservoir]({bbox});
      way[water=reservoir]({bbox});
      relation[natural=water]({bbox});
      relation[landuse=reservoir]({bbox});
    );
    out geom;
    """
    try:
        r = httpx.post(
            overpass,
            data={"data": query},
            headers={
                "User-Agent": "tunisia-water-watch/1.0 (github.com/geminimir/water-watch)",
                "Accept": "application/json",
            },
            timeout=90.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        log.warning("overpass %s failed: %s", overpass, exc)
        return None


def _union_bbox(features: list[dict]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for f in features:
        geom = f.get("geometry", [])
        for pt in geom:
            xs.append(pt["lon"])
            ys.append(pt["lat"])
        for m in f.get("members", []):
            for pt in m.get("geometry", []):
                xs.append(pt["lon"])
                ys.append(pt["lat"])
    if not xs or not ys:
        return None
    return (min(xs) - BUFFER_DEG, min(ys) - BUFFER_DEG,
            max(xs) + BUFFER_DEG, max(ys) + BUFFER_DEG)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * 111.32
    dlon = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def recalibrate(only: list[str] | None, radius_km: float) -> None:
    dams = json.loads(DAMS_JSON.read_text())["dams"]
    if only:
        dams = [d for d in dams if d["id"] in only]
    suggestions: list[dict] = []
    for dam in dams:
        data = None
        for endpoint in OVERPASS_ENDPOINTS:
            data = _query(endpoint, dam["lat"], dam["lon"], radius_km)
            if data:
                break
            time.sleep(1.0)
        if data is None:
            log.warning("no overpass response for %s", dam["id"])
            continue
        features = [f for f in data.get("elements", [])
                    if f.get("geometry") or f.get("members")]
        # Rank features by size × proximity to the dam center. Pick the single
        # largest water polygon whose centroid is close — this avoids sweeping
        # in nearby lakes or small barrages when we're looking for one reservoir.
        candidates: list[tuple[float, dict]] = []
        for f in features:
            geom = f.get("geometry") or [pt for m in f.get("members", []) for pt in m.get("geometry", [])]
            if len(geom) < 5:
                continue
            xs = [p["lon"] for p in geom]
            ys = [p["lat"] for p in geom]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            dist = _distance_km(dam["lat"], dam["lon"], cy, cx)
            if dist > radius_km:
                continue
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            # Higher score = bigger and closer. Distance penalty rises quickly past 3km.
            score = area / (1.0 + (dist / 3.0) ** 2)
            candidates.append((score, f))
        candidates.sort(key=lambda t: t[0], reverse=True)
        bbox = _union_bbox([candidates[0][1]]) if candidates else None
        near = [candidates[0][1]] if candidates else []
        if bbox is None:
            log.info("%s: no water polygons found within %.1f km", dam["id"], radius_km)
            continue
        w, s, e, n = bbox
        # Only suggest a change if the union bbox differs meaningfully from the configured one.
        existing = dam["bbox"]
        area_new = (e - w) * (n - s)
        area_old = (existing[2] - existing[0]) * (existing[3] - existing[1])
        diff = abs(area_new - area_old) / max(area_old, 1e-6)
        suggestions.append({
            "id": dam["id"],
            "name": dam["name"],
            "current_bbox": existing,
            "suggested_bbox": [round(w, 4), round(s, 4), round(e, 4), round(n, 4)],
            "features_matched": len(near),
            "area_change_pct": round(100.0 * diff, 1),
        })
        log.info(
            "%s: %d water features, suggested [%.4f, %.4f, %.4f, %.4f] (%.0f%% area change)",
            dam["id"], len(near), w, s, e, n, 100.0 * diff,
        )
        time.sleep(1.0)  # be gentle
    OUTPUT_JSON.write_text(json.dumps({"suggestions": suggestions}, indent=2))
    log.info("wrote %d suggestions to %s", len(suggestions), OUTPUT_JSON)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="Comma-separated dam ids")
    ap.add_argument("--radius-km", type=float, default=8.0)
    args = ap.parse_args()
    only = args.only.split(",") if args.only else None
    recalibrate(only, args.radius_km)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
