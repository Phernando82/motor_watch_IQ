"""
MotorWatch IQ — Analytics & Dashboard Tab (Tab 3)
==================================================
Widget isolado, integrado ao launcher.py via:

    from analytics.analytics_tab import AnalyticsTab
    self.analytics_tab = AnalyticsTab()
    tabs.addTab(self.analytics_tab, "📊  Analytics")

    # No LauncherWidget._finish_startup():
    QTimer.singleShot(1500, self._start_analytics)

    # No LauncherWidget._start_analytics() — ver launcher_patch.py

Arquitetura:
    AnalyticsTab (QWidget)
    ├── _top_row
    │   ├── MotorCard × 4  — status badge, anomaly score, vrms/temp/apeak
    │   └── AlertList      — alertas ativos com duração (refresh 5s)
    ├── _bottom_row
    │   ├── SparklinePanel — mini séries temporais por motor (5min, InfluxDB)
    │   └── InterventionList — plano de ação priorizado
    └── _footer_bar        — botões de relatório EN/ES + abrir último

Fonte de dados:
    motor_anomaly measurement no InfluxDB — escrito pelo anomaly_detector.py.
    Consultas Flux diretas via influxdb-client (QTimer 5s).

Dependências (já no venv):
    PySide6, influxdb-client, python-dotenv
"""

from __future__ import annotations

import os
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

from PySide6.QtCore import Qt, QTimer, QThread, Signal as QSignal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

# ─── .env ─────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "motorwatch")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "motors")

REPORTS_DIR   = _ROOT / "reports"

# ─── Catppuccin Mocha (copiado do launcher — sem import circular) ──────────────
MOCHA = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "surface2": "#585b70",
    "overlay0": "#6c7086",
    "overlay1": "#7f849c",
    "text":     "#cdd6f4",
    "subtext0": "#a6adc8",
    "subtext1": "#bac2de",
    "red":      "#f38ba8",
    "yellow":   "#f9e2af",
    "green":    "#a6e3a1",
    "blue":     "#89b4fa",
    "mauve":    "#cba6f7",
    "teal":     "#94e2d5",
    "peach":    "#fab387",
}

# Cor por motor (Catppuccin) — Motor 1=blue, 2=yellow, 3=red, 4=peach
MOTOR_COLORS = {
    "1": MOCHA["blue"],
    "2": MOCHA["yellow"],
    "3": MOCHA["red"],
    "4": MOCHA["peach"],
}

# alert_level → (label, cor_fg, cor_bg_dim)
LEVEL_STYLE = {
    0: ("OK",       MOCHA["green"],  "#0f3d1a"),
    1: ("WATCH",    MOCHA["teal"],   "#0f2e2e"),
    2: ("WARNING",  MOCHA["yellow"], "#3d2f0e"),
    3: ("CRITICAL", MOCHA["red"],    "#3d0f1a"),
}

# ── InfluxWorker — queries em background thread ────────────────────────────────

class InfluxWorker(QThread):
    """
    Executa todas as queries Flux em background.
    Emite resultados via signals — nunca bloqueia a UI thread.
    """
    anomaly_ready    = QSignal(dict)   # motor_id → {anomaly_score, alert_level, ...}
    telemetry_ready  = QSignal(dict)   # motor_id → {vrms:[], temp:[], apeak:[]}
    durations_ready  = QSignal(dict)   # motor_id → duration_str
    connect_result   = QSignal(bool, str)  # ok, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client: InfluxDBClient | None = None
        self._query_api = None
        self._connected = False
        self._task: str = "connect"   # "connect" | "refresh"
        self._running = True

    def request_connect(self):
        self._task = "connect"
        if not self.isRunning():
            self.start()

    def request_refresh(self):
        if not self._connected:
            return
        self._task = "refresh"
        if not self.isRunning():
            self.start()

    def run(self):
        if self._task == "connect":
            self._do_connect()
        elif self._task == "refresh":
            self._do_refresh()

    def _do_connect(self):
        try:
            client = InfluxDBClient(
                url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG,
                timeout=3_000   # 3s — falha rápida, não bloqueia
            )
            # Ping simples para validar
            client.ping()
            self._client    = client
            self._query_api = client.query_api()
            self._connected = True
            self.connect_result.emit(True, "connected")
        except Exception as e:
            self._connected = False
            self.connect_result.emit(False, f"InfluxDB unavailable")

    def _do_refresh(self):
        if not self._query_api:
            return
        try:
            self._fetch_anomaly()
            self._fetch_telemetry()
            self._fetch_durations()
        except Exception:
            pass

    def _fetch_anomaly(self):
        flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "motor_anomaly")
  |> filter(fn: (r) =>
       r._field == "anomaly_score" or
       r._field == "alert_level" or
       r._field == "alert_message" or
       r._field == "trend_slope" or
       r._field == "fault_type" or
       r._field == "fault_prob")
  |> last()
  |> pivot(rowKey: ["_time","motor_id"], columnKey: ["_field"], valueColumn: "_value")
