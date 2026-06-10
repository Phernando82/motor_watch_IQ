"""
MotorWatch IQ — Series Trainer
================================
Trains a RandomForestClassifier on synthetic time-series features
that match the InfluxDB live data space (aggregated vrms/apeak/temp values
at ~1 tick per 10 seconds, ~30 points per 5-minute window).

This is separate from cwru_trainer.py which trains the Isolation Forest
on CWRU raw signal features (kurtosis/skewness of 48kHz waveforms).

Feature space — 8 features from aggregated series:
  vrms_rms_norm     RMS of vrms series / PREALARM_VRMS  (energy level)
  vrms_skewness     skewness of vrms series              (asymmetry)
  vrms_crest        max(vrms) / rms(vrms)               (peak ratio)
  vrms_std_norm     std(vrms) / mean(vrms)              (variability)
  apeak_rms_norm    RMS of apeak series / PREALARM_APEAK (impact energy)
  apeak_skewness    skewness of apeak series             (spike asymmetry)
  apeak_crest       max(apeak) / rms(apeak)             (impact peak ratio)
  temp_norm         last_temp / ALARM_TEMP              (temperature)

Scenario characteristics (calibrated to BearingSignalGenerator output):
  normal:      vrms low + stable, apeak low + Gaussian, temp normal
  inner_race:  vrms HIGH (drift), apeak HIGH periodic, vrms_skew ~0
  ball_fault:  vrms moderate, apeak HIGH + POSITIVE SKEW, apeak_crest high
  thermal:     vrms normal, apeak normal, temp HIGH

Model saved to analytics/models/series_rf.pkl
"""

from __future__ import annotations

import logging
import pickle
import random
from pathlib import Path

import numpy as np
import scipy.stats
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.preprocessing import LabelEncoder

logger = logging.getLogger("series_trainer")

# ── paths ─────────────────────────────────────────────────────────────────────
MODELS_DIR    = Path(__file__).resolve().parent / "models"
MODEL_RF_SERIES = MODELS_DIR / "series_rf.pkl"
MODEL_RF_META   = MODELS_DIR / "series_rf_meta.pkl"

# ── thresholds (ISO 20816-3) ──────────────────────────────────────────────────
PREALARM_VRMS  =  7.1    # mm/s
ALARM_VRMS     = 11.2    # mm/s
PREALARM_APEAK = 1000.0  # mg
ALARM_APEAK    = 2000.0  # mg
ALARM_TEMP     =  75.0   # °C

# ── labels ────────────────────────────────────────────────────────────────────
LABEL_NORMAL   = "normal"
LABEL_INNER    = "inner_race"
LABEL_BALL     = "ball_fault"
LABEL_THERMAL  = "thermal"

N_SAMPLES_PER_CLASS = 2000   # synthetic samples per class
N_POINTS            = 30     # points per series window (5min @ 10s tick)


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction from series
# ─────────────────────────────────────────────────────────────────────────────

