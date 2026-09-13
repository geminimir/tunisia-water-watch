"""NDWI-based bbox recalibration for near-zero dams.

Identifies dams that have been reading near-zero surface area for several
passes running, expands the search window around their configured center
coordinates, finds the largest contiguous water body on a recent Sentinel-2
scene, and derives a corrected bbox. Results are validated against wet-season
and dry-season imagery to distinguish real reservoirs from noise.

Output:
    config/dams_recalibrated.json  — copy of dams.json with updated bboxes
                                     for dams that were successfully recalibrated.
    stdout                          — a summary report per dam.

The script never overwrites config/dams.json directly. An operator reviews
the diff, applies it manually, then re-runs the main pipeline.

Runtime: ~15-30 minutes for ~20 candidate dams, dominated by STAC queries
and COG windowed reads. No new dependencies — reuses fetch.py and compute.py.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import sys
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import rasterio
from rasterio.features import shapes as rio_shapes
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute import compute_ndwi  # noqa: E402
from fetch import (  # noqa: E402
    STAC_ENDPOINTS, Scene, _sign_planetary_computer, read_window,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("calibrate")

DAMS_JSON = ROOT / "config" / "dams.json"
READINGS_CSV = ROOT / "data" / "readings.csv"
OUTPUT_JSON = ROOT / "config" / "dams_recalibrated.json"

# Tunables (kept as constants; the task description locks specific values)
NEAR_ZERO_KM2 = 0.05
LOOKBACK_READINGS = 6
NEAR_ZERO_MIN_HITS = 5
SEARCH_BUFFER_DEG = 0.05           # ~5.5 km
DISCOVERY_NDWI_THRESHOLD = 0.0
MIN_WATER_PIXELS = 10
PIXEL_BUFFER = 20                  # ~200 m at 10 m/px
VALIDATION_MIN_KM2 = 0.1
HTTP_TIMEOUT = 10.0
STAC_CLOUD_MAX = 30.0


# -----------------------------------------------------------------------------
# Step 1 — identify candidates from history
# -----------------------------------------------------------------------------

def load_readings_by_dam() -> dict[str, list[dict]]:
    if not READINGS_CSV.exists():
        return {}
    per_dam: dict[str, list[dict]] = {}
    with READINGS_CSV.open() as fh:
        for r in csv.DictReader(fh):
            per_dam.setdefault(r["dam_id"], []).append(r)
    for k in per_dam:
        per_dam[k].sort(key=lambda x: x.get("date", ""))
    return per_dam


def candidate_dams(dams: list[dict], per_dam: dict[str, list[dict]]) -> list[dict]:
    out: list[dict] = []
    for d in dams:
        readings = per_dam.get(d["id"], [])[-LOOKBACK_READINGS:]
        if len(readings) < 1:
            continue
        hits = 0
        for r in readings:
            v = r.get("surface_area_km2", "")
            try:
                area = float(v) if v else 0.0
            except ValueError:
                area = 0.0
            if not v or area < NEAR_ZERO_KM2:
                hits += 1
        if hits >= NEAR_ZERO_MIN_HITS and len(readings) >= NEAR_ZERO_MIN_HITS:
            out.append(d)
    return out


# -----------------------------------------------------------------------------
# Step 2 — STAC scene discovery over an expanded bbox
# -----------------------------------------------------------------------------

def _search_stac(endpoint: dict, bbox: list[float], datetime_range: str) -> Scene | None:
    payload = {
        "collections": [endpoint["collection"]],
        "bbox": bbox,
        "datetime": datetime_range,
        "limit": 20,
        "query": {"eo:cloud_cover": {"lt": STAC_CLOUD_MAX}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    try:
        r = httpx.post(f"{endpoint['url']}/search", json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("STAC %s: %s", endpoint["name"], exc)
        return None
    features = r.json().get("features", [])
    keys = endpoint["asset_keys"]
    sign = endpoint.get("sign", False)
    for feat in features:
        assets = feat.get("assets", {})
        try:
            green = assets[keys["green"]]["href"]
            nir = assets[keys["nir"]]["href"]
        except KeyError:
            continue
        scl = assets.get(keys.get("scl", ""), {}).get("href", "")
        if sign:
            green = _sign_planetary_computer(green)
            nir = _sign_planetary_computer(nir)
            if scl:
                scl = _sign_planetary_computer(scl)
        return Scene(
            endpoint=endpoint["name"],
            scene_id=feat.get("id", ""),
            datetime=feat.get("properties", {}).get("datetime", ""),
            cloud_cover=float(feat.get("properties", {}).get("eo:cloud_cover", 0.0)),
            green_href=green, nir_href=nir, scl_href=scl,
        )
    return None


def _range_recent(days: int = 30) -> str:
    now = datetime.now(timezone.utc)
    start = now.replace(microsecond=0) - _td(days)
    return f"{start.isoformat().replace('+00:00', 'Z')}/{now.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}"


def _range_month(year: int, month: int) -> str:
    last = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01T00:00:00Z/{year:04d}-{month:02d}-{last:02d}T23:59:59Z"


def _td(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def find_scene(bbox: list[float], datetime_range: str) -> Scene | None:
    for endpoint in STAC_ENDPOINTS:
        s = _search_stac(endpoint, bbox, datetime_range)
        if s is not None:
            return s
    return None


# -----------------------------------------------------------------------------
# Step 3 & 4 — NDWI + connected components + pick largest cluster
# -----------------------------------------------------------------------------

@dataclass
class WaterFinding:
    scene: Scene
    ndwi_area_km2: float          # area with the discovery threshold
    cluster_px_count: int         # size of the largest connected water cluster
    corrected_bbox: list[float] | None
    corrected_area_km2: float | None


def _largest_water_cluster_bbox(
    cog_href: str, expanded_bbox: list[float], reference_nir: bool,
) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float, float], float, "rasterio.Affine", str] | None:
    """Read the green (or NIR) window and return arrays + window bounds + pixel_area + transform + crs."""
    with rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.jp2",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    ):
        with rasterio.open(cog_href) as src:
            src_bounds = transform_bounds("EPSG:4326", src.crs, *expanded_bbox, densify_pts=21)
            win = window_from_bounds(*src_bounds, transform=src.transform).round_offsets().round_lengths()
            data = src.read(1, window=win)
            xres, yres = src.res
            pxa = float(abs(xres * yres))
            win_transform = window_transform(win, src.transform)
            return data, None, src_bounds, pxa, win_transform, src.crs.to_string()


def _read_pair(scene: Scene, bbox: list[float]) -> tuple[np.ndarray, np.ndarray, "rasterio.Affine", str, float] | None:
    """Return (green, nir, window_transform, source_crs, pixel_area_m2)."""
    with rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.jp2",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    ):
        with rasterio.open(scene.green_href) as src_g:
            src_bounds = transform_bounds("EPSG:4326", src_g.crs, *bbox, densify_pts=21)
            win = window_from_bounds(*src_bounds, transform=src_g.transform).round_offsets().round_lengths()
            green = src_g.read(1, window=win)
            xres, yres = src_g.res
            pxa = float(abs(xres * yres))
            win_transform = window_transform(win, src_g.transform)
            crs = src_g.crs.to_string()
        with rasterio.open(scene.nir_href) as src_n:
            n_bounds = transform_bounds("EPSG:4326", src_n.crs, *bbox, densify_pts=21)
            n_win = window_from_bounds(*n_bounds, transform=src_n.transform).round_offsets().round_lengths()
            nir = src_n.read(1, window=n_win)
    # match shapes defensively
    if nir.shape != green.shape:
        from compute import _nearest_resample
        nir = _nearest_resample(nir, green.shape)
    return green, nir, win_transform, crs, pxa


def _polygon_pixel_area(coords: list[list[float]]) -> float:
    """Shoelace area for a polygon in pixel coordinates."""
    n = len(coords)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) * 0.5


def _shape_pixel_area(geom: dict) -> float:
    """Sum ring pixel-areas (exterior minus holes) for a Polygon GeoJSON dict."""
    if geom.get("type") != "Polygon":
        return 0.0
    rings = geom.get("coordinates", [])
    if not rings:
        return 0.0
    ext = _polygon_pixel_area(rings[0])
    holes = sum(_polygon_pixel_area(r) for r in rings[1:])
    return max(0.0, ext - holes)


def _shape_pixel_bounds(geom: dict) -> tuple[float, float, float, float] | None:
    if geom.get("type") != "Polygon":
        return None
    xs: list[float] = []
    ys: list[float] = []
    for ring in geom.get("coordinates", []):
        for pt in ring:
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _pixel_to_bbox_wgs84(
    px_bounds: tuple[float, float, float, float],
    win_transform, src_crs: str,
) -> list[float]:
    """Convert pixel-space bounds (col_min, row_min, col_max, row_max) to WGS84 [w,s,e,n]."""
    col_min, row_min, col_max, row_max = px_bounds
    # rasterio Affine: (x, y) = transform * (col, row)
    x_min, y_max = win_transform * (col_min, row_min)
    x_max, y_min = win_transform * (col_max, row_max)
    # Because y-axis is flipped in image coordinates, y_max corresponds to row_min.
    src_w, src_s = min(x_min, x_max), min(y_min, y_max)
    src_e, src_n = max(x_min, x_max), max(y_min, y_max)
    ll_bounds = transform_bounds(src_crs, "EPSG:4326", src_w, src_s, src_e, src_n, densify_pts=21)
    return [round(ll_bounds[0], 5), round(ll_bounds[1], 5),
            round(ll_bounds[2], 5), round(ll_bounds[3], 5)]


def find_largest_water_bbox(
    scene: Scene, expanded_bbox: list[float], ndwi_threshold: float,
) -> WaterFinding:
    """Discover water on the expanded bbox and return the largest cluster's bbox."""
    pair = _read_pair(scene, expanded_bbox)
    if pair is None:
        return WaterFinding(scene, 0.0, 0, None, None)
    green, nir, win_tr, crs, pxa = pair
    ndwi = compute_ndwi(green, nir)
    mask = (ndwi > ndwi_threshold).astype(np.uint8)
    ndwi_area = float(mask.sum()) * pxa / 1_000_000.0
    if mask.sum() < MIN_WATER_PIXELS:
        return WaterFinding(scene, ndwi_area, 0, None, None)

    # Extract connected water polygons in pixel space (transform=identity default).
    largest_area = 0.0
    largest_geom = None
    for geom, val in rio_shapes(mask, mask=mask.astype(bool), connectivity=8):
        if int(val) != 1:
            continue
        px_area = _shape_pixel_area(geom)
        if px_area > largest_area:
            largest_area = px_area
            largest_geom = geom

    if largest_geom is None or largest_area < MIN_WATER_PIXELS:
        return WaterFinding(scene, ndwi_area, int(largest_area), None, None)

    px_bounds = _shape_pixel_bounds(largest_geom)
    if px_bounds is None:
        return WaterFinding(scene, ndwi_area, int(largest_area), None, None)

    # Add a pixel buffer to allow for seasonal fluctuation.
    col_min, row_min, col_max, row_max = px_bounds
    col_min -= PIXEL_BUFFER
    row_min -= PIXEL_BUFFER
    col_max += PIXEL_BUFFER
    row_max += PIXEL_BUFFER
    new_bbox = _pixel_to_bbox_wgs84((col_min, row_min, col_max, row_max), win_tr, crs)

    # Clamp to WGS84 bounds
    new_bbox[0] = max(-180.0, new_bbox[0])
    new_bbox[1] = max(-90.0, new_bbox[1])
    new_bbox[2] = min(180.0, new_bbox[2])
    new_bbox[3] = min(90.0, new_bbox[3])
    return WaterFinding(scene, ndwi_area, int(largest_area), new_bbox, None)


