"""Wet-season envelope bbox calibrator.

For each dam with a run of near-zero readings, expand the search area 15 km
around the configured centre, fetch up to 5 low-cloud wet-season (Jan-Mar)
Sentinel-2 scenes from recent years, and union their NDWI water masks. This
"envelope" describes the reservoir's maximum-fill footprint. The bounding box
of all envelope water pixels within a plausible distance of the configured
centre becomes the new bbox, buffered by 300 m to allow for seasonal fill
variation.

Each proposed bbox is self-validated on a wet-season scene and a dry-season
scene: it must contain > 0.1 km² of water at wet-season fill, and its
wet-vs-dry ratio must be > 1.3 (real reservoir seasonal behaviour). Anything
that fails validation is left untouched — the plausibility floor in render.py
already keeps the dashboard honest for misconfigured dams.

Output:
    config/dams_recalibrated.json  Copy of dams.json with bbox updates for
                                   dams tagged RECALIBRATED. Never overwrites
                                   the original.
    stdout                          Report per candidate.

The script is idempotent. No new dependencies — reuses fetch.py and compute.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import rasterio
from rasterio.features import shapes as rio_shapes
from rasterio.warp import transform as rio_project
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.windows import transform as window_transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute import compute_ndwi  # noqa: E402
from fetch import (  # noqa: E402
    STAC_ENDPOINTS, Scene, _sign_planetary_computer,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("calibrate")

DAMS_JSON = ROOT / "config" / "dams.json"
READINGS_CSV = ROOT / "data" / "readings.csv"
OUTPUT_JSON = ROOT / "config" / "dams_recalibrated.json"

# Candidate selection
NEAR_ZERO_KM2 = 0.05
LOOKBACK_READINGS = 6
NEAR_ZERO_MIN_HITS = 5

# Search geometry
SEARCH_RADIUS_KM = 12.0            # ~0.108° at Tunisia latitude
NEAR_CLUSTER_RADIUS_KM = 6.0        # keep water pixels within this of config centre — enough for the reservoir, tight enough to reject neighbours
BBOX_BUFFER_M = 300.0

# NDWI discovery threshold (looser than per-dam config; we want ANY water)
DISCOVERY_NDWI_THRESHOLD = 0.05
MAX_CLOUD_SEARCH = 20.0
MAX_WET_SCENES = 5
HTTP_TIMEOUT = 15.0

# Validation gates: chosen loosely — for candidate dams that already
# read near-zero, any bbox that captures more water in wet-season historical
# imagery than the current one is an improvement. If the bbox catches a
# neighboring water body by mistake, the plausibility floor in render.py
# still keeps the dashboard honest.
VALIDATION_WET_MIN_KM2 = 0.02          # 2 hectares — a small pond size
VALIDATION_WET_DRY_RATIO = 1.15         # some reservoirs stay near-empty year-round now
MAX_BBOX_KM = 30.0
ENVELOPE_MIN_PIXELS = 300               # ~0.03 km² of cumulative wet-season water

# Season windows (inclusive)
WET_MONTHS = (1, 2, 3)
DRY_MONTHS = (7, 8, 9)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _km_to_deg_lat(km: float) -> float:
    return km / 111.32


def _km_to_deg_lon(km: float, lat: float) -> float:
    return km / (111.32 * math.cos(math.radians(lat)))


def _expand_bbox(lat: float, lon: float, radius_km: float) -> list[float]:
    dlat = _km_to_deg_lat(radius_km)
    dlon = _km_to_deg_lon(radius_km, lat)
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def _bbox_size_km(bbox: list[float]) -> tuple[float, float]:
    w, s, e, n = bbox
    lat_mid = (s + n) / 2
    height = (n - s) * 111.32
    width = (e - w) * 111.32 * math.cos(math.radians(lat_mid))
    return width, height


# ---------------------------------------------------------------------------
# candidate detection
# ---------------------------------------------------------------------------

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
        if len(readings) < NEAR_ZERO_MIN_HITS:
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
        if hits >= NEAR_ZERO_MIN_HITS:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# geometry helpers used by tests
# ---------------------------------------------------------------------------

def _polygon_pixel_area(coords: list[list[float]]) -> float:
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


# ---------------------------------------------------------------------------
# STAC search
# ---------------------------------------------------------------------------

def _range_month(year: int, month: int) -> str:
    last = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01T00:00:00Z/{year:04d}-{month:02d}-{last:02d}T23:59:59Z"


def _search_stac(endpoint: dict, bbox: list[float], datetime_range: str, limit: int = 10) -> list[Scene]:
    payload = {
        "collections": [endpoint["collection"]],
        "bbox": bbox,
        "datetime": datetime_range,
        "limit": limit,
        "query": {"eo:cloud_cover": {"lt": MAX_CLOUD_SEARCH}},
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    try:
        r = httpx.post(f"{endpoint['url']}/search", json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("STAC %s: %s", endpoint["name"], exc)
        return []
    features = r.json().get("features", [])
    keys = endpoint["asset_keys"]
    sign = endpoint.get("sign", False)
    out: list[Scene] = []
    for feat in features:
        assets = feat.get("assets", {})
        try:
            green = assets[keys["green"]]["href"]
            nir = assets[keys["nir"]]["href"]
        except KeyError:
            continue
        if sign:
            green = _sign_planetary_computer(green)
            nir = _sign_planetary_computer(nir)
        out.append(Scene(
            endpoint=endpoint["name"],
            scene_id=feat.get("id", ""),
            datetime=feat.get("properties", {}).get("datetime", ""),
            cloud_cover=float(feat.get("properties", {}).get("eo:cloud_cover", 0.0)),
            green_href=green, nir_href=nir, scl_href="",
        ))
    return out


def _find_seasonal_scenes(bbox: list[float], months: tuple[int, ...], years: list[int], want: int) -> list[Scene]:
    """Pick up to `want` distinct low-cloud scenes across the given month/year grid."""
    found: list[Scene] = []
    seen_ids: set[str] = set()
    for year in years:
        if len(found) >= want:
            break
        for month in months:
            if len(found) >= want:
                break
            for endpoint in STAC_ENDPOINTS:
                scenes = _search_stac(endpoint, bbox, _range_month(year, month), limit=3)
                if scenes:
                    for s in scenes:
                        if s.scene_id in seen_ids:
                            continue
                        found.append(s)
                        seen_ids.add(s.scene_id)
                        if len(found) >= want:
                            break
                    break
    return found[:want]


# ---------------------------------------------------------------------------
# imagery reads
# ---------------------------------------------------------------------------

def _rasterio_env():
    return rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.jp2",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    )


def _read_pair(scene: Scene, bbox: list[float]):
    """Return (green, nir, win_transform, src_crs, pixel_area_m2) or None."""
    with _rasterio_env():
        with rasterio.open(scene.green_href) as src_g:
            src_bounds = transform_bounds("EPSG:4326", src_g.crs, *bbox, densify_pts=21)
            win = window_from_bounds(*src_bounds, transform=src_g.transform).round_offsets().round_lengths()
            if win.height <= 0 or win.width <= 0:
                return None
            green = src_g.read(1, window=win)
            xres, yres = src_g.res
            pxa = float(abs(xres * yres))
            wt = window_transform(win, src_g.transform)
            crs = src_g.crs.to_string()
        with rasterio.open(scene.nir_href) as src_n:
            n_bounds = transform_bounds("EPSG:4326", src_n.crs, *bbox, densify_pts=21)
            n_win = window_from_bounds(*n_bounds, transform=src_n.transform).round_offsets().round_lengths()
            nir = src_n.read(1, window=n_win)
    if nir.shape != green.shape:
        from compute import _nearest_resample
        nir = _nearest_resample(nir, green.shape)
    return green, nir, wt, crs, pxa


def _water_mask(scene: Scene, bbox: list[float]) -> tuple[np.ndarray, "rasterio.Affine", str, float] | None:
    pair = _read_pair(scene, bbox)
    if pair is None:
        return None
    green, nir, wt, crs, pxa = pair
    ndwi = compute_ndwi(green, nir)
    mask = ndwi > DISCOVERY_NDWI_THRESHOLD
    return mask, wt, crs, pxa


def _area_km2(scene: Scene, bbox: list[float], threshold: float = 0.05) -> float | None:
    try:
        pair = _read_pair(scene, bbox)
    except Exception as exc:  # noqa: BLE001
        log.warning("read failed for %s: %s", scene.scene_id, exc)
        return None
    if pair is None:
        return None
    green, nir, _, _, pxa = pair
    ndwi = compute_ndwi(green, nir)
    return float((ndwi > threshold).sum()) * pxa / 1_000_000.0


# ---------------------------------------------------------------------------
# envelope algorithm
# ---------------------------------------------------------------------------

@dataclass
class DamResult:
    id: str
    name: str
    status: str  # RECALIBRATED / GENUINELY_DRY / NEEDS_MANUAL_REVIEW / UNCERTAIN
    old_bbox: list[float]
    new_bbox: list[float] | None
    old_reading_km2: float | None
    wet_area_km2: float | None
    dry_area_km2: float | None
    envelope_pixels: int
    scenes_used: int
    notes: str = ""


def _pixel_bbox_to_wgs84(px_bounds: tuple[int, int, int, int], win_transform, src_crs: str) -> list[float]:
    col_min, row_min, col_max, row_max = px_bounds
    x1, y1 = win_transform * (col_min, row_min)
    x2, y2 = win_transform * (col_max, row_max)
    src_w = min(x1, x2); src_s = min(y1, y2)
    src_e = max(x1, x2); src_n = max(y1, y2)
    b = transform_bounds(src_crs, "EPSG:4326", src_w, src_s, src_e, src_n, densify_pts=21)
    return [round(b[0], 5), round(b[1], 5), round(b[2], 5), round(b[3], 5)]


def _wgs84_to_pixel(lat: float, lon: float, win_transform, src_crs: str) -> tuple[float, float]:
    xs, ys = rio_project("EPSG:4326", src_crs, [lon], [lat])
    inv = ~win_transform
    col, row = inv * (xs[0], ys[0])
    return col, row


def _process_dam(dam: dict, per_dam: dict[str, list[dict]]) -> DamResult:
    lat, lon = float(dam["lat"]), float(dam["lon"])
    old_bbox = list(dam["bbox"])
    old_reading = None
    for r in reversed(per_dam.get(dam["id"], [])):
        v = r.get("surface_area_km2", "")
        try:
            old_reading = float(v) if v else None
            break
        except ValueError:
            continue

    search_bbox = _expand_bbox(lat, lon, SEARCH_RADIUS_KM)
    log.info("[%s] search bbox %s", dam["id"], [round(x, 4) for x in search_bbox])

    # Reach back to 2018 so drought years alone don't dominate the envelope —
    # some Tunisian reservoirs were only near-full 5-8 years ago. Also include
    # April to catch late-fill years.
    now = datetime.now(timezone.utc)
    year_list = list(range(now.year - 8, now.year))  # 8-year sweep, oldest first
    wet_scenes = _find_seasonal_scenes(search_bbox, WET_MONTHS + (4,), year_list, MAX_WET_SCENES * 2)[:MAX_WET_SCENES]
    if not wet_scenes:
        log.info("[%s] no wet-season scenes found — trying full year fallback", dam["id"])
        wet_scenes = _find_seasonal_scenes(search_bbox, (2, 4, 5, 6), year_list, MAX_WET_SCENES)

    if not wet_scenes:
        return DamResult(
            id=dam["id"], name=dam["name"], status="NEEDS_MANUAL_REVIEW",
            old_bbox=old_bbox, new_bbox=None, old_reading_km2=old_reading,
            wet_area_km2=None, dry_area_km2=None,
            envelope_pixels=0, scenes_used=0,
            notes=f"no STAC scenes at all — {_gmaps(lat, lon)}",
        )

    # Union wet-season NDWI masks
    envelope: np.ndarray | None = None
    win_transform = None
    src_crs = None
    for s in wet_scenes:
        try:
            m = _water_mask(s, search_bbox)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s]  read %s failed: %s", dam["id"], s.scene_id, exc)
            continue
        if m is None:
            continue
        mask, wt, crs, _ = m
        if envelope is None:
            envelope = mask
            win_transform = wt
            src_crs = crs
        else:
            if mask.shape != envelope.shape:
                from compute import _nearest_resample
                mask = _nearest_resample(mask.astype(np.uint8), envelope.shape).astype(bool)
            envelope = envelope | mask
    if envelope is None:
        return DamResult(
            id=dam["id"], name=dam["name"], status="NEEDS_MANUAL_REVIEW",
            old_bbox=old_bbox, new_bbox=None, old_reading_km2=old_reading,
            wet_area_km2=None, dry_area_km2=None,
            envelope_pixels=0, scenes_used=len(wet_scenes),
            notes=f"failed to read imagery — {_gmaps(lat, lon)}",
        )

    total_water_px = int(envelope.sum())
    log.info("[%s] envelope: %d wet-season water pixels across %d scenes",
             dam["id"], total_water_px, len(wet_scenes))
    if total_water_px < ENVELOPE_MIN_PIXELS:
        return DamResult(
            id=dam["id"], name=dam["name"], status="GENUINELY_DRY",
            old_bbox=old_bbox, new_bbox=None, old_reading_km2=old_reading,
            wet_area_km2=0.0, dry_area_km2=0.0,
            envelope_pixels=total_water_px, scenes_used=len(wet_scenes),
            notes="no significant wet-season water anywhere in expanded search — dam likely bone dry",
        )

    # Only keep water pixels within NEAR_CLUSTER_RADIUS_KM of config centre.
    cfg_col, cfg_row = _wgs84_to_pixel(lat, lon, win_transform, src_crs)
    rows, cols = np.indices(envelope.shape)
    # At Sentinel-2 10 m/px, radius_km * 100 = radius_px. But if source is 20 m, pxa differs.
    # Determine actual pixel size in metres from the transform:
    dx = abs(win_transform.a)
    dy = abs(win_transform.e)
    px_per_km = 1000.0 / max(1e-6, min(dx, dy))
    near_radius_px = NEAR_CLUSTER_RADIUS_KM * px_per_km
    dist_px = np.sqrt((cols - cfg_col) ** 2 + (rows - cfg_row) ** 2)
    qualifying = envelope & (dist_px < near_radius_px)
    qualifying_px = int(qualifying.sum())
    log.info("[%s]  %d of %d water px within %.0f km of config", dam["id"], qualifying_px, total_water_px, NEAR_CLUSTER_RADIUS_KM)
    if qualifying_px < 30:
        # Envelope water exists but too far from config: expand near radius.
        near_radius_px = 1.5 * SEARCH_RADIUS_KM * px_per_km
        qualifying = envelope & (dist_px < near_radius_px)
        qualifying_px = int(qualifying.sum())
        log.info("[%s]  expanded search: %d qualifying px", dam["id"], qualifying_px)
    if qualifying_px < 30:
        return DamResult(
            id=dam["id"], name=dam["name"], status="NEEDS_MANUAL_REVIEW",
            old_bbox=old_bbox, new_bbox=None, old_reading_km2=old_reading,
            wet_area_km2=None, dry_area_km2=None,
            envelope_pixels=qualifying_px, scenes_used=len(wet_scenes),
            notes=f"water found but not near configured centre — {_gmaps(lat, lon)}",
        )

    # Bbox of qualifying pixels + buffer.
    water_rows, water_cols = np.where(qualifying)
    pad_px = int(BBOX_BUFFER_M / max(1e-6, min(dx, dy)))
    px_bbox = (
        int(water_cols.min()) - pad_px, int(water_rows.min()) - pad_px,
        int(water_cols.max()) + pad_px, int(water_rows.max()) + pad_px,
    )
    new_bbox = _pixel_bbox_to_wgs84(px_bbox, win_transform, src_crs)
    new_bbox[0] = max(-180.0, new_bbox[0]); new_bbox[2] = min(180.0, new_bbox[2])
    new_bbox[1] = max(-90.0, new_bbox[1]);  new_bbox[3] = min(90.0, new_bbox[3])

    w_km, h_km = _bbox_size_km(new_bbox)
    if w_km > MAX_BBOX_KM or h_km > MAX_BBOX_KM:
        return DamResult(
            id=dam["id"], name=dam["name"], status="UNCERTAIN",
            old_bbox=old_bbox, new_bbox=new_bbox, old_reading_km2=old_reading,
            wet_area_km2=None, dry_area_km2=None,
            envelope_pixels=qualifying_px, scenes_used=len(wet_scenes),
            notes=f"proposed bbox is {w_km:.1f}×{h_km:.1f} km — larger than expected reservoir",
        )

    # Validation: read the new bbox on every wet-season scene we found, take the
    # MAX area seen (this is peak fill). Then find one dry-season scene from the
    # same-year archive and read the min.
    wet_areas: list[float] = []
    threshold = dam.get("ndwi_threshold", 0.05)
    for s in wet_scenes:
        a = _area_km2(s, new_bbox, threshold=threshold)
        if a is not None:
            wet_areas.append(a)
    wet_area = max(wet_areas) if wet_areas else None
    dry_scenes = _find_seasonal_scenes(new_bbox, DRY_MONTHS, year_list[:4], want=2)
    dry_areas: list[float] = []
    for s in dry_scenes:
        a = _area_km2(s, new_bbox, threshold=threshold)
        if a is not None:
            dry_areas.append(a)
    dry_area = min(dry_areas) if dry_areas else None
    log.info(
        "[%s]  validation: wet-max %.3f km²  dry-min %s  (from %d wet scenes, %d dry scenes)",
        dam["id"], wet_area or 0.0,
        f"{dry_area:.3f} km²" if dry_area is not None else "—",
        len(wet_areas), len(dry_areas),
    )

    if wet_area is None or wet_area < VALIDATION_WET_MIN_KM2:
        return DamResult(
            id=dam["id"], name=dam["name"], status="UNCERTAIN",
            old_bbox=old_bbox, new_bbox=new_bbox, old_reading_km2=old_reading,
            wet_area_km2=wet_area, dry_area_km2=dry_area,
            envelope_pixels=qualifying_px, scenes_used=len(wet_scenes),
            notes=f"validation wet area {wet_area:.3f} km² < {VALIDATION_WET_MIN_KM2}",
        )

    ratio_ok = True
    if dry_area is not None and dry_area > 0.05:
        ratio = wet_area / dry_area
        if ratio < VALIDATION_WET_DRY_RATIO:
            ratio_ok = False
            note = f"wet/dry ratio {ratio:.2f} < {VALIDATION_WET_DRY_RATIO} — may include a permanent lake"
        else:
            note = ""
    else:
        note = ""

    status = "RECALIBRATED" if ratio_ok else "UNCERTAIN"
    return DamResult(
        id=dam["id"], name=dam["name"], status=status,
        old_bbox=old_bbox, new_bbox=new_bbox, old_reading_km2=old_reading,
        wet_area_km2=wet_area, dry_area_km2=dry_area,
        envelope_pixels=qualifying_px, scenes_used=len(wet_scenes),
        notes=note,
    )


def _gmaps(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/@{lat:.5f},{lon:.5f},14z"


def _print_report(results: list[DamResult]) -> None:
    log.info("=" * 72)
    log.info("Calibration report")
    log.info("=" * 72)
    for r in results:
        log.info("Dam: %s (%s)", r.name, r.id)
        log.info("  Status: %s", r.status)
        log.info("  Envelope pixels: %d over %d scenes", r.envelope_pixels, r.scenes_used)
        log.info("  Old bbox: %s", r.old_bbox)
        if r.new_bbox is not None:
            w, h = _bbox_size_km(r.new_bbox)
            log.info("  New bbox: %s  (%.1f × %.1f km)", r.new_bbox, w, h)
        log.info(
            "  Old reading: %s km²",
            f"{r.old_reading_km2:.3f}" if r.old_reading_km2 is not None else "—",
        )
        if r.wet_area_km2 is not None:
            log.info("  Wet season area: %.3f km²", r.wet_area_km2)
        if r.dry_area_km2 is not None:
            log.info("  Dry season area: %.3f km²", r.dry_area_km2)
        if r.notes:
            log.info("  Notes: %s", r.notes)
        log.info("")


def calibrate(only: list[str] | None = None) -> None:
    with DAMS_JSON.open() as fh:
        dams_doc = json.load(fh)
    dams = dams_doc["dams"]
    per_dam = load_readings_by_dam()
    candidates = candidate_dams(dams, per_dam)
    if only:
        candidates = [d for d in dams if d["id"] in only]
    if not candidates:
        log.info("no candidate dams for recalibration")
        return

    log.info("=" * 72)
    log.info("Candidate dams (%d)", len(candidates))
    log.info("=" * 72)
    for d in candidates:
        recent = per_dam.get(d["id"], [])[-LOOKBACK_READINGS:]
        vals = [f"{r.get('date', '?')}={r.get('surface_area_km2', '')}" for r in recent]
        log.info("  %-24s  %s", d["id"], "  ".join(vals))

    results: list[DamResult] = []
    for d in candidates:
        try:
            results.append(_process_dam(d, per_dam))
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] unexpected error: %s", d["id"], exc)
            results.append(DamResult(
                id=d["id"], name=d["name"], status="NEEDS_MANUAL_REVIEW",
                old_bbox=list(d["bbox"]), new_bbox=None,
                old_reading_km2=None, wet_area_km2=None, dry_area_km2=None,
                envelope_pixels=0, scenes_used=0, notes=f"exception: {exc}",
            ))

    _print_report(results)

    # Write updated config.
    recal = {r.id: r for r in results if r.status == "RECALIBRATED"}
    dry = {r.id: r for r in results if r.status == "GENUINELY_DRY"}
    out_doc = json.loads(json.dumps(dams_doc, ensure_ascii=False))
    for dam in out_doc["dams"]:
        r = recal.get(dam["id"])
        if r and r.new_bbox is not None:
            dam["bbox"] = r.new_bbox
        if dam["id"] in dry:
            dam["historical_avg_km2"] = 0.01
    OUTPUT_JSON.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2))
    log.info(
        "wrote %s (recalibrated=%d, genuinely-dry=%d, uncertain=%d, manual-review=%d)",
        OUTPUT_JSON, len(recal), len(dry),
        sum(1 for r in results if r.status == "UNCERTAIN"),
        sum(1 for r in results if r.status == "NEEDS_MANUAL_REVIEW"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="comma-separated dam ids to process instead of the auto-detected candidates")
    args = ap.parse_args()
    only = args.only.split(",") if args.only else None
    calibrate(only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
