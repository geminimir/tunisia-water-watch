"""Render the bilingual (French + Arabic) static site + language-neutral oracle.

Layout produced under site/:

    site/index.html             Language picker (fr / ar)
    site/fr/…                   French tree
    site/ar/…                   Arabic tree (RTL)
    site/data/latest.json       Machine-readable summary (language-neutral)
    site/data/readings.csv
    site/data/gov_readings.csv
    site/oracle/                Phase 4 verifiable data endpoints

Each language tree mirrors the same structure:

    <lang>/index.html
    <lang>/agriculture.html
    <lang>/about.html
    <lang>/dam/<id>.html
    <lang>/governorate/<id>.html

The `data/` and `oracle/` trees stay at the site root because their content
is machine-consumable — bots, notebooks, and smart contracts don't have a
language. Every HTML page is fully translated: no English is exposed to a
human reader.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from anomalies import composite_severity, series_anomaly, severity_band
from i18n import (
    LANGUAGES, DEFAULT_LANG, t_for,
    translate_band, translate_focus, translate_region, translate_status,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
SITE = ROOT / "site"
DATA = ROOT / "data"

READINGS_CSV = DATA / "readings.csv"
GOV_CSV = DATA / "gov_readings.csv"
LATEST_JSON = DATA / "latest.json"
DAMS_JSON = ROOT / "config" / "dams.json"
GOV_JSON = ROOT / "config" / "governorates.json"

MIN_MONTH_SAMPLES = 3
MIN_ANNUAL_SAMPLES = 12
STALENESS_DAYS = 10

ORACLE_SCHEMA_VERSION = "1.0.0"


# ------------------------- csv/json helpers -------------------------

def _load_readings() -> list[dict[str, Any]]:
    if not READINGS_CSV.exists():
        return []
    with READINGS_CSV.open() as fh:
        return list(csv.DictReader(fh))


def _load_gov_readings() -> list[dict[str, Any]]:
    if not GOV_CSV.exists():
        return []
    with GOV_CSV.open() as fh:
        return list(csv.DictReader(fh))


def _load_dams() -> list[dict[str, Any]]:
    with DAMS_JSON.open() as fh:
        return json.load(fh)["dams"]


def _load_govs() -> list[dict[str, Any]]:
    if not GOV_JSON.exists():
        return []
    with GOV_JSON.open() as fh:
        return json.load(fh).get("governorates", [])


def _to_float(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ------------------------- status + trend -------------------------

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


# ------------------------- baselines (dams) -------------------------

def compute_baselines(
    per_dam: dict[str, list[dict[str, Any]]],
    dams_cfg: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
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
        if len(all_vals) >= MIN_ANNUAL_SAMPLES:
            annual = statistics.median(all_vals)
            source = "rolling"
        else:
            annual = hand_guessed.get(dam_id, 0.0)
            source = "config"
        out[dam_id] = {"annual": annual, "monthly": monthly, "source": source}
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
    dated = [r for r in rows if r.get("surface_area_km2") or r.get("mean_ndvi")]
    if not dated:
        return None, True
    try:
        latest = max(datetime.strptime(r["date"], "%Y-%m-%d") for r in dated)
    except ValueError:
        return None, True
    days = (now.replace(tzinfo=None) - latest).days
    return days, days > STALENESS_DAYS


def _drought_index(dam_view: list[dict[str, Any]]) -> float | None:
    num = 0.0
    den = 0.0
    for d in dam_view:
        if d["pct_of_avg"] is None:
            continue
        w = float(d.get("capacity_hm3") or 0.0) or 1.0
        num += d["pct_of_avg"] * w
        den += w
    return None if den == 0 else num / den


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, obj: Any) -> str:
    body = json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return _sha256(body.encode("utf-8"))


def _group_dam_readings(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["dam_id"]].append({
            "date": r["date"],
            "surface_area_km2": _to_float(r.get("surface_area_km2")),
            "surface_area_mndwi_km2": _to_float(r.get("surface_area_mndwi_km2")),
            "cloud_pct": _to_float(r.get("cloud_pct")),
            "confidence": _to_float(r.get("confidence")),
            "pct_of_avg": _to_float(r.get("pct_of_avg")),
            "scene_id": r.get("scene_id") or "",
        })
    for k in out:
        out[k].sort(key=lambda x: x["date"])
    return out


def _group_gov_readings(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["gov_id"]].append({
            "date": r["date"],
            "mean_ndvi": _to_float(r.get("mean_ndvi")),
            "mean_ndmi": _to_float(r.get("mean_ndmi")),
            "healthy_pct": _to_float(r.get("healthy_pct")),
            "cloud_pct": _to_float(r.get("cloud_pct")),
            "confidence": _to_float(r.get("confidence")),
            "scene_id": r.get("scene_id") or "",
        })
    for k in out:
        out[k].sort(key=lambda x: x["date"])
    return out


def _sparkline_svg(values: list[float | None], width: int = 100, height: int = 24,
                   stroke: str = "#0f4c81") -> str:
    """Return an inline SVG sparkline for the given series.

    None values create gaps. Empty or all-None input returns an empty string
    so the template can render a placeholder instead."""
    clean = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(clean) < 2:
        return ""
    ys = [v for _, v in clean]
    ymin = min(ys)
    ymax = max(ys)
    yrange = max(1e-6, ymax - ymin)
    n = len(values)
    pts: list[str] = []
    for i, v in clean:
        x = (i / max(1, n - 1)) * (width - 2) + 1
        y = height - 2 - ((v - ymin) / yrange) * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    last_x = (clean[-1][0] / max(1, n - 1)) * (width - 2) + 1
    last_y = height - 2 - ((clean[-1][1] - ymin) / yrange) * (height - 4)
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.4" stroke-linecap="round" '
        f'stroke-linejoin="round" points="{" ".join(pts)}"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="1.8" fill="{stroke}"/>'
        f'</svg>'
    )


def _monthly_medians(series: list[dict], key: str) -> dict[str, float]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in series:
        v = r.get(key)
        if v is None:
            continue
        buckets[r["date"][:7]].append(float(v))
    return {ym: statistics.median(vs) for ym, vs in sorted(buckets.items())}


def _span_days(rows: list[dict]) -> int:
    dates: list[datetime] = []
    for r in rows:
        d = r.get("date", "")
        if len(d) >= 10:
            try:
                dates.append(datetime.strptime(d[:10], "%Y-%m-%d"))
            except ValueError:
                pass
    if not dates:
        return 0
    return (max(dates) - min(dates)).days


# ------------------------- data preparation (language-neutral) -------------------------

def _prepare_data() -> dict[str, Any]:
    """Compute everything that doesn't depend on language."""
    dams_cfg = _load_dams()
    govs_cfg = _load_govs()
    rows = _load_readings()
    gov_rows = _load_gov_readings()

    per_dam = _group_dam_readings(rows)
    per_gov = _group_gov_readings(gov_rows)
    baselines = compute_baselines(per_dam, dams_cfg)
    hand_guessed = {d["id"]: float(d["historical_avg_km2"]) for d in dams_cfg}

    for dam_id, series in per_dam.items():
        for row in series:
            b, _ = effective_baseline(baselines[dam_id], row["date"], hand_guessed.get(dam_id, 0.0))
            row["pct_of_avg"] = _pct(row["surface_area_km2"], b)

    now = datetime.now(timezone.utc)
    stale_days, is_stale = _staleness(rows, now)

    # Per-region dam pct_of_avg (used by gov composite severity below)
    dam_pct_by_gov: dict[str, list[float]] = defaultdict(list)
    dam_view: list[dict[str, Any]] = []
    total_area = 0.0
    total_avg = 0.0
    dams_read = 0
    for d in dams_cfg:
        series = per_dam.get(d["id"], [])
        latest = series[-1] if series else None
        base_val, base_src = effective_baseline(
            baselines[d["id"]], latest["date"] if latest else None, hand_guessed[d["id"]],
        )
        area = latest["surface_area_km2"] if latest else None
        pct = latest["pct_of_avg"] if latest else None
        conf = latest["confidence"] if latest else None
        if area is not None:
            total_area += area
            total_avg += base_val
            dams_read += 1
        wa = series_anomaly(series, "surface_area_km2", latest["date"] if latest else "")
        sev, breakdown = composite_severity(
            dam_pct_of_avg=pct, ndvi_z=None, ndmi_z=None,
            dam_confidence=conf, veg_confidence=None,
        )
        dam_view.append({
            "id": d["id"], "name": d["name"], "name_ar": d.get("name_ar", d["name"]),
            "governorate": d["governorate"],
            "river": d.get("river", ""), "river_ar": d.get("river_ar", d.get("river", "")),
            "lat": d["lat"], "lon": d["lon"],
            "historical_avg_km2": d["historical_avg_km2"],
            "effective_baseline_km2": round(base_val, 2), "baseline_source": base_src,
            "capacity_hm3": d.get("capacity_hm3", 0), "ndwi_threshold": d["ndwi_threshold"],
            "last_date": latest["date"] if latest else None,
            "surface_area_km2": area,
            "surface_area_mndwi_km2": latest["surface_area_mndwi_km2"] if latest else None,
            "confidence": conf,
            "pct_of_avg": pct, "status": _status(pct),
            "z_score_water": wa.z_score,
            "sample_count_water": wa.sample_count,
            "severity_score": sev, "severity_band": severity_band(sev),
            "severity_breakdown": breakdown,
        })
        if pct is not None:
            dam_pct_by_gov[d["governorate"]].append(pct)

    def gov_water_pct(gov_name: str) -> float | None:
        vs = dam_pct_by_gov.get(gov_name, [])
        return sum(vs) / len(vs) if vs else None

    gov_view: list[dict[str, Any]] = []
    for g in govs_cfg:
        series = per_gov.get(g["id"], [])
        latest = series[-1] if series else None
        ndvi_anom = series_anomaly(series, "mean_ndvi", latest["date"] if latest else "")
        ndmi_anom = series_anomaly(series, "mean_ndmi", latest["date"] if latest else "")
        water_pct = gov_water_pct(g["name"])
        sev, breakdown = composite_severity(
            dam_pct_of_avg=water_pct,
            ndvi_z=ndvi_anom.z_score, ndmi_z=ndmi_anom.z_score,
            dam_confidence=None, veg_confidence=latest["confidence"] if latest else None,
        )
        gov_view.append({
            "id": g["id"], "name": g["name"], "name_ar": g.get("name_ar", g["name"]),
            "region": g["region"], "lat": g["lat"], "lon": g["lon"],
            "weight": g.get("weight", 1.0), "focus": g.get("focus", ""),
            "last_date": latest["date"] if latest else None,
            "mean_ndvi": latest["mean_ndvi"] if latest else None,
            "mean_ndmi": latest["mean_ndmi"] if latest else None,
            "healthy_pct": latest["healthy_pct"] if latest else None,
            "confidence": latest["confidence"] if latest else None,
            "z_ndvi": ndvi_anom.z_score, "z_ndmi": ndmi_anom.z_score,
            "sample_count_ndvi": ndvi_anom.sample_count,
            "regional_water_pct": water_pct,
            "severity_score": sev, "severity_band": severity_band(sev),
            "severity_breakdown": breakdown,
        })

    date_totals: dict[str, float] = defaultdict(float)
    for r in rows:
        area = _to_float(r.get("surface_area_km2"))
        if area is not None:
            date_totals[r["date"]] += area
    national_series = [
        {"date": k, "total_area_km2": round(v, 2)} for k, v in sorted(date_totals.items())
    ]
    drought_index = _drought_index(dam_view)

    def national_composite() -> float | None:
        num = 0.0
        den = 0.0
        for d in dam_view:
            if d["severity_score"] is None:
                continue
            w = float(d.get("capacity_hm3") or 1.0)
            num += d["severity_score"] * w
            den += w
        for g in gov_view:
            if g["severity_score"] is None:
                continue
            w = float(g.get("weight", 1.0)) * 50.0
            num += g["severity_score"] * w
            den += w
        return None if den == 0 else num / den

    composite_score = national_composite()

    return {
        "now": now,
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "next_run_at": (now + timedelta(days=5)).strftime("%Y-%m-%d"),
        "stale_days": stale_days, "is_stale": is_stale,
        "dams_cfg": dams_cfg, "govs_cfg": govs_cfg,
        "per_dam": per_dam, "per_gov": per_gov,
        "dam_view": dam_view, "gov_view": gov_view,
        "rows": rows, "gov_rows": gov_rows,
        "total_area": total_area, "total_avg": total_avg, "dams_read": dams_read,
        "national_series": national_series,
        "drought_index": drought_index,
        "composite_score": composite_score,
    }


