"""End-to-end oracle rendering test."""

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import render  # noqa: E402


class TestOracle(unittest.TestCase):
    def test_oracle_render_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp) / "data"
            tmp_data.mkdir()
            tmp_site = Path(tmp) / "site"
            csv_path = tmp_data / "readings.csv"
            gov_path = tmp_data / "gov_readings.csv"
            latest_path = tmp_data / "latest.json"

            with csv_path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow([
                    "date", "dam_id", "dam_name", "surface_area_km2", "cloud_pct",
                    "scene_id", "threshold_used", "historical_avg_km2", "pct_of_avg",
                    "surface_area_mndwi_km2", "confidence",
                ])
                w.writerow([
                    "2026-09-10", "sidi-salem", "Sidi Salem", "18.4", "5",
                    "S2B_TEST", "0.05", "28.8", "63.9", "18.2", "0.98",
                ])

            with gov_path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow([
                    "date", "gov_id", "gov_name", "region", "mean_ndvi", "std_ndvi",
                    "mean_ndmi", "healthy_pct", "cloud_pct", "confidence", "scene_id",
                ])
                w.writerow([
                    "2026-09-10", "beja", "Béja", "north", "0.32", "0.12", "0.08", "35",
                    "3", "0.95", "S2B_TEST",
                ])

            with patch.object(render, "READINGS_CSV", csv_path), \
                 patch.object(render, "GOV_CSV", gov_path), \
                 patch.object(render, "LATEST_JSON", latest_path), \
                 patch.object(render, "SITE", tmp_site):
                render.render_site("test/repo")

            oracle_dir = tmp_site / "oracle"
            self.assertTrue((oracle_dir / "index.json").exists())
            self.assertTrue((oracle_dir / "manifest.json").exists())
            self.assertTrue((oracle_dir / "anomalies.json").exists())
            self.assertTrue((oracle_dir / "monthly.json").exists())
            self.assertTrue((oracle_dir / "latest.json").exists())
            self.assertTrue((oracle_dir / "dams" / "sidi-salem.json").exists())
            self.assertTrue((oracle_dir / "governorates" / "beja.json").exists())

            manifest = json.loads((oracle_dir / "manifest.json").read_text())
            self.assertEqual(manifest["algorithm"], "sha256")
            # Every claimed hash must match the actual file bytes.
            for rel, entry in manifest["files"].items():
                fpath = tmp_site / rel
                self.assertTrue(fpath.exists(), f"missing {rel}")
                actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
                self.assertEqual(actual, entry["sha256"], f"hash mismatch for {rel}")

            oracle_idx = json.loads((oracle_dir / "index.json").read_text())
            self.assertEqual(oracle_idx["schema_version"], render.ORACLE_SCHEMA_VERSION)
            self.assertEqual(oracle_idx["counts"]["dams"], 37)
            self.assertEqual(oracle_idx["counts"]["governorates"], 24)


if __name__ == "__main__":
    unittest.main()
