"""STAC scene discovery + Cloud-Optimized GeoTIFF band extraction.

Tries multiple public STAC endpoints in order so the pipeline never depends on
a single provider. Reads only the pixels inside each dam's bounding box using
HTTP range requests via rasterio's remote-COG support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx
import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

log = logging.getLogger(__name__)

STAC_ENDPOINTS = [
    {
        "name": "earth-search",
        "url": "https://earth-search.aws.element84.com/v1",
        "collection": "sentinel-2-l2a",
        "asset_keys": {"green": "green", "nir": "nir", "scl": "scl"},
    },
    {
        "name": "planetary-computer",
        "url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "collection": "sentinel-2-l2a",
        "asset_keys": {"green": "B03", "nir": "B08", "scl": "SCL"},
        "sign": True,
    },
    {
        "name": "cdse",
        "url": "https://catalogue.dataspace.copernicus.eu/stac",
        "collection": "SENTINEL-2",
        "asset_keys": {"green": "B03_10m", "nir": "B08_10m", "scl": "SCL_20m"},
    },
]

REQUEST_TIMEOUT = 30.0
MAX_CLOUD_PCT_SEARCH = 30.0


@dataclass
class Scene:
    endpoint: str
    scene_id: str
    datetime: str
    cloud_cover: float
    green_href: str
    nir_href: str
    scl_href: str


def _sign_planetary_computer(href: str) -> str:
    """Fetch a SAS-signed URL from the Planetary Computer SAS service."""
    try:
        r = httpx.get(
            "https://planetarycomputer.microsoft.com/api/sas/v1/sign",
            params={"href": href},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("href", href)
    except httpx.HTTPError as exc:
        log.warning("planetary-computer signing failed for %s: %s", href, exc)
        return href


def _search_endpoint(
    endpoint: dict,
    bbox: list[float],
    days: int,
) -> list[Scene]:
    """Query one STAC endpoint for recent, low-cloud scenes over a bbox."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    payload = {
        "collections": [endpoint["collection"]],
        "bbox": bbox,
        "datetime": f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "limit": 10,
        "query": {"eo:cloud_cover": {"lt": MAX_CLOUD_PCT_SEARCH}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    r = httpx.post(
        f"{endpoint['url']}/search",
        json=payload,
        timeout=REQUEST_TIMEOUT,
        headers={"Accept": "application/geo+json"},
    )
    r.raise_for_status()
    features = r.json().get("features", [])
    scenes: list[Scene] = []
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
        scenes.append(
            Scene(
                endpoint=endpoint["name"],
                scene_id=feat.get("id", ""),
                datetime=feat.get("properties", {}).get("datetime", ""),
                cloud_cover=float(feat.get("properties", {}).get("eo:cloud_cover", 0.0)),
                green_href=green,
                nir_href=nir,
                scl_href=scl,
            )
        )
    return scenes


def discover_latest_scene(bbox: list[float], days: int = 12) -> Scene | None:
    """Try each STAC endpoint until one returns a usable recent scene.

    A "usable" scene is the most recent scene with <30% overall cloud cover
    intersecting the bbox in the past `days` days.
    """
    last_error: Exception | None = None
    for endpoint in STAC_ENDPOINTS:
        try:
            scenes = _search_endpoint(endpoint, bbox, days)
        except httpx.HTTPError as exc:
            log.warning("STAC %s failed: %s", endpoint["name"], exc)
            last_error = exc
            continue
        if scenes:
            return scenes[0]
    if last_error:
        log.warning("all STAC endpoints failed, last error: %s", last_error)
    return None


def read_window(cog_href: str, bbox: list[float]) -> tuple[np.ndarray, float]:
    """Read a single-band COG window inside bbox (WGS84).

    Returns (array, pixel_area_m2). Uses HTTP range requests via rasterio.
    """
    with rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.jp2",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    ):
        with rasterio.open(cog_href) as src:
            src_bounds = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
            window = from_bounds(*src_bounds, transform=src.transform)
            window = window.round_offsets().round_lengths()
            data = src.read(1, window=window)
            xres, yres = src.res
            return data, float(abs(xres * yres))


def fetch_bands(scene: Scene, bbox: list[float]) -> dict[str, tuple[np.ndarray, float]]:
    """Fetch green, NIR, and SCL windows for the given bbox."""
    return {
        "green": read_window(scene.green_href, bbox),
        "nir": read_window(scene.nir_href, bbox),
        "scl": read_window(scene.scl_href, bbox),
    }


__all__ = ["Scene", "discover_latest_scene", "fetch_bands", "read_window"]
