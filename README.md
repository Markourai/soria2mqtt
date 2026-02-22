# soria2mqtt

> **Avidsen Soria Solar Inverter (product_id 5l1ht8jygsyr1wn1) → MQTT Bridge**
> Inspired by [tydom2mqtt](https://github.com/tydom2mqtt/tydom2mqtt)

Connects to a Soria / Karst-400 solar inverter over the local network using
the Tuya v3.5 protocol, decodes the proprietary TLV binary frames,
and publishes all measurements to an MQTT broker with **Home Assistant
auto-discovery** support.

---

## Features

- Push-based — no polling, the inverter sends updates every ~2s (power) and ~60s (full report)
- Automatic reconnection to the inverter and to the MQTT broker
- Home Assistant auto-discovery: 14 sensors appear automatically, grouped as a single device
- Docker-ready, runs as a lightweight container
- MIT license

## Sensors published to Home Assistant

| Sensor | Unit | Source |
|--------|------|--------|
| Solar Power | W | DPS 25 (~2s) |
| DC  Power | W | DPS 25 (~2s) |
| DC Voltage | V | DPS 21 (~60s) |
| DC Current | A | DPS 21 (~60s) |
| DC Power | W | DPS 21 (~60s) |
| AC Voltage | V | DPS 21 (~60s) |
| AC Current | A | DPS 21 (~60s) |
| AC Power | W | DPS 21 (~60s) |
| Grid Frequency | Hz | DPS 21 (~60s) |
| Power Factor | — | DPS 21 (~60s) |
| Temperature 1 | °C | DPS 21 (~60s) |
| Temperature 2 | °C | DPS 21 (~60s) |
| Energy Exported | kWh | DPS 21 (~60s) |
| WiFi Signal | — | DPS 21 (~60s) |

## MQTT topics

```
soria2mqtt/availability      →  online / offline
soria2mqtt/state             →  JSON with all sensor values
homeassistant/sensor/soria_inverter/<sensor>/config  →  discovery (retained)
```

## Quick start

### 1. Prerequisites

- Docker + Docker Compose
- MQTT broker (Mosquitto addon in Home Assistant, or standalone)
- Your Soria device credentials (device_id, IP, local_key)
  → retrieve them with `python -m tinytuya wizard`

### 2. Clone and configure

```bash
git clone https://github.com/yourname/soria2mqtt.git
cd soria2mqtt
cp .env.example .env
nano .env   # fill in your credentials
```

### 3. Run with Docker Compose

```bash
docker compose up -d
docker compose logs -f
```

### 4. Home Assistant

Make sure the **Mosquitto MQTT integration** is configured in HA.
The Soria device and all 14 sensors will appear automatically
under **Settings → Devices & Services → MQTT**.

## Run without Docker

```bash
pip install -r requirements.txt
# Also install SoriaInverterDevice into your tinytuya Contrib:
# copy SoriaInverterDevice.py to tinytuya/Contrib/ and register it in __init__.py

cp .env.example .env && source .env  # or export variables manually
python app/main.py
```

## Protocol notes

The inverter sends Base64-encoded binary payloads on DPS keys 21 and 25.
Each payload uses a repeating TLV structure with a 3-byte prefix detected
dynamically (it varies across firmware versions):

```
[PREFIX: 3 bytes] [TAG: 1 byte] [VALUE: 2 bytes big-endian]
```

Full protocol documentation: see `docs/protocol.md` (or the Word document
generated during reverse engineering).

## License

MIT