# ------------------------- language-specific view fill -------------------------

def _view_for_lang(dam_view: list[dict], gov_view: list[dict], lang: str) -> tuple[list[dict], list[dict]]:
    """Return (dam_view_lang, gov_view_lang) with translated display fields."""
    # Build governorate French-name → Arabic-name lookup so dam pages can show
    # the right script for the containing governorate.
    gov_name_to_ar = {g["name"]: g.get("name_ar", g["name"]) for g in gov_view}
    out_dams = []
    for d in dam_view:
        e = dict(d)
        e["display_name"] = d["name_ar"] if lang == "ar" else d["name"]
        e["display_river"] = d["river_ar"] if lang == "ar" else d["river"]
        e["display_governorate"] = (
            gov_name_to_ar.get(d["governorate"], d["governorate"])
            if lang == "ar" else d["governorate"]
        )
        e["display_status"] = translate_status(d["status"], lang)
        e["display_band"] = translate_band(d["severity_band"] or "unknown", lang)
        out_dams.append(e)
    out_govs = []
    for g in gov_view:
        e = dict(g)
        e["display_name"] = g["name_ar"] if lang == "ar" else g["name"]
        e["display_region"] = translate_region(g["region"], lang)
        e["display_focus"] = translate_focus(g["focus"], lang) if g.get("focus") else ""
        e["display_band"] = translate_band(g["severity_band"] or "unknown", lang)
        out_govs.append(e)
    return out_dams, out_govs


