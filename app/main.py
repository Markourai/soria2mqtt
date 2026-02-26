#!/usr/bin/env python3
"""
soria2mqtt — Avidsen Soria Solar Inverter to MQTT Bridge
Inspired by tydom2mqtt (https://github.com/tydom2mqtt/tydom2mqtt)

Reads data from a Soria solar inverter via tinytuya,
decodes the proprietary TLV protocol,
and publishes measurements to MQTT with Home Assistant auto-discovery.
"""

import asyncio
import logging
import signal
import sys

from config import Config
from bridge import SoriaBridge

VERSION = "1.0.4"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logging.getLogger('tinytuya').setLevel(logging.WARNING)
logger = logging.getLogger('soria2mqtt')


async def main():
    logger.info("=" * 50)
    logger.info("  soria2mqtt — Soria Solar Inverter to MQTT")
    logger.info("  version: %s  ", VERSION)
    logger.info("=" * 50)

    config = Config.from_env()
    config.log()

    bridge = SoriaBridge(config)

    loop = asyncio.get_event_loop()

    def _shutdown(sig):
        logger.info("Signal %s received, shutting down...", sig.name)
        loop.create_task(bridge.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _shutdown(s))

    try:
        await bridge.start()
    except Exception as e:
        logger.error("Fatal error: %s", e)
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())