"""
        try:
            tables = self._query_api.query(flux, org=INFLUX_ORG)
            result = {}
            for table in tables:
                for rec in table.records:
                    mid = str(rec.values.get("motor_id", ""))
                    if not mid:
                        continue
                    result[mid] = {
                        "anomaly_score": float(rec.values.get("anomaly_score", 0.0) or 0.0),
                        "alert_level":   int(rec.values.get("alert_level", 0) or 0),
                        "alert_message": str(rec.values.get("alert_message", "") or ""),
                        "trend_slope":   float(rec.values.get("trend_slope", 0.0) or 0.0),
                        "fault_type":    str(rec.values.get("fault_type", "unknown") or "unknown"),
                        "fault_prob":    float(rec.values.get("fault_prob", 0.0) or 0.0),
                    }
            self.anomaly_ready.emit(result)
        except Exception:
            pass

    def _fetch_telemetry(self):
        flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) =>
       r._field == "vrms_magnitude_mms" or
       r._field == "temperature_c" or
       r._field == "apeak_magnitude_mg")
  |> filter(fn: (r) => r._value >= 0.0)
  |> sort(columns: ["_time"])
"""
        try:
            tables = self._query_api.query(flux, org=INFLUX_ORG)
            series: dict[str, dict[str, list]] = {}
            for table in tables:
                for rec in table.records:
                    mid   = str(rec.values.get("motor_id", ""))
                    field = str(rec.get_field())
                    val   = rec.get_value()
                    if not mid or val is None:
                        continue
                    series.setdefault(mid, {}).setdefault(field, []).append(float(val))
            self.telemetry_ready.emit(series)
        except Exception:
            pass

    def _fetch_durations(self):
        # Usa janela de 30min — suficiente e muito mais rápido que 2h
        flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "motor_anomaly")
  |> filter(fn: (r) => r._field == "alert_level")
  |> filter(fn: (r) => r._value > 0)
  |> group(columns: ["motor_id"])
  |> first()
"""
        durations: dict[str, str] = {}
        try:
            tables = self._query_api.query(flux, org=INFLUX_ORG)
            now = datetime.now(timezone.utc)
            for table in tables:
                for rec in table.records:
                    mid = str(rec.values.get("motor_id", ""))
                    t   = rec.get_time()
                    if mid and t:
                        secs = int((now - t).total_seconds())
                        if secs < 60:
                            durations[mid] = f"{secs}s"
                        elif secs < 3600:
                            durations[mid] = f"{secs // 60}m {secs % 60}s"
                        else:
                            durations[mid] = f"{secs // 3600}h {(secs % 3600) // 60}m"
        except Exception:
            pass
        self.durations_ready.emit(durations)

    def close_client(self):
        self._connected = False
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client    = None
            self._query_api = None

    def _fetch_last_telemetry_sync(self) -> dict[str, dict]:
        """Síncrono — usado apenas pelo botão Generate Report (ação manual)."""
        result: dict[str, dict] = {}
        if not self._query_api:
            return result
        flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) =>
       r._field == "vrms_magnitude_mms" or
       r._field == "temperature_c" or
       r._field == "apeak_magnitude_mg")
  |> filter(fn: (r) => r._value >= 0.0)
  |> last()
  |> pivot(rowKey: ["_time","motor_id"], columnKey: ["_field"], valueColumn: "_value")
"""
        try:
            tables = self._query_api.query(flux, org=INFLUX_ORG)
            for table in tables:
                for rec in table.records:
                    mid = str(rec.values.get("motor_id", ""))
                    if mid:
                        result[mid] = {
                            "vrms":  rec.values.get("vrms_magnitude_mms"),
                            "temp":  rec.values.get("temperature_c"),
                            "apeak": rec.values.get("apeak_magnitude_mg"),
                        }
        except Exception:
            pass
        return result

