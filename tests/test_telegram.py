"""Tests for the opt-in Telegram alerter (no network)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import telegram  # noqa: E402


class TestTelegramGate(unittest.TestCase):
    def test_noop_when_secrets_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(telegram.maybe_alert(dry_run=True), [])


class TestTelegramMessaging(unittest.TestCase):
    def test_worsened_detection(self):
        self.assertTrue(telegram._worsened("normal", "drought"))
        self.assertTrue(telegram._worsened("watch", "severe"))
        self.assertFalse(telegram._worsened("drought", "drought"))
        self.assertFalse(telegram._worsened("severe", "watch"))

    def test_dry_run_emits_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = Path(tmp) / "latest.json"
            state = Path(tmp) / "state.json"
            latest.write_text(json.dumps({
                "generated_at": "2026-09-12 22:00 UTC",
                "dams": [
                    {"id": "sidi-salem", "name": "Sidi Salem", "governorate": "Béja",
                     "surface_area_km2": 5.0, "pct_of_avg": 15.0, "date": "2026-09-12",
                     "severity_band": "severe"},
                ],
                "governorates": [
                    {"id": "beja", "name": "Béja", "region": "north",
                     "mean_ndvi": 0.1, "mean_ndmi": -0.2, "date": "2026-09-12",
                     "severity_band": "drought"},
                ],
            }))
            state.write_text(json.dumps({"dams": {}, "governorates": {}}))
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"}), \
                 patch.object(telegram, "LATEST_JSON", latest), \
                 patch.object(telegram, "STATE_FILE", state):
                msgs = telegram.maybe_alert(dry_run=True)
            self.assertEqual(len(msgs), 2)
            self.assertIn("Sidi Salem", msgs[0])
            self.assertIn("SEVERE", msgs[0])
            self.assertIn("Béja", msgs[1])
            self.assertIn("DROUGHT", msgs[1])


if __name__ == "__main__":
    unittest.main()
