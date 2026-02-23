"""
Configuration — loaded from environment variables.
All settings can also be placed in a .env file (loaded by Docker Compose).
"""

import logging
import os

logger = logging.getLogger(__name__)


class Config:
    # --- Tuya device ---
    DEVICE_ID:   str
    DEVICE_IP:   str
    LOCAL_KEY:   str
    TUYA_VERSION: float

    # --- MQTT broker ---
    MQTT_HOST:     str
    MQTT_PORT:     int
    MQTT_USER:     str
    MQTT_PASSWORD: str
    MQTT_TLS:      bool

    # --- MQTT topics ---
    MQTT_TOPIC_PREFIX: str   # e.g. soria2mqtt
    HA_DISCOVERY_PREFIX: str  # e.g. homeassistant

    # --- Behaviour ---
    LOG_LEVEL:        str
    HEARTBEAT_DELAY:  int   # seconds between keep-alive pings to the device

    @classmethod
    def from_env(cls) -> 'Config':
        c = cls()

        # Tuya
        c.DEVICE_ID    = _require('SORIA_DEVICE_ID')
        c.DEVICE_IP    = _require('SORIA_DEVICE_IP')
        c.LOCAL_KEY    = _require('SORIA_LOCAL_KEY')
        c.TUYA_VERSION = float(os.getenv('SORIA_TUYA_VERSION', '3.5'))

        # MQTT
        c.MQTT_HOST     = os.getenv('MQTT_HOST', 'localhost')
        c.MQTT_PORT     = int(os.getenv('MQTT_PORT', '1883'))
        c.MQTT_USER     = os.getenv('MQTT_USER', '')
        c.MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', '')
        c.MQTT_TLS      = os.getenv('MQTT_TLS', 'false').lower() == 'true'

        # Topics
        c.MQTT_TOPIC_PREFIX  = os.getenv('MQTT_TOPIC_PREFIX', 'soria2mqtt')
        c.HA_DISCOVERY_PREFIX = os.getenv('HA_DISCOVERY_PREFIX', 'homeassistant')

        # Behaviour
        c.LOG_LEVEL       = os.getenv('LOG_LEVEL', 'INFO').upper()
        c.HEARTBEAT_DELAY = int(os.getenv('HEARTBEAT_DELAY', '20'))

        logging.getLogger().setLevel(c.LOG_LEVEL)
        return c

    def log(self):
        logger.info("Device  : %s @ %s (Tuya v%s)", self.DEVICE_ID, self.DEVICE_IP, self.TUYA_VERSION)
        logger.info("MQTT    : %s:%s (tls=%s)", self.MQTT_HOST, self.MQTT_PORT, self.MQTT_TLS)
        logger.info("Topic   : %s/#", self.MQTT_TOPIC_PREFIX)
        logger.info("Discovery prefix : %s", self.HA_DISCOVERY_PREFIX)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val