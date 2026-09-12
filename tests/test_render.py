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
            self.assertTrue((tmp_site / "index.html").exists())
            self.assertTrue((tmp_site / "dam" / "sidi-salem.html").exists())
            self.assertTrue((tmp_site / "about.html").exists())
            latest = json.loads((tmp_data / "latest.json").read_text())
            self.assertIn("dams", latest)
            sidi = next(d for d in latest["dams"] if d["id"] == "sidi-salem")
            self.assertAlmostEqual(sidi["surface_area_km2"], 18.6, places=1)


if __name__ == "__main__":
    unittest.main()
