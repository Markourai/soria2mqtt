"""
decoder.py — TLV protocol decoder for the Avidsen Soria solar inverter.

Ported from tinytuya/Contrib/SoriaInverterDevice.py.
Decodes Base64-encoded binary frames using the repeating TLV structure:
    [PREFIX: 3 bytes] [TAG: 1 byte] [VALUE: 2 bytes big-endian]
"""

import base64
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TAG identifiers (1 byte)
# ---------------------------------------------------------------------------
TAG_WIFI_SIGNAL  = 0x00
TAG_ENERGY_KWH   = 0x02  # duplicated in 0x06 and 0x4c
TAG_V2_VOLTS     = 0x07
TAG_A2_AMPERES   = 0x1a
TAG_W2_WATTS     = 0x1e
TAG_HZ           = 0x23
TAG_W_APPARENT   = 0x27  # duplicated in 0x2a
TAG_W1_WATTS     = 0x31
TAG_V1_VOLTS     = 0x32
TAG_A1_AMPERES   = 0x33
TAG_W_ACTIVE     = 0x49  # same as 0x31 in realtime frame
TAG_COS_PHI      = 0x4a
TAG_TEMP1        = 0x57
TAG_TEMP2        = 0x58


# ---------------------------------------------------------------------------
# Decoded data structures
# ---------------------------------------------------------------------------

@dataclass
class RealtimeData:
    """Decoded from DPS '25' — published every ~2 seconds."""
    W_active:   Optional[int] = None   # active power (W)
    W_apparent: Optional[int] = None   # apparent power (VA)


@dataclass
class FullReportData:
    """Decoded from DPS '21' — published every ~60 seconds."""
    # DC circuit (panel / battery)
    V1_volts:    Optional[float] = None
    A1_amperes:  Optional[float] = None
    W1_watts:    Optional[int]   = None
    # AC grid
    V2_volts:    Optional[float] = None
    A2_amperes:  Optional[float] = None
    W2_watts:    Optional[int]   = None
    # Grid quality
    Hz:          Optional[float] = None
    cos_phi:     Optional[float] = None
    # Thermal
    temp1_C:     Optional[float] = None
    temp2_C:     Optional[float] = None
    # Energy
    energy_kwh:  Optional[float] = None
    # Connectivity
    wifi_signal: Optional[int]   = None


# ---------------------------------------------------------------------------
# Low-level TLV parsing
# ---------------------------------------------------------------------------

def _detect_prefix(data: bytes) -> bytes:
    """Return the most frequent 3-byte sequence — that is the TLV prefix."""
    counts: Counter = Counter()
    for i in range(len(data) - 5):
        candidate = data[i:i+3]
        if candidate != bytes(3):
            counts[candidate] += 1
    if not counts:
        return bytes.fromhex('010110')
    return counts.most_common(1)[0][0]


def _parse_tlv(data: bytes) -> dict:
    """Extract all {tag: value} pairs from a binary TLV frame."""
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
    """Decode a Base64 DPS string into a tag dict. Returns {} on error."""
    try:
        return _parse_tlv(base64.b64decode(b64_value))
    except Exception as e:
        logger.warning("Failed to decode base64 frame: %s", e)
        return {}


def _get(tags: dict, tag_id: int) -> Optional[int]:
    return tags.get(tag_id)


# ---------------------------------------------------------------------------
# Public decoders
# ---------------------------------------------------------------------------

def decode_realtime(b64_value: str) -> Optional[RealtimeData]:
    """Decode DPS '25' — real-time power frame."""
    tags = _decode_b64(b64_value)
    if not tags:
        return None

    w_active = _get(tags, TAG_W_ACTIVE)
    if w_active is None:
        return None

    return RealtimeData(
        W_active   = w_active,
        W_apparent = _get(tags, TAG_W_APPARENT),
    )


def decode_full_report(b64_value: str) -> Optional[FullReportData]:
    """Decode DPS '21' — full electrical report frame."""
    tags = _decode_b64(b64_value)
    if not tags:
        return None

    def t(tag_id):
        return _get(tags, tag_id)

    if t(TAG_W1_WATTS) is None:
        return None

    return FullReportData(
        V1_volts    = round(t(TAG_V1_VOLTS)   / 10,  1) if t(TAG_V1_VOLTS)   is not None else None,
        A1_amperes  = round(t(TAG_A1_AMPERES)  / 100, 2) if t(TAG_A1_AMPERES) is not None else None,
        W1_watts    = t(TAG_W1_WATTS),
        V2_volts    = round(t(TAG_V2_VOLTS)   / 10,  1) if t(TAG_V2_VOLTS)   is not None else None,
        A2_amperes  = round(t(TAG_A2_AMPERES)  / 100, 2) if t(TAG_A2_AMPERES) is not None else None,
        W2_watts    = t(TAG_W2_WATTS),
        Hz          = round(t(TAG_HZ)          / 100, 2) if t(TAG_HZ)         is not None else None,
        cos_phi     = round(t(TAG_COS_PHI)     / 100, 2) if t(TAG_COS_PHI)    is not None else None,
        temp1_C     = round(t(TAG_TEMP1)       / 10,  1) if t(TAG_TEMP1)      is not None else None,
        temp2_C     = round(t(TAG_TEMP2)       / 10,  1) if t(TAG_TEMP2)      is not None else None,
        energy_kwh  = round(t(TAG_ENERGY_KWH)  / 100, 2) if t(TAG_ENERGY_KWH) is not None else None,
        wifi_signal = t(TAG_WIFI_SIGNAL),
    )