# -----------------------------------------------------------------------------
# Step 6 & 7 — validate new bbox on the same and additional scenes
# -----------------------------------------------------------------------------

def area_for_bbox(scene: Scene, bbox: list[float], threshold: float) -> float | None:
    try:
        pair = _read_pair(scene, bbox)
    except Exception as exc:  # noqa: BLE001
        log.warning("read failed for %s: %s", scene.scene_id, exc)
        return None
    if pair is None:
        return None
    green, nir, _, _, pxa = pair
    ndwi = compute_ndwi(green, nir)
    mask = ndwi > threshold
    return float(mask.sum()) * pxa / 1_000_000.0


# -----------------------------------------------------------------------------
# Wet / dry season cross-check
# -----------------------------------------------------------------------------

def _wet_and_dry_scenes(bbox: list[float]) -> tuple[Scene | None, Scene | None]:
    now = datetime.now(timezone.utc)
    wet_year = now.year - 1
    dry_year = now.year - 1
    # Wet: any Jan-Mar scene from last year (or 2 years ago as fallback)
    wet = None
    for y in (wet_year, wet_year - 1):
        for m in (2, 3, 1):
            wet = find_scene(bbox, _range_month(y, m))
            if wet is not None:
                break
        if wet is not None:
            break
    # Dry: any Jul-Sep scene from last year
    dry = None
    for y in (dry_year, dry_year - 1):
        for m in (8, 7, 9):
            dry = find_scene(bbox, _range_month(y, m))
            if dry is not None:
                break
        if dry is not None:
            break
    return wet, dry


