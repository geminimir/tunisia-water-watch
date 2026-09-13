"""UI translations for French and Arabic.

Every string that appears on the dashboard has an entry for both `fr` and `ar`.
Templates access strings via the `t` dict passed at render time, e.g. {{ t.dams_page_title }}.

Also defines translations for enum-like config values (regions, focus areas,
severity bands, status colours) so we don't leave any English visible.
"""

from __future__ import annotations

LANGUAGES = ("fr", "ar")
DEFAULT_LANG = "fr"

# Everything the templates render.
STRINGS: dict[str, dict[str, str]] = {
    # Site chrome
    "site_title": {
        "fr": "Tunisia Water Watch",
        "ar": "مراقبة مياه تونس",
    },
    "site_description": {
        "fr": "Suivi indépendant et satellitaire de l'eau et de l'agriculture en Tunisie.",
        "ar": "مراقبة مستقلة عبر الأقمار الاصطناعية للموارد المائية والزراعة في تونس.",
    },
    "nav_dams": {"fr": "Barrages", "ar": "السدود"},
    "nav_agriculture": {"fr": "Agriculture", "ar": "الزراعة"},
    "nav_about": {"fr": "À propos", "ar": "حول"},
    "nav_source": {"fr": "Code source", "ar": "الرمز المصدري"},
    "lang_toggle_to_fr": {"fr": "Français", "ar": "Français"},
    "lang_toggle_to_ar": {"fr": "العربية", "ar": "العربية"},

    # Footer
    "footer_updated": {"fr": "Dernière mise à jour :", "ar": "آخر تحديث:"},
    "footer_next_run": {"fr": "Prochaine exécution :", "ar": "التشغيل التالي:"},
    "footer_disclaimer": {
        "fr": "Données dérivées de Sentinel-2 L2A via des API STAC publiques. "
              "La surface d'eau est un indicateur du volume du réservoir, non un "
              "substitut à un relevé bathymétrique. Sous licence MIT. Sans garantie.",
        "ar": "بيانات مستنبطة من Sentinel-2 L2A عبر واجهات STAC العامة. "
              "المساحة المائية مؤشر تقريبي لحجم الخزان وليست بديلاً عن المسح البحري. "
              "رخصة MIT. بدون ضمان.",
    },
    "footer_oracle_link": {
        "fr": "Oracle de données (JSON + manifeste SHA-256)",
        "ar": "أوراكل البيانات (JSON + بصمة SHA-256)",
    },
    "footer_latest_link": {"fr": "latest.json", "ar": "latest.json"},
    "footer_readings_link": {"fr": "readings.csv", "ar": "readings.csv"},

    # Staleness banner
    "banner_stale_title": {"fr": "Le pipeline est en retard.", "ar": "توقفت التحديثات."},
    "banner_stale_body": {
        "fr": "La dernière lecture réussie remonte à {days} jours (seuil : {threshold} jours). "
              "Un problème est probable — consultez",
        "ar": "منذ آخر قراءة ناجحة {days} يومًا (الحد المسموح: {threshold} يومًا). "
              "من المحتمل وجود خلل — راجع",
    },
    "banner_stale_link": {"fr": "les journaux Actions", "ar": "سجلات Actions"},

    # Landing / hero
    "index_hero_title": {
        "fr": "Les barrages de Tunisie, observés depuis l'orbite",
        "ar": "سدود تونس تحت المراقبة من المدار",
    },
    "index_hero_body": {
        "fr": "Surface satellitaire indépendante de {n} grands réservoirs. "
              "Mise à jour tous les 5 jours. Zéro coût, zéro serveur, aucune clé API.",
        "ar": "مساحة سطح المياه المستنبطة من الأقمار الاصطناعية لـ{n} خزانًا كبيرًا. "
              "تحديث كل 5 أيام. بدون تكلفة، بدون خوادم، بدون مفاتيح API.",
    },
    "agri_hero_title": {
        "fr": "Les terres agricoles de Tunisie, observées depuis l'orbite",
        "ar": "الأراضي الزراعية التونسية تحت المراقبة من المدار",
    },
    "agri_hero_body": {
        "fr": "État sanitaire de la végétation dérivé par satellite pour "
              "{n} gouvernorats. NDVI (verdure) et NDMI (humidité de la canopée) "
              "calculés tous les 5 jours — une parcelle peut paraître verte tout "
              "en manquant d'eau, et suivre les deux permet de détecter la "
              "sécheresse tôt.",
        "ar": "حالة الغطاء النباتي مستنبطة من الأقمار الاصطناعية لـ{n} ولاية. "
              "يتم حساب NDVI (الاخضرار) وNDMI (رطوبة النبات) كل 5 أيام — "
              "قد تبدو الأرض خضراء بينما هي تجفّ، ومتابعة المؤشرين معًا تكشف الجفاف مبكرًا.",
    },

    # Stats
    "stat_total_area": {"fr": "Surface totale cartographiée (dernier passage)", "ar": "المساحة الإجمالية المقاسة (آخر مرور)"},
    "stat_dams_read": {"fr": "Barrages lus lors du dernier passage", "ar": "السدود المقروءة في آخر تشغيل"},
    "stat_pct_of_avg": {"fr": "de la moyenne saisonnière à long terme", "ar": "من المتوسط الموسمي طويل الأمد"},
    "stat_reservoir_index": {"fr": "Indice des réservoirs (moyenne pondérée par capacité)", "ar": "مؤشر الخزانات (متوسط مرجّح بالسعة)"},
    "stat_composite_severity": {"fr": "Sévérité composite de la sécheresse ({band})", "ar": "الشدة المركبة للجفاف ({band})"},
    "stat_national_composite": {"fr": "Sévérité nationale de la sécheresse ({band})", "ar": "الشدة الوطنية للجفاف ({band})"},
    "stat_governorates_monitored": {"fr": "Gouvernorats suivis", "ar": "الولايات المتابعة"},
    "stat_ndvi_label": {"fr": "NDVI (verdure) — {date}", "ar": "NDVI (الاخضرار) — {date}"},
    "stat_ndmi_label": {"fr": "NDMI (humidité de la canopée)", "ar": "NDMI (رطوبة الغطاء النباتي)"},
    "stat_healthy_label": {"fr": "Pixels sains (NDVI > 0,3 et NDMI > 0)", "ar": "بكسلات صحية (NDVI > 0.3 وNDMI > 0)"},
    "stat_latest_surface": {"fr": "Surface la plus récente ({date})", "ar": "أحدث مساحة ({date})"},
    "stat_of_baseline_rolling": {"fr": "de la ligne de base glissante ({v} km²)", "ar": "من الأساس المتحرك ({v} كم²)"},
    "stat_of_baseline_config": {"fr": "de la moyenne à long terme ({v} km²)", "ar": "من المتوسط طويل الأمد ({v} كم²)"},
    "stat_trend_30d": {"fr": "Tendance sur 30 jours", "ar": "الاتجاه خلال 30 يومًا"},
    "stat_severity_short": {"fr": "Sévérité composite de la sécheresse ({band})", "ar": "الشدة المركبة للجفاف ({band})"},
    "no_reading": {"fr": "aucune lecture", "ar": "لا توجد قراءة"},

    # Tables
    "th_dam": {"fr": "Barrage", "ar": "السد"},
    "th_governorate": {"fr": "Gouvernorat", "ar": "الولاية"},
    "th_region": {"fr": "Région", "ar": "المنطقة"},
    "th_last_reading": {"fr": "Dernière lecture", "ar": "آخر قراءة"},
    "th_surface_km2": {"fr": "Surface (km²)", "ar": "المساحة (كم²)"},
    "th_pct_of_avg": {"fr": "% de la moyenne", "ar": "% من المتوسط"},
    "th_status": {"fr": "Statut", "ar": "الحالة"},
    "th_date": {"fr": "Date", "ar": "التاريخ"},
    "th_cloud_pct": {"fr": "% de nuages", "ar": "% السحب"},
    "th_scene": {"fr": "Scène", "ar": "المشهد"},
    "th_ndvi": {"fr": "NDVI", "ar": "NDVI"},
    "th_ndmi": {"fr": "NDMI", "ar": "NDMI"},
    "th_healthy_pct": {"fr": "% sain", "ar": "% صحي"},
    "th_severity": {"fr": "Sévérité", "ar": "الشدة"},
    "th_band": {"fr": "Niveau", "ar": "المستوى"},
    "th_confidence": {"fr": "Confiance", "ar": "الثقة"},

    # Sections
    "section_all_dams": {"fr": "Tous les barrages", "ar": "جميع السدود"},
    "section_all_govs": {"fr": "Tous les gouvernorats", "ar": "جميع الولايات"},
    "section_national_series": {"fr": "Surface nationale des réservoirs dans le temps", "ar": "المساحة الوطنية للخزانات عبر الزمن"},
    "section_recent_readings": {"fr": "Lectures récentes", "ar": "القراءات الأخيرة"},

    # Dam / gov page bits
    "crumbs_back_dams": {"fr": "← Tous les barrages", "ar": "→ جميع السدود"},
    "crumbs_back_govs": {"fr": "← Tous les gouvernorats", "ar": "→ جميع الولايات"},
    "meta_river": {"fr": "rivière {v}", "ar": "نهر {v}"},
    "meta_capacity": {"fr": "capacité ≈ {v} hm³", "ar": "السعة ≈ {v} هم³"},
    "meta_governorate_of": {"fr": "gouvernorat de {v}", "ar": "ولاية {v}"},
    "meta_region_of": {"fr": "région {v}", "ar": "منطقة {v}"},
    "meta_focus": {"fr": "Cultures dominantes : {v}", "ar": "المحاصيل السائدة: {v}"},
    "next_reading_note": {
        "fr": "Prochaine lecture attendue : {date}. Source : Sentinel-2 L2A via STAC public. Seuil NDWI : {th}.",
        "ar": "القراءة القادمة المتوقعة: {date}. المصدر: Sentinel-2 L2A عبر STAC العام. عتبة NDWI: {th}.",
    },
    "composite_note": {
        "fr": "La sévérité composite combine l'anomalie du NDVI (par rapport à l'historique du même mois), "
              "l'anomalie du NDMI et l'indice des réservoirs de la région, chacun pondéré par la confiance. "
              "Plus élevé = plus sec que d'habitude.",
        "ar": "الشدة المركبة تجمع بين شذوذ NDVI (مقارنة بتاريخ الشهر نفسه)، وشذوذ NDMI، ومؤشر خزانات المنطقة، "
              "مع ترجيح كل مؤشر حسب الثقة. القيم الأعلى تعني ظروفًا أكثر جفافًا.",
    },
    "chart_surface": {"fr": "Surface (km²)", "ar": "المساحة (كم²)"},
    "chart_baseline_rolling": {"fr": "Ligne de base glissante", "ar": "الأساس المتحرك"},
    "chart_baseline_config": {"fr": "Moyenne à long terme", "ar": "المتوسط طويل الأمد"},
    "chart_ndvi_label": {"fr": "NDVI (verdure)", "ar": "NDVI (الاخضرار)"},
    "chart_ndmi_label": {"fr": "NDMI (humidité)", "ar": "NDMI (الرطوبة)"},

    # About page
    "about_h1": {"fr": "À propos de Tunisia Water Watch", "ar": "حول مراقبة مياه تونس"},
    "about_lede": {
        "fr": "Un système indépendant de suivi de l'eau et de l'agriculture pour la Tunisie, "
              "basé sur des données satellitaires. Conçu pour fonctionner indéfiniment sans "
              "maintenance, sans coût et sans intervention humaine.",
        "ar": "منظومة مستقلة لمراقبة الماء والزراعة في تونس تعتمد على الأقمار الاصطناعية، "
              "مصممة للعمل إلى أجل غير مسمى دون صيانة أو تكلفة أو تدخل بشري.",
    },
    "about_how_h": {"fr": "Comment ça fonctionne", "ar": "كيف يعمل النظام"},
    "about_how_steps": {
        "fr": [
            "Tous les 5 jours, un cron GitHub Actions se déclenche.",
            "Le pipeline interroge une API STAC publique pour la dernière scène Sentinel-2 peu nuageuse au-dessus de chaque zone.",
            "Seuls les pixels à l'intérieur de la zone sont téléchargés (lectures COG par plages) — souvent <1 Mo par barrage.",
            "Les bandes verte et proche infrarouge sont combinées en NDWI = (V − PIR) / (V + PIR).",
            "Les pixels au-dessus du seuil sont comptés comme eau ; le résultat est la surface en km².",
            "La lecture est ajoutée à data/readings.csv, et ce site statique est régénéré puis publié.",
        ],
        "ar": [
            "كل 5 أيام يعمل مؤقت GitHub Actions.",
            "يستعلم النظام واجهة STAC عمومية للحصول على آخر مشهد Sentinel-2 قليل السحب فوق كل منطقة.",
            "لا يتم تنزيل سوى البكسلات داخل المربع (قراءات COG جزئية) — عادةً أقل من 1 ميغابايت لكل سد.",
            "يُحسب NDWI = (الأخضر − الأشعة تحت الحمراء القريبة) / (الأخضر + الأشعة تحت الحمراء القريبة).",
            "تُحسب البكسلات فوق العتبة كماء والنتيجة هي المساحة بالكيلومتر المربع.",
            "تُضاف القراءة إلى data/readings.csv ثم يُعاد توليد الموقع الثابت ونشره.",
        ],
    },
    "about_notthis_h": {"fr": "Ce que ce système n'est pas", "ar": "ما ليس هذا النظام"},
    "about_notthis_p": {
        "fr": "Ce n'est pas une mesure volumétrique (qui exige une bathymétrie). Il ne détecte pas "
              "les fuites de canalisations enterrées, ne fournit pas d'alertes crues en temps réel, "
              "et ne fait pas tourner de modèles météo. Sa valeur est la continuité : il prend des "
              "données satellitaires gratuites qui survolent la Tunisie tous les 5 jours, calcule un "
              "indice simple, publie le résultat, et accumule un historique — pour toujours, gratuitement, "
              "sans que personne ait à s'impliquer.",
        "ar": "هذا النظام ليس قياسًا للحجم (الذي يتطلب مسحًا بحريًا). لا يكشف تسريبات الأنابيب "
              "المدفونة، ولا يقدم إنذارات فيضانات لحظية، ولا يشغّل نماذج طقس. قيمته في الاستمرارية: "
              "يستخدم بيانات ساتلية مجانية تعبر تونس كل 5 أيام، ويحسب مؤشرًا بسيطًا، ينشر النتيجة، "
              "ويراكم أرشيفًا تاريخيًا — للأبد، دون تكلفة، دون تدخل بشري.",
    },
    "about_license_h": {"fr": "Licence & provenance", "ar": "الترخيص والمصدر"},
    "about_license_p": {
        "fr": "Sous licence MIT. Données satellitaires © ESA / Copernicus. Tuiles cartographiques © contributeurs OpenStreetMap. "
              "Code source :",
        "ar": "الترخيص MIT. البيانات الساتلية © ESA / Copernicus. بلاطات الخريطة © مساهمو OpenStreetMap. "
              "الرمز المصدري:",
    },

    # Agriculture placeholder (kept as fallback)
    "agri_soon_h": {"fr": "Agriculture", "ar": "الزراعة"},

    # Language picker (root landing)
    "picker_title": {"fr": "Choisir la langue", "ar": "اختر اللغة"},
    "picker_fr": {"fr": "Français", "ar": "Français"},
    "picker_ar": {"fr": "العربية", "ar": "العربية"},

    # Severity band labels (used as {band} substitution and in badges).
    "band_abundant": {"fr": "abondant", "ar": "وفير"},
    "band_normal": {"fr": "normal", "ar": "عادي"},
    "band_watch": {"fr": "vigilance", "ar": "تحذير"},
    "band_drought": {"fr": "sécheresse", "ar": "جفاف"},
    "band_severe": {"fr": "sévère", "ar": "شديد"},
    "band_unknown": {"fr": "inconnu", "ar": "غير معروف"},

    # Status colour labels used on the dam-list badge
    "status_green": {"fr": "normal", "ar": "عادي"},
    "status_yellow": {"fr": "à surveiller", "ar": "قيد المتابعة"},
    "status_red": {"fr": "bas", "ar": "منخفض"},
    "status_gray": {"fr": "inconnu", "ar": "غير معروف"},
}

