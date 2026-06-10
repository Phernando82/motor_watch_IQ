"""
MotorWatch IQ — CWRU Trainer
=============================
Downloads CWRU bearing dataset, extracts adimensional features, and trains:

  Model 1 — Isolation Forest  (anomaly_score 0.0–1.0)
  Model 2 — RandomForestClassifier  (fault_type + fault_prob)

Feature space — 7 adimensional + relative features:
  crest_factor    peak / rms                (impulse indicator)
  kurtosis        scipy.stats.kurtosis      (impulsive fault indicator)
  skewness        scipy.stats.skew          (asymmetry — early fault)
  shape_factor    rms / mean_abs            (waveform shape)
  impulse_factor  peak / mean_abs           (impact severity)
  rms_norm        rms / PREALARM_RMS_REF    (relative energy, adimensional)
  temp_norm       temp_c / ALARM_TEMP       (already used — kept for consistency)

These features are scale-independent: valid for CWRU (g) and VVB306 (mm/s).
rms_norm uses a reference value so CWRU g-scale and VVB306 mm/s-scale are
normalised to the same order of magnitude.

CWRU files used:
  Normal:      97.mat  (1797 RPM, 48k, drive end)
  Inner race:  105.mat (0.007"), 106.mat (0.014"), 107.mat (0.021")
  Outer race:  130.mat (0.007"), 131.mat (0.014"), 132.mat (0.021")
  Ball fault:  118.mat (0.007"), 119.mat (0.014"), 120.mat (0.021")
  Fan end:     278.mat (inner), 274.mat (outer), 270.mat (ball)

Reference:
  Case Western Reserve University Bearing Data Center
  https://engineering.case.edu/bearingdatacenter
"""

from __future__ import annotations

import logging
import pickle
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import scipy.io
import scipy.stats
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import cross_val_score

logger = logging.getLogger("cwru_trainer")

# ── paths ─────────────────────────────────────────────────────────────────────
CWRU_DIR    = Path(__file__).resolve().parent / "cwru_data"
MODELS_DIR  = Path(__file__).resolve().parent / "models"
MODEL_IF    = MODELS_DIR / "isolation_forest.pkl"
MODEL_RF    = MODELS_DIR / "fault_classifier.pkl"
MODEL_META  = MODELS_DIR / "model_meta.pkl"

# ── feature extraction config ─────────────────────────────────────────────────
WINDOW_SIZE     = 2048   # samples per feature window (≈42ms at 48kHz)
STEP_SIZE       = 1024   # 50% overlap
PREALARM_RMS_REF = 0.15  # g — typical CWRU normal RMS ≈ 0.10–0.20g
                          # used to normalise rms_norm to ~0.5–1.5 range

# ── fault labels ──────────────────────────────────────────────────────────────
LABEL_NORMAL     = "normal"
LABEL_INNER      = "inner_race"
LABEL_OUTER      = "outer_race"
LABEL_BALL       = "ball_fault"

# ── CWRU file catalogue ───────────────────────────────────────────────────────
# (file_number, fault_type, fault_size_in, location, rpm)
CWRU_FILES = [
    # Normal baseline — 4 speeds
    (97,  LABEL_NORMAL, 0.0,   "DE", 1797),
    (98,  LABEL_NORMAL, 0.0,   "DE", 1772),
    (99,  LABEL_NORMAL, 0.0,   "DE", 1750),
    (100, LABEL_NORMAL, 0.0,   "DE", 1730),

    # Drive end — inner race
    (105, LABEL_INNER, 0.007, "DE", 1797),
    (106, LABEL_INNER, 0.014, "DE", 1797),
    (107, LABEL_INNER, 0.021, "DE", 1797),
    (169, LABEL_INNER, 0.007, "DE", 1772),
    (170, LABEL_INNER, 0.014, "DE", 1772),
    (171, LABEL_INNER, 0.021, "DE", 1772),

    # Drive end — outer race
    (130, LABEL_OUTER, 0.007, "DE", 1797),
    (131, LABEL_OUTER, 0.014, "DE", 1797),
    (132, LABEL_OUTER, 0.021, "DE", 1797),
    (197, LABEL_OUTER, 0.007, "DE", 1772),
    (198, LABEL_OUTER, 0.014, "DE", 1772),
    (199, LABEL_OUTER, 0.021, "DE", 1772),

    # Drive end — ball fault
    (118, LABEL_BALL, 0.007, "DE", 1797),
    (119, LABEL_BALL, 0.014, "DE", 1797),
    (120, LABEL_BALL, 0.021, "DE", 1797),
    (185, LABEL_BALL, 0.007, "DE", 1772),
    (186, LABEL_BALL, 0.014, "DE", 1772),
    (187, LABEL_BALL, 0.021, "DE", 1772),

    # Fan end — inner race
    (278, LABEL_INNER, 0.007, "FE", 1797),
    (282, LABEL_INNER, 0.014, "FE", 1797),

    # Fan end — outer race
    (274, LABEL_OUTER, 0.007, "FE", 1797),
    (286, LABEL_OUTER, 0.014, "FE", 1797),

    # Fan end — ball
    (270, LABEL_BALL, 0.007, "FE", 1797),
    (290, LABEL_BALL, 0.014, "FE", 1797),
]