def _historical_scenes(bbox: list[float]) -> list[Scene]:
    """A handful of 2022-2023 scenes to check whether water was ever present."""
    out: list[Scene] = []
    for y in (2022, 2023):
        for m in (2, 5, 8):
            s = find_scene(bbox, _range_month(y, m))
            if s is not None:
                out.append(s)
            if len(out) >= 3:
                return out
    return out


# -----------------------------------------------------------------------------
# Reporting + writing
# -----------------------------------------------------------------------------

@dataclass
class DamResult:
    id: str
    name: str
    status: str  # RECALIBRATED / GENUINELY DRY / NEEDS MANUAL REVIEW
    old_bbox: list[float]
    new_bbox: list[float] | None
    old_reading_km2: float | None
    new_reading_km2: float | None
    wet_km2: float | None = None
    dry_km2: float | None = None
    notes: str = ""


def _print_report(results: list[DamResult]) -> None:
    log.info("=" * 66)
    log.info("Calibration report")
    log.info("=" * 66)
    for r in results:
        log.info("Dam: %s (%s)", r.name, r.id)
        log.info("  Status: %s", r.status)
        log.info("  Old bbox: %s", r.old_bbox)
        if r.new_bbox is not None:
            log.info("  New bbox: %s", r.new_bbox)
        log.info(
            "  Old reading: %s km²",
            f"{r.old_reading_km2:.3f}" if r.old_reading_km2 is not None else "—",
        )
        if r.new_reading_km2 is not None:
            log.info("  New reading: %.3f km²", r.new_reading_km2)
        if r.wet_km2 is not None:
            log.info("  Wet season check: %.3f km²", r.wet_km2)
        if r.dry_km2 is not None:
            log.info("  Dry season check: %.3f km²", r.dry_km2)
        if r.notes:
            log.info("  Notes: %s", r.notes)
        log.info("")


