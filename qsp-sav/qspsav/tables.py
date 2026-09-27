"""Engine algorithms that depend on the generated tables: CRC, hashes, upper case."""

from __future__ import annotations

import sys
import zlib
from array import array

from ._tables import CRC_TABLE, RAND8_570, UPPER_570, UPPER_59

# 5.7.0: variables live in a flat array of 256 windows * 50 slots (variables.h).
VARS_SEEK_570 = 50
VARS_COUNT_570 = 256 * VARS_SEEK_570
# 5.9: global variables are spread over 512 buckets of at most 32 variables.
VARS_GLOBAL_BUCKETS_59 = 512
MAX_VARS_BUCKET_SIZE_59 = 32


def _to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _crc_570_step(crc: int, byte: int) -> int:
    """One step of qspCRC() of qsp-legacy on an unsigned 32-bit state."""
    return CRC_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8) ^ (0xFF000000 if crc & 0x80000000 else 0) ^ _CRC_570_XOR


_CRC_570_XOR = 0xD202EF8D
_crc_570_tables: tuple | None = None


def _crc_570_slices() -> tuple:
    """Tables that apply eight steps of qspCRC() at once.

    Every step is affine over GF(2) in the state and in the input byte, so
    eight steps are the XOR of the contributions of each part: of the state
    (two 16-bit halves) and of each input byte. The first three input bytes
    can be XORed into the state beforehand, as in a slice-by-N CRC-32; the
    fourth cannot, because the sign bit of the state decides the arithmetic
    shift of the first step and that bit must not see input byte 3 yet.
    """
    global _crc_570_tables
    if _crc_570_tables is None:
        def run(crc: int, data: list[int]) -> int:
            for byte in data:
                crc = _crc_570_step(crc, byte)
            return crc

        zero = run(0, [0] * 8)
        state = [[run(v << (8 * k), [0] * 8) ^ zero for v in range(256)] for k in range(4)]
        inputs = [[run(0, [0] * k + [v] + [0] * (7 - k)) ^ zero for v in range(256)] for k in range(8)]

        def pair(low: list[int], high: list[int]) -> list[int]:
            return [low[v & 0xFF] ^ high[v >> 8] for v in range(0x10000)]

        _crc_570_tables = (zero, pair(state[0], state[1]), pair(state[2], state[3]), inputs[3],
                           pair(inputs[4], inputs[5]), pair(inputs[6], inputs[7]))
    return _crc_570_tables


def crc_570(data: bytes) -> int:
    """qspCRC() of qsp-legacy (game.c), as the signed int stored in saves.

    crc = (qspCRCTable[(crc & 0xFF) ^ *ptr++] ^ crc >> 8) ^ 0xD202EF8D;
    `crc` is a signed int, so `crc >> 8` is an arithmetic shift. Computed
    eight bytes at a time (see _crc_570_slices), which is about four times
    faster than byte by byte.
    """
    zero, state_low, state_high, input3, input45, input67 = _crc_570_slices()
    crc = 0
    body = len(data) & ~7
    words = memoryview(data)[:body].cast("Q")
    if sys.byteorder == "big":
        words = array("Q", words)
        words.byteswap()
    for word in words:
        crc ^= word & 0xFFFFFF
        crc = (state_low[crc & 0xFFFF] ^ state_high[crc >> 16] ^ input3[(word >> 24) & 0xFF]
               ^ input45[(word >> 32) & 0xFFFF] ^ input67[word >> 48] ^ zero)
    for byte in data[body:]:
        crc = _crc_570_step(crc, byte)
    return _to_int32(crc)


def crc_59(data: bytes) -> int:
    """qspCRC() of qsp 5.9 (game.c): the standard CRC-32, stored as a signed int."""
    return _to_int32(zlib.crc32(data))


def upper_570(text: str) -> str:
    """qspUpperStr() of qsp-legacy, applied per UTF-16 code unit."""
    # The tables only hold BMP code units; characters outside the BMP are
    # surrogate pairs in the engines and are never case-mapped.
    return text.translate(UPPER_570)


def upper_59(text: str) -> str:
    """qspUpperStr() of qsp 5.9, applied per UTF-16 code unit."""
    return text.translate(UPPER_59)


def _utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def var_window_570(name: str) -> int:
    """First slot of the 50-slot window qspVarReference() scans for `name`.

    `name` must already be the stored form: no '$' prefix, upper-cased.
    bCode = qspRand8[bCode ^ QSP_MBTOSB(ch)] with QSP_MBTOSB(a) = a % 256.
    """
    code = 0
    for unit in _utf16_units(name):
        code = RAND8_570[code ^ (unit & 0xFF)]
    return code * VARS_SEEK_570


def var_bucket_59(name: str) -> int:
    """Global bucket index of `name` in 5.9 (qspGetNameHash() % 512).

    nameHash = nameHash * 31 + (unsigned char)*pos, starting from 7.
    """
    name_hash = 7
    for unit in _utf16_units(name):
        name_hash = (name_hash * 31 + (unit & 0xFF)) & 0xFFFFFFFF
    return name_hash % VARS_GLOBAL_BUCKETS_59
