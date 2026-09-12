"""Render the static site from the accumulated CSV."""

from __future__ import annotations

import csv
import json
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
SITE = ROOT / "site"
DATA = ROOT / "data"

READINGS_CSV = DATA / "readings.csv"
LATEST_JSON = DATA / "latest.json"
DAMS_JSON = ROOT / "config" / "dams.json"

# Minimum same-month readings across years before we trust the rolling median
# as a baseline. Below this the hand-guessed value in dams.json is used.
MIN_MONTH_SAMPLES = 3
# Minimum total readings before we trust the rolling annual median.
MIN_ANNUAL_SAMPLES = 12
# Days after which we consider the pipeline stale and surface a red banner.
STALENESS_DAYS = 10


def _load_readings() -> list[dict[str, Any]]:
    if not READINGS_CSV.exists():
        return []
    with READINGS_CSV.open() as fh:
        return list(csv.DictReader(fh))


def _load_dams() -> list[dict[str, Any]]:
    with DAMS_JSON.open() as fh:
        return json.load(fh)["dams"]


def _to_float(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _status(pct_of_avg: float | None) -> str:
    if pct_of_avg is None:
        return "gray"
    if pct_of_avg >= 80:
        return "green"
    if pct_of_avg >= 50:
        return "yellow"
    return "red"


def _trend_symbol(series: list[dict[str, Any]]) -> str:
    values = [p["surface_area_km2"] for p in series if p["surface_area_km2"] is not None]
    if len(values) < 2:
        return "→"
    delta = values[-1] - values[0]
    scale = max(abs(v) for v in values) or 1.0
    if abs(delta) / scale < 0.03:
        return "→"
    return "↑" if delta > 0 else "↓"


def compute_baselines(
    per_dam: dict[str, list[dict[str, Any]]],
    dams_cfg: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per-dam baselines: rolling monthly + annual medians with fallbacks.

    Returns {dam_id: {"annual": float, "monthly": {1..12: float or None}, "source": str}}.
    """
    hand_guessed = {d["id"]: float(d["historical_avg_km2"]) for d in dams_cfg}
    out: dict[str, dict[str, Any]] = {}
    for dam_id, series in per_dam.items():
        by_month: dict[int, list[float]] = defaultdict(list)
        all_vals: list[float] = []
        for row in series:
            v = row.get("surface_area_km2")
            if v is None:
                continue
            try:
                month = int(row["date"][5:7])
            except (ValueError, KeyError):
                continue
            by_month[month].append(float(v))
            all_vals.append(float(v))
        monthly: dict[int, float | None] = {}
        for m in range(1, 13):
            samples = by_month.get(m, [])
            monthly[m] = statistics.median(samples) if len(samples) >= MIN_MONTH_SAMPLES else None
        annual: float
        source: str
        if len(all_vals) >= MIN_ANNUAL_SAMPLES:
            annual = statistics.median(all_vals)
            source = "rolling"
        else:
            annual = hand_guessed.get(dam_id, 0.0)
            source = "config"
        out[dam_id] = {"annual": annual, "monthly": monthly, "source": source}
    # Fill in dams with no readings at all.
    for d in dams_cfg:
        if d["id"] not in out:
            out[d["id"]] = {
                "annual": hand_guessed[d["id"]],
                "monthly": {m: None for m in range(1, 13)},
                "source": "config",
            }
    return out


def effective_baseline(
    baseline: dict[str, Any], reading_date: str | None, hand_guessed: float
) -> tuple[float, str]:
    """Pick the best baseline for a given reading date. Returns (value, source_tag).

    A rolling baseline is only trusted if it clears a plausibility floor —
    otherwise the dam's bbox is probably misconfigured (systematically reads
    near-zero across history) and dividing current readings by ~0 produces
    absurd pct_of_avg values. We fall back to the hand-guessed config value.
    """
    floor = max(0.02, hand_guessed * 0.05)
    monthly = None
    if reading_date and len(reading_date) >= 7:
        try:
            m = int(reading_date[5:7])
            monthly = baseline["monthly"].get(m)
        except ValueError:
            monthly = None
    if monthly is not None and monthly >= floor:
        return monthly, "rolling-monthly"
    annual = baseline["annual"] if baseline["source"] == "rolling" else None
    if annual is not None and annual >= floor:
        return annual, "rolling-annual"
    return hand_guessed, "config"


def _pct(value: float | None, baseline: float) -> float | None:
    if value is None or baseline <= 0:
        return None
    return 100.0 * value / baseline


def _staleness(rows: list[dict[str, Any]], now: datetime) -> tuple[int | None, bool]:
    dated = [r for r in rows if r.get("surface_area_km2")]
    if not dated:
        return None, True
    try:
        latest = max(datetime.strptime(r["date"], "%Y-%m-%d") for r in dated)
    except ValueError:
        return None, True
    days = (now.replace(tzinfo=None) - latest).days
    return days, days > STALENESS_DAYS


def _drought_index(dam_view: list[dict[str, Any]]) -> float | None:
    """Capacity-weighted mean of pct_of_avg over dams with a current reading."""
    num = 0.0
    den = 0.0
    for d in dam_view:
        if d["pct_of_avg"] is None:
            continue
        w = float(d.get("capacity_hm3") or 0.0) or 1.0
        num += d["pct_of_avg"] * w
        den += w
    if den == 0:
        return None
    return num / den


def render_site(github_repo: str = "geminimir/tunisia-water-watch") -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    dams_cfg = _load_dams()
    rows = _load_readings()
    per_dam: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        per_dam[r["dam_id"]].append(
            {
                "date": r["date"],
                "surface_area_km2": _to_float(r.get("surface_area_km2")),
                "cloud_pct": _to_float(r.get("cloud_pct")),
                "pct_of_avg": _to_float(r.get("pct_of_avg")),
                "scene_id": r.get("scene_id") or "",
            }
        )
    for k in per_dam:
        per_dam[k].sort(key=lambda x: x["date"])

    baselines = compute_baselines(per_dam, dams_cfg)
    hand_guessed = {d["id"]: float(d["historical_avg_km2"]) for d in dams_cfg}

    # Overlay effective pct_of_avg on each series row (does not modify the CSV).
    for dam_id, series in per_dam.items():
        for row in series:
            b, _ = effective_baseline(baselines[dam_id], row["date"], hand_guessed[dam_id])
            row["pct_of_avg"] = _pct(row["surface_area_km2"], b)

    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    next_run_at = (now + timedelta(days=5)).strftime("%Y-%m-%d")
    stale_days, is_stale = _staleness(rows, now)

    dam_view: list[dict[str, Any]] = []
    total_area = 0.0
    total_avg = 0.0
    dams_read = 0
    for d in dams_cfg:
        series = per_dam.get(d["id"], [])
        latest = series[-1] if series else None
        base_val, base_src = effective_baseline(
            baselines[d["id"]],
            latest["date"] if latest else None,
            hand_guessed[d["id"]],
        )
        pct = latest["pct_of_avg"] if latest else None
        area = latest["surface_area_km2"] if latest else None
        if area is not None:
            total_area += area
            total_avg += base_val
            dams_read += 1
        dam_view.append(
            {
                "id": d["id"],
                "name": d["name"],
                "governorate": d["governorate"],
                "river": d["river"],
                "lat": d["lat"],
                "lon": d["lon"],
                "historical_avg_km2": d["historical_avg_km2"],
                "effective_baseline_km2": round(base_val, 2),
                "baseline_source": base_src,
                "capacity_hm3": d.get("capacity_hm3", 0),
                "ndwi_threshold": d["ndwi_threshold"],
                "last_date": latest["date"] if latest else None,
                "surface_area_km2": area,
                "pct_of_avg": pct,
                "status": _status(pct),
            }
        )

    # National daily total series (only days where >=1 dam was read)
    date_totals: dict[str, float] = defaultdict(float)
    for r in rows:
        area = _to_float(r.get("surface_area_km2"))
        if area is not None:
            date_totals[r["date"]] += area
    national_series = [{"date": k, "total_area_km2": round(v, 2)} for k, v in sorted(date_totals.items())]

    drought_index = _drought_index(dam_view)

    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "dam").mkdir(parents=True, exist_ok=True)
    (SITE / "assets").mkdir(parents=True, exist_ok=True)

    common = {
        "generated_at": generated_at,
        "next_run_at": next_run_at,
        "github_repo": github_repo,
        "stale_days": stale_days,
        "is_stale": is_stale,
        "staleness_days": STALENESS_DAYS,
    }

    total_avg_ref = total_avg or 1.0
    (SITE / "index.html").write_text(
        env.get_template("index.html").render(
            page_title="Dams",
            root="",
            dams=dam_view,
            dams_read=dams_read,
            total_area_km2=total_area,
            pct_of_avg=(100.0 * total_area / total_avg_ref) if total_avg else 0.0,
            drought_index=drought_index,
            dams_json=json.dumps(dam_view),
            national_series_json=json.dumps(national_series),
            **common,
        )
    )

    (SITE / "agriculture.html").write_text(
        env.get_template("agriculture.html").render(
            page_title="Agriculture", root="", **common
        )
    )
    (SITE / "about.html").write_text(
        env.get_template("about.html").render(page_title="About", root="", **common)
    )

    dam_tpl = env.get_template("dam.html")
    for d in dam_view:
        series = per_dam.get(d["id"], [])
        recent = list(reversed(series[-24:]))
        latest = series[-1] if series else {
            "date": None, "surface_area_km2": None, "pct_of_avg": None
        }
        (SITE / "dam" / f"{d['id']}.html").write_text(
            dam_tpl.render(
                page_title=d["name"],
                root="../",
                dam=d,
                latest=latest,
                recent=recent,
                series_json=json.dumps(series),
                trend_symbol=_trend_symbol(series[-6:] if len(series) >= 2 else series),
                **common,
            )
        )

    latest_json = {
        "generated_at": generated_at,
        "stale_days": stale_days,
        "national_drought_index": drought_index,
        "national_surface_area_km2": round(total_area, 2),
        "national_baseline_km2": round(total_avg, 2),
        "dams": [
            {
                "id": d["id"], "name": d["name"], "governorate": d["governorate"],
                "lat": d["lat"], "lon": d["lon"],
                "date": d["last_date"], "surface_area_km2": d["surface_area_km2"],
                "historical_avg_km2": d["historical_avg_km2"],
                "effective_baseline_km2": d["effective_baseline_km2"],
                "baseline_source": d["baseline_source"],
                "pct_of_avg": d["pct_of_avg"], "status": d["status"],
            }
            for d in dam_view
        ],
    }
    LATEST_JSON.parent.mkdir(parents=True, exist_ok=True)
    latest_body = json.dumps(latest_json, indent=2)
    LATEST_JSON.write_text(latest_body)
    # Also expose data files under site/ so GitHub Pages serves them.
    site_data = SITE / "data"
    site_data.mkdir(parents=True, exist_ok=True)
    (site_data / "latest.json").write_text(latest_body)
    if READINGS_CSV.exists():
        (site_data / "readings.csv").write_bytes(READINGS_CSV.read_bytes())


if __name__ == "__main__":
    render_site(os.environ.get("GITHUB_REPOSITORY", "geminimir/tunisia-water-watch"))
