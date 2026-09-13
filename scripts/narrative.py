"""Template-driven natural-language descriptions of the current state.

No LLM, no credentials, deterministic given inputs. Every phrase is a
Python f-string picked from a small vocabulary keyed by language.

Produces three things:

    build_briefing(ctx, lang) → 3-sentence "State of Tunisia's water"
                                paragraph, ready to quote verbatim.

    build_notable(ctx, lang) → structured "notable this week" data:
                               top 3 dams below trend, top 3 above,
                               plus governorate NDVI extremes.

    build_dam_line(dam, lang) → one-line anomaly description per dam page.

Language is fully driven by the passed `lang` argument; strings live here
alongside the logic so the pattern of the paragraph is obvious.
"""

from __future__ import annotations

from typing import Any


_MONTH_FR = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "août",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}
_MONTH_AR = {
    1: "جانفي", 2: "فيفري", 3: "مارس", 4: "أفريل",
    5: "ماي", 6: "جوان", 7: "جويلية", 8: "أوت",
    9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}


def _month_name(month: int, lang: str) -> str:
    return (_MONTH_AR if lang == "ar" else _MONTH_FR).get(month, str(month))


def _fmt_pct(v: float) -> str:
    """Return e.g. '12%' with no decimal for readability."""
    return f"{abs(v):.0f} %"


def _dam_display_name(d: dict, lang: str) -> str:
    return d.get("name_ar" if lang == "ar" else "name", d.get("id", ""))


def _gov_display_name(g: dict, lang: str) -> str:
    return g.get("name_ar" if lang == "ar" else "name", g.get("id", ""))


# ---------------------------------------------------------------------------
# state-of-Tunisia-water paragraph
# ---------------------------------------------------------------------------

