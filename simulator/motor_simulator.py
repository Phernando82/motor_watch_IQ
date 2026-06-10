"""
MotorWatch IQ — Motor Simulator
================================
PySide6 visual tool that generates synthetic motor telemetry and publishes
it via MQTT to Mosquitto (localhost:1883).

Designed as a standalone testing tool and as an embeddable widget inside
launcher.py. Can run independently via __main__ or as SimulatorWidget
inside the launcher's tab interface.

Features:
  - 4 motor cards in a 2×2 grid, each with Values and Status tabs
  - AUTO scenarios: Normal, Thermal drift, Vibration drift, Impact spike
  - MANUAL mode: direct slider control of v-RMS, a-Peak, Temperature
  - Fault injection: Failure (device_status=4), NoData, Overload
  - Intermittent fault with configurable frequency
  - Configurable publish interval (500ms – 5000ms)

Hardware reference: Siemens S7-1200 + IFM VVB306 (IO-Link COM3)
IODD reference:     ifm-0006F4-20240924-IODD1.1 v1.3.68.1792338
Alarm thresholds:   ISO 20816-3

MQTT topics published per motor (id = 1–4):
  motorwatch/motors/{id}/telemetry        — full JSON payload
  motorwatch/motors/{id}/vrms_magnitude   — {value, unit}
  motorwatch/motors/{id}/apeak_magnitude  — {value, unit}
  motorwatch/motors/{id}/temperature      — {value, unit}
  motorwatch/motors/{id}/alarm            — {state}

Usage:
    python simulator/motor_simulator.py

Requirements:
    PySide6, paho-mqtt
"""

import sys
import json
import random
import numpy as np
from datetime import datetime, timezone
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QSlider, QComboBox, QGroupBox,
    QTabWidget, QFrame
)
from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QFont, QColor, QPalette

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS — based on IODD ifm-0006F4 and ISO 20816-3
# ─────────────────────────────────────────────────────────────────────────────

RAW_NODATA    = 32764
RAW_OVERLOAD  = 32760
RAW_UNDERLOAD = -32760

THRESHOLDS = {
    "vrms":  {"prealarm": 7.1,   "alarm": 11.2},
    "apeak": {"prealarm": 1000,  "alarm": 2000},
    "temp":  {"prealarm": 65.0,  "alarm": 75.0},
}

MOTOR_RUNNING_THRESHOLD_MMS = 0.5

SCENARIOS = {
    "normal":           {"label": "Normal (stable)",         "vrms": (0.5, 4.5),   "apeak": (50,  500),  "temp": (30.0, 55.0)},
    "thermal_drift":    {"label": "Thermal drift",           "vrms": (0.8, 3.0),   "apeak": (80,  400),  "temp": (55.0, 80.0)},
    "vibration_drift":  {"label": "Vibration drift",         "vrms": (4.0, 14.0),  "apeak": (200, 1200), "temp": (35.0, 60.0)},
    "impact_spike":     {"label": "Impact spike",            "vrms": (1.0, 5.0),   "apeak": (800, 3000), "temp": (40.0, 65.0)},
}


DEVICE_STATUS_LABELS = {
    0: "OK",
    1: "Maintenance required",
    2: "Out of specification",
    3: "Functional check",
    4: "Failure",
}

# ─────────────────────────────────────────────────────────────────────────────
# BEARING SIGNAL GENERATOR
# Produces statistically realistic fault signatures matching CWRU feature space.
#
# Features targeted (same as cwru_trainer.py):
#   crest_factor   — peak / rms
#   kurtosis       — impulse sharpness  (normal~3, inner_race~8-15, ball~5-10)
#   skewness       — asymmetry          (normal~0, ball_fault offset)
#   shape_factor   — rms / mean_abs
#   impulse_factor — peak / mean_abs
#   rms_norm       — rms / prealarm_vrms
#
# Bearing frequencies — SKF 6205-2RS at 1797 RPM (same as CWRU dataset):
#   BPFI = 162.2 Hz  (inner race, n_ball=9, contact_angle=0°)
#   BPFO = 107.4 Hz  (outer race)
#   BSF  = 141.2 Hz  (ball spin)
#   FTF  =  14.0 Hz  (cage / train)
# ─────────────────────────────────────────────────────────────────────────────

