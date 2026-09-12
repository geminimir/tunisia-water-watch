import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compute import (  # noqa: E402
    Reading,
    cloud_percent,
    compute_ndwi,
    read_dam,
    surface_area_km2,
    water_mask,
)


class TestCompute(unittest.TestCase):
    def test_ndwi_water_positive(self):
        # Water: high green, low NIR → NDWI > 0
        g = np.array([[3000, 3000], [3000, 3000]], dtype=np.uint16)
        n = np.array([[500, 500], [500, 500]], dtype=np.uint16)
        ndwi = compute_ndwi(g, n)
        self.assertTrue((ndwi > 0.5).all())

    def test_ndwi_land_negative(self):
        # Vegetation: low green, high NIR → NDWI < 0
        g = np.array([[500, 500]], dtype=np.uint16)
        n = np.array([[3000, 3000]], dtype=np.uint16)
        ndwi = compute_ndwi(g, n)
        self.assertTrue((ndwi < 0).all())

    def test_ndwi_zero_safe(self):
        g = np.zeros((2, 2), dtype=np.uint16)
        n = np.zeros((2, 2), dtype=np.uint16)
        ndwi = compute_ndwi(g, n)
        self.assertTrue(np.isfinite(ndwi).all())

    def test_cloud_percent(self):
        scl = np.array([[9, 4, 4, 8], [3, 4, 4, 10]], dtype=np.uint8)
        # cloud classes: 8, 9, 10, 3 → 4 of 8
        self.assertAlmostEqual(cloud_percent(scl), 50.0)

    def test_water_mask_excludes_clouds(self):
        ndwi = np.array([[0.5, 0.5], [0.5, 0.5]], dtype=np.float32)
        scl = np.array([[4, 9], [4, 4]], dtype=np.uint8)  # class 9 = cloud
        m = water_mask(ndwi, 0.0, scl)
        self.assertEqual(m.sum(), 3)

    def test_surface_area(self):
        mask = np.ones((10, 10), dtype=bool)
        # pixel area 100 m² → 100 * 100 * 100 = 10_000 m² = 0.01 km²
        self.assertAlmostEqual(surface_area_km2(mask, 100.0), 0.01, places=6)

    def test_read_dam_skips_on_high_cloud(self):
        g = np.ones((10, 10), dtype=np.uint16) * 3000
        n = np.ones((10, 10), dtype=np.uint16) * 500
        scl = np.ones((10, 10), dtype=np.uint8) * 9  # 100% cloud
        r = read_dam(g, 100.0, n, scl, threshold=0.0, max_cloud_pct=50.0)
        self.assertIsNone(r.surface_area_km2)
        self.assertGreater(r.cloud_pct, 50.0)

    def test_read_dam_returns_area(self):
        g = np.ones((10, 10), dtype=np.uint16) * 3000
        n = np.ones((10, 10), dtype=np.uint16) * 500
        scl = np.ones((10, 10), dtype=np.uint8) * 4  # vegetation, no cloud
        r = read_dam(g, 100.0, n, scl, threshold=0.0, max_cloud_pct=50.0)
        self.assertIsNotNone(r.surface_area_km2)
        self.assertAlmostEqual(r.surface_area_km2, 0.01, places=6)


class TestDamsConfig(unittest.TestCase):
    def test_dams_file_shape(self):
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "config" / "dams.json"
        data = json.loads(p.read_text())
        self.assertIn("dams", data)
        self.assertEqual(len(data["dams"]), 37)
        ids = [d["id"] for d in data["dams"]]
        self.assertEqual(len(set(ids)), len(ids), "dam IDs must be unique")
        for d in data["dams"]:
            for k in ("id", "name", "lat", "lon", "bbox", "ndwi_threshold", "historical_avg_km2"):
                self.assertIn(k, d, f"missing {k} in {d.get('id')}")
            self.assertEqual(len(d["bbox"]), 4)
            w, s, e, n = d["bbox"]
            self.assertLess(w, e, f"{d['id']} bbox west >= east")
            self.assertLess(s, n, f"{d['id']} bbox south >= north")
            self.assertGreaterEqual(d["lat"], 30)  # Tunisia latitude sanity
            self.assertLessEqual(d["lat"], 38)
            self.assertGreaterEqual(d["lon"], 7)
            self.assertLessEqual(d["lon"], 12)


if __name__ == "__main__":
    unittest.main()