def extract_series_features(
    vrms_vals:  list[float],
    apeak_vals: list[float],
    temp_norm:  float,
) -> np.ndarray | None:
    """
    Extract 8 features from aggregated series data (InfluxDB live window).
    Returns ndarray shape (1, 8) or None if insufficient data.
    """
    if len(vrms_vals) < 5:
        return None

    v = np.array(vrms_vals,  dtype=np.float64)
    a = np.array(apeak_vals, dtype=np.float64) if len(apeak_vals) >= 5 else np.ones_like(v) * 100.0

    # v-RMS features
    v_rms      = float(np.sqrt(np.mean(v ** 2)))
    v_peak     = float(np.max(np.abs(v)))
    v_mean     = float(np.mean(v))
    v_std      = float(np.std(v))
    v_rms_norm = v_rms / PREALARM_VRMS
    v_skew     = float(scipy.stats.skew(v))     if len(v) >= 5 else 0.0
    v_crest    = v_peak / v_rms                 if v_rms > 1e-9 else 1.0
    v_std_norm = v_std  / v_mean                if v_mean > 1e-9 else 0.0

    # a-Peak features
    a_rms      = float(np.sqrt(np.mean(a ** 2)))
    a_peak     = float(np.max(np.abs(a)))
    a_rms_norm = a_rms / PREALARM_APEAK
    a_skew     = float(scipy.stats.skew(a))     if len(a) >= 5 else 0.0
    a_crest    = a_peak / a_rms                 if a_rms > 1e-9 else 1.0

    return np.array([[
        v_rms_norm, v_skew, v_crest, v_std_norm,
        a_rms_norm, a_skew, a_crest, temp_norm,
    ]], dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic data generators — calibrated to BearingSignalGenerator output
# ─────────────────────────────────────────────────────────────────────────────

def _gen_normal(n: int = N_POINTS) -> tuple[list, list, float]:
    """
    Normal stable motor.
    vrms: 1.5–4.0 mm/s Gaussian, apeak: 50–400 mg, temp: 25–55°C
    """
    vrms_base = random.uniform(1.5, 4.0)
    vrms  = [max(0.1, vrms_base + random.gauss(0, 0.15)) for _ in range(n)]
    apeak = [max(10,  int(random.gauss(200, 80)))         for _ in range(n)]
    temp  = random.uniform(25, 55) / ALARM_TEMP
    return vrms, apeak, temp


def _gen_inner_race(n: int = N_POINTS) -> tuple[list, list, float]:
    """
    Inner race fault — vibration drift upward.
    vrms: 8–14 mm/s with slow drift, apeak: 1000–3500 mg, near-zero skewness.
    The BearingSignalGenerator.inner_race produces vrms with low variability
    (periodic BPFI → stable RMS) and high apeak.
    """
    vrms_base = random.uniform(8.0, 14.0)
    # Slow drift with low noise (periodic fault → stable RMS)
    drift = random.uniform(-0.3, 0.3)
    vrms  = [max(0.1, vrms_base + i * drift / n + random.gauss(0, 0.2))
             for i in range(n)]

    # apeak: high and relatively stable (periodic impacts)
    apeak_base = random.uniform(1000, 3500)
    apeak = [max(100, int(apeak_base + random.gauss(0, apeak_base * 0.08)))
             for _ in range(n)]

    temp = random.uniform(40, 65) / ALARM_TEMP
    return vrms, apeak, temp


def _gen_ball_fault(n: int = N_POINTS) -> tuple[list, list, float]:
    """
    Ball fault — moderate vrms, HIGH positive apeak skewness.
    The BearingSignalGenerator.ball_fault produces vrms similar to normal
    but apeak has occasional large positive spikes (BSF modulation).
    Key signature: apeak_skewness > 1.0
    """
    vrms_base = random.uniform(2.0, 6.0)
    vrms  = [max(0.1, vrms_base + random.gauss(0, 0.25)) for _ in range(n)]

    # apeak: moderate baseline + positive spikes (BSF non-stationarity)
    apeak_base = random.uniform(300, 1200)
    spike_prob = random.uniform(0.15, 0.35)  # 15–35% chance of spike per tick
    apeak = []
    for _ in range(n):
        if random.random() < spike_prob:
            # Positive spike — key ball fault signature
            val = apeak_base * random.uniform(2.5, 5.0)
        else:
            val = apeak_base * random.uniform(0.4, 1.2)
        apeak.append(max(50, int(val)))

    temp = random.uniform(35, 60) / ALARM_TEMP
    return vrms, apeak, temp


def _gen_thermal(n: int = N_POINTS) -> tuple[list, list, float]:
    """
    Thermal fault — normal vibration but HIGH temperature.
    vrms and apeak are essentially normal; temp > 75°C.
    """
    vrms_base = random.uniform(1.0, 4.0)
    vrms  = [max(0.1, vrms_base + random.gauss(0, 0.15)) for _ in range(n)]
    apeak = [max(10,  int(random.gauss(180, 70)))         for _ in range(n)]
    temp  = random.uniform(75, 85) / ALARM_TEMP
    return vrms, apeak, temp


GENERATORS = {
    LABEL_NORMAL:  _gen_normal,
    LABEL_INNER:   _gen_inner_race,
    LABEL_BALL:    _gen_ball_fault,
    LABEL_THERMAL: _gen_thermal,
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataset builder
# ─────────────────────────────────────────────────────────────────────────────

def build_series_dataset(
    n_per_class: int = N_SAMPLES_PER_CLASS,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate synthetic series dataset.
    Returns (X, y_str) where X shape (n_total, 8), y_str string labels.
    """
    X_rows, y_rows = [], []

    for label, gen_fn in GENERATORS.items():
        logger.info("  Generating %d %s samples …", n_per_class, label)
        for _ in range(n_per_class):
            vrms, apeak, temp = gen_fn()
            fv = extract_series_features(vrms, apeak, temp)
            if fv is not None:
                X_rows.append(fv[0])
                y_rows.append(label)

    X = np.array(X_rows, dtype=np.float32)
    y = np.array(y_rows)

    logger.info(
        "Dataset: %d samples — %s",
        len(X),
        {lbl: int(np.sum(y == lbl)) for lbl in GENERATORS},
    )
    return X, y


# ─────────────────────────────────────────────────────────────────────────────
# Training
# ─────────────────────────────────────────────────────────────────────────────

def train_series_classifier(
    n_per_class: int = N_SAMPLES_PER_CLASS,
) -> tuple[RandomForestClassifier, LabelEncoder, dict]:
    """
    Train RandomForest on synthetic series features.
    Returns (clf, label_encoder, metadata).
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    X, y_str = build_series_dataset(n_per_class)
    le       = LabelEncoder()
    y_enc    = le.fit_transform(y_str)

    clf = RandomForestClassifier(
        n_estimators=300,
        max_depth=15,
        min_samples_leaf=3,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(clf, X, y_enc, cv=cv, scoring="accuracy")
    logger.info(
        "Series RF CV accuracy: %.3f ± %.3f",
        cv_scores.mean(), cv_scores.std(),
    )

    clf.fit(X, y_enc)
    logger.info("Series RF trained — classes: %s", list(le.classes_))

    meta = {
        "feature_names": [
            "vrms_rms_norm", "vrms_skewness", "vrms_crest", "vrms_std_norm",
            "apeak_rms_norm", "apeak_skewness", "apeak_crest", "temp_norm",
        ],
        "label_classes":  list(le.classes_),
        "cv_accuracy":    float(cv_scores.mean()),
        "cv_std":         float(cv_scores.std()),
        "n_per_class":    n_per_class,
        "feature_space":  "aggregated_series",
    }

    with open(MODEL_RF_SERIES, "wb") as f: pickle.dump(clf, f)
    with open(MODEL_RF_META,   "wb") as f: pickle.dump((meta, le), f)
    logger.info("Series RF saved → %s", MODEL_RF_SERIES)

    return clf, le, meta


def load_series_classifier() -> tuple[RandomForestClassifier, LabelEncoder, dict] | None:
    """Load pre-trained series classifier. Returns None if not found."""
    if not (MODEL_RF_SERIES.exists() and MODEL_RF_META.exists()):
        return None
    try:
        with open(MODEL_RF_SERIES, "rb") as f: clf = pickle.load(f)
        with open(MODEL_RF_META,   "rb") as f: meta, le = pickle.load(f)
        logger.info(
            "Series RF loaded — acc=%.3f ± %.3f  classes=%s",
            meta["cv_accuracy"], meta["cv_std"], meta["label_classes"],
        )
        return clf, le, meta
    except Exception as e:
        logger.warning("Failed to load series RF: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Inference helper
# ─────────────────────────────────────────────────────────────────────────────

def classify_series(
    clf:        RandomForestClassifier,
    le:         LabelEncoder,
    vrms_vals:  list[float],
    apeak_vals: list[float],
    temp_norm:  float,
) -> tuple[str, float]:
    """
    Classify fault type from live series data.
    Returns (fault_type_str, probability).
    """
    fv = extract_series_features(vrms_vals, apeak_vals, temp_norm)
    if fv is None:
        return "unknown", 0.0
    proba     = clf.predict_proba(fv)[0]
    class_idx = int(np.argmax(proba))
    return str(le.classes_[class_idx]), float(proba[class_idx])


# ─────────────────────────────────────────────────────────────────────────────
# Standalone — train and validate
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    clf, le, meta = train_series_classifier()

    print("\n── Training complete ─────────────────────────────────")
    print(f"  Features:    {meta['feature_names']}")
    print(f"  Classes:     {meta['label_classes']}")
    print(f"  CV accuracy: {meta['cv_accuracy']:.3f} ± {meta['cv_std']:.3f}")

    # Validation — test each scenario
    print("\n── Validation — 20 samples per class ────────────────")
    random.seed(99)
    np.random.seed(99)
    for label, gen_fn in GENERATORS.items():
        correct = 0
        for _ in range(20):
            vrms, apeak, temp = gen_fn()
            pred, prob = classify_series(clf, le, vrms, apeak, temp)
            if pred == label:
                correct += 1
        print(f"  {label:12s}: {correct}/20 correct")