def _latest_reading(readings: list[dict]) -> float | None:
    for r in reversed(readings):
        v = r.get("surface_area_km2", "")
        try:
            return float(v) if v else None
        except ValueError:
            return None
    return None


def _osm_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/@{lat:.5f},{lon:.5f},14z"


def _process_dam(
    dam: dict, per_dam: dict[str, list[dict]],
) -> DamResult:
    lat, lon = float(dam["lat"]), float(dam["lon"])
    center_bbox = [
        lon - SEARCH_BUFFER_DEG, lat - SEARCH_BUFFER_DEG,
        lon + SEARCH_BUFFER_DEG, lat + SEARCH_BUFFER_DEG,
    ]
    old_reading = _latest_reading(per_dam.get(dam["id"], []))
    old_bbox = list(dam["bbox"])

    log.info("[%s] step 2: STAC search over expanded bbox %s", dam["id"], center_bbox)
    scene = find_scene(center_bbox, _range_recent(30))
    if scene is None:
        # try last 90 days
        scene = find_scene(center_bbox, _range_recent(90))
    if scene is None:
        return DamResult(
            id=dam["id"], name=dam["name"], status="NEEDS MANUAL REVIEW",
            old_bbox=old_bbox, new_bbox=None,
            old_reading_km2=old_reading, new_reading_km2=None,
            notes=f"no recent STAC scene found — {_osm_link(lat, lon)}",
        )

    log.info("[%s] step 3-4: NDWI on scene %s (cloud %.0f%%)", dam["id"], scene.scene_id, scene.cloud_cover)
    finding = find_largest_water_bbox(scene, center_bbox, DISCOVERY_NDWI_THRESHOLD)
    log.info("[%s] discovery NDWI area %.3f km², largest cluster %d px",
             dam["id"], finding.ndwi_area_km2, finding.cluster_px_count)

    if finding.corrected_bbox is None:
        # No water found on this scene → check history
        log.info("[%s] step 7: checking 2022-2023 history for any water", dam["id"])
        historical_water = False
        for hs in _historical_scenes(center_bbox):
            a = area_for_bbox(hs, center_bbox, DISCOVERY_NDWI_THRESHOLD)
            if a is not None and a > 0.05:
                historical_water = True
                log.info("[%s]  history %s: %.3f km²", dam["id"], hs.scene_id, a)
                break
        if historical_water:
            return DamResult(
                id=dam["id"], name=dam["name"], status="GENUINELY DRY",
                old_bbox=old_bbox, new_bbox=None,
                old_reading_km2=old_reading, new_reading_km2=None,
                notes="water present historically but not now; keep original bbox, lower historical_avg_km2 to ~0.01",
            )
        return DamResult(
            id=dam["id"], name=dam["name"], status="NEEDS MANUAL REVIEW",
            old_bbox=old_bbox, new_bbox=None,
            old_reading_km2=old_reading, new_reading_km2=None,
            notes=f"no water in expanded search at any time — coords may be wrong: {_osm_link(lat, lon)}",
        )

    # Validate on the same scene with the new bbox
    log.info("[%s] step 6: validating new bbox %s", dam["id"], finding.corrected_bbox)
    new_reading = area_for_bbox(scene, finding.corrected_bbox, dam.get("ndwi_threshold", 0.05))
    if new_reading is None or new_reading < VALIDATION_MIN_KM2:
        return DamResult(
            id=dam["id"], name=dam["name"], status="NEEDS MANUAL REVIEW",
            old_bbox=old_bbox, new_bbox=finding.corrected_bbox,
            old_reading_km2=old_reading, new_reading_km2=new_reading,
            notes=(
                f"candidate bbox found but validation reading is only "
                f"{new_reading:.3f} km² (< {VALIDATION_MIN_KM2}). Manual review: "
                f"{_osm_link(lat, lon)}"
            ),
        )

    # Cross-check with wet/dry season scenes
    log.info("[%s] step 7: wet/dry season cross-check", dam["id"])
    wet_scene, dry_scene = _wet_and_dry_scenes(finding.corrected_bbox)
    wet_area = area_for_bbox(wet_scene, finding.corrected_bbox, dam.get("ndwi_threshold", 0.05)) if wet_scene else None
    dry_area = area_for_bbox(dry_scene, finding.corrected_bbox, dam.get("ndwi_threshold", 0.05)) if dry_scene else None
    notes = ""
    if wet_area is not None and dry_area is not None:
        if wet_area <= dry_area:
            notes = "wet-season area not greater than dry-season area; the new bbox may include a lake not tied to seasonal fill"
    return DamResult(
        id=dam["id"], name=dam["name"], status="RECALIBRATED",
        old_bbox=old_bbox, new_bbox=finding.corrected_bbox,
        old_reading_km2=old_reading, new_reading_km2=new_reading,
        wet_km2=wet_area, dry_km2=dry_area, notes=notes,
    )


