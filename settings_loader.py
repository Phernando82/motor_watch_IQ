"""
MotorWatch IQ — Settings Loader (M7)
=====================================
Utilitário central para leitura e escrita do settings.json.

Uso:
    from settings_loader import load_settings, get_thresholds, save_settings

    # Thresholds efectivos para um motor (custom ou ISO default)
    thr = get_thresholds(motor_id=1)
    thr["vrms_prealarm_mms"]   # → 7.1 (ISO) ou valor custom
    thr["vrms_alarm_mms"]
    thr["apeak_prealarm_mg"]
    thr["apeak_alarm_mg"]
    thr["temp_prealarm_c"]
    thr["temp_alarm_c"]

ISO 20816-3 defaults (hardcoded aqui — fonte única da verdade):
    v-RMS  prealarm ≥ 7.1 mm/s  |  alarm ≥ 11.2 mm/s
    a-Peak prealarm ≥ 1000 mg   |  alarm ≥ 2000 mg
    Temp   prealarm ≥ 65.0 °C   |  alarm ≥ 75.0 °C
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ─── Caminhos ─────────────────────────────────────────────────────────────────
_ROOT          = Path(__file__).resolve().parent
SETTINGS_PATH  = _ROOT / "settings.json"

# ─── ISO 20816-3 defaults (fonte única) ───────────────────────────────────────
ISO_DEFAULTS: dict[str, float] = {
    "vrms_prealarm_mms":  7.1,
    "vrms_alarm_mms":     11.2,
    "apeak_prealarm_mg":  1000.0,
    "apeak_alarm_mg":     2000.0,
    "temp_prealarm_c":    65.0,
    "temp_alarm_c":       75.0,
}

# ─── Estrutura default do settings.json ───────────────────────────────────────
def _default_motor_thresholds() -> dict:
    return {k: {"value": v, "use_default": True} for k, v in ISO_DEFAULTS.items()}


def _default_settings() -> dict:
    return {
        "plc": {
            "default_mode": "simulator",       # "simulator" | "opcua" | "snap7"
            "opcua": {
                "ip":       "192.168.0.10",
                "port":     4840,
                "url":      "opc.tcp://192.168.0.10:4840",
                "security": "None",
                "user":     "",
                "password": "",
            },
            "snap7": {
                "rack":             0,
                "slot":             1,
                "poll_interval_ms": 1000,
            },
        },
        "thresholds": {
            "motors": {
                str(mid): _default_motor_thresholds()
                for mid in range(1, 5)
            }
        },
    }


# ─── API pública ──────────────────────────────────────────────────────────────

def load_settings() -> dict:
    """Carrega settings.json. Se não existir, cria com defaults e devolve."""
    if not SETTINGS_PATH.exists():
        data = _default_settings()
        save_settings(data)
        return data
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Garante que motores novos têm todos os campos (migração suave)
        _ensure_motor_defaults(data)
        return data
    except Exception:
        return _default_settings()


def save_settings(data: dict) -> bool:
    """Grava settings.json. Devolve True se bem sucedido."""
    try:
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def get_thresholds(motor_id: int | str) -> dict[str, float]:
    """
    Devolve os thresholds efectivos para um motor.
    Se use_default=True para uma grandeza, devolve o valor ISO.
    Se use_default=False, devolve o valor custom configurado.
    """
    mid = str(motor_id)
    data = load_settings()
    motor_cfg = (
        data.get("thresholds", {})
            .get("motors", {})
            .get(mid, _default_motor_thresholds())
    )
    result: dict[str, float] = {}
    for key, iso_val in ISO_DEFAULTS.items():
        entry = motor_cfg.get(key, {"value": iso_val, "use_default": True})
        if entry.get("use_default", True):
            result[key] = iso_val
        else:
            result[key] = float(entry.get("value", iso_val))
    return result


def get_plc_config() -> dict:
    """Devolve a secção 'plc' do settings."""
    return load_settings().get("plc", _default_settings()["plc"])


def _ensure_motor_defaults(data: dict) -> None:
    """Migração suave: garante que todos os motores têm todos os campos."""
    motors = data.setdefault("thresholds", {}).setdefault("motors", {})
    for mid in ("1", "2", "3", "4"):
        if mid not in motors:
            motors[mid] = _default_motor_thresholds()
        else:
            for key, iso_val in ISO_DEFAULTS.items():
                if key not in motors[mid]:
                    motors[mid][key] = {"value": iso_val, "use_default": True}
