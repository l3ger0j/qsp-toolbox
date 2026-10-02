"""Input archives, resource lookup and SWF font inspection."""

import json
import re
import struct
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from .constants import GAME_SOURCE_EXTENSIONS, MEDIA_EXTENSIONS
from .findings import Finding

ARCHIVE_EXTENSIONS = (".aqsp", ".zip")


class ResourceIndex:
    def __init__(self, files: list[str]):
        self.files = sorted(files)
        self.by_lower = {f.lower(): f for f in self.files}
        self.basenames = {f.rsplit("/", 1)[-1].lower() for f in self.files}
        dirs: set[str] = set()
        for f in self.files:
            p = PurePosixPath(f)
            for parent in list(p.parents)[:-1]:
                dirs.add(str(parent))
        self.dirs = sorted(dirs)
        self.dirs_by_lower = {d.lower(): d for d in self.dirs}
        self._file_re = self._build(self.files, file_mode=True)
        self._dir_re = self._build(self.dirs, file_mode=False)

    @staticmethod
    def _build(paths: list[str], file_mode: bool) -> re.Pattern[str] | None:
        if not paths:
            return None
        alts = []
        for path in sorted(paths, key=len, reverse=True):
            parts = [re.escape(part) for part in path.split("/")]
            alts.append(r"[\\/]".join(parts))
        tail = r"(?![\w\-])" if file_mode else r"(?=[\\/])"
        return re.compile(r"(?<![\w.\-/\\])(?:" + "|".join(alts) + ")" + tail, re.IGNORECASE)

    def canonical_file(self, ref: str) -> str | None:
        return self.by_lower.get(ref.replace("\\", "/").lower())

    def has_basename(self, name: str) -> bool:
        return name.lower() in self.basenames

    def match_spans(self, text: str) -> list[tuple[int, int]]:
        return [(m.start(), m.end()) for m in self._file_re.finditer(text)] if self._file_re else []

    def fix_paths(self, text: str) -> list[tuple[int, int, str, str]]:
        """Return (start, end, old, new) for file and directory references in `text`."""
        results: list[tuple[int, int, str, str]] = []
        covered: list[tuple[int, int]] = []
        if self._file_re:
            for m in self._file_re.finditer(text):
                canonical = self.by_lower[m.group(0).replace("\\", "/").lower()]
                covered.append((m.start(), m.end()))
                if m.group(0) != canonical:
                    results.append((m.start(), m.end(), m.group(0), canonical))
        if self._dir_re:
            for m in self._dir_re.finditer(text):
                if any(s <= m.start() < e for s, e in covered):
                    continue
                canonical = self.dirs_by_lower[m.group(0).replace("\\", "/").lower()]
                sep = text[m.end()] if m.end() < len(text) else ""
                old = m.group(0) + sep
                new = canonical + "/"
                if old != new:
                    results.append((m.start(), m.end() + len(sep), old, new))
        return results


MISSING_REF_RE = re.compile(
    r"(?<![\w.\-/\\<])((?:[\w\-.]+[\\/])*[\w\-.]*\.(?:" + "|".join(MEDIA_EXTENSIONS) + r"))(?![\w\-])",
    re.IGNORECASE,
)


def swf_font_names(data: bytes) -> list[tuple[str, int, bool, bool]]:
    """Return (font name, glyph count, bold, italic) for DefineFont2/3 tags in a SWF file."""
    try:
        sig = data[:3]
        if sig == b"CWS":
            body = zlib.decompress(data[8:])
        elif sig == b"FWS":
            body = data[8:]
        else:
            return []
        nbits = body[0] >> 3
        pos = (5 + nbits * 4 + 7) // 8 + 4
        fonts = []
        while pos + 2 <= len(body):
            code_len = struct.unpack_from("<H", body, pos)[0]
            pos += 2
            code, length = code_len >> 6, code_len & 0x3F
            if length == 0x3F:
                length = struct.unpack_from("<I", body, pos)[0]
                pos += 4
            if code == 0:
                break
            if code in (48, 75):  # DefineFont2, DefineFont3
                flags = body[pos + 2]
                name_len = body[pos + 4]
                name = body[pos + 5 : pos + 5 + name_len].rstrip(b"\0").decode("utf-8", errors="replace")
                glyphs = struct.unpack_from("<H", body, pos + 5 + name_len)[0]
                fonts.append((name, glyphs, bool(flags & 1), bool(flags & 2)))
            pos += length
        return fonts
    except (IndexError, struct.error, zlib.error):
        return []


def _zip_name(info: zipfile.ZipInfo) -> str:
    """AeroQSP's FZip always decodes file names as UTF-8, even without the UTF-8 flag."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp866")


def load_input(path: Path, exclude: Path | None = None) -> tuple[dict[str, bytes], list[Finding]]:
    """Read a game folder or archive, skipping `exclude` and earlier aero2qspider output inside the folder."""
    findings: list[Finding] = []
    files: dict[str, bytes] = {}
    if path.is_dir():
        skip = [exclude.resolve()] if exclude else []
        skip += [report.parent.resolve() for report in path.rglob("CONVERSION_REPORT.md") if report.parent != path]
        for p in sorted(path.rglob("*")):
            if p.is_file() and not any(p.resolve().is_relative_to(folder) for folder in skip):
                files[p.relative_to(path).as_posix()] = p.read_bytes()
        return files, findings
    data = path.read_bytes()
    if data[:2] == b"PK":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = _zip_name(info)
                if not info.flag_bits & 0x800 and any(ord(c) > 127 for c in name):
                    findings.append(
                        Finding("info", "archive", f"`{name}` is stored without the UTF-8 flag; decoded as UTF-8 like AeroQSP did")
                    )
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    findings.append(
                        Finding("warn", "archive", f"`{name}`: compression method {info.compress_type} was not supported by AeroQSP")
                    )
                files[name] = z.read(info)
        return files, findings
    files[path.name] = data
    return files, findings


def strip_common_root(files: dict[str, bytes]) -> dict[str, bytes]:
    """If every file sits in one top-level folder (zip of a folder), drop that folder."""
    tops = {name.split("/", 1)[0] for name in files}
    if len(tops) == 1 and all("/" in name for name in files):
        top = tops.pop()
        return {name[len(top) + 1 :]: data for name, data in files.items()}
    return files


def pick_main_game(names: list[str]) -> str | None:
    games = [n for n in names if n.lower().endswith(GAME_SOURCE_EXTENSIONS)]
    if not games:
        return None
    for n in games:
        if n.lower() == "game.qsp":
            return n
    root = [n for n in games if "/" not in n]
    return (root or games)[0]


_TRANSLIT = str.maketrans(
    dict(zip("абвгдеёзийклмнопрстуфхцыэ", "abvgdeeziyklmnoprstufhcye", strict=True))
    | {"ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ь": "", "ю": "yu", "я": "ya"}
)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFC", value).lower().translate(_TRANSLIT)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "aero-game"


def decode_text(data: bytes) -> str:
    """Decode a text file the way Flash's ByteArray.toString() does: BOM first, then UTF-8."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
