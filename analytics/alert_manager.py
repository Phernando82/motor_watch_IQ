"""
MotorWatch IQ — Alert Manager
==============================
Manages alert lifecycle across detection cycles.

Responsibilities:
  - Track alert state per motor: new → active → escalated → resolved
  - Suppress repeated notifications for sustained alerts (no spam)
  - Calculate alert duration
  - Provide active alert summary for launcher Tab 3 and report_generator

Alert lifecycle:
    First cycle above OK  → state = NEW     (log + notify)
    Same level sustained  → state = ACTIVE  (silent)
    Level increases       → state = ESCALATED (log + notify)
    Returns to OK         → state = RESOLVED  (log + notify)

Usage:
    from analytics.alert_manager import AlertManager
    manager = AlertManager()

    # Call once per detection cycle:
    events = manager.process(results)   # results: list[DetectionResult]
    for event in events:
        print(event.description)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from analytics.anomaly_detector import DetectionResult

# ── alert levels (mirrors anomaly_detector) ───────────────────────────────────
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

ALERT_ICONS = {
    ALERT_OK:       "✅",
    ALERT_WATCH:    "👁",
    ALERT_WARNING:  "🟠",
    ALERT_CRITICAL: "🔴",
}


# ─────────────────────────────────────────────────────────────────────────────
# Event types
# ─────────────────────────────────────────────────────────────────────────────

class AlertEventType(Enum):
    NEW       = "new"        # first detection above OK
    ESCALATED = "escalated"  # level increased
    RESOLVED  = "resolved"   # returned to OK
    # ACTIVE events are not emitted — sustained alerts are silent


@dataclass
class AlertEvent:
    """A state-change event emitted by AlertManager."""
    motor_id:    str
    event_type:  AlertEventType
    level:       int
    prev_level:  int
    message:     str
    timestamp:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def description(self) -> str:
        icon  = ALERT_ICONS.get(self.level, "?")
        label = ALERT_LABELS.get(self.level, "?")
        match self.event_type:
            case AlertEventType.NEW:
                return f"{icon} Motor {self.motor_id} NEW {label} — {self.message}"
            case AlertEventType.ESCALATED:
                prev = ALERT_LABELS.get(self.prev_level, "?")
                return f"{icon} Motor {self.motor_id} ESCALATED {prev}→{label} — {self.message}"
            case AlertEventType.RESOLVED:
                prev = ALERT_LABELS.get(self.prev_level, "?")
                return f"✅ Motor {self.motor_id} RESOLVED (was {prev}) — back to normal"
        return f"Motor {self.motor_id} {self.event_type.value} {label}"


# ─────────────────────────────────────────────────────────────────────────────
# Per-motor alert state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MotorAlertState:
    """Tracks the alert lifecycle for a single motor."""
    motor_id:     str
    level:        int      = ALERT_OK
    message:      str      = ""
    first_seen:   datetime = None   # when alert first triggered
    last_updated: datetime = None   # last cycle timestamp
    cycle_count:  int      = 0      # consecutive cycles at current level or above

    @property
    def is_active(self) -> bool:
        return self.level > ALERT_OK

    @property
    def duration_seconds(self) -> float:
        if self.first_seen is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.first_seen).total_seconds()

    @property
    def duration_str(self) -> str:
        s = int(self.duration_seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"


# ─────────────────────────────────────────────────────────────────────────────
# Alert Manager
# ─────────────────────────────────────────────────────────────────────────────

class AlertManager:
    """
    Manages alert lifecycle across detection cycles for all 4 motors.

    Call process(results) once per cycle to get state-change events.
    Query active_alerts() for current state summary.
    """

    def __init__(self):
        self._states: dict[str, MotorAlertState] = {
            mid: MotorAlertState(motor_id=mid)
            for mid in ("1", "2", "3", "4")
        }
        self._history: list[AlertEvent] = []

    # ── public ────────────────────────────────────────────────────────────────

    def process(self, results: list) -> list[AlertEvent]:
        """
        Process one detection cycle. Returns list of AlertEvents (state changes only).
        Sustained alerts at same level produce no events.
        """
        events = []
        now    = datetime.now(timezone.utc)

        for result in results:
            mid   = str(result.motor_id)
            state = self._states[mid]
            new_level = result.alert_level
            new_msg   = result.alert_message

            event = self._transition(state, new_level, new_msg, now)
            if event is not None:
                events.append(event)
                self._history.append(event)

        return events

    def active_alerts(self) -> list[MotorAlertState]:
        """Return list of currently active alert states, sorted by severity."""
        active = [s for s in self._states.values() if s.is_active]
        return sorted(active, key=lambda s: (-s.level, s.motor_id))

    def all_states(self) -> list[MotorAlertState]:
        """Return all motor states (including OK), sorted by motor_id."""
        return sorted(self._states.values(), key=lambda s: s.motor_id)

    def history(self, limit: int = 50) -> list[AlertEvent]:
        """Return last N alert events."""
        return self._history[-limit:]

    def summary(self) -> str:
        """One-line summary of current alert status."""
        active = self.active_alerts()
        if not active:
            return "✅ All motors normal"
        parts = []
        for s in active:
            icon = ALERT_ICONS.get(s.level, "?")
            parts.append(f"{icon} M{s.motor_id} {ALERT_LABELS[s.level]} ({s.duration_str})")
        return " | ".join(parts)

    # ── private ───────────────────────────────────────────────────────────────

    def _transition(
        self,
        state:     MotorAlertState,
        new_level: int,
        new_msg:   str,
        now:       datetime,
    ) -> AlertEvent | None:
        """
        Apply state transition. Returns an AlertEvent if state changed, else None.

        Transitions:
            OK → above OK       : NEW
            active → higher     : ESCALATED
            active → same/lower : ACTIVE (silent — no event)
            active → OK         : RESOLVED
        """
        prev_level = state.level
        event      = None

        if new_level > ALERT_OK:
            if prev_level == ALERT_OK:
                # NEW alert
                state.first_seen = now
                state.cycle_count = 1
                event = AlertEvent(
                    motor_id=state.motor_id,
                    event_type=AlertEventType.NEW,
                    level=new_level,
                    prev_level=prev_level,
                    message=new_msg,
                    timestamp=now,
                )
            elif new_level > prev_level:
                # ESCALATED
                state.cycle_count += 1
                event = AlertEvent(
                    motor_id=state.motor_id,
                    event_type=AlertEventType.ESCALATED,
                    level=new_level,
                    prev_level=prev_level,
                    message=new_msg,
                    timestamp=now,
                )
            else:
                # ACTIVE — same or lower level, silent
                state.cycle_count += 1

        else:
            if prev_level > ALERT_OK:
                # RESOLVED
                event = AlertEvent(
                    motor_id=state.motor_id,
                    event_type=AlertEventType.RESOLVED,
                    level=new_level,
                    prev_level=prev_level,
                    message=new_msg,
                    timestamp=now,
                )
                state.first_seen  = None
                state.cycle_count = 0

        # Always update level and message
        state.level       = new_level
        state.message     = new_msg
        state.last_updated = now

        return event


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    import sys
    import time
    from pathlib import Path

    # Add project root to path so anomaly_detector is importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("alert_manager_test")

    from analytics.anomaly_detector import (
        AnomalyDetector, _make_client, ALERT_LABELS
    )

    client   = _make_client()
    detector = AnomalyDetector(client)
    manager  = AlertManager()

    logger.info("AlertManager test — running 3 cycles …")

    for cycle in range(1, 4):
        logger.info("── Cycle %d ─────────────────────────────────", cycle)
        results = detector.run_cycle()
        events  = manager.process(results)

        if events:
            logger.info("── State changes:")
            for e in events:
                logger.info("   %s", e.description)
        else:
            logger.info("   (no state changes — alerts sustained)")

        logger.info("── Summary: %s", manager.summary())
        logger.info("── Active alerts:")
        for s in manager.active_alerts():
            logger.info(
                "   Motor %s  [%s]  duration=%s  cycles=%d  — %s",
                s.motor_id, ALERT_LABELS[s.level],
                s.duration_str, s.cycle_count, s.message,
            )

        if cycle < 3:
            logger.info("Next cycle in 30s …")
            time.sleep(30)

    client.close()
    logger.info("AlertManager test complete")
