"""Tests for anomalies + composite drought severity."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from anomalies import (  # noqa: E402
    composite_severity, series_anomaly, severity_band,
    _severity_from_pct, _severity_from_z,
)


def _series(month: int, values: list[float]) -> list[dict]:
    return [
        {"date": f"20{20 + i:02d}-{month:02d}-15", "surface_area_km2": v}
        for i, v in enumerate(values)
    ]


class TestAnomaly(unittest.TestCase):
    def test_z_score_from_history(self):
        series = _series(6, [10.0, 11.0, 10.5, 9.5, 10.2, 6.0])  # last is the anomaly
        a = series_anomaly(series, "surface_area_km2", "2025-06-15", min_samples=5)
        self.assertIsNotNone(a.z_score)
        self.assertLess(a.z_score, -2.0)

    def test_z_score_needs_min_samples(self):
        series = _series(6, [10.0, 11.0])
        a = series_anomaly(series, "surface_area_km2", "2025-06-15", min_samples=5)
        self.assertIsNone(a.z_score)

    def test_percent_rank_is_reasonable(self):
        series = _series(6, [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 5.0])
        a = series_anomaly(series, "surface_area_km2", "2025-06-15", min_samples=5)
        # 5.0 is below every historical value
        self.assertLessEqual(a.percent_rank, 20.0)


class TestSeverity(unittest.TestCase):
    def test_pct_of_avg_low_gives_high_severity(self):
        self.assertGreater(_severity_from_pct(30), 80)

    def test_pct_of_avg_normal(self):
        self.assertAlmostEqual(_severity_from_pct(100), 50.0)

    def test_z_score_negative_gives_high_severity(self):
        self.assertGreater(_severity_from_z(-2.0), 70)

    def test_composite_all_signals(self):
        score, br = composite_severity(
            dam_pct_of_avg=50.0, ndvi_z=-1.5, ndmi_z=-1.0,
            dam_confidence=0.9, veg_confidence=0.8,
        )
        self.assertIsNotNone(score)
        self.assertGreater(score, 55.0)
        self.assertEqual(len(br["components"]), 3)

    def test_composite_missing_signals(self):
        score, br = composite_severity(
            dam_pct_of_avg=None, ndvi_z=1.0, ndmi_z=None,
        )
        self.assertIsNotNone(score)
        self.assertEqual(len(br["components"]), 1)

    def test_composite_no_signal_returns_none(self):
        score, _ = composite_severity(None, None, None)
        self.assertIsNone(score)

    def test_band_thresholds(self):
        self.assertEqual(severity_band(None), "unknown")
        self.assertEqual(severity_band(20), "abundant")
        self.assertEqual(severity_band(40), "normal")
        self.assertEqual(severity_band(60), "watch")
        self.assertEqual(severity_band(70), "drought")
        self.assertEqual(severity_band(90), "severe")


if __name__ == "__main__":
    unittest.main()
