# MotorWatch IQ

![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=flat&logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/PySide6-6.x-41CD52?style=flat&logo=qt&logoColor=white)
![InfluxDB](https://img.shields.io/badge/InfluxDB-2.7-22ADF6?style=flat&logo=influxdb&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-13-F46800?style=flat&logo=grafana&logoColor=white)
![MQTT](https://img.shields.io/badge/MQTT-Mosquitto-660066?style=flat&logo=eclipsemosquitto&logoColor=white)
![scikit-learn](https://img.shields.io/badge/scikit--learn-Isolation_Forest-F7931E?style=flat&logo=scikitlearn&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-a6e3a1?style=flat)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat&logo=windows&logoColor=white)

End-to-end IIoT motor condition monitoring system — from IFM VVB306 vibration and temperature sensors on a Siemens S7-1200 PLC, through a full data pipeline, to machine learning anomaly detection, Grafana dashboards, and bilingual maintenance reports. 

**Portfolio page:** https://phernando82.github.io/motorwatch-iq-page/  
**PLC/HMI project:** https://github.com/Phernando82/motorwatch-iq-plc-hmi

---

## Overview

MotorWatch IQ monitors 4 industrial motors in real time, collecting vibration (v-RMS mm/s), acceleration (a-Peak mg), and temperature (°C) data from IFM VVB306 IO-Link sensors. The system was built simulator-first — the full analytics pipeline was developed and validated before any PLC was connected, enabling rapid iteration independent of hardware availability.

```
IFM VVB306 sensors
      │
  S7-1200 PLC ──OPC UA──► plc_collector.py
                                │
                           Mosquitto MQTT
                                │
                        influx_writer.py
                                │
                          InfluxDB 2.7
                          ┌─────┴──────┐
                     Grafana 13    anomaly_detector.py
                     dashboards         │
                                  HTML reports (EN/ES)
```

---

## Features

- **PySide6 desktop application** — single launcher controls all services (Mosquitto, InfluxDB writer, analytics engine, PLC collector) as supervised subprocesses with live console output
- **Motor simulator** — injects realistic fault scenarios per motor (normal, thermal drift, vibration drift, impact spike) without physical hardware
- **OPC UA primary / Snap7 fallback** — IEC 62541 compliant PLC communication with automatic failover and exponential backoff reconnection
- **InfluxDB 2.7 time-series storage** — Flux query language, `motor_telemetry` and `motor_anomaly` measurements, sentinel values for fault states
- **Isolation Forest anomaly detection** — unsupervised scoring (0.0–1.0) trained on CWRU bearing dataset, no labelled fault data required
- **Trend analysis** — linear regression on v-RMS windows, outputs slope (mm/s/h) and ETA to pre-alarm/alarm thresholds
- **CWRU fault classification** — identifies normal, thermal, inner/outer race, ball fault with probability score
- **Alert state machine** — OK → WATCH → WARNING → CRITICAL with hysteresis, Windows native notifications on CRITICAL
- **Bilingual HTML reports** — EN/ES maintenance reports with executive summary, intervention plan, and alert history (ISO 20816-3 thresholds)
- **Grafana 13 dashboards** — sensor dashboard (alarm state timeline, gauges, sparklines) + anomaly dashboard (scores, trends, fault classification table)
- **Catppuccin Mocha palette** — consistent visual identity across PySide6 UI, Grafana, and generated reports

---

## Stack

| Layer | Technology |
|-------|-----------|
| Desktop UI | PySide6 6.x |
| PLC communication | asyncua (OPC UA), python-snap7 (fallback) |
| Message broker | Mosquitto MQTT 3.1.1 |
| Time-series DB | InfluxDB 2.7.12 |
| Dashboards | Grafana 13.0.2 |
| ML / Analytics | scikit-learn (Isolation Forest), NumPy, pandas |
| Report generation | Python (Jinja2-style templating, HTML/CSS) |
| Hardware | Siemens S7-1200 CPU 1214C, IFM VVB306 ×4, IFM AL1901 |

---

## Project Structure

```
Motor_Watch_IQ/
├── main.py                  # PySide6 launcher entry point
├── launcher_tab.py          # Service orchestration tab
├── simulator_tab.py         # Motor simulator tab
├── analytics_tab.py         # Live analytics + alert tab
├── settings_tab.py          # OPC UA / Snap7 / threshold settings
│
├── plc_collector.py         # OPC UA collector (asyncua) + Snap7 fallback
├── simulator.py             # MQTT sensor data generator
├── influx_writer.py         # MQTT → InfluxDB writer
├── anomaly_detector.py      # ML analytics engine (30 s cycle)
│
├── series_trainer.py        # Isolation Forest model training
├── cwru_trainer.py          # CWRU fault classifier training
├── models/                  # Trained .pkl model files
│
├── reports/                 # Generated HTML maintenance reports
├── config/                  # Connection and threshold configuration
└── grafana/                 # Dashboard JSON exports
```

---

## Quick Start

### Prerequisites

- Python 3.14+
- [Mosquitto MQTT broker](https://mosquitto.org/download/)
- [InfluxDB 2.7](https://www.influxdata.com/downloads/)
- [Grafana 13](https://grafana.com/grafana/download)

### Installation

```bash
git clone https://github.com/Phernando82/motor_watch_IQ.git
cd motor_watch_IQ
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### Configuration

Edit `config/settings.json` with your InfluxDB token, bucket, and OPC UA endpoint:

```json
{
  "influxdb": {
    "url": "http://localhost:8086",
    "token": "your-token",
    "org": "motorwatch",
    "bucket": "motors"
  },
  "opcua": {
    "url": "opc.tcp://192.168.0.10:4840"
  }
}
```

### Run

```bash
python main.py
```

Select **Simulator** as data source in the Launcher tab and click **Start All**. Open Grafana at `http://localhost:3000` to view dashboards.

### Train ML models

```bash
python cwru_trainer.py       # download CWRU dataset and train fault classifier
python series_trainer.py     # train Isolation Forest per motor
```

---

## PLC / HMI Hardware

The system is designed for a Siemens S7-1200 CPU 1214C with 4× IFM VVB306 IO-Link vibration/temperature sensors connected via an IFM AL1901 IO-Link master over PROFINET. A Siemens MTP700 Unified Basic HMI provides shop-floor operator interface.

See the [motorwatch-iq-plc-hmi](https://github.com/Phernando82/motorwatch-iq-plc-hmi) repository for the full TIA Portal V20 project.

---

## ISO 20816-3 Thresholds

Default thresholds follow ISO 20816-3 Group 2 (15–75 kW machines):

| Parameter | Pre-alarm | Alarm |
|-----------|-----------|-------|
| v-RMS (mm/s) | 4.5 | 7.1 |
| a-Peak (mg) | 1000 | 2000 |
| Temperature (°C) | 75 | 90 |

Thresholds are configurable per motor from the Settings tab or HMI.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Developed by [Fernando Valverde](https://github.com/Phernando82) · [Portfolio](https://phernando82.github.io/portfolio) · [LinkedIn](https://www.linkedin.com/in/fernandos-valverde/)*