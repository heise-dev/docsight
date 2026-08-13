"""Incident Report PDF generator for DOCSight."""

import io
import json
import logging
import math
import os
from datetime import datetime, timezone

from fpdf import FPDF

from app.analyzer import _get_ds_power_thresholds, _get_snr_thresholds, get_thresholds
from app.docsis_utils import (
    channel_type_label as _channel_type_label,
    classify_channel_family as _classify_channel_family,
    modulation_threshold_key as _modulation_threshold_key,
)
from app.threshold_profiles import BUILTIN_THRESHOLD_PROFILES

log = logging.getLogger("docsis.report")

_FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fonts")


def _format_threshold_table():
    """Build display-ready threshold rows from the active analyzer profile."""
    t = get_thresholds()
    rows = []
    # DS Power - per modulation
    ds = t.get("downstream_power", {})
    for mod in sorted(k for k in ds if not k.startswith("_")):
        v = ds[mod]
        g = v.get("good", [0, 0])
        w = v.get("warning", [0, 0])
        c = v.get("critical", [0, 0])
        rows.append({
            "category": "DS Power",
            "variant": mod,
            "good": f"{g[0]} to {g[1]} dBmV",
            "tolerated": f"{w[0]} to {w[1]} dBmV",
            "critical": f"{c[0]} to {c[1]} dBmV",
            "ref": "VFKD",
        })
    # US Power - per channel type
    us = t.get("upstream_power", {})
    for key in sorted(k for k in us if not k.startswith("_")):
        v = us[key]
        g = v.get("good", [0, 0])
        w = v.get("warning", [0, 0])
        c = v.get("critical", [0, 0])
        rows.append({
            "category": "US Power",
            "variant": key,
            "good": f"{g[0]} to {g[1]} dBmV",
            "tolerated": f"{w[0]} to {w[1]} dBmV",
            "critical": f"{c[0]} to {c[1]} dBmV",
            "ref": "VFKD",
        })
    # SNR - per modulation
    snr = t.get("snr", {})
    for mod in sorted(k for k in snr if not k.startswith("_")):
        v = snr[mod]
        rows.append({
            "category": "SNR/MER",
            "variant": mod,
            "good": f">= {v.get('good_min', 0)} dB",
            "tolerated": f">= {v.get('warning_min', 0)} dB",
            "critical": f">= {v.get('critical_min', 0)} dB",
            "ref": "VFKD",
        })
    # US Modulation - QAM order health
    us_mod = t.get("upstream_modulation", {})
    warn_qam = us_mod.get("warning_max_qam")
    crit_qam = us_mod.get("critical_max_qam")
    if warn_qam is not None and crit_qam is not None:
        rows.append({
            "category": "US Modulation",
            "variant": "QAM Order",
            "good": f"> {warn_qam}-QAM",
            "tolerated": f"<= {warn_qam}-QAM",
            "critical": f"<= {crit_qam}-QAM",
            "ref": "VFKD",
        })
    return rows


def _default_warn_thresholds(ds_snr_warn_min=None):
    """Get default warning thresholds as display strings for report."""
    t = get_thresholds()
    ds = t.get("downstream_power", {}).get("256QAM", {})
    us = t.get("upstream_power", {}).get("sc_qam", {})
    snr = t.get("snr", {}).get("256QAM", {})
    ds_w = ds.get("warning", [-5.9, 18.0])
    us_w = us.get("warning", [37.1, 51.0])
    return {
        "ds_power": f"{ds_w[0]} to {ds_w[1]} dBmV",
        "us_power": f"{us_w[0]} to {us_w[1]} dBmV",
        "snr": f">= {ds_snr_warn_min if ds_snr_warn_min is not None else snr.get('warning_min', 31.0)} dB",
    }


def _historical_builtin_thresholds(snapshot):
    """Return immutable built-in thresholds only for an exact provenance match."""
    meta = snapshot.get("analysis_meta")
    profile_ref = meta.get("threshold_profile") if isinstance(meta, dict) else None
    if not isinstance(profile_ref, dict):
        return None

    profile_id = profile_ref.get("id")
    profile_version = profile_ref.get("version")
    for profile in BUILTIN_THRESHOLD_PROFILES:
        if profile.get("id") != profile_id or profile.get("version") != profile_version:
            continue
        thresholds = profile.get("thresholds")
        return thresholds if isinstance(thresholds, dict) else None
    return None


def _has_analyzer_snapshot_semantics(snapshot):
    """Return whether sparse metric-health keys use analyzer-era semantics."""
    meta = snapshot.get("analysis_meta")
    if not isinstance(meta, dict):
        return False

    schema = meta.get("analyzer_schema")
    if isinstance(schema, bool):
        return False
    if isinstance(schema, int):
        return schema >= 1
    if isinstance(schema, float):
        return math.isfinite(schema) and schema.is_integer() and schema >= 1
    return False


def _historical_power_bounds(thresholds, direction, channel, family):
    if thresholds is None:
        return None
    section_name = "upstream_power" if direction == "us" else "downstream_power"
    section = thresholds.get(section_name)
    if not isinstance(section, dict):
        return None

    if direction == "us":
        key = "ofdma" if family == "ofdma" else "sc_qam"
    elif family == "ofdm":
        key = "ofdm"
    else:
        key = _modulation_threshold_key(channel.get("modulation"), section)
    spec = section.get(key)
    critical = spec.get("critical") if isinstance(spec, dict) else None
    if not isinstance(critical, (list, tuple)) or len(critical) != 2:
        return None
    try:
        return float(critical[0]), float(critical[1])
    except (TypeError, ValueError):
        return None


def _historical_snr_min(thresholds, channel, family):
    if thresholds is None:
        return None
    section = thresholds.get("snr")
    if not isinstance(section, dict):
        return None
    key = "ofdm" if family == "ofdm" else _modulation_threshold_key(
        channel.get("modulation"), section
    )
    spec = section.get(key)
    if not isinstance(spec, dict) or "critical_min" not in spec:
        return None
    try:
        return float(spec["critical_min"])
    except (TypeError, ValueError):
        return None


def _stored_metric_direction(channel, metric):
    """Read the analyzer's stored critical direction without reclassification."""
    detail = str(channel.get("health_detail") or "").lower()
    if metric == "snr" and "snr critical" in detail:
        return "low"
    if f"{metric} critical high" in detail:
        return "high"
    if f"{metric} critical low" in detail:
        return "low"
    return None


def _diagnostic_note(
    *, note_type, channel, channel_label, metric, value, boundary=None,
    boundary_key=None, extreme_pct=50,
):
    note = {
        "type": note_type,
        "channel_id": channel.get("channel_id", "?"),
        "channel_type": channel_label,
        "metric": metric,
        "value": value,
        "severity": "critical",
    }
    if boundary is None or boundary_key is None:
        return note

    if boundary_key == "spec_max":
        distance = value - boundary
    else:
        distance = boundary - value
    if distance <= 0:
        # The stored critical classification remains authoritative even when a
        # supposedly matching profile is internally inconsistent with it.
        return note

    deviation = round(distance / max(abs(boundary), 1) * 100)
    note.update({boundary_key: boundary, "deviation_pct": deviation})
    note["severity"] = "extreme" if deviation > extreme_pct else "critical"
    return note


