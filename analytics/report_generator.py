"""
MotorWatch IQ — Report Generator
==================================
Generates a bilingual (EN/ES) HTML maintenance report from detection results
and alert manager state.

Auto-trigger conditions (any one sufficient):
  - Any motor at CRITICAL for > 60 seconds
  - Any motor at WARNING for > 60 seconds
  - Any motor with trend slope > 0.5 mm/s/h
  - ML anomaly score > 0.7

Manual trigger:
  generator.generate(results, manager)  → returns Path to HTML file

Output:
  reports/motorwatch_report_YYYYMMDD_HHMMSS.html
  reports/motorwatch_report_latest.html  (always overwritten)

Usage (integrated in anomaly_detector.py main loop):
  from analytics.report_generator import ReportGenerator
  generator = ReportGenerator()
  ...
  path = generator.maybe_generate(results, manager)
  if path:
      logger.info("Report generated → %s", path)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on path when run standalone
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

logger = logging.getLogger("report_generator")

# ── alert level constants (mirrors anomaly_detector) ─────────────────────────
ALERT_OK       = 0
ALERT_WATCH    = 1
ALERT_WARNING  = 2
ALERT_CRITICAL = 3

# ── trigger thresholds ────────────────────────────────────────────────────────
TRIGGER_DURATION_S  = 60    # seconds at WARNING+ before auto-report
TRIGGER_SLOPE       = 0.5   # mm/s/h trend threshold
TRIGGER_ML_SCORE    = 0.7   # ML anomaly score threshold

# ── output directory ──────────────────────────────────────────────────────────
REPORTS_DIR = _ROOT / "reports"


# ─────────────────────────────────────────────────────────────────────────────
# Translations
# ─────────────────────────────────────────────────────────────────────────────

T = {
    "title": {
        "en": "MotorWatch IQ — Maintenance Report",
        "es": "MotorWatch IQ — Informe de Mantenimiento",
    },
    "generated": {
        "en": "Generated",
        "es": "Generado",
    },
    "executive_summary": {
        "en": "Executive Summary",
        "es": "Resumen Ejecutivo",
    },
    "intervention_plan": {
        "en": "Intervention Plan",
        "es": "Plan de Intervención",
    },
    "alert_history": {
        "en": "Alert History",
        "es": "Historial de Alertas",
    },
    "technical_details": {
        "en": "Technical Details",
        "es": "Detalles Técnicos",
    },
    "motor": {
        "en": "Motor",
        "es": "Motor",
    },
    "status": {
        "en": "Status",
        "es": "Estado",
    },
    "anomaly_score": {
        "en": "Anomaly Score",
        "es": "Puntuación de Anomalía",
    },
    "trend": {
        "en": "Trend",
        "es": "Tendencia",
    },
    "eta_prealarm": {
        "en": "ETA Pre-alarm",
        "es": "ETA Pre-alarma",
    },
    "last_vrms": {
        "en": "v-RMS (mm/s)",
        "es": "v-RMS (mm/s)",
    },
    "last_apeak": {
        "en": "a-Peak (mg)",
        "es": "a-Pico (mg)",
    },
    "last_temp": {
        "en": "Temp (°C)",
        "es": "Temp (°C)",
    },
    "duration": {
        "en": "Duration",
        "es": "Duración",
    },
    "cause": {
        "en": "Cause",
        "es": "Causa",
    },
    "action": {
        "en": "Action",
        "es": "Acción",
    },
    "priority": {
        "en": "Priority",
        "es": "Prioridad",
    },
    "immediate": {
        "en": "IMMEDIATE",
        "es": "INMEDIATA",
    },
    "planned_48h": {
        "en": "PLANNED < 48h",
        "es": "PLANIFICADA < 48h",
    },
    "preventive": {
        "en": "PREVENTIVE",
        "es": "PREVENTIVA",
    },
    "no_alerts": {
        "en": "No active alerts",
        "es": "Sin alertas activas",
    },
    "normal_op": {
        "en": "Normal operation",
        "es": "Operación normal",
    },
    "all_normal": {
        "en": "All motors operating within ISO 20816-3 limits.",
        "es": "Todos los motores operando dentro de los límites ISO 20816-3.",
    },
    "iso_ref": {
        "en": "Thresholds per ISO 20816-3 — Group 2 machines (15–75 kW)",
        "es": "Umbrales según ISO 20816-3 — Grupo 2 (15–75 kW)",
    },
    "switch_lang": {
        "en": "Español",
        "es": "English",
    },
    "event": {
        "en": "Event",
        "es": "Evento",
    },
    "time": {
        "en": "Time",
        "es": "Hora",
    },
    "na": {
        "en": "N/A",
        "es": "N/D",
    },
}

# ── fault-specific intervention recommendations ───────────────────────────────

INTERVENTIONS = {
    "temp_critical": {
        "en": (
            "CRITICAL temperature detected. Immediately check: "
            "(1) cooling fan operation and airflow, "
            "(2) lubrication level and quality, "
            "(3) motor load vs rated capacity, "
            "(4) ambient temperature and ventilation."
        ),
        "es": (
            "Temperatura CRÍTICA detectada. Verificar de inmediato: "
            "(1) funcionamiento del ventilador y circulación de aire, "
            "(2) nivel y calidad de lubricación, "
            "(3) carga del motor vs capacidad nominal, "
            "(4) temperatura ambiente y ventilación."
        ),
        "priority": "immediate",
    },
    "temp_prealarm": {
        "en": (
            "Temperature approaching alarm threshold. Plan within 48h: "
            "inspect cooling system, check lubrication, verify load conditions."
        ),
        "es": (
            "Temperatura aproximándose al umbral de alarma. Planificar en 48h: "
            "inspeccionar sistema de refrigeración, verificar lubricación y condiciones de carga."
        ),
        "priority": "planned_48h",
    },
    "vrms_critical": {
        "en": (
            "CRITICAL vibration (v-RMS) detected. Immediately: "
            "(1) stop motor if safe to do so, "
            "(2) inspect bearings for wear, pitting or contamination, "
            "(3) check shaft alignment and coupling condition, "
            "(4) verify rotor balance."
        ),
        "es": (
            "Vibración CRÍTICA (v-RMS) detectada. De inmediato: "
            "(1) parar el motor si es seguro hacerlo, "
            "(2) inspeccionar rodamientos por desgaste, picaduras o contaminación, "
            "(3) verificar alineación del eje y estado del acoplamiento, "
            "(4) comprobar balance del rotor."
        ),
        "priority": "immediate",
    },
    "vrms_prealarm": {
        "en": (
            "Vibration approaching alarm threshold. Plan within 48h: "
            "bearing inspection, alignment check, coupling inspection."
        ),
        "es": (
            "Vibración aproximándose al umbral de alarma. Planificar en 48h: "
            "inspección de rodamientos, verificación de alineación e inspección del acoplamiento."
        ),
        "priority": "planned_48h",
    },
    "apeak_critical": {
        "en": (
            "CRITICAL impact (a-Peak) detected. Immediately: "
            "(1) inspect mechanical coupling and flexible elements, "
            "(2) check for loose fasteners or mechanical clearances, "
            "(3) inspect driven equipment for damage or blockage."
        ),
        "es": (
            "Impacto CRÍTICO (a-Pico) detectado. De inmediato: "
            "(1) inspeccionar acoplamiento mecánico y elementos flexibles, "
            "(2) verificar tornillería suelta o holguras mecánicas, "
            "(3) inspeccionar equipo accionado por daños u obstrucciones."
        ),
        "priority": "immediate",
    },
    "apeak_prealarm": {
        "en": (
            "Impact level approaching alarm threshold. Plan within 48h: "
            "coupling inspection, fastener torque check, driven equipment inspection."
        ),
        "es": (
            "Nivel de impacto aproximándose al umbral de alarma. Planificar en 48h: "
            "inspección del acoplamiento, verificación de par de tornillos e inspección del equipo accionado."
        ),
        "priority": "planned_48h",
    },
    "ml_anomaly": {
        "en": (
            "ML model detected abnormal vibration pattern not yet reflected in ISO thresholds. "
            "Recommended preventive inspection: bearing condition, lubrication, alignment."
        ),
        "es": (
            "El modelo ML detectó un patrón de vibración anormal no reflejado aún en los umbrales ISO. "
            "Inspección preventiva recomendada: estado de rodamientos, lubricación, alineación."
        ),
        "priority": "preventive",
    },
    "trend_fast": {
        "en": (
            "Rapid vibration degradation trend detected. "
            "Schedule inspection before estimated threshold crossing."
        ),
        "es": (
            "Tendencia de degradación de vibración acelerada detectada. "
            "Programar inspección antes del cruce de umbral estimado."
        ),
        "priority": "planned_48h",
    },
}

PRIORITY_ORDER = {"immediate": 0, "planned_48h": 1, "preventive": 2}


# ─────────────────────────────────────────────────────────────────────────────
# Intervention builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_interventions(results: list, manager) -> list[dict]:
    """
    Build prioritised intervention list from detection results and alert states.
    Returns list of dicts with keys: motor_id, key, priority, en, es.
    """
    items = []
    seen  = set()  # avoid duplicates per motor

    states = {s.motor_id: s for s in manager.all_states()}

    for r in results:
        mid   = str(r.motor_id)
        state = states.get(mid)
        level = r.alert_level

        if level == ALERT_OK:
            continue

        # Determine which faults are active from message
        msg = r.alert_message.lower()

        def _add(key: str):
            uid = f"{mid}_{key}"
            if uid not in seen:
                seen.add(uid)
                items.append({
                    "motor_id": mid,
                    "key": key,
                    "priority": INTERVENTIONS[key]["priority"],
                    "en": INTERVENTIONS[key]["en"],
                    "es": INTERVENTIONS[key]["es"],
                    "duration": state.duration_str if state else "",
                })

        if "temp" in msg and "≥ alarm" in msg:
            _add("temp_critical")
        elif "temp" in msg and "≥ prealarm" in msg:
            _add("temp_prealarm")

        if "v-rms" in msg and "≥ alarm" in msg:
            _add("vrms_critical")
        elif "v-rms" in msg and "≥ prealarm" in msg:
            _add("vrms_prealarm")

        if "a-peak" in msg and "≥ alarm" in msg:
            _add("apeak_critical")
        elif "a-peak" in msg and "≥ prealarm" in msg:
            _add("apeak_prealarm")

        if "ml score" in msg and r.anomaly_score >= TRIGGER_ML_SCORE:
            _add("ml_anomaly")

        if "trend" in msg and r.trend_slope > TRIGGER_SLOPE:
            _add("trend_fast")

    items.sort(key=lambda x: (PRIORITY_ORDER[x["priority"]], x["motor_id"]))
    return items


# ─────────────────────────────────────────────────────────────────────────────
# HTML builder
# ─────────────────────────────────────────────────────────────────────────────

def _level_badge(level: int, lang: str) -> str:
    labels = {
        ALERT_OK:       ("OK",       "#a6e3a1", "#1e1e2e"),
        ALERT_WATCH:    ("WATCH",    "#f9e2af", "#1e1e2e"),
        ALERT_WARNING:  ("WARNING",  "#fab387", "#1e1e2e"),
        ALERT_CRITICAL: ("CRITICAL", "#f38ba8", "#1e1e2e"),
    }
    text, bg, fg = labels.get(level, ("?", "#cdd6f4", "#1e1e2e"))
    return f'<span class="badge" style="background:{bg};color:{fg}">{text}</span>'


def _fmt_eta(eta: float, lang: str) -> str:
    if eta < 0:
        return T["na"][lang]
    return f"{eta:.1f}h"


def _fmt_slope(slope: float) -> str:
    if abs(slope) < 0.05:
        return "→ stable"
    arrow = "↑" if slope > 0 else "↓"
    return f"{arrow} {slope:+.2f} mm/s/h"


def _priority_badge(priority: str, lang: str) -> str:
    styles = {
        "immediate":  ("f38ba8", T["immediate"][lang]),
        "planned_48h": ("f9e2af", T["planned_48h"][lang]),
        "preventive": ("94e2d5", T["preventive"][lang]),
    }
    color, label = styles.get(priority, ("cdd6f4", priority))
    return f'<span class="badge" style="background:#{color};color:#1e1e2e">{label}</span>'


def _build_html(results: list, manager, generated_at: datetime) -> str:
    interventions = _build_interventions(results, manager)
    states        = {s.motor_id: s for s in manager.all_states()}
    history       = manager.history(limit=20)
    ts_str        = generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── per-motor rows ────────────────────────────────────────────────────────
    def motor_rows(lang: str) -> str:
        rows = []
        for r in sorted(results, key=lambda x: x.motor_id):
            mid   = str(r.motor_id)
            state = states.get(mid)
            dur   = state.duration_str if state and state.is_active else "—"
            rows.append(f"""
            <tr>
              <td><strong>Motor {mid}</strong></td>
              <td>{_level_badge(r.alert_level, lang)}</td>
              <td>{r.anomaly_score:.3f}</td>
              <td>{_fmt_slope(r.trend_slope)}</td>
              <td>{_fmt_eta(r.eta_prealarm_h, lang)}</td>
              <td>{r.last_vrms:.2f}</td>
              <td>{r.last_apeak:.0f}</td>
              <td>{r.last_temp:.1f}</td>
              <td>{dur}</td>
            </tr>""")
        return "\n".join(rows)

    # ── intervention rows ─────────────────────────────────────────────────────
    def intervention_rows(lang: str) -> str:
        if not interventions:
            return f'<tr><td colspan="4" style="text-align:center;color:#a6adc8">{T["all_normal"][lang]}</td></tr>'
        rows = []
        for i, item in enumerate(interventions, 1):
            rows.append(f"""
            <tr>
              <td>{i}. Motor {item['motor_id']}</td>
              <td>{_priority_badge(item['priority'], lang)}</td>
              <td class="intervention-text">{item[lang]}</td>
              <td>{item['duration']}</td>
            </tr>""")
        return "\n".join(rows)

    # ── alert history rows ────────────────────────────────────────────────────
    def history_rows(lang: str) -> str:
        if not history:
            return f'<tr><td colspan="3" style="text-align:center;color:#a6adc8">{T["no_alerts"][lang]}</td></tr>'
        rows = []
        for e in reversed(history):
            t = e.timestamp.strftime("%H:%M:%S")
            rows.append(f"""
            <tr>
              <td>{t}</td>
              <td>Motor {e.motor_id} — {e.event_type.value.upper()}</td>
              <td>{e.message}</td>
            </tr>""")
        return "\n".join(rows)

    # ── active alert count for header ─────────────────────────────────────────
    n_critical = sum(1 for r in results if r.alert_level == ALERT_CRITICAL)
    n_warning  = sum(1 for r in results if r.alert_level == ALERT_WARNING)
    n_watch    = sum(1 for r in results if r.alert_level == ALERT_WATCH)

    def header_status(lang: str) -> str:
        parts = []
        if n_critical:
            parts.append(f'<span class="badge" style="background:#f38ba8;color:#1e1e2e">🔴 {n_critical} CRITICAL</span>')
        if n_warning:
            parts.append(f'<span class="badge" style="background:#fab387;color:#1e1e2e">🟠 {n_warning} WARNING</span>')
        if n_watch:
            parts.append(f'<span class="badge" style="background:#f9e2af;color:#1e1e2e">👁 {n_watch} WATCH</span>')
        if not parts:
            parts.append(f'<span class="badge" style="background:#a6e3a1;color:#1e1e2e">✅ ALL OK</span>')
        return " ".join(parts)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MotorWatch IQ — Maintenance Report</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

  :root {{
    --base:     #1e1e2e;
    --mantle:   #181825;
    --crust:    #11111b;
    --surface0: #313244;
    --surface1: #45475a;
    --surface2: #585b70;
    --overlay0: #6c7086;
    --text:     #cdd6f4;
    --subtext:  #a6adc8;
    --red:      #f38ba8;
    --yellow:   #f9e2af;
    --green:    #a6e3a1;
    --blue:     #89b4fa;
    --mauve:    #cba6f7;
    --teal:     #94e2d5;
    --peach:    #fab387;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--crust);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }}

  /* ── Header ── */
  .header {{
    background: var(--mantle);
    border-bottom: 2px solid var(--surface0);
    padding: 24px 40px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
  }}

  .header-left h1 {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 20px;
    font-weight: 600;
    color: var(--blue);
    letter-spacing: 0.5px;
  }}

  .header-left .subtitle {{
    font-size: 12px;
    color: var(--subtext);
    margin-top: 4px;
    font-family: 'IBM Plex Mono', monospace;
  }}

  .header-right {{
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }}

  .lang-btn {{
    background: var(--surface0);
    color: var(--text);
    border: 1px solid var(--surface1);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    transition: background 0.2s;
  }}
  .lang-btn:hover {{ background: var(--surface1); }}

  /* ── Main layout ── */
  .container {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 32px 40px;
    display: flex;
    flex-direction: column;
    gap: 32px;
  }}

  /* ── Section ── */
  .section {{
    background: var(--base);
    border: 1px solid var(--surface0);
    border-radius: 8px;
    overflow: hidden;
  }}

  .section-header {{
    background: var(--mantle);
    padding: 12px 20px;
    border-bottom: 1px solid var(--surface0);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    font-weight: 600;
    color: var(--mauve);
    letter-spacing: 1px;
    text-transform: uppercase;
  }}

  .section-body {{ padding: 20px; }}

  /* ── Table ── */
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}

  th {{
    background: var(--surface0);
    color: var(--subtext);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid var(--surface1);
  }}

  td {{
    padding: 10px 12px;
    border-bottom: 1px solid var(--surface0);
    vertical-align: top;
  }}

  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--mantle); }}

  /* ── Badge ── */
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 3px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }}

  /* ── Intervention text ── */
  .intervention-text {{
    color: var(--subtext);
    font-size: 12px;
    line-height: 1.5;
    max-width: 500px;
  }}

  /* ── Footer ── */
  .footer {{
    text-align: center;
    color: var(--overlay0);
    font-size: 11px;
    font-family: 'IBM Plex Mono', monospace;
    padding: 24px;
    border-top: 1px solid var(--surface0);
  }}

  /* ── Language visibility ── */
  span.lang-en, span.lang-es {{ display: none; }}
  body.lang-en span.lang-en {{ display: inline; }}
  body.lang-es span.lang-es {{ display: inline; }}
  table.lang-en, table.lang-es {{ display: none; }}
  body.lang-en table.lang-en {{ display: table; }}
  body.lang-es table.lang-es {{ display: table; }}
</style>
</head>
<body class="lang-en">

<div class="header">
  <div class="header-left">
    <h1>⚙ MotorWatch IQ</h1>
    <div class="subtitle">
      <span class="lang-en">{T['generated']['en']}: {ts_str}</span>
      <span class="lang-es">{T['generated']['es']}: {ts_str}</span>
    </div>
  </div>
  <div class="header-right">
    {header_status('en')}
    <button class="lang-btn" onclick="toggleLang()" id="lang-btn">Español</button>
  </div>
</div>

<div class="container">

  <!-- Executive Summary -->
  <div class="section">
    <div class="section-header">
      <span class="lang-en">{T['executive_summary']['en']}</span>
      <span class="lang-es">{T['executive_summary']['es']}</span>
    </div>
    <div class="section-body">
      <table class="lang-en">
        <thead>
          <tr>
            <th>{T['motor']['en']}</th>
            <th>{T['status']['en']}</th>
            <th>{T['anomaly_score']['en']}</th>
            <th>{T['trend']['en']}</th>
            <th>{T['eta_prealarm']['en']}</th>
            <th>{T['last_vrms']['en']}</th>
            <th>{T['last_apeak']['en']}</th>
            <th>{T['last_temp']['en']}</th>
            <th>{T['duration']['en']}</th>
          </tr>
        </thead>
        <tbody>{motor_rows('en')}</tbody>
      </table>
      <table class="lang-es">
        <thead>
          <tr>
            <th>{T['motor']['es']}</th>
            <th>{T['status']['es']}</th>
            <th>{T['anomaly_score']['es']}</th>
            <th>{T['trend']['es']}</th>
            <th>{T['eta_prealarm']['es']}</th>
            <th>{T['last_vrms']['es']}</th>
            <th>{T['last_apeak']['es']}</th>
            <th>{T['last_temp']['es']}</th>
            <th>{T['duration']['es']}</th>
          </tr>
        </thead>
        <tbody>{motor_rows('es')}</tbody>
      </table>
    </div>
  </div>

  <!-- Intervention Plan -->
  <div class="section">
    <div class="section-header">
      <span class="lang-en">{T['intervention_plan']['en']}</span>
      <span class="lang-es">{T['intervention_plan']['es']}</span>
    </div>
    <div class="section-body">
      <table class="lang-en">
        <thead>
          <tr>
            <th>{T['motor']['en']}</th>
            <th>{T['priority']['en']}</th>
            <th>{T['action']['en']}</th>
            <th>{T['duration']['en']}</th>
          </tr>
        </thead>
        <tbody>{intervention_rows('en')}</tbody>
      </table>
      <table class="lang-es">
        <thead>
          <tr>
            <th>{T['motor']['es']}</th>
            <th>{T['priority']['es']}</th>
            <th>{T['action']['es']}</th>
            <th>{T['duration']['es']}</th>
          </tr>
        </thead>
        <tbody>{intervention_rows('es')}</tbody>
      </table>
    </div>
  </div>

  <!-- Alert History -->
  <div class="section">
    <div class="section-header">
      <span class="lang-en">{T['alert_history']['en']}</span>
      <span class="lang-es">{T['alert_history']['es']}</span>
    </div>
    <div class="section-body">
      <table class="lang-en">
        <thead>
          <tr>
            <th>{T['time']['en']}</th>
            <th>{T['event']['en']}</th>
            <th>{T['cause']['en']}</th>
          </tr>
        </thead>
        <tbody>{history_rows('en')}</tbody>
      </table>
      <table class="lang-es">
        <thead>
          <tr>
            <th>{T['time']['es']}</th>
            <th>{T['event']['es']}</th>
            <th>{T['cause']['es']}</th>
          </tr>
        </thead>
        <tbody>{history_rows('es')}</tbody>
      </table>
    </div>
  </div>

</div>

<div class="footer">
  MotorWatch IQ · IFM VVB306 · Siemens S7-1200 · {T['iso_ref']['en']} / {T['iso_ref']['es']}
</div>

<script>
  function toggleLang() {{
    const body = document.body;
    const btn  = document.getElementById('lang-btn');
    if (body.classList.contains('lang-en')) {{
      body.classList.replace('lang-en', 'lang-es');
      btn.textContent = 'English';
    }} else {{
      body.classList.replace('lang-es', 'lang-en');
      btn.textContent = 'Español';
    }}
  }}
</script>

</body>
</html>"""

    return html


