"""Render the static site from the accumulated CSV."""

from __future__ import annotations

import csv
import json
import os
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

    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    next_run_at = (now + timedelta(days=5)).strftime("%Y-%m-%d")

    dam_view: list[dict[str, Any]] = []
    total_area = 0.0
    total_avg = 0.0
    dams_read = 0
    for d in dams_cfg:
        series = per_dam.get(d["id"], [])
        latest = series[-1] if series else None
        pct = latest["pct_of_avg"] if latest else None
        area = latest["surface_area_km2"] if latest else None
        if area is not None:
            total_area += area
            total_avg += d["historical_avg_km2"]
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
                "capacity_hm3": d.get("capacity_hm3", 0),
                "ndwi_threshold": d["ndwi_threshold"],
                "last_date": latest["date"] if latest else None,
                "surface_area_km2": area,
                "pct_of_avg": pct,
                "status": _status(pct),
            }
        )

    # national daily total series
    date_totals: dict[str, float] = defaultdict(float)
    for r in rows:
        area = _to_float(r.get("surface_area_km2"))
        if area is not None:
            date_totals[r["date"]] += area
    national_series = [{"date": k, "total_area_km2": v} for k, v in sorted(date_totals.items())]

    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "dam").mkdir(parents=True, exist_ok=True)
    (SITE / "assets").mkdir(parents=True, exist_ok=True)

    css_src = ROOT / "site" / "assets" / "style.css"
    if not css_src.exists():
        css_src.parent.mkdir(parents=True, exist_ok=True)
        css_src.write_text("/* placeholder */")

    common = {
        "generated_at": generated_at,
        "next_run_at": next_run_at,
        "github_repo": github_repo,
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
        "dams": [
            {
                "id": d["id"], "name": d["name"], "governorate": d["governorate"],
                "lat": d["lat"], "lon": d["lon"],
                "date": d["last_date"], "surface_area_km2": d["surface_area_km2"],
                "historical_avg_km2": d["historical_avg_km2"],
                "pct_of_avg": d["pct_of_avg"], "status": d["status"],
            }
            for d in dam_view
        ],
    }
    LATEST_JSON.parent.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(latest_json, indent=2))


if __name__ == "__main__":
    render_site(os.environ.get("GITHUB_REPOSITORY", "geminimir/tunisia-water-watch"))