BASE_URL = "https://engineering.case.edu/sites/default/files/{num}.mat"

# Key patterns for DE and FE accelerometer channels
DE_KEYS = ["DE_time", "de_time"]
FE_KEYS = ["FE_time", "fe_time"]


# ─────────────────────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────────────────────

def _download_file(num: int, min_size_kb: int = 100) -> Path:
    """
    Download .mat file if not cached or if cached file is too small (corrupt).
    Retries once on failure. Returns local path or None.
    """
    CWRU_DIR.mkdir(parents=True, exist_ok=True)
    dest = CWRU_DIR / f"{num}.mat"

    # Check if cached file is valid (not truncated)
    if dest.exists():
        if dest.stat().st_size >= min_size_kb * 1024:
            return dest
        else:
            logger.warning("  %d.mat cached but too small (%d bytes) — re-downloading",
                           num, dest.stat().st_size)
            dest.unlink()

    url = BASE_URL.format(num=num)
    logger.info("  Downloading %s …", url)

    for attempt in range(1, 3):  # 2 attempts
        try:
            urllib.request.urlretrieve(url, dest)
            if dest.exists() and dest.stat().st_size >= min_size_kb * 1024:
                return dest
            logger.warning("  %d.mat attempt %d: file too small after download", num, attempt)
            if dest.exists():
                dest.unlink()
        except Exception as e:
            logger.warning("  %d.mat attempt %d failed: %s", num, attempt, e)
            if dest.exists():
                dest.unlink()

    logger.warning("  %d.mat — download failed after 2 attempts, skipping", num)
    return None


