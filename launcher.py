"""
MotorWatch IQ — Launcher
========================
Single-window PySide6 application with two integrated tabs:

  Tab 1 — Launcher
    Manages the full service stack: Mosquitto MQTT broker, InfluxDB writer,
    and optional external services (InfluxDB, Grafana). Provides real-time
    health indicators, a filtered console output, and convenience buttons
    to open browser tabs and a MQTT monitor terminal.

  Tab 2 — Simulator
    Full PySide6 motor simulator UI embedded directly — no separate process.
    Generates synthetic telemetry for 4 motors and publishes via MQTT.

Architecture:
    launcher.py (QMainWindow)
    ├── LauncherWidget   — service management, console, health timer
    └── SimulatorWidget  — motor cards, MQTT publish, tick timer

Process management (Windows):
    All subprocesses are terminated via `taskkill /F /T /PID` which kills
    the full process tree. On Stop All, stdout pipes are closed first to
    unblock ProcessOutputReader threads before joining them.

Usage:
    python launcher.py

Configuration (edit constants at top of file):
    MOSQUITTO_EXE  — path to mosquitto.exe
    INFLUXDB_EXE   — path to influxd.exe
    MOSQUITTO_CONF — path to mosquitto.conf (auto-created dirs)
    WRITER_SCRIPT  — path to influx_writer.py
    VENV_PYTHON    — path to venv python.exe
    INFLUXDB_URL   — InfluxDB UI URL
    GRAFANA_URL    — Grafana dashboard URL (direct link)

Requirements:
    PySide6, paho-mqtt, influxdb-client, python-dotenv
    Mosquitto installed at C:/Program Files/Mosquitto/
"""

import sys
import subprocess
import webbrowser
import time
import socket
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QFrame, QGridLayout, QSizePolicy,
    QTabWidget, QGroupBox, QSlider, QComboBox
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QFont, QColor, QPalette, QTextCursor

from analytics.analytics_tab import AnalyticsTab
from settings_tab import SettingsTab
from settings_loader import load_settings, get_thresholds

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent
VENV_PYTHON      = BASE_DIR / ".venv" / "Scripts" / "python.exe"
MOSQUITTO_EXE    = Path(r"C:\Program Files\Mosquitto\mosquitto.exe")
MOSQUITTO_CONF   = BASE_DIR / "broker" / "mosquitto.conf"
WRITER_SCRIPT    = BASE_DIR / "influxdb" / "influx_writer.py"
ANALYTICS_SCRIPT = BASE_DIR / "analytics" / "anomaly_detector.py"

INFLUXDB_EXE = Path(r"C:\influxdb2\influxd.exe")
INFLUXDB_URL = "http://localhost:8086"
GRAFANA_URL  = "http://localhost:3000/d/adzwmdq/motorwatch-iq?orgId=1&from=now-5m&to=now&timezone=browser&refresh=auto"
MQTT_HOST    = "localhost"
MQTT_PORT    = 1883

# ─── Catppuccin Mocha ─────────────────────────────────────────────────────────
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

# Alias used by simulator code
CLR = {
    "bg":           MOCHA["base"],
    "surface":      MOCHA["surface0"],
    "overlay":      MOCHA["surface1"],
    "text":         MOCHA["text"],
    "subtext":      MOCHA["subtext0"],
    "blue":         MOCHA["blue"],
    "blue_dim":     "#1e3a5f",
    "yellow":       MOCHA["yellow"],
    "yellow_dim":   "#3d2f0e",
    "red":          MOCHA["red"],
    "red_dim":      "#3d0f1a",
    "green":        MOCHA["green"],
    "green_dim":    "#0f3d1a",
    "gray":         MOCHA["overlay0"],
    "border":       MOCHA["surface1"],
    "header":       MOCHA["mantle"],
}

STATE_COLORS = {
    "normal":   {"bg": CLR["blue_dim"],   "fg": CLR["blue"],   "badge": "#1e3a5f",  "badge_text": "#89b4fa"},
    "prealarm": {"bg": CLR["yellow_dim"], "fg": CLR["yellow"], "badge": "#3d2f0e",  "badge_text": "#f9e2af"},
    "alarm":    {"bg": CLR["red_dim"],    "fg": CLR["red"],    "badge": "#3d0f1a",  "badge_text": "#f38ba8"},
    "stopped":  {"bg": CLR["overlay"],    "fg": CLR["subtext"],"badge": CLR["overlay"], "badge_text": CLR["subtext"]},
}

# ─── Simulator constants ──────────────────────────────────────────────────────
RAW_NODATA    = 32764
RAW_OVERLOAD  = 32760

# ISO defaults — usados como fallback se settings_loader falhar
THRESHOLDS = {
    "vrms":  {"prealarm": 7.1,  "alarm": 11.2},
    "apeak": {"prealarm": 1000, "alarm": 2000},
    "temp":  {"prealarm": 65.0, "alarm": 75.0},
}
MOTOR_RUNNING_THRESHOLD_MMS = 0.5


def _load_motor_thresholds(motor_id: int) -> dict:
    """Thresholds efectivos para um motor (custom ou ISO) via settings_loader."""
    try:
        thr = get_thresholds(motor_id)
        return {
            "vrms":  {"prealarm": thr["vrms_prealarm_mms"],  "alarm": thr["vrms_alarm_mms"]},
            "apeak": {"prealarm": thr["apeak_prealarm_mg"],  "alarm": thr["apeak_alarm_mg"]},
            "temp":  {"prealarm": thr["temp_prealarm_c"],    "alarm": thr["temp_alarm_c"]},
        }
    except Exception:
        return THRESHOLDS

SCENARIOS = {
    "normal":          {"label": "Normal (stable)",  "vrms": (0.5, 4.5),  "apeak": (50,  500),  "temp": (30.0, 55.0)},
    "thermal_drift":   {"label": "Thermal drift",    "vrms": (0.8, 3.0),  "apeak": (80,  400),  "temp": (55.0, 80.0)},
    "vibration_drift": {"label": "Vibration drift",  "vrms": (4.0, 14.0), "apeak": (200, 1200), "temp": (35.0, 60.0)},
    "impact_spike":    {"label": "Impact spike",     "vrms": (1.0, 5.0),  "apeak": (800, 3000), "temp": (40.0, 65.0)},
}

DEVICE_STATUS_LABELS = {0: "OK", 1: "Maintenance required", 2: "Out of specification",
                         3: "Functional check", 4: "Failure"}

# ══════════════════════════════════════════════════════════════════════════════
# LAUNCHER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection can be established to host:port within timeout seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


class StatusDot(QLabel):
    """Colored circle indicator for service health status (●).

    Colors follow Catppuccin Mocha: green=ok, red=error, yellow=starting, gray=unknown.
    """
    def __init__(self, parent=None):
        super().__init__("●", parent)
        self.setFont(QFont("Segoe UI", 14))
        self.set_unknown()

    # Estilos guardados para comparação na lógica de LED
    _green_style   = f"color: {MOCHA['green']};"
    _red_style     = f"color: {MOCHA['red']};"
    _unknown_style = f"color: {MOCHA['overlay0']};"
    _yellow_style  = f"color: {MOCHA['yellow']};"

    def set_ok(self):
        self.setStyleSheet(self._green_style)
    def set_error(self):
        self.setStyleSheet(self._red_style)
    def set_unknown(self):
        self.setStyleSheet(self._unknown_style)
    def set_starting(self):
        self.setStyleSheet(self._yellow_style)


