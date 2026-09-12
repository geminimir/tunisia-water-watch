"""Phase 3: opt-in Telegram alerts.

If TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in the environment (as
GitHub Actions secrets, typically), post an alert message summarizing any
new drought-severity events since the last successful post. If either
variable is missing, this is a silent no-op — the system continues to run
unattended, aligned with the "no expiring credentials" design principle.

State: data/telegram_state.json tracks the last-alerted severity per
dam and governorate so we don't spam the channel on every 5-day run.

One-time setup (~5 minutes, no ongoing maintenance):
    1. Message @BotFather on Telegram, run /newbot, pick a name.
    2. Copy the bot token.
    3. Message the bot, then GET https://api.telegram.org/bot<TOKEN>/getUpdates
       to find your chat_id.
    4. Store TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as repository secrets.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("tww.telegram")

ROOT = Path(__file__).resolve().parent.parent
LATEST_JSON = ROOT / "data" / "latest.json"
STATE_FILE = ROOT / "data" / "telegram_state.json"
TELEGRAM_API = "https://api.telegram.org"

# Severity bands from anomalies.py — alert on transitions into "drought" or "severe"
ALERT_BANDS = {"drought", "severe"}
ALERT_BAND_RANK = {"abundant": 0, "normal": 1, "watch": 2, "drought": 3, "severe": 4, "unknown": -1}


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"dams": {}, "governorates": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"dams": {}, "governorates": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _post(token: str, chat_id: str, text: str) -> bool:
    try:
        r = httpx.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15.0,
        )
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("telegram post failed: %s", exc)
        return False


def _format_dam_alert(dam: dict, severity: str) -> str:
    pct = dam.get("pct_of_avg")
    pct_s = f"{pct:.0f}%" if pct is not None else "—"
    return (
        f"💧 *{dam['name']}* ({dam.get('governorate', '')}) → *{severity.upper()}*\n"
        f"Surface: {dam.get('surface_area_km2', '?')} km² ({pct_s} of typical)\n"
        f"Reading: {dam.get('date', '?')}"
    )


def _format_gov_alert(gov: dict, severity: str) -> str:
    ndvi = gov.get("mean_ndvi")
    ndmi = gov.get("mean_ndmi")
    return (
        f"🌾 *{gov['name']}* ({gov.get('region', '')}) → *{severity.upper()}*\n"
        f"NDVI: {ndvi if ndvi is not None else '—'}, "
        f"NDMI: {ndmi if ndmi is not None else '—'}\n"
        f"Reading: {gov.get('date', '?')}"
    )


def _worsened(prev: str, cur: str) -> bool:
    return ALERT_BAND_RANK.get(cur, -1) > ALERT_BAND_RANK.get(prev, -1)


def maybe_alert(dry_run: bool = False) -> list[str]:
    """Send alerts if configured; returns the list of messages that were (or would be) sent."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("telegram alerts skipped (secrets not set)")
        return []
    if not LATEST_JSON.exists():
        log.info("telegram alerts skipped (no latest.json)")
        return []
    latest = json.loads(LATEST_JSON.read_text())
    state = _load_state()
    messages: list[str] = []

    for dam in latest.get("dams", []):
        band = dam.get("severity_band") or "unknown"
        prev = state["dams"].get(dam["id"], "unknown")
        if band in ALERT_BANDS and _worsened(prev, band):
            messages.append(_format_dam_alert(dam, band))
        state["dams"][dam["id"]] = band

    for gov in latest.get("governorates", []):
        band = gov.get("severity_band") or "unknown"
        prev = state["governorates"].get(gov["id"], "unknown")
        if band in ALERT_BANDS and _worsened(prev, band):
            messages.append(_format_gov_alert(gov, band))
        state["governorates"][gov["id"]] = band

    if messages and not dry_run:
        header = (
            f"🚨 Tunisia Water Watch alert — "
            f"{latest.get('generated_at', '?')}\n"
            f"Dashboard: https://geminimir.github.io/water-watch/"
        )
        _post(token, chat_id, header)
        for m in messages:
            _post(token, chat_id, m)
        _save_state(state)
    elif messages:
        log.info("dry-run: would send %d messages", len(messages))
    else:
        log.info("no new alerts")
        _save_state(state)
    return messages


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    maybe_alert()
