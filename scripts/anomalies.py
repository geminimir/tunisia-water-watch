"""Phase 3: anomaly detection + composite drought severity.

Two flavours of "how unusual is this reading":

    z-score:  (current - mean) / stddev   → in units of standard deviations.
              Negative = drier than typical. Requires ≥6 same-month samples;
              otherwise returns None.

    percent-rank: how the current value ranks against the same-month history,
                  as a 0..100 percentile.

Composite drought severity per governorate (0-100, higher = worse):
    - dam anomaly (regional weighted reservoir pct_of_avg, inverted)
    - vegetation anomaly (NDVI z-score, inverted)
    - moisture anomaly (NDMI z-score, inverted)
Weights each by its confidence and merges into a single score. Missing signals
degrade to available ones; if no signal at all is available, drought severity
is None (gray on the dashboard).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass
class Anomaly:
    z_score: float | None
    percent_rank: float | None  # 0..100
    baseline_mean: float | None
    baseline_std: float | None
    sample_count: int


def _month_from_date(d: str) -> int | None:
    if not d or len(d) < 7:
        return None
    try:
        return int(d[5:7])
    except ValueError:
        return None


def series_anomaly(
    series: list[dict[str, Any]],
    value_key: str,
    reference_date: str,
    min_samples: int = 4,
) -> Anomaly:
    """Z-score and percent-rank of the latest same-month reading vs history."""
    ref_month = _month_from_date(reference_date)
    if ref_month is None:
        return Anomaly(None, None, None, None, 0)
    same_month: list[float] = []
    for r in series:
        if _month_from_date(r.get("date", "")) != ref_month:
            continue
        v = r.get(value_key)
        if v is None or v == "":
            continue
        try:
            same_month.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(same_month) < min_samples:
        return Anomaly(None, None, None, None, len(same_month))
    # exclude the reference reading itself from the baseline (last with matching date)
    baseline = same_month[:-1] if len(same_month) >= min_samples + 1 else same_month
    mean = statistics.mean(baseline)
    try:
        std = statistics.stdev(baseline)
    except statistics.StatisticsError:
        std = 0.0
    current = same_month[-1]
    z = ((current - mean) / std) if std > 0 else 0.0
    below = sum(1 for v in baseline if v <= current)
    rank = 100.0 * below / len(baseline)
    return Anomaly(
        z_score=z, percent_rank=rank,
        baseline_mean=mean, baseline_std=std,
        sample_count=len(same_month),
    )


# ------------------------- composite severity -------------------------

def _severity_from_z(z: float | None) -> float | None:
    """Map a z-score to a 0..100 severity. z=0 → 50, z=-2 → 90, z=-4 → 100."""
    if z is None:
        return None
    # linear clip; -4σ or below is 100, +4σ or above is 0
    return max(0.0, min(100.0, 50.0 - 12.5 * z))


def _severity_from_pct(pct: float | None) -> float | None:
    if pct is None:
        return None
    # pct_of_avg=100 → 50 severity; pct=0 → 100; pct=200 → 0
    return max(0.0, min(100.0, 100.0 - 0.5 * pct))


def composite_severity(
    dam_pct_of_avg: float | None,
    ndvi_z: float | None,
    ndmi_z: float | None,
    dam_confidence: float | None = None,
    veg_confidence: float | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Confidence-weighted severity (0-100). Returns (score, breakdown).

    Breakdown is a JSON-friendly dict useful for the dashboard tooltip.
    """
    components: list[tuple[str, float, float]] = []  # (name, severity, weight)
    dam_sev = _severity_from_pct(dam_pct_of_avg)
    if dam_sev is not None:
        components.append(("water", dam_sev, max(0.3, dam_confidence or 0.5)))
    ndvi_sev = _severity_from_z(ndvi_z)
    if ndvi_sev is not None:
        components.append(("vegetation", ndvi_sev, max(0.3, veg_confidence or 0.5)))
    ndmi_sev = _severity_from_z(ndmi_z)
    if ndmi_sev is not None:
        components.append(("moisture", ndmi_sev, 0.6 * max(0.3, veg_confidence or 0.5)))
    if not components:
        return None, {"components": []}
    total_w = sum(w for _, _, w in components)
    score = sum(s * w for _, s, w in components) / total_w
    return score, {
        "score": round(score, 2),
        "components": [
            {"name": n, "severity": round(s, 2), "weight": round(w, 3)}
            for n, s, w in components
        ],
    }


def severity_band(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score < 30:
        return "abundant"
    if score < 50:
        return "normal"
    if score < 65:
        return "watch"
    if score < 80:
        return "drought"
    return "severe"


__all__ = [
    "Anomaly", "series_anomaly", "composite_severity", "severity_band",
    "_severity_from_z", "_severity_from_pct",
]