# Region translations (config values)
REGIONS: dict[str, dict[str, str]] = {
    "north":       {"fr": "Nord",         "ar": "الشمال"},
    "cap-bon":     {"fr": "Cap Bon",      "ar": "الوطن القبلي"},
    "sahel":       {"fr": "Sahel",        "ar": "الساحل"},
    "centre-west": {"fr": "Centre-Ouest", "ar": "الوسط الغربي"},
    "south":       {"fr": "Sud",          "ar": "الجنوب"},
}

# Governorate agricultural focus translations (config values)
FOCUS: dict[str, dict[str, str]] = {
    "cereals":         {"fr": "céréales",              "ar": "حبوب"},
    "olive":           {"fr": "olivier",               "ar": "زيتون"},
    "citrus-hort":     {"fr": "agrumes / maraîchage",  "ar": "حمضيات / بستنة"},
    "oasis":           {"fr": "oasis",                 "ar": "واحات"},
    "rangeland":       {"fr": "parcours",              "ar": "مراعي"},
    "peri-urban":      {"fr": "périurbain",            "ar": "شبه حضري"},
    "cereals-olive":   {"fr": "céréales / olivier",    "ar": "حبوب / زيتون"},
    "olive-orchard":   {"fr": "olivier / vergers",     "ar": "زيتون / بستنة"},
}