def _load_signal(path: Path, location: str) -> np.ndarray | None:
    """Load drive-end or fan-end accelerometer signal from .mat file."""
    try:
        mat  = scipy.io.loadmat(path)
        keys = list(mat.keys())

        # Try location-specific keys first
        search_keys = DE_KEYS if location == "DE" else FE_KEYS
        for pattern in search_keys:
            for k in keys:
                if pattern.lower() in k.lower():
                    signal = mat[k].flatten().astype(np.float64)
                    if len(signal) > WINDOW_SIZE:
                        return signal

        # Fallback: any key ending in _time with enough samples
        for k in keys:
            if k.startswith("__"):
                continue
            if "time" in k.lower():
                signal = mat[k].flatten().astype(np.float64)
                if len(signal) > WINDOW_SIZE:
                    return signal

    except Exception as e:
        logger.warning("  Could not load %s: %s", path.name, e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_features_from_signal(
    signal: np.ndarray,
    rms_ref: float = PREALARM_RMS_REF,
    temp_norm: float = 0.4,   # default: ~30°C / 75°C — neutral temperature
) -> np.ndarray:
    """
    Extract feature matrix from a 1D signal using sliding windows.

    Returns ndarray shape (n_windows, 7):
        [crest_factor, kurtosis, skewness, shape_factor,
         impulse_factor, rms_norm, temp_norm]

    All features are adimensional or normalised — valid across g and mm/s.
    """
    n_windows = (len(signal) - WINDOW_SIZE) // STEP_SIZE + 1
    if n_windows <= 0:
        return np.empty((0, 7), dtype=np.float32)

    rows = []
    for i in range(n_windows):
        w = signal[i * STEP_SIZE : i * STEP_SIZE + WINDOW_SIZE]

        rms        = float(np.sqrt(np.mean(w ** 2)))
        peak       = float(np.max(np.abs(w)))
        mean_abs   = float(np.mean(np.abs(w)))

        crest      = peak / rms       if rms      > 1e-12 else 0.0
        shape      = rms  / mean_abs  if mean_abs > 1e-12 else 0.0
        impulse    = peak / mean_abs  if mean_abs > 1e-12 else 0.0
        kurt       = float(scipy.stats.kurtosis(w, fisher=True))
        skew       = float(scipy.stats.skew(w))
        rms_n      = rms / rms_ref    if rms_ref  > 1e-12 else rms

        rows.append([crest, kurt, skew, shape, impulse, rms_n, temp_norm])

    return np.array(rows, dtype=np.float32)


def extract_features_from_live(
    vrms_vals: list[float],
    apeak_vals: list[float],
    temp_norm: float,
    prealarm_vrms: float = 7.1,
) -> np.ndarray | None:
    """
    Extract a single feature vector from live VVB306 data (mm/s / mg).

    Maps VVB306 metrics to the same 7-feature space as CWRU:
      - v-RMS series → used as proxy signal for crest/shape/impulse/rms_norm
      - a-Peak series → kurtosis and skewness (impact channel)
      - temp_norm → direct

    Returns ndarray shape (1, 7) or None if insufficient data.
    """
    if len(vrms_vals) < 5:
        return None

    v = np.array(vrms_vals,  dtype=np.float64)
    a = np.array(apeak_vals, dtype=np.float64) if len(apeak_vals) >= 5 else v.copy()

    rms      = float(np.sqrt(np.mean(v ** 2)))
    peak     = float(np.max(np.abs(v)))
    mean_abs = float(np.mean(np.abs(v)))

    crest   = peak / rms      if rms      > 1e-12 else 0.0
    shape   = rms  / mean_abs if mean_abs > 1e-12 else 0.0
    impulse = peak / mean_abs if mean_abs > 1e-12 else 0.0

    # kurtosis + skewness from a-Peak channel (richer in impact info)
    # Suppress catastrophic cancellation warning when all values are identical
    # (e.g. Motor 1 normal with very stable apeak readings)
    if len(a) >= 5 and float(np.std(a)) > 1e-9:
        with np.errstate(all="ignore"):
            kurt = float(scipy.stats.kurtosis(a, fisher=True))
            skew = float(scipy.stats.skew(a))
    else:
        kurt = 0.0
        skew = 0.0

    # rms_norm: normalise vrms relative to ISO prealarm threshold
    # 0.5 = well below prealarm / 1.0 = at prealarm / >1.4 = at alarm
    rms_n = rms / prealarm_vrms if prealarm_vrms > 1e-12 else rms

    return np.array([[crest, kurt, skew, shape, impulse, rms_n, temp_norm]],
                    dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Dataset builder
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CWRUDataset:
    X_normal: np.ndarray       # feature rows — normal only
    X_all:    np.ndarray       # feature rows — all classes
    y_all:    np.ndarray       # string labels
    label_counts: dict         # {label: n_windows}
    files_loaded: int
    files_failed: int


def build_dataset() -> CWRUDataset:
    """
    Download (if needed) and process all CWRU files.
    Returns feature matrices for IF and RF training.
    """
    CWRU_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Building CWRU dataset — %d files …", len(CWRU_FILES))

    X_rows, y_rows = [], []
    label_counts   = {LABEL_NORMAL: 0, LABEL_INNER: 0,
                      LABEL_OUTER: 0, LABEL_BALL: 0}
    files_loaded = files_failed = 0

    for num, label, size, loc, rpm in CWRU_FILES:
        path = _download_file(num)
        if path is None:
            files_failed += 1
            continue

        signal = _load_signal(path, loc)
        if signal is None:
            logger.warning("  %d.mat — no usable signal", num)
            files_failed += 1
            continue

        features = extract_features_from_signal(signal)
        if len(features) == 0:
            files_failed += 1
            continue

        X_rows.append(features)
        y_rows.extend([label] * len(features))
        label_counts[label] = label_counts.get(label, 0) + len(features)
        files_loaded += 1
        logger.debug("  %d.mat  %s  size=%.3f  loc=%s  rpm=%d  → %d windows",
                     num, label, size, loc, rpm, len(features))

    if not X_rows:
        raise RuntimeError("No CWRU files could be loaded. Check internet connection.")

    X_all   = np.vstack(X_rows)
    y_all   = np.array(y_rows)
    X_normal = X_all[y_all == LABEL_NORMAL]

    logger.info(
        "Dataset: %d windows total — normal=%d inner=%d outer=%d ball=%d  "
        "(%d files OK, %d failed)",
        len(X_all),
        label_counts.get(LABEL_NORMAL, 0),
        label_counts.get(LABEL_INNER,  0),
        label_counts.get(LABEL_OUTER,  0),
        label_counts.get(LABEL_BALL,   0),
        files_loaded, files_failed,
    )

    return CWRUDataset(
        X_normal=X_normal,
        X_all=X_all,
        y_all=y_all,
        label_counts=label_counts,
        files_loaded=files_loaded,
        files_failed=files_failed,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Model training
# ─────────────────────────────────────────────────────────────────────────────

def train_models(
    dataset: CWRUDataset,
    live_normal_rows: np.ndarray | None = None,
) -> tuple[IsolationForest, RandomForestClassifier, LabelEncoder, dict]:
    """
    Train both models.

    Model 1 — Isolation Forest:
        Training data = CWRU normal + live Motor 1 normal (if available)
        contamination = n_fault / (n_normal + n_fault) — calculated from data

    Model 2 — RandomForestClassifier:
        Training data = all CWRU classes (normal + 3 fault types)
        Evaluated with 5-fold cross-validation

    Returns (isolation_forest, random_forest, label_encoder, metadata_dict)
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Model 1: Isolation Forest ─────────────────────────────────────────────
    X_normal = dataset.X_normal.copy()

    if live_normal_rows is not None and len(live_normal_rows) >= 5:
        logger.info(
            "Augmenting CWRU normal with %d live Motor 1 windows …",
            len(live_normal_rows),
        )
        X_normal = np.vstack([X_normal, live_normal_rows])

    n_normal = len(X_normal)
    n_fault  = len(dataset.X_all) - len(dataset.X_normal)

    # contamination = expected anomaly rate in production, NOT dataset ratio.
    # Dataset has artificially equal class distribution (by design in CWRU).
    # Industrial reality: ~5% of observations are anomalous.
    # Using dataset ratio (0.49) would flag nearly everything as anomaly.
    contamination = 0.05

    logger.info(
        "Training Isolation Forest — %d normal + %d fault windows  "
        "contamination=%.3f (fixed industrial rate) …",
        n_normal, n_fault, contamination,
    )

    # Build mixed training set: all normal + fault samples
    X_fault   = dataset.X_all[dataset.y_all != LABEL_NORMAL]
    X_if_train = np.vstack([X_normal, X_fault])

    clf_if = IsolationForest(
        n_estimators=300,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    clf_if.fit(X_if_train)
    logger.info("Isolation Forest trained OK")

    # ── Model 2: RandomForest classifier ──────────────────────────────────────
    logger.info("Training RandomForest fault classifier …")

    le = LabelEncoder()
    y_enc = le.fit_transform(dataset.y_all)

    clf_rf = RandomForestClassifier(
        n_estimators=300,
        max_depth=12,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    # 5-fold cross-validation for accuracy estimate
    cv_scores = cross_val_score(clf_rf, dataset.X_all, y_enc, cv=5, scoring="accuracy")
    logger.info(
        "RF cross-validation accuracy: %.3f ± %.3f",
        cv_scores.mean(), cv_scores.std(),
    )

    # Final fit on full dataset
    clf_rf.fit(dataset.X_all, y_enc)
    logger.info(
        "RandomForest trained OK — classes: %s",
        list(le.classes_),
    )

    # ── Save models ───────────────────────────────────────────────────────────
    meta = {
        "feature_names":  ["crest_factor", "kurtosis", "skewness",
                           "shape_factor", "impulse_factor", "rms_norm", "temp_norm"],
        "label_classes":  list(le.classes_),
        "contamination":  contamination,
        "n_normal":       n_normal,
        "n_fault":        n_fault,
        "cv_accuracy":    float(cv_scores.mean()),
        "cv_std":         float(cv_scores.std()),
        "window_size":    WINDOW_SIZE,
        "prealarm_rms_ref": PREALARM_RMS_REF,
        "cwru_files_loaded": dataset.files_loaded,
        "cwru_files_failed": dataset.files_failed,
        "label_counts":   dataset.label_counts,
    }

    with open(MODEL_IF,   "wb") as f: pickle.dump(clf_if, f)
    with open(MODEL_RF,   "wb") as f: pickle.dump(clf_rf, f)
    with open(MODEL_META, "wb") as f: pickle.dump((meta, le), f)

    logger.info("Models saved → %s", MODELS_DIR)
    return clf_if, clf_rf, le, meta


def load_models() -> tuple[IsolationForest, RandomForestClassifier, LabelEncoder, dict] | None:
    """Load pre-trained models from disk. Returns None if not found."""
    if not (MODEL_IF.exists() and MODEL_RF.exists() and MODEL_META.exists()):
        return None
    try:
        with open(MODEL_IF,   "rb") as f: clf_if = pickle.load(f)
        with open(MODEL_RF,   "rb") as f: clf_rf = pickle.load(f)
        with open(MODEL_META, "rb") as f: meta, le = pickle.load(f)
        logger.info(
            "Models loaded from disk — IF contamination=%.3f  RF acc=%.3f ± %.3f",
            meta["contamination"], meta["cv_accuracy"], meta["cv_std"],
        )
        return clf_if, clf_rf, le, meta
    except Exception as e:
        logger.warning("Failed to load models: %s — will retrain", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Standalone — train and save models
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    dataset = build_dataset()
    clf_if, clf_rf, le, meta = train_models(dataset)

    print("\n── Training complete ─────────────────────────────────")
    print(f"  Features:       {meta['feature_names']}")
    print(f"  Contamination:  {meta['contamination']:.3f}")
    print(f"  RF CV accuracy: {meta['cv_accuracy']:.3f} ± {meta['cv_std']:.3f}")
    print(f"  Classes:        {meta['label_classes']}")
    print(f"  Label counts:   {meta['label_counts']}")
    print(f"  Models saved →  {MODELS_DIR}")