def calibrate() -> None:
    with DAMS_JSON.open() as fh:
        dams_doc = json.load(fh)
    dams = dams_doc["dams"]
    per_dam = load_readings_by_dam()

    candidates = candidate_dams(dams, per_dam)
    if not candidates:
        log.info("no candidate dams for recalibration")
        return

    log.info("=" * 66)
    log.info("Candidate dams for recalibration (%d)", len(candidates))
    log.info("=" * 66)
    for d in candidates:
        recent = per_dam.get(d["id"], [])[-LOOKBACK_READINGS:]
        vals = [(r.get("date", "?"), r.get("surface_area_km2", "")) for r in recent]
        log.info("  %-24s bbox=%s recent=%s", d["id"], d["bbox"], vals)

    # Step 2..7 per dam.
    results: list[DamResult] = []
    for d in candidates:
        try:
            results.append(_process_dam(d, per_dam))
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] unexpected error: %s", d["id"], exc)
            results.append(DamResult(
                id=d["id"], name=d["name"], status="NEEDS MANUAL REVIEW",
                old_bbox=list(d["bbox"]), new_bbox=None,
                old_reading_km2=_latest_reading(per_dam.get(d["id"], [])),
                new_reading_km2=None, notes=f"exception: {exc}",
            ))

    # Build the recalibrated config: copy dams.json, patch bboxes for RECALIBRATED entries.
    recal_ids = {r.id: r for r in results if r.status == "RECALIBRATED"}
    dry_ids = {r.id: r for r in results if r.status == "GENUINELY DRY"}
    out_doc = json.loads(json.dumps(dams_doc))  # deep copy
    for dam in out_doc["dams"]:
        r = recal_ids.get(dam["id"])
        if r and r.new_bbox is not None:
            dam["bbox"] = r.new_bbox
            dam["ndwi_threshold"] = 0.05  # default
        if dam["id"] in dry_ids:
            dam["historical_avg_km2"] = 0.01
    OUTPUT_JSON.write_text(json.dumps(out_doc, indent=2))
    log.info("wrote %s (recalibrated=%d, genuinely-dry=%d)",
             OUTPUT_JSON, len(recal_ids), len(dry_ids))

    _print_report(results)


if __name__ == "__main__":
    calibrate()
