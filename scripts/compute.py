"""Spectral indices + confidence metrics for the Tunisia Water Watch pipeline.

    NDWI  (McFeeters 1996)      = (Green − NIR)  / (Green + NIR)     → surface water
    MNDWI (Xu 2006)             = (Green − SWIR) / (Green + SWIR)    → surface water (cross-check)
    NDVI  (Rouse 1974)          = (NIR   − Red)  / (NIR   + Red)     → vegetation vigor
    NDMI  (Gao 1996)            = (NIR   − SWIR) / (NIR   + SWIR)    → canopy moisture

Sentinel-2 Level-2A carries a Scene Classification Layer (SCL); we treat
classes 3 (cloud shadow), 8 (medium cloud), 9 (high cloud), 10 (thin cirrus) as
cloud-affected and exclude them from every index-derived aggregate.

Cross-referencing (dual-index for confidence):
- Water: NDWI and MNDWI both flag water but respond differently to sediment,
  shadow, and built-up land. High agreement between the two indices = high
  confidence in the water reading; low agreement = flag for review.
- Vegetation: NDVI measures greenness; NDMI measures canopy moisture. A field
  can look green (high NDVI) while being water-stressed (low NDMI). Tracking
  both catches drought early.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import numpy as np

SCL_CLOUD_CLASSES = {3, 8, 9, 10}
SCL_WATER_CLASS = 6
SCL_VEG_CLASS = 4
SCL_NOT_VEGETATED_CLASS = 5


# ------------------------- basic indices -------------------------

def _safe_normalized_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Compute (a - b) / (a + b) with zero-safe denominator. Returns float32."""
    af = a.astype(np.float32)
    bf = b.astype(np.float32)
    denom = af + bf
    out = np.where(denom != 0, (af - bf) / np.where(denom == 0, 1, denom), 0.0)
    return out.astype(np.float32)


def compute_ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    return _safe_normalized_diff(green, nir)


def compute_mndwi(green: np.ndarray, swir: np.ndarray) -> np.ndarray:
    return _safe_normalized_diff(green, swir)


def compute_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    return _safe_normalized_diff(nir, red)


def compute_ndmi(nir: np.ndarray, swir: np.ndarray) -> np.ndarray:
    return _safe_normalized_diff(nir, swir)


# ------------------------- masking + resampling -------------------------

def cloud_percent(scl: np.ndarray) -> float:
    if scl is None or scl.size == 0:
        return 100.0
    mask = np.isin(scl, list(SCL_CLOUD_CLASSES))
    return float(mask.sum()) * 100.0 / float(scl.size)


def _nearest_resample(arr: np.ndarray, target_shape: tuple[int, int]) -> np.ndarray:
    """Cheap nearest-neighbour resample so we don't need scipy."""
    src_h, src_w = arr.shape
    dst_h, dst_w = target_shape
    if src_h == 0 or src_w == 0:
        return np.zeros(target_shape, dtype=arr.dtype)
    row_idx = (np.arange(dst_h) * src_h / dst_h).astype(np.int64)
    col_idx = (np.arange(dst_w) * src_w / dst_w).astype(np.int64)
    return arr[np.ix_(row_idx, col_idx)]


def align(arr: np.ndarray | None, shape: tuple[int, int]) -> np.ndarray | None:
    if arr is None:
        return None
    return arr if arr.shape == shape else _nearest_resample(arr, shape)


# ------------------------- water surface (per dam) -------------------------

@dataclass
class DamReading:
    """Cross-referenced dam reading. surface_area_km2 is the primary output,
    surface_area_mndwi_km2 is the independent second estimate, and
    confidence is the geometric-mean fractional agreement (0..1)."""
    surface_area_km2: float | None
    surface_area_mndwi_km2: float | None
    cloud_pct: float
    threshold_used: float
    water_pixel_count: int
    total_pixel_count: int
    confidence: float | None  # 0..1; None if MNDWI unavailable

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def water_mask(ndwi: np.ndarray, threshold: float, cloud_mask: np.ndarray | None = None) -> np.ndarray:
    m = ndwi > threshold
    if cloud_mask is not None and cloud_mask.shape == m.shape:
        m = m & ~cloud_mask
    return m


def surface_area_km2(mask: np.ndarray, pixel_area_m2: float) -> float:
    return float(mask.sum()) * pixel_area_m2 / 1_000_000.0


def _agreement(a: float | None, b: float | None) -> float | None:
    """Fractional agreement between two positive estimates. 1.0 = identical,
    0.0 = fully divergent. Returns None if either estimate is missing."""
    if a is None or b is None:
        return None
    if a <= 0 and b <= 0:
        return 1.0
    scale = max(a, b, 1e-6)
    return max(0.0, 1.0 - abs(a - b) / scale)


