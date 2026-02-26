"""
mqtt_client.py — MQTT publisher with Home Assistant auto-discovery support.

Discovery format:
  homeassistant/sensor/<node_id>/<sensor_id>/config       →  discovery payload
  soria2mqtt/state                                        →  JSON state payload
  soria2mqtt/availability                                 →  online / offline
"""

import json
import logging
import asyncio
from typing import Any

import paho.mqtt.client as mqtt

from config import Config

logger = logging.getLogger(__name__)

AVAILABILITY_TOPIC = '{prefix}/availability'
STATE_TOPIC        = '{prefix}/state'
DISCOVERY_TOPIC    = '{ha_prefix}/sensor/{node_id}/{sensor_id}/config'

# (sensor_id, friendly_name, unit, device_class, state_class, value_template)
SENSORS = [
    # --- Power — update by DPS 25 (~2s) and DPS 21 (~60s)
    ('solar_power',  'Solar Power',      'W',   'power',         'measurement',      '{{ value_json.solar_power }}'),
    ('ac_power',     'AC Power',         'W',   'power',         'measurement',      '{{ value_json.ac_power }}'),
    # --- DC solar panel
    ('dc_voltage',   'DC Voltage',       'V',   'voltage',       'measurement',      '{{ value_json.dc_voltage }}'),
    ('dc_current',   'DC Current',       'A',   'current',       'measurement',      '{{ value_json.dc_current }}'),
    # --- AC grid
    ('ac_voltage',   'AC Voltage',       'V',   'voltage',       'measurement',      '{{ value_json.ac_voltage }}'),
    ('ac_current',   'AC Current',       'A',   'current',       'measurement',      '{{ value_json.ac_current }}'),
    # --- Grid quality
    ('frequency',    'Grid Frequency',   'Hz',  'frequency',     'measurement',      '{{ value_json.frequency }}'),
    # --- Temperatures
    ('temp1',        'Temperature 1',    '°C',  'temperature',   'measurement',      '{{ value_json.temp1 }}'),
    ('temp2',        'Temperature 2',    '°C',  'temperature',   'measurement',      '{{ value_json.temp2 }}'),
    # --- Cumulative Energy
    ('energy_kwh',   'Energy Exported',  'kWh', 'energy',        'total_increasing', '{{ value_json.energy_kwh }}'),
    # --- Connectivity
    ('wifi_signal',  'WiFi Signal',      None,  None,            'measurement',      '{{ value_json.wifi_signal }}'),
]


class MqttClient:

    def __init__(self, config: Config):
        self._config    = config
        # clean_session=True : no persistent session that would replay the will
        self._client    = mqtt.Client(client_id='soria2mqtt', clean_session=True)
        self._connected = asyncio.Event()
        self._loop      = None

        if config.MQTT_USER:
            self._client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)

        if config.MQTT_TLS:
            self._client.tls_set()

        # Last Will: published by the broker if the connection is suddenly lost
        availability_topic = AVAILABILITY_TOPIC.format(prefix=config.MQTT_TOPIC_PREFIX)
        self._client.will_set(availability_topic, payload='offline', retain=True, qos=1)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self):
        self._loop = asyncio.get_event_loop()
        self._client.connect_async(self._config.MQTT_HOST, self._config.MQTT_PORT)
        self._client.loop_start()
        await self._connected.wait()
        await self._publish_discovery()
        # Do not publish online here — bridge.py does that after successful connection to the inverter
        logger.info("MQTT ready.")

    async def disconnect(self):
        await self.publish_availability('offline')
        await asyncio.sleep(0.2)
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT disconnected.")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker %s:%s", self._config.MQTT_HOST, self._config.MQTT_PORT)
            self._loop.call_soon_threadsafe(self._connected.set)
        else:
            logger.error("MQTT connection failed (rc=%s)", rc)

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("MQTT unexpected disconnect (rc=%s), reconnecting...", rc)
            self._loop.call_soon_threadsafe(self._connected.clear)

    # ------------------------------------------------------------------
    # Home Assistant MQTT auto-discovery
    # ------------------------------------------------------------------

    async def _publish_discovery(self):
        cfg     = self._config
        suffix  = cfg.DEVICE_ID[-8:]
        node_id = f'soria_{suffix}'
        device  = {
            'identifiers':  [cfg.DEVICE_ID],
            'name':         'Soria Solar Inverter',
            'manufacturer': 'Avidsen',
            'model':        'Soria 400W',
            'sw_version':   '1.0.0',
        }
        avail_topic = AVAILABILITY_TOPIC.format(prefix=cfg.MQTT_TOPIC_PREFIX)
        state_topic = STATE_TOPIC.format(prefix=cfg.MQTT_TOPIC_PREFIX)

        for (sensor_id, name, unit, device_class, state_class, value_template) in SENSORS:
            topic = DISCOVERY_TOPIC.format(
                ha_prefix=cfg.HA_DISCOVERY_PREFIX,
                node_id=node_id,
                sensor_id=sensor_id,
            )
            payload = {
                'name':               name,
                'unique_id':          f'soria2mqtt_{suffix}_{sensor_id}',
                'state_topic':        state_topic,
                'value_template':     value_template,
                'state_class':        state_class,
                'device':             device,
            }
            if unit:
                payload['unit_of_measurement'] = unit
            if device_class:
                payload['device_class'] = device_class

            self._publish(topic, json.dumps(payload), retain=True)
            logger.debug("Discovery: %s", topic)

        # Binary sensor — producing (ON if solar_power > 0)
        binary_topic = f'{cfg.HA_DISCOVERY_PREFIX}/binary_sensor/{node_id}/producing/config'
        binary_payload = {
            'name':           'Producing',
            'unique_id':      f'soria2mqtt_{suffix}_producing',
            'state_topic':    state_topic,
            'value_template': '{{ "ON" if value_json.solar_power | int(0) > 0 else "OFF" }}',
            'device_class':   'power',
            'device':         device,
        }
        self._publish(binary_topic, json.dumps(binary_payload), retain=True)
        logger.debug("Discovery binary_sensor: %s", binary_topic)

        # Binary sensor — connectivity (ON if bridge is connected to device)
        connectivity_topic = f'{cfg.HA_DISCOVERY_PREFIX}/binary_sensor/{node_id}/connectivity/config'
        connectivity_payload = {
            'name':                  'Connected',
            'unique_id':             f'soria2mqtt_{suffix}_connectivity',
            'state_topic':           avail_topic,
            'payload_on':            'online',
            'payload_off':           'offline',
            'device_class':          'connectivity',
            'device':                device,
        }
        self._publish(connectivity_topic, json.dumps(connectivity_payload), retain=True)
        logger.debug("Discovery binary_sensor: %s", connectivity_topic)

        logger.info("MQTT discovery published (%d sensors + 2 binary_sensors).", len(SENSORS))

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------

    async def publish_state(self, state: dict[str, Any]):
        topic   = STATE_TOPIC.format(prefix=self._config.MQTT_TOPIC_PREFIX)
        # Filter out None values so HA keeps the last known value
        payload = {k: v for k, v in state.items() if v is not None}
        self._publish(topic, json.dumps(payload))
        logger.debug("State published: %s", payload)

    async def publish_availability(self, status: str):
        topic = AVAILABILITY_TOPIC.format(prefix=self._config.MQTT_TOPIC_PREFIX)
        self._publish(topic, status, retain=True)
        logger.info("Availability: %s", status)

    def _publish(self, topic: str, payload: str, retain: bool = False):
        result = self._client.publish(topic, payload, retain=retain, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("Failed to publish to %s (rc=%s)", topic, result.rc)