# ─── helpers ──────────────────────────────────────────────────────────────────

def _label(text: str, size: int = 10, bold: bool = False, color: str = MOCHA["text"]) -> QLabel:
    lbl = QLabel(text)
    font = QFont("Consolas", size)
    font.setBold(bold)
    lbl.setFont(font)
    lbl.setStyleSheet(f"color: {color};")
    return lbl


def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color: {MOCHA['surface0']};")
    return f


# ══════════════════════════════════════════════════════════════════════════════
# MotorCard — um card por motor
# ══════════════════════════════════════════════════════════════════════════════

class MotorCard(QFrame):
    """
    Card compacto mostrando estado analytics de um motor.

    Campos exibidos:
        • Badge de nível  (OK / WATCH / WARNING / CRITICAL)
        • Anomaly score   (0.00 – 1.00)
        • v-RMS / Temp / a-Peak (último valor de motor_telemetry)
    """

    def __init__(self, motor_id: str, parent=None):
        super().__init__(parent)
        self.motor_id = motor_id
        self._build()
        self._apply_level(0, 0.0, None, None, None)

    def _build(self):
        self.setFixedWidth(200)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.setStyleSheet(f"""
            QFrame {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 8px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(4)

        # Header: ● Motor N  [BADGE]
        hdr = QHBoxLayout()
        color = MOTOR_COLORS[self.motor_id]
        dot = QLabel("●")
        dot.setFont(QFont("Segoe UI", 12))
        dot.setStyleSheet(f"color: {color};")
        name = QLabel(f"Motor {self.motor_id}")
        name.setFont(QFont("Consolas", 11, QFont.Bold))
        name.setStyleSheet(f"color: {MOCHA['text']};")
        self._badge = QLabel("OK")
        self._badge.setFont(QFont("Consolas", 9, QFont.Bold))
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedWidth(72)
        self._badge.setFixedHeight(20)
        hdr.addWidget(dot)
        hdr.addSpacing(4)
        hdr.addWidget(name)
        hdr.addStretch()
        hdr.addWidget(self._badge)
        root.addLayout(hdr)

        root.addWidget(_hline())

        # Anomaly score
        score_row = QHBoxLayout()
        score_row.addWidget(_label("Anomaly:", 9, color=MOCHA["subtext0"]))
        score_row.addStretch()
        self._lbl_score = _label("—", 11, bold=True)
        score_row.addWidget(self._lbl_score)
        root.addLayout(score_row)

        # v-RMS
        vrms_row = QHBoxLayout()
        vrms_row.addWidget(_label("v-RMS:", 9, color=MOCHA["subtext0"]))
        vrms_row.addStretch()
        self._lbl_vrms = _label("— mm/s", 9)
        vrms_row.addWidget(self._lbl_vrms)
        root.addLayout(vrms_row)

        # Temp
        temp_row = QHBoxLayout()
        temp_row.addWidget(_label("Temp:", 9, color=MOCHA["subtext0"]))
        temp_row.addStretch()
        self._lbl_temp = _label("— °C", 9)
        temp_row.addWidget(self._lbl_temp)
        root.addLayout(temp_row)

        # a-Peak
        apeak_row = QHBoxLayout()
        apeak_row.addWidget(_label("a-Peak:", 9, color=MOCHA["subtext0"]))
        apeak_row.addStretch()
        self._lbl_apeak = _label("— mg", 9)
        apeak_row.addWidget(self._lbl_apeak)
        root.addLayout(apeak_row)

    def _apply_level(
        self,
        level: int,
        score: float,
        vrms: float | None,
        temp: float | None,
        apeak: float | None,
    ):
        label, fg, bg_dim = LEVEL_STYLE.get(level, LEVEL_STYLE[0])
        self._badge.setText(label)
        self._badge.setStyleSheet(
            f"background: {bg_dim}; color: {fg}; border-radius: 4px; padding: 0 4px;"
        )
        # score color
        if score >= 0.75:
            sc = MOCHA["red"]
        elif score >= 0.60:
            sc = MOCHA["yellow"]
        else:
            sc = MOCHA["green"]
        self._lbl_score.setText(f"{score:.2f}")
        self._lbl_score.setStyleSheet(f"color: {sc}; font-weight: bold;")

        self._lbl_vrms.setText(f"{vrms:.1f} mm/s" if vrms is not None else "— mm/s")
        self._lbl_temp.setText(f"{temp:.1f} °C" if temp is not None else "— °C")
        self._lbl_apeak.setText(f"{apeak:.0f} mg" if apeak is not None else "— mg")

    def update_data(
        self,
        level: int,
        score: float,
        vrms: float | None,
        temp: float | None,
        apeak: float | None,
    ):
        self._apply_level(level, score, vrms, temp, apeak)


# ══════════════════════════════════════════════════════════════════════════════
# SparklineWidget — mini gráfico por motor
# ══════════════════════════════════════════════════════════════════════════════

class SparklineWidget(QWidget):
    """
    Mini gráfico v-RMS dos últimos 5min para um motor.
    Dados injetados externamente via set_data().
    """

    def __init__(self, motor_id: str, parent=None):
        super().__init__(parent)
        self.motor_id = motor_id
        self._values: list[float] = []
        self._current: float | None = None
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, values: list[float], current: float | None):
        self._values = values[-80:] if values else []  # max 80 pontos
        self._current = current
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        color = QColor(MOTOR_COLORS[self.motor_id])

        if len(self._values) < 2:
            # sem dados — linha tracejada cinza
            pen = QPen(QColor(MOCHA["surface1"]), 1, Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(0, h // 2, w, h // 2)
            return

        v_min = min(self._values)
        v_max = max(self._values)
        v_range = v_max - v_min if v_max > v_min else 1.0

        points = []
        n = len(self._values)
        for i, v in enumerate(self._values):
            x = int(i / (n - 1) * (w - 2)) + 1
            y = int((1.0 - (v - v_min) / v_range) * (h - 4)) + 2
            points.append((x, y))

        pen = QPen(color, 1.5)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])

        # ponto atual
        if points:
            lx, ly = points[-1]
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(lx - 3, ly - 3, 6, 6)


# ══════════════════════════════════════════════════════════════════════════════
# SparklinePanel — painel com 4 sparklines + labels
# ══════════════════════════════════════════════════════════════════════════════

class SparklinePanel(QFrame):
    """
    4 linhas: [Motor N] [sparkline] [valor atual mm/s]
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 8px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)
        root.addWidget(_label("v-RMS — last 5 min", 9, bold=True, color=MOCHA["subtext0"]))

        self._sparklines:  dict[str, SparklineWidget] = {}
        self._val_labels:  dict[str, QLabel] = {}
        self._lvl_labels:  dict[str, QLabel] = {}   # badge de nível por motor

        for mid in ("1", "2", "3", "4"):
            row = QHBoxLayout()
            row.setSpacing(6)

            color = MOTOR_COLORS[mid]
            ml = QLabel(f"M{mid}")
            ml.setFont(QFont("Consolas", 10, QFont.Bold))
            ml.setStyleSheet(f"color: {color};")
            ml.setFixedWidth(22)

            # Badge de nível — atualizado por update_level()
            lvl_lbl = QLabel("OK")
            lvl_lbl.setFont(QFont("Consolas", 8, QFont.Bold))
            lvl_lbl.setAlignment(Qt.AlignCenter)
            lvl_lbl.setFixedWidth(52)
            lvl_lbl.setFixedHeight(16)
            lvl_lbl.setStyleSheet(
                f"background: #0f3d1a; color: {MOCHA['green']}; border-radius: 3px;"
            )
            self._lvl_labels[mid] = lvl_lbl

            spark = SparklineWidget(mid)
            self._sparklines[mid] = spark

            val_lbl = _label("— mm/s", 9, color=MOCHA["subtext0"])
            val_lbl.setFixedWidth(72)
            val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._val_labels[mid] = val_lbl

            row.addWidget(ml)
            row.addWidget(lvl_lbl)
            row.addWidget(spark, 1)
            row.addWidget(val_lbl)
            root.addLayout(row)

    def update_motor(self, motor_id: str, values: list[float], current: float | None):
        self._sparklines[motor_id].set_data(values, current)
        txt = f"{current:.1f} mm/s" if current is not None else "— mm/s"
        self._val_labels[motor_id].setText(txt)

    def update_level(self, motor_id: str, level: int):
        """Atualiza o badge de nível na linha da sparkline."""
        lbl = self._lvl_labels.get(motor_id)
        if not lbl:
            return
        label, fg, bg = LEVEL_STYLE.get(level, LEVEL_STYLE[0])
        lbl.setText(label)
        lbl.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 3px; padding: 0 2px;"
        )
        # Cor do valor também reflete o nível
        val_lbl = self._val_labels.get(motor_id)
        if val_lbl:
            val_lbl.setStyleSheet(f"color: {fg};" if level > 0 else f"color: {MOCHA['subtext0']};")


