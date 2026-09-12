"""Render the static site + oracle endpoints from accumulated CSVs.

Outputs:
    site/index.html             National overview (dams + governorates + composite)
    site/dam/<id>.html          Per-dam page
    site/governorate/<id>.html  Per-governorate page (Phase 2)
    site/agriculture.html       Governorate index (Phase 2)
    site/about.html
    site/data/latest.json       Compact machine-readable summary
    site/data/readings.csv      Full dam history
    site/data/gov_readings.csv  Full governorate history
    site/oracle/                Phase 4: verifiable data endpoints
      index.json                Schema + entrypoint URLs
      manifest.json             SHA-256 of every published file
      anomalies.json            Current drought/severity flags
      monthly.json              Per-dam per-governorate monthly medians
      dams/<id>.json            Full history + baseline for one dam
      governorates/<id>.json    Full history + anomalies for one governorate
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

from anomalies import (
    Anomaly, composite_severity, series_anomaly, severity_band,
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


# ------------------------- misc -------------------------

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
    body = json.dumps(obj, indent=2, sort_keys=False, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return _sha256(body.encode("utf-8"))


# ------------------------- main render -------------------------

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


def _monthly_medians(series: list[dict], key: str) -> dict[str, float]:
    """Return {'YYYY-MM': median} for the given key."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in series:
        v = r.get(key)
        if v is None:
            continue
        buckets[r["date"][:7]].append(float(v))
    return {ym: statistics.median(vs) for ym, vs in sorted(buckets.items())}


