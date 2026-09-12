"""Unit tests for the extended spectral index and vegetation modules."""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute import (  # noqa: E402
    compute_mndwi, compute_ndvi, compute_ndmi,
    read_dam, read_governorate,
)


class TestSpectralIndices(unittest.TestCase):
    def test_mndwi_water_positive(self):
        # Water: green much higher than SWIR
        g = np.array([[3000, 3000]], dtype=np.uint16)
        swir = np.array([[500, 500]], dtype=np.uint16)
        m = compute_mndwi(g, swir)
        self.assertTrue((m > 0.5).all())

    def test_ndvi_vegetation_positive(self):
        # Healthy veg: NIR high, red low
        nir = np.array([[3500]], dtype=np.uint16)
        red = np.array([[600]], dtype=np.uint16)
        n = compute_ndvi(nir, red)
        self.assertGreater(float(n[0, 0]), 0.5)

    def test_ndvi_bare_low(self):
        nir = np.array([[800]], dtype=np.uint16)
        red = np.array([[900]], dtype=np.uint16)
        self.assertLess(float(compute_ndvi(nir, red)[0, 0]), 0.05)

    def test_ndmi_moist_positive(self):
        # Moist canopy: NIR high, SWIR moderate
        nir = np.array([[3500]], dtype=np.uint16)
        swir = np.array([[1500]], dtype=np.uint16)
        self.assertGreater(float(compute_ndmi(nir, swir)[0, 0]), 0.3)

    def test_ndmi_dry_negative(self):
        # Dry: SWIR > NIR
        nir = np.array([[1200]], dtype=np.uint16)
        swir = np.array([[2000]], dtype=np.uint16)
        self.assertLess(float(compute_ndmi(nir, swir)[0, 0]), 0.0)


class TestDualIndexAgreement(unittest.TestCase):
    def test_high_agreement_when_water_clean(self):
        # Uniform water bbox → NDWI and MNDWI should agree closely.
        g = np.ones((20, 20), dtype=np.uint16) * 3000
        nir = np.ones((20, 20), dtype=np.uint16) * 500
        swir = np.ones((20, 20), dtype=np.uint16) * 400
        scl = np.ones((20, 20), dtype=np.uint8) * 6  # water class
        r = read_dam(g, 100.0, nir, scl, threshold=0.0, swir=swir)
        self.assertIsNotNone(r.surface_area_km2)
        self.assertIsNotNone(r.surface_area_mndwi_km2)
        self.assertIsNotNone(r.confidence)
        self.assertGreater(r.confidence, 0.9)

    def test_missing_swir_yields_no_confidence(self):
        g = np.ones((10, 10), dtype=np.uint16) * 3000
        nir = np.ones((10, 10), dtype=np.uint16) * 500
        scl = np.ones((10, 10), dtype=np.uint8) * 6
        r = read_dam(g, 100.0, nir, scl, threshold=0.0)
        self.assertIsNotNone(r.surface_area_km2)
        self.assertIsNone(r.surface_area_mndwi_km2)
        self.assertIsNone(r.confidence)


class TestGovernorateReader(unittest.TestCase):
    def test_healthy_field(self):
        red = np.ones((30, 30), dtype=np.uint16) * 600
        nir = np.ones((30, 30), dtype=np.uint16) * 3500
        swir = np.ones((30, 30), dtype=np.uint16) * 1500
        scl = np.ones((30, 30), dtype=np.uint8) * 4  # veg
        r = read_governorate(red, 100.0, nir, swir, scl)
        self.assertGreater(r.mean_ndvi, 0.5)
        self.assertGreater(r.mean_ndmi, 0.3)
        self.assertGreater(r.healthy_pct, 90)
        self.assertGreater(r.confidence, 0.9)

    def test_dry_field(self):
        red = np.ones((30, 30), dtype=np.uint16) * 1200
        nir = np.ones((30, 30), dtype=np.uint16) * 1500
        swir = np.ones((30, 30), dtype=np.uint16) * 2500
        scl = np.ones((30, 30), dtype=np.uint8) * 5  # not-vegetated bare
        r = read_governorate(red, 100.0, nir, swir, scl)
        self.assertLess(r.mean_ndvi, 0.2)
        self.assertLess(r.mean_ndmi, 0.0)
        self.assertLess(r.healthy_pct, 10)

    def test_skips_on_high_cloud(self):
        shape = (30, 30)
        red = np.ones(shape, dtype=np.uint16) * 1000
        nir = np.ones(shape, dtype=np.uint16) * 2000
        swir = np.ones(shape, dtype=np.uint16) * 1500
        scl = np.ones(shape, dtype=np.uint8) * 9  # all cloud
        r = read_governorate(red, 100.0, nir, swir, scl)
        self.assertIsNone(r.mean_ndvi)
        self.assertGreater(r.cloud_pct, 50.0)


class TestGovConfig(unittest.TestCase):
    def test_governorates_file(self):
        import json
        p = ROOT / "config" / "governorates.json"
        data = json.loads(p.read_text())
        self.assertEqual(len(data["governorates"]), 24)
        ids = [g["id"] for g in data["governorates"]]
        self.assertEqual(len(set(ids)), len(ids))
        for g in data["governorates"]:
            for k in ("id", "name", "region", "lat", "lon", "bbox", "weight", "focus"):
                self.assertIn(k, g)
            w, s, e, n = g["bbox"]
            self.assertLess(w, e)
            self.assertLess(s, n)


if __name__ == "__main__":
    unittest.main()
