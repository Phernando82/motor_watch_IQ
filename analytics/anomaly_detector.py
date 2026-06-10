"""
MotorWatch IQ — Layer 6: Anomaly Detector
==========================================
Standalone process — runs every 30 seconds.

Pipeline per cycle:
  1. Query last 60 min of motor_telemetry from InfluxDB — Motor 1 normal
     baseline used to train Isolation Forest at startup (same scale as live data)
  2. Extract features per motor: [vrms_rms, vrms_peak, vrms_crest, apeak_mean, temp_norm]
  3. Isolation Forest inference → anomaly_score 0.0–1.0
  4. Trend analysis → slope mm/s/h + ETA to prealarm/alarm
  5. ISO 20816-3 rules engine → deterministic alert_level
  6. Combine ML + rules + trend → final alert_level + alert_message
  7. Write measurement motor_anomaly to InfluxDB
  8. Log summary to console

Training strategy:
  Two models trained at startup:

  Model 1 — Isolation Forest (anomaly_score 0.0–1.0):
    CWRU normal baseline (28 files, 4 speeds) + live Motor 1 normal data.
    Contamination calculated from actual fault/normal ratio in dataset.

  Model 2 — RandomForestClassifier (fault_type + fault_prob):
    CWRU normal + inner_race + outer_race + ball_fault (Drive End + Fan End).
    5-fold cross-validation reported at startup.

  Feature space — 7 adimensional features (valid for g and mm/s):
    crest_factor, kurtosis, skewness, shape_factor,
    impulse_factor, rms_norm, temp_norm

  Models are cached to analytics/models/ after first training.
  On subsequent starts they load from disk (fast) unless cache is missing.

Measurement: motor_anomaly
  Tags:   motor_id
  Fields: anomaly_score  (float 0.0–1.0)
          fault_type     (string: normal|inner_race|outer_race|ball_fault|unknown)
          fault_prob     (float 0.0–1.0, classifier confidence)
          trend_slope    (float mm/s/h)
          eta_prealarm_h (float, -1 if not applicable)
          eta_alarm_h    (float, -1 if not applicable)
          alert_level    (int 0=ok 1=watch 2=warning 3=critical)
          alert_message  (string)

Run:
    python analytics/anomaly_detector.py

Requires .env in project root:
    INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from sklearn.ensemble import IsolationForest

# ── analytics package path ──────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from analytics.cwru_trainer import (
    build_dataset, train_models, load_models,
    extract_features_from_live, PREALARM_RMS_REF,
)
from analytics.series_trainer import (
    train_series_classifier, load_series_classifier,
    classify_series, extract_series_features,
)

# ── project root .env ─────────────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("anomaly_detector")

# ── config ────────────────────────────────────────────────────────────────────
INFLUX_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "motorwatch")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "motors")

CYCLE_INTERVAL_S  = 30      # seconds between inference cycles
HISTORY_WINDOW    = "-30m"  # InfluxDB range for trend analysis
ANOMALY_WINDOW    = "-5m"   # shorter window for real-time anomaly features
TRAINING_WINDOW   = "-60m"  # window for Motor 1 baseline training data
MIN_POINTS        = 5       # minimum points to run inference
MIN_TRAIN_POINTS  = 20      # minimum Motor 1 points to train IF

# ISO 20816-3 thresholds — valores default (fallback se settings_loader falhar)
PREALARM_VRMS  =  7.1    # mm/s
ALARM_VRMS     = 11.2    # mm/s
PREALARM_APEAK = 1000.0  # mg
ALARM_APEAK    = 2000.0  # mg
PREALARM_TEMP  =  65.0   # °C
ALARM_TEMP     =  75.0   # °C


def _get_thresholds(motor_id: str) -> dict:
    """Thresholds efectivos para um motor — custom ou ISO default via settings_loader."""
    try:
        from settings_loader import get_thresholds
        return get_thresholds(motor_id)
    except Exception:
        return {
            "vrms_prealarm_mms":  PREALARM_VRMS,
            "vrms_alarm_mms":     ALARM_VRMS,
            "apeak_prealarm_mg":  PREALARM_APEAK,
            "apeak_alarm_mg":     ALARM_APEAK,
            "temp_prealarm_c":    PREALARM_TEMP,
            "temp_alarm_c":       ALARM_TEMP,
        }

# IF score thresholds
ML_WARNING_SCORE = 0.6   # score >= this → WATCH
ML_ALERT_SCORE   = 0.75  # score >= this → WARNING

# Trend thresholds
TREND_WATCH_MMS_H   = 0.5   # mm/s/h → WATCH
TREND_WARNING_MMS_H = 2.0   # mm/s/h → WARNING
ETA_MAX_HOURS       = 500.0 # ETAs above this are suppressed as meaningless

# ── alert levels ──────────────────────────────────────────────────────────────
ALERT_OK       = 0
ALERT_WATCH    = 1
ALERT_WARNING  = 2
ALERT_CRITICAL = 3

ALERT_LABELS = {
    ALERT_OK:       "OK",
    ALERT_WATCH:    "WATCH",
    ALERT_WARNING:  "WARNING",
    ALERT_CRITICAL: "CRITICAL",
}


# ─────────────────────────────────────────────────────────────────────────────
# InfluxDB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_client() -> InfluxDBClient:
    return InfluxDBClient(
        url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=10_000
    )


def _query_series(
    client: InfluxDBClient,
    motor_id: str,
    field: str,
    window: str,
) -> tuple[list[datetime], list[float]]:
    """Return (times, values) for a field, excluding sentinel -1.0 points."""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {window})
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) => r.motor_id == "{motor_id}")
  |> filter(fn: (r) => r._field == "{field}")
  |> filter(fn: (r) => r._value >= 0.0)
  |> sort(columns: ["_time"])
"""
    tables = client.query_api().query(flux, org=INFLUX_ORG)
    times, values = [], []
    for table in tables:
        for record in table.records:
            times.append(record.get_time())
            values.append(float(record.get_value()))
    return times, values


