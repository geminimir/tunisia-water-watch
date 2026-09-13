"""Smoke tests for scripts/calibrate.py — no network."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import calibrate  # noqa: E402


class TestCandidateDetection(unittest.TestCase):
    def _rows(self, values):
        return [{"date": f"2026-0{i+1}-15", "surface_area_km2": str(v) if v is not None else ""}
                for i, v in enumerate(values)]

    def test_candidate_when_five_of_six_are_near_zero(self):
        dams = [{"id": "d1", "name": "D1", "bbox": [0, 0, 1, 1], "lat": 0, "lon": 0,
                 "ndwi_threshold": 0.05, "historical_avg_km2": 5.0}]
        per_dam = {"d1": self._rows([0.001, 0.002, 0.0, "", 0.03, 0.001])}
        self.assertEqual(len(calibrate.candidate_dams(dams, per_dam)), 1)

    def test_not_a_candidate_when_readings_are_healthy(self):
        dams = [{"id": "d1", "name": "D1", "bbox": [0, 0, 1, 1], "lat": 0, "lon": 0,
                 "ndwi_threshold": 0.05, "historical_avg_km2": 5.0}]
        per_dam = {"d1": self._rows([4.0, 5.0, 5.5, 6.0, 4.8, 5.1])}
        self.assertEqual(calibrate.candidate_dams(dams, per_dam), [])

    def test_not_a_candidate_when_history_too_short(self):
        dams = [{"id": "d1", "name": "D1", "bbox": [0, 0, 1, 1], "lat": 0, "lon": 0,
                 "ndwi_threshold": 0.05, "historical_avg_km2": 5.0}]
        per_dam = {"d1": self._rows([0.001, 0.002])}
        self.assertEqual(calibrate.candidate_dams(dams, per_dam), [])


class TestGeometryHelpers(unittest.TestCase):
    def test_polygon_area_unit_square(self):
        self.assertAlmostEqual(calibrate._polygon_pixel_area([[0, 0], [1, 0], [1, 1], [0, 1]]), 1.0)

    def test_polygon_area_ccw_and_cw_equal(self):
        cw = [[0, 0], [0, 3], [4, 3], [4, 0]]
        ccw = [[0, 0], [4, 0], [4, 3], [0, 3]]
        self.assertAlmostEqual(calibrate._polygon_pixel_area(cw),
                               calibrate._polygon_pixel_area(ccw))

    def test_shape_area_subtracts_holes(self):
        geom = {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],   # 100
                [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]],       # -4
            ],
        }
        self.assertAlmostEqual(calibrate._shape_pixel_area(geom), 96.0)

    def test_shape_pixel_bounds(self):
        geom = {"type": "Polygon", "coordinates": [[[3, 4], [7, 4], [7, 9], [3, 9], [3, 4]]]}
        self.assertEqual(calibrate._shape_pixel_bounds(geom), (3, 4, 7, 9))


if __name__ == "__main__":
    unittest.main()
