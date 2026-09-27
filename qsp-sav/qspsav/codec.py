"""Record-level codec shared by the 5.7.0 and 5.9 save formats.

A save file is a sequence of records separated by CR LF. Most records are
"encoded": every code unit is shifted down by QSP_CODREMOV (5), with the unit
equal to 5 written as -5 instead (qspCodeReCode in 5.7.0, qspEncodeString in
5.9). The shift is applied to raw UTF-16 code units (or to CP1251 bytes for
5.9 ANSI saves), so this module keeps text as code units until the shift is
done and only then turns it into a Python str.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from enum import StrEnum

CODREMOV = 5
DELIM = "\r\n"


class SaveFormatError(ValueError):
    """The data cannot be parsed as a save of the expected format."""

    def __init__(self, message: str, record: int | None = None) -> None:
        self.record = record
        super().__init__(message if record is None else f"record {record}: {message}")


class Encoding(StrEnum):
    UCS2 = "utf-16le"
    CP1251 = "cp1251"


def detect_encoding(data: bytes) -> Encoding:
    """The engines treat the data as UCS-2 when the second byte is zero."""
    return Encoding.UCS2 if len(data) >= 2 and data[1] == 0 else Encoding.CP1251


# CP1251 as the engines see it: byte -> code point, with the one byte Python's
# codec leaves undefined (0x98) mapped to itself so that nothing is lost.
_CP1251_DECODE = [
    codecs.decode(bytes([b]), "cp1251", errors="ignore") or chr(b) for b in range(256)
]
_CP1251_ENCODE = {ch: b for b, ch in enumerate(_CP1251_DECODE)}


def _unit_mask(encoding: Encoding) -> int:
    return 0xFFFF if encoding is Encoding.UCS2 else 0xFF


_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")


def _split_pair(match: re.Match) -> str:
    cp = ord(match.group()) - 0x10000
    return chr(0xD800 | cp >> 10) + chr(0xDC00 | cp & 0x3FF)


def split_records(data: bytes, encoding: Encoding) -> list[str]:
    """Split raw file data into records of code units (one str char per unit)."""
    if encoding is Encoding.UCS2:
        if len(data) % 2:
            raise SaveFormatError("UCS-2 data has an odd number of bytes")
        # The decoder joins surrogate pairs; split them back so that every
        # code unit is one character again.
        raw = _ASTRAL.sub(_split_pair, data.decode("utf-16-le", "surrogatepass"))
    else:
        raw = data.decode("latin-1")  # one char per byte
    return raw.split(DELIM)


def join_records(records: list[str], encoding: Encoding) -> bytes:
    """Inverse of split_records()."""
    raw = DELIM.join(records)
    if encoding is Encoding.UCS2:
        # Records hold code units only (no characters outside the BMP), so
        # this writes every character as exactly its unit, lone surrogates too.
        return raw.encode("utf-16-le", "surrogatepass")
    return raw.encode("latin-1")


def _shift_table(encoding: Encoding, decode: bool) -> dict[int, int]:
    mask = _unit_mask(encoding)
    minus = (-CODREMOV) & mask
    table = {}
    for unit in range(mask + 1):
        if decode:
            table[unit] = CODREMOV if unit == minus else (unit + CODREMOV) & mask
        else:
            table[unit] = minus if unit == CODREMOV else (unit - CODREMOV) & mask
    return table


_SHIFT = {
    (enc, decode): _shift_table(enc, decode)
    for enc in Encoding
    for decode in (True, False)
}


_CP1251_TO_TEXT = {b: ch for b, ch in enumerate(_CP1251_DECODE) if ord(ch) != b}
# Characters CP1251 cannot hold map to U+FFFF (not in CP1251 either), so a
# translated text is valid exactly when it has no character above U+00FF.
_CP1251_FROM_TEXT = {**{b: "\uffff" for b in range(256)}, **{ord(ch): chr(b) for ch, b in _CP1251_ENCODE.items()}}


def units_to_text(units: str, encoding: Encoding) -> str:
    """Turn a record of code units into text (surrogate pairs are joined)."""
    if encoding is Encoding.UCS2:
        return units.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return units.translate(_CP1251_TO_TEXT)


def text_to_units(text: str, encoding: Encoding) -> str:
    """Inverse of units_to_text(). Raises SaveFormatError for unencodable text."""
    if encoding is Encoding.UCS2:
        return _ASTRAL.sub(_split_pair, text)
    units = text.translate(_CP1251_FROM_TEXT)
    if units and max(units) > "\xff":
        bad = next(ch for ch, unit in zip(text, units) if unit > "\xff")
        raise SaveFormatError(f"character {bad!r} cannot be stored in a CP1251 save")
    return units


def decode_text(record: str, encoding: Encoding) -> str:
    """Decode an encoded (shifted) record."""
    return units_to_text(record.translate(_SHIFT[(encoding, True)]), encoding)


def decode_records(records: list[str], encoding: Encoding) -> list[str]:
    """decode_text() of every record, in a few calls for the whole list.

    The records are decoded joined by CR LF, which decodes to "\x12\x0f".
    That pair cannot occur inside a decoded record: the only units decoding
    to 0x12 and 0x0F are CR and LF, and a record never holds CR LF. So
    splitting at it gives back exactly the records.
    """
    shift = _SHIFT[(encoding, True)]
    texts = units_to_text(DELIM.join(records).translate(shift), encoding).split(DELIM.translate(shift))
    assert len(texts) == len(records)
    return texts


def encode_text(text: str, encoding: Encoding) -> str:
    """Encode text into a shifted record.

    NUL cannot be stored: the engines encode it as -5, the same unit as 5, and
    5.7.0 strings are zero-terminated anyway.
    """
    if "\0" in text:
        raise SaveFormatError("text contains a NUL character, which a save cannot hold")
    return text_to_units(text, encoding).translate(_SHIFT[(encoding, False)])


def utf16_key(text: str) -> bytes:
    """Sort key that orders strings by UTF-16 code units, like qspStrsComp()."""
    return text.encode("utf-16-be", "surrogatepass")


def parse_int(text: str) -> tuple[int, bool]:
    """qspStrToNum(): returns (value, is_canonical).

    Mirrors the engines: leading/trailing spaces are skipped, an optional sign
    is accepted, an empty string is 0 and anything else that is not a number
    is 0 as well. `is_canonical` tells whether writing the value back with
    str() reproduces the same text.
    """
    s = text.strip(" \t")
    value = 0
    if s:
        body = s[1:] if s[0] in "+-" else s
        if body and body.isascii() and body.isdigit():
            value = int(body)
            if s[0] == "-":
                value = -value
    return value, str(value) == text


@dataclass
class Issue:
    """A note about the data that does not stop parsing."""

    level: str  # "error" -- the engine would reject the save; "warning" -- it would not
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclass
class RecordReader:
    """Sequential reader over the records of a save."""

    records: list[str]
    encoding: Encoding
    pos: int = 0
    issues: list[Issue] = field(default_factory=list)
    _texts: list[str] | None = field(default=None, repr=False)  # all records decoded, on first use

    def _take(self, what: str) -> str:
        if self.pos >= len(self.records):
            raise SaveFormatError(f"unexpected end of data while reading {what}", self.pos)
        record = self.records[self.pos]
        self.pos += 1
        return record

    def raw(self, what: str) -> str:
        return units_to_text(self._take(what), self.encoding)

    def text(self, what: str) -> str:
        index = self.pos
        self._take(what)
        if self._texts is None:
            self._texts = decode_records(self.records, self.encoding)
        return self._texts[index]

    def int(self, what: str) -> int:
        index = self.pos
        text = self.text(what)
        value, canonical = parse_int(text)
        if not canonical:
            self.issues.append(Issue("warning", what,
                                     f"record {index}: number {text!r} is read as {value} "
                                     f"and will be written back as {value!r}"))
        return value

    def count(self, what: str) -> int:
        index = self.pos
        value = self.int(what)
        if value < 0:
            raise SaveFormatError(f"negative {what}: {value}", index)
        return value

    def finish(self) -> None:
        """Both engines end the file with CR LF, i.e. one trailing empty record."""
        rest = self.records[self.pos:]
        if rest != [""]:
            if not rest:
                self.issues.append(Issue("warning", "end of file", "missing final CR LF"))
            else:
                self.issues.append(Issue("error", "end of file",
                                         f"{len(rest) - 1} unexpected record(s) after the last field; they are dropped"))


@dataclass
class RecordWriter:
    encoding: Encoding
    records: list[str] = field(default_factory=list)

    def raw(self, text: str) -> None:
        self.records.append(text_to_units(text, self.encoding))

    def text(self, text: str) -> None:
        self.records.append(encode_text(text, self.encoding))

    def int(self, value: int) -> None:
        self.text(str(int(value)))

    def bool(self, value: bool) -> None:
        self.int(1 if value else 0)

    def to_bytes(self) -> bytes:
        return join_records(self.records + [""], self.encoding)
