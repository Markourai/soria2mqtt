"""
bridge.py — Main bridge loop.

Connects to the Soria inverter via tinytuya (persistent connection),
decodes incoming TLV frames, and publishes to MQTT.

DPS handling:
  - DPS 25 arrives every ~2s  → updates solar_power + ac_power
  - DPS 21 arrives every ~60s → updates all sensors including solar_power and ac_power
  Both update the same SoriaState and trigger an MQTT publish.

Note: we call device.receive() directly (not receive_and_update()) to
intercept all DPS keys including DPS 25 which SoriaInverterDevice
may not forward via receive_and_update().
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
RECONNECT_DELAY = 5


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

        while self._running:
            try:
                await self._run_device_loop()
            except Exception as e:
                if self._running:
                    logger.error("Device loop error: %s — retrying in %ds", e, RECONNECT_DELAY)
                    await asyncio.sleep(RECONNECT_DELAY)

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
        cfg = self._config
        logger.info("Connecting to Soria inverter %s @ %s...", cfg.DEVICE_ID, cfg.DEVICE_IP)

        self._device = SoriaInverterDevice(
            dev_id                 = cfg.DEVICE_ID,
            address                = cfg.DEVICE_IP,
            local_key              = cfg.LOCAL_KEY,
            version                = cfg.TUYA_VERSION,
            persist                = True,
            connection_timeout     = 5,
            connection_retry_limit = 999,
            connection_retry_delay = 1,
        )

        loop = asyncio.get_event_loop()

        # Initial handshake
        await loop.run_in_executor(None, self._device.receive)
        logger.info("Connected. Listening for DPS updates...")

        last_heartbeat = time.time()

        while self._running:
            # Read raw message directly — catches ALL DPS keys (25 and 21)
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
            logger.warning("Heartbeat failed: %s", e)