_BAND_STROKE = {
    "abundant": "#2f7d5c",
    "normal": "#7a8a2a",
    "watch": "#c98a2b",
    "drought": "#b0361c",
    "severe": "#7a2417",
    "unknown": "#8a8f98",
}


def _attach_sparklines(dam_view: list[dict], gov_view: list[dict],
                      per_dam: dict, per_gov: dict) -> None:
    """Mutate views to add a `sparkline` SVG string per entity — last 24 months."""
    for d in dam_view:
        series = per_dam.get(d["id"], [])
        vals = [r.get("surface_area_km2") for r in series[-24:]]
        colour = _BAND_STROKE.get(d.get("severity_band") or "unknown", "#0f4c81")
        d["sparkline"] = _sparkline_svg(vals, stroke=colour)
    for g in gov_view:
        series = per_gov.get(g["id"], [])
        vals = [r.get("mean_ndvi") for r in series[-24:]]
        colour = _BAND_STROKE.get(g.get("severity_band") or "unknown", "#0f4c81")
        g["sparkline"] = _sparkline_svg(vals, stroke=colour)


# ------------------------- language tree renderer -------------------------

def _render_language_tree(
    env: Environment, lang: str, ctx: dict[str, Any], github_repo: str,
) -> None:
    lang_root = SITE / lang
    lang_root.mkdir(parents=True, exist_ok=True)
    (lang_root / "dam").mkdir(parents=True, exist_ok=True)
    (lang_root / "governorate").mkdir(parents=True, exist_ok=True)

    other = "ar" if lang == "fr" else "fr"
    direction = "rtl" if lang == "ar" else "ltr"
    t = t_for(lang)
    dam_view, gov_view = _view_for_lang(ctx["dam_view"], ctx["gov_view"], lang)
    _attach_sparklines(dam_view, gov_view, ctx["per_dam"], ctx["per_gov"])

    common = {
        "t": t,
        "lang": lang, "other_lang": other, "dir": direction,
        "generated_at": ctx["generated_at"],
        "next_run_at": ctx["next_run_at"],
        "github_repo": github_repo,
        "stale_days": ctx["stale_days"], "is_stale": ctx["is_stale"],
        "staleness_days": STALENESS_DAYS,
    }

    total_area = ctx["total_area"]
    total_avg = ctx["total_avg"]
    total_avg_ref = total_avg or 1.0
    composite_score = ctx["composite_score"]

    # ---- index (national dam overview) ----
    (lang_root / "index.html").write_text(env.get_template("index.html").render(
        page_title=t["nav_dams"],
        root="",           # links within same language
        site_root="../",   # up to site/
        alt_url=f"../{other}/index.html",
        dams=dam_view, dams_read=ctx["dams_read"],
        total_area_km2=total_area,
        pct_of_avg=(100.0 * total_area / total_avg_ref) if total_avg else 0.0,
        drought_index=ctx["drought_index"],
        composite_score=composite_score,
        composite_band=translate_band(severity_band(composite_score), lang),
        govs=gov_view,
        dams_json=json.dumps(dam_view, ensure_ascii=False, default=str),
        national_series_json=json.dumps(ctx["national_series"], default=str),
        **common,
    ))

    # ---- agriculture (governorate overview) ----
    (lang_root / "agriculture.html").write_text(env.get_template("agriculture.html").render(
        page_title=t["nav_agriculture"],
        root="", site_root="../",
        alt_url=f"../{other}/agriculture.html",
        govs=gov_view,
        govs_json=json.dumps(gov_view, ensure_ascii=False, default=str),
        composite_score=composite_score,
        composite_band=translate_band(severity_band(composite_score), lang),
        **common,
    ))

    # ---- about ----
    (lang_root / "about.html").write_text(env.get_template("about.html").render(
        page_title=t["nav_about"], root="", site_root="../",
        alt_url=f"../{other}/about.html",
        **common,
    ))

    # ---- per-dam ----
    dam_tpl = env.get_template("dam.html")
    for d in dam_view:
        series = ctx["per_dam"].get(d["id"], [])
        recent = list(reversed(series[-24:]))
        latest = series[-1] if series else {
            "date": None, "surface_area_km2": None, "pct_of_avg": None,
        }
        (lang_root / "dam" / f"{d['id']}.html").write_text(dam_tpl.render(
            page_title=d["display_name"],
            root="../", site_root="../../",
            alt_url=f"../../{other}/dam/{d['id']}.html",
            dam=d, latest=latest, recent=recent,
            series_json=json.dumps(series, default=str),
            trend_symbol=_trend_symbol(series[-6:] if len(series) >= 2 else series),
            **common,
        ))

    # ---- per-governorate ----
    gov_tpl = env.get_template("governorate.html")
    for g in gov_view:
        series = ctx["per_gov"].get(g["id"], [])
        recent = list(reversed(series[-24:]))
        latest = series[-1] if series else {}
        (lang_root / "governorate" / f"{g['id']}.html").write_text(gov_tpl.render(
            page_title=g["display_name"],
            root="../", site_root="../../",
            alt_url=f"../../{other}/governorate/{g['id']}.html",
            gov=g, latest=latest, recent=recent,
            series_json=json.dumps(series, default=str),
            **common,
        ))