def build_briefing(ctx: dict, lang: str) -> str:
    """Return a 2-3 sentence paragraph summarising the current national state."""
    total = ctx["total_area"]
    baseline = ctx["total_avg"]
    dam_view = ctx["dam_view"]
    gov_view = ctx["gov_view"]
    now = ctx["now"]
    month = _month_name(now.month, lang)

    if not baseline or total == 0:
        return {
            "fr": "Pas encore de données consolidées pour cette semaine.",
            "ar": "لا تتوفر بيانات موحّدة لهذا الأسبوع.",
        }[lang]

    pct_vs_baseline = 100.0 * total / baseline
    delta_pct = pct_vs_baseline - 100.0

    if abs(delta_pct) < 3:
        state_fr = (
            f"Cette semaine, la surface totale cartographiée des réservoirs tunisiens "
            f"atteint {total:.1f} km², proche de la médiane de {month} des dernières années."
        )
        state_ar = (
            f"هذا الأسبوع، بلغت المساحة الإجمالية للخزانات التونسية المرصودة "
            f"{total:.1f} كم²، وهي قريبة من المتوسط المرجعي لشهر {month}."
        )
    elif delta_pct < 0:
        state_fr = (
            f"Cette semaine, la surface totale cartographiée des réservoirs tunisiens "
            f"s'établit à {total:.1f} km² — soit {_fmt_pct(delta_pct)} en dessous "
            f"de la médiane de {month} sur l'historique disponible."
        )
        state_ar = (
            f"هذا الأسبوع، بلغت المساحة الإجمالية للخزانات التونسية المرصودة "
            f"{total:.1f} كم²، أي بانخفاض {_fmt_pct(delta_pct)} عن الوسيط "
            f"المرجعي لشهر {month}."
        )
    else:
        state_fr = (
            f"Cette semaine, la surface totale cartographiée des réservoirs tunisiens "
            f"s'établit à {total:.1f} km² — soit {_fmt_pct(delta_pct)} au-dessus "
            f"de la médiane de {month} sur l'historique disponible."
        )
        state_ar = (
            f"هذا الأسبوع، بلغت المساحة الإجمالية للخزانات التونسية المرصودة "
            f"{total:.1f} كم²، أي بارتفاع {_fmt_pct(delta_pct)} فوق الوسيط "
            f"المرجعي لشهر {month}."
        )

    # Sentence 2: top decliners by dam z-score
    dam_ranked = sorted(
        [d for d in dam_view if d.get("z_score_water") is not None and d["z_score_water"] < -0.5],
        key=lambda x: x["z_score_water"],
    )[:2]
    detail_fr = ""
    detail_ar = ""
    if dam_ranked:
        parts_fr = []
        parts_ar = []
        for d in dam_ranked:
            n_fr = _dam_display_name(d, "fr")
            n_ar = _dam_display_name(d, "ar")
            if d["pct_of_avg"] is not None:
                pct_below = 100 - d["pct_of_avg"]
                parts_fr.append(f"{n_fr} ({_fmt_pct(pct_below)} sous sa moyenne)")
                parts_ar.append(f"{n_ar} (أقل بـ{_fmt_pct(pct_below)} من متوسطه)")
            else:
                parts_fr.append(n_fr)
                parts_ar.append(n_ar)
        detail_fr = "Les baisses les plus marquées concernent " + " et ".join(parts_fr) + "."
        detail_ar = "أبرز التراجعات تشمل " + " و".join(parts_ar) + "."

    # Sentence 3: agricultural context from governorate NDVI
    gov_stressed = [g for g in gov_view if g.get("severity_band") in ("drought", "severe")]
    veg_fr = ""
    veg_ar = ""
    if len(gov_stressed) >= 4:
        veg_fr = (
            f"Côté agricole, {len(gov_stressed)} gouvernorats présentent un stress "
            f"végétal marqué (NDVI et humidité de la canopée sous la normale)."
        )
        veg_ar = (
            f"في المجال الزراعي، تشهد {len(gov_stressed)} ولاية إجهادًا نباتيًا "
            f"واضحًا (مؤشرا NDVI ورطوبة النبات تحت المعدل)."
        )
    elif gov_stressed:
        names = ", ".join(_gov_display_name(g, lang) for g in gov_stressed[:3])
        veg_fr = f"Signaux de stress agricole plus localisés à {names}."
        veg_ar = f"إشارات إجهاد زراعي مركّزة في {names}."
    else:
        veg_fr = "Les gouvernorats agricoles restent dans les normes saisonnières."
        veg_ar = "تبقى الولايات الزراعية ضمن المعدلات الموسمية."

    if lang == "ar":
        return " ".join(filter(None, [state_ar, detail_ar, veg_ar]))
    return " ".join(filter(None, [state_fr, detail_fr, veg_fr]))


# ---------------------------------------------------------------------------
# notable this week
# ---------------------------------------------------------------------------

def build_notable(ctx: dict, lang: str) -> dict[str, list[dict]]:
    dams = [d for d in ctx["dam_view"] if d.get("z_score_water") is not None]
    dams_below = sorted(dams, key=lambda x: x["z_score_water"])[:3]
    dams_above = sorted(dams, key=lambda x: -x["z_score_water"])[:3]

    def _decorate_dam(d, direction):
        return {
            "id": d["id"],
            "name": _dam_display_name(d, lang),
            "governorate": d.get("governorate", ""),
            "pct_of_avg": d.get("pct_of_avg"),
            "z_score": d.get("z_score_water"),
            "severity_band": d.get("severity_band"),
            "direction": direction,
        }

    govs = [g for g in ctx["gov_view"] if g.get("z_ndvi") is not None]
    govs_below = sorted(govs, key=lambda x: x["z_ndvi"])[:3]
    govs_above = sorted(govs, key=lambda x: -x["z_ndvi"])[:3]

    def _decorate_gov(g, direction):
        return {
            "id": g["id"],
            "name": _gov_display_name(g, lang),
            "region": g.get("region", ""),
            "mean_ndvi": g.get("mean_ndvi"),
            "z_ndvi": g.get("z_ndvi"),
            "z_ndmi": g.get("z_ndmi"),
            "severity_band": g.get("severity_band"),
            "direction": direction,
        }

    return {
        "dams_below": [_decorate_dam(d, "below") for d in dams_below],
        "dams_above": [_decorate_dam(d, "above") for d in dams_above],
        "govs_below": [_decorate_gov(g, "below") for g in govs_below],
        "govs_above": [_decorate_gov(g, "above") for g in govs_above],
    }


