"""
MotorWatch IQ — Layer 3: InfluxDB Writer
=========================================
Subscribes to the Mosquitto MQTT broker, parses motor telemetry JSON
payloads, and writes valid readings to InfluxDB v2.

Key behaviours:
  - Writes sentinel point (values=-1, alarm_state='fault') when sensor_fault is not null,
    allowing Grafana to display FAULT state instead of a data gap
  - Handles MQTT reconnection with exponential backoff (1–30 s)
  - Handles InfluxDB reconnection on write failure (non-blocking)
  - Uses unique client_id per instance (uuid suffix) to prevent
    Mosquitto from dropping existing connections on reconnect

Data model (InfluxDB measurement: motor_telemetry):
  Tags  — motor_id, channel, alarm_state ('normal'|'prealarm'|'alarm'|'fault'), device_status_text
  Fields — vrms_magnitude_mms, apeak_magnitude_mg, temperature_c,
            apeak_magnitude_g, *_raw ints, device_status int, is_running bool,
            alarm_state_num int (0=normal, 1=prealarm, 2=alarm, 3=fault)

Run:
    python influxdb/influx_writer.py

Requires .env in project root:
    INFLUX_URL=http://localhost:8086
    INFLUX_TOKEN=<operator token>
    INFLUX_ORG=motorwatch
    INFLUX_BUCKET=motors

ISO 20816-3 alarm thresholds are not enforced here — they are applied
in the simulator and visualised in Grafana dashboards.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import uuid

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    LEVEL_MAP = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "time":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": self.LEVEL_MAP.get(record.levelno, "INFO"),
            "event": record.getMessage(),
        }
        # Merge any extra fields passed via extra={...}
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                base[key] = val
        return json.dumps(base, default=str)


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("influx_writer")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


logger = setup_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "motorwatch")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "motors")

MQTT_HOST  = "localhost"
MQTT_PORT  = 1883
MQTT_TOPIC = "motorwatch/motors/+/telemetry"

RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]   # exponential backoff (seconds)

# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------

class InfluxWriter:
    """Wraps the InfluxDB v2 client with automatic reconnection.

    Connects synchronously on init with exponential backoff. On write failure,
    marks the write_api as None and reconnects on the next write call to avoid
    blocking the MQTT callback thread.

    The InfluxDB client is configured with a 10-second timeout to prevent
    indefinite hangs on slow or unreachable hosts.
    """

    def __init__(self):
        self._client   = None
        self._write_api = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Create InfluxDB client and verify connectivity."""
        attempt = 0
        while True:
            try:
                self._client = InfluxDBClient(
                    url=INFLUX_URL,
                    token=INFLUX_TOKEN,
                    org=INFLUX_ORG,
                    timeout=10_000,  # 10 seconds — prevents indefinite hang
                )
                if not self._client.ping():
                    raise ConnectionError("ping() returned False")

                self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
                logger.info("influxdb_connected", extra={
                    "url": INFLUX_URL, "org": INFLUX_ORG, "bucket": INFLUX_BUCKET
                })
                return

            except Exception as exc:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                logger.error("influxdb_connect_failed", extra={
                    "error": str(exc), "retry_in_s": delay
                })
                time.sleep(delay)
                attempt += 1

    # ------------------------------------------------------------------
    # Public write method
    # ------------------------------------------------------------------

    def write(self, payload: dict) -> None:
        """
        Write a telemetry payload to InfluxDB.

        Normal readings: writes full point with all sensor fields.
        Fault readings (sensor_fault != null): writes a sentinel point with
            values=-1 and alarm_state='fault' so Grafana can display FAULT
            state instead of a gap. Sentinel values are filtered in Grafana
            using threshold color mapping (value < 0 → gray/fault color).

        On write failure, marks write_api as None for non-blocking reconnect
        on the next tick — does NOT call _connect() which is blocking.
        """
        motor_id     = payload.get("motor_id")
        # Aceita sensor_fault (str: "nodata"|"overload") do simulador
        # e sensor_fault_code (int: 1|2) do plc_collector — normaliza para str
        sensor_fault = payload.get("sensor_fault")
        if sensor_fault is None:
            _code = payload.get("sensor_fault_code")
            if _code == 1:
                sensor_fault = "nodata"
            elif _code == 2:
                sensor_fault = "overload"

        # Reconnect if write_api was lost (non-blocking check)
        if self._write_api is None:
            try:
                self._connect()
            except Exception:
                return  # skip this point, will retry on next tick

        if sensor_fault is not None:
            point = self._build_fault_point(payload, sensor_fault)
            logger.info("point_fault_written", extra={
                "motor_id":     motor_id,
                "sensor_fault": sensor_fault,
            })
        else:
            point = self._build_point(payload)
            logger.info("point_written", extra={
                "motor_id":    motor_id,
                "alarm_state": payload.get("alarm_state"),
                "vrms":        payload.get("vrms_magnitude_mms"),
                "temp":        payload.get("temperature_c"),
                "apeak":       payload.get("apeak_magnitude_mg"),
            })

        try:
            self._write_api.write(
                bucket=INFLUX_BUCKET,
                org=INFLUX_ORG,
                record=point,
            )
        except Exception as exc:
            logger.error("influxdb_write_failed", extra={
                "motor_id": motor_id,
                "error":    str(exc),
            })
            self._write_api = None

    # ------------------------------------------------------------------
    # Point builder
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(payload: dict) -> datetime:
        """Extract and parse ISO 8601 timestamp from payload, falling back to now()."""
        raw_ts = payload.get("timestamp")
        try:
            return datetime.fromisoformat(raw_ts)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
            logger.warning("timestamp_parse_failed", extra={
                "raw": raw_ts, "fallback": ts.isoformat()
            })
            return ts

    @staticmethod
    def _build_point(payload: dict) -> Point:
        """
        Convert a normal telemetry payload dict into an InfluxDB Point.

        Tags   — low cardinality, indexed (used for WHERE / GROUP BY)
        Fields — measured values, high cardinality, compressed storage
        """
        ts = InfluxWriter._parse_timestamp(payload)

        point = (
            Point("motor_telemetry")

            # ---- Tags (indexed, low cardinality) ----------------------
            .tag("motor_id",           str(payload.get("motor_id", "")))
            .tag("channel",            payload.get("channel", ""))
            .tag("alarm_state",        payload.get("alarm_state", ""))
            .tag("device_status_text", payload.get("device_status_text", ""))

            # ---- Fields (measured values) -----------------------------
            # None-safe: use 'or default' — handles None values from PLC sentinels
            .field("vrms_magnitude_mms",  float(payload.get("vrms_magnitude_mms")  if payload.get("vrms_magnitude_mms")  is not None else -1.0))
            .field("apeak_magnitude_mg",  float(payload.get("apeak_magnitude_mg")  if payload.get("apeak_magnitude_mg")  is not None else -1.0))
            .field("apeak_magnitude_g",   float(payload.get("apeak_magnitude_g")   if payload.get("apeak_magnitude_g")   is not None else -1.0))
            .field("temperature_c",       float(payload.get("temperature_c")       if payload.get("temperature_c")       is not None else -1.0))
            .field("vrms_magnitude_raw",    int(payload.get("vrms_magnitude_raw")  if payload.get("vrms_magnitude_raw")  is not None else 0))
            .field("apeak_magnitude_raw",   int(payload.get("apeak_magnitude_raw") if payload.get("apeak_magnitude_raw") is not None else 0))
            .field("temperature_raw",       int(payload.get("temperature_raw")     if payload.get("temperature_raw")     is not None else 0))
            .field("device_status",         int(payload.get("device_status")       if payload.get("device_status")       is not None else 0))
            .field("is_running",           bool(payload.get("is_running",          False)))

            # ---- Alarm state as numeric field for Grafana State timeline --
            # 0=normal, 1=prealarm, 2=alarm, 3=fault
            # Stored as field (not tag) so it is directly queryable in Flux
            .field("alarm_state_num", (
                2 if payload.get("alarm_state") == "alarm"
                else 1 if payload.get("alarm_state") == "prealarm"
                else 0
            ))

            # ---- Alarm state as string field for Grafana State timeline --
            # String fields work natively with State timeline value mappings
            # Values: "normal" | "prealarm" | "alarm"
            .field("alarm_state_str", str(payload.get("alarm_state", "normal")))

            # ---- Timestamp from payload (not write time) --------------
            .time(ts, WritePrecision.NS)
        )

        return point


    @staticmethod
    def _build_fault_point(payload: dict, sensor_fault: str) -> Point:
        """
        Build a sentinel InfluxDB Point for fault conditions (NoData / Overload).

        Uses -1.0 as sentinel value for numeric fields so Grafana thresholds
        can distinguish faults from valid zero readings.
        alarm_state tag is set to 'fault' for filtering in Grafana queries.

        Args:
            payload:      original MQTT payload dict
            sensor_fault: "nodata" | "overload"
        """
        ts = InfluxWriter._parse_timestamp(payload)

        return (
            Point("motor_telemetry")

            # ---- Tags ------------------------------------------------
            .tag("motor_id",           str(payload.get("motor_id", "")))
            .tag("channel",            payload.get("channel", ""))
            .tag("alarm_state",        "fault")           # distinct from normal alarm states
            .tag("device_status_text", payload.get("device_status_text", ""))

            # ---- Sentinel fields (-1 = fault, distinguishable from 0) ---
            .field("vrms_magnitude_mms",  -1.0)
            .field("apeak_magnitude_mg",  -1.0)
            .field("apeak_magnitude_g",   -1.0)
            .field("temperature_c",       -1.0)
            .field("vrms_magnitude_raw",   int(payload.get("vrms_magnitude_raw", 0)))
            .field("apeak_magnitude_raw",  int(payload.get("apeak_magnitude_raw", 0)))
            .field("temperature_raw",      int(payload.get("temperature_raw", 0)))
            .field("device_status",        int(payload.get("device_status", 0)))
            .field("is_running",          bool(payload.get("is_running", False)))
            .field("sensor_fault_code",    sensor_fault)  # "nodata" | "overload"
            .field("alarm_state_num",       3)  # 3 = fault — distinct from 0/1/2
            .field("alarm_state_str",       "fault")  # string equivalent for State timeline

            .time(ts, WritePrecision.NS)
        )