def read_dam(
    green: np.ndarray, pixel_area_m2: float,
    nir: np.ndarray, scl: np.ndarray | None,
    threshold: float, max_cloud_pct: float = 50.0,
    swir: np.ndarray | None = None,
) -> DamReading:
    """Compute NDWI area, optionally MNDWI area, and their agreement."""
    scl_aligned = align(scl, green.shape) if scl is not None else None
    nir_a = align(nir, green.shape)
    cpct = cloud_percent(scl_aligned) if scl_aligned is not None else 0.0
    if cpct > max_cloud_pct:
        return DamReading(
            surface_area_km2=None, surface_area_mndwi_km2=None,
            cloud_pct=cpct, threshold_used=threshold,
            water_pixel_count=0, total_pixel_count=int(green.size),
            confidence=None,
        )
    cloud_mask = (
        np.isin(scl_aligned, list(SCL_CLOUD_CLASSES)) if scl_aligned is not None else None
    )
    ndwi = compute_ndwi(green, nir_a)
    mask = water_mask(ndwi, threshold, cloud_mask)
    ndwi_area = surface_area_km2(mask, pixel_area_m2)

    mndwi_area = None
    if swir is not None and swir.size:
        swir_a = align(swir, green.shape)
        mndwi = compute_mndwi(green, swir_a)
        mndwi_mask = water_mask(mndwi, threshold, cloud_mask)
        mndwi_area = surface_area_km2(mndwi_mask, pixel_area_m2)

    return DamReading(
        surface_area_km2=ndwi_area,
        surface_area_mndwi_km2=mndwi_area,
        cloud_pct=cpct,
        threshold_used=threshold,
        water_pixel_count=int(mask.sum()),
        total_pixel_count=int(green.size),
        confidence=_agreement(ndwi_area, mndwi_area),
    )


# ------------------------- vegetation (per governorate) -------------------------

@dataclass
class GovReading:
    """Cross-referenced governorate vegetation reading.

    mean_ndvi/ndmi are averaged over land pixels (SCL classes 4 or 5); pixels
    flagged as cloud/water are excluded from both aggregates. healthy_pct is
    the share of land pixels with NDVI > 0.3 AND NDMI > 0.0, meaning green
    and not-obviously-water-stressed.
    """
    mean_ndvi: float | None
    std_ndvi: float | None
    mean_ndmi: float | None
    healthy_pct: float | None
    cloud_pct: float
    land_pixel_count: int
    total_pixel_count: int
    confidence: float | None  # 0..1 derived from land coverage + agreement


def read_governorate(
    red: np.ndarray, pixel_area_m2: float,
    nir: np.ndarray, swir: np.ndarray | None, scl: np.ndarray | None,
    max_cloud_pct: float = 40.0,
) -> GovReading:
    scl_a = align(scl, red.shape) if scl is not None else None
    nir_a = align(nir, red.shape)
    swir_a = align(swir, red.shape) if swir is not None else None
    cpct = cloud_percent(scl_a) if scl_a is not None else 0.0
    total = int(red.size)
    if cpct > max_cloud_pct:
        return GovReading(
            mean_ndvi=None, std_ndvi=None, mean_ndmi=None, healthy_pct=None,
            cloud_pct=cpct, land_pixel_count=0, total_pixel_count=total,
            confidence=None,
        )
    ndvi = compute_ndvi(nir_a, red)
    ndmi = compute_ndmi(nir_a, swir_a) if swir_a is not None else None
    if scl_a is not None:
        land = np.isin(scl_a, [SCL_VEG_CLASS, SCL_NOT_VEGETATED_CLASS])
    else:
        land = np.ones(red.shape, dtype=bool)
    land_count = int(land.sum())
    if land_count < 100:  # not enough non-cloud, non-water pixels
        return GovReading(
            mean_ndvi=None, std_ndvi=None, mean_ndmi=None, healthy_pct=None,
            cloud_pct=cpct, land_pixel_count=land_count, total_pixel_count=total,
            confidence=None,
        )
    ndvi_land = ndvi[land]
    mean_ndvi = float(ndvi_land.mean())
    std_ndvi = float(ndvi_land.std())
    if ndmi is not None:
        ndmi_land = ndmi[land]
        mean_ndmi = float(ndmi_land.mean())
        healthy = np.logical_and(ndvi_land > 0.3, ndmi_land > 0.0)
    else:
        mean_ndmi = None
        healthy = ndvi_land > 0.3
    healthy_pct = 100.0 * float(healthy.sum()) / float(land_count)
    land_share = land_count / total if total else 0.0
    # Confidence: land pixels available, low cloud, and (if we have NDMI) a
    # meaningful moisture co-signal. Simple product of factors clipped to 0..1.
    conf = min(1.0, land_share / 0.5) * (1.0 - cpct / 100.0)
    if mean_ndmi is None:
        conf *= 0.7
    return GovReading(
        mean_ndvi=mean_ndvi, std_ndvi=std_ndvi, mean_ndmi=mean_ndmi,
        healthy_pct=healthy_pct, cloud_pct=cpct,
        land_pixel_count=land_count, total_pixel_count=total,
        confidence=conf,
    )


__all__ = [
    "DamReading", "GovReading",
    "compute_ndwi", "compute_mndwi", "compute_ndvi", "compute_ndmi",
    "cloud_percent", "water_mask", "surface_area_km2", "align",
    "read_dam", "read_governorate",
    "SCL_CLOUD_CLASSES",
]