def t_for(lang: str) -> dict[str, object]:
    """Return a flat mapping of key → string for the given lang."""
    out: dict[str, object] = {}
    for k, v in STRINGS.items():
        val = v.get(lang, v.get(DEFAULT_LANG, ""))
        out[k] = val
    return out


def translate_region(v: str, lang: str) -> str:
    return REGIONS.get(v, {}).get(lang, v)


def translate_focus(v: str, lang: str) -> str:
    return FOCUS.get(v, {}).get(lang, v)


def translate_band(v: str, lang: str) -> str:
    key = f"band_{v}"
    return STRINGS.get(key, {}).get(lang, v)


def translate_status(v: str, lang: str) -> str:
    key = f"status_{v}"
    return STRINGS.get(key, {}).get(lang, v)


def coverage_check() -> list[str]:
    """Return the list of translation keys missing an fr or ar entry."""
    missing: list[str] = []
    for k, v in STRINGS.items():
        for lang in LANGUAGES:
            if lang not in v or v[lang] in (None, ""):
                missing.append(f"{k}:{lang}")
    for k, v in REGIONS.items():
        for lang in LANGUAGES:
            if lang not in v or v[lang] in (None, ""):
                missing.append(f"REGION[{k}]:{lang}")
    for k, v in FOCUS.items():
        for lang in LANGUAGES:
            if lang not in v or v[lang] in (None, ""):
                missing.append(f"FOCUS[{k}]:{lang}")
    return missing


__all__ = [
    "STRINGS", "REGIONS", "FOCUS", "LANGUAGES", "DEFAULT_LANG",
    "t_for", "translate_region", "translate_focus",
    "translate_band", "translate_status", "coverage_check",
]