# ══════════════════════════════════════════════════════════════════════════════
# AlertList — lista de alertas ativos
# ══════════════════════════════════════════════════════════════════════════════

class AlertList(QFrame):
    """
    Lista de alertas ativos com nível, motor, duração e mensagem.
    Populada via update_alerts(alert_rows).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 8px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.addWidget(_label("Active Alerts", 9, bold=True, color=MOCHA["subtext0"]))
        hdr.addStretch()
        self._lbl_count = _label("", 9, color=MOCHA["overlay0"])
        hdr.addWidget(self._lbl_count)
        root.addLayout(hdr)

        self._list = QListWidget()
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: {MOCHA['mantle']};
                color: {MOCHA['text']};
                border: none;
                border-radius: 4px;
                font-family: Consolas;
                font-size: 10px;
            }}
            QListWidget::item {{
                padding: 4px 6px;
                border-bottom: 1px solid {MOCHA['surface0']};
            }}
            QListWidget::item:selected {{
                background: {MOCHA['surface1']};
            }}
        """)
        root.addWidget(self._list)

    def update_alerts(self, alert_rows: list[dict]):
        """
        alert_rows: list of dicts with keys:
            motor_id, level, label, duration_str, message
        """
        self._list.clear()
        if not alert_rows:
            item = QListWidgetItem("✅  All motors normal")
            item.setForeground(QColor(MOCHA["green"]))
            self._list.addItem(item)
            self._lbl_count.setText("")
            return

        self._lbl_count.setText(f"{len(alert_rows)} active")
        level_icons = {0: "✅", 1: "👁", 2: "🟠", 3: "🔴"}
        for row in alert_rows:
            icon = level_icons.get(row["level"], "?")
            text = (
                f"{icon}  Motor {row['motor_id']}  [{row['label']}]"
                f"  {row['duration_str']}  —  {row['message']}"
            )
            item = QListWidgetItem(text)
            _, fg, _ = LEVEL_STYLE.get(row["level"], LEVEL_STYLE[0])
            item.setForeground(QColor(fg))
            self._list.addItem(item)


