"""
decoder.py — TLV protocol decoder for the Avidsen Soria solar inverter.

Ported from tinytuya/Contrib/SoriaInverterDevice.py.
Decodes Base64-encoded binary frames using the repeating TLV structure:
    [PREFIX: 3 bytes] [TAG: 1 byte] [VALUE: 2 bytes big-endian]

Notes:
  - solar_power (W) is the single authoritative power sensor, sourced from:
      DPS 25 (TAG 0x49, realtime ~2s)  or  DPS 21 (TAG 0x31, full report ~60s)
  - A1_amperes uses a 16-bit unsigned tag: 0xFFFF (65535 raw) means the DC
    current sensor is saturated / in error — these values are filtered out.
  - cos_phi is always 1.0 on this device (micro-inverter injects in-phase),
    so it is not published.
"""

import base64
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TAG identifiers (1 byte)
# ---------------------------------------------------------------------------
TAG_WIFI_SIGNAL  = 0x00
TAG_ENERGY_KWH   = 0x02  # duplicated in 0x06 and 0x4c
TAG_V2_VOLTS     = 0x07
TAG_A2_AMPERES   = 0x1a
TAG_GRID         = 0x1e  # AC power (W) - realtime (same value as 0x27)
TAG_HZ           = 0x23
TAG_W_APPARENT   = 0x27  # AC power (W) - full report
TAG_W_SOLAR      = 0x31  # DC solar power (W) — full report
TAG_V1_VOLTS     = 0x32
TAG_A1_AMPERES   = 0x33
TAG_W_ACTIVE     = 0x49  # DC solar power (W) — realtime (same value as 0x31)
TAG_TEMP1        = 0x57
TAG_TEMP2        = 0x58

# Sentinel values emitted by the device when DC current is unmeasurable
# (panel in shadow, end of day). Raw 16-bit value = 0xFFFF = 65535.
_A1_INVALID_RAW = 0xFFFF

# Sanity ceiling for DC current: above this the value is certainly garbage.
# The Soria 400W has a rated max DC current of ~10A.
_A1_MAX_AMPS = 20.0


# ---------------------------------------------------------------------------
# Unified state (merged from both DPS 25 and DPS 21)
# ---------------------------------------------------------------------------

@dataclass
class SoriaState:
    """
    Single state object updated by both realtime (DPS 25) and full report
    (DPS 21) frames. solar_power is the canonical power sensor shared by both.
    """
    # Power — updated by DPS 25 every ~2s AND by DPS 21 every ~60s
    solar_power:  Optional[int]   = None  # DC power (W)
    ac_power:     Optional[int]   = None  # AC power injected (W)

    # DC circuit — updated by DPS 21 only
    V1_volts:     Optional[float] = None  # DC voltage (V)
    A1_amperes:   Optional[float] = None  # DC current (A) — None if invalid

    # AC grid — updated by DPS 21 only
    V2_volts:     Optional[float] = None  # grid voltage (V)
    A2_amperes:   Optional[float] = None  # grid current (A)

    # Grid quality — updated by DPS 21 only
    Hz:           Optional[float] = None

    # Thermal — updated by DPS 21 only
    temp1_C:      Optional[float] = None
    temp2_C:      Optional[float] = None

    # Energy — updated by DPS 21 only
    energy_kwh:   Optional[float] = None

    # Connectivity — updated by DPS 21 only
    wifi_signal:  Optional[int]   = None


# ---------------------------------------------------------------------------
# Low-level TLV parsing
# ---------------------------------------------------------------------------

def _detect_prefix(data: bytes) -> bytes:
    counts: Counter = Counter()
    for i in range(len(data) - 5):
        candidate = data[i:i+3]
        if candidate != bytes(3):
            counts[candidate] += 1
    if not counts:
        return bytes.fromhex('010110')
    return counts.most_common(1)[0][0]


def _parse_tlv(data: bytes) -> dict:
    prefix = _detect_prefix(data)
    tags = {}
    i = 0
    while i < len(data) - 5:
        if data[i:i+3] == prefix:
            tag = data[i+3]
            val = (data[i+4] << 8) | data[i+5]
            tags[tag] = val
            i += 6
        else:
            i += 1
    return tags


def _decode_b64(b64_value: str) -> dict:
    try:
        return _parse_tlv(base64.b64decode(b64_value))
    except Exception as e:
        logger.warning("Failed to decode base64 frame: %s", e)
        return {}


def _get(tags: dict, tag_id: int) -> Optional[int]:
    return tags.get(tag_id)


def _valid_a1(raw: Optional[int]) -> Optional[float]:
    """Return DC current in A, or None if the raw value is a sentinel/garbage."""
    if raw is None:
        return None
    if raw == _A1_INVALID_RAW:
        return None
    value = round(raw / 100, 2)
    if value > _A1_MAX_AMPS:
        logger.debug("A1 filtered out (raw=%d, computed=%.2f A > max %.1f A)", raw, value, _A1_MAX_AMPS)
        return None
    return value


# ---------------------------------------------------------------------------
# Public decoders — both return partial updates to be merged into SoriaState
# ---------------------------------------------------------------------------

def decode_realtime(b64_value: str, state: SoriaState) -> bool:
    """
    Decode DPS '25' and update state in-place.
    Returns True if anything changed.
    """
    tags = _decode_b64(b64_value)
    if not tags:
        return False

    w_active = _get(tags, TAG_W_ACTIVE)
    if w_active is None:
        return False

    state.solar_power = w_active

    w_apparent = _get(tags, TAG_W_APPARENT)
    if w_apparent is None:
        return False
    
    state.ac_power  = w_apparent

def decode_full_report(b64_value: str, state: SoriaState) -> bool:
    """
    Decode DPS '21' and update state in-place.
    Returns True if anything changed.
    """
    tags = _decode_b64(b64_value)
    if not tags:
        return False

    def t(tag_id):
        return _get(tags, tag_id)

    w_solar = t(TAG_W_SOLAR)
    if w_solar is None:
        return False
    
    ac_power = t(TAG_GRID)
    if ac_power is None:
        return False

    # solar_power: take the full-report value (same measurement as DPS 25)
    state.solar_power = w_solar
    state.V1_volts    = round(t(TAG_V1_VOLTS)  / 10,  1) if t(TAG_V1_VOLTS)  is not None else None
    state.A1_amperes  = _valid_a1(t(TAG_A1_AMPERES))
    state.V2_volts    = round(t(TAG_V2_VOLTS)  / 10,  1) if t(TAG_V2_VOLTS)  is not None else None
    state.A2_amperes  = round(t(TAG_A2_AMPERES) / 100, 2) if t(TAG_A2_AMPERES) is not None else None
    state.ac_power    = ac_power
    state.Hz          = round(t(TAG_HZ)         / 100, 2) if t(TAG_HZ)         is not None else None
    state.temp1_C     = round(t(TAG_TEMP1)      / 10,  1) if t(TAG_TEMP1)      is not None else None
    state.temp2_C     = round(t(TAG_TEMP2)      / 10,  1) if t(TAG_TEMP2)      is not None else None
    state.energy_kwh  = round(t(TAG_ENERGY_KWH) / 100, 2) if t(TAG_ENERGY_KWH) is not None else None
    state.wifi_signal = t(TAG_WIFI_SIGNAL)
    return True
