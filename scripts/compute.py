"""NDWI computation and water-surface-area estimation.

NDWI = (Green - NIR) / (Green + NIR)

Pixels with NDWI above a per-dam threshold are classified as water. Sentinel-2
Level-2A scenes carry a Scene Classification Layer (SCL) whose class codes are
used to compute the cloud-affected fraction of the window (classes 3, 8, 9, 10).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCL_CLOUD_CLASSES = {3, 8, 9, 10}  # shadow, medium-cloud, high-cloud, cirrus


@dataclass
class Reading:
    surface_area_km2: float | None
    cloud_pct: float
    threshold_used: float
    water_pixel_count: int
    total_pixel_count: int


def compute_ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """NDWI with zero-safe division. Returns float32 array."""
    g = green.astype(np.float32)
    n = nir.astype(np.float32)
    denom = g + n
    ndwi = np.where(denom != 0, (g - n) / np.where(denom == 0, 1, denom), 0.0)
    return ndwi.astype(np.float32)


def cloud_percent(scl: np.ndarray) -> float:
    if scl.size == 0:
        return 100.0
    mask = np.isin(scl, list(SCL_CLOUD_CLASSES))
    return float(mask.sum()) * 100.0 / float(scl.size)


def water_mask(ndwi: np.ndarray, threshold: float, scl: np.ndarray | None = None) -> np.ndarray:
    """Binary water mask, excluding cloud/shadow pixels if SCL is provided."""
    mask = ndwi > threshold
    if scl is not None and scl.shape == mask.shape:
        mask = mask & ~np.isin(scl, list(SCL_CLOUD_CLASSES))
    return mask


def surface_area_km2(mask: np.ndarray, pixel_area_m2: float) -> float:
    return float(mask.sum()) * pixel_area_m2 / 1_000_000.0


def read_dam(
    green: np.ndarray,
    green_pixel_area_m2: float,
    nir: np.ndarray,
    scl: np.ndarray | None,
    threshold: float,
    max_cloud_pct: float = 50.0,
) -> Reading:
    """Compute a full reading for one dam window.

    If cloud coverage exceeds `max_cloud_pct` the surface area is left None so
    the CSV row records the skip explicitly.
    """
    # Resize SCL (20m) to green shape (10m) via nearest-neighbour if needed.
    scl_aligned: np.ndarray | None = None
    if scl is not None:
        if scl.shape == green.shape:
            scl_aligned = scl
        else:
            scl_aligned = _nearest_resample(scl, green.shape)
    # Match NIR shape to green shape (usually identical at 10m).
    if nir.shape != green.shape:
        nir = _nearest_resample(nir, green.shape)
    cpct = cloud_percent(scl_aligned) if scl_aligned is not None else 0.0
    if cpct > max_cloud_pct:
        return Reading(
            surface_area_km2=None,
            cloud_pct=cpct,
            threshold_used=threshold,
            water_pixel_count=0,
            total_pixel_count=int(green.size),
        )
    ndwi = compute_ndwi(green, nir)
    mask = water_mask(ndwi, threshold, scl_aligned)
    area = surface_area_km2(mask, green_pixel_area_m2)
    return Reading(
        surface_area_km2=area,
        cloud_pct=cpct,
        threshold_used=threshold,
        water_pixel_count=int(mask.sum()),
        total_pixel_count=int(green.size),
    )


def _nearest_resample(arr: np.ndarray, target_shape: tuple[int, int]) -> np.ndarray:
    """Cheap nearest-neighbour resample without pulling in scipy."""
    src_h, src_w = arr.shape
    dst_h, dst_w = target_shape
    if src_h == 0 or src_w == 0:
        return np.zeros(target_shape, dtype=arr.dtype)
    row_idx = (np.arange(dst_h) * src_h / dst_h).astype(np.int64)
    col_idx = (np.arange(dst_w) * src_w / dst_w).astype(np.int64)
    return arr[np.ix_(row_idx, col_idx)]


__all__ = [
    "Reading",
    "compute_ndwi",
    "cloud_percent",
    "water_mask",
    "surface_area_km2",
    "read_dam",
]