# ------------------------- language picker landing -------------------------

_PICKER_HTML = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AquaWatch · أكواووتش</title>
<link rel="stylesheet" href="assets/style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>💧</text></svg>">
<style>
  body { display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: linear-gradient(135deg,#1e6fbf 0%,#0e3a68 100%); }
  .picker { background: white; border-radius: 12px; padding: 2rem 2.5rem; box-shadow: 0 12px 40px rgba(0,0,0,0.25); text-align: center; max-width: 420px; }
  .picker h1 { margin: 0 0 0.5rem; font-size: 1.4rem; color: #1c1f26; }
  .picker p { margin: 0 0 1.5rem; color: #6b7280; }
  .lang-btns { display: flex; gap: 1rem; justify-content: center; }
  .lang-btns a { display: inline-block; padding: 0.7rem 1.4rem; border-radius: 6px; text-decoration: none; font-weight: 600; }
  .lang-btns .fr { background: #1e6fbf; color: white; }
  .lang-btns .ar { background: #f4f6fa; color: #1c1f26; border: 1px solid #d5dae2; font-family: system-ui, -apple-system, "Segoe UI Arabic", "Noto Naskh Arabic", "Amiri", serif; }
</style>
</head>
<body>
<div class="picker">
  <h1>💧 AquaWatch</h1>
  <p>Choisir la langue &middot; اختر اللغة</p>
  <div class="lang-btns">
    <a class="fr" href="fr/index.html">Français</a>
    <a class="ar" href="ar/index.html" dir="rtl">العربية</a>
  </div>
</div>
<script>
  // Auto-redirect based on browser language on first visit only.
  try {
    var pref = localStorage.getItem('tww-lang');
    if (!pref) {
      var langs = (navigator.languages || [navigator.language || 'fr']).join(',').toLowerCase();
      pref = /(^|,)ar/.test(langs) ? 'ar' : 'fr';
    }
    // Respect explicit picker interactions by not overriding once chosen.
    if (pref === 'fr' || pref === 'ar') {
      location.replace(pref + '/index.html');
    }
  } catch (e) { /* keep the picker visible */ }
</script>
</body>
</html>
"""


def _write_picker() -> None:
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(_PICKER_HTML)


# ------------------------- latest.json + oracle (language-neutral) -------------------------

def _write_latest_and_data(ctx: dict[str, Any]) -> None:
    dam_view = ctx["dam_view"]
    gov_view = ctx["gov_view"]
    latest_json = {
        "generated_at": ctx["generated_at"],
        "stale_days": ctx["stale_days"],
        "national_drought_index": ctx["drought_index"],
        "national_composite_severity": ctx["composite_score"],
        "national_composite_band": severity_band(ctx["composite_score"]),
        "national_surface_area_km2": round(ctx["total_area"], 2),
        "national_baseline_km2": round(ctx["total_avg"], 2),
        "dams": [
            {
                "id": d["id"], "name": d["name"], "name_ar": d["name_ar"],
                "governorate": d["governorate"], "lat": d["lat"], "lon": d["lon"],
                "date": d["last_date"],
                "surface_area_km2": d["surface_area_km2"],
                "surface_area_mndwi_km2": d["surface_area_mndwi_km2"],
                "confidence": d["confidence"],
                "historical_avg_km2": d["historical_avg_km2"],
                "effective_baseline_km2": d["effective_baseline_km2"],
                "baseline_source": d["baseline_source"],
                "pct_of_avg": d["pct_of_avg"], "status": d["status"],
                "z_score_water": d["z_score_water"],
                "severity_score": d["severity_score"], "severity_band": d["severity_band"],
            } for d in dam_view
        ],
        "governorates": [
            {
                "id": g["id"], "name": g["name"], "name_ar": g["name_ar"],
                "region": g["region"], "lat": g["lat"], "lon": g["lon"],
                "date": g["last_date"],
                "mean_ndvi": g["mean_ndvi"], "mean_ndmi": g["mean_ndmi"],
                "healthy_pct": g["healthy_pct"], "confidence": g["confidence"],
                "z_ndvi": g["z_ndvi"], "z_ndmi": g["z_ndmi"],
                "regional_water_pct": g["regional_water_pct"],
                "severity_score": g["severity_score"], "severity_band": g["severity_band"],
            } for g in gov_view
        ],
    }
    LATEST_JSON.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(latest_json, indent=2, ensure_ascii=False, default=str)
    LATEST_JSON.write_text(body)
    site_data = SITE / "data"
    site_data.mkdir(parents=True, exist_ok=True)
    (site_data / "latest.json").write_text(body)
    if READINGS_CSV.exists():
        (site_data / "readings.csv").write_bytes(READINGS_CSV.read_bytes())
    if GOV_CSV.exists():
        (site_data / "gov_readings.csv").write_bytes(GOV_CSV.read_bytes())


def _write_oracle(ctx: dict[str, Any]) -> None:
    dam_view = ctx["dam_view"]
    gov_view = ctx["gov_view"]
    generated_at = ctx["generated_at"]
    per_dam = ctx["per_dam"]
    per_gov = ctx["per_gov"]

    oracle_dir = SITE / "oracle"
    oracle_dams = oracle_dir / "dams"
    oracle_govs = oracle_dir / "governorates"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    oracle_dams.mkdir(parents=True, exist_ok=True)
    oracle_govs.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict[str, str]] = {}

    for d in dam_view:
        series = per_dam.get(d["id"], [])
        payload = {
            "id": d["id"], "name": d["name"], "name_ar": d["name_ar"],
            "governorate": d["governorate"], "lat": d["lat"], "lon": d["lon"],
            "capacity_hm3": d["capacity_hm3"], "ndwi_threshold": d["ndwi_threshold"],
            "historical_avg_km2": d["historical_avg_km2"],
            "effective_baseline_km2": d["effective_baseline_km2"],
            "baseline_source": d["baseline_source"],
            "readings": series,
            "monthly_medians_km2": _monthly_medians(series, "surface_area_km2"),
            "generated_at": generated_at,
        }
        rel = f"oracle/dams/{d['id']}.json"
        manifest[rel] = {"sha256": _write_json(SITE / rel, payload), "type": "dam-history"}

    for g in gov_view:
        series = per_gov.get(g["id"], [])
        payload = {
            "id": g["id"], "name": g["name"], "name_ar": g["name_ar"],
            "region": g["region"], "lat": g["lat"], "lon": g["lon"],
            "weight": g["weight"], "readings": series,
            "monthly_medians_ndvi": _monthly_medians(series, "mean_ndvi"),
            "monthly_medians_ndmi": _monthly_medians(series, "mean_ndmi"),
            "generated_at": generated_at,
        }
        rel = f"oracle/governorates/{g['id']}.json"
        manifest[rel] = {"sha256": _write_json(SITE / rel, payload), "type": "governorate-history"}

    anomalies_payload = {
        "generated_at": generated_at,
        "dams": [
            {
                "id": d["id"], "name": d["name"], "name_ar": d["name_ar"],
                "severity_score": d["severity_score"], "band": d["severity_band"],
                "pct_of_avg": d["pct_of_avg"], "z_score_water": d["z_score_water"],
                "date": d["last_date"],
            }
            for d in dam_view if d["severity_band"] in ("drought", "severe", "watch")
        ],
        "governorates": [
            {
                "id": g["id"], "name": g["name"], "name_ar": g["name_ar"],
                "severity_score": g["severity_score"], "band": g["severity_band"],
                "z_ndvi": g["z_ndvi"], "z_ndmi": g["z_ndmi"], "date": g["last_date"],
            }
            for g in gov_view if g["severity_band"] in ("drought", "severe", "watch")
        ],
    }
    manifest["oracle/anomalies.json"] = {
        "sha256": _write_json(oracle_dir / "anomalies.json", anomalies_payload),
        "type": "anomalies-snapshot",
    }

    monthly_national: dict[str, float] = defaultdict(float)
    monthly_counts: dict[str, int] = defaultdict(int)
    for r in ctx["rows"]:
        area = _to_float(r.get("surface_area_km2"))
        if area is None:
            continue
        ym = r["date"][:7]
        monthly_national[ym] += area
        monthly_counts[ym] += 1
    monthly_payload = {
        "generated_at": generated_at,
        "national": {
            ym: {"total_area_km2": round(v, 2), "dam_readings": monthly_counts[ym]}
            for ym, v in sorted(monthly_national.items())
        },
    }
    manifest["oracle/monthly.json"] = {
        "sha256": _write_json(oracle_dir / "monthly.json", monthly_payload),
        "type": "monthly-aggregate",
    }
    manifest["oracle/latest.json"] = {
        "sha256": _write_json(oracle_dir / "latest.json", json.loads(LATEST_JSON.read_text())),
        "type": "latest-summary",
    }

    oracle_index = {
        "schema_version": ORACLE_SCHEMA_VERSION,
        "description": "Tunisia Water Watch verifiable data oracle. Every listed endpoint's SHA-256 is in manifest.json.",
        "generated_at": generated_at,
        "endpoints": {
            "latest": "latest.json", "anomalies": "anomalies.json",
            "monthly": "monthly.json", "manifest": "manifest.json",
            "dam_history_template": "dams/<dam_id>.json",
            "governorate_history_template": "governorates/<gov_id>.json",
        },
        "counts": {
            "dams": len(dam_view), "governorates": len(gov_view),
            "dam_readings": sum(len(v) for v in per_dam.values()),
            "governorate_readings": sum(len(v) for v in per_gov.values()),
        },
        "coverage": {
            "dam_history_span_days": _span_days(ctx["rows"]),
            "governorate_history_span_days": _span_days(ctx["gov_rows"]),
        },
    }
    idx_hash = _write_json(oracle_dir / "index.json", oracle_index)
    manifest["oracle/index.json"] = {"sha256": idx_hash, "type": "index"}

    manifest_payload = {
        "schema_version": ORACLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "algorithm": "sha256",
        "files": manifest,
    }
    (oracle_dir / "manifest.json").write_text(json.dumps(manifest_payload, indent=2))


# ------------------------- entrypoint -------------------------

def render_site(github_repo: str = "geminimir/water-watch") -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    SITE.mkdir(parents=True, exist_ok=True)
    ctx = _prepare_data()

    _write_latest_and_data(ctx)
    _write_oracle(ctx)

    for lang in LANGUAGES:
        _render_language_tree(env, lang, ctx, github_repo)

    _write_picker()


if __name__ == "__main__":
    render_site(os.environ.get("GITHUB_REPOSITORY", "geminimir/water-watch"))
