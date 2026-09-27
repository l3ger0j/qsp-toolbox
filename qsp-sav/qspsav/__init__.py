"""Reading, writing and checking QSP 5.7.0 and 5.9 save files."""

from __future__ import annotations

from . import v59, v570
from .codec import Encoding, Issue, SaveFormatError, detect_encoding, split_records, units_to_text

__all__ = ["Encoding", "Issue", "SaveFormatError", "detect_engine", "read_save", "write_save",
           "validate_save", "v570", "v59"]

Save = v570.Save | v59.Save


def detect_engine(data: bytes) -> str:
    """Return "5.7.0" or "5.9" from the signature and version records."""
    encoding = detect_encoding(data)
    records = split_records(data, encoding)
    if len(records) < 3:
        raise SaveFormatError("too short to be a QSP save")
    signature, version = (units_to_text(record, encoding) for record in records[:2])
    if signature != v570.SAVED_GAME_ID:
        raise SaveFormatError(f"not a QSP save: signature {signature!r}")
    if version.startswith("5.7."):
        return v570.ENGINE
    if version.startswith("5.9."):
        return v59.ENGINE
    raise SaveFormatError(f"saves of QSP {version!r} are not supported (only 5.7.x and 5.9.x)")


def read_save(data: bytes) -> tuple[Save, list[Issue]]:
    engine = detect_engine(data)
    return (v570 if engine == v570.ENGINE else v59).read(data)


def write_save(save: Save) -> bytes:
    return v570.write(save) if isinstance(save, v570.Save) else v59.write(save)


def validate_save(save: Save, game_crc: int | None = None) -> list[Issue]:
    return (v570 if isinstance(save, v570.Save) else v59).validate(save, game_crc)