class ConsoleWidget(QTextEdit):
    """Read-only timestamped log console with color-coded severity levels.

    Automatically trims to 500 lines to prevent memory accumulation during
    high-frequency log events (e.g. MQTT reconnection loops).

    Levels: info (subtext), ok (green), warn (yellow), error (red), section (blue).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 10))
        self.setStyleSheet(f"""
            QTextEdit {{
                background-color: {MOCHA['crust']};
                color: {MOCHA['text']};
                border: 1px solid {MOCHA['surface0']};
                border-radius: 6px;
                padding: 6px;
            }}
        """)

    def _append(self, text: str, color: str):
        # Limit to 500 lines to avoid memory bloat from reconnect loops
        doc = self.document()
        if doc.blockCount() > 500:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, 50)
            cursor.removeSelectedText()
        self.moveCursor(QTextCursor.End)
        self.setTextColor(QColor(color))
        self.insertPlainText(text + "\n")
        self.moveCursor(QTextCursor.End)

    def info(self, msg: str):
        self._append(f"[{time.strftime('%H:%M:%S')}] {msg}", MOCHA["subtext1"])
    def ok(self, msg: str):
        self._append(f"[{time.strftime('%H:%M:%S')}] ✓ {msg}", MOCHA["green"])
    def warn(self, msg: str):
        self._append(f"[{time.strftime('%H:%M:%S')}] ⚠ {msg}", MOCHA["yellow"])
    def error(self, msg: str):
        self._append(f"[{time.strftime('%H:%M:%S')}] ✗ {msg}", MOCHA["red"])
    def section(self, msg: str):
        self._append(f"[{time.strftime('%H:%M:%S')}] ─── {msg} ───", MOCHA["blue"])


# High-frequency INFO events suppressed from console to reduce UI overhead
_SUPPRESS_EVENTS = frozenset((
    "point_written",
    "point_fault_written",
    "mqtt_reconnecting",
    "mqtt_connected",
    "mqtt_disconnected",
    "influxdb_connect_failed",
))


class ProcessOutputReader(QThread):
    """QThread that reads stdout from a subprocess line by line and emits signals.

    High-frequency INFO events defined in _SUPPRESS_EVENTS are filtered out
    to avoid flooding the Qt signal queue and the console UI. Errors and warnings
    from suppressed event types are still emitted.

    Thread termination: call stop() which closes the stdout pipe, causing the
    blocking readline loop to exit immediately.

    Signals:
        line_received(str, str): emitted with (formatted_line, level)
            level is one of: "info", "ok", "warn", "error"
    """
    line_received = Signal(str, str)

    def __init__(self, process: subprocess.Popen, label: str, parent=None):
        super().__init__(parent)
        self.process = process
        self.label = label
        self._running = True

    def _classify(self, text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ("error", "failed", "exception", "critical")):
            return "error"
        if any(w in lower for w in ("warn", "warning")):
            return "warn"
        if any(w in lower for w in ("started", "connected", "ready", "listening", "running", "success")):
            return "ok"
        return "info"

    def run(self):
        try:
            for line in self.process.stdout:
                if not self._running:
                    break
                text = line.strip()
                if not text:
                    continue
                level = self._classify(text)
                # Suppress high-frequency info-level events to reduce UI load
                if level == "info" and any(s in text for s in _SUPPRESS_EVENTS):
                    continue
                self.line_received.emit(f"[{self.label}] {text}", level)
        except Exception:
            pass

    def stop(self):
        self._running = False
        try:
            if self.process.stdout:
                self.process.stdout.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATOR — MotorState
# ══════════════════════════════════════════════════════════════════════════════

class MotorState:
    """Holds the runtime state of a single simulated motor.

    Thresholds follow ISO 20816-3. Alarm state is computed as a property
    based on current sensor values and device_status.

    Attributes:
        motor_id:          1-4
        scenario_key:      one of SCENARIOS keys
        mode:              "auto" | "manual"
        vrms_mms:          vibration RMS in mm/s
        apeak_mg:          acceleration peak in mg
        temp_c:            temperature in °C
        device_status:     IFM VVB306 device status (0=OK, 4=Failure)
        running:           motor running flag
        nodata_*:          sensor communication lost flags
        _overload:         sensor range exceeded flag
        _nodata_manual:    prevents auto-reset of nodata flags when manually injected
    """
    def __init__(self, motor_id: int, scenario_key: str):
        self.motor_id = motor_id
        self.scenario_key = scenario_key
        self.mode = "auto"
        self.vrms_mms   = 2.0
        self.apeak_mg   = 200
        self.temp_c     = 40.0
        self.device_status = 0
        self.running    = True
        self.intermittent = False
        self.intermittent_freq = 3
        self.nodata_vrms = self.nodata_apeak = self.nodata_temp = False
        self._tick = 0
        self._drift_temp = self._drift_vrms = 0.0
        self._nodata_manual = False
        self._overload = False
        # Thresholds carregados do settings.json (custom ou ISO default)
        self._thr = _load_motor_thresholds(motor_id)

    def reload_thresholds(self):
        """Recarrega thresholds do settings.json — chamado após Apply."""
        self._thr = _load_motor_thresholds(self.motor_id)

    @property
    def alarm_state(self) -> str:
        thr = self._thr
        if self.device_status == 4:
            return "alarm"
        if not self.running or self.vrms_mms <= MOTOR_RUNNING_THRESHOLD_MMS:
            if (self.vrms_mms >= thr["vrms"]["alarm"] or
                    self.apeak_mg >= thr["apeak"]["alarm"] or
                    self.temp_c >= thr["temp"]["alarm"]):
                return "alarm"
            if (self.vrms_mms >= thr["vrms"]["prealarm"] or
                    self.apeak_mg >= thr["apeak"]["prealarm"] or
                    self.temp_c >= thr["temp"]["prealarm"]):
                return "prealarm"
            return "stopped"
        if (self.vrms_mms >= thr["vrms"]["alarm"] or
                self.apeak_mg >= thr["apeak"]["alarm"] or
                self.temp_c >= thr["temp"]["alarm"]):
            return "alarm"
        if (self.vrms_mms >= thr["vrms"]["prealarm"] or
                self.apeak_mg >= thr["apeak"]["prealarm"] or
                self.temp_c >= thr["temp"]["prealarm"]):
            return "prealarm"
        return "normal"

    @property
    def is_running(self) -> bool:
        return self.running and self.vrms_mms > MOTOR_RUNNING_THRESHOLD_MMS

    def param_state(self, param: str) -> str:
        val = {"vrms": self.vrms_mms, "apeak": float(self.apeak_mg), "temp": self.temp_c}[param]
        thr = self._thr
        if val >= thr[param]["alarm"]:    return "alarm"
        if val >= thr[param]["prealarm"]: return "prealarm"
        return "normal"

    def tick_auto(self):
        if self.mode != "auto":
            return
        self._tick += 1
        sc = SCENARIOS[self.scenario_key]
        if self.intermittent and self._tick % self.intermittent_freq == 0:
            self.device_status = 0 if self.device_status == 4 else 4
        if not self._nodata_manual:
            self.nodata_vrms = self.nodata_apeak = self.nodata_temp = False

        # Motor stopped — skip scenario calculation entirely
        if not self.running:
            self.vrms_mms = 0.0
            self.apeak_mg = max(0, int(random.uniform(10, 80) + random.gauss(0, 5)))
            if self.temp_c > 25.0:
                self.temp_c = round(max(25.0, self.temp_c - random.uniform(0.1, 0.3)), 1)
            return

        key = self.scenario_key
        if key == "thermal_drift":
            self._drift_temp += random.uniform(0.0, 0.4)
            rng = sc["temp"][1] - sc["temp"][0]
            self.temp_c   = sc["temp"][0] + (self._drift_temp % rng)
            self.vrms_mms = round(random.uniform(*sc["vrms"]), 1)
            self.apeak_mg = random.randint(*sc["apeak"])
        elif key == "vibration_drift":
            self._drift_vrms += random.uniform(0.0, 0.2)
            rng = sc["vrms"][1] - sc["vrms"][0]
            self.vrms_mms = round(sc["vrms"][0] + (self._drift_vrms % rng), 1)
            self.apeak_mg = random.randint(*sc["apeak"])
            self.temp_c   = round(random.uniform(*sc["temp"]), 1)
        elif key == "impact_spike":
            self.vrms_mms = round(random.uniform(*sc["vrms"]), 1)
            self.apeak_mg = random.randint(1500, 3000) if random.random() < 0.20 else random.randint(sc["apeak"][0], 800)
            self.temp_c   = round(random.uniform(*sc["temp"]), 1)
        else:
            self.vrms_mms = max(0.1, round(random.uniform(*sc["vrms"]) + random.gauss(0, 0.1), 1))
            self.apeak_mg = max(0, int(random.uniform(*sc["apeak"]) + random.gauss(0, 10)))
            self.temp_c   = round(random.uniform(*sc["temp"]) + random.gauss(0, 0.2), 1)

    def to_mqtt_payload(self) -> dict:
        raw_sentinel = RAW_OVERLOAD if self._overload else RAW_NODATA
        return {
            "timestamp":           datetime.now(timezone.utc).isoformat(),
            "motor_id":            self.motor_id,
            "channel":             f"CH{self.motor_id}",
            "vrms_magnitude_raw":  int(self.vrms_mms / 0.1) if not self.nodata_vrms else raw_sentinel,
            "vrms_magnitude_mms":  round(self.vrms_mms, 2) if not self.nodata_vrms else None,
            "apeak_magnitude_raw": self.apeak_mg if not self.nodata_apeak else raw_sentinel,
            "apeak_magnitude_mg":  self.apeak_mg if not self.nodata_apeak else None,
            "apeak_magnitude_g":   round(self.apeak_mg * 0.001, 4) if not self.nodata_apeak else None,
            "temperature_raw":     int(self.temp_c / 0.1) if not self.nodata_temp else raw_sentinel,
            "temperature_c":       round(self.temp_c, 2) if not self.nodata_temp else None,
            "device_status":       self.device_status,
            "device_status_text":  DEVICE_STATUS_LABELS.get(self.device_status, "Unknown"),
            "alarm_state":         self.alarm_state,
            "is_running":          self.is_running,
            "sensor_fault":        "overload" if self._overload else ("nodata" if (self.nodata_vrms or self.nodata_apeak or self.nodata_temp) else None),
        }


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATOR — MQTT Worker
# ══════════════════════════════════════════════════════════════════════════════

class MqttWorker(QThread):
    """QThread managing a Paho MQTT client for the simulator.

    Runs loop_forever() in a background thread. Publishes motor telemetry
    payloads to motorwatch/motors/{id}/telemetry and sub-topics.

    Uses CallbackAPIVersion.VERSION2 to avoid paho deprecation warnings.

    Signals:
        connection_changed(bool, str): emitted on connect/disconnect with
            (is_connected, status_message)

    Note: call loop_stop() before disconnect() on shutdown.
    """
    connection_changed = Signal(bool, str)

    def __init__(self, host="localhost", port=1883):
        super().__init__()
        self.host = host
        self.port = port
        self._client = None
        self._connected = False

    def run(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.connection_changed.emit(False, "paho-mqtt not installed")
            return
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="motorwatch_simulator")
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        try:
            self._client.connect(self.host, self.port, keepalive=60)
            self._client.loop_forever()
        except Exception as e:
            self.connection_changed.emit(False, str(e))

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._connected = (reason_code.value == 0)
        self.connection_changed.emit(self._connected, "Connected" if self._connected else f"Error: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        self.connection_changed.emit(False, "Disconnected")

    def publish(self, topic: str, payload: dict):
        if self._client and self._connected:
            try:
                self._client.publish(topic, json.dumps(payload), qos=0)
            except Exception:
                pass

    def disconnect(self):
        if self._client:
            self._client.loop_stop()   # must stop loop before disconnect
            self._client.disconnect()


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATOR — ParamRow widget
# ══════════════════════════════════════════════════════════════════════════════

class ParamRow(QWidget):
    value_changed = Signal(float)

    def __init__(self, label, unit, min_val, max_val, step=0.1, parent=None):
        super().__init__(parent)
        self._min = min_val
        self._max = max_val
        self._step = step
        self._multiplier = max(1, round(1.0 / step))  # step>=1 would give 0 without max(1,...)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)

        top = QHBoxLayout()
        self.lbl_name = QLabel(f"{label}  <span style='color:{CLR['subtext']};font-size:11px'>{unit}</span>")
        self.lbl_name.setTextFormat(Qt.RichText)
        self.lbl_name.setFont(QFont("Consolas", 11))
        self.lbl_value = QLabel("—")
        self.lbl_value.setFont(QFont("Consolas", 11, QFont.Bold))
        self.lbl_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_value.setMinimumWidth(80)
        top.addWidget(self.lbl_name)
        top.addStretch()
        top.addWidget(self.lbl_value)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(int(min_val * self._multiplier))
        self.slider.setMaximum(int(max_val * self._multiplier))
        self.slider.setSingleStep(1)
        self.slider.valueChanged.connect(lambda raw: self.value_changed.emit(raw / self._multiplier))
        self.slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height:4px; background:{CLR['overlay']}; border-radius:2px; }}
            QSlider::handle:horizontal {{ width:14px;height:14px;margin:-5px 0;background:{CLR['blue']};border-radius:7px; }}
            QSlider::sub-page:horizontal {{ background:{CLR['blue']};border-radius:2px; }}
        """)
        layout.addLayout(top)
        layout.addWidget(self.slider)

    def set_value(self, val: float, state: str = "normal"):
        colors = {"normal": CLR["text"], "prealarm": CLR["yellow"], "alarm": CLR["red"]}
        color = colors.get(state, CLR["text"])
        self.lbl_value.setText(f"<span style='color:{color}'>{val:.1f}</span>")
        self.lbl_value.setTextFormat(Qt.RichText)
        self.slider.blockSignals(True)
        self.slider.setValue(int(val * self._multiplier))
        self.slider.blockSignals(False)

    def set_nodata(self):
        self.lbl_value.setText(f"<span style='color:{CLR['gray']}'>NoData</span>")
        self.lbl_value.setTextFormat(Qt.RichText)

    def set_enabled(self, enabled: bool):
        self.slider.setEnabled(enabled)


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATOR — MotorCard widget
# ══════════════════════════════════════════════════════════════════════════════