def _build_diagnostic_notes(current_analysis):
    """Build critical notes without reinterpreting stored analyzer output.

    Analyzer-era metric-health keys are sparse, so an absent key means good.
    Exact numeric claims are reconstructed only from a built-in profile whose
    stored id and version both match. Legacy snapshots without a key retain
    the active-threshold behavior used before provenance was persisted.
    """
    if not current_analysis:
        return []

    notes = []
    analyzer_snapshot = _has_analyzer_snapshot_semantics(current_analysis)
    us_thresholds = (
        {} if analyzer_snapshot else get_thresholds().get("upstream_power", {})
    )
    historical_thresholds = _historical_builtin_thresholds(current_analysis)

    for ch in current_analysis.get("us_channels", []):
        power = ch.get("power")
        if power is None:
            continue
        family = _classify_channel_family("us", ch)
        key = "ofdma" if family == "ofdma" else "sc_qam"
        channel_label = _channel_type_label("us", ch) or (ch.get("modulation") or key.upper())
        if "power_health" in ch:
            if ch.get("power_health") != "critical":
                continue
            bounds = _historical_power_bounds(historical_thresholds, "us", ch, family)
            stored_direction = _stored_metric_direction(ch, "power")
            if stored_direction is None and bounds is not None:
                if power > bounds[1]:
                    stored_direction = "high"
                elif power < bounds[0]:
                    stored_direction = "low"
            if stored_direction == "high":
                notes.append(_diagnostic_note(
                    note_type="us_power_high", channel=ch, channel_label=channel_label,
                    metric="upstream power", value=power,
                    boundary=bounds[1] if bounds else None, boundary_key="spec_max",
                ))
            elif stored_direction == "low":
                notes.append(_diagnostic_note(
                    note_type="us_power_low", channel=ch, channel_label=channel_label,
                    metric="upstream power", value=power,
                    boundary=bounds[0] if bounds else None, boundary_key="spec_min",
                ))
            continue
        if analyzer_snapshot:
            continue

        # Explicit legacy behavior: only snapshots predating analyzer schema
        # provenance use the currently active analyzer profile.
        spec = us_thresholds.get(key, {})
        crit = spec.get("critical", [35.0, 53.0])
        if power > crit[1]:
            notes.append(_diagnostic_note(
                note_type="us_power_high", channel=ch, channel_label=channel_label,
                metric="upstream power", value=power,
                boundary=crit[1], boundary_key="spec_max",
            ))
        elif power < crit[0]:
            notes.append(_diagnostic_note(
                note_type="us_power_low", channel=ch, channel_label=channel_label,
                metric="upstream power", value=power,
                boundary=crit[0], boundary_key="spec_min",
            ))

    for ch in current_analysis.get("ds_channels", []):
        mod = (ch.get("modulation") or "256QAM").upper()
        family = ch.get("channel_family") or _classify_channel_family("ds", ch)
        channel_label = _channel_type_label("ds", ch) or mod
        power = ch.get("power")
        if power is not None:
            if "power_health" in ch:
                if ch.get("power_health") == "critical":
                    bounds = _historical_power_bounds(historical_thresholds, "ds", ch, family)
                    stored_direction = _stored_metric_direction(ch, "power")
                    if stored_direction is None and bounds is not None:
                        if power > bounds[1]:
                            stored_direction = "high"
                        elif power < bounds[0]:
                            stored_direction = "low"
                    if stored_direction == "high":
                        notes.append(_diagnostic_note(
                            note_type="ds_power_high", channel=ch, channel_label=channel_label,
                            metric="downstream power", value=power,
                            boundary=bounds[1] if bounds else None, boundary_key="spec_max",
                        ))
                    elif stored_direction == "low":
                        notes.append(_diagnostic_note(
                            note_type="ds_power_low", channel=ch, channel_label=channel_label,
                            metric="downstream power", value=power,
                            boundary=bounds[0] if bounds else None, boundary_key="spec_min",
                        ))
            elif not analyzer_snapshot:
                spec = _get_ds_power_thresholds(mod, channel_family=family)
                if power > spec["crit_max"]:
                    notes.append(_diagnostic_note(
                        note_type="ds_power_high", channel=ch, channel_label=channel_label,
                        metric="downstream power", value=power,
                        boundary=spec["crit_max"], boundary_key="spec_max",
                    ))
                elif power < spec["crit_min"]:
                    notes.append(_diagnostic_note(
                        note_type="ds_power_low", channel=ch, channel_label=channel_label,
                        metric="downstream power", value=power,
                        boundary=spec["crit_min"], boundary_key="spec_min",
                    ))

        snr = ch.get("snr")
        if snr is not None:
            if "snr_health" in ch:
                if ch.get("snr_health") == "critical":
                    snr_crit = _historical_snr_min(historical_thresholds, ch, family)
                    if _stored_metric_direction(ch, "snr") == "low":
                        notes.append(_diagnostic_note(
                            note_type="snr_low", channel=ch, channel_label=channel_label,
                            metric="SNR/MER", value=snr, boundary=snr_crit,
                            boundary_key="spec_min", extreme_pct=30,
                        ))
            elif not analyzer_snapshot:
                snr_spec = _get_snr_thresholds(mod, channel_family=family)
                snr_crit = snr_spec["crit_min"]
                if snr < snr_crit:
                    notes.append(_diagnostic_note(
                        note_type="snr_low", channel=ch, channel_label=channel_label,
                        metric="SNR/MER", value=snr, boundary=snr_crit,
                        boundary_key="spec_min", extreme_pct=30,
                    ))

    return notes


_DIAGNOSTIC_TYPE_ORDER = {
    "us_power_high": 0,
    "us_power_low": 1,
    "ds_power_high": 2,
    "ds_power_low": 3,
    "snr_low": 4,
}


def _canonical_snapshot_timestamp(value):
    """Return a canonical UTC timestamp for deterministic report rendering."""
    raw = str(value or "").strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return str(value or "")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _stable_channel_identity(note):
    channel_id = note.get("channel_id")
    try:
        return 0, int(channel_id)
    except (TypeError, ValueError):
        return 1, str(channel_id or "")


def derive_historical_report_data(snapshots):
    """Derive latest status and one deterministic worst diagnostic per type."""
    prepared = []
    candidates = []
    for snapshot in snapshots or []:
        observed_at = _canonical_snapshot_timestamp(snapshot.get("timestamp"))
        prepared.append((observed_at, snapshot))
        for note in _build_diagnostic_notes(snapshot):
            candidate = dict(note)
            candidate["observed_at"] = observed_at
            candidates.append(candidate)

    latest_snapshot = None
    if prepared:
        latest_snapshot = max(prepared, key=lambda item: item[0])[1]

    selected = {}
    for note in candidates:
        note_type = note.get("type")
        if note_type not in _DIAGNOSTIC_TYPE_ORDER:
            continue
        numeric_provenance = "deviation_pct" in note
        if numeric_provenance:
            try:
                evidence_rank = -float(note["deviation_pct"])
            except (TypeError, ValueError):
                numeric_provenance = False
        if not numeric_provenance:
            try:
                raw_value = float(note.get("value"))
            except (TypeError, ValueError):
                raw_value = 0.0
            evidence_rank = -raw_value if note_type.endswith("_high") else raw_value

        # Exact numeric provenance is preferred because its deviations are
        # historically comparable. Neutral stored-critical evidence is ranked
        # only by its direction-aware raw value; no percentage is fabricated.
        rank = (
            0 if numeric_provenance else 1,
            evidence_rank,
            note["observed_at"],
            _stable_channel_identity(note),
            str(note.get("channel_type") or ""),
            _DIAGNOSTIC_TYPE_ORDER[note_type],
        )
        existing = selected.get(note_type)
        if existing is None or rank < existing[0]:
            selected[note_type] = (rank, note)

    notes = [
        selected[note_type][1]
        for note_type in sorted(selected, key=_DIAGNOSTIC_TYPE_ORDER.get)
    ]
    return {"latest_snapshot": latest_snapshot, "diagnostic_notes": notes}