def render_site(github_repo: str = "geminimir/water-watch") -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    dams_cfg = _load_dams()
    govs_cfg = _load_govs()
    rows = _load_readings()
    gov_rows = _load_gov_readings()

    per_dam = _group_dam_readings(rows)
    per_gov = _group_gov_readings(gov_rows)

    baselines = compute_baselines(per_dam, dams_cfg)
    hand_guessed = {d["id"]: float(d["historical_avg_km2"]) for d in dams_cfg}

    # Overlay effective pct_of_avg on each dam series row (does not touch CSV).
    for dam_id, series in per_dam.items():
        for row in series:
            b, _ = effective_baseline(baselines[dam_id], row["date"], hand_guessed.get(dam_id, 0.0))
            row["pct_of_avg"] = _pct(row["surface_area_km2"], b)

    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    next_run_at = (now + timedelta(days=5)).strftime("%Y-%m-%d")
    stale_days_dams, is_stale_dams = _staleness(rows, now)
    stale_days_govs, is_stale_govs = _staleness(gov_rows, now) if gov_rows else (None, False)
    stale_days = stale_days_dams
    is_stale = is_stale_dams

    # Per-region regional dam pct_of_avg — used by composite severity below.
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
        # water anomaly z-score against same-month history
        wa = series_anomaly(series, "surface_area_km2", latest["date"] if latest else "")
        sev, breakdown = composite_severity(
            dam_pct_of_avg=pct, ndvi_z=None, ndmi_z=None,
            dam_confidence=conf, veg_confidence=None,
        )
        dam_view.append({
            "id": d["id"], "name": d["name"], "governorate": d["governorate"],
            "river": d["river"], "lat": d["lat"], "lon": d["lon"],
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
        if not vs:
            return None
        return sum(vs) / len(vs)

    # Governorate view + composite severity per governorate.
    gov_view: list[dict[str, Any]] = []
    for g in govs_cfg:
        series = per_gov.get(g["id"], [])
        latest = series[-1] if series else None
        ndvi = latest["mean_ndvi"] if latest else None
        ndmi = latest["mean_ndmi"] if latest else None
        healthy = latest["healthy_pct"] if latest else None
        conf = latest["confidence"] if latest else None
        ndvi_anom = series_anomaly(series, "mean_ndvi", latest["date"] if latest else "")
        ndmi_anom = series_anomaly(series, "mean_ndmi", latest["date"] if latest else "")
        water_pct = gov_water_pct(g["name"])
        sev, breakdown = composite_severity(
            dam_pct_of_avg=water_pct,
            ndvi_z=ndvi_anom.z_score,
            ndmi_z=ndmi_anom.z_score,
            dam_confidence=None, veg_confidence=conf,
        )
        gov_view.append({
            "id": g["id"], "name": g["name"], "region": g["region"],
            "lat": g["lat"], "lon": g["lon"], "weight": g.get("weight", 1.0),
            "focus": g.get("focus", ""),
            "last_date": latest["date"] if latest else None,
            "mean_ndvi": ndvi, "mean_ndmi": ndmi,
            "healthy_pct": healthy, "confidence": conf,
            "z_ndvi": ndvi_anom.z_score, "z_ndmi": ndmi_anom.z_score,
            "sample_count_ndvi": ndvi_anom.sample_count,
            "regional_water_pct": water_pct,
            "severity_score": sev, "severity_band": severity_band(sev),
            "severity_breakdown": breakdown,
        })

    # National daily total series
    date_totals: dict[str, float] = defaultdict(float)
    for r in rows:
        area = _to_float(r.get("surface_area_km2"))
        if area is not None:
            date_totals[r["date"]] += area
    national_series = [
        {"date": k, "total_area_km2": round(v, 2)} for k, v in sorted(date_totals.items())
    ]
    drought_index = _drought_index(dam_view)

    # National composite: capacity-weighted average of dam severities
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
            w = float(g.get("weight", 1.0)) * 50.0  # rough scale so 1 gov ≈ 50 hm³ of dam weight
            num += g["severity_score"] * w
            den += w
        return None if den == 0 else num / den

    composite_score = national_composite()

    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "dam").mkdir(parents=True, exist_ok=True)
    (SITE / "governorate").mkdir(parents=True, exist_ok=True)
    (SITE / "assets").mkdir(parents=True, exist_ok=True)

    common = {
        "generated_at": generated_at, "next_run_at": next_run_at,
        "github_repo": github_repo,
        "stale_days": stale_days, "is_stale": is_stale,
        "staleness_days": STALENESS_DAYS,
    }

    total_avg_ref = total_avg or 1.0
    (SITE / "index.html").write_text(env.get_template("index.html").render(
        page_title="Dams", root="",
        dams=dam_view, dams_read=dams_read,
        total_area_km2=total_area,
        pct_of_avg=(100.0 * total_area / total_avg_ref) if total_avg else 0.0,
        drought_index=drought_index,
        composite_score=composite_score,
        composite_band=severity_band(composite_score),
        govs=gov_view,
        dams_json=json.dumps(dam_view, default=str),
        national_series_json=json.dumps(national_series),
        **common,
    ))

    (SITE / "agriculture.html").write_text(env.get_template("agriculture.html").render(
        page_title="Agriculture", root="", govs=gov_view,
        govs_json=json.dumps(gov_view, default=str),
        composite_score=composite_score,
        composite_band=severity_band(composite_score),
        **common,
    ))
    (SITE / "about.html").write_text(env.get_template("about.html").render(
        page_title="About", root="", **common,
    ))

    dam_tpl = env.get_template("dam.html")
    for d in dam_view:
        series = per_dam.get(d["id"], [])
        recent = list(reversed(series[-24:]))
        latest = series[-1] if series else {
            "date": None, "surface_area_km2": None, "pct_of_avg": None,
        }
        (SITE / "dam" / f"{d['id']}.html").write_text(dam_tpl.render(
            page_title=d["name"], root="../",
            dam=d, latest=latest, recent=recent,
            series_json=json.dumps(series, default=str),
            trend_symbol=_trend_symbol(series[-6:] if len(series) >= 2 else series),
            **common,
        ))

    gov_tpl = env.get_template("governorate.html")
    for g in gov_view:
        series = per_gov.get(g["id"], [])
        recent = list(reversed(series[-24:]))
        latest = series[-1] if series else {}
        (SITE / "governorate" / f"{g['id']}.html").write_text(gov_tpl.render(
            page_title=g["name"], root="../",
            gov=g, latest=latest, recent=recent,
            series_json=json.dumps(series, default=str),
            **common,
        ))

    # ------- latest.json (dashboard-facing summary) -------
    latest_json = {
        "generated_at": generated_at,
        "stale_days": stale_days,
        "national_drought_index": drought_index,
        "national_composite_severity": composite_score,
        "national_composite_band": severity_band(composite_score),
        "national_surface_area_km2": round(total_area, 2),
        "national_baseline_km2": round(total_avg, 2),
        "dams": [
            {
                "id": d["id"], "name": d["name"], "governorate": d["governorate"],
                "lat": d["lat"], "lon": d["lon"], "date": d["last_date"],
                "surface_area_km2": d["surface_area_km2"],
                "surface_area_mndwi_km2": d["surface_area_mndwi_km2"],
                "confidence": d["confidence"],
                "historical_avg_km2": d["historical_avg_km2"],
                "effective_baseline_km2": d["effective_baseline_km2"],
                "baseline_source": d["baseline_source"],
                "pct_of_avg": d["pct_of_avg"], "status": d["status"],
                "z_score_water": d["z_score_water"],
                "severity_score": d["severity_score"],
                "severity_band": d["severity_band"],
            }
            for d in dam_view
        ],
        "governorates": [
            {
                "id": g["id"], "name": g["name"], "region": g["region"],
                "lat": g["lat"], "lon": g["lon"], "date": g["last_date"],
                "mean_ndvi": g["mean_ndvi"], "mean_ndmi": g["mean_ndmi"],
                "healthy_pct": g["healthy_pct"], "confidence": g["confidence"],
                "z_ndvi": g["z_ndvi"], "z_ndmi": g["z_ndmi"],
                "regional_water_pct": g["regional_water_pct"],
                "severity_score": g["severity_score"],
                "severity_band": g["severity_band"],
            }
            for g in gov_view
        ],
    }
    LATEST_JSON.parent.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(latest_json, indent=2, default=str))

    # Mirror data files under site/ for Pages.
    site_data = SITE / "data"
    site_data.mkdir(parents=True, exist_ok=True)
    (site_data / "latest.json").write_text(json.dumps(latest_json, indent=2, default=str))
    if READINGS_CSV.exists():
        (site_data / "readings.csv").write_bytes(READINGS_CSV.read_bytes())
    if GOV_CSV.exists():
        (site_data / "gov_readings.csv").write_bytes(GOV_CSV.read_bytes())

    # -------------------- Phase 4: oracle endpoints + manifest --------------------
    oracle_dir = SITE / "oracle"
    oracle_dams = oracle_dir / "dams"
    oracle_govs = oracle_dir / "governorates"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    oracle_dams.mkdir(parents=True, exist_ok=True)
    oracle_govs.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict[str, str]] = {}

    # Per-dam history endpoint
    for d in dam_view:
        series = per_dam.get(d["id"], [])
        payload = {
            "id": d["id"], "name": d["name"], "governorate": d["governorate"],
            "lat": d["lat"], "lon": d["lon"],
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

    # Per-governorate history endpoint
    for g in gov_view:
        series = per_gov.get(g["id"], [])
        payload = {
            "id": g["id"], "name": g["name"], "region": g["region"],
            "lat": g["lat"], "lon": g["lon"], "weight": g["weight"],
            "readings": series,
            "monthly_medians_ndvi": _monthly_medians(series, "mean_ndvi"),
            "monthly_medians_ndmi": _monthly_medians(series, "mean_ndmi"),
            "generated_at": generated_at,
        }
        rel = f"oracle/governorates/{g['id']}.json"
        manifest[rel] = {"sha256": _write_json(SITE / rel, payload), "type": "governorate-history"}

    # Anomalies snapshot — any entity in "drought" or "severe" band right now
    anomalies_payload = {
        "generated_at": generated_at,
        "dams": [
            {
                "id": d["id"], "name": d["name"],
                "severity_score": d["severity_score"], "band": d["severity_band"],
                "pct_of_avg": d["pct_of_avg"], "z_score_water": d["z_score_water"],
                "date": d["last_date"],
            }
            for d in dam_view
            if d["severity_band"] in ("drought", "severe", "watch")
        ],
        "governorates": [
            {
                "id": g["id"], "name": g["name"],
                "severity_score": g["severity_score"], "band": g["severity_band"],
                "z_ndvi": g["z_ndvi"], "z_ndmi": g["z_ndmi"],
                "date": g["last_date"],
            }
            for g in gov_view
            if g["severity_band"] in ("drought", "severe", "watch")
        ],
    }
    manifest["oracle/anomalies.json"] = {
        "sha256": _write_json(oracle_dir / "anomalies.json", anomalies_payload),
        "type": "anomalies-snapshot",
    }

    # Monthly aggregates (national)
    monthly_national: dict[str, float] = defaultdict(float)
    monthly_counts: dict[str, int] = defaultdict(int)
    for r in rows:
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

    # latest.json also under oracle/ so it has a hash entry
    manifest["oracle/latest.json"] = {
        "sha256": _write_json(oracle_dir / "latest.json", latest_json),
        "type": "latest-summary",
    }

    # Oracle index — schema + entrypoints
    oracle_index = {
        "schema_version": ORACLE_SCHEMA_VERSION,
        "description": "Tunisia Water Watch verifiable data oracle. Every listed endpoint's SHA-256 is in manifest.json. Downstream consumers (parametric insurance, development banks, researchers) can hash the file they receive and compare to the manifest to detect tampering.",
        "generated_at": generated_at,
        "endpoints": {
            "latest": "latest.json",
            "anomalies": "anomalies.json",
            "monthly": "monthly.json",
            "manifest": "manifest.json",
            "dam_history_template": "dams/<dam_id>.json",
            "governorate_history_template": "governorates/<gov_id>.json",
        },
        "counts": {
            "dams": len(dam_view),
            "governorates": len(gov_view),
            "dam_readings": sum(len(v) for v in per_dam.values()),
            "governorate_readings": sum(len(v) for v in per_gov.values()),
        },
        "coverage": {
            "dam_history_span_days": _span_days(rows),
            "governorate_history_span_days": _span_days(gov_rows),
        },
    }
    # Write index first, hash it, then include self-hash in manifest.
    idx_hash = _write_json(oracle_dir / "index.json", oracle_index)
    manifest["oracle/index.json"] = {"sha256": idx_hash, "type": "index"}

    manifest_payload = {
        "schema_version": ORACLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "algorithm": "sha256",
        "files": manifest,
    }
    (oracle_dir / "manifest.json").write_text(json.dumps(manifest_payload, indent=2))


def _span_days(rows: list[dict]) -> int:
    dates = []
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


if __name__ == "__main__":
    render_site(os.environ.get("GITHUB_REPOSITORY", "geminimir/water-watch"))