def _query_last(
    client: InfluxDBClient,
    motor_id: str,
    field: str,
) -> float | None:
    """Return the most recent valid value for a field, or None."""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) => r.motor_id == "{motor_id}")
  |> filter(fn: (r) => r._field == "{field}")
  |> filter(fn: (r) => r._value >= 0.0)
  |> last()
"""
    tables = client.query_api().query(flux, org=INFLUX_ORG)
    for table in tables:
        for record in table.records:
            return float(record.get_value())
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Isolation Forest — train on Motor 1 normal baseline (same units as live data)
# ─────────────────────────────────────────────────────────────────────────────

# _build_feature_vector replaced by cwru_trainer.extract_features_from_live
# which uses the 7 adimensional features consistent with CWRU training space.


def _get_live_normal_features(client: InfluxDBClient) -> np.ndarray | None:
    """
    Extract adimensional feature rows from Motor 1 normal baseline (InfluxDB).
    Used to augment CWRU normal data in IF training.
    Returns ndarray (n_windows, 7) or None if insufficient data.
    """
    logger.info("Querying Motor 1 live baseline for IF augmentation …")

    _, vrms_vals  = _query_series(client, "1", "vrms_magnitude_mms", TRAINING_WINDOW)
    _, apeak_vals = _query_series(client, "1", "apeak_magnitude_mg", TRAINING_WINDOW)
    last_temp     = _query_last(client, "1", "temperature_c")

    if len(vrms_vals) < MIN_TRAIN_POINTS:
        logger.warning(
            "Motor 1 baseline: only %d points (need %d) — IF trains on CWRU only.",
            len(vrms_vals), MIN_TRAIN_POINTS,
        )
        return None

    t_norm = (last_temp / ALARM_TEMP) if last_temp else 0.4
    rows   = []
    step   = max(1, len(vrms_vals) // 40)  # ~40 windows

    for start in range(0, len(vrms_vals) - MIN_POINTS, step):
        end    = min(start + MIN_POINTS * 4, len(vrms_vals))
        v_win  = vrms_vals[start:end]
        a_win  = apeak_vals[start:end] if len(apeak_vals) >= end else []
        fv = extract_features_from_live(v_win, a_win, t_norm)
        if fv is not None:
            rows.append(fv[0])

    if not rows:
        return None

    X = np.array(rows, dtype=np.float32)
    logger.info("Motor 1 live: %d feature windows extracted", len(X))
    return X


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction from live InfluxDB data
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MotorFeatures:
    motor_id:   str
    vrms_rms:   float
    vrms_peak:  float
    vrms_crest: float
    apeak_mean: float
    temp_norm:  float
    n_points:   int
    valid:      bool
    # raw last values for ISO rules engine
    last_vrms:  float = 0.0
    last_apeak: float = 0.0
    last_temp:  float = 0.0


def extract_motor_features(
    client: InfluxDBClient, motor_id: str
) -> MotorFeatures:
    """
    Extract 7 adimensional features from last 5 minutes of motor data.
    Feature space matches CWRU training space — valid across g and mm/s.
    """
    _, vrms_vals  = _query_series(client, motor_id, "vrms_magnitude_mms", ANOMALY_WINDOW)
    _, apeak_vals = _query_series(client, motor_id, "apeak_magnitude_mg", ANOMALY_WINDOW)

    last_vrms  = _query_last(client, motor_id, "vrms_magnitude_mms")
    last_apeak = _query_last(client, motor_id, "apeak_magnitude_mg")
    last_temp  = _query_last(client, motor_id, "temperature_c")

    t_norm = (last_temp / ALARM_TEMP) if last_temp is not None else 0.4
    n      = len(vrms_vals)

    fv = extract_features_from_live(vrms_vals, apeak_vals, t_norm)

    if fv is None:
        return MotorFeatures(
            motor_id=motor_id, vrms_rms=0.0, vrms_peak=0.0,
            vrms_crest=0.0, apeak_mean=0.0, temp_norm=t_norm,
            n_points=n, valid=False,
            last_vrms=last_vrms or 0.0,
            last_apeak=last_apeak or 0.0,
            last_temp=last_temp or 0.0,
        )

    # fv shape (1,7): [crest, kurt, skew, shape, impulse, rms_norm, temp_norm]
    return MotorFeatures(
        motor_id=motor_id,
        vrms_rms=float(fv[0, 5]),    # rms_norm as proxy for energy level
        vrms_peak=float(fv[0, 0]),   # crest_factor as proxy for peak
        vrms_crest=float(fv[0, 0]),  # crest_factor
        apeak_mean=float(fv[0, 4]),  # impulse_factor
        temp_norm=float(fv[0, 6]),
        n_points=n, valid=True,
        last_vrms=last_vrms or 0.0,
        last_apeak=last_apeak or 0.0,
        last_temp=last_temp or 0.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ISO 20816-3 rules engine
# ─────────────────────────────────────────────────────────────────────────────

def iso_rules_alert(f: MotorFeatures, thr: dict | None = None) -> tuple[int, str]:
    """
    Deterministic alert based on thresholds (custom or ISO 20816-3 defaults).
    Returns (alert_level, message). Priority: alarm > prealarm > ok.
    thr: dict from _get_thresholds(motor_id); uses global ISO defaults if None.
    """
    if thr is None:
        thr = {
            "vrms_prealarm_mms":  PREALARM_VRMS,  "vrms_alarm_mms":    ALARM_VRMS,
            "apeak_prealarm_mg":  PREALARM_APEAK,  "apeak_alarm_mg":    ALARM_APEAK,
            "temp_prealarm_c":    PREALARM_TEMP,   "temp_alarm_c":      ALARM_TEMP,
        }
    reasons = []
    level   = ALERT_OK

    if f.last_vrms >= thr["vrms_alarm_mms"]:
        level = ALERT_CRITICAL
        reasons.append(f"v-RMS {f.last_vrms:.2f} mm/s ≥ alarm {thr['vrms_alarm_mms']}")
    elif f.last_vrms >= thr["vrms_prealarm_mms"]:
        level = max(level, ALERT_WARNING)
        reasons.append(f"v-RMS {f.last_vrms:.2f} mm/s ≥ prealarm {thr['vrms_prealarm_mms']}")

    if f.last_apeak >= thr["apeak_alarm_mg"]:
        level = ALERT_CRITICAL
        reasons.append(f"a-Peak {f.last_apeak:.0f} mg ≥ alarm {thr['apeak_alarm_mg']:.0f}")
    elif f.last_apeak >= thr["apeak_prealarm_mg"]:
        level = max(level, ALERT_WARNING)
        reasons.append(f"a-Peak {f.last_apeak:.0f} mg ≥ prealarm {thr['apeak_prealarm_mg']:.0f}")

    if f.last_temp >= thr["temp_alarm_c"]:
        level = ALERT_CRITICAL
        reasons.append(f"Temp {f.last_temp:.1f}°C ≥ alarm {thr['temp_alarm_c']:.0f}°C")
    elif f.last_temp >= thr["temp_prealarm_c"]:
        level = max(level, ALERT_WARNING)
        reasons.append(f"Temp {f.last_temp:.1f}°C ≥ prealarm {thr['temp_prealarm_c']:.0f}°C")

    if not reasons:
        return ALERT_OK, "All parameters within ISO 20816-3 limits"

    return level, " | ".join(reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly score — Isolation Forest
# ─────────────────────────────────────────────────────────────────────────────

def compute_anomaly_score(clf: IsolationForest, f: MotorFeatures, thr: dict | None = None) -> float:
    """
    Map IF decision_function to 0.0–1.0 using 7 adimensional features.
    score = clip(0.5 - raw, 0.0, 1.0)
      raw > 0  → normal  → score < 0.5
      raw < 0  → anomaly → score > 0.5
    thr: thresholds dict — used for correct t_norm per motor.
    """
    if not f.valid:
        return 0.0
    _, _, _, last_vrms, last_apeak, last_temp = (
        f.vrms_rms, f.vrms_peak, f.vrms_crest, f.last_vrms, f.last_apeak, f.last_temp
    )
    alarm_temp = thr.get("temp_alarm_c", ALARM_TEMP) if thr else ALARM_TEMP
    t_norm = (last_temp / alarm_temp) if last_temp > 0 else 0.4
    fv = extract_features_from_live(
        [last_vrms] * 6, [last_apeak] * 6, t_norm
    )
    if fv is None:
        return 0.0
    raw = float(clf.decision_function(fv)[0])
    return float(np.clip(0.5 - raw, 0.0, 1.0))


def compute_fault_type(
    clf_rf: RandomForestClassifier,
    le: LabelEncoder,
    f: MotorFeatures,
    thr: dict | None = None,
) -> tuple[str, float]:
    """
    Classify fault type using RandomForest.
    Returns (fault_type_str, probability).
    thr: thresholds dict — used for correct t_norm per motor.
    """
    if not f.valid or clf_rf is None:
        return "unknown", 0.0
    alarm_temp = thr.get("temp_alarm_c", ALARM_TEMP) if thr else ALARM_TEMP
    t_norm = (f.last_temp / alarm_temp) if f.last_temp > 0 else 0.4
    fv = extract_features_from_live(
        [f.last_vrms] * 6, [f.last_apeak] * 6, t_norm
    )
    if fv is None:
        return "unknown", 0.0
    proba     = clf_rf.predict_proba(fv)[0]
    class_idx = int(np.argmax(proba))
    fault_str = str(le.classes_[class_idx])
    prob      = float(proba[class_idx])
    return fault_str, prob


# ─────────────────────────────────────────────────────────────────────────────
# Trend analysis
# ─────────────────────────────────────────────────────────────────────────────

def compute_trend(
    client: InfluxDBClient, motor_id: str, thr: dict | None = None
) -> tuple[float, float, float]:
    """
    Linear regression on last 30 min of vrms_magnitude_mms.
    Returns (slope_mms_h, eta_prealarm_h, eta_alarm_h).
    thr: thresholds dict from _get_thresholds(motor_id).

    ETA rules:
        -1.0 if current already at or above threshold
        -1.0 if slope <= 0.01 mm/s/h (no meaningful trend)
        -1.0 if projected ETA > ETA_MAX_HOURS (meaningless projection)
    """
    if thr is None:
        thr = {"vrms_prealarm_mms": PREALARM_VRMS, "vrms_alarm_mms": ALARM_VRMS}
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {HISTORY_WINDOW})
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) => r.motor_id == "{motor_id}")
  |> filter(fn: (r) => r._field == "vrms_magnitude_mms")
  |> filter(fn: (r) => r._value >= 0.0)
  |> sort(columns: ["_time"])
"""
    tables   = client.query_api().query(flux, org=INFLUX_ORG)
    times_dt, vals = [], []
    for table in tables:
        for record in table.records:
            times_dt.append(record.get_time())
            vals.append(float(record.get_value()))

    if len(vals) < 10:
        return 0.0, -1.0, -1.0

    t0     = times_dt[0]
    t_h    = np.array([(t - t0).total_seconds() / 3600.0 for t in times_dt])
    v      = np.array(vals, dtype=np.float64)
    coeffs = np.polyfit(t_h, v, 1)
    slope   = float(coeffs[0])
    current = float(v[-1])

    def eta(threshold: float) -> float:
        if current >= threshold:
            return -1.0                      # already at or above — not applicable
        if slope <= 0.05:
            return -1.0                      # no meaningful upward trend
        hours = (threshold - current) / slope
        if hours > ETA_MAX_HOURS:
            return -1.0                      # too far out to be actionable
        return round(hours, 1)

    return slope, eta(thr["vrms_prealarm_mms"]), eta(thr["vrms_alarm_mms"])


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DetectionResult:
    motor_id:       str
    anomaly_score:  float
    trend_slope:    float
    eta_prealarm_h: float
    eta_alarm_h:    float
    alert_level:    int
    alert_message:  str
    last_vrms:      float = 0.0
    last_apeak:     float = 0.0
    last_temp:      float = 0.0
    fault_type:     str   = "unknown"
    fault_prob:     float = 0.0
    timestamp:      datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    def log_line(self) -> str:
        label = ALERT_LABELS.get(self.alert_level, "?")
        score = f"score={self.anomaly_score:.3f}"
        fault = f"fault={self.fault_type}({self.fault_prob:.2f})"
        slope = f"slope={self.trend_slope:+.3f}mm/s/h"
        eta   = ""
        if self.eta_prealarm_h >= 0:
            eta = f" ETA_prealarm={self.eta_prealarm_h:.1f}h"
        elif self.eta_alarm_h >= 0:
            eta = f" ETA_alarm={self.eta_alarm_h:.1f}h"
        return f"Motor {self.motor_id}  [{label:8s}]  {score}  {fault}  {slope}{eta}  — {self.alert_message}"