class BearingSignalGenerator:
    """
    Generates synthetic bearing vibration statistics that match the
    statistical feature space of the CWRU dataset.

    Does NOT generate a full time-domain waveform — computes vrms and apeak
    directly from parametric models calibrated to CWRU feature distributions.

    severity: 0.0 (early fault) → 1.0 (advanced fault)
    """

    # CWRU empirical feature ranges (from dataset analysis)
    # [normal_range, early_fault_range, advanced_fault_range]
    _INNER_KURTOSIS  = (3.0, 8.0,  15.0)   # Fisher kurtosis
    _INNER_CREST     = (2.5, 4.5,   8.0)   # peak / rms
    _BALL_KURTOSIS   = (3.0, 5.0,  10.0)
    _BALL_CREST      = (2.5, 3.5,   6.5)
    _BALL_SKEW       = (0.0, 0.8,   2.0)   # positive skew for ball fault

    @staticmethod
    def _lerp(a: float, b: float, t: float) -> float:
        return a + (b - a) * t

    @classmethod
    def inner_race(
        cls,
        severity: float,           # 0.0 – 1.0
        vrms_baseline: float,      # mm/s — scenario vrms value
    ) -> tuple[float, int]:
        """
        Inner race fault signature — calibrated to CWRU DE 48k feature distributions.

        Target features (empirically calibrated, 100-sample validation):
          kurtosis:  14–26,  mean ~20  (high — periodic sharp impacts)
          crest:     9–12              (high — impulsive)
          skewness:  ±0.5              (near-symmetric — bilateral BPFI impacts)

        Key distinguisher from ball_fault: near-zero skewness.
        BPFI = 162.2 Hz at 1797 RPM (SKF 6205-2RS) → ~7 impacts per 2048-sample window.
        """
        sev = float(np.clip(severity, 0.0, 1.0))
        n, fs, bpfi = 2048, 48000.0, 162.2

        noise  = np.random.normal(0, 1.0, n)
        period = int(fs / bpfi)       # ~296 samples between BPFI impacts
        n_imp  = max(1, n // period)  # ~7 impacts in window

        # amp_rel 6–12 × sigma → kurtosis 14–26 (empirically calibrated)
        amp_rel = cls._lerp(6.0, 12.0, sev)

        for k in range(n_imp):
            center  = int(np.clip(
                k * period + random.randint(-period // 10, period // 10), 0, n - 1
            ))
            amp     = amp_rel * np.random.uniform(0.7, 1.3)
            sign    = 1 if random.random() > 0.5 else -1   # bilateral → skew ~0
            d_len   = min(8, n - center)
            noise[center:center + d_len] += sign * amp * np.exp(-np.arange(d_len) / 2.0)

        rms = float(np.sqrt(np.mean(noise ** 2)))
        if rms > 1e-9:
            noise = noise * (vrms_baseline / rms)

        vrms_mms = round(float(np.sqrt(np.mean(noise ** 2))) + random.gauss(0, 0.05), 2)
        vrms_mms = max(0.1, vrms_mms)

        crest    = cls._lerp(8.0, 12.0, sev)
        apeak_mg = int(vrms_mms * crest * 160 * (0.8 + sev * 0.4)
                       + random.gauss(0, vrms_mms * 15))
        apeak_mg = max(100, apeak_mg)
        return vrms_mms, apeak_mg

    @classmethod
    def ball_fault(
        cls,
        severity: float,
        vrms_baseline: float,
    ) -> tuple[float, int]:
        """
        Ball fault signature — calibrated to CWRU DE 48k feature distributions.

        Target features (empirically calibrated, 100-sample validation):
          kurtosis:  5–10,  mean ~6   (lower than inner race)
          skewness:  1.5–2.3          (positive asymmetry — KEY distinguisher)
          crest:     6–9

        Key distinguisher from inner_race: positive skewness (one-sided impacts).
        BSF = 141.2 Hz at 1797 RPM → ~6 impacts per 2048-sample window.
        Amplitude modulation at 2×FTF = 28 Hz (ball position effect).
        """
        sev = float(np.clip(severity, 0.0, 1.0))
        n, fs, bsf = 2048, 48000.0, 141.2

        # Chi-squared base → inherent positive skewness
        skew_target = cls._lerp(0.6, 1.8, sev)
        k_dof = max(1.5, (2.0 / max(0.1, skew_target)) ** 2)
        noise = (np.random.chisquare(k_dof, n) - k_dof)
        noise = noise / (float(np.std(noise)) + 1e-9)

        period = int(fs / bsf)        # ~340 samples between BSF impacts
        n_imp  = max(1, n // period)  # ~6 impacts in window

        # amp_rel 2.5–5 × sigma → kurtosis 5–10 (lower than inner race)
        amp_rel = cls._lerp(2.5, 5.0, sev)

        for k in range(n_imp):
            # BSF: larger timing jitter than BPFI (±15%)
            center = int(np.clip(
                k * period + random.randint(-period // 5, period // 5), 0, n - 1
            ))
            amp    = amp_rel * np.random.uniform(0.5, 1.5)   # more variable
            sign   = 1 if random.random() > 0.25 else -1      # 75% positive → skew > 0
            d_len  = min(6, n - center)
            noise[center:center + d_len] += sign * amp * np.exp(-np.arange(d_len) / 2.0)

        rms = float(np.sqrt(np.mean(noise ** 2)))
        if rms > 1e-9:
            noise = noise * (vrms_baseline / rms)

        vrms_mms = round(float(np.sqrt(np.mean(noise ** 2))) + random.gauss(0, 0.05), 2)
        vrms_mms = max(0.1, vrms_mms)

        crest = cls._lerp(3.5, 5.5, sev)
        if random.random() < 0.25 + sev * 0.30:
            apeak_mg = int(vrms_mms * crest * 190 * (1.1 + sev * 1.2)
                           + random.gauss(0, vrms_mms * 20))
        else:
            apeak_mg = int(vrms_mms * crest * 130 * (0.7 + sev * 0.5)
                           + random.gauss(0, vrms_mms * 12))
        apeak_mg = max(80, apeak_mg)
        return vrms_mms, apeak_mg

# ─────────────────────────────────────────────────────────────────────────────
# COLORS — Catppuccin Mocha-inspired industrial palette
# ─────────────────────────────────────────────────────────────────────────────

CLR = {
    "bg":           "#1e1e2e",
    "surface":      "#313244",
    "overlay":      "#45475a",
    "text":         "#cdd6f4",
    "subtext":      "#a6adc8",
    "blue":         "#89b4fa",
    "blue_dim":     "#1e3a5f",
    "yellow":       "#f9e2af",
    "yellow_dim":   "#3d2f0e",
    "red":          "#f38ba8",
    "red_dim":      "#3d0f1a",
    "green":        "#a6e3a1",
    "green_dim":    "#0f3d1a",
    "gray":         "#6c7086",
    "border":       "#45475a",
    "header":       "#181825",
}

STATE_COLORS = {
    "normal":   {"bg": CLR["blue_dim"],   "fg": CLR["blue"],   "badge": "#1e3a5f",  "badge_text": "#89b4fa"},
    "prealarm": {"bg": CLR["yellow_dim"], "fg": CLR["yellow"], "badge": "#3d2f0e",  "badge_text": "#f9e2af"},
    "alarm":    {"bg": CLR["red_dim"],    "fg": CLR["red"],    "badge": "#3d0f1a",  "badge_text": "#f38ba8"},
    "stopped":  {"bg": CLR["overlay"],    "fg": CLR["subtext"],"badge": CLR["overlay"], "badge_text": CLR["subtext"]},
}

# ─────────────────────────────────────────────────────────────────────────────
# MOTOR STATE
# ─────────────────────────────────────────────────────────────────────────────

class MotorState:
    """Runtime state container for a single simulated motor.

    Alarm state is computed as a property from current sensor values,
    following ISO 20816-3 thresholds. device_status=4 always forces alarm.

    Drift accumulators (_drift_temp, _drift_vrms) are reset when _inject('reset')
    is called, ensuring scenarios restart from baseline values.
    """
    def __init__(self, motor_id: int, scenario_key: str):
        self.motor_id = motor_id
        self.scenario_key = scenario_key
        self.mode = "auto"              # "auto" | "manual"
        self.vrms_mms   = 2.0
        self.apeak_mg   = 200
        self.temp_c     = 40.0
        self.device_status = 0
        self.running    = True
        self.intermittent = False
        self.intermittent_freq = 3      # every N ticks
        self.nodata_vrms  = False
        self.nodata_apeak = False
        self.nodata_temp  = False
        self._tick = 0
        self._drift_temp  = 0.0
        self._drift_vrms  = 0.0

    @property
    def alarm_state(self) -> str:
        if self.device_status == 4:
            return "alarm"
        if not self.running or self.vrms_mms <= MOTOR_RUNNING_THRESHOLD_MMS:
            if (self.vrms_mms >= THRESHOLDS["vrms"]["alarm"]
                    or self.apeak_mg >= THRESHOLDS["apeak"]["alarm"]
                    or self.temp_c >= THRESHOLDS["temp"]["alarm"]):
                return "alarm"
            if (self.vrms_mms >= THRESHOLDS["vrms"]["prealarm"]
                    or self.apeak_mg >= THRESHOLDS["apeak"]["prealarm"]
                    or self.temp_c >= THRESHOLDS["temp"]["prealarm"]):
                return "prealarm"
            return "stopped"
        if (self.vrms_mms >= THRESHOLDS["vrms"]["alarm"]
                or self.apeak_mg >= THRESHOLDS["apeak"]["alarm"]
                or self.temp_c >= THRESHOLDS["temp"]["alarm"]):
            return "alarm"
        if (self.vrms_mms >= THRESHOLDS["vrms"]["prealarm"]
                or self.apeak_mg >= THRESHOLDS["apeak"]["prealarm"]
                or self.temp_c >= THRESHOLDS["temp"]["prealarm"]):
            return "prealarm"
        return "normal"

    @property
    def is_running(self) -> bool:
        return self.running and self.vrms_mms > MOTOR_RUNNING_THRESHOLD_MMS

    def param_state(self, param: str) -> str:
        val = {"vrms": self.vrms_mms, "apeak": float(self.apeak_mg), "temp": self.temp_c}[param]
        if val >= THRESHOLDS[param]["alarm"]:
            return "alarm"
        if val >= THRESHOLDS[param]["prealarm"]:
            return "prealarm"
        return "normal"

    def tick_auto(self):
        if self.mode != "auto":
            return
        self._tick += 1
        sc = SCENARIOS[self.scenario_key]

        # Intermittent fault
        if self.intermittent and self._tick % self.intermittent_freq == 0:
            self.device_status = 0 if self.device_status == 4 else 4

        # Reset nodata flags in auto only if not manually injected
        # (manual injection sets _nodata_manual = True to persist across ticks)
        if not getattr(self, '_nodata_manual', False):
            self.nodata_vrms = self.nodata_apeak = self.nodata_temp = False

        # Motor stopped — skip scenario calculation entirely
        # Simulate physically realistic stopped-motor readings
        if not self.running:
            self.vrms_mms = 0.0
            # Residual structural vibration from nearby machines (~10-80 mg)
            self.apeak_mg = int(random.uniform(10, 80) + random.gauss(0, 5))
            self.apeak_mg = max(0, self.apeak_mg)
            # Gradual cooldown toward ambient temperature (25°C), 0.3°C per tick
            if self.temp_c > 25.0:
                self.temp_c = round(max(25.0, self.temp_c - random.uniform(0.1, 0.3)), 1)
            return

        key = self.scenario_key
        if key == "thermal_drift":
            self._drift_temp += random.uniform(0.0, 0.4)
            rng = sc["temp"][1] - sc["temp"][0]
            self.temp_c = sc["temp"][0] + (self._drift_temp % rng)
            self.vrms_mms = round(random.uniform(*sc["vrms"]), 1)
            self.apeak_mg = random.randint(*sc["apeak"])

        elif key == "vibration_drift":
            # Inner race fault — severity grows with drift accumulator
            self._drift_vrms += random.uniform(0.0, 0.2)
            rng      = sc["vrms"][1] - sc["vrms"][0]
            vrms_raw = sc["vrms"][0] + (self._drift_vrms % rng)
            severity = (vrms_raw - sc["vrms"][0]) / max(rng, 0.1)
            self.vrms_mms, self.apeak_mg = BearingSignalGenerator.inner_race(
                severity=severity, vrms_baseline=vrms_raw
            )
            self.vrms_mms = round(self.vrms_mms, 1)
            self.temp_c   = round(random.uniform(*sc["temp"]), 1)

        elif key == "impact_spike":
            # Ball fault — random high-energy spikes at BSF frequency
            vrms_raw = random.uniform(*sc["vrms"])
            severity = random.uniform(0.3, 0.9)  # variable severity per tick
            self.vrms_mms, self.apeak_mg = BearingSignalGenerator.ball_fault(
                severity=severity, vrms_baseline=vrms_raw
            )
            self.vrms_mms = round(self.vrms_mms, 1)
            self.temp_c   = round(random.uniform(*sc["temp"]), 1)

        else:  # normal
            self.vrms_mms = round(random.uniform(*sc["vrms"]) + random.gauss(0, 0.1), 1)
            self.vrms_mms = max(0.1, self.vrms_mms)
            self.apeak_mg = int(random.uniform(*sc["apeak"]) + random.gauss(0, 10))
            self.apeak_mg = max(0, self.apeak_mg)
            self.temp_c   = round(random.uniform(*sc["temp"]) + random.gauss(0, 0.2), 1)

    def to_mqtt_payload(self) -> dict:
        ts = datetime.now(timezone.utc).isoformat()
        is_overload = getattr(self, '_overload', False)
        raw_sentinel = RAW_OVERLOAD if is_overload else RAW_NODATA
        return {
            "timestamp":           ts,
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
            "sensor_fault":        "overload" if is_overload else ("nodata" if (self.nodata_vrms or self.nodata_apeak or self.nodata_temp) else None),
        }


# ─────────────────────────────────────────────────────────────────────────────
# MQTT WORKER
# ─────────────────────────────────────────────────────────────────────────────

class MqttWorker(QThread):
    """QThread managing a Paho MQTT client for simulator publishing.

    Runs loop_forever() in a background thread. Uses CallbackAPIVersion.VERSION2
    to avoid paho deprecation warnings with Mosquitto 2.x.

    Important: loop_stop() must be called before disconnect() on shutdown.

    Signals:
        connection_changed(bool, str): (is_connected, status_message)
    """
    connection_changed = Signal(bool, str)

    def __init__(self, host: str = "localhost", port: int = 1883):
        super().__init__()
        self.host = host
        self.port = port
        self._client = None
        self._connected = False

    def run(self):
        if not MQTT_AVAILABLE:
            self.connection_changed.emit(False, "paho-mqtt not installed")
            return
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="motorwatch_simulator",
        )
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        try:
            self._client.connect(self.host, self.port, keepalive=60)
            self._client.loop_forever()
        except Exception as e:
            self.connection_changed.emit(False, str(e))

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._connected = (reason_code.value == 0)
        msg = "Connected" if self._connected else f"Error: {reason_code}"
        self.connection_changed.emit(self._connected, msg)

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


# ─────────────────────────────────────────────────────────────────────────────
# PARAMETER ROW WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class ParamRow(QWidget):
    value_changed = Signal(float)

    def __init__(self, label: str, unit: str, min_val: float, max_val: float,
                 step: float = 0.1, parent=None):
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
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {CLR['overlay']};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 14px; height: 14px;
                margin: -5px 0;
                background: {CLR['blue']};
                border-radius: 7px;
            }}
            QSlider::sub-page:horizontal {{
                background: {CLR['blue']};
                border-radius: 2px;
            }}
        """)

        layout.addLayout(top)
        layout.addWidget(self.slider)

    def _on_slider(self, raw: int):
        val = raw / self._multiplier
        self.value_changed.emit(val)

    def set_value(self, val: float, state: str = "normal"):
        colors = {"normal": CLR["text"], "prealarm": CLR["yellow"], "alarm": CLR["red"]}
        color = colors.get(state, CLR["text"])
        if isinstance(val, float):
            self.lbl_value.setText(f"<span style='color:{color}'>{val:.1f}</span>")
        else:
            self.lbl_value.setText(f"<span style='color:{color}'>{val}</span>")
        self.lbl_value.setTextFormat(Qt.RichText)
        # Update slider without triggering signal
        self.slider.blockSignals(True)
        self.slider.setValue(int(val * self._multiplier))
        self.slider.blockSignals(False)

    def set_nodata(self):
        gray = CLR["gray"]
        self.lbl_value.setText(f"<span style='color:{gray}'>NoData</span>")
        self.lbl_value.setTextFormat(Qt.RichText)

    def set_enabled(self, enabled: bool):
        self.slider.setEnabled(enabled)


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR CARD WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class MotorCard(QGroupBox):
    state_changed = Signal(int)  # motor_id

    def __init__(self, motor_state: MotorState, parent=None):
        super().__init__(parent)
        self.ms = motor_state
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        self.setMinimumHeight(320)
        self.setStyleSheet(f"""
            QGroupBox {{
                background: {CLR['surface']};
                border: 1px solid {CLR['border']};
                border-radius: 8px;
                margin-top: 0px;
                padding: 0px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────────
        self.header = QFrame()
        self.header.setFixedHeight(44)
        self.header.setStyleSheet(f"background: {CLR['header']}; border-radius: 8px 8px 0 0;")
        hdr_layout = QHBoxLayout(self.header)
        hdr_layout.setContentsMargins(12, 0, 12, 0)

        self.lbl_icon = QLabel("⬤")
        self.lbl_icon.setFont(QFont("Segoe UI", 16))

        self.lbl_name = QLabel(f"Motor {self.ms.motor_id}")
        self.lbl_name.setFont(QFont("Consolas", 12, QFont.Bold))
        self.lbl_name.setStyleSheet(f"color: {CLR['text']};")

        self.lbl_badge = QLabel("NORMAL")
        self.lbl_badge.setFont(QFont("Consolas", 10, QFont.Bold))
        self.lbl_badge.setAlignment(Qt.AlignCenter)
        self.lbl_badge.setFixedWidth(110)
        self.lbl_badge.setFixedHeight(22)
        self.lbl_badge.setStyleSheet("border-radius: 4px; padding: 0 6px;")

        self.lbl_running = QLabel("● RUNNING")
        self.lbl_running.setFont(QFont("Consolas", 9))

        hdr_layout.addWidget(self.lbl_icon)
        hdr_layout.addSpacing(6)
        hdr_layout.addWidget(self.lbl_name)
        hdr_layout.addStretch()
        hdr_layout.addWidget(self.lbl_running)
        hdr_layout.addSpacing(8)
        hdr_layout.addWidget(self.lbl_badge)
        root.addWidget(self.header)

        # ── Tabs ────────────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: {CLR['surface']};
            }}
            QTabBar::tab {{
                background: {CLR['overlay']};
                color: {CLR['subtext']};
                font-family: Consolas; font-size: 11px;
                padding: 5px 16px;
                border: none;
            }}
            QTabBar::tab:selected {{
                background: {CLR['surface']};
                color: {CLR['blue']};
                border-bottom: 2px solid {CLR['blue']};
            }}
            QTabBar::tab:hover {{ color: {CLR['text']}; }}
        """)

        # Tab 0 — Values
        tab_values = QWidget()
        tv = QVBoxLayout(tab_values)
        tv.setContentsMargins(12, 10, 12, 10)
        tv.setSpacing(4)

        # Mode row
        mode_row = QHBoxLayout()
        lbl_mode = QLabel("Mode:")
        lbl_mode.setFont(QFont("Consolas", 11))
        lbl_mode.setStyleSheet(f"color: {CLR['subtext']};")

        self.btn_auto = QPushButton("AUTO")
        self.btn_manual = QPushButton("MANUAL")
        for btn in (self.btn_auto, self.btn_manual):
            btn.setFont(QFont("Consolas", 10, QFont.Bold))
            btn.setFixedHeight(24)
            btn.setFixedWidth(72)
            btn.setCursor(Qt.PointingHandCursor)
        self.btn_auto.clicked.connect(lambda: self._set_mode("auto"))
        self.btn_manual.clicked.connect(lambda: self._set_mode("manual"))

        self.combo_scenario = QComboBox()
        self.combo_scenario.setFont(QFont("Consolas", 10))
        for k, v in SCENARIOS.items():
            self.combo_scenario.addItem(v["label"], k)
        self.combo_scenario.setCurrentIndex(list(SCENARIOS.keys()).index(self.ms.scenario_key))
        self.combo_scenario.currentIndexChanged.connect(self._on_scenario_change)
        self.combo_scenario.setStyleSheet(f"""
            QComboBox {{
                background: {CLR['overlay']}; color: {CLR['text']};
                border: 1px solid {CLR['border']}; border-radius: 4px;
                padding: 2px 6px; font-family: Consolas; font-size: 10px;
            }}
            QComboBox QAbstractItemView {{
                background: {CLR['surface']}; color: {CLR['text']};
                selection-background-color: {CLR['overlay']};
            }}
        """)

        mode_row.addWidget(lbl_mode)
        mode_row.addSpacing(4)
        mode_row.addWidget(self.btn_auto)
        mode_row.addWidget(self.btn_manual)
        mode_row.addSpacing(4)
        mode_row.addWidget(self.combo_scenario, 1)
        tv.addLayout(mode_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet(f"color: {CLR['border']};")
        tv.addWidget(separator)

        # Param rows
        self.row_vrms  = ParamRow("v-RMS",  "mm/s", 0.0, 20.0,  0.1)
        self.row_apeak = ParamRow("a-Peak", "mg",   0,   3000,  10)
        self.row_temp  = ParamRow("Temp",   "°C",   20,  100,   0.5)

        self.row_vrms.value_changed.connect(lambda v: self._manual_set("vrms", v))
        self.row_apeak.value_changed.connect(lambda v: self._manual_set("apeak", v))
        self.row_temp.value_changed.connect(lambda v: self._manual_set("temp", v))

        tv.addWidget(self.row_vrms)
        tv.addWidget(self.row_apeak)
        tv.addWidget(self.row_temp)
        tv.addStretch()

        # Tab 1 — Status
        tab_status = QWidget()
        ts = QVBoxLayout(tab_status)
        ts.setContentsMargins(12, 10, 12, 10)
        ts.setSpacing(8)

        # Device status + running
        row1 = QHBoxLayout()
        lbl_ds = QLabel("Device status:")
        lbl_ds.setFont(QFont("Consolas", 11))
        lbl_ds.setStyleSheet(f"color: {CLR['subtext']};")

        self.combo_ds = QComboBox()
        self.combo_ds.setFont(QFont("Consolas", 10))
        for val, label in DEVICE_STATUS_LABELS.items():
            self.combo_ds.addItem(f"{val} — {label}", val)
        self.combo_ds.currentIndexChanged.connect(self._on_ds_change)
        self.combo_ds.setStyleSheet(f"""
            QComboBox {{
                background: {CLR['overlay']}; color: {CLR['text']};
                border: 1px solid {CLR['border']}; border-radius: 4px;
                padding: 2px 6px; font-family: Consolas; font-size: 10px;
            }}
            QComboBox QAbstractItemView {{
                background: {CLR['surface']}; color: {CLR['text']};
                selection-background-color: {CLR['overlay']};
            }}
        """)
        row1.addWidget(lbl_ds)
        row1.addWidget(self.combo_ds, 1)
        ts.addLayout(row1)

        # Motor running toggle
        row2 = QHBoxLayout()
        lbl_run = QLabel("Motor running:")
        lbl_run.setFont(QFont("Consolas", 11))
        lbl_run.setStyleSheet(f"color: {CLR['subtext']};")

        self.btn_run_on  = QPushButton("ON")
        self.btn_run_off = QPushButton("OFF")
        for btn in (self.btn_run_on, self.btn_run_off):
            btn.setFont(QFont("Consolas", 10, QFont.Bold))
            btn.setFixedHeight(24)
            btn.setFixedWidth(52)
            btn.setCursor(Qt.PointingHandCursor)
        self.btn_run_on.clicked.connect(lambda: self._set_running(True))
        self.btn_run_off.clicked.connect(lambda: self._set_running(False))

        row2.addWidget(lbl_run)
        row2.addStretch()
        row2.addWidget(self.btn_run_on)
        row2.addWidget(self.btn_run_off)
        ts.addLayout(row2)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(f"color: {CLR['border']};")
        ts.addWidget(sep2)

        # Fault injection buttons
        lbl_faults = QLabel("Fault injection")
        lbl_faults.setFont(QFont("Consolas", 11, QFont.Bold))
        lbl_faults.setStyleSheet(f"color: {CLR['subtext']};")
        ts.addWidget(lbl_faults)

        fault_grid = QGridLayout()
        fault_grid.setSpacing(6)

        btn_failure   = self._fault_btn("⚡  Inject failure",    "alarm",    lambda: self._inject("failure"))
        btn_maint     = self._fault_btn("🔧  Maintenance",        "prealarm", lambda: self._inject("maintenance"))
        btn_nodata    = self._fault_btn("⛔  NoData (32764)",     "stopped",  lambda: self._inject("nodata"))
        btn_overload  = self._fault_btn("⚠  Overload (32760)",   "prealarm", lambda: self._inject("overload"))
        btn_reset     = self._fault_btn("↺  Reset to normal",    "normal",   lambda: self._inject("reset"))

        fault_grid.addWidget(btn_failure,  0, 0)
        fault_grid.addWidget(btn_maint,    0, 1)
        fault_grid.addWidget(btn_nodata,   1, 0)
        fault_grid.addWidget(btn_overload, 1, 1)
        fault_grid.addWidget(btn_reset,    2, 0, 1, 2)
        ts.addLayout(fault_grid)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setStyleSheet(f"color: {CLR['border']};")
        ts.addWidget(sep3)

        # Intermittent fault
        lbl_inter = QLabel("Intermittent fault")
        lbl_inter.setFont(QFont("Consolas", 11, QFont.Bold))
        lbl_inter.setStyleSheet(f"color: {CLR['subtext']};")
        ts.addWidget(lbl_inter)

        inter_row = QHBoxLayout()
        self.btn_inter_on  = QPushButton("ON")
        self.btn_inter_off = QPushButton("OFF")
        for btn in (self.btn_inter_on, self.btn_inter_off):
            btn.setFont(QFont("Consolas", 10, QFont.Bold))
            btn.setFixedHeight(24)
            btn.setFixedWidth(52)
            btn.setCursor(Qt.PointingHandCursor)
        self.btn_inter_on.clicked.connect(lambda: self._set_intermittent(True))
        self.btn_inter_off.clicked.connect(lambda: self._set_intermittent(False))

        lbl_freq = QLabel("Freq (ticks):")
        lbl_freq.setFont(QFont("Consolas", 10))
        lbl_freq.setStyleSheet(f"color: {CLR['subtext']};")

        self.slider_freq = QSlider(Qt.Horizontal)
        self.slider_freq.setMinimum(1)
        self.slider_freq.setMaximum(10)
        self.slider_freq.setValue(3)
        self.slider_freq.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height:4px; background:{CLR['overlay']}; border-radius:2px; }}
            QSlider::handle:horizontal {{ width:12px;height:12px;margin:-4px 0;background:{CLR['yellow']};border-radius:6px; }}
            QSlider::sub-page:horizontal {{ background:{CLR['yellow']};border-radius:2px; }}
        """)
        self.slider_freq.valueChanged.connect(lambda v: setattr(self.ms, "intermittent_freq", v))

        self.lbl_freq_val = QLabel("3")
        self.lbl_freq_val.setFont(QFont("Consolas", 11, QFont.Bold))
        self.lbl_freq_val.setStyleSheet(f"color: {CLR['yellow']};")
        self.lbl_freq_val.setFixedWidth(20)
        self.slider_freq.valueChanged.connect(lambda v: self.lbl_freq_val.setText(str(v)))

        inter_row.addWidget(self.btn_inter_on)
        inter_row.addWidget(self.btn_inter_off)
        inter_row.addSpacing(8)
        inter_row.addWidget(lbl_freq)
        inter_row.addWidget(self.slider_freq, 1)
        inter_row.addWidget(self.lbl_freq_val)
        ts.addLayout(inter_row)
        ts.addStretch()

        self.tabs.addTab(tab_values, "Values")
        self.tabs.addTab(tab_status, "Status")
        root.addWidget(self.tabs)

        self._set_mode("auto")

    def _fault_btn(self, text: str, severity: str, slot) -> QPushButton:
        colors = {
            "alarm":    (CLR["red_dim"],    CLR["red"],    "#5f1f2a"),
            "prealarm": (CLR["yellow_dim"], CLR["yellow"], "#5f4a10"),
            "normal":   (CLR["green_dim"],  CLR["green"],  "#1a4f2a"),
            "stopped":  (CLR["overlay"],    CLR["subtext"], CLR["border"]),
        }
        bg, fg, border = colors.get(severity, (CLR["overlay"], CLR["text"], CLR["border"]))
        btn = QPushButton(text)
        btn.setFont(QFont("Consolas", 10))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(28)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {bg}; color: {fg};
                border: 1px solid {border}; border-radius: 4px; padding: 0 8px;
            }}
            QPushButton:hover {{ border-color: {fg}; }}
            QPushButton:pressed {{ opacity: 0.8; }}
        """)
        btn.clicked.connect(slot)
        return btn

    def _set_mode(self, mode: str):
        self.ms.mode = mode
        is_auto = (mode == "auto")
        self._style_mode_btn(self.btn_auto,   is_auto)
        self._style_mode_btn(self.btn_manual, not is_auto)
        self.combo_scenario.setEnabled(is_auto)
        self.row_vrms.set_enabled(not is_auto)
        self.row_apeak.set_enabled(not is_auto)
        self.row_temp.set_enabled(not is_auto)

    def _style_mode_btn(self, btn: QPushButton, active: bool):
        if active:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {CLR['blue_dim']}; color: {CLR['blue']};
                    border: 1px solid {CLR['blue']}; border-radius: 4px;
                }}
            """)
        else:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {CLR['overlay']}; color: {CLR['subtext']};
                    border: 1px solid {CLR['border']}; border-radius: 4px;
                }}
                QPushButton:hover {{ color: {CLR['text']}; border-color: {CLR['text']}; }}
            """)

    def _on_scenario_change(self, idx: int):
        self.ms.scenario_key = self.combo_scenario.itemData(idx)
        self.ms._drift_temp = 0.0
        self.ms._drift_vrms = 0.0

    def _on_ds_change(self, idx: int):
        self.ms.device_status = self.combo_ds.itemData(idx)

    def _set_running(self, val: bool):
        self.ms.running = val
        if not val:
            self.ms.vrms_mms = 0.0
        self._style_run_btn(val)

    def _style_run_btn(self, running: bool):
        on_style = f"background:{CLR['green_dim']};color:{CLR['green']};border:1px solid {CLR['green']};border-radius:4px;"
        off_style = f"background:{CLR['red_dim']};color:{CLR['red']};border:1px solid {CLR['red']};border-radius:4px;"
        dim_style = f"background:{CLR['overlay']};color:{CLR['subtext']};border:1px solid {CLR['border']};border-radius:4px;"
        self.btn_run_on.setFont(QFont("Consolas", 10, QFont.Bold))
        self.btn_run_off.setFont(QFont("Consolas", 10, QFont.Bold))
        self.btn_run_on.setStyleSheet(on_style if running else dim_style)
        self.btn_run_off.setStyleSheet(off_style if not running else dim_style)

    def _manual_set(self, param: str, val: float):
        if self.ms.mode != "manual":
            return
        if param == "vrms":
            self.ms.vrms_mms = round(val, 1)
        elif param == "apeak":
            self.ms.apeak_mg = int(val)
        elif param == "temp":
            self.ms.temp_c = round(val, 1)
        self.refresh()

    def _set_intermittent(self, val: bool):
        self.ms.intermittent = val
        on_style  = f"background:{CLR['yellow_dim']};color:{CLR['yellow']};border:1px solid {CLR['yellow']};border-radius:4px;"
        dim_style = f"background:{CLR['overlay']};color:{CLR['subtext']};border:1px solid {CLR['border']};border-radius:4px;"
        self.btn_inter_on.setStyleSheet(on_style if val else dim_style)
        self.btn_inter_off.setStyleSheet(dim_style if val else on_style.replace(CLR['yellow_dim'], CLR['red_dim']).replace(CLR['yellow'], CLR['red']))

    def _inject(self, fault_type: str):
        ms = self.ms
        if fault_type == "failure":
            ms.device_status = 4
            self.combo_ds.setCurrentIndex(4)
        elif fault_type == "maintenance":
            ms.device_status = 1
            self.combo_ds.setCurrentIndex(1)
        elif fault_type == "nodata":
            ms.nodata_vrms = ms.nodata_apeak = ms.nodata_temp = True
            ms._nodata_manual = True
        elif fault_type == "overload":
            ms.nodata_vrms  = True
            ms.nodata_apeak = True
            ms.nodata_temp  = True
            ms._nodata_manual = True
            ms._overload = True
        elif fault_type == "reset":
            ms.device_status = 0
            ms.nodata_vrms = ms.nodata_apeak = ms.nodata_temp = False
            ms._nodata_manual = False
            ms._overload = False
            ms.running = True
            ms.intermittent = False
            ms.vrms_mms = 2.0
            ms.apeak_mg = 200
            ms.temp_c   = 40.0
            ms._drift_temp = 0.0   # reset drift accumulators so scenarios restart cleanly
            ms._drift_vrms = 0.0
            ms._tick = 0
            self.combo_ds.setCurrentIndex(0)
            self._set_running(True)
            self._set_intermittent(False)
        self.refresh()

    def refresh(self):
        ms = self.ms
        state = ms.alarm_state
        any_nodata   = ms.nodata_vrms or ms.nodata_apeak or ms.nodata_temp
        is_overload  = ms._overload

        # Header color
        sc = STATE_COLORS.get(state, STATE_COLORS["normal"])
        self.lbl_icon.setStyleSheet(f"color: {sc['fg']};")

        # Badge — priority: Overload > NoData > device_status warning > alarm_state
        DS_BADGE = {
            1: ("MAINTENANCE",  CLR["yellow_dim"], CLR["yellow"], CLR["yellow"]),
            2: ("OUT OF SPEC",  CLR["red_dim"],    CLR["red"],    CLR["red"]),
            3: ("FUNC CHECK",   CLR["overlay"],    CLR["subtext"], CLR["gray"]),
        }
        if is_overload:
            self.lbl_badge.setText("OVERLOAD")
            self.lbl_badge.setStyleSheet(
                f"background: {CLR['yellow_dim']}; color: {CLR['yellow']}; "
                f"border: 1px solid {CLR['yellow']}; border-radius: 4px; padding: 0 6px;"
            )
        elif any_nodata:
            self.lbl_badge.setText("NO DATA")
            self.lbl_badge.setStyleSheet(
                f"background: {CLR['overlay']}; color: {CLR['subtext']}; "
                f"border: 1px solid {CLR['gray']}; border-radius: 4px; padding: 0 6px;"
            )
        elif ms.device_status in DS_BADGE and state not in ("alarm", "prealarm"):
            label, bg, fg, brd = DS_BADGE[ms.device_status]
            self.lbl_badge.setText(label)
            self.lbl_badge.setStyleSheet(
                f"background: {bg}; color: {fg}; "
                f"border: 1px solid {brd}; border-radius: 4px; padding: 0 6px;"
            )
        else:
            self.lbl_badge.setText(state.upper().replace("PREALARM", "PRE-ALARM"))
            self.lbl_badge.setStyleSheet(
                f"background: {sc['badge']}; color: {sc['badge_text']}; "
                f"border-radius: 4px; padding: 0 6px;"
            )

        # Running label — show fault type if applicable
        if is_overload:
            self.lbl_running.setText("⚠ OVERLOAD")
            self.lbl_running.setStyleSheet(f"color: {CLR['yellow']}; font-family: Consolas; font-size: 9px;")
        elif any_nodata:
            self.lbl_running.setText("⚠ NO DATA")
            self.lbl_running.setStyleSheet(f"color: {CLR['gray']}; font-family: Consolas; font-size: 9px;")
        elif ms.is_running:
            self.lbl_running.setText("● RUNNING")
            self.lbl_running.setStyleSheet(f"color: {CLR['green']}; font-family: Consolas; font-size: 9px;")
        else:
            self.lbl_running.setText("● STOPPED")
            self.lbl_running.setStyleSheet(f"color: {CLR['gray']}; font-family: Consolas; font-size: 9px;")

        # Param rows
        if ms.nodata_vrms:
            self.row_vrms.set_nodata()
        else:
            self.row_vrms.set_value(ms.vrms_mms, ms.param_state("vrms"))

        if ms.nodata_apeak:
            self.row_apeak.set_nodata()
        else:
            self.row_apeak.set_value(float(ms.apeak_mg), ms.param_state("apeak"))

        if ms.nodata_temp:
            self.row_temp.set_nodata()
        else:
            self.row_temp.set_value(ms.temp_c, ms.param_state("temp"))

        # Run buttons
        self._style_run_btn(ms.running)

        # Card border — dashed when NoData/Overload, colored for device_status warnings
        border_colors = {"normal": CLR["border"], "prealarm": "#BA7517", "alarm": "#A32D2D", "stopped": CLR["border"]}
        if is_overload:
            bc = CLR["yellow"]
            bs = "dashed"
        elif any_nodata:
            bc = CLR["gray"]
            bs = "dashed"
        elif ms.device_status == 1 and state not in ("alarm", "prealarm"):
            bc = CLR["yellow"]
            bs = "solid"
        elif ms.device_status == 2 and state not in ("alarm", "prealarm"):
            bc = CLR["red"]
            bs = "solid"
        elif ms.device_status == 3 and state not in ("alarm", "prealarm"):
            bc = CLR["gray"]
            bs = "solid"
        else:
            bc = border_colors.get(state, CLR["border"])
            bs = "solid"
        self.setStyleSheet(f"""
            QGroupBox {{
                background: {CLR['surface']};
                border: 1px {bs} {bc};
                border-radius: 8px;
                margin-top: 0px; padding: 0px;
            }}
        """)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class SimulatorWindow(QMainWindow):
    """Standalone main window for the motor simulator.

    Used when running motor_simulator.py directly. When embedded in the
    launcher, SimulatorWidget (in launcher.py) is used instead, which
    contains the same logic without the QMainWindow wrapper.
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MotorWatch IQ — Motor Simulator")
        self.setMinimumSize(860, 680)

        # Motor states
        self.motor_states = [
            MotorState(1, "normal"),
            MotorState(2, "thermal_drift"),
            MotorState(3, "vibration_drift"),
            MotorState(4, "impact_spike"),
        ]

        self._pub_count = 0

        # MQTT
        self.mqtt_worker = None
        self._mqtt_connected = False

        self._build_ui()
        self._apply_theme()

        # Auto tick timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        # ── Top bar ─────────────────────────────────────────────────────────
        top_bar = QHBoxLayout()

        lbl_title = QLabel("MotorWatch IQ")
        lbl_title.setFont(QFont("Consolas", 15, QFont.Bold))
        lbl_title.setStyleSheet(f"color: {CLR['blue']};")

        lbl_sub = QLabel("Motor Simulator  /  S7-1200 + IFM VVB306")
        lbl_sub.setFont(QFont("Consolas", 10))
        lbl_sub.setStyleSheet(f"color: {CLR['subtext']};")

        self.lbl_mqtt = QLabel("⬤  MQTT: connecting…")
        self.lbl_mqtt.setFont(QFont("Consolas", 10))
        self.lbl_mqtt.setStyleSheet(f"color: {CLR['gray']};")

        top_bar.addWidget(lbl_title)
        top_bar.addSpacing(12)
        top_bar.addWidget(lbl_sub)
        top_bar.addStretch()
        top_bar.addWidget(self.lbl_mqtt)
        root.addLayout(top_bar)

        # ── Motors grid 2×2 ─────────────────────────────────────────────────
        self.motor_cards = []
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, ms in enumerate(self.motor_states):
            card = MotorCard(ms)
            self.motor_cards.append(card)
            grid.addWidget(card, i // 2, i % 2)
        root.addLayout(grid)

        # ── Bottom bar ──────────────────────────────────────────────────────
        bottom = QFrame()
        bottom.setFixedHeight(44)
        bottom.setStyleSheet(f"""
            QFrame {{
                background: {CLR['surface']};
                border: 1px solid {CLR['border']};
                border-radius: 6px;
            }}
        """)
        bot_layout = QHBoxLayout(bottom)
        bot_layout.setContentsMargins(14, 0, 14, 0)

        lbl_interval = QLabel("Publish interval:")
        lbl_interval.setFont(QFont("Consolas", 11))
        lbl_interval.setStyleSheet(f"color: {CLR['subtext']};")

        self.slider_interval = QSlider(Qt.Horizontal)
        self.slider_interval.setMinimum(500)
        self.slider_interval.setMaximum(5000)
        self.slider_interval.setSingleStep(500)
        self.slider_interval.setValue(1000)
        self.slider_interval.setFixedWidth(160)
        self.slider_interval.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height:4px; background:{CLR['overlay']}; border-radius:2px; }}
            QSlider::handle:horizontal {{ width:14px;height:14px;margin:-5px 0;background:{CLR['blue']};border-radius:7px; }}
            QSlider::sub-page:horizontal {{ background:{CLR['blue']};border-radius:2px; }}
        """)
        self.slider_interval.valueChanged.connect(self._on_interval_change)

        self.lbl_interval_val = QLabel("1000 ms")
        self.lbl_interval_val.setFont(QFont("Consolas", 11, QFont.Bold))
        self.lbl_interval_val.setStyleSheet(f"color: {CLR['text']};")
        self.lbl_interval_val.setFixedWidth(70)

        self.btn_publish = QPushButton("▶  Publish now")
        self.btn_publish.setFont(QFont("Consolas", 11, QFont.Bold))
        self.btn_publish.setFixedHeight(30)
        self.btn_publish.setCursor(Qt.PointingHandCursor)
        self.btn_publish.setStyleSheet(f"""
            QPushButton {{
                background: {CLR['blue_dim']}; color: {CLR['blue']};
                border: 1px solid {CLR['blue']}; border-radius: 5px; padding: 0 14px;
            }}
            QPushButton:hover {{ background: #1a3060; }}
            QPushButton:pressed {{ background: #0e1e40; }}
        """)
        self.btn_publish.clicked.connect(self._force_publish)

        self.lbl_pub_count = QLabel("Published: 0")
        self.lbl_pub_count.setFont(QFont("Consolas", 10))
        self.lbl_pub_count.setStyleSheet(f"color: {CLR['subtext']};")

        bot_layout.addWidget(lbl_interval)
        bot_layout.addSpacing(8)
        bot_layout.addWidget(self.slider_interval)
        bot_layout.addWidget(self.lbl_interval_val)
        bot_layout.addStretch()
        bot_layout.addWidget(self.lbl_pub_count)
        bot_layout.addSpacing(12)
        bot_layout.addWidget(self.btn_publish)
        root.addWidget(bottom)

    def _apply_theme(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background: {CLR['bg']};
                color: {CLR['text']};
                font-family: Consolas;
            }}
            QTabWidget {{ background: transparent; }}
        """)

    def _mqtt_connect(self):
        if not MQTT_AVAILABLE:
            self.lbl_mqtt.setText("⬤  MQTT: paho-mqtt not installed")
            self.lbl_mqtt.setStyleSheet(f"color: {CLR['gray']};")
            return
        self.mqtt_worker = MqttWorker("localhost", 1883)
        self.mqtt_worker.connection_changed.connect(self._on_mqtt_status)
        self.mqtt_worker.start()

    def _on_mqtt_status(self, connected: bool, msg: str):
        self._mqtt_connected = connected
        if connected:
            self.lbl_mqtt.setText("⬤  MQTT: connected")
            self.lbl_mqtt.setStyleSheet(f"color: {CLR['green']};")
        else:
            self.lbl_mqtt.setText(f"⬤  MQTT: {msg}")
            self.lbl_mqtt.setStyleSheet(f"color: {CLR['red']};")

    def _on_interval_change(self, val: int):
        rounded = round(val / 500) * 500
        self.lbl_interval_val.setText(f"{rounded} ms")
        self._timer.setInterval(rounded)

    def _tick(self):
        for ms in self.motor_states:
            ms.tick_auto()
        self._publish_all()
        for card in self.motor_cards:
            card.refresh()

    def _publish_all(self):
        if not self._mqtt_connected or not self.mqtt_worker:
            return
        for ms in self.motor_states:
            payload = ms.to_mqtt_payload()
            topic = f"motorwatch/motors/{ms.motor_id}/telemetry"
            self.mqtt_worker.publish(topic, payload)
            # Individual topics
            self.mqtt_worker.publish(f"motorwatch/motors/{ms.motor_id}/vrms_magnitude",
                                     {"value": payload["vrms_magnitude_mms"], "unit": "mm/s"})
            self.mqtt_worker.publish(f"motorwatch/motors/{ms.motor_id}/apeak_magnitude",
                                     {"value": payload["apeak_magnitude_mg"], "unit": "mg"})
            self.mqtt_worker.publish(f"motorwatch/motors/{ms.motor_id}/temperature",
                                     {"value": payload["temperature_c"], "unit": "C"})
            self.mqtt_worker.publish(f"motorwatch/motors/{ms.motor_id}/alarm",
                                     {"state": payload["alarm_state"]})
        self._pub_count += 1
        self.lbl_pub_count.setText(f"Published: {self._pub_count}")

    def _force_publish(self):
        for ms in self.motor_states:
            ms.tick_auto()
        self._publish_all()
        for card in self.motor_cards:
            card.refresh()
        self.btn_publish.setStyleSheet(f"""
            QPushButton {{
                background: {CLR['green_dim']}; color: {CLR['green']};
                border: 1px solid {CLR['green']}; border-radius: 5px; padding: 0 14px;
            }}
        """)
        QTimer.singleShot(300, self._reset_pub_btn)

    def _reset_pub_btn(self):
        self.btn_publish.setStyleSheet(f"""
            QPushButton {{
                background: {CLR['blue_dim']}; color: {CLR['blue']};
                border: 1px solid {CLR['blue']}; border-radius: 5px; padding: 0 14px;
            }}
            QPushButton:hover {{ background: #1a3060; }}
            QPushButton:pressed {{ background: #0e1e40; }}
        """)

    def closeEvent(self, event):
        self._timer.stop()
        if self.mqtt_worker:
            self.mqtt_worker.disconnect()
            self.mqtt_worker.quit()
            self.mqtt_worker.wait(2000)
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = SimulatorWindow()
    window.show()
    sys.exit(app.exec())