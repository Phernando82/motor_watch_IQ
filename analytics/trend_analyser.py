# analytics/trend_analyser.py
from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from influxdb_client import InfluxDBClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

# ── ISO 20816-3 thresholds ────────────────────────────────────────────────────
PREALARM_VRMS  = 7.1   # mm/s
ALARM_VRMS     = 11.2  # mm/s
MIN_POINTS     = 10    # mínimo de pontos para regressão válida
HISTORY_WINDOW = "-30m"


@dataclass
class TrendResult:
    motor_id:       str
    slope_mms_h:    float          # mm/s por hora (positivo = degradando)
    current_vrms:   float          # último valor lido
    eta_prealarm_h: float          # horas até prealarm (-1 se não aplicável)
    eta_alarm_h:    float          # horas até alarm    (-1 se não aplicável)
    n_points:       int            # pontos usados na regressão
    valid:          bool           # False se dados insuficientes
    timestamp:      datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    def summary(self) -> str:
        if not self.valid:
            return f"Motor {self.motor_id} — dados insuficientes para trend"
        direction = "↑" if self.slope_mms_h > 0.05 else ("↓" if self.slope_mms_h < -0.05 else "→")
        parts = [f"Motor {self.motor_id} {direction} {self.slope_mms_h:+.3f} mm/s/h"]
        if self.eta_prealarm_h >= 0:
            parts.append(f"prealarm em {self.eta_prealarm_h:.1f}h")
        if self.eta_alarm_h >= 0:
            parts.append(f"alarm em {self.eta_alarm_h:.1f}h")
        return " | ".join(parts)


def _eta_hours(current: float, slope_mms_h: float, threshold: float) -> float:
    """Horas até atingir threshold. Retorna -1 se não aplicável."""
    if slope_mms_h <= 0:
        return -1.0
    if current >= threshold:
        return 0.0
    return (threshold - current) / slope_mms_h


class TrendAnalyser:

    def __init__(self):
        self._client = InfluxDBClient(
            url   = os.getenv("INFLUX_URL",   "http://localhost:8086"),
            token = os.getenv("INFLUX_TOKEN", ""),
            org   = os.getenv("INFLUX_ORG",   "motorwatch"),
        )
        self._query_api = self._client.query_api()
        self._org       = os.getenv("INFLUX_ORG", "motorwatch")

    # ── public ────────────────────────────────────────────────────────────────

    def analyse_all(self) -> dict[str, TrendResult]:
        """Retorna TrendResult para os 4 motores."""
        results = {}
        for motor_id in ("1", "2", "3", "4"):
            results[motor_id] = self._analyse_motor(motor_id)
        return results

    def analyse_motor(self, motor_id: str) -> TrendResult:
        return self._analyse_motor(motor_id)

    def close(self):
        self._client.close()

    # ── private ───────────────────────────────────────────────────────────────

    def _analyse_motor(self, motor_id: str) -> TrendResult:
        times, values = self._query_vrms(motor_id)

        if len(times) < MIN_POINTS:
            logger.warning("Motor %s — apenas %d pontos, trend inválido", motor_id, len(times))
            return TrendResult(
                motor_id=motor_id, slope_mms_h=0.0,
                current_vrms=values[-1] if values else 0.0,
                eta_prealarm_h=-1.0, eta_alarm_h=-1.0,
                n_points=len(times), valid=False,
            )

        # Converte timestamps para horas relativas (t0 = 0)
        t0        = times[0]
        t_hours   = np.array([(t - t0).total_seconds() / 3600.0 for t in times])
        v_array   = np.array(values)

        # Regressão linear: v = slope * t + intercept
        coeffs        = np.polyfit(t_hours, v_array, 1)
        slope_mms_h   = float(coeffs[0])   # mm/s por hora
        current_vrms  = float(v_array[-1])

        eta_prealarm = _eta_hours(current_vrms, slope_mms_h, PREALARM_VRMS)
        eta_alarm    = _eta_hours(current_vrms, slope_mms_h, ALARM_VRMS)

        return TrendResult(
            motor_id=motor_id,
            slope_mms_h=slope_mms_h,
            current_vrms=current_vrms,
            eta_prealarm_h=eta_prealarm,
            eta_alarm_h=eta_alarm,
            n_points=len(times),
            valid=True,
        )

    def _query_vrms(self, motor_id: str) -> tuple[list[datetime], list[float]]:
        """Busca série temporal de vrms_magnitude_mms, exclui pontos de fault (-1.0)."""
        flux = f"""
from(bucket: "motors")
  |> range(start: {HISTORY_WINDOW})
  |> filter(fn: (r) => r._measurement == "motor_telemetry")
  |> filter(fn: (r) => r.motor_id == "{motor_id}")
  |> filter(fn: (r) => r._field == "vrms_magnitude_mms")
  |> filter(fn: (r) => r._value >= 0.0)
  |> sort(columns: ["_time"])
"""
        tables = self._query_api.query(flux, org=self._org)
        times, values = [], []
        for table in tables:
            for record in table.records:
                times.append(record.get_time())
                values.append(float(record.get_value()))
        return times, values


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    analyser = TrendAnalyser()
    try:
        results = analyser.analyse_all()
        for r in results.values():
            print(r.summary())
    finally:
        analyser.close()