# ---------------------------------------------------------------------------
# per-dam anomaly one-liner
# ---------------------------------------------------------------------------

def build_dam_line(dam: dict, lang: str, month: int, history_years: int) -> str:
    """One-line anomaly summary for the dam page."""
    area = dam.get("surface_area_km2")
    pct = dam.get("pct_of_avg")
    baseline = dam.get("effective_baseline_km2")
    baseline_source = dam.get("baseline_source", "")
    month_name = _month_name(month, lang)
    if area is None:
        return {
            "fr": "Aucune lecture récente exploitable pour ce barrage.",
            "ar": "لا توجد قراءة حديثة قابلة للاستخدام لهذا السد.",
        }[lang]
    if pct is None or baseline is None:
        return {
            "fr": f"Surface actuelle : {area:.2f} km².",
            "ar": f"المساحة الحالية: {area:.2f} كم².",
        }[lang]
    delta = pct - 100
    ref_fr = (
        f"la médiane glissante de {month_name}"
        if baseline_source.startswith("rolling")
        else "la moyenne de référence"
    )
    ref_ar = (
        f"الوسيط المتحرك لشهر {month_name}"
        if baseline_source.startswith("rolling")
        else "المتوسط المرجعي"
    )
    if abs(delta) < 3:
        if lang == "ar":
            return f"يبلغ سطح المياه اليوم {area:.2f} كم²، أي مقارب لـ{ref_ar} ({baseline} كم²)."
        return f"Surface aujourd'hui : {area:.2f} km², proche de {ref_fr} ({baseline} km²)."
    if delta < 0:
        if lang == "ar":
            return (
                f"يبلغ سطح المياه اليوم {area:.2f} كم²، أي بانخفاض "
                f"{_fmt_pct(delta)} عن {ref_ar} ({baseline} كم²) على مدى "
                f"{history_years} سنوات."
            )
        return (
            f"Surface aujourd'hui : {area:.2f} km², soit {_fmt_pct(delta)} en dessous "
            f"de {ref_fr} ({baseline} km²) sur {history_years} années d'historique."
        )
    if lang == "ar":
        return (
            f"يبلغ سطح المياه اليوم {area:.2f} كم²، أي بارتفاع "
            f"{_fmt_pct(delta)} فوق {ref_ar} ({baseline} كم²)."
        )
    return (
        f"Surface aujourd'hui : {area:.2f} km², soit {_fmt_pct(delta)} au-dessus "
        f"de {ref_fr} ({baseline} km²)."
    )


def build_gov_line(gov: dict, lang: str, month: int, history_years: int) -> str:
    ndvi = gov.get("mean_ndvi")
    z = gov.get("z_ndvi")
    month_name = _month_name(month, lang)
    if ndvi is None:
        return {
            "fr": "Aucune lecture récente exploitable pour ce gouvernorat.",
            "ar": "لا توجد قراءة حديثة قابلة للاستخدام لهذه الولاية.",
        }[lang]
    if z is None:
        return {
            "fr": f"NDVI moyen actuel : {ndvi:.3f}.",
            "ar": f"متوسط NDVI الحالي: {ndvi:.3f}.",
        }[lang]
    stddev_txt = f"{abs(z):.1f} σ"
    direction_fr = "en dessous" if z < 0 else "au-dessus"
    direction_ar = "أقل من" if z < 0 else "أعلى من"
    if lang == "ar":
        return (
            f"متوسط NDVI الحالي {ndvi:.3f}، وهو {stddev_txt} {direction_ar} "
            f"التوزيع المرجعي لشهر {month_name} على مدى {history_years} سنوات."
        )
    return (
        f"NDVI moyen actuel : {ndvi:.3f}, soit {stddev_txt} {direction_fr} "
        f"de la distribution de {month_name} sur {history_years} années d'historique."
    )


__all__ = ["build_briefing", "build_notable", "build_dam_line", "build_gov_line"]
