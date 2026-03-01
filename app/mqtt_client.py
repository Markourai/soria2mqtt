"""
mqtt_client.py — MQTT publisher with Home Assistant auto-discovery support.

Uses aiomqtt (async-native MQTT client, actively maintained).
Le client reste dans son context manager pendant toute la durée de vie du bridge.

Discovery format:
  homeassistant/binary_sensor/soria_<suffix>/connectivity/config  (retained)
  homeassistant/binary_sensor/soria_<suffix>/producing/config     (retained)
  homeassistant/sensor/soria_<suffix>/<sensor_id>/config          (retained)
  soria2mqtt/state         →  JSON state payload
  soria2mqtt/availability  →  online / offline (retained)
"""

import json
import logging
from typing import Any

import aiomqtt

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
        self._config      = config
        self._client: aiomqtt.Client | None = None

        suffix            = config.DEVICE_ID[-8:]
        self._suffix      = suffix
        self._node_id     = f'soria_{suffix}'
        self._avail_topic = AVAILABILITY_TOPIC.format(prefix=config.MQTT_TOPIC_PREFIX)
        self._state_topic = STATE_TOPIC.format(prefix=config.MQTT_TOPIC_PREFIX)
        self._device      = {
            'identifiers':  [config.DEVICE_ID],
            'name':         'Soria Solar Inverter',
            'manufacturer': 'Avidsen',
            'model':        'Soria 400W',
            'sw_version':   '1.1.0',
        }

    def _make_client(self) -> aiomqtt.Client:
        """Instancie le client aiomqtt avec la config courante."""
        cfg = self._config
        kwargs = dict(
            hostname   = cfg.MQTT_HOST,
            port       = cfg.MQTT_PORT,
            identifier = 'soria2mqtt',
            will       = aiomqtt.Will(
                topic   = self._avail_topic,
                payload = 'offline',
                qos     = 1,
                retain  = True,
            ),
        )
        if cfg.MQTT_USER:
            kwargs['username'] = cfg.MQTT_USER
            kwargs['password'] = cfg.MQTT_PASSWORD
        if cfg.MQTT_TLS:
            import ssl
            kwargs['tls_context'] = ssl.create_default_context()
        return aiomqtt.Client(**kwargs)

    # ------------------------------------------------------------------
    # Context manager — à utiliser avec `async with mqtt_client:`
    # ------------------------------------------------------------------

    async def __aenter__(self):
        self._client = self._make_client()
        await self._client.__aenter__()
        logger.info("Connected to MQTT broker %s:%s",
                    self._config.MQTT_HOST, self._config.MQTT_PORT)
        await self._publish_discovery()
        logger.info("MQTT ready.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.publish_availability('offline')
        if self._client:
            await self._client.__aexit__(exc_type, exc_val, exc_tb)
        logger.info("MQTT disconnected.")

    # ------------------------------------------------------------------
    # Home Assistant MQTT auto-discovery
    # ------------------------------------------------------------------

    async def _publish_discovery(self):
        for (sensor_id, name, unit, device_class, state_class, value_template) in SENSORS:
            topic = DISCOVERY_TOPIC.format(
                ha_prefix = self._config.HA_DISCOVERY_PREFIX,
                node_id   = self._node_id,
                sensor_id = sensor_id,
            )
            payload: dict = {
                'name':           name,
                'unique_id':      f'soria2mqtt_{self._suffix}_{sensor_id}',
                'state_topic':    self._state_topic,
                'value_template': value_template,
                'state_class':    state_class,
                'device':         self._device,
            }
            if unit:
                payload['unit_of_measurement'] = unit
            if device_class:
                payload['device_class'] = device_class

            await self._publish(topic, json.dumps(payload), retain=True)
            logger.debug("Discovery: %s", topic)

        # Binary sensor — producing
        await self._publish(
            f'{self._config.HA_DISCOVERY_PREFIX}/binary_sensor/{self._node_id}/producing/config',
            json.dumps({
                'name':           'Producing',
                'unique_id':      f'soria2mqtt_{self._suffix}_producing',
                'state_topic':    self._state_topic,
                'value_template': '{{ "ON" if value_json.solar_power | int(0) > 0 else "OFF" }}',
                'device_class':   'power',
                'device':         self._device,
            }),
            retain=True,
        )
        logger.debug("Discovery binary_sensor: producing")

        # Binary sensor — connectivity
        await self._publish(
            f'{self._config.HA_DISCOVERY_PREFIX}/binary_sensor/{self._node_id}/connectivity/config',
            json.dumps({
                'name':        'Connected',
                'unique_id':   f'soria2mqtt_{self._suffix}_connectivity',
                'state_topic': self._avail_topic,
                'payload_on':  'online',
                'payload_off': 'offline',
                'device_class':'connectivity',
                'device':      self._device,
            }),
            retain=True,
        )
        logger.debug("Discovery binary_sensor: connectivity")

        logger.info("MQTT discovery published (%d sensors + 2 binary_sensors).", len(SENSORS))

    # ------------------------------------------------------------------
    # State & availability publishing
    # ------------------------------------------------------------------

    async def publish_state(self, state: dict[str, Any]):
        payload = {k: v for k, v in state.items() if v is not None}
        await self._publish(self._state_topic, json.dumps(payload))
        logger.debug("State published: %s", payload)

    async def publish_availability(self, status: str):
        await self._publish(self._avail_topic, status, retain=True)
        logger.info("Availability: %s", status)

    async def _publish(self, topic: str, payload: str, retain: bool = False):
        try:
            await self._client.publish(topic, payload, qos=1, retain=retain)
            logger.debug("-> %s : %s", topic, payload)
        except Exception as e:
            logger.warning("Failed to publish to %s: %s", topic, e)