# ─────────────────────────────────────────────────────────────────────────────
# Main detector
# ─────────────────────────────────────────────────────────────────────────────

def _get_vrms_series(client: InfluxDBClient, motor_id: str) -> list[float]:
    """Return list of vrms values from ANOMALY_WINDOW for series classification."""
    _, vals = _query_series(client, motor_id, "vrms_magnitude_mms", ANOMALY_WINDOW)
    return vals


def _get_apeak_series(client: InfluxDBClient, motor_id: str) -> list[float]:
    """Return list of apeak values from ANOMALY_WINDOW for series classification."""
    _, vals = _query_series(client, motor_id, "apeak_magnitude_mg", ANOMALY_WINDOW)
    return vals


class AnomalyDetector:

    def __init__(self, client: InfluxDBClient):
        self._client     = client
        self._write      = client.write_api(write_options=SYNCHRONOUS)
        self._clf_if     = None   # Isolation Forest (CWRU features)
        self._clf_rf     = None   # RandomForest classifier (CWRU features — unused for fault type)
        self._le         = None   # LabelEncoder for CWRU RF
        self._clf_series = None   # Series RF (aggregated InfluxDB features)
        self._le_series  = None   # LabelEncoder for series RF
        self._train()

    def _train(self) -> None:
        """Train or load both models. Falls back to ISO-only if training fails."""
        # Try loading cached models first
        cached = load_models()
        if cached is not None:
            self._clf_if, self._clf_rf, self._le, meta = cached
            logger.info(
                "Models loaded from cache — RF acc=%.3f  contamination=%.3f",
                meta["cv_accuracy"], meta["contamination"],
            )
            # Augment IF with fresh Motor 1 live data
            live_rows = _get_live_normal_features(self._client)
            if live_rows is not None:
                from analytics.cwru_trainer import build_dataset
                dataset = build_dataset()
                self._clf_if, self._clf_rf, self._le, _ = train_models(
                    dataset, live_rows
                )
            # Fall through to load Series RF below (do NOT return early)

        else:
            # No cache — full training
            live_rows = _get_live_normal_features(self._client)
            try:
                dataset = build_dataset()
                self._clf_if, self._clf_rf, self._le, meta = train_models(
                    dataset, live_rows
                )
                logger.info(
                    "Training complete — RF CV acc=%.3f ± %.3f  contamination=%.3f",
                    meta["cv_accuracy"], meta["cv_std"], meta["contamination"],
                )
            except Exception as exc:
                logger.error("Model training failed: %s — running ISO-only", exc)

        # Series RF — always load/train regardless of CWRU cache state
        cached_series = load_series_classifier()
        if cached_series is not None:
            self._clf_series, self._le_series, _ = cached_series
        else:
            logger.info("Training series RF classifier …")
            try:
                self._clf_series, self._le_series, smeta = train_series_classifier()
                logger.info("Series RF trained — acc=%.3f", smeta["cv_accuracy"])
            except Exception as exc:
                logger.error("Series RF training failed: %s", exc)

    def run_cycle(self) -> list[DetectionResult]:
        results = []
        for motor_id in ("1", "2", "3", "4"):
            result = self._analyse_motor(motor_id)
            results.append(result)
            self._write_result(result)
            logger.info(result.log_line())
        return results

    def close(self):
        self._client.close()

    # ── private ───────────────────────────────────────────────────────────────

    def _analyse_motor(self, motor_id: str) -> DetectionResult:
        # 0. Load effective thresholds for this motor (custom or ISO default)
        thr = _get_thresholds(motor_id)

        # 1. Features
        features = extract_motor_features(self._client, motor_id)

        # 2. ML score (0.0 if no model)
        # t_norm uses the motor's own alarm threshold for correct normalisation
        anomaly_score = (
            compute_anomaly_score(self._clf_if, features, thr)
            if self._clf_if is not None else 0.0
        )

        # 2b. Fault classification via series RF (aggregated InfluxDB features)
        alarm_temp = thr.get("temp_alarm_c", ALARM_TEMP)
        t_norm_live = (features.last_temp / alarm_temp) if features.last_temp > 0 else 0.4
        if self._clf_series is not None:
            fault_type, fault_prob = classify_series(
                self._clf_series, self._le_series,
                vrms_vals=_get_vrms_series(self._client, motor_id),
                apeak_vals=_get_apeak_series(self._client, motor_id),
                temp_norm=t_norm_live,
            )
        else:
            fault_type, fault_prob = "unknown", 0.0

        # 3. Trend — uses motor's vrms thresholds for ETA calculation
        slope, eta_prealarm, eta_alarm = compute_trend(self._client, motor_id, thr)

        # 4. ISO rules — deterministic, uses motor's custom thresholds
        iso_level, iso_msg = iso_rules_alert(features, thr)

        # 5. ML level — only upgrades if ISO does not already flag CRITICAL
        ml_level = ALERT_OK
        if iso_level < ALERT_CRITICAL and self._clf_if is not None:
            if anomaly_score >= ML_ALERT_SCORE:
                ml_level = ALERT_WARNING
            elif anomaly_score >= ML_WARNING_SCORE:
                ml_level = ALERT_WATCH

        # 6. Trend level
        trend_level = ALERT_OK
        if slope > TREND_WARNING_MMS_H:
            trend_level = ALERT_WARNING
        elif slope > TREND_WATCH_MMS_H:
            trend_level = ALERT_WATCH

        # 7. Final level
        final_level = max(iso_level, ml_level, trend_level)

        # 8. Message — ETA shown only when below threshold (not already in alarm)
        if final_level == ALERT_OK:
            message = "Normal operation"
        else:
            parts = []
            if iso_level >= ALERT_WARNING:
                parts.append(f"ISO: {iso_msg}")
            if ml_level >= ALERT_WATCH:
                parts.append(f"ML score {anomaly_score:.2f}")
            if trend_level >= ALERT_WATCH:
                parts.append(f"Trend {slope:+.2f} mm/s/h")
            # Only show ETA if not already at CRITICAL from ISO rules
            if iso_level < ALERT_CRITICAL and eta_prealarm >= 0:
                parts.append(f"prealarm in {eta_prealarm:.1f}h")
            message = " | ".join(parts) if parts else iso_msg

        return DetectionResult(
            motor_id=motor_id,
            anomaly_score=anomaly_score,
            trend_slope=slope,
            eta_prealarm_h=eta_prealarm,
            eta_alarm_h=eta_alarm,
            alert_level=final_level,
            alert_message=message,
            last_vrms=features.last_vrms,
            last_apeak=features.last_apeak,
            last_temp=features.last_temp,
            fault_type=fault_type,
            fault_prob=fault_prob,
        )

    def _write_result(self, r: DetectionResult) -> None:
        point = (
            Point("motor_anomaly")
            .tag("motor_id", r.motor_id)
            .field("anomaly_score",  r.anomaly_score)
            .field("fault_type",     r.fault_type)
            .field("fault_prob",     r.fault_prob)
            .field("trend_slope",    r.trend_slope)
            .field("eta_prealarm_h", r.eta_prealarm_h)
            .field("eta_alarm_h",    r.eta_alarm_h)
            .field("alert_level",    r.alert_level)
            .field("alert_message",  r.alert_message)
            .time(r.timestamp, WritePrecision.NS)
        )
        try:
            self._write.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
        except Exception as exc:
            logger.error("InfluxDB write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not INFLUX_TOKEN:
        logger.error("INFLUX_TOKEN not set — check .env in project root")
        sys.exit(1)

    from analytics.alert_manager import AlertManager
    from analytics.report_generator import ReportGenerator

    logger.info("AnomalyDetector starting — cycle interval %ds", CYCLE_INTERVAL_S)
    client    = _make_client()
    detector  = AnomalyDetector(client)
    manager   = AlertManager()
    generator = ReportGenerator()

    try:
        cycle = 0
        while True:
            cycle += 1
            logger.info("── Cycle %d ─────────────────────────────────", cycle)
            try:
                results = detector.run_cycle()
                events  = manager.process(results)

                # Log state-change events only (sustained alerts are silent)
                for event in events:
                    logger.info("   %s", event.description)

                # Log summary every cycle so status is always visible
                logger.info("   %s", manager.summary())

                # Auto-generate report when trigger conditions are met
                path = generator.maybe_generate(results, manager)
                if path:
                    logger.info("   📄 Report → %s", path.name)

            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)

            logger.info("Next cycle in %ds", CYCLE_INTERVAL_S)
            time.sleep(CYCLE_INTERVAL_S)

    except KeyboardInterrupt:
        logger.info("Stopping …")
    finally:
        detector.close()
        logger.info("AnomalyDetector stopped")


if __name__ == "__main__":
    main()
