"""
bridge.py — Main bridge loop.

Connects to the Soria inverter via tinytuya (persistent connection),
decodes incoming TLV frames, and publishes to MQTT.

DPS handling:
  - DPS 25 arrives every ~2s  → updates solar_power + ac_power
  - DPS 21 arrives every ~60s → updates all sensors including solar_power and ac_power
  Both update the same SoriaState and trigger an MQTT publish.
Device availability:
  - The inverter is only reachable when producing solar energy (daytime).
  - At night it shuts down completely (no DC power = no WiFi).
  - On connection loss, the heartbeat fails and triggers offline + backoff.
  - Reconnection is retried with exponential backoff (5s -> 10s -> ... -> 5min).
"""

import asyncio
import dataclasses
import logging
import time

import tinytuya
from tinytuya.Contrib.SoriaInverterDevice import SoriaInverterDevice

from config import Config
from decoder import SoriaState, decode_realtime, decode_full_report
from mqtt_client import MqttClient

logger = logging.getLogger(__name__)

DPS_REALTIME    = '25'
DPS_FULL        = '21'

# Reconnection backoff
RECONNECT_DELAY_MIN = 5
RECONNECT_DELAY_MAX = 300


class SoriaBridge:

    def __init__(self, config: Config):
        self._config  = config
        self._mqtt    = MqttClient(config)
        self._device  = None
        self._running = False
        self._state   = SoriaState()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._running = True
        await self._mqtt.connect()

        delay = RECONNECT_DELAY_MIN
        while self._running:
            try:
                await self._run_device_loop()
                delay = RECONNECT_DELAY_MIN
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Inverter unreachable: %s", e)
                await self._mqtt.publish_availability('offline')
                logger.info("Retrying in %ds...", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

        await self._mqtt.disconnect()

    async def stop(self):
        logger.info("Stopping bridge...")
        self._running = False
        if self._device:
            try:
                self._device.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Device loop
    # ------------------------------------------------------------------

    async def _run_device_loop(self):
        cfg  = self._config
        loop = asyncio.get_event_loop()
        logger.info("Connecting to Soria inverter %s @ %s...", cfg.DEVICE_ID, cfg.DEVICE_IP)

        self._device = SoriaInverterDevice(
            dev_id             = cfg.DEVICE_ID,
            address            = cfg.DEVICE_IP,
            local_key          = cfg.LOCAL_KEY,
            version            = cfg.TUYA_VERSION,
            persist            = True,
            connection_timeout = 5,
        )

        # Initial handshake
        first = await loop.run_in_executor(None, self._device.receive)
        if first is None:
            raise ConnectionError("Inverter did not respond")

        logger.info("Connected. Listening for DPS updates...")
        await self._mqtt.publish_availability('online')

        last_heartbeat = time.time()

        while self._running:
            raw = await loop.run_in_executor(None, self._device.receive)

            if raw and 'dps' in raw:
                dps = raw['dps']
                changed = False
                logger.debug("DPS received — keys: %s", list(dps.keys()))

                if DPS_REALTIME in dps:
                    changed = decode_realtime(dps[DPS_REALTIME], self._state)
                    if changed:
                        logger.info("Realtime → solar_power=%sW ac_power=%sW",
                                    self._state.solar_power, self._state.ac_power)

                if DPS_FULL in dps:
                    changed = decode_full_report(dps[DPS_FULL], self._state)
                    if changed:
                        logger.info("Full report → %s", self._state)

                if changed:
                    await self._mqtt.publish_state(dataclasses.asdict(self._state))

            elif raw:
                logger.debug("Non-DPS message: %s", raw)

            # raw is None = timeout interne tinytuya, pas une vraie déconnexion
            # On envoie un heartbeat pour vérifier que le device répond encore

            # Heartbeat
            if time.time() - last_heartbeat > cfg.HEARTBEAT_DELAY:
                await loop.run_in_executor(None, self._send_heartbeat)
                last_heartbeat = time.time()

            await asyncio.sleep(0.05)

    def _send_heartbeat(self):
        try:
            payload = self._device.generate_payload(tinytuya.HEART_BEAT)
            self._device.send(payload)
            logger.debug("Heartbeat sent.")
        except Exception as e:
            # Heartbeay failed = true disconnection
            raise ConnectionError(f"Heartbeat failed — inverter offline: {e}")