class MotorCard(QGroupBox):
    def __init__(self, motor_state: MotorState, parent=None):
        super().__init__(parent)
        self.ms = motor_state
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        self.setMinimumHeight(320)
        self.setStyleSheet(f"QGroupBox {{ background:{CLR['surface']}; border:1px solid {CLR['border']}; border-radius:8px; margin-top:0px; padding:0px; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        self.header = QFrame()
        self.header.setFixedHeight(44)
        self.header.setStyleSheet(f"background:{CLR['header']}; border-radius:8px 8px 0 0;")
        hdr = QHBoxLayout(self.header)
        hdr.setContentsMargins(12, 0, 12, 0)
        self.lbl_icon    = QLabel("⬤"); self.lbl_icon.setFont(QFont("Segoe UI", 16))
        self.lbl_name    = QLabel(f"Motor {self.ms.motor_id}"); self.lbl_name.setFont(QFont("Consolas", 12, QFont.Bold)); self.lbl_name.setStyleSheet(f"color:{CLR['text']};")
        self.lbl_badge   = QLabel("NORMAL"); self.lbl_badge.setFont(QFont("Consolas", 10, QFont.Bold)); self.lbl_badge.setAlignment(Qt.AlignCenter); self.lbl_badge.setFixedWidth(110); self.lbl_badge.setFixedHeight(22); self.lbl_badge.setStyleSheet("border-radius:4px; padding:0 6px;")
        self.lbl_running = QLabel("● RUNNING"); self.lbl_running.setFont(QFont("Consolas", 9))
        hdr.addWidget(self.lbl_icon); hdr.addSpacing(6); hdr.addWidget(self.lbl_name); hdr.addStretch()
        hdr.addWidget(self.lbl_running); hdr.addSpacing(8); hdr.addWidget(self.lbl_badge)
        root.addWidget(self.header)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border:none; background:{CLR['surface']}; }}
            QTabBar::tab {{ background:{CLR['overlay']}; color:{CLR['subtext']}; font-family:Consolas; font-size:11px; padding:5px 16px; border:none; }}
            QTabBar::tab:selected {{ background:{CLR['surface']}; color:{CLR['blue']}; border-bottom:2px solid {CLR['blue']}; }}
            QTabBar::tab:hover {{ color:{CLR['text']}; }}
        """)

        # Tab Values
        tab_v = QWidget(); tv = QVBoxLayout(tab_v); tv.setContentsMargins(12,10,12,10); tv.setSpacing(4)
        mode_row = QHBoxLayout()
        lbl_mode = QLabel("Mode:"); lbl_mode.setFont(QFont("Consolas",11)); lbl_mode.setStyleSheet(f"color:{CLR['subtext']};")
        self.btn_auto   = QPushButton("AUTO")
        self.btn_manual = QPushButton("MANUAL")
        for btn in (self.btn_auto, self.btn_manual):
            btn.setFont(QFont("Consolas",10,QFont.Bold)); btn.setFixedHeight(24); btn.setFixedWidth(72); btn.setCursor(Qt.PointingHandCursor)
        self.btn_auto.clicked.connect(lambda: self._set_mode("auto"))
        self.btn_manual.clicked.connect(lambda: self._set_mode("manual"))
        self.combo_scenario = QComboBox(); self.combo_scenario.setFont(QFont("Consolas",10))
        for k, v in SCENARIOS.items(): self.combo_scenario.addItem(v["label"], k)
        self.combo_scenario.setCurrentIndex(list(SCENARIOS.keys()).index(self.ms.scenario_key))
        self.combo_scenario.currentIndexChanged.connect(self._on_scenario_change)
        self.combo_scenario.setStyleSheet(f"QComboBox {{ background:{CLR['overlay']}; color:{CLR['text']}; border:1px solid {CLR['border']}; border-radius:4px; padding:2px 6px; font-family:Consolas; font-size:10px; }} QComboBox QAbstractItemView {{ background:{CLR['surface']}; color:{CLR['text']}; selection-background-color:{CLR['overlay']}; }}")
        mode_row.addWidget(lbl_mode); mode_row.addSpacing(4); mode_row.addWidget(self.btn_auto); mode_row.addWidget(self.btn_manual); mode_row.addSpacing(4); mode_row.addWidget(self.combo_scenario,1)
        tv.addLayout(mode_row)
        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setStyleSheet(f"color:{CLR['border']};"); tv.addWidget(sep)
        self.row_vrms  = ParamRow("v-RMS",  "mm/s", 0.0, 20.0, 0.1)
        self.row_apeak = ParamRow("a-Peak", "mg",   0,   3000, 10)
        self.row_temp  = ParamRow("Temp",   "°C",   20,  100,  0.5)
        self.row_vrms.value_changed.connect(lambda v: self._manual_set("vrms",  v))
        self.row_apeak.value_changed.connect(lambda v: self._manual_set("apeak", v))
        self.row_temp.value_changed.connect(lambda v: self._manual_set("temp",  v))
        tv.addWidget(self.row_vrms); tv.addWidget(self.row_apeak); tv.addWidget(self.row_temp); tv.addStretch()

        # Tab Status
        tab_s = QWidget(); ts = QVBoxLayout(tab_s); ts.setContentsMargins(12,10,12,10); ts.setSpacing(8)
        row1 = QHBoxLayout()
        lbl_ds = QLabel("Device status:"); lbl_ds.setFont(QFont("Consolas",11)); lbl_ds.setStyleSheet(f"color:{CLR['subtext']};")
        self.combo_ds = QComboBox(); self.combo_ds.setFont(QFont("Consolas",10))
        for val, label in DEVICE_STATUS_LABELS.items(): self.combo_ds.addItem(f"{val} — {label}", val)
        self.combo_ds.currentIndexChanged.connect(self._on_ds_change)
        self.combo_ds.setStyleSheet(f"QComboBox {{ background:{CLR['overlay']}; color:{CLR['text']}; border:1px solid {CLR['border']}; border-radius:4px; padding:2px 6px; font-family:Consolas; font-size:10px; }} QComboBox QAbstractItemView {{ background:{CLR['surface']}; color:{CLR['text']}; selection-background-color:{CLR['overlay']}; }}")
        row1.addWidget(lbl_ds); row1.addWidget(self.combo_ds,1); ts.addLayout(row1)
        row2 = QHBoxLayout()
        lbl_run = QLabel("Motor running:"); lbl_run.setFont(QFont("Consolas",11)); lbl_run.setStyleSheet(f"color:{CLR['subtext']};")
        self.btn_run_on = QPushButton("ON"); self.btn_run_off = QPushButton("OFF")
        for btn in (self.btn_run_on, self.btn_run_off):
            btn.setFont(QFont("Consolas",10,QFont.Bold)); btn.setFixedHeight(24); btn.setFixedWidth(52); btn.setCursor(Qt.PointingHandCursor)
        self.btn_run_on.clicked.connect(lambda: self._set_running(True))
        self.btn_run_off.clicked.connect(lambda: self._set_running(False))
        row2.addWidget(lbl_run); row2.addStretch(); row2.addWidget(self.btn_run_on); row2.addWidget(self.btn_run_off)
        ts.addLayout(row2)
        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine); sep2.setStyleSheet(f"color:{CLR['border']};"); ts.addWidget(sep2)
        lbl_faults = QLabel("Fault injection"); lbl_faults.setFont(QFont("Consolas",11,QFont.Bold)); lbl_faults.setStyleSheet(f"color:{CLR['subtext']};"); ts.addWidget(lbl_faults)
        fault_grid = QGridLayout(); fault_grid.setSpacing(6)
        fault_grid.addWidget(self._fault_btn("⚡  Inject failure",  "alarm",    lambda: self._inject("failure")),   0,0)
        fault_grid.addWidget(self._fault_btn("🔧  Maintenance",     "prealarm", lambda: self._inject("maintenance")),0,1)
        fault_grid.addWidget(self._fault_btn("⛔  NoData (32764)",  "stopped",  lambda: self._inject("nodata")),    1,0)
        fault_grid.addWidget(self._fault_btn("⚠  Overload (32760)","prealarm", lambda: self._inject("overload")),  1,1)
        fault_grid.addWidget(self._fault_btn("↺  Reset to normal", "normal",   lambda: self._inject("reset")),     2,0,1,2)
        ts.addLayout(fault_grid)
        sep3 = QFrame(); sep3.setFrameShape(QFrame.HLine); sep3.setStyleSheet(f"color:{CLR['border']};"); ts.addWidget(sep3)
        lbl_inter = QLabel("Intermittent fault"); lbl_inter.setFont(QFont("Consolas",11,QFont.Bold)); lbl_inter.setStyleSheet(f"color:{CLR['subtext']};"); ts.addWidget(lbl_inter)
        inter_row = QHBoxLayout()
        self.btn_inter_on  = QPushButton("ON")
        self.btn_inter_off = QPushButton("OFF")
        for btn in (self.btn_inter_on, self.btn_inter_off):
            btn.setFont(QFont("Consolas",10,QFont.Bold)); btn.setFixedHeight(24); btn.setFixedWidth(52); btn.setCursor(Qt.PointingHandCursor)
        self.btn_inter_on.clicked.connect(lambda: self._set_intermittent(True))
        self.btn_inter_off.clicked.connect(lambda: self._set_intermittent(False))
        lbl_freq = QLabel("Freq (ticks):"); lbl_freq.setFont(QFont("Consolas",10)); lbl_freq.setStyleSheet(f"color:{CLR['subtext']};")
        self.slider_freq = QSlider(Qt.Horizontal); self.slider_freq.setMinimum(1); self.slider_freq.setMaximum(10); self.slider_freq.setValue(3)
        self.slider_freq.setStyleSheet(f"QSlider::groove:horizontal {{height:4px;background:{CLR['overlay']};border-radius:2px;}} QSlider::handle:horizontal {{width:12px;height:12px;margin:-4px 0;background:{CLR['yellow']};border-radius:6px;}} QSlider::sub-page:horizontal {{background:{CLR['yellow']};border-radius:2px;}}")
        self.slider_freq.valueChanged.connect(lambda v: setattr(self.ms,"intermittent_freq",v))
        self.lbl_freq_val = QLabel("3"); self.lbl_freq_val.setFont(QFont("Consolas",11,QFont.Bold)); self.lbl_freq_val.setStyleSheet(f"color:{CLR['yellow']};"); self.lbl_freq_val.setFixedWidth(20)
        self.slider_freq.valueChanged.connect(lambda v: self.lbl_freq_val.setText(str(v)))
        inter_row.addWidget(self.btn_inter_on); inter_row.addWidget(self.btn_inter_off); inter_row.addSpacing(8)
        inter_row.addWidget(lbl_freq); inter_row.addWidget(self.slider_freq,1); inter_row.addWidget(self.lbl_freq_val)
        ts.addLayout(inter_row); ts.addStretch()

        self.tabs.addTab(tab_v, "Values")
        self.tabs.addTab(tab_s, "Status")
        root.addWidget(self.tabs)
        self._set_mode("auto")

    def _fault_btn(self, text, severity, slot):
        colors = {
            "alarm":    (CLR["red_dim"],    CLR["red"],    "#5f1f2a"),
            "prealarm": (CLR["yellow_dim"], CLR["yellow"], "#5f4a10"),
            "normal":   (CLR["green_dim"],  CLR["green"],  "#1a4f2a"),
            "stopped":  (CLR["overlay"],    CLR["subtext"], CLR["border"]),
        }
        bg, fg, border = colors.get(severity, (CLR["overlay"], CLR["text"], CLR["border"]))
        btn = QPushButton(text)
        btn.setFont(QFont("Consolas",10)); btn.setCursor(Qt.PointingHandCursor); btn.setFixedHeight(28)
        btn.setStyleSheet(f"QPushButton {{background:{bg};color:{fg};border:1px solid {border};border-radius:4px;padding:0 8px;}} QPushButton:hover {{border-color:{fg};}}")
        btn.clicked.connect(slot)
        return btn

    def _set_mode(self, mode):
        self.ms.mode = mode
        is_auto = (mode == "auto")
        active   = f"background:{CLR['blue_dim']};color:{CLR['blue']};border:1px solid {CLR['blue']};border-radius:4px;"
        inactive = f"background:{CLR['overlay']};color:{CLR['subtext']};border:1px solid {CLR['border']};border-radius:4px;"
        self.btn_auto.setStyleSheet(active if is_auto else inactive)
        self.btn_manual.setStyleSheet(inactive if is_auto else active)
        self.combo_scenario.setEnabled(is_auto)
        self.row_vrms.set_enabled(not is_auto)
        self.row_apeak.set_enabled(not is_auto)
        self.row_temp.set_enabled(not is_auto)

    def _on_scenario_change(self, idx):
        self.ms.scenario_key = self.combo_scenario.itemData(idx)
        self.ms._drift_temp = self.ms._drift_vrms = 0.0

    def _on_ds_change(self, idx):
        self.ms.device_status = self.combo_ds.itemData(idx)

    def _set_running(self, val):
        self.ms.running = val
        if not val: self.ms.vrms_mms = 0.0
        on_s  = f"background:{CLR['green_dim']};color:{CLR['green']};border:1px solid {CLR['green']};border-radius:4px;"
        off_s = f"background:{CLR['red_dim']};color:{CLR['red']};border:1px solid {CLR['red']};border-radius:4px;"
        dim_s = f"background:{CLR['overlay']};color:{CLR['subtext']};border:1px solid {CLR['border']};border-radius:4px;"
        self.btn_run_on.setStyleSheet(on_s if val else dim_s)
        self.btn_run_off.setStyleSheet(off_s if not val else dim_s)

    def _manual_set(self, param, val):
        if self.ms.mode != "manual": return
        if param == "vrms":   self.ms.vrms_mms  = round(val,1)
        elif param == "apeak": self.ms.apeak_mg = int(val)
        elif param == "temp":  self.ms.temp_c   = round(val,1)
        self.refresh()

    def _set_intermittent(self, val):
        self.ms.intermittent = val
        on_s  = f"background:{CLR['yellow_dim']};color:{CLR['yellow']};border:1px solid {CLR['yellow']};border-radius:4px;"
        dim_s = f"background:{CLR['overlay']};color:{CLR['subtext']};border:1px solid {CLR['border']};border-radius:4px;"
        self.btn_inter_on.setStyleSheet(on_s if val else dim_s)
        self.btn_inter_off.setStyleSheet(dim_s if val else on_s)

    def _inject(self, fault_type):
        ms = self.ms
        if fault_type == "failure":
            ms.device_status = 4; self.combo_ds.setCurrentIndex(4)
        elif fault_type == "maintenance":
            ms.device_status = 1; self.combo_ds.setCurrentIndex(1)
        elif fault_type == "nodata":
            ms.nodata_vrms = ms.nodata_apeak = ms.nodata_temp = True; ms._nodata_manual = True
        elif fault_type == "overload":
            ms.nodata_vrms = ms.nodata_apeak = ms.nodata_temp = True; ms._nodata_manual = True; ms._overload = True
        elif fault_type == "reset":
            ms.device_status = 0; ms.nodata_vrms = ms.nodata_apeak = ms.nodata_temp = False
            ms._nodata_manual = False; ms._overload = False; ms.running = True; ms.intermittent = False
            ms.vrms_mms = 2.0; ms.apeak_mg = 200; ms.temp_c = 40.0
            self.combo_ds.setCurrentIndex(0); self._set_running(True); self._set_intermittent(False)
        self.refresh()

    def refresh(self):
        ms = self.ms
        state = ms.alarm_state
        any_nodata  = ms.nodata_vrms or ms.nodata_apeak or ms.nodata_temp
        is_overload = ms._overload
        sc = STATE_COLORS.get(state, STATE_COLORS["normal"])
        self.lbl_icon.setStyleSheet(f"color:{sc['fg']};")

        DS_BADGE = {
            1: ("MAINTENANCE", CLR["yellow_dim"], CLR["yellow"], CLR["yellow"]),
            2: ("OUT OF SPEC",  CLR["red_dim"],   CLR["red"],    CLR["red"]),
            3: ("FUNC CHECK",   CLR["overlay"],   CLR["subtext"],CLR["gray"]),
        }
        if is_overload:
            self.lbl_badge.setText("OVERLOAD")
            self.lbl_badge.setStyleSheet(f"background:{CLR['yellow_dim']};color:{CLR['yellow']};border:1px solid {CLR['yellow']};border-radius:4px;padding:0 6px;")
        elif any_nodata:
            self.lbl_badge.setText("NO DATA")
            self.lbl_badge.setStyleSheet(f"background:{CLR['overlay']};color:{CLR['subtext']};border:1px solid {CLR['gray']};border-radius:4px;padding:0 6px;")
        elif ms.device_status in DS_BADGE and state not in ("alarm","prealarm"):
            label, bg, fg, brd = DS_BADGE[ms.device_status]
            self.lbl_badge.setText(label)
            self.lbl_badge.setStyleSheet(f"background:{bg};color:{fg};border:1px solid {brd};border-radius:4px;padding:0 6px;")
        else:
            self.lbl_badge.setText(state.upper().replace("PREALARM","PRE-ALARM"))
            self.lbl_badge.setStyleSheet(f"background:{sc['badge']};color:{sc['badge_text']};border-radius:4px;padding:0 6px;")

        if is_overload:
            self.lbl_running.setText("⚠ OVERLOAD"); self.lbl_running.setStyleSheet(f"color:{CLR['yellow']};font-family:Consolas;font-size:9px;")
        elif any_nodata:
            self.lbl_running.setText("⚠ NO DATA");  self.lbl_running.setStyleSheet(f"color:{CLR['gray']};font-family:Consolas;font-size:9px;")
        elif ms.is_running:
            self.lbl_running.setText("● RUNNING");  self.lbl_running.setStyleSheet(f"color:{CLR['green']};font-family:Consolas;font-size:9px;")
        else:
            self.lbl_running.setText("● STOPPED");  self.lbl_running.setStyleSheet(f"color:{CLR['gray']};font-family:Consolas;font-size:9px;")

        if ms.nodata_vrms:  self.row_vrms.set_nodata()
        else:               self.row_vrms.set_value(ms.vrms_mms, ms.param_state("vrms"))
        if ms.nodata_apeak: self.row_apeak.set_nodata()
        else:               self.row_apeak.set_value(float(ms.apeak_mg), ms.param_state("apeak"))
        if ms.nodata_temp:  self.row_temp.set_nodata()
        else:               self.row_temp.set_value(ms.temp_c, ms.param_state("temp"))

        self._set_running(ms.running)

        bc = CLR["yellow"] if is_overload else (CLR["gray"] if any_nodata else {"normal":CLR["border"],"prealarm":"#BA7517","alarm":"#A32D2D","stopped":CLR["border"]}.get(state,CLR["border"]))
        bs = "dashed" if (is_overload or any_nodata) else "solid"
        self.setStyleSheet(f"QGroupBox {{background:{CLR['surface']};border:1px {bs} {bc};border-radius:8px;margin-top:0px;padding:0px;}}")


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATOR TAB — widget (replaces SimulatorWindow)
# ══════════════════════════════════════════════════════════════════════════════

class SimulatorWidget(QWidget):
    """Motor simulator panel — Tab 2 of the main window.

    Embeds the full MotorWatch IQ simulator UI directly as a QWidget,
    eliminating the need for a separate process. Contains 4 MotorCard
    widgets in a 2×2 grid, a tick timer (1s default), and a MqttWorker
    thread for publishing telemetry.

    MQTT connection is NOT started in __init__ — call connect_mqtt()
    explicitly after the Mosquitto broker is confirmed running. This
    prevents WinError 10061 (connection refused) on application startup.

    Signals:
        mqtt_status_changed(bool, str): forwarded from MqttWorker,
            consumed by LauncherWidget console for status display.
    """
    mqtt_status_changed = Signal(bool, str)  # propagate to launcher tab

    def __init__(self, parent=None):
        super().__init__(parent)
        self.motor_states = [
            MotorState(1, "normal"),
            MotorState(2, "thermal_drift"),
            MotorState(3, "vibration_drift"),
            MotorState(4, "impact_spike"),
        ]
        self._pub_count = 0
        self.mqtt_worker = None
        self._mqtt_connected = False
        self._sim_active     = False   # True only when launcher is in Simulator mode
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        # MQTT connect is triggered by launcher after Mosquitto is ready
        # Call self.connect_mqtt() manually or it auto-retries via _mqtt_connect

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        # Top bar
        top_bar = QHBoxLayout()
        lbl_title = QLabel("MotorWatch IQ"); lbl_title.setFont(QFont("Consolas",15,QFont.Bold)); lbl_title.setStyleSheet(f"color:{CLR['blue']};")
        lbl_sub   = QLabel("Motor Simulator  /  S7-1200 + IFM VVB306"); lbl_sub.setFont(QFont("Consolas",10)); lbl_sub.setStyleSheet(f"color:{CLR['subtext']};")
        self.lbl_mqtt = QLabel("⬤  MQTT: connecting…"); self.lbl_mqtt.setFont(QFont("Consolas",10)); self.lbl_mqtt.setStyleSheet(f"color:{CLR['gray']};")
        top_bar.addWidget(lbl_title); top_bar.addSpacing(12); top_bar.addWidget(lbl_sub); top_bar.addStretch(); top_bar.addWidget(self.lbl_mqtt)
        root.addLayout(top_bar)

        # Motor grid 2×2
        self.motor_cards = []
        grid = QGridLayout(); grid.setSpacing(10)
        for i, ms in enumerate(self.motor_states):
            card = MotorCard(ms)
            self.motor_cards.append(card)
            grid.addWidget(card, i // 2, i % 2)
        root.addLayout(grid)

        # Bottom bar
        bottom = QFrame()
        bottom.setFixedHeight(44)
        bottom.setStyleSheet(f"QFrame {{background:{CLR['surface']};border:1px solid {CLR['border']};border-radius:6px;}}")
        bot = QHBoxLayout(bottom); bot.setContentsMargins(14,0,14,0)
        lbl_int = QLabel("Publish interval:"); lbl_int.setFont(QFont("Consolas",11)); lbl_int.setStyleSheet(f"color:{CLR['subtext']};")
        self.slider_interval = QSlider(Qt.Horizontal); self.slider_interval.setMinimum(500); self.slider_interval.setMaximum(5000); self.slider_interval.setSingleStep(500); self.slider_interval.setValue(1000); self.slider_interval.setFixedWidth(160)
        self.slider_interval.setStyleSheet(f"QSlider::groove:horizontal {{height:4px;background:{CLR['overlay']};border-radius:2px;}} QSlider::handle:horizontal {{width:14px;height:14px;margin:-5px 0;background:{CLR['blue']};border-radius:7px;}} QSlider::sub-page:horizontal {{background:{CLR['blue']};border-radius:2px;}}")
        self.slider_interval.valueChanged.connect(self._on_interval_change)
        self.lbl_interval_val = QLabel("1000 ms"); self.lbl_interval_val.setFont(QFont("Consolas",11,QFont.Bold)); self.lbl_interval_val.setStyleSheet(f"color:{CLR['text']};"); self.lbl_interval_val.setFixedWidth(70)
        self.btn_publish = QPushButton("▶  Publish now"); self.btn_publish.setFont(QFont("Consolas",11,QFont.Bold)); self.btn_publish.setFixedHeight(30); self.btn_publish.setCursor(Qt.PointingHandCursor)
        self.btn_publish.setStyleSheet(f"QPushButton {{background:{CLR['blue_dim']};color:{CLR['blue']};border:1px solid {CLR['blue']};border-radius:5px;padding:0 14px;}} QPushButton:hover {{background:#1a3060;}}")
        self.btn_publish.clicked.connect(self._force_publish)
        self.lbl_pub_count = QLabel("Published: 0"); self.lbl_pub_count.setFont(QFont("Consolas",10)); self.lbl_pub_count.setStyleSheet(f"color:{CLR['subtext']};")
        bot.addWidget(lbl_int); bot.addSpacing(8); bot.addWidget(self.slider_interval); bot.addWidget(self.lbl_interval_val); bot.addStretch()
        bot.addWidget(self.lbl_pub_count); bot.addSpacing(12); bot.addWidget(self.btn_publish)
        root.addWidget(bottom)

    def set_sim_active(self, active: bool):
        """
        Controla se o simulador publica dados via MQTT.
        Chamado pelo launcher ao mudar de modo.
        active=True  → modo Simulator — publica normalmente
        active=False → modo PLC (OPC UA / Snap7) — timer continua mas não publica
        """
        self._sim_active = active
        # Feedback visual no label MQTT
        if not active and self._mqtt_connected:
            self.lbl_mqtt.setText("⬤  MQTT: connected (silent)")
            self.lbl_mqtt.setStyleSheet(f"color:{CLR['overlay']};")
        elif active and self._mqtt_connected:
            self.lbl_mqtt.setText("⬤  MQTT: connected")
            self.lbl_mqtt.setStyleSheet(f"color:{CLR['green']};")

    def connect_mqtt(self):
        """Start MQTT connection — call after Mosquitto is ready."""
        if self.mqtt_worker and self.mqtt_worker.isRunning():
            self.mqtt_worker.disconnect()
            self.mqtt_worker.quit()
            self.mqtt_worker.wait(1000)
        self.mqtt_worker = MqttWorker("localhost", 1883)
        self.mqtt_worker.connection_changed.connect(self._on_mqtt_status)
        self.mqtt_worker.start()

    def _mqtt_connect(self):
        self.connect_mqtt()

    def _on_mqtt_status(self, connected, msg):
        self._mqtt_connected = connected
        if connected:
            self.lbl_mqtt.setText("⬤  MQTT: connected"); self.lbl_mqtt.setStyleSheet(f"color:{CLR['green']};")
        else:
            self.lbl_mqtt.setText(f"⬤  MQTT: {msg}"); self.lbl_mqtt.setStyleSheet(f"color:{CLR['red']};")
        self.mqtt_status_changed.emit(connected, msg)

    def _on_interval_change(self, val):
        rounded = round(val / 500) * 500
        self.lbl_interval_val.setText(f"{rounded} ms")
        self._timer.setInterval(rounded)

    def _tick(self):
        for ms in self.motor_states: ms.tick_auto()
        self._publish_all()
        for card in self.motor_cards: card.refresh()

    def _publish_all(self):
        if not self._mqtt_connected or not self.mqtt_worker: return
        if not self._sim_active: return   # PLC mode active — simulator is silent
        for ms in self.motor_states:
            p = ms.to_mqtt_payload()
            base = f"motorwatch/motors/{ms.motor_id}"
            self.mqtt_worker.publish(f"{base}/telemetry", p)
            self.mqtt_worker.publish(f"{base}/vrms_magnitude",  {"value": p["vrms_magnitude_mms"],  "unit": "mm/s"})
            self.mqtt_worker.publish(f"{base}/apeak_magnitude", {"value": p["apeak_magnitude_mg"],  "unit": "mg"})
            self.mqtt_worker.publish(f"{base}/temperature",     {"value": p["temperature_c"],       "unit": "C"})
            self.mqtt_worker.publish(f"{base}/alarm",           {"state": p["alarm_state"]})
        self._pub_count += 1
        self.lbl_pub_count.setText(f"Published: {self._pub_count}")

    def _force_publish(self):
        for ms in self.motor_states: ms.tick_auto()
        self._publish_all()
        for card in self.motor_cards: card.refresh()

    def shutdown(self):
        self._timer.stop()
        if self.mqtt_worker:
            self.mqtt_worker.disconnect()
            self.mqtt_worker.quit()
            self.mqtt_worker.wait(2000)


# ══════════════════════════════════════════════════════════════════════════════
# LAUNCHER TAB — widget
# ══════════════════════════════════════════════════════════════════════════════

class LauncherWidget(QWidget):
    """Service management panel — Tab 1 of the main window.

    Responsibilities:
    - Start/stop Mosquitto, InfluxDB writer as subprocesses
    - Check InfluxDB and Grafana reachability (external services)
    - Display real-time health indicators (StatusDot per service)
    - Stream subprocess stdout to ConsoleWidget via ProcessOutputReader threads
    - Trigger SimulatorWidget MQTT reconnect after Mosquitto is ready

    Process lifecycle:
        Start All → _start_mosquitto → _check_influxdb → _check_grafana
                  → _start_writer → _finish_startup → health timer (5s)
        Stop All  → taskkill /F /T all PIDs → close pipes → join readers

    Attributes:
        _simulator_ref: set by MainWindow to allow post-startup MQTT connect
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._processes: dict[str, subprocess.Popen] = {}
        self._readers:   dict[str, ProcessOutputReader] = {}
        self._simulator_ref = None  # set by MainWindow after both widgets are created
        # Modo activo: "simulator" | "opcua" | "snap7"
        try:
            from settings_loader import get_plc_config
            self._active_mode = get_plc_config().get("default_mode", "simulator")
        except Exception:
            self._active_mode = "simulator"
        # LEDs de estado por modo (preenchidos em _build_ui)
        self._mode_dots: dict[str, StatusDot] = {}
        self._build_ui()
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(5000)
        self._health_timer.timeout.connect(self._health_check)
        # Health check imediato — actualiza dots de serviços já em execução
        QTimer.singleShot(800, self._startup_health_check)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Header
        header = QLabel("MotorWatch IQ"); header.setFont(QFont("Segoe UI",18,QFont.Bold)); header.setStyleSheet(f"color:{MOCHA['blue']};letter-spacing:1px;")
        sub = QLabel("System Launcher"); sub.setFont(QFont("Segoe UI",10)); sub.setStyleSheet(f"color:{MOCHA['subtext0']};")
        root.addWidget(header); root.addWidget(sub)
        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setStyleSheet(f"color:{MOCHA['surface0']};"); root.addWidget(sep)

        # Services grid
        lbl_s = QLabel("Services"); lbl_s.setFont(QFont("Segoe UI",10,QFont.Bold)); lbl_s.setStyleSheet(f"color:{MOCHA['overlay1']};"); root.addWidget(lbl_s)
        grid = QGridLayout(); grid.setSpacing(8)
        self._service_dots: dict[str, StatusDot] = {}
        services = [
            ("mosquitto",      "Mosquitto MQTT Broker", f"Port {MQTT_PORT}"),
            ("influxdb",       "InfluxDB v2",            "Port 8086"),
            ("grafana",        "Grafana Dashboard",      "Port 3000"),
            ("writer",         "InfluxDB Writer",         "influx_writer.py"),
            ("analytics",      "Analytics Engine",        "anomaly_detector.py"),
            ("plc_collector",  "PLC Collector",           "plc_collector.py"),
        ]
        for row, (key, name, detail) in enumerate(services):
            dot = StatusDot(); self._service_dots[key] = dot
            name_lbl = QLabel(name); name_lbl.setFont(QFont("Segoe UI",10,QFont.Bold)); name_lbl.setStyleSheet(f"color:{MOCHA['text']};")
            detail_lbl = QLabel(detail); detail_lbl.setFont(QFont("Consolas",9)); detail_lbl.setStyleSheet(f"color:{MOCHA['overlay0']};")
            grid.addWidget(dot, row, 0, Qt.AlignCenter); grid.addWidget(name_lbl, row, 1); grid.addWidget(detail_lbl, row, 2)
        grid.setColumnStretch(1,1); root.addLayout(grid)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine); sep2.setStyleSheet(f"color:{MOCHA['surface0']};"); root.addWidget(sep2)

        # ── Data source mode toggle ───────────────────────────────────────────
        lbl_src = QLabel("Data source"); lbl_src.setFont(QFont("Segoe UI",10,QFont.Bold)); lbl_src.setStyleSheet(f"color:{MOCHA['overlay1']};"); root.addWidget(lbl_src)
        src_row = QHBoxLayout(); src_row.setSpacing(10)
        self._btn_mode_sim   = self._make_mode_btn("🖥  Simulator", "simulator")
        self._btn_mode_opcua = self._make_mode_btn("🔌  OPC UA",    "opcua")
        self._btn_mode_snap7 = self._make_mode_btn("🔗  Snap7",     "snap7")
        self._btn_mode_sim.clicked.connect(lambda: self._set_active_mode("simulator"))
        self._btn_mode_opcua.clicked.connect(lambda: self._set_active_mode("opcua"))
        self._btn_mode_snap7.clicked.connect(lambda: self._set_active_mode("snap7"))
        # LED de estado por modo — [btn] [●]
        for btn, mode in (
            (self._btn_mode_sim,   "simulator"),
            (self._btn_mode_opcua, "opcua"),
            (self._btn_mode_snap7, "snap7"),
        ):
            cell = QHBoxLayout(); cell.setSpacing(4); cell.setContentsMargins(0,0,0,0)
            dot  = StatusDot(); dot.set_unknown()
            self._mode_dots[mode] = dot
            cell.addWidget(btn)
            cell.addWidget(dot)
            src_row.addLayout(cell)
        src_row.addStretch()
        root.addLayout(src_row)
        self._refresh_mode_buttons()

        sep3 = QFrame(); sep3.setFrameShape(QFrame.HLine); sep3.setStyleSheet(f"color:{MOCHA['surface0']};"); root.addWidget(sep3)

        # Buttons
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self._btn_start   = self._make_btn("▶  Start All",              MOCHA["green"])
        self._btn_stop    = self._make_btn("■  Stop All",               MOCHA["red"])
        self._btn_browser = self._make_btn("🌐  Open Browser",          MOCHA["blue"])
        self._btn_mqttsub = self._make_btn("📡  MQTT Monitor (Motor 1)", MOCHA["mauve"])
        self._btn_stop.setEnabled(False); self._btn_browser.setEnabled(False); self._btn_mqttsub.setEnabled(False)
        self._btn_start.clicked.connect(self._start_all)
        self._btn_stop.clicked.connect(self._stop_all)
        self._btn_browser.clicked.connect(self._open_browser)
        self._btn_mqttsub.clicked.connect(self._open_mqtt_monitor)
        for btn in (self._btn_start, self._btn_stop, self._btn_browser, self._btn_mqttsub):
            btn_row.addWidget(btn)
        root.addLayout(btn_row)

        # Console
        lbl_c = QLabel("Console Output"); lbl_c.setFont(QFont("Segoe UI",10,QFont.Bold)); lbl_c.setStyleSheet(f"color:{MOCHA['overlay1']};"); root.addWidget(lbl_c)
        self.console = ConsoleWidget(); self.console.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding); root.addWidget(self.console)
        btn_clear = QPushButton("Clear console"); btn_clear.setFixedHeight(28)
        btn_clear.setStyleSheet(f"QPushButton {{background:{MOCHA['surface0']};color:{MOCHA['overlay1']};border:none;border-radius:4px;font-size:11px;padding:0 12px;}} QPushButton:hover {{background:{MOCHA['surface1']};color:{MOCHA['text']};}}")
        btn_clear.clicked.connect(self.console.clear)
        row_clear = QHBoxLayout(); row_clear.addStretch(); row_clear.addWidget(btn_clear); root.addLayout(row_clear)

    def _make_btn(self, text, color):
        btn = QPushButton(text); btn.setFixedHeight(36)
        btn.setStyleSheet(f"""
            QPushButton {{background:{MOCHA['surface0']};color:{color};border:1px solid {color}40;border-radius:6px;font-size:12px;font-weight:bold;padding:0 16px;}}
            QPushButton:hover {{background:{color}20;border-color:{color}80;}}
            QPushButton:pressed {{background:{color}30;}}
            QPushButton:disabled {{color:{MOCHA['overlay0']};border-color:{MOCHA['surface1']};background:{MOCHA['surface0']};}}
        """)
        return btn

    # ── source mode helpers ──────────────────────────────────────────────────
    def _make_mode_btn(self, text: str, mode: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedHeight(30)
        btn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setProperty("mode", mode)
        btn.setStyleSheet(self._mode_btn_style(active=False))
        return btn

    def _mode_btn_style(self, active: bool) -> str:
        if active:
            return (f"QPushButton {{background:{MOCHA['blue']}22;color:{MOCHA['blue']};"
                    f"border:2px solid {MOCHA['blue']};border-radius:6px;padding:0 14px;font-weight:bold;}}"
                    f"QPushButton:hover {{background:{MOCHA['blue']}33;}}")
        return (f"QPushButton {{background:{MOCHA['surface0']};color:{MOCHA['subtext0']};"
                f"border:1px solid {MOCHA['surface1']};border-radius:6px;padding:0 14px;}}"
                f"QPushButton:hover {{color:{MOCHA['text']};border-color:{MOCHA['overlay0']};}}")

    def _set_active_mode(self, mode: str):
        """Altera o modo de fonte de dados em runtime e persiste no settings.json."""
        if mode == self._active_mode:
            return
        self._active_mode = mode
        self._refresh_mode_buttons()
        # Persiste default_mode no settings.json
        try:
            from settings_loader import load_settings, save_settings
            data = load_settings()
            data.setdefault("plc", {})["default_mode"] = mode
            save_settings(data)
        except Exception:
            pass
        mode_labels = {"simulator": "Simulator", "opcua": "OPC UA", "snap7": "Snap7"}
        self.console.ok(f"Data source switched to: {mode_labels.get(mode, mode)}")
        # Se os serviços já estiverem a correr, avisa que é necessário reiniciar
        if self._processes:
            self.console.warn("Restart services (Stop All → Start All) to activate the new data source")

    def _refresh_mode_buttons(self):
        for btn, mode in (
            (self._btn_mode_sim,   "simulator"),
            (self._btn_mode_opcua, "opcua"),
            (self._btn_mode_snap7, "snap7"),
        ):
            btn.setStyleSheet(self._mode_btn_style(active=(mode == self._active_mode)))

    # ── service lifecycle ─────────────────────────────────────────────────────
    def _start_all(self):
        self._btn_start.setEnabled(False)
        self.console.section("Starting MotorWatch IQ stack")
        self._start_mosquitto()

    def _start_mosquitto(self):
        dot = self._service_dots["mosquitto"]
        if not MOSQUITTO_EXE.exists():
            self.console.error(f"Mosquitto not found: {MOSQUITTO_EXE}"); dot.set_error()
            QTimer.singleShot(200, self._check_influxdb); return

        # Kill leftover process — port release handled by delay in QTimer below
        subprocess.run(["taskkill","/F","/IM","mosquitto.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Ensure broker dirs exist
        for d in (BASE_DIR/"broker"/"data", BASE_DIR/"broker"/"log"):
            try: d.mkdir(parents=True, exist_ok=True)
            except Exception as e: self.console.warn(f"Could not create {d}: {e}")

        cmd = [str(MOSQUITTO_EXE), "-c", str(MOSQUITTO_CONF), "-v"] if MOSQUITTO_CONF.exists() else [str(MOSQUITTO_EXE), "-v"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            self._processes["mosquitto"] = proc; dot.set_starting()
            self.console.info(f"Mosquitto started (PID {proc.pid})")
            self._attach_reader(proc, "MQTT")
        except Exception as e:
            self.console.error(f"Failed to start Mosquitto: {e}"); dot.set_error()
        QTimer.singleShot(2500, self._check_influxdb)

    def _check_influxdb(self):
        """Start InfluxDB if not running, or confirm it is reachable."""
        dot = self._service_dots["influxdb"]

        if port_open("localhost", 8086):
            dot.set_ok()
            self.console.ok("InfluxDB already running at localhost:8086")
            QTimer.singleShot(300, self._check_grafana)
            return

        if not INFLUXDB_EXE.exists():
            dot.set_error()
            self.console.error(f"influxd.exe not found: {INFLUXDB_EXE}")
            QTimer.singleShot(300, self._check_grafana)
            return

        try:
            proc = subprocess.Popen(
                [str(INFLUXDB_EXE)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(INFLUXDB_EXE.parent),
            )
            self._processes["influxdb"] = proc
            dot.set_starting()
            self.console.info(f"InfluxDB starting (PID {proc.pid}) — waiting for port 8086...")
            self._attach_reader(proc, "InfluxDB")
            # Wait up to 10s for InfluxDB to be ready before proceeding
            QTimer.singleShot(5000, self._wait_influxdb)
        except Exception as e:
            dot.set_error()
            self.console.error(f"Failed to start InfluxDB: {e}")
            QTimer.singleShot(300, self._check_grafana)

    def _wait_influxdb(self):
        """Check if InfluxDB port is open after startup delay."""
        dot = self._service_dots["influxdb"]
        if port_open("localhost", 8086):
            dot.set_ok()
            self.console.ok("InfluxDB ready at localhost:8086")
        else:
            dot.set_error()
            self.console.error("InfluxDB did not start in time — check influxd.exe logs")
        QTimer.singleShot(300, self._check_grafana)

    def _check_grafana(self):
        dot = self._service_dots["grafana"]
        if port_open("localhost", 3000):
            dot.set_ok(); self.console.ok("Grafana reachable at localhost:3000")
        else:
            dot.set_error(); self.console.warn("Grafana not reachable at localhost:3000 — set Startup type to Automatic in services.msc")
        QTimer.singleShot(300, self._start_writer)

    def _start_writer(self):
        dot = self._service_dots["writer"]
        if not WRITER_SCRIPT.exists():
            self.console.error(f"Writer not found: {WRITER_SCRIPT}"); dot.set_error()
            QTimer.singleShot(200, self._finish_startup); return
        if not VENV_PYTHON.exists():
            self.console.error(f"venv not found: {VENV_PYTHON}"); dot.set_error()
            QTimer.singleShot(200, self._finish_startup); return
        try:
            proc = subprocess.Popen([str(VENV_PYTHON), str(WRITER_SCRIPT)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", cwd=str(BASE_DIR))
            self._processes["writer"] = proc; dot.set_starting()
            self.console.info(f"InfluxDB Writer started (PID {proc.pid})")
            self._attach_reader(proc, "Writer")
        except Exception as e:
            self.console.error(f"Failed to start Writer: {e}"); dot.set_error()
        QTimer.singleShot(1000, self._finish_startup)

    def _finish_startup(self):
        self.console.section("Startup complete — running health check")
        self._health_check()
        self._health_timer.start()
        self._btn_stop.setEnabled(True); self._btn_browser.setEnabled(True); self._btn_mqttsub.setEnabled(True)
        mode_labels = {"simulator": "Simulator", "opcua": "OPC UA (plc_collector)", "snap7": "Snap7 (plc_collector)"}
        self.console.ok(f"Stack running — data source: {mode_labels.get(self._active_mode, self._active_mode)}")
        if self._active_mode == "simulator":
            # Simulator mode: activar publicação + conectar MQTT
            if hasattr(self, '_simulator_ref') and self._simulator_ref:
                self._simulator_ref.set_sim_active(True)
                QTimer.singleShot(500, self._simulator_ref.connect_mqtt)
            self._set_mode_dot("simulator", "starting")
        else:
            # PLC mode: silenciar simulador + arrancar plc_collector
            if hasattr(self, '_simulator_ref') and self._simulator_ref:
                self._simulator_ref.set_sim_active(False)
            QTimer.singleShot(500, self._start_plc_collector)
        # Start analytics process after writer is stable
        QTimer.singleShot(1500, self._start_analytics)
        # Clear pending badge in settings tab after restart
        if hasattr(self, '_settings_tab_ref') and self._settings_tab_ref:
            self._settings_tab_ref.reload()

    def _on_settings_saved(self):
        """Chamado quando o utilizador clica Save no SettingsTab."""
        self.console.warn("Settings saved — click Apply in Settings tab to restart services")
        self._update_pending_badge(True)

    def _on_settings_applied(self):
        """Chamado quando o utilizador confirma Apply no SettingsTab — para e reinicia."""
        self.console.section("Applying new settings — restarting services")
        self._update_pending_badge(False)
        # Sincroniza _active_mode com o default_mode gravado no settings
        try:
            from settings_loader import get_plc_config
            saved_mode = get_plc_config().get("default_mode", "simulator")
            self._active_mode = saved_mode
            self._refresh_mode_buttons()
        except Exception:
            pass
        # Recarrega thresholds nos MotorStates do simulador
        if hasattr(self, '_simulator_ref') and self._simulator_ref:
            for ms in self._simulator_ref.motor_states:
                ms.reload_thresholds()
        self._stop_all()
        QTimer.singleShot(1500, self._start_all)

    def _update_pending_badge(self, pending: bool):
        """Mostra/esconde o indicador ⚠ no título da tab Settings."""
        # MainWindow guarda referência a tabs em self._tabs via MainWindow.__init__
        pass  # badge visual gerido pelo próprio SettingsTab._lbl_pending

    def _start_analytics(self):
        dot = self._service_dots.get("analytics")
        if not ANALYTICS_SCRIPT.exists():
            self.console.error(f"Analytics not found: {ANALYTICS_SCRIPT}")
            if dot: dot.set_error()
            return
        if not VENV_PYTHON.exists():
            self.console.error(f"venv not found: {VENV_PYTHON}")
            if dot: dot.set_error()
            return
        try:
            proc = subprocess.Popen(
                [str(VENV_PYTHON), str(ANALYTICS_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(BASE_DIR),
            )
            self._processes["analytics"] = proc
            if dot: dot.set_starting()
            self.console.info(f"Analytics started (PID {proc.pid})")
            self._attach_reader(proc, "Analytics")
            if hasattr(self, '_analytics_tab_ref') and self._analytics_tab_ref:
                self._analytics_tab_ref.set_analytics_running(True)
        except Exception as e:
            self.console.error(f"Failed to start Analytics: {e}")
            if dot: dot.set_error()

    def _start_plc_collector(self):
        """Inicia plc_collector.py como subprocess (modo OPC UA ou Snap7).
        Se o ficheiro não existir, faz fallback para o simulador com aviso claro."""
        PLC_SCRIPT = BASE_DIR / "plc" / "plc_collector.py"
        dot = self._service_dots.get("plc_collector")

        if not PLC_SCRIPT.exists():
            self.console.error(
                f"✗ plc_collector.py not found: {PLC_SCRIPT}"
            )
            self.console.warn(
                f"⚠ MODE {self._active_mode.upper()} — no PLC data source available"
            )
            self.console.warn(
                "  Falling back to SIMULATOR — switch to Simulator mode or build M8"
            )
            if dot: dot.set_error()
            # Fallback: activa e arranca o simulador para não ficar sem dados
            if hasattr(self, '_simulator_ref') and self._simulator_ref:
                self._simulator_ref.set_sim_active(True)
                QTimer.singleShot(200, self._simulator_ref.connect_mqtt)
            return

        if not VENV_PYTHON.exists():
            self.console.error(f"venv not found: {VENV_PYTHON}")
            if dot: dot.set_error()
            return

        import os as _os
        env = _os.environ.copy()
        env["PLC_MODE"] = "opcua" if self._active_mode == "opcua" else "snap7"
        try:
            proc = subprocess.Popen(
                [str(VENV_PYTHON), str(PLC_SCRIPT)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(BASE_DIR), env=env,
            )
            self._processes["plc_collector"] = proc
            if dot: dot.set_starting()
            self._set_mode_dot(self._active_mode, "starting")
            self.console.ok(
                f"PLC Collector started (PID {proc.pid}) — mode: {self._active_mode.upper()}"
            )
            self.console.info(
                f"  Connecting to PLC at {self._get_plc_ip()} — "
                "watch console for connection status"
            )
            self._attach_reader(proc, "PLC")
        except Exception as e:
            self.console.error(f"Failed to start PLC Collector: {e}")
            if dot: dot.set_error()
            self._set_mode_dot(self._active_mode, "error")

    def _get_plc_ip(self) -> str:
        """Devolve o IP do PLC configurado no settings.json."""
        try:
            from settings_loader import get_plc_config
            return get_plc_config().get("opcua", {}).get("ip", "192.168.0.10")
        except Exception:
            return "192.168.0.10"

    def _stop_all(self):
        self.console.section("Stopping all services")
        self._health_timer.stop()

        # Always kill mosquitto by name as fallback — in case PID was not registered
        subprocess.run(["taskkill", "/F", "/IM", "mosquitto.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Terminate processes first (closes pipes, unblocks readers)
        for key, proc in list(self._processes.items()):
            try:
                if proc.poll() is None:
                    # /T kills the entire process tree — essential on Windows
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    self.console.info(f"Stopped: {key} (PID {proc.pid})")
                # Close stdout to unblock any reader thread
                try:
                    if proc.stdout: proc.stdout.close()
                except Exception:
                    pass
            except Exception as e:
                self.console.warn(f"Could not stop {key}: {e}")
            self._service_dots[key].set_unknown()

        self._processes.clear()

        # Now stop readers (stdout already closed, they will exit quickly)
        for r in self._readers.values():
            r.stop()
            r.wait(500)  # safe now — pipe is closed, thread exits fast
        self._readers.clear()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_browser.setEnabled(False)
        self._btn_mqttsub.setEnabled(False)
        # Reset LEDs de modo
        for dot in self._mode_dots.values():
            dot.set_unknown()
        # Silenciar simulador ao parar — será reactivado no próximo Start All
        if hasattr(self, '_simulator_ref') and self._simulator_ref:
            self._simulator_ref.set_sim_active(False)
        self.console.ok("All services stopped")

    def _startup_health_check(self):
        """Verifica serviços já em execução ao abrir o launcher — actualiza dots."""
        import urllib.request
        checks = [
            ("grafana",  "http://localhost:3000/api/health"),
            ("influxdb", f"{INFLUXDB_URL}/health"),
        ]
        for key, url in checks:
            dot = self._service_dots.get(key)
            if not dot:
                continue
            try:
                urllib.request.urlopen(url, timeout=1)
                dot.set_ok()
            except Exception:
                pass
        # Mosquitto: tenta ligar ao porto TCP
        import socket
        dot_mqtt = self._service_dots.get("mosquitto")
        if dot_mqtt:
            try:
                s = socket.create_connection(("localhost", MQTT_PORT), timeout=1)
                s.close()
                dot_mqtt.set_ok()
            except Exception:
                pass

    def _health_check(self):
        if port_open(MQTT_HOST, MQTT_PORT): self._service_dots["mosquitto"].set_ok()
        elif "mosquitto" in self._processes: self._service_dots["mosquitto"].set_error()
        # InfluxDB — check port AND process if managed
        if port_open("localhost", 8086):
            self._service_dots["influxdb"].set_ok()
        else:
            self._service_dots["influxdb"].set_error()
            if "influxdb" in self._processes:
                proc = self._processes["influxdb"]
                if proc.poll() is not None:
                    self.console.error(f"InfluxDB process exited unexpectedly (code {proc.returncode})")
        self._service_dots["grafana"].set_ok()  if port_open("localhost",3000) else self._service_dots["grafana"].set_error()
        if "writer" in self._processes:
            proc = self._processes["writer"]
            if proc.poll() is None: self._service_dots["writer"].set_ok()
            else: self._service_dots["writer"].set_error(); self.console.error(f"Writer exited unexpectedly (code {proc.returncode})")
        if "analytics" in self._processes:
            proc = self._processes["analytics"]
            dot  = self._service_dots.get("analytics")
            if dot:
                if proc.poll() is None:
                    dot.set_ok()
                else:
                    dot.set_error()
                    self.console.error(f"Analytics exited unexpectedly (code {proc.returncode})")

    def _open_browser(self):
        webbrowser.open(INFLUXDB_URL)
        # Delay second tab with QTimer — avoids blocking Qt main thread with time.sleep
        QTimer.singleShot(400, lambda: webbrowser.open(GRAFANA_URL))
        self.console.ok(f"Opened {INFLUXDB_URL} and {GRAFANA_URL}")

    def _open_mqtt_monitor(self):
        # On Windows, topic must NOT be quoted in cmd — quotes become part of the string
        try:
            subprocess.Popen(
                ["cmd", "/k",
                 "mosquitto_sub",
                 "-h", MQTT_HOST,
                 "-p", str(MQTT_PORT),
                 "-t", "motorwatch/motors/1/#",
                 "-v"],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            self.console.ok(f"MQTT monitor opened: motorwatch/motors/1/#")
        except Exception as e:
            self.console.error(f"Failed to open MQTT monitor: {e}")

    def _attach_reader(self, proc, label):
        reader = ProcessOutputReader(proc, label)
        reader.line_received.connect(self._on_line)
        reader.start(); self._readers[label] = reader

    def _on_line(self, line, level):
        getattr(self.console, level)(line)
        self._update_mode_dot_from_line(line, level)
        self._update_writer_dot_from_line(line)

    def _update_writer_dot_from_line(self, line: str):
        """Writer dot — verde quando conectado ao InfluxDB e MQTT."""
        pass  # dot gerido por _classify via connected/running keywords

    def _update_mode_dot_from_line(self, line: str, level: str):
        """Actualiza o LED do modo activo com base nas mensagens de stdout.

        Prioridade: error > warn > ok
        Uma linha de erro nunca é sobrescrita por uma linha ok posterior
        dentro do mesmo ciclo de conexão — o reset acontece apenas no próximo
        Start All ou quando uma ligação é estabelecida com sucesso.
        """
        lo = line.lower()

        if "[plc]" not in lo:
            # Simulator: via sinal mqtt_status_changed (patch MainWindow)
            return

        mode = self._active_mode  # opcua ou snap7

        # ── Ligação PLC estabelecida com sucesso ──────────────────────────
        # OPC UA: "opc ua connected ✓" ou "subscribed motor"
        # Snap7:  "snap7 connected ✓"
        # Não confundir com "mqtt connected" que é só o broker local
        plc_connected = (
            ("opc ua connected" in lo and "✓" in lo) or
            ("snap7 connected" in lo and "✓" in lo) or
            ("subscribed motor" in lo)
        )
        if plc_connected:
            self._set_mode_dot(mode, "ok")
            return

        # ── Erro de ligação — vermelho, prioridade máxima ─────────────────
        # Detecta erros reais de ligação ao PLC, não mensagens de info
        plc_error = (
            level == "error" and
            any(w in lo for w in (
                "opc ua error", "snap7 error", "tcp connection failed",
                "connection refused", "timed out", "timeout",
                "no nodes subscribed", "failed", "exception",
            ))
        )
        if plc_error:
            self._set_mode_dot(mode, "error")
            return

        # ── A reconectar — amarelo ────────────────────────────────────────
        plc_reconnecting = any(w in lo for w in (
            "reconnecting in", "fault sentinel", "disconnected —",
        ))
        if plc_reconnecting:
            # Só muda para amarelo se não estiver já vermelho
            dot = self._mode_dots.get(mode)
            if dot and dot.styleSheet() != dot._red_style:
                self._set_mode_dot(mode, "warn")
            return

        # "Disconnected from IP:102" é info do snap7 durante tentativa —
        # não altera o LED (pode aparecer antes do erro real)

    def _set_mode_dot(self, mode: str, state: str):
        """Actualiza o LED do modo especificado. state: ok|warn|error|starting|unknown"""
        dot = self._mode_dots.get(mode)
        if not dot:
            return
        if state == "ok":
            dot.set_ok()
        elif state == "error":
            dot.set_error()
        elif state in ("warn", "starting"):
            dot.set_starting()   # amarelo
        else:
            dot.set_unknown()

    def update_mqtt_dot(self, connected: bool):
        """Called from simulator tab to reflect MQTT status."""
        pass  # MQTT is managed by Mosquitto dot already

    def shutdown(self):
        self._health_timer.stop()
        # Always kill mosquitto by name as fallback
        subprocess.run(["taskkill", "/F", "/IM", "mosquitto.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Terminate processes and close pipes first
        for key, proc in list(self._processes.items()):
            try:
                if proc.poll() is None:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                try:
                    if proc.stdout: proc.stdout.close()
                except Exception:
                    pass
            except Exception:
                pass
        self._processes.clear()
        # Stop readers after pipes are closed
        for r in self._readers.values():
            r.stop()
            r.wait(300)
        self._readers.clear()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW — tabs
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MotorWatch IQ")
        self.setMinimumSize(900, 700)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background: {MOCHA['base']}; }}
            QTabBar::tab {{
                background: {MOCHA['surface0']}; color: {MOCHA['subtext0']};
                font-family: 'Segoe UI'; font-size: 12px;
                padding: 8px 24px; border: none;
                border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                background: {MOCHA['base']}; color: {MOCHA['blue']};
                border-bottom: 2px solid {MOCHA['blue']};
            }}
            QTabBar::tab:hover {{ color: {MOCHA['text']}; }}
        """)

        self.launcher_tab  = LauncherWidget()
        self.simulator_tab = SimulatorWidget()
        self.analytics_tab = AnalyticsTab()
        self.settings_tab  = SettingsTab()

        # Give launcher a reference to simulator for post-startup MQTT connect
        self.launcher_tab._simulator_ref = self.simulator_tab

        # Give launcher a reference to analytics tab for status updates
        self.launcher_tab._analytics_tab_ref = self.analytics_tab

        # Give launcher a reference to settings tab
        self.launcher_tab._settings_tab_ref = self.settings_tab

        # Forward MQTT status to launcher console + LED do simulador
        def _on_sim_mqtt(ok, msg):
            if ok:
                self.launcher_tab.console.ok(f"Simulator MQTT: {msg}")
                self.launcher_tab._set_mode_dot("simulator", "ok")
            else:
                self.launcher_tab.console.warn(f"Simulator MQTT: {msg}")
                self.launcher_tab._set_mode_dot("simulator", "warn")
        self.simulator_tab.mqtt_status_changed.connect(_on_sim_mqtt)

        # Forward analytics status to launcher console
        self.analytics_tab.analytics_status_changed.connect(
            lambda ok, msg: self.launcher_tab.console.ok(f"Analytics: {msg}") if ok
                       else self.launcher_tab.console.warn(f"Analytics: {msg}")
        )

        # Settings signals
        self.settings_tab.settings_saved.connect(self.launcher_tab._on_settings_saved)
        self.settings_tab.settings_applied.connect(self.launcher_tab._on_settings_applied)

        tabs.addTab(self.launcher_tab,  "⚙  Launcher")
        tabs.addTab(self.simulator_tab, "📡  Simulator")
        tabs.addTab(self.analytics_tab, "📊  Analytics")
        self._settings_tab_index = tabs.addTab(self.settings_tab, "🔧  Settings")
        self._tabs = tabs

        # Root widget with tabs + footer
        root_widget = QWidget()
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(tabs)

        footer = QLabel("Developed by Fernando Valverde")
        footer.setAlignment(Qt.AlignCenter)
        footer.setFixedHeight(24)
        footer.setFont(QFont("Segoe UI", 9))
        footer.setStyleSheet(f"""
            background: {MOCHA['crust']};
            color: {MOCHA['overlay0']};
            border-top: 1px solid {MOCHA['surface0']};
        """)
        root_layout.addWidget(footer)
        self.setCentralWidget(root_widget)

    def closeEvent(self, event):
        self.analytics_tab.shutdown()
        self.simulator_tab.shutdown()
        self.launcher_tab.shutdown()
        event.accept()

    def show_settings(self):
        """Navega para a aba Settings — chamável externamente."""
        if hasattr(self, '_tabs') and hasattr(self, '_settings_tab_index'):
            self._tabs.setCurrentIndex(self._settings_tab_index)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Windows taskbar icon — MUST be called before any window is created
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MotorWatchIQ.Launcher.1.0"
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(MOCHA["base"]))
    palette.setColor(QPalette.WindowText,      QColor(MOCHA["text"]))
    palette.setColor(QPalette.Base,            QColor(MOCHA["mantle"]))
    palette.setColor(QPalette.AlternateBase,   QColor(MOCHA["surface0"]))
    palette.setColor(QPalette.Text,            QColor(MOCHA["text"]))
    palette.setColor(QPalette.Button,          QColor(MOCHA["surface0"]))
    palette.setColor(QPalette.ButtonText,      QColor(MOCHA["text"]))
    palette.setColor(QPalette.Highlight,       QColor(MOCHA["blue"]))
    palette.setColor(QPalette.HighlightedText, QColor(MOCHA["crust"]))
    app.setPalette(palette)

    # Load icon before creating window — applied to both app and window
    from PySide6.QtGui import QIcon
    icon_path = BASE_DIR / "icon.ico"
    app_icon = QIcon(str(icon_path)) if icon_path.exists() else None
    if app_icon:
        app.setWindowIcon(app_icon)

    # Pre-load settings.json before MainWindow so tabs don't block UI init
    try:
        from settings_loader import load_settings
        load_settings()  # warms the cache; fast on subsequent calls
    except Exception:
        pass

    window = MainWindow()

    if app_icon:
        window.setWindowIcon(app_icon)

    window.show()
    sys.exit(app.exec())