def _format_diagnostic_note(note, s):
    if "spec_max" in note:
        template = s.get(
            "diag_note_high",
            "Channel {ch} ({ch_type}): {metric} of {value} dBmV exceeds spec "
            "maximum ({spec} dBmV) by {pct}%.",
        )
        spec = note["spec_max"]
    elif "spec_min" in note:
        if note["type"] == "snr_low":
            template = s.get(
                "diag_note_snr_low",
                "Channel {ch} ({ch_type}): {metric} of {value} dB is below spec "
                "minimum ({spec} dB) by {pct}%.",
            )
        else:
            template = s.get(
                "diag_note_low",
                "Channel {ch} ({ch_type}): {metric} of {value} dBmV is below spec "
                "minimum ({spec} dBmV) by {pct}%.",
            )
        spec = note["spec_min"]
    else:
        unit = "dB" if note["type"] == "snr_low" else "dBmV"
        if note["type"].endswith("_high"):
            template = s.get(
                "diag_note_stored_critical_high",
                "Channel {ch} ({ch_type}): {metric} of {value} {unit} was "
                "recorded as critically high by the stored analyzer result; "
                "the exact historical threshold is unavailable.",
            )
        else:
            template = s.get(
                "diag_note_stored_critical_low",
                "Channel {ch} ({ch_type}): {metric} of {value} {unit} was "
                "recorded as critically low by the stored analyzer result; "
                "the exact historical threshold is unavailable.",
            )
        diagnostic = template.format(
            ch=note["channel_id"],
            ch_type=note["channel_type"],
            metric=note["metric"],
            value=note["value"],
            unit=unit,
        )
        observed = s.get(
            "diagnostic_observed_at", "Observed at {observed_at} UTC."
        ).format(observed_at=note["observed_at"])
        return f"{diagnostic} {observed}"
    diagnostic = template.format(
        ch=note["channel_id"],
        ch_type=note["channel_type"],
        metric=note["metric"],
        value=note["value"],
        spec=spec,
        pct=note["deviation_pct"],
    )
    observed = s.get(
        "diagnostic_observed_at", "Observed at {observed_at} UTC."
    ).format(observed_at=note["observed_at"])
    return f"{diagnostic} {observed}"


def _format_diagnostic_complaint(notes, s):
    """Format diagnostic notes as complaint letter text section."""
    if not notes:
        return ""
    lines = [s.get("complaint_diag_header", "Diagnostic analysis:")]
    for note in notes:
        text = _format_diagnostic_note(note, s)
        if text:
            lines.append("- " + text)
    if any(n.get("severity") == "extreme" for n in notes):
        lines.append("")
        lines.append(s.get("diag_note_isp_hint", ""))
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Localised strings for PDF reports
# ---------------------------------------------------------------------------
_REPORT_I18N_DIR = os.path.join(os.path.dirname(__file__), "i18n")


