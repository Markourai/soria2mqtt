"""
bridge.py — Main bridge loop.

Connects to the Soria inverter via tinytuya (persistent connection),
decodes incoming TLV frames, and publishes to MQTT.
Handles automatic reconnection on both sides.
"""

import asyncio
import dataclasses
import logging
import time

import tinytuya
from tinytuya.Contrib.SoriaInverterDevice import SoriaInverterDevice

from config import Config
from decoder import decode_realtime, decode_full_report
from mqtt_client import MqttClient

logger = logging.getLogger(__name__)

DPS_REALTIME    = '25'
DPS_FULL        = '21'
DPS_STATUS      = '24'
RECONNECT_DELAY = 5   # seconds before retrying after a connection error


class SoriaBridge:

    def __init__(self, config: Config):
        self._config   = config
        self._mqtt     = MqttClient(config)
        self._device   = None
        self._running  = False
        self._state    = {}  # accumulated state dict published to MQTT

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

        # Initial handshake
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._device.receive)
        logger.info("Connected to inverter. Listening for updates...")

        last_heartbeat = time.time()

        while self._running:
            # Receive one message (blocking, run in thread pool)
            raw = await loop.run_in_executor(None, self._device.receive_and_update)

            if raw and 'dps' in raw:
                dps_keys = raw['dps'].keys()

                if DPS_REALTIME in dps_keys:
                    b64 = raw['dps'][DPS_REALTIME]
                    data = decode_realtime(b64)
                    if data:
                        logger.debug("Realtime: %s", data)
                        self._state.update(dataclasses.asdict(data))
                        await self._mqtt.publish_state(self._state)

                if DPS_FULL in dps_keys:
                    b64 = raw['dps'][DPS_FULL]
                    data = decode_full_report(b64)
                    if data:
                        logger.info("Full report: %s", data)
                        self._state.update(dataclasses.asdict(data))
                        await self._mqtt.publish_state(self._state)

            # Heartbeat to keep connection alive
            if time.time() - last_heartbeat > self._config.HEARTBEAT_DELAY:
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