# ══════════════════════════════════════════════════════════════════════════════
# InterventionList — plano de ação
# ══════════════════════════════════════════════════════════════════════════════

class InterventionList(QFrame):
    """
    Lista de ações sugeridas derivadas dos alertas ativos.
    Regras simples baseadas em alert_level e alert_message.
    """

    _ACTIONS = {
        3: {
            "temp":    "Check cooling system and bearings.",
            "vrms":    "Inspect bearings and shaft alignment.",
            "apeak":   "Check for impacts — possible bearing defect.",
            "fault":   "Sensor fault — check IO-Link wiring.",
            "default": "Inspect motor immediately.",
        },
        2: {
            "temp":    "Monitor temperature — ventilation may be compromised.",
            "vrms":    "Vibration trend rising — schedule preventive inspection.",
            "apeak":   "Elevated impacts — consider reducing load temporarily.",
            "default": "Schedule preventive maintenance.",
        },
        1: {
            "default": "Monitor trend over the next few hours.",
        },
    }

    _PRIORITY_LABELS = {3: "IMMEDIATE", 2: "URGENT", 1: "PREVENTIVE"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 8px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        root.addWidget(_label("Intervention Plan", 9, bold=True, color=MOCHA["subtext0"]))

        self._list = QListWidget()
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: {MOCHA['mantle']};
                color: {MOCHA['text']};
                border: none;
                border-radius: 4px;
                font-family: Consolas;
                font-size: 10px;
            }}
            QListWidget::item {{
                padding: 4px 6px;
                border-bottom: 1px solid {MOCHA['surface0']};
            }}
        """)
        root.addWidget(self._list)

    def _classify_message(self, message: str) -> str:
        msg = message.lower()
        if "temp" in msg:
            return "temp"
        if "vrms" in msg or "vib" in msg or "rms" in msg:
            return "vrms"
        if "apeak" in msg or "impact" in msg or "peak" in msg:
            return "apeak"
        if "fault" in msg or "sensor" in msg or "nodata" in msg:
            return "fault"
        return "default"

    def update_interventions(self, alert_rows: list[dict]):
        self._list.clear()
        if not alert_rows:
            item = QListWidgetItem("✅  No intervention required.")
            item.setForeground(QColor(MOCHA["green"]))
            self._list.addItem(item)
            return

        # Ordena por nível decrescente
        sorted_rows = sorted(alert_rows, key=lambda r: -r["level"])
        idx = 1
        for row in sorted_rows:
            level = row["level"]
            if level == 0:
                continue
            actions = self._ACTIONS.get(level, {})
            cat = self._classify_message(row["message"])
            action = actions.get(cat, actions.get("default", "Verificar motor."))
            priority = self._PRIORITY_LABELS.get(level, "")
            text = f"{idx}.  [{priority}]  Motor {row['motor_id']} — {action}"
            item = QListWidgetItem(text)
            _, fg, _ = LEVEL_STYLE.get(level, LEVEL_STYLE[0])
            item.setForeground(QColor(fg))
            self._list.addItem(item)
            idx += 1


# ══════════════════════════════════════════════════════════════════════════════
# AnalyticsTab — widget principal do Tab 3
# ══════════════════════════════════════════════════════════════════════════════

class AnalyticsTab(QWidget):
    """
    Tab 3 — Analytics & Dashboard.

    Todas as queries InfluxDB rodam em InfluxWorker (QThread).
    A UI thread nunca bloqueia — zero "Not responding".

    Sinais:
        analytics_status_changed(bool, str) — para o launcher exibir no console
    """

    analytics_status_changed = QSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._alert_states: dict[str, dict] = {}
        self._durations:    dict[str, str]  = {}
        self._telemetry:    dict[str, dict] = {}   # cache: motor_id → {vrms, temp, apeak}
        self._connected = False

        self._build_ui()

        # Worker thread — todas as queries em background
        self._worker = InfluxWorker(self)
        self._worker.connect_result.connect(self._on_connect_result)
        self._worker.anomaly_ready.connect(self._on_anomaly_ready)
        self._worker.telemetry_ready.connect(self._on_telemetry_ready)
        self._worker.durations_ready.connect(self._on_durations_ready)

        # Delay inicial — dá tempo ao InfluxDB subir antes da 1ª tentativa
        QTimer.singleShot(3000, self._try_connect)

        # Refresh timer — só dispara queries se worker não estiver ocupado
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start()

        # Reconnect timer — tenta reconectar se desconectado (30s)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(30_000)
        self._reconnect_timer.timeout.connect(self._try_connect)
        self._reconnect_timer.start()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── Header bar ────────────────────────────────────────────────────────
        hdr_row = QHBoxLayout()
        title = QLabel("📊  Analytics & Dashboard")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        title.setStyleSheet(f"color: {MOCHA['blue']};")
        hdr_row.addWidget(title)
        hdr_row.addStretch()

        # Status dot analytics process
        self._dot_label = QLabel("●  Analytics: —")
        self._dot_label.setFont(QFont("Consolas", 10))
        self._dot_label.setStyleSheet(f"color: {MOCHA['overlay0']};")
        hdr_row.addWidget(self._dot_label)
        root.addLayout(hdr_row)

        root.addWidget(_hline())

        # ── Top row: Motor Cards + Alert List ────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(12)

        # Motor cards — 2×2 grid
        cards_frame = QFrame()
        cards_frame.setStyleSheet("QFrame { background: transparent; border: none; }")
        cards_grid = QGridLayout(cards_frame)
        cards_grid.setSpacing(8)
        cards_grid.setContentsMargins(0, 0, 0, 0)

        self._motor_cards: dict[str, MotorCard] = {}
        positions = [("1", 0, 0), ("2", 0, 1), ("3", 1, 0), ("4", 1, 1)]
        for mid, row, col in positions:
            card = MotorCard(mid)
            self._motor_cards[mid] = card
            cards_grid.addWidget(card, row, col)

        top.addWidget(cards_frame)

        # Alert list
        self._alert_list = AlertList()
        self._alert_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(self._alert_list, 1)

        root.addLayout(top)

        # ── Bottom row: Sparklines + Intervention Plan ────────────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        self._sparkline_panel = SparklinePanel()
        self._sparkline_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(self._sparkline_panel, 1)

        self._intervention_list = InterventionList()
        self._intervention_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bottom.addWidget(self._intervention_list, 1)

        root.addLayout(bottom)

        # ── Footer — botões de relatório ──────────────────────────────────────
        footer = QFrame()
        footer.setFixedHeight(50)
        footer.setStyleSheet(f"""
            QFrame {{
                background: {MOCHA['surface0']};
                border: 1px solid {MOCHA['surface1']};
                border-radius: 6px;
            }}
        """)
        foot_row = QHBoxLayout(footer)
        foot_row.setContentsMargins(14, 0, 14, 0)
        foot_row.setSpacing(10)

        self._btn_report = self._make_report_btn("▶  Generate Report", MOCHA["blue"])
        self._btn_open   = self._make_report_btn("⬇  Open Latest",     MOCHA["teal"])

        self._btn_report.clicked.connect(self._generate_report)
        self._btn_open.clicked.connect(self._open_latest_report)

        foot_row.addWidget(self._btn_report)
        foot_row.addStretch()
        foot_row.addWidget(self._btn_open)

        root.addWidget(footer)

    def _make_report_btn(self, text: str, color: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedHeight(32)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFont(QFont("Consolas", 10, QFont.Bold))
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {MOCHA['surface1']};
                color: {color};
                border: 1px solid {color}60;
                border-radius: 5px;
                padding: 0 14px;
            }}
            QPushButton:hover  {{ background: {color}25; border-color: {color}; }}
            QPushButton:pressed {{ background: {color}40; }}
            QPushButton:disabled {{
                color: {MOCHA['overlay0']};
                border-color: {MOCHA['surface1']};
            }}
        """)
        return btn

    # ── InfluxDB via worker ───────────────────────────────────────────────────

    def _try_connect(self):
        """Tenta conectar ao InfluxDB em background — não bloqueia UI."""
        if not self._connected and not self._worker.isRunning():
            self._worker.request_connect()

    def _on_connect_result(self, ok: bool, msg: str):
        self._connected = ok
        self._set_dot(ok, msg)

    def _on_refresh_tick(self):
        if self._connected and not self._worker.isRunning():
            self._worker.request_refresh()

    def _on_anomaly_ready(self, latest: dict):
        self._alert_states = latest
        # Atualiza badges de nível nas sparklines
        for mid, st in latest.items():
            self._sparkline_panel.update_level(mid, st["alert_level"])
        self._update_cards_and_alerts(latest)

    def _on_telemetry_ready(self, series: dict):
        for mid in ("1", "2", "3", "4"):
            mdata      = series.get(mid, {})
            vrms_vals  = mdata.get("vrms_magnitude_mms", [])
            temp_vals  = mdata.get("temperature_c", [])
            apeak_vals = mdata.get("apeak_magnitude_mg", [])
            current_vrms  = vrms_vals[-1]  if vrms_vals  else None
            current_temp  = temp_vals[-1]  if temp_vals  else None
            current_apeak = apeak_vals[-1] if apeak_vals else None

            # Guarda em cache — usado por _update_cards_and_alerts
            self._telemetry[mid] = {
                "vrms": current_vrms, "temp": current_temp, "apeak": current_apeak
            }

            self._sparkline_panel.update_motor(mid, vrms_vals, current_vrms)

            # Atualiza card com telemetria — pega nível/score do cache de anomaly se disponível
            st = self._alert_states.get(mid)
            self._motor_cards[mid].update_data(
                level=st["alert_level"] if st else 0,
                score=st["anomaly_score"] if st else 0.0,
                vrms=current_vrms, temp=current_temp, apeak=current_apeak,
            )

    def _on_durations_ready(self, durations: dict):
        self._durations = durations
        if self._alert_states:
            self._update_cards_and_alerts(self._alert_states)

    def _set_dot(self, ok: bool, detail: str):
        if ok:
            self._dot_label.setText(f"●  Analytics: {detail}")
            self._dot_label.setStyleSheet(f"color: {MOCHA['green']};")
        else:
            self._dot_label.setText(f"●  Analytics: {detail}")
            self._dot_label.setStyleSheet(f"color: {MOCHA['red']};")
        self.analytics_status_changed.emit(ok, detail)

    def _update_cards_and_alerts(self, latest: dict):
        alert_rows = []
        for mid in ("1", "2", "3", "4"):
            if mid not in latest:
                continue
            st    = latest[mid]
            level = st["alert_level"]
            score = st["anomaly_score"]
            # Usa cache de telemetria — nunca passa None se já temos dados
            tele  = self._telemetry.get(mid, {})
            self._motor_cards[mid].update_data(
                level=level, score=score,
                vrms=tele.get("vrms"), temp=tele.get("temp"), apeak=tele.get("apeak"),
            )
            if level > 0:
                label_str = LEVEL_STYLE[level][0]
                dur_str   = self._durations.get(mid, "—")
                alert_rows.append({
                    "motor_id":     mid,
                    "level":        level,
                    "label":        label_str,
                    "duration_str": dur_str,
                    "message":      st["alert_message"],
                })
        self._alert_list.update_alerts(alert_rows)
        self._intervention_list.update_interventions(alert_rows)

    # ── Relatórios ────────────────────────────────────────────────────────────

    def _generate_report(self):
        import sys
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        try:
            from analytics.report_generator import ReportGenerator
            from analytics.alert_manager import AlertManager

            class _FakeDetectionResult:
                def __init__(self, mid, data, vrms, temp, apeak):
                    self.motor_id       = mid
                    self.anomaly_score  = data.get("anomaly_score", 0.0)
                    self.alert_level    = data.get("alert_level", 0)
                    self.alert_message  = data.get("alert_message", "")
                    self.trend_slope    = data.get("trend_slope", 0.0)
                    self.eta_prealarm_h = -1.0
                    self.eta_alarm_h    = -1.0
                    self.trained        = True
                    self.last_vrms      = vrms  if vrms  is not None else 0.0
                    self.last_temp      = temp  if temp  is not None else 0.0
                    self.last_apeak     = apeak if apeak is not None else 0.0
                    self.fault_type     = data.get("fault_type",  "unknown")
                    self.fault_prob     = data.get("fault_prob",  0.0)

            # Busca telemetria via worker de forma síncrona (botão manual — OK bloquear brevemente)
            tele = self._worker._fetch_last_telemetry_sync() if hasattr(self._worker, '_fetch_last_telemetry_sync') else {}
            fake_manager = AlertManager()
            results = []
            for mid, data in self._alert_states.items():
                mt = tele.get(mid, {})
                results.append(_FakeDetectionResult(
                    mid, data, mt.get("vrms"), mt.get("temp"), mt.get("apeak")
                ))
            if not results:
                self._set_dot(False, "no data — run detector first")
                return
            for r in results:
                state = fake_manager._states[str(r.motor_id)]
                state.level   = r.alert_level
                state.message = r.alert_message
                if r.alert_level > 0:
                    state.first_seen = datetime.now(timezone.utc)
            rg   = ReportGenerator()
            path = rg.generate(results=results, manager=fake_manager)
            if path:
                self._set_dot(True, "report generated")
                webbrowser.open(path.as_uri())
            else:
                self._set_dot(False, "report generation failed")
        except Exception as e:
            self._set_dot(False, f"report error: {e}")

    def _open_latest_report(self):
        latest = REPORTS_DIR / "motorwatch_report_latest.html"
        if latest.exists():
            webbrowser.open(latest.as_uri())
        else:
            self._set_dot(False, "no report found")

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def set_analytics_running(self, running: bool):
        if running:
            self._dot_label.setText("●  Analytics: running")
            self._dot_label.setStyleSheet(f"color: {MOCHA['green']};")
        else:
            self._dot_label.setText("●  Analytics: stopped")
            self._dot_label.setStyleSheet(f"color: {MOCHA['red']};")

    def shutdown(self):
        self._refresh_timer.stop()
        self._reconnect_timer.stop()
        self._worker.quit()
        self._worker.wait(2000)
        self._worker.close_client()
