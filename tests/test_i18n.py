"""Sanity checks for the translation table."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import i18n  # noqa: E402


class TestI18nCoverage(unittest.TestCase):
    def test_no_missing_translation_keys(self):
        missing = i18n.coverage_check()
        self.assertEqual(missing, [], f"missing translations: {missing}")

    def test_helpers_default_to_input_for_unknown_values(self):
        self.assertEqual(i18n.translate_region("mars", "fr"), "mars")
        self.assertEqual(i18n.translate_focus("magic-beans", "ar"), "magic-beans")
        self.assertEqual(i18n.translate_band("unknown", "fr"), "inconnu")

    def test_t_for_returns_all_keys(self):
        fr = i18n.t_for("fr")
        ar = i18n.t_for("ar")
        self.assertEqual(set(fr), set(ar), "fr and ar should expose the same keys")
        # Spot-check
        self.assertEqual(fr["nav_dams"], "Barrages")
        self.assertEqual(ar["nav_dams"], "السدود")
        self.assertNotEqual(fr["site_description"], ar["site_description"])


class TestConfigsHaveArabicNames(unittest.TestCase):
    def test_dams(self):
        cfg = json.loads((ROOT / "config" / "dams.json").read_text())
        for d in cfg["dams"]:
            self.assertIn("name_ar", d, f"dam {d['id']} missing name_ar")
            self.assertTrue(d["name_ar"], f"dam {d['id']} name_ar is empty")

    def test_governorates(self):
        cfg = json.loads((ROOT / "config" / "governorates.json").read_text())
        for g in cfg["governorates"]:
            self.assertIn("name_ar", g, f"gov {g['id']} missing name_ar")
            self.assertTrue(g["name_ar"], f"gov {g['id']} name_ar is empty")


if __name__ == "__main__":
    unittest.main()