def _load_report_strings():
    if not os.path.isdir(_REPORT_I18N_DIR):
        raise RuntimeError(f"Report i18n directory missing: {_REPORT_I18N_DIR}")

    strings = {}
    for fname in sorted(os.listdir(_REPORT_I18N_DIR)):
        if not fname.endswith(".json") or fname == "template.json":
            continue

        lang = fname[:-5]
        fpath = os.path.join(_REPORT_I18N_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            if lang == "en":
                raise RuntimeError(f"Failed to load required report i18n file {fpath}") from exc
            log.warning("Failed to load optional report i18n file %s: %s", fpath, exc)
            continue

        if not isinstance(data, dict):
            if lang == "en":
                raise RuntimeError(f"Required report i18n file {fpath} does not contain a JSON object")
            log.warning("Skipping non-object report i18n payload in %s", fpath)
            continue

        strings[lang] = {
            key: value for key, value in data.items()
            if not str(key).startswith("_")
        }

    if "en" not in strings:
        raise RuntimeError("Report i18n requires app/modules/reports/i18n/en.json")

    return strings


REPORT_STRINGS = _load_report_strings()


def _get_report_strings(lang="en"):
    strings = dict(REPORT_STRINGS["en"])
    if lang != "en":
        strings.update(REPORT_STRINGS.get(lang, {}))
    return strings


class IncidentReport(FPDF):
    """Custom PDF class for DOCSight incident reports."""

    def __init__(self, lang="en"):
        super().__init__()
        self.lang = lang
        self._s = _get_report_strings(lang)
        self.add_font("dejavu", "", os.path.join(_FONT_DIR, "DejaVuSans.ttf"))
        self.add_font("dejavu", "B", os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf"))
        self.add_font("dejavu", "I", os.path.join(_FONT_DIR, "DejaVuSans-Oblique.ttf"))
        self.set_auto_page_break(auto=True, margin=20)

    def header(self):
        s = self._s
        self.set_font("dejavu", "B", 16)
        self.cell(0, 10, s["report_title"], new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("dejavu", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 5, f"{s['generated']} {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_text_color(0, 0, 0)
        self.ln(5)

    def footer(self):
        s = self._s
        self.set_y(-15)
        self.set_font("dejavu", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"{s['footer']} - {s['page']} {self.page_no()}/{{nb}}", align="C")

    def _section_title(self, title):
        self.set_font("dejavu", "B", 13)
        self.set_fill_color(41, 128, 185)
        self.set_text_color(255, 255, 255)
        self.cell(0, 8, f"  {title}", new_x="LMARGIN", new_y="NEXT", fill=True)
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def _key_value(self, key, value, bold_value=False):
        self.set_font("dejavu", "", 10)
        key_text = key + ":"
        key_w = max(65, self.get_string_width(key_text) + 4)
        self.cell(key_w, 6, key_text, new_x="RIGHT")
        self.set_font("dejavu", "B" if bold_value else "", 10)
        self.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    def _health_color(self, health):
        if health == "good":
            return (39, 174, 96)
        elif health == "tolerated":
            return (132, 204, 22)
        elif health == "marginal":
            return (243, 156, 18)
        return (231, 76, 60)

    def _table_header(self, cols, widths):
        self.set_font("dejavu", "B", 9)
        self.set_fill_color(220, 220, 220)
        for col, w in zip(cols, widths):
            self.cell(w, 6, col, border=1, fill=True, align="C")
        self.ln()

    def _table_row(self, cells, widths, health=None):
        self.set_font("dejavu", "", 8)
        if health:
            r, g, b = self._health_color(health)
            self.set_text_color(r, g, b)
        for cell, w in zip(cells, widths):
            self.cell(w, 5, str(cell), border=1, align="C")
        self.set_text_color(0, 0, 0)
        self.ln()


def _format_optional_count(value):
    """Format a counter value while preserving unsupported/null as N/A."""
    return f"{value:,}" if value is not None else "N/A"


def _format_optional_decimal(value):
    """Format a decimal metric while preserving unavailable values."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _format_optional_measurement(value):
    """Format a historical aggregate without turning missing data into zero."""
    return "N/A" if value is None else str(value)


def _compute_worst_values(snapshots):
    """Compute worst values across all snapshots in the range."""
    worst = {
        "ds_power_max": None,
        "ds_power_min": None,
        "us_power_max": None,
        "ds_snr_min": None,
        "ds_snr_warn_min": None,
        "ds_uncorrectable_max": None,
        "ds_correctable_max": None,
        "health_critical_count": 0,
        "health_marginal_count": 0,
        "health_tolerated_count": 0,
        "total_snapshots": len(snapshots),
    }
    for snap in snapshots:
        s = snap["summary"]
        ds_power_max = s.get("ds_power_max")
        if ds_power_max is not None and (
            worst["ds_power_max"] is None
            or abs(ds_power_max) > abs(worst["ds_power_max"])
        ):
            worst["ds_power_max"] = ds_power_max
        ds_power_min = s.get("ds_power_min")
        if ds_power_min is not None and (
            worst["ds_power_min"] is None
            or abs(ds_power_min) > abs(worst["ds_power_min"])
        ):
            worst["ds_power_min"] = ds_power_min
        us_power_max = s.get("us_power_max")
        if us_power_max is not None and (
            worst["us_power_max"] is None or us_power_max > worst["us_power_max"]
        ):
            worst["us_power_max"] = us_power_max
        ds_snr_min = s.get("ds_snr_min")
        if ds_snr_min is not None and (
            worst["ds_snr_min"] is None or ds_snr_min < worst["ds_snr_min"]
        ):
            worst["ds_snr_min"] = ds_snr_min
            worst["ds_snr_warn_min"] = None
        if (
            ds_snr_min is not None
            and ds_snr_min == worst["ds_snr_min"]
            and worst["ds_snr_warn_min"] is None
        ):
            try:
                target_snr = float(ds_snr_min)
            except (TypeError, ValueError):
                target_snr = None
            if target_snr is not None:
                for ch in snap.get("ds_channels") or []:
                    try:
                        channel_snr = float(ch.get("snr"))
                    except (TypeError, ValueError):
                        continue
                    if channel_snr != target_snr:
                        continue
                    family = ch.get("channel_family") or _classify_channel_family("ds", ch)
                    snr_spec = _get_snr_thresholds(ch.get("modulation"), channel_family=family)
                    worst["ds_snr_warn_min"] = snr_spec["warn_min"]
                    break
        errors_supported = s.get("errors_supported", True)
        uncorr_errors = s.get("ds_uncorrectable_errors") if errors_supported else None
        corr_errors = s.get("ds_correctable_errors") if errors_supported else None
        if uncorr_errors is not None and (
            worst["ds_uncorrectable_max"] is None or uncorr_errors > worst["ds_uncorrectable_max"]
        ):
            worst["ds_uncorrectable_max"] = uncorr_errors
        if corr_errors is not None and (
            worst["ds_correctable_max"] is None or corr_errors > worst["ds_correctable_max"]
        ):
            worst["ds_correctable_max"] = corr_errors
        health = s.get("health", "good")
        if health == "critical":
            worst["health_critical_count"] += 1
        elif health == "marginal":
            worst["health_marginal_count"] += 1
        elif health == "tolerated":
            worst["health_tolerated_count"] += 1
    return worst


def _find_worst_channels(snapshots):
    """Find channels that were most frequently in bad health."""
    ds_issues = {}
    us_issues = {}
    for snap in snapshots:
        for ch in snap.get("ds_channels", []):
            cid = ch.get("channel_id", 0)
            if ch.get("health") not in ("good", "tolerated"):
                ds_issues[cid] = ds_issues.get(cid, 0) + 1
        for ch in snap.get("us_channels", []):
            cid = ch.get("channel_id", 0)
            if ch.get("health") not in ("good", "tolerated"):
                us_issues[cid] = us_issues.get(cid, 0) + 1
    ds_sorted = sorted(ds_issues.items(), key=lambda x: x[1], reverse=True)[:5]
    us_sorted = sorted(us_issues.items(), key=lambda x: x[1], reverse=True)[:5]
    return ds_sorted, us_sorted


def _comparison_label(s, key):
    labels = {
        "good": s.get("comparison_health_good", "Good"),
        "tolerated": s.get("comparison_health_tolerated", "Tolerated"),
        "marginal": s.get("comparison_health_marginal", "Marginal"),
        "critical": s.get("comparison_health_critical", "Critical"),
        "unknown": s.get("comparison_health_unknown", "Unknown"),
    }
    return labels.get(key, key.title())


def _comparison_top_health(period, s):
    dist = period.get("health_distribution") or {}
    if not dist:
        return _comparison_label(s, "unknown")
    best_key = max(dist, key=lambda name: dist.get(name, 0))
    total = max(period.get("snapshots", 0), 1)
    pct = round(dist.get(best_key, 0) / total * 100)
    return f"{_comparison_label(s, best_key)} ({pct}%)"


def _format_comparison_value(value, unit="", is_int=False):
    if value is None:
        return "-"
    if is_int:
        text = f"{int(value):,}"
    else:
        text = f"{value:+.2f}"
    return f"{text} {unit}".strip()


def _format_comparison_timestamp(ts):
    if not ts:
        return "-"
    raw = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return str(ts)
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_comparison_evidence(comparison_data, s):
    if not comparison_data:
        return ""

    period_a = comparison_data.get("period_a") or {}
    period_b = comparison_data.get("period_b") or {}
    delta = comparison_data.get("delta") or {}

    lines = [
        s.get("comparison_complaint_header", "Before/After comparison evidence:"),
        "",
        s.get("comparison_complaint_periods", "Compared {from_a} to {to_a} against {from_b} to {to_b}.").format(
            from_a=_format_comparison_timestamp(period_a.get("from")),
            to_a=_format_comparison_timestamp(period_a.get("to")),
            from_b=_format_comparison_timestamp(period_b.get("from")),
            to_b=_format_comparison_timestamp(period_b.get("to")),
        ),
        f"- {s.get('comparison_complaint_snapshots', 'Snapshots: Period A {snapshots_a}, Period B {snapshots_b}.').format(snapshots_a=period_a.get('snapshots', 0), snapshots_b=period_b.get('snapshots', 0))}",
        f"- {s.get('comparison_complaint_verdict', 'Overall verdict: {verdict}.').format(verdict=s.get('comparison_verdict_' + str(delta.get('verdict', 'unchanged')), str(delta.get('verdict', 'unchanged')).title()))}",
        f"- {s.get('comparison_complaint_health', 'Dominant health changed from {health_a} to {health_b}.').format(health_a=_comparison_top_health(period_a, s), health_b=_comparison_top_health(period_b, s))}",
        f"- {s.get('comparison_complaint_ds_power', 'Average DS power delta: {value}.').format(value=_format_comparison_value(delta.get('ds_power'), 'dBmV'))}",
        f"- {s.get('comparison_complaint_ds_snr', 'Average DS SNR delta: {value}.').format(value=_format_comparison_value(delta.get('ds_snr'), 'dB'))}",
        f"- {s.get('comparison_complaint_us_power', 'Average US power delta: {value}.').format(value=_format_comparison_value(delta.get('us_power'), 'dBmV'))}",
        f"- {s.get('comparison_complaint_uncorr', 'Uncorrectable error delta: {value}.').format(value=_format_comparison_value(delta.get('uncorr_errors'), '', True))}",
        "",
    ]
    return "\n".join(lines)


def _localized_closing_placeholder(s, line_index, fallback):
    closing = s.get("complaint_closing", "")
    lines = closing.splitlines() if isinstance(closing, str) else []
    if len(lines) > line_index and lines[line_index].strip():
        return lines[line_index].strip()
    return fallback


def _format_customer_closing(s, customer_name="", customer_number="", customer_address=""):
    """Build a localized complaint closing with provided customer details."""
    label = s.get("complaint_closing_label") or _localized_closing_placeholder(s, 0, "Sincerely,")
    name = (customer_name or "").strip() or _localized_closing_placeholder(s, 1, "[Your Name]")
    number = (customer_number or "").strip() or _localized_closing_placeholder(s, 2, "[Customer Number]")
    address = (customer_address or "").strip()
    address_lines = address.splitlines() if address else [_localized_closing_placeholder(s, 3, "[Address]")]
    return "\n".join([label, name, number, *address_lines])


def _report_bounds(snapshots, report_start=None, report_end=None):
    timestamps = sorted(
        _canonical_snapshot_timestamp(snapshot.get("timestamp"))
        for snapshot in snapshots or []
    )
    start = report_start or (timestamps[0] if timestamps else "-")
    end = report_end or (timestamps[-1] if timestamps else "-")
    return start, end


def generate_report(
    snapshots,
    current_analysis=None,
    config=None,
    connection_info=None,
    lang="en",
    comparison_data=None,
    customer_name="",
    customer_number="",
    customer_address="",
    report_start=None,
    report_end=None,
):
    """Generate a PDF incident report.

    Args:
        snapshots: List of snapshot dicts from storage.get_range_data()
        current_analysis: Deprecated compatibility argument; not used for reports
        config: Config dict (isp_name, etc.)
        connection_info: Connection info dict (speeds, etc.)
        lang: Language code
        comparison_data: Optional before/after comparison payload
        customer_name: Customer name for the embedded complaint letter
        customer_number: Customer/contract number for the embedded complaint letter
        customer_address: Customer address for the embedded complaint letter
        report_start: Requested inclusive report-window start in canonical UTC
        report_end: Requested inclusive report-window end in canonical UTC

    Returns:
        bytes: PDF file content
    """
    config = config or {}
    connection_info = connection_info or {}
    s = _get_report_strings(lang)
    report_start, report_end = _report_bounds(snapshots, report_start, report_end)
    historical = derive_historical_report_data(snapshots)
    latest_snapshot = historical["latest_snapshot"]
    diag_notes = historical["diagnostic_notes"]
    pdf = IncidentReport(lang=lang)
    pdf.alias_nb_pages()
    pdf.add_page()

    # --- Connection Info ---
    pdf._section_title(s["section_connection_info"])
    isp = config.get("isp_name", "Unknown ISP")
    pdf._key_value(s["isp"], isp)
    ds_mbps = connection_info.get("max_downstream_kbps", 0) // 1000 if connection_info.get("max_downstream_kbps") else "N/A"
    us_mbps = connection_info.get("max_upstream_kbps", 0) // 1000 if connection_info.get("max_upstream_kbps") else "N/A"
    pdf._key_value(s["tariff"], f"{ds_mbps} / {us_mbps} Mbit/s (Down / Up)")
    device = config.get("modem_type", connection_info.get("device_name", "Unknown"))
    pdf._key_value(s["modem"], device)

    pdf._key_value(
        s["report_period"], f"{report_start}  {s['period_to']}  {report_end}"
    )
    pdf._key_value(s["data_points"], str(len(snapshots)))
    pdf.ln(3)

    # --- Latest status recorded inside the report period ---
    pdf._section_title(s["section_latest_recorded_status"])
    if latest_snapshot:
        observed_at = _canonical_snapshot_timestamp(latest_snapshot.get("timestamp"))
        pdf._key_value(s["observed_at"], observed_at)
        sm = latest_snapshot["summary"]
        health = sm.get("health", "unknown")
        pdf.set_font("dejavu", "B", 12)
        r, g, b = pdf._health_color(health)
        pdf.set_text_color(r, g, b)
        pdf.cell(0, 8, f"{s['connection_health']}: {health.upper()}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        if sm.get("health_issues"):
            pdf.set_font("dejavu", "", 10)
            labels = s.get("issue_labels", {})
            translated = [labels.get(i, i) for i in sm["health_issues"]]
            pdf.multi_cell(0, 6, f"{s['issues']}: {', '.join(translated)}", align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # Latest recorded channel table
        pdf.set_font("dejavu", "B", 10)
        pdf.cell(0, 6, s["ds_channels"], new_x="LMARGIN", new_y="NEXT")
        cols = [s["col_ch"], s["col_freq"], s["col_power"], s["col_snr"], s["col_mod"], s["col_corr_err"], s["col_uncorr_err"], s["col_health"]]
        widths = [12, 25, 20, 18, 22, 25, 25, 20]
        pdf._table_header(cols, widths)
        for ch in latest_snapshot.get("ds_channels", []):
            pdf._table_row([
                ch.get("channel_id", ""),
                (ch.get("frequency") or "")[:10],
                _format_optional_decimal(ch.get('power')),
                _format_optional_decimal(ch.get("snr")),
                str(ch.get("modulation") or "")[:10],
                _format_optional_count(ch.get('correctable_errors')),
                _format_optional_count(ch.get('uncorrectable_errors')),
                ch.get("health", ""),
            ], widths, health=ch.get("health"))

        pdf.ln(3)
        pdf.set_font("dejavu", "B", 10)
        pdf.cell(0, 6, s["us_channels"], new_x="LMARGIN", new_y="NEXT")
        cols_us = [s["col_ch"], s["col_freq"], s["col_power"], s["col_mod"], s["col_multiplex"], s["col_health"]]
        widths_us = [15, 30, 25, 30, 35, 25]
        pdf._table_header(cols_us, widths_us)
        for ch in latest_snapshot.get("us_channels", []):
            pdf._table_row([
                ch.get("channel_id", ""),
                (ch.get("frequency") or "")[:12],
                _format_optional_decimal(ch.get('power')),
                str(ch.get("modulation") or "")[:12],
                str(ch.get("multiplex") or "")[:15],
                ch.get("health", ""),
            ], widths_us, health=ch.get("health"))

    else:
        pdf.set_font("dejavu", "", 10)
        pdf.multi_cell(
            0,
            6,
            s["no_docsis_data"],
            new_x="LMARGIN",
            new_y="NEXT",
        )

    # --- Diagnostic Notes ---
    if diag_notes:
        pdf.ln(4)
        pdf._section_title(s.get("section_diagnostic_notes", "Diagnostic Notes"))
        pdf.set_font("dejavu", "", 9)
        for note in diag_notes:
            text = _format_diagnostic_note(note, s)
            r, g, b = pdf._health_color("critical")
            pdf.set_text_color(r, g, b)
            pdf.multi_cell(0, 4, f"  {text}", new_x="LMARGIN", new_y="NEXT")
        if any(n.get("severity") == "extreme" for n in diag_notes):
            pdf.ln(2)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("dejavu", "I", 9)
            pdf.multi_cell(0, 4, s.get("diag_note_isp_hint", ""), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("dejavu", "", 10)

    comparison_section = _format_comparison_evidence(comparison_data, s)
    if comparison_section:
        pdf.ln(4)
        pdf._section_title(s.get("comparison_section_title", "Before/After Comparison"))
        pdf.set_font("dejavu", "", 9)
        pdf.multi_cell(0, 4, comparison_section)
        pdf.set_font("dejavu", "", 10)

    # --- Historical Analysis ---
    if snapshots:
        pdf.add_page()
        pdf._section_title(s["section_historical"])
        worst = _compute_worst_values(snapshots)

        pdf._key_value(s["total_measurements"], str(worst["total_snapshots"]))
        pdf._key_value(s["measurements_critical"], str(worst["health_critical_count"]), bold_value=True)
        pdf._key_value(s["measurements_marginal"], str(worst["health_marginal_count"]))
        pdf._key_value(s["measurements_tolerated"], str(worst["health_tolerated_count"]))
        pdf.ln(2)

        pdf.set_font("dejavu", "B", 10)
        pdf.cell(0, 6, s["worst_recorded"], new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dejavu", "", 10)

        warn = _default_warn_thresholds(worst["ds_snr_warn_min"])
        pdf._key_value(s["ds_power_worst"], f"{_format_optional_measurement(worst['ds_power_max'])} dBmV (threshold: {warn['ds_power']})")
        pdf._key_value(s["us_power_worst"], f"{_format_optional_measurement(worst['us_power_max'])} dBmV (threshold: {warn['us_power']})")
        pdf._key_value(s["ds_snr_worst"], f"{_format_optional_measurement(worst['ds_snr_min'])} dB (threshold: {warn['snr']})")
        pdf._key_value(s["uncorr_err_max"], _format_optional_count(worst["ds_uncorrectable_max"]))
        pdf._key_value(s["corr_err_max"], _format_optional_count(worst["ds_correctable_max"]))
        pdf.ln(3)

        # Worst channels
        ds_worst, us_worst = _find_worst_channels(snapshots)
        if ds_worst:
            pdf.set_font("dejavu", "B", 10)
            pdf.cell(0, 6, s["worst_ds_channels"], new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("dejavu", "", 9)
            for cid, count in ds_worst:
                pct = round(count / len(snapshots) * 100)
                pdf.cell(0, 5, f"  {s['channel_unhealthy'].format(cid=cid, count=count, total=len(snapshots), pct=pct)}", new_x="LMARGIN", new_y="NEXT")
        if us_worst:
            pdf.ln(2)
            pdf.set_font("dejavu", "B", 10)
            pdf.cell(0, 6, s["worst_us_channels"], new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("dejavu", "", 9)
            for cid, count in us_worst:
                pct = round(count / len(snapshots) * 100)
                pdf.cell(0, 5, f"  {s['channel_unhealthy'].format(cid=cid, count=count, total=len(snapshots), pct=pct)}", new_x="LMARGIN", new_y="NEXT")

    # --- Reference Thresholds ---
    pdf.add_page()
    pdf._section_title(s["section_thresholds"])
    pdf.set_font("dejavu", "", 9)
    cols_ref = [s["col_parameter"], s["col_modulation"], s["col_good"], s["col_tolerated"], s["col_critical_thresh"], s["col_reference"]]
    widths_ref = [28, 28, 35, 35, 35, 25]
    pdf._table_header(cols_ref, widths_ref)
    for row in _format_threshold_table():
        pdf._table_row([row["category"], row["variant"], row["good"], row["tolerated"], row["critical"], row["ref"]], widths_ref)
    pdf.ln(5)

    # --- ISP Complaint Template ---
    pdf._section_title(s["section_complaint"])
    pdf.set_font("dejavu", "", 9)

    diag_complaint = _format_diagnostic_complaint(diag_notes, s)
    comparison_section = _format_comparison_evidence(comparison_data, s)
    complaint_closing = _format_customer_closing(s, customer_name, customer_number, customer_address)

    if snapshots:
        worst = _compute_worst_values(snapshots)
        warn = _default_warn_thresholds(worst["ds_snr_warn_min"])
        start = report_start[:10]
        end = report_end[:10]
        poor_pct = round(worst['health_critical_count'] / max(worst['total_snapshots'], 1) * 100)
        complaint = (
            f"{s['complaint_subject']}\n\n"
            f"{s['complaint_greeting'].format(isp=isp)}\n\n"
            f"{s['complaint_body'].format(count=len(snapshots), start=start, end=end)}\n\n"
            f"{s['complaint_findings']}\n"
            f"- {s['complaint_poor_rate'].format(poor=worst['health_critical_count'], total=worst['total_snapshots'], pct=poor_pct)}\n"
            f"- {s['complaint_ds_power'].format(val=_format_optional_measurement(worst['ds_power_max']), thresh=warn['ds_power'])}\n"
            f"- {s['complaint_us_power'].format(val=_format_optional_measurement(worst['us_power_max']), thresh=warn['us_power'])}\n"
            f"- {s['complaint_snr'].format(val=_format_optional_measurement(worst['ds_snr_min']), thresh=warn['snr'])}\n"
            f"- {s['complaint_uncorr'].format(val=_format_optional_count(worst['ds_uncorrectable_max']))}\n\n"
            f"{diag_complaint}"
            f"{s['complaint_exceed']}\n\n"
            f"{s['complaint_request']}\n"
            f"1. {s['complaint_req1']}\n"
            f"2. {s['complaint_req2']}\n"
            f"3. {s['complaint_req3']}\n\n"
            f"{s['complaint_escalation']}\n\n"
            f"{complaint_closing}"
        )
    else:
        complaint = (
            f"{s['report_period']}: {report_start} {s['period_to']} {report_end}\n\n"
            f"{s['complaint_short_greeting']}\n\n"
            f"{s['complaint_no_docsis_data'].format(start=report_start, end=report_end)}\n\n"
            f"{complaint_closing}"
        )

    pdf.multi_cell(0, 4, complaint)

    # Output
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def generate_incident_report(incident, entries, snapshots, speedtests, bnetz_list,
                              config=None, connection_info=None, lang="en",
                              customer_name="", customer_number="", customer_address="",
                              attachment_loader=None):
    """Generate PDF complaint report scoped to a specific incident.

    Args:
        incident: Incident dict (name, status, description, start_date, end_date)
        entries: List of journal entry dicts (with attachment_count, attachments list)
        snapshots: List of snapshot dicts from storage.get_range_data()
        speedtests: List of speedtest result dicts
        bnetz_list: List of BNetzA measurement dicts
        config: Config dict (isp_name, modem_type)
        connection_info: Connection info dict
        lang: Language code
        customer_name: Customer name for the embedded complaint letter
        customer_number: Customer/contract number for the embedded complaint letter
        customer_address: Customer address for the embedded complaint letter
        attachment_loader: Optional callable(attachment_id) -> dict with 'data', 'mime_type'

    Returns:
        bytes: PDF file content
    """
    config = config or {}
    connection_info = connection_info or {}
    s = _get_report_strings(lang)
    pdf = IncidentReport(lang=lang)
    # Override the header title for incident reports
    pdf._s = dict(pdf._s)
    pdf._s["report_title"] = s["incident_report_title"]
    pdf._s["footer"] = s["incident_report_title"]
    pdf.alias_nb_pages()

    # ── Page 1: Incident Summary ──
    pdf.add_page()
    pdf._section_title(s["section_incident_summary"])

    pdf._key_value(s["incident_name"], incident.get("name", ""))
    status = incident.get("status", "open")
    pdf._key_value(s["incident_status"], status.upper(), bold_value=True)

    if incident.get("start_date"):
        start_str = incident["start_date"]
        end_str = incident.get("end_date") or ""
        period = start_str
        if end_str:
            period += f"  {s.get('period_to', 'to')}  {end_str}"
            try:
                d1 = datetime.strptime(start_str, "%Y-%m-%d")
                d2 = datetime.strptime(end_str, "%Y-%m-%d")
                days = (d2 - d1).days
                duration = s["incident_duration_days"].format(days=days)
            except ValueError:
                duration = ""
        else:
            period += f"  {s.get('period_to', 'to')}  ..."
            duration = s["incident_duration_ongoing"]
        pdf._key_value(s["incident_period"], period)
        if duration:
            pdf._key_value(s["incident_duration"], duration)

    if incident.get("description"):
        pdf.ln(2)
        pdf.set_font("dejavu", "", 10)
        pdf.multi_cell(0, 5, incident["description"])

    # Connection info
    pdf.ln(3)
    pdf._section_title(s["section_connection_info"])
    isp = config.get("isp_name", "Unknown ISP")
    pdf._key_value(s["isp"], isp)
    ds_mbps = connection_info.get("max_downstream_kbps", 0) // 1000 if connection_info.get("max_downstream_kbps") else "N/A"
    us_mbps = connection_info.get("max_upstream_kbps", 0) // 1000 if connection_info.get("max_upstream_kbps") else "N/A"
    pdf._key_value(s["tariff"], f"{ds_mbps} / {us_mbps} Mbit/s (Down / Up)")
    device = config.get("modem_type", connection_info.get("device_name", "Unknown"))
    pdf._key_value(s["modem"], device)

    # ── Page 2: Signal Analysis (if snapshots available) ──
    if snapshots:
        pdf.add_page()
        pdf._section_title(s["section_historical"])
        worst = _compute_worst_values(snapshots)

        pdf._key_value(s["total_measurements"], str(worst["total_snapshots"]))
        pdf._key_value(s["measurements_critical"], str(worst["health_critical_count"]), bold_value=True)
        pdf._key_value(s["measurements_marginal"], str(worst["health_marginal_count"]))
        pdf._key_value(s["measurements_tolerated"], str(worst["health_tolerated_count"]))
        pdf.ln(2)

        pdf.set_font("dejavu", "B", 10)
        pdf.cell(0, 6, s["worst_recorded"], new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dejavu", "", 10)

        warn = _default_warn_thresholds(worst["ds_snr_warn_min"])
        pdf._key_value(s["ds_power_worst"], f"{_format_optional_measurement(worst['ds_power_max'])} dBmV (threshold: {warn['ds_power']})")
        pdf._key_value(s["us_power_worst"], f"{_format_optional_measurement(worst['us_power_max'])} dBmV (threshold: {warn['us_power']})")
        pdf._key_value(s["ds_snr_worst"], f"{_format_optional_measurement(worst['ds_snr_min'])} dB (threshold: {warn['snr']})")
        pdf._key_value(s["uncorr_err_max"], _format_optional_count(worst["ds_uncorrectable_max"]))
        pdf._key_value(s["corr_err_max"], _format_optional_count(worst["ds_correctable_max"]))
        pdf.ln(3)

        # Worst channels
        ds_worst, us_worst = _find_worst_channels(snapshots)
        if ds_worst:
            pdf.set_font("dejavu", "B", 10)
            pdf.cell(0, 6, s["worst_ds_channels"], new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("dejavu", "", 9)
            for cid, count in ds_worst:
                pct = round(count / len(snapshots) * 100)
                pdf.cell(0, 5, f"  {s['channel_unhealthy'].format(cid=cid, count=count, total=len(snapshots), pct=pct)}", new_x="LMARGIN", new_y="NEXT")
        if us_worst:
            pdf.ln(2)
            pdf.set_font("dejavu", "B", 10)
            pdf.cell(0, 6, s["worst_us_channels"], new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("dejavu", "", 9)
            for cid, count in us_worst:
                pct = round(count / len(snapshots) * 100)
                pdf.cell(0, 5, f"  {s['channel_unhealthy'].format(cid=cid, count=count, total=len(snapshots), pct=pct)}", new_x="LMARGIN", new_y="NEXT")

    # ── Page 3: Speedtest Results (if available) ──
    if speedtests:
        pdf.add_page()
        pdf._section_title(s["section_speedtest"])

        cols = [s["speedtest_date"], s["speedtest_download"], s["speedtest_upload"], s["speedtest_ping"], "Jitter", "Loss"]
        widths = [35, 30, 30, 25, 25, 25]
        pdf._table_header(cols, widths)

        dl_vals = []
        ul_vals = []
        for st in speedtests:
            ts = st.get("timestamp", "")[:16].replace("T", " ")
            dl = st.get("download_mbps") or st.get("download_human", "")
            ul = st.get("upload_mbps") or st.get("upload_human", "")
            ping = st.get("ping_ms", "-")
            jitter = st.get("jitter_ms", "-")
            loss = st.get("packet_loss_pct", "-")
            dl_display = f"{dl}" if dl else "-"
            ul_display = f"{ul}" if ul else "-"
            pdf._table_row([ts, dl_display, ul_display, str(ping), str(jitter), f"{loss}%"], widths)
            try:
                dl_vals.append(float(dl) if dl else 0)
            except (ValueError, TypeError):
                pass
            try:
                ul_vals.append(float(ul) if ul else 0)
            except (ValueError, TypeError):
                pass

        # Summary
        if dl_vals or ul_vals:
            pdf.ln(3)
            pdf.set_font("dejavu", "B", 10)
            if dl_vals:
                avg_dl = round(sum(dl_vals) / len(dl_vals), 1)
                min_dl = round(min(dl_vals), 1)
                pdf._key_value(f"{s['speedtest_avg']} {s['speedtest_download']}", f"{avg_dl} Mbit/s")
                pdf._key_value(f"{s['speedtest_min']} {s['speedtest_download']}", f"{min_dl} Mbit/s")
            if ul_vals:
                avg_ul = round(sum(ul_vals) / len(ul_vals), 1)
                min_ul = round(min(ul_vals), 1)
                pdf._key_value(f"{s['speedtest_avg']} {s['speedtest_upload']}", f"{avg_ul} Mbit/s")
                pdf._key_value(f"{s['speedtest_min']} {s['speedtest_upload']}", f"{min_ul} Mbit/s")

    # ── Page 4: BNetzA Measurements (if available) ──
    if bnetz_list:
        pdf.add_page()
        pdf._section_title(s["section_bnetz"])

        has_deviation = False
        for m in bnetz_list:
            pdf.set_font("dejavu", "B", 10)
            pdf.cell(0, 6, f"{m.get('date', '')} - {m.get('tariff', '')} ({m.get('provider', '')})", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("dejavu", "", 9)

            dl_max = round(m.get("download_max_tariff") or 0)
            dl_avg = round(m.get("download_measured_avg") or 0)
            dl_pct = round(dl_avg / dl_max * 100) if dl_max else 0
            ul_max = round(m.get("upload_max_tariff") or 0)
            ul_avg = round(m.get("upload_measured_avg") or 0)
            ul_pct = round(ul_avg / ul_max * 100) if ul_max else 0

            pdf.cell(0, 5, f"  Download: {dl_avg} / {dl_max} Mbit/s ({dl_pct}%)", new_x="LMARGIN", new_y="NEXT")
            pdf.cell(0, 5, f"  Upload: {ul_avg} / {ul_max} Mbit/s ({ul_pct}%)", new_x="LMARGIN", new_y="NEXT")

            verdict_dl = m.get("verdict_download", "-")
            verdict_ul = m.get("verdict_upload", "-")
            pdf.cell(0, 5, f"  Verdict: DL {verdict_dl} / UL {verdict_ul}", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

            if verdict_dl == "deviation" or verdict_ul == "deviation":
                has_deviation = True

        if has_deviation:
            pdf.ln(2)
            pdf.set_font("dejavu", "B", 9)
            pdf.set_text_color(231, 76, 60)
            pdf.multi_cell(0, 4, s.get("complaint_bnetz_legal", ""))
            pdf.set_text_color(0, 0, 0)

    # ── Page 5: Journal Entries ──
    if entries:
        pdf.add_page()
        pdf._section_title(s["section_journal"])

        for entry in entries:
            pdf.set_font("dejavu", "B", 10)
            date_str = entry.get("date", "")
            title = entry.get("title", "")
            pdf.cell(0, 6, f"{date_str}  -  {title}", new_x="LMARGIN", new_y="NEXT")

            desc = entry.get("description", "")
            if desc:
                if len(desc) > 500:
                    desc = desc[:500] + "..."
                pdf.set_font("dejavu", "", 9)
                pdf.multi_cell(0, 4, desc)

            att_count = entry.get("attachment_count", 0)
            if att_count:
                pdf.set_font("dejavu", "I", 8)
                pdf.set_text_color(128, 128, 128)
                pdf.cell(0, 4, s["journal_attachments"].format(count=att_count), new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)

            # Embed image attachments if loader provided
            if attachment_loader and entry.get("attachments"):
                for att_meta in entry["attachments"]:
                    mime = att_meta.get("mime_type", "")
                    if mime not in ("image/jpeg", "image/png"):
                        continue
                    try:
                        att = attachment_loader(att_meta["id"])
                        if not att or len(att.get("data", b"")) > 500 * 1024:
                            continue
                        img_buf = io.BytesIO(att["data"])
                        ext = "jpeg" if "jpeg" in mime else "png"
                        # Check remaining page space
                        if pdf.get_y() > 220:
                            pdf.add_page()
                        pdf.image(img_buf, x=pdf.l_margin, w=min(170, pdf.epw), type=ext)
                        pdf.ln(3)
                    except Exception:
                        log.warning("Failed to embed attachment %d in incident report", att_meta.get("id", 0))

            pdf.ln(3)

    # ── Last Page: Complaint Template ──
    pdf.add_page()
    pdf._section_title(s["section_complaint"])
    pdf.set_font("dejavu", "", 9)

    if snapshots:
        worst = _compute_worst_values(snapshots)
        warn = _default_warn_thresholds(worst["ds_snr_warn_min"])
        start = snapshots[0]["timestamp"][:10]
        end = snapshots[-1]["timestamp"][:10]
        poor_pct = round(worst['health_critical_count'] / max(worst['total_snapshots'], 1) * 100)
        complaint = (
            f"{s['complaint_subject']}\n\n"
            f"{s['complaint_greeting'].format(isp=isp)}\n\n"
            f"{s['complaint_body'].format(count=len(snapshots), start=start, end=end)}\n\n"
            f"{s['complaint_findings']}\n"
            f"- {s['complaint_poor_rate'].format(poor=worst['health_critical_count'], total=worst['total_snapshots'], pct=poor_pct)}\n"
            f"- {s['complaint_ds_power'].format(val=_format_optional_measurement(worst['ds_power_max']), thresh=warn['ds_power'])}\n"
            f"- {s['complaint_us_power'].format(val=_format_optional_measurement(worst['us_power_max']), thresh=warn['us_power'])}\n"
            f"- {s['complaint_snr'].format(val=_format_optional_measurement(worst['ds_snr_min']), thresh=warn['snr'])}\n"
            f"- {s['complaint_uncorr'].format(val=_format_optional_count(worst['ds_uncorrectable_max']))}\n\n"
            f"{s['complaint_exceed']}\n\n"
            f"{s['complaint_request']}\n"
            f"1. {s['complaint_req1']}\n"
            f"2. {s['complaint_req2']}\n"
            f"3. {s['complaint_req3']}\n\n"
        )
    else:
        complaint = (
            f"{s['complaint_short_subject']}\n\n"
            f"{s['complaint_short_greeting']}\n\n"
            f"{s['complaint_short_body']}\n\n"
        )

    # Add BNetzA reference if measurements exist
    if bnetz_list:
        # Pick best measurement (prefer deviation)
        bnetz_data = None
        for m in reversed(bnetz_list):
            if m.get("verdict_download") == "deviation" or m.get("verdict_upload") == "deviation":
                bnetz_data = m
                break
        if not bnetz_data:
            bnetz_data = bnetz_list[-1]

        dl_max = round(bnetz_data.get("download_max_tariff") or 0)
        dl_avg = round(bnetz_data.get("download_measured_avg") or 0)
        dl_pct = round(dl_avg / dl_max * 100) if dl_max else 0
        ul_max = round(bnetz_data.get("upload_max_tariff") or 0)
        ul_avg = round(bnetz_data.get("upload_measured_avg") or 0)
        ul_pct = round(ul_avg / ul_max * 100) if ul_max else 0

        complaint += (
            f"\n{s.get('complaint_bnetz_header', '')}\n\n"
            f"{s.get('complaint_bnetz_body', '').format(date=bnetz_data.get('date', ''))}\n"
            f"- {s.get('complaint_bnetz_dl', '').format(max=dl_max, avg=dl_avg, pct=dl_pct)}\n"
            f"- {s.get('complaint_bnetz_ul', '').format(max=ul_max, avg=ul_avg, pct=ul_pct)}\n"
            f"- {s.get('complaint_bnetz_verdict', '').format(verdict_dl=bnetz_data.get('verdict_download', '-'), verdict_ul=bnetz_data.get('verdict_upload', '-'))}\n\n"
        )
        has_dev = bnetz_data.get("verdict_download") == "deviation" or bnetz_data.get("verdict_upload") == "deviation"
        if has_dev:
            complaint += s.get("complaint_bnetz_legal", "") + "\n\n"

    complaint_closing = _format_customer_closing(s, customer_name, customer_number, customer_address)
    complaint += f"{s['complaint_escalation']}\n\n{complaint_closing}"

    pdf.multi_cell(0, 4, complaint)

    # Output
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def generate_complaint_text(snapshots, config=None, connection_info=None, lang="en",
                            customer_name="", customer_number="", customer_address="",
                            bnetz_data=None, current_analysis=None, comparison_data=None,
                            report_start=None, report_end=None):
    """Generate ISP complaint letter as plain text.

    Args:
        snapshots: List of snapshot dicts
        config: Config dict (isp_name, etc.)
        connection_info: Connection info dict
        lang: Language code
        customer_name: Customer name for letter
        customer_number: Customer/contract number
        customer_address: Customer address
        bnetz_data: Optional BNetzA measurement dict
        current_analysis: Deprecated compatibility argument; not used for reports
        comparison_data: Optional before/after comparison payload
        report_start: Requested inclusive report-window start in canonical UTC
        report_end: Requested inclusive report-window end in canonical UTC

    Returns:
        str: Complaint letter text
    """
    config = config or {}
    s = _get_report_strings(lang)
    isp = config.get("isp_name", "Unknown ISP")
    report_start, report_end = _report_bounds(snapshots, report_start, report_end)
    historical = derive_historical_report_data(snapshots)
    diag_notes = historical["diagnostic_notes"]

    # Build closing with actual customer data
    closing_lines = []
    closing_lines.append(s.get("complaint_closing_label", "Sincerely,"))
    closing_lines.append(customer_name if customer_name else "[Your Name]")
    if customer_number:
        closing_lines.append(customer_number)
    else:
        closing_lines.append("[Customer Number]")
    if customer_address:
        closing_lines.append(customer_address)
    else:
        closing_lines.append("[Address]")
    closing = "\n".join(closing_lines)

    # Build BNetzA section if data provided
    bnetz_section = ""
    if bnetz_data:
        has_deviation = (
            bnetz_data.get("verdict_download") == "deviation"
            or bnetz_data.get("verdict_upload") == "deviation"
        )
        dl_max = round(bnetz_data.get("download_max_tariff") or 0)
        dl_avg = round(bnetz_data.get("download_measured_avg") or 0)
        dl_pct = round(dl_avg / dl_max * 100) if dl_max else 0
        ul_max = round(bnetz_data.get("upload_max_tariff") or 0)
        ul_avg = round(bnetz_data.get("upload_measured_avg") or 0)
        ul_pct = round(ul_avg / ul_max * 100) if ul_max else 0
        bnetz_lines = [
            s.get("complaint_bnetz_header", ""),
            "",
            s.get("complaint_bnetz_body", "").format(date=bnetz_data.get("date", "")),
            "",
            f"- {s.get('complaint_bnetz_tariff', '').format(tariff=bnetz_data.get('tariff', '-'), provider=bnetz_data.get('provider', '-'))}",
            f"- {s.get('complaint_bnetz_dl', '').format(max=dl_max, avg=dl_avg, pct=dl_pct)}",
            f"- {s.get('complaint_bnetz_ul', '').format(max=ul_max, avg=ul_avg, pct=ul_pct)}",
            f"- {s.get('complaint_bnetz_verdict', '').format(verdict_dl=bnetz_data.get('verdict_download', '-'), verdict_ul=bnetz_data.get('verdict_upload', '-'))}",
        ]
        if has_deviation:
            bnetz_lines.append("")
            bnetz_lines.append(s.get("complaint_bnetz_legal", ""))
        bnetz_section = "\n".join(bnetz_lines) + "\n\n"

    diag_complaint = _format_diagnostic_complaint(diag_notes, s)
    comparison_section = _format_comparison_evidence(comparison_data, s)

    if snapshots:
        worst = _compute_worst_values(snapshots)
        warn = _default_warn_thresholds(worst["ds_snr_warn_min"])
        start = report_start[:10]
        end = report_end[:10]
        poor_pct = round(worst['health_critical_count'] / max(worst['total_snapshots'], 1) * 100)
        return (
            f"{s['complaint_subject']}\n\n"
            f"{s['complaint_greeting'].format(isp=isp)}\n\n"
            f"{s['complaint_body'].format(count=len(snapshots), start=start, end=end)}\n\n"
            f"{s['complaint_findings']}\n"
            f"- {s['complaint_poor_rate'].format(poor=worst['health_critical_count'], total=worst['total_snapshots'], pct=poor_pct)}\n"
            f"- {s['complaint_ds_power'].format(val=_format_optional_measurement(worst['ds_power_max']), thresh=warn['ds_power'])}\n"
            f"- {s['complaint_us_power'].format(val=_format_optional_measurement(worst['us_power_max']), thresh=warn['us_power'])}\n"
            f"- {s['complaint_snr'].format(val=_format_optional_measurement(worst['ds_snr_min']), thresh=warn['snr'])}\n"
            f"- {s['complaint_uncorr'].format(val=_format_optional_count(worst['ds_uncorrectable_max']))}\n\n"
            f"{diag_complaint}"
            f"{comparison_section}"
            f"{bnetz_section}"
            f"{s['complaint_exceed']}\n\n"
            f"{s['complaint_request']}\n"
            f"1. {s['complaint_req1']}\n"
            f"2. {s['complaint_req2']}\n"
            f"3. {s['complaint_req3']}\n\n"
            f"{s['complaint_escalation']}\n\n"
            f"{closing}"
        )
    elif bnetz_section:
        # No DOCSIS snapshots but BNetzA data available
        return (
            f"{s['report_period']}: {report_start} {s['period_to']} {report_end}\n\n"
            f"{s['complaint_greeting'].format(isp=isp)}\n\n"
            f"{s['complaint_no_docsis_data'].format(start=report_start, end=report_end)}\n\n"
            f"{bnetz_section}"
            f"{s['complaint_request']}\n"
            f"1. {s['complaint_req1']}\n"
            f"2. {s['complaint_req2']}\n"
            f"3. {s['complaint_req3']}\n\n"
            f"{s['complaint_escalation']}\n\n"
            f"{closing}"
        )
    else:
        return (
            f"{s['report_period']}: {report_start} {s['period_to']} {report_end}\n\n"
            f"{s['complaint_short_greeting']}\n\n"
            f"{s['complaint_no_docsis_data'].format(start=report_start, end=report_end)}\n\n"
            f"{closing}"
        )
