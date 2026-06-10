"""
MotorWatch IQ — PLC Collector (M8)
====================================
Processo standalone que lê dados do Siemens S7-1200 via OPC UA (primário)
ou Snap7 (fallback) e publica no mesmo formato MQTT do simulador.

Drop-in replacement do simulador — influx_writer.py e anomaly_detector.py
recebem os mesmos tópicos MQTT sem modificação.

Modos:
    PLC_MODE=opcua   → OpcUaCollector  (asyncua, async, DataChange subscription)
    PLC_MODE=snap7   → Snap7Collector  (python-snap7, sync, poll loop)

Configuração:
    Lida do settings.json via settings_loader. Variável de ambiente PLC_MODE
    pode sobrepor o valor do settings.json (usada pelo launcher).

Execução:
    python plc/plc_collector.py
    PLC_MODE=snap7 python plc/plc_collector.py

IFM VVB306 conversões (IODD ifm-0006F4-20240924-IODD1.1 v1.3.68.1792338):
    Temperature : °C  = raw × 0.1    (raw range -300…800)
    v-RMS       : mm/s = raw × 0.1   (raw range 0…3000)
    a-Peak      : mg   = raw × 1.0   (raw range 0…16000)
    RAW_NODATA  = 32764
    RAW_OVERLOAD= 32760

Alarm thresholds: ISO 20816-3 — lidos do settings.json via settings_loader.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Path setup ───────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from settings_loader import get_plc_config, get_thresholds

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("plc_collector")

# ─── Constants ────────────────────────────────────────────────────────────────
RAW_NODATA   = 32764
RAW_OVERLOAD = 32760

MQTT_HOST  = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))

NODES_FILE = Path(__file__).parent / "plc_nodes.json"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load_nodes() -> dict:
    if not NODES_FILE.exists():
        log.error(f"plc_nodes.json not found: {NODES_FILE}")
        return {}
    with NODES_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _check_fault(vrms_raw: int, apeak_raw: int, temp_raw: int) -> int | None:
    """
    Devolve sensor_fault_code:
        1 = NODATA (32764)
        2 = OVERLOAD (32760)
        None = OK
    """
    for raw in (vrms_raw, apeak_raw, temp_raw):
        if raw == RAW_NODATA:
            return 1
        if raw == RAW_OVERLOAD:
            return 2
    return None


def _compute_alarm(motor_id: str,
                   vrms_mms: float | None,
                   apeak_mg: float | None,
                   temp_c: float | None,
                   device_status: int,
                   is_running: bool,
                   fault_code: int | None) -> tuple[str, int, str]:
    """
    Devolve (alarm_state, alarm_state_num, alarm_state_str).
    Mesma lógica do MotorState.alarm_state no launcher.
    """
    if fault_code is not None or device_status == 4:
        return "alarm", 2, "alarm"

    thr = get_thresholds(motor_id)
    vrms  = vrms_mms  if vrms_mms  is not None else 0.0
    apeak = apeak_mg  if apeak_mg  is not None else 0.0
    temp  = temp_c    if temp_c    is not None else 0.0

    if not is_running or vrms <= 0.5:
        if (vrms  >= thr["vrms_alarm_mms"]  or
            apeak >= thr["apeak_alarm_mg"]   or
            temp  >= thr["temp_alarm_c"]):
            return "alarm", 2, "alarm"
        if (vrms  >= thr["vrms_prealarm_mms"]  or
            apeak >= thr["apeak_prealarm_mg"]   or
            temp  >= thr["temp_prealarm_c"]):
            return "prealarm", 1, "prealarm"
        return "stopped", 0, "normal"

    if (vrms  >= thr["vrms_alarm_mms"]  or
        apeak >= thr["apeak_alarm_mg"]   or
        temp  >= thr["temp_alarm_c"]):
        return "alarm", 2, "alarm"
    if (vrms  >= thr["vrms_prealarm_mms"]  or
        apeak >= thr["apeak_prealarm_mg"]   or
        temp  >= thr["temp_prealarm_c"]):
        return "prealarm", 1, "prealarm"
    return "normal", 0, "normal"


def _build_payload(motor_id: str,
                   vrms_raw: int,
                   apeak_raw: int,
                   temp_raw: int,
                   device_status: int,
                   is_running: bool,
                   source: str) -> dict:
    """Constrói payload MQTT idêntico ao do simulador."""
    fault_code = _check_fault(vrms_raw, apeak_raw, temp_raw)

    # Conversões IFM VVB306
    if vrms_raw in (RAW_NODATA, RAW_OVERLOAD):
        vrms_mms = None
    else:
        vrms_mms = round(vrms_raw * 0.1, 2)

    if apeak_raw in (RAW_NODATA, RAW_OVERLOAD):
        apeak_mg = None
        apeak_g  = None
    else:
        apeak_mg = float(apeak_raw)
        apeak_g  = round(apeak_raw / 1000.0, 4)

    if temp_raw in (RAW_NODATA, RAW_OVERLOAD):
        temp_c = None
    else:
        temp_c = round(temp_raw * 0.1, 2)

    alarm_state, alarm_num, alarm_str = _compute_alarm(
        motor_id, vrms_mms, apeak_mg, temp_c, device_status, is_running, fault_code
    )

    return {
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "motor_id":            int(motor_id),
        "channel":             f"CH{motor_id}",
        "vrms_magnitude_raw":  vrms_raw,
        "vrms_magnitude_mms":  vrms_mms,
        "apeak_magnitude_raw": apeak_raw,
        "apeak_magnitude_mg":  apeak_mg,
        "apeak_magnitude_g":   apeak_g,
        "temperature_raw":     temp_raw,
        "temperature_c":       temp_c,
        "device_status":       device_status,
        "device_status_text":  {0:"OK",1:"Maintenance required",2:"Out of specification",
                                 3:"Functional check",4:"Failure"}.get(device_status, "Unknown"),
        "alarm_state":         alarm_state,
        "alarm_state_num":     alarm_num,
        "alarm_state_str":     alarm_str,
        "is_running":          is_running,
        "sensor_fault_code":   fault_code,
        "source":              source,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MQTT publisher (sync, shared entre OpcUa e Snap7)
# ══════════════════════════════════════════════════════════════════════════════

class MqttPublisher:
    def __init__(self):
        self._client = None
        self._connected = False

    def connect(self):
        import paho.mqtt.client as mqtt
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="motorwatch_plc_collector"
        )
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        try:
            self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            self._client.loop_start()
            # Aguarda conexão até 5s
            for _ in range(50):
                if self._connected:
                    break
                time.sleep(0.1)
            if self._connected:
                log.info(f"MQTT connected → {MQTT_HOST}:{MQTT_PORT}")
            else:
                log.error(f"MQTT connection timeout → {MQTT_HOST}:{MQTT_PORT}")
        except Exception as e:
            log.error(f"MQTT connect failed: {e}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._connected = (reason_code.value == 0)
        if not self._connected:
            log.error(f"MQTT connect error: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        log.warning("MQTT disconnected")

    def publish(self, motor_id: str, payload: dict):
        if not self._client or not self._connected:
            return
        base = f"motorwatch/motors/{motor_id}"
        try:
            self._client.publish(f"{base}/telemetry",      json.dumps(payload), qos=0)
            self._client.publish(f"{base}/vrms_magnitude", json.dumps({"value": payload["vrms_magnitude_mms"],  "unit": "mm/s"}), qos=0)
            self._client.publish(f"{base}/apeak_magnitude",json.dumps({"value": payload["apeak_magnitude_mg"],  "unit": "mg"}),   qos=0)
            self._client.publish(f"{base}/temperature",    json.dumps({"value": payload["temperature_c"],       "unit": "C"}),    qos=0)
            self._client.publish(f"{base}/alarm",          json.dumps({"state": payload["alarm_state"]}),                        qos=0)
        except Exception as e:
            log.warning(f"MQTT publish error motor {motor_id}: {e}")

    def disconnect(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()


# ══════════════════════════════════════════════════════════════════════════════
# OPC UA Collector
# ══════════════════════════════════════════════════════════════════════════════

class OpcUaCollector:
    """
    Lê dados via OPC UA DataChange subscription (asyncua).
    Reconecta automaticamente com backoff exponencial.
    """

    def __init__(self, mqtt: MqttPublisher):
        self._mqtt   = mqtt
        self._nodes  = _load_nodes().get("motors", {})
        cfg          = get_plc_config().get("opcua", {})
        self._url    = cfg.get("url", "opc.tcp://192.168.0.10:4840")
        self._security = cfg.get("security", "None")
        self._user   = cfg.get("user", "") or None
        self._password = cfg.get("password", "") or None
        # Cache de valores por motor — actualizado por DataChange
        self._cache: dict[str, dict] = {
            mid: {"vrms_raw": 0, "apeak_raw": 0, "temp_raw": 0,
                  "device_status": 0, "is_running": False}
            for mid in ("1", "2", "3", "4")
        }

    async def run(self):
        backoff = 5
        while True:
            try:
                await self._connect_and_subscribe()
            except Exception as e:
                log.error(f"OPC UA error: {e}")
            log.warning(f"OPC UA disconnected — reconnecting in {backoff}s")
            # Publica sentinela de falha enquanto desconectado
            self._publish_fault_sentinels()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _connect_and_subscribe(self):
        from asyncua import Client

        log.info(f"OPC UA connecting → {self._url}  security={self._security}")

        class _Handler:
            """DataChange handler — compatível com asyncua >= 0.9 e >= 1.0."""
            def __init__(self, collector: OpcUaCollector):
                self._c = collector

            def datachange_notification(self, node, val, data):
                self._c._on_data_change(node, val)

            def event_notification(self, event):
                pass

            def status_change_notification(self, status):
                pass

        async with Client(url=self._url) as client:
            if self._user:
                await client.set_user(self._user)
            if self._password:
                await client.set_password(self._password)

            log.info("OPC UA connected ✓")
            backoff_reset = 5  # reset backoff on success

            handler = _Handler(self)
            sub = await client.create_subscription(500, handler)

            # Subscreve todos os nodes configurados
            node_map: dict = {}  # node_obj → (motor_id, field)
            for mid, fields in self._nodes.items():
                for field, node_id in fields.items():
                    try:
                        node = client.get_node(node_id)
                        await sub.subscribe_data_change(node)
                        node_map[node_id] = (mid, field)
                        log.info(f"  Subscribed Motor {mid}.{field} → {node_id}")
                    except Exception as e:
                        log.warning(f"  Node not found Motor {mid}.{field} ({node_id}): {e}")
                        # Publica sentinela para este motor
                        self._cache[mid]["vrms_raw"] = RAW_NODATA

            if not node_map:
                log.error("No OPC UA nodes subscribed — check plc_nodes.json and TIA Portal config")
                raise ConnectionError("No nodes subscribed")

            # Mantém a conexão viva — DataChange dispara _on_data_change
            while True:
                await asyncio.sleep(1.0)

    def _on_data_change(self, node, val):
        """Callback do DataChange — actualiza cache e publica MQTT."""
        # Procura motor_id e field pelo node
        node_id_str = str(node.nodeid)
        for mid, fields in self._nodes.items():
            for field, nid in fields.items():
                if nid in node_id_str or node_id_str in nid:
                    try:
                        self._cache[mid][field] = int(val) if val is not None else RAW_NODATA
                        self._publish_motor(mid)
                    except Exception as e:
                        log.warning(f"Data change error motor {mid}.{field}: {e}")
                    return

    def _publish_motor(self, motor_id: str):
        c = self._cache[motor_id]
        payload = _build_payload(
            motor_id      = motor_id,
            vrms_raw      = c["vrms_raw"],
            apeak_raw     = c["apeak_raw"],
            temp_raw      = c["temp_raw"],
            device_status = c["device_status"],
            is_running    = c["is_running"],
            source        = "opcua",
        )
        self._mqtt.publish(motor_id, payload)
        log.info(
            f"Motor {motor_id}  alarm={payload['alarm_state']:8s}  "
            f"vrms={payload['vrms_magnitude_mms']}mm/s  "
            f"temp={payload['temperature_c']}°C  "
            f"apeak={payload['apeak_magnitude_mg']}mg"
        )

    def _publish_fault_sentinels(self):
        for mid in ("1", "2", "3", "4"):
            payload = _build_payload(
                motor_id=mid, vrms_raw=RAW_NODATA, apeak_raw=RAW_NODATA,
                temp_raw=RAW_NODATA, device_status=4, is_running=False,
                source="opcua",
            )
            self._mqtt.publish(mid, payload)
        log.warning("Fault sentinels published for all motors (OPC UA disconnected)")


# ══════════════════════════════════════════════════════════════════════════════
# Snap7 Collector
# ══════════════════════════════════════════════════════════════════════════════

class Snap7Collector:
    """
    Lê dados via Snap7 polling (python-snap7, síncrono).
    Lê directamente endereços %IW da área de entradas do S7-1200.
    """

    # Offsets dentro de cada canal (bytes relativos ao iw_base)
    _OFFSETS = {
        "vrms_raw":      6,
        "apeak_raw":     16,
        "temp_raw":      28,
        "device_status": 32,
    }

    def __init__(self, mqtt: MqttPublisher):
        self._mqtt    = mqtt
        cfg           = get_plc_config()
        plc_cfg       = cfg.get("opcua", {})   # IP vem do bloco opcua
        snap7_cfg     = cfg.get("snap7", {})
        self._ip      = plc_cfg.get("ip", "192.168.0.10")
        self._rack    = int(snap7_cfg.get("rack", 0))
        self._slot    = int(snap7_cfg.get("slot", 1))
        self._interval = int(snap7_cfg.get("poll_interval_ms", 1000)) / 1000.0
        nodes         = _load_nodes()
        self._channels = nodes.get("snap7", {}).get("channels", {})

    def run(self):
        backoff = 5
        while True:
            try:
                self._connect_and_poll()
            except Exception as e:
                log.error(f"Snap7 error: {e}")
            log.warning(f"Snap7 disconnected — reconnecting in {backoff}s")
            self._publish_fault_sentinels()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _connect_and_poll(self):
        import snap7
        from snap7.util import get_int

        log.info(f"Snap7 connecting → {self._ip}  rack={self._rack}  slot={self._slot}")
        client = snap7.client.Client()
        client.connect(self._ip, self._rack, self._slot)
        log.info("Snap7 connected ✓")
        backoff_reset = 5

        while True:
            for ch_key, ch in self._channels.items():
                mid      = str(ch["motor_id"])
                iw_base  = int(ch["iw_base"])
                iw_ds    = int(ch["iw_device_status"])

                try:
                    # Lê bloco de inputs: base + 40 bytes cobre todos os offsets
                    data = client.read_area(snap7.type.Areas.PE, 0, iw_base, 40)

                    vrms_raw      = get_int(data, self._OFFSETS["vrms_raw"])
                    apeak_raw     = get_int(data, self._OFFSETS["apeak_raw"])
                    temp_raw      = get_int(data, self._OFFSETS["temp_raw"])
                    device_status = get_int(data, self._OFFSETS["device_status"])
                    is_running    = (vrms_raw > 5)  # >0.5mm/s = motor em marcha

                    payload = _build_payload(
                        motor_id      = mid,
                        vrms_raw      = vrms_raw,
                        apeak_raw     = apeak_raw,
                        temp_raw      = temp_raw,
                        device_status = device_status,
                        is_running    = is_running,
                        source        = "snap7",
                    )
                    self._mqtt.publish(mid, payload)
                    log.info(
                        f"Motor {mid}  alarm={payload['alarm_state']:8s}  "
                        f"vrms={payload['vrms_magnitude_mms']}mm/s  "
                        f"temp={payload['temperature_c']}°C  "
                        f"apeak={payload['apeak_magnitude_mg']}mg"
                    )
                except Exception as e:
                    log.warning(f"Snap7 read error motor {mid}: {e}")
                    self._publish_fault_sentinel(mid, "snap7")

            time.sleep(self._interval)

    def _publish_fault_sentinels(self):
        for mid in ("1", "2", "3", "4"):
            self._publish_fault_sentinel(mid, "snap7")
        log.warning("Fault sentinels published for all motors (Snap7 disconnected)")

    def _publish_fault_sentinel(self, motor_id: str, source: str):
        payload = _build_payload(
            motor_id=motor_id, vrms_raw=RAW_NODATA, apeak_raw=RAW_NODATA,
            temp_raw=RAW_NODATA, device_status=4, is_running=False,
            source=source,
        )
        self._mqtt.publish(motor_id, payload)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Modo: variável de ambiente prevalece sobre settings.json
    # (launcher passa PLC_MODE=opcua|snap7 no ambiente do subprocess)
    mode = os.getenv("PLC_MODE", "").lower()
    if not mode:
        mode = get_plc_config().get("default_mode", "opcua")
    if mode == "simulator":
        mode = "opcua"  # se chamado directamente com mode=simulator, usa opcua

    log.info(f"PLC Collector starting — mode={mode.upper()}")

    mqtt = MqttPublisher()
    mqtt.connect()

    if mode == "snap7":
        try:
            import snap7  # noqa: F401
        except ImportError:
            log.error("python-snap7 not installed. Run: pip install python-snap7 --break-system-packages")
            log.error("Falling back to OPC UA")
            mode = "opcua"

    if mode == "opcua":
        try:
            import asyncua  # noqa: F401
        except ImportError:
            log.error("asyncua not installed. Run: pip install asyncua --break-system-packages")
            sys.exit(1)
        collector = OpcUaCollector(mqtt)
        try:
            asyncio.run(collector.run())
        except KeyboardInterrupt:
            log.info("PLC Collector stopped by user")
        finally:
            mqtt.disconnect()

    elif mode == "snap7":
        collector = Snap7Collector(mqtt)
        try:
            collector.run()
        except KeyboardInterrupt:
            log.info("PLC Collector stopped by user")
        finally:
            mqtt.disconnect()

    else:
        log.error(f"Unknown PLC_MODE: {mode}. Use 'opcua' or 'snap7'")
        sys.exit(1)


if __name__ == "__main__":
    main()
