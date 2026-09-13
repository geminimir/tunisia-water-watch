import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import render  # noqa: E402


class TestRender(unittest.TestCase):
    def test_status_thresholds(self):
        self.assertEqual(render._status(None), "gray")
        self.assertEqual(render._status(90), "green")
        self.assertEqual(render._status(65), "yellow")
        self.assertEqual(render._status(30), "red")

    def test_trend_symbol_flat_empty(self):
        self.assertEqual(render._trend_symbol([]), "→")
        self.assertEqual(render._trend_symbol([{"surface_area_km2": 1.0}]), "→")

    def test_trend_symbol_up_and_down(self):
        up = [{"surface_area_km2": 10.0}, {"surface_area_km2": 20.0}]
        down = [{"surface_area_km2": 20.0}, {"surface_area_km2": 10.0}]
        self.assertEqual(render._trend_symbol(up), "↑")
        self.assertEqual(render._trend_symbol(down), "↓")

    def test_render_end_to_end(self):
        # Use a temp csv but the real templates and dams.json
        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp) / "data"
            tmp_data.mkdir()
            tmp_site = Path(tmp) / "site"
            csv_path = tmp_data / "readings.csv"
            with csv_path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(render.CSV_HEADER if hasattr(render, "CSV_HEADER") else [
                    "date", "dam_id", "dam_name", "surface_area_km2", "cloud_pct",
                    "scene_id", "threshold_used", "historical_avg_km2", "pct_of_avg",
                ])
                w.writerow([
                    "2026-09-10", "sidi-salem", "Sidi Salem", "18.4", "12",
                    "S2B_MSIL2A_TEST", "0.05", "28.8", "63.9",
                ])
                w.writerow([
                    "2026-09-11", "sidi-salem", "Sidi Salem", "18.6", "5",
                    "S2A_MSIL2A_TEST", "0.05", "28.8", "64.6",
                ])
            with patch.object(render, "READINGS_CSV", csv_path), \
                 patch.object(render, "SITE", tmp_site), \
                 patch.object(render, "LATEST_JSON", tmp_data / "latest.json"):
                render.render_site("test/repo")
            # Language picker at root
            self.assertTrue((tmp_site / "index.html").exists())
            # Both language trees
            for lang in ("fr", "ar"):
                self.assertTrue((tmp_site / lang / "index.html").exists())
                self.assertTrue((tmp_site / lang / "dam" / "sidi-salem.html").exists())
                self.assertTrue((tmp_site / lang / "about.html").exists())
                self.assertTrue((tmp_site / lang / "agriculture.html").exists())
            # Arabic page uses RTL
            ar_index = (tmp_site / "ar" / "index.html").read_text()
            self.assertIn('dir="rtl"', ar_index)
            self.assertIn('lang="ar"', ar_index)
            # French page uses LTR
            fr_index = (tmp_site / "fr" / "index.html").read_text()
            self.assertIn('dir="ltr"', fr_index)
            # latest.json still language-neutral
            latest = json.loads((tmp_data / "latest.json").read_text())
            self.assertIn("dams", latest)
            sidi = next(d for d in latest["dams"] if d["id"] == "sidi-salem")
            self.assertAlmostEqual(sidi["surface_area_km2"], 18.6, places=1)
            self.assertEqual(sidi["name_ar"], "سيدي سالم")