# ─────────────────────────────────────────────────────────────────────────────
# ReportGenerator
# ─────────────────────────────────────────────────────────────────────────────

class ReportGenerator:

    COOLDOWN_S = 300   # mínimo 5 minutos entre relatórios auto-gerados

    def __init__(self):
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self._last_generated: datetime | None = None

    def should_generate(self, results: list, manager) -> bool:
        """Return True if any trigger condition is met AND cooldown has elapsed."""
        # Cooldown — evita gerar relatório a cada ciclo de 30s
        if self._last_generated is not None:
            elapsed = (datetime.now(timezone.utc) - self._last_generated).total_seconds()
            if elapsed < self.COOLDOWN_S:
                return False

        for r in results:
            state = next((s for s in manager.all_states() if s.motor_id == str(r.motor_id)), None)

            # Alarm/warning sustained > 60s
            if r.alert_level >= ALERT_WARNING:
                if state and state.duration_seconds >= TRIGGER_DURATION_S:
                    return True

            # Fast trend
            if r.trend_slope > TRIGGER_SLOPE:
                return True

            # ML anomaly score
            if r.anomaly_score >= TRIGGER_ML_SCORE:
                return True

        return False

    def generate(self, results: list, manager) -> Path:
        """Generate HTML report unconditionally. Returns path to file."""
        now  = datetime.now(timezone.utc)
        html = _build_html(results, manager, now)

        # Timestamped file
        ts       = now.strftime("%Y%m%d_%H%M%S")
        filename = REPORTS_DIR / f"motorwatch_report_{ts}.html"
        filename.write_text(html, encoding="utf-8")

        # Latest symlink (always overwrite)
        latest = REPORTS_DIR / "motorwatch_report_latest.html"
        latest.write_text(html, encoding="utf-8")

        self._last_generated = now
        logger.info("Report saved → %s", filename)
        return filename

    def maybe_generate(self, results: list, manager) -> Path | None:
        """Generate only if trigger conditions are met."""
        if self.should_generate(results, manager):
            return self.generate(results, manager)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    from analytics.anomaly_detector import AnomalyDetector, _make_client
    from analytics.alert_manager import AlertManager

    client    = _make_client()
    detector  = AnomalyDetector(client)
    manager   = AlertManager()
    generator = ReportGenerator()

    logger.info("Running 1 detection cycle …")
    results = detector.run_cycle()
    manager.process(results)

    logger.info("Generating report …")
    path = generator.generate(results, manager)
    logger.info("Done → %s", path)

    client.close()
