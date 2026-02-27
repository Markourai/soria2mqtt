"""
decoder.py — TLV protocol decoder for the Avidsen Soria solar inverter.

Decodes Base64-encoded binary frames using the repeating TLV structure:
    [PREFIX: 3 bytes] [TAG: 1 byte] [VALUE: 2 bytes big-endian]

Notes:
  - solar_power (W) is the single authoritative power sensor, sourced from:
      DPS 25 (TAG 0x49, realtime ~2s)  or  DPS 21 (TAG 0x31, full report ~60s)
  - dc_current uses a 16-bit unsigned tag: 0xFFFF (65535 raw) means the DC
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
TAG_ENERGY_KWH   = 0x02
TAG_V2_VOLTS     = 0x07
TAG_A2_AMPERES   = 0x1a
TAG_GRID         = 0x1e  # AC power (W) — full report
TAG_HZ           = 0x23
TAG_W_APPARENT   = 0x27  # AC power (W) — realtime
TAG_W_SOLAR      = 0x31  # DC solar power (W) — full report
TAG_V1_VOLTS     = 0x32
TAG_A1_AMPERES   = 0x33
TAG_W_ACTIVE     = 0x49  # DC solar power (W) — realtime (same value as 0x31)
TAG_TEMP1        = 0x57
TAG_TEMP2        = 0x58

# ---------------------------------------------------------------------------
# Sanity thresholds — values above these are garbage (16-bit overflow at EOD)
# ---------------------------------------------------------------------------

# Sentinel value emitted when DC current is unmeasurable (end of day, shadow)
# DC current : 0xFFFF = sentinel "not measurable", ceiling at 2x rated max
_A1_INVALID_RAW = 0xFFFF
_A1_MAX_AMPS    = 20.0    # Soria 400W rated max ~10A
# AC power : Soria 400W max rated output
MAX_AC_POWER_W  = 400     # W  — configurable
# AC current : P = U*I → 400W / 220V ≈ 1.8A, ceiling at 3x for safety
MAX_AC_CURRENT_A = 5.0    # A  — configurable
# Solar power : same physical limit as AC power
MAX_SOLAR_POWER_W = 400   # W  — configurable


# ---------------------------------------------------------------------------
# Unified state — all field names are snake_case, matching JSON keys in MQTT
# ---------------------------------------------------------------------------

@dataclass
class SoriaState:
    """
    Single state object updated by both realtime (DPS 25) and full report
    (DPS 21) frames.
    Field names must exactly match the value_json keys in mqtt_client.py.
    """
    # Power — DPS 25 (~2s) AND DPS 21 (~60s)
    solar_power:  Optional[int]   = None  # DC solar panel power (W)
    ac_power:     Optional[int]   = None  # AC power injected to grid (W)

    # DC circuit — DPS 21 only
    dc_voltage:   Optional[float] = None  # DC solar panel voltage (V)
    dc_current:   Optional[float] = None  # DC solar panel current (A)

    # AC grid — DPS 21 only
    ac_voltage:   Optional[float] = None  # grid voltage (V)
    ac_current:   Optional[float] = None  # grid current (A)

    # Grid quality — DPS 21 only
    frequency:    Optional[float] = None  # grid frequency (Hz)

    # Thermal — DPS 21 only
    temp1:        Optional[float] = None  # °C
    temp2:        Optional[float] = None  # °C

    # Energy — DPS 21 only
    energy_kwh:   Optional[float] = None  # total exported (kWh)

    # Connectivity — DPS 21 only
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
        logger.debug("dc_current filtered out (raw=%d -> %.2fA > max %.1fA)", raw, value, _A1_MAX_AMPS)
        return None
    return value


def _valid_ac_power(raw: Optional[int]) -> Optional[int]:
    """Return AC power in W, or None if value exceeds device maximum."""
    if raw is None:
        return None
    if raw > MAX_AC_POWER_W:
        logger.debug("ac_power filtered out (raw=%d > max %dW)", raw, MAX_AC_POWER_W)
        return None
    return raw


def _valid_solar_power(raw: Optional[int]) -> Optional[int]:
    """Return solar power in W, or None if value exceeds device maximum."""
    if raw is None:
        return None
    if raw > MAX_SOLAR_POWER_W:
        logger.debug("solar_power filtered out (raw=%d > max %dW)", raw, MAX_SOLAR_POWER_W)
        return None
    return raw


def _valid_ac_current(raw: Optional[int]) -> Optional[float]:
    """Return AC current in A, or None if value exceeds sanity ceiling."""
    if raw is None:
        return None
    value = round(raw / 100, 2)
    if value > MAX_AC_CURRENT_A:
        logger.debug("ac_current filtered out (raw=%d -> %.2fA > max %.1fA)", raw, value, MAX_AC_CURRENT_A)
        return None
    return value


# ---------------------------------------------------------------------------
# Public decoders
# ---------------------------------------------------------------------------

def decode_realtime(b64_value: str, state: SoriaState) -> bool:
    """Decode DPS '25' (realtime, ~2s) and update state in-place.
    Returns True if data was successfully decoded."""
    tags = _decode_b64(b64_value)
    if not tags:
        return False

    w_active = _get(tags, TAG_W_ACTIVE)
    if w_active is None:
        return False

    state.solar_power = _valid_solar_power(w_active)
    state.ac_power    = _valid_ac_power(_get(tags, TAG_W_APPARENT))
    return True


def decode_full_report(b64_value: str, state: SoriaState) -> bool:
    """Decode DPS '21' (full report, ~60s) and update state in-place.
    Returns True if data was successfully decoded."""
    tags = _decode_b64(b64_value)
    if not tags:
        return False

    def t(tag_id):
        return _get(tags, tag_id)

    w_solar = t(TAG_W_SOLAR)
    if w_solar is None:
        return False

    state.solar_power = _valid_solar_power(w_solar)
    state.ac_power    = _valid_ac_power(t(TAG_GRID))
    state.dc_voltage  = round(t(TAG_V1_VOLTS)   / 10,  1) if t(TAG_V1_VOLTS)   is not None else None
    state.dc_current  = _valid_a1(t(TAG_A1_AMPERES))
    state.ac_voltage  = round(t(TAG_V2_VOLTS)   / 10,  1) if t(TAG_V2_VOLTS)   is not None else None
    state.ac_current  = _valid_ac_current(t(TAG_A2_AMPERES))
    state.frequency   = round(t(TAG_HZ)          / 100, 2) if t(TAG_HZ)         is not None else None
    state.temp1       = round(t(TAG_TEMP1)       / 10,  1) if t(TAG_TEMP1)      is not None else None
    state.temp2       = round(t(TAG_TEMP2)       / 10,  1) if t(TAG_TEMP2)      is not None else None
    state.energy_kwh  = round(t(TAG_ENERGY_KWH)  / 100, 2) if t(TAG_ENERGY_KWH) is not None else None
    state.wifi_signal = t(TAG_WIFI_SIGNAL)
    return True