class TestBaselines(unittest.TestCase):
    def _series(self, dates_and_values):
        return [
            {"date": d, "surface_area_km2": v, "pct_of_avg": None, "cloud_pct": 0, "scene_id": ""}
            for d, v in dates_and_values
        ]

    def test_baseline_falls_back_to_config_when_thin(self):
        per_dam = {"x": self._series([("2026-01-15", 5.0), ("2026-02-15", 6.0)])}
        cfg = [{"id": "x", "historical_avg_km2": 10.0}]
        b = render.compute_baselines(per_dam, cfg)
        self.assertEqual(b["x"]["source"], "config")
        self.assertEqual(b["x"]["annual"], 10.0)
        self.assertIsNone(b["x"]["monthly"][1])

    def test_baseline_uses_rolling_when_enough_samples(self):
        # 12 readings, 3+ in month 6
        rows = []
        for year in (2019, 2020, 2021, 2022):
            for month in (3, 6, 9):
                rows.append((f"{year}-{month:02d}-15", 10.0 + year - 2019))
        per_dam = {"x": self._series(rows)}
        cfg = [{"id": "x", "historical_avg_km2": 100.0}]
        b = render.compute_baselines(per_dam, cfg)
        self.assertEqual(b["x"]["source"], "rolling")
        self.assertAlmostEqual(b["x"]["annual"], 11.5, places=1)
        self.assertIsNotNone(b["x"]["monthly"][6])

    def test_effective_baseline_prefers_monthly(self):
        base = {"annual": 20.0, "monthly": {6: 15.0, 7: None}, "source": "rolling"}
        val, tag = render.effective_baseline(base, "2026-06-15", 30.0)
        self.assertEqual(val, 15.0)
        self.assertEqual(tag, "rolling-monthly")
        val, tag = render.effective_baseline(base, "2026-07-15", 30.0)
        self.assertEqual(val, 20.0)
        self.assertEqual(tag, "rolling-annual")

    def test_effective_baseline_falls_back_to_config(self):
        base = {"annual": 0.0, "monthly": {6: None}, "source": "config"}
        val, tag = render.effective_baseline(base, "2026-06-15", 12.5)
        self.assertEqual(val, 12.5)
        self.assertEqual(tag, "config")

    def test_effective_baseline_plausibility_floor(self):
        # Rolling values that fall below 5% of the config value (or 0.02 km²)
        # are treated as bbox misconfiguration and skipped.
        base = {"annual": 0.01, "monthly": {6: 0.005}, "source": "rolling"}
        val, tag = render.effective_baseline(base, "2026-06-15", 10.0)
        self.assertEqual(val, 10.0)
        self.assertEqual(tag, "config")


class TestDroughtIndex(unittest.TestCase):
    def test_capacity_weighted(self):
        dams = [
            {"pct_of_avg": 50.0, "capacity_hm3": 500},
            {"pct_of_avg": 100.0, "capacity_hm3": 100},
        ]
        idx = render._drought_index(dams)
        self.assertAlmostEqual(idx, (50 * 500 + 100 * 100) / 600, places=2)

    def test_ignores_missing_readings(self):
        dams = [
            {"pct_of_avg": None, "capacity_hm3": 500},
            {"pct_of_avg": 80.0, "capacity_hm3": 100},
        ]
        self.assertAlmostEqual(render._drought_index(dams), 80.0, places=2)

    def test_no_readings_returns_none(self):
        self.assertIsNone(render._drought_index([{"pct_of_avg": None, "capacity_hm3": 1}]))


class TestStaleness(unittest.TestCase):
    def test_fresh(self):
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)).strftime("%Y-%m-%d")
        days, stale = render._staleness(
            [{"date": recent, "surface_area_km2": "1.0"}], datetime.now(timezone.utc).replace(tzinfo=None)
        )
        self.assertFalse(stale)
        self.assertLessEqual(days, 3)

    def test_stale(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)).strftime("%Y-%m-%d")
        days, stale = render._staleness(
            [{"date": old, "surface_area_km2": "1.0"}], datetime.now(timezone.utc).replace(tzinfo=None)
        )
        self.assertTrue(stale)
        self.assertGreaterEqual(days, 29)

    def test_empty(self):
        from datetime import datetime, timezone
        days, stale = render._staleness([], datetime.now(timezone.utc).replace(tzinfo=None))
        self.assertTrue(stale)
        self.assertIsNone(days)


if __name__ == "__main__":
    unittest.main()