# ---------------------------------------------------------------------------
# MQTT handler
# ---------------------------------------------------------------------------

class MQTTHandler:
    """Manages the Paho MQTT client lifecycle for the InfluxDB writer.

    Responsibilities:
    - Connect to Mosquitto broker with exponential backoff on failure
    - Subscribe to motorwatch/motors/+/telemetry (QoS 1)
    - Parse incoming JSON payloads and delegate to InfluxWriter.write()
    - Reconnect on unexpected disconnection (rc != 0 and rc is not None)

    Client ID:
        Uses a unique UUID suffix per instance to prevent Mosquitto from
        forcibly disconnecting an existing session when a new client with
        the same ID connects (common cause of connect/disconnect loops).

    Thread model:
        loop_start() runs the Paho network loop in a daemon thread.
        loop_stop() must be called before disconnect() on shutdown.
    """

    def __init__(self, influx_writer: InfluxWriter):
        self._writer  = influx_writer
        self._client  = None
        self._attempt = 0
        self._running = False
        self._setup_client()

    # ------------------------------------------------------------------
    # Client setup
    # ------------------------------------------------------------------

    def _setup_client(self) -> None:
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"motorwatch_influx_writer_{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv5,
        )
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        # CallbackAPIVersion.VERSION2: reason_code is always a ReasonCode object
        if reason_code.is_failure:
            logger.error("mqtt_connect_refused", extra={"rc": str(reason_code)})
            return
        self._attempt = 0
        client.subscribe(MQTT_TOPIC, qos=1)
        logger.info("mqtt_connected", extra={
            "host": MQTT_HOST, "port": MQTT_PORT, "topic": MQTT_TOPIC
        })

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("mqtt_json_parse_failed", extra={
                "topic": msg.topic, "error": str(exc)
            })
            return

        self._writer.write(payload)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        # CallbackAPIVersion.VERSION2: reason_code is a ReasonCode object
        # reason_code.is_failure == False means clean disconnect
        if reason_code.is_failure and self._running:
            logger.warning("mqtt_disconnected_unexpected", extra={"rc": str(reason_code)})
            self._reconnect()

    # ------------------------------------------------------------------
    # Connection / reconnection
    # ------------------------------------------------------------------

    def _connect_once(self) -> bool:
        try:
            self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            return True
        except Exception as exc:
            logger.error("mqtt_connect_failed", extra={"error": str(exc)})
            return False

    def _reconnect(self) -> None:
        while self._running:
            delay = RECONNECT_DELAYS[min(self._attempt, len(RECONNECT_DELAYS) - 1)]
            logger.info("mqtt_reconnecting", extra={
                "attempt": self._attempt + 1, "delay_s": delay
            })
            time.sleep(delay)
            self._attempt += 1

            # Stop previous loop before recreating client — prevents thread leak
            try:
                self._client.loop_stop()
            except Exception:
                pass
            # Re-create client object for clean reconnect
            self._setup_client()
            if self._connect_once():
                self._client.loop_start()
                return

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and start the MQTT network loop (non-blocking)."""
        self._running = True
        if self._connect_once():
            self._client.loop_start()
        else:
            self._reconnect()

    def stop(self) -> None:
        """Graceful shutdown — loop_stop must precede disconnect."""
        self._running = False
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        logger.info("mqtt_stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("influx_writer_starting", extra={
        "influx_url":    INFLUX_URL,
        "influx_org":    INFLUX_ORG,
        "influx_bucket": INFLUX_BUCKET,
        "mqtt_host":     MQTT_HOST,
        "mqtt_port":     MQTT_PORT,
        "mqtt_topic":    MQTT_TOPIC,
    })

    # Validate required config
    if not INFLUX_TOKEN:
        logger.error("influx_token_missing",
                     extra={"hint": "Set INFLUX_TOKEN in .env"})
        sys.exit(1)

    influx = InfluxWriter()
    mqtt_handler = MQTTHandler(influx)

    try:
        mqtt_handler.start()
        logger.info("influx_writer_running",
                    extra={"hint": "Press Ctrl+C to stop"})
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("influx_writer_stopping")
        mqtt_handler.stop()
        logger.info("influx_writer_stopped")


if __name__ == "__main__":
    main()