"""Game model and readers/writers for binary .qsp and TXT2GAM .qsps files."""

import dataclasses
import re

from . import __version__


@dataclasses.dataclass
class Action:
    name: str
    image: str
    code: list[str]


@dataclasses.dataclass
class Location:
    name: str
    description: list[str]
    code: list[str]
    actions: list[Action]


QSP_CODREMOV = 5


def _qsp_decode(text: str) -> str:
    out = []
    for ch in text:
        c = ord(ch)
        out.append(chr(QSP_CODREMOV if c == 0x10000 - QSP_CODREMOV else (c + QSP_CODREMOV) & 0xFFFF))
    return "".join(out)


def _qsp_encode(text: str) -> str:
    out = []
    for ch in text:
        c = ord(ch)
        out.append(chr(0x10000 - QSP_CODREMOV if c == QSP_CODREMOV else (c - QSP_CODREMOV) & 0xFFFF))
    return "".join(out)


def _split_field(value: str) -> list[str]:
    return value.split("\r\n") if value else []


def read_qsp(data: bytes) -> list[Location]:
    """Read a binary .qsp/.gam game (new "QSPGAME" format or the old ANSI one)."""
    is_unicode = len(data) > 1 and data[1] == 0
    if is_unicode:
        lines = data.decode("utf-16-le", errors="surrogatepass").split("\r\n")
        dec = _qsp_decode
    else:
        raw_lines = data.split(b"\r\n")
        lines = [line.decode("cp1251", errors="replace") for line in raw_lines]

        def dec(value: str) -> str:
            raw = value.encode("cp1251", errors="replace")
            return bytes((b + QSP_CODREMOV) & 0xFF for b in raw).decode("cp1251", errors="replace")

    pos = 0

    def take(decode: bool = True) -> str:
        nonlocal pos
        value = lines[pos] if pos < len(lines) else ""
        pos += 1
        return dec(value) if decode else value

    locations: list[Location] = []
    if lines and lines[0] == "QSPGAME":
        take(False)  # signature
        take(False)  # editor version
        take()  # password
        count = int(take() or 0)
        for _ in range(count):
            name = take()
            description = _split_field(take())
            code = _split_field(take())
            actions = []
            for _ in range(int(take() or 0)):
                image = take()
                act_name = take()
                actions.append(Action(act_name, image, _split_field(take())))
            locations.append(Location(name, description, code, actions))
    else:
        count = int(take(False) or 0)
        take()  # password
        take(False)  # version
        for _ in range(27):
            take()
        for _ in range(count):
            name = take()
            description = _split_field(take())
            code = _split_field(take())
            actions = []
            for _ in range(20):
                act_name = take()
                act_code = _split_field(take())
                if act_name:
                    actions.append(Action(act_name, "", act_code))
            locations.append(Location(name, description, code, actions))
    return locations


def write_qsp(locations: list[Location]) -> bytes:
    """Write a binary .qsp game in the "QSPGAME" (UTF-16) format understood by QSP 5.7+."""
    parts: list[str] = []

    def line(value: str, encode: bool = True) -> None:
        parts.append((_qsp_encode(value) if encode else value) + "\r\n")

    line("QSPGAME", False)
    line(f"aero2qspider {__version__}", False)
    line("No")
    line(str(len(locations)))
    for loc in locations:
        line(loc.name)
        line("\r\n".join(loc.description))
        line("\r\n".join(loc.code))
        line(str(len(loc.actions)))
        for act in loc.actions:
            line(act.image)
            line(act.name)
            line("\r\n".join(act.code))
    return "".join(parts).encode("utf-16-le", errors="surrogatepass")


def read_qsps(text: str) -> list[Location]:
    """Read a TXT2GAM (.qsps) game, following the logic of @qsp/converters."""
    text = text.lstrip("﻿")
    locations: list[Location] = []
    current: Location | None = None
    quote = ""
    braces = 0
    for line in re.split(r"\r?\n", text):
        if current is None:
            if line.startswith("#"):
                current = Location(line[1:].strip(), [], [], [])
                quote, braces = "", 0
            continue
        if not quote and not braces and line.startswith("-"):
            locations.append(current)
            current = None
            continue
        current.code.append(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == quote:
                    if i + 1 < len(line) and line[i + 1] == quote:
                        i += 1
                    else:
                        quote = ""
            elif ch == "{":
                braces += 1
            elif ch == "}":
                braces = max(0, braces - 1)
            elif ch in "'\"":
                quote = ch
            i += 1
    if current is not None:
        locations.append(current)
    return locations


def _qsps_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_qsps(locations: list[Location], newline: str = "\n") -> tuple[str, dict[tuple[str, str], int]]:
    """Write a .qsps text. Returns the text and the first line number of every location part."""
    out: list[str] = []
    starts: dict[tuple[str, str], int] = {}
    line_no = 1

    def emit(value: str) -> None:
        nonlocal line_no
        out.append(value)
        line_no += 1

    for index, loc in enumerate(locations):
        if index:
            emit("")
        emit(f"# {loc.name}")
        starts[(loc.name, "desc")] = line_no
        for i, desc in enumerate(loc.description):
            cmd = "*p" if i == len(loc.description) - 1 else "*pl"
            emit(f"{cmd} {_qsps_str(desc)}")
        for a_index, act in enumerate(loc.actions):
            image = f", {_qsps_str(act.image)}" if act.image else ""
            starts[(loc.name, f"act-name:{a_index}")] = line_no
            emit(f"act {_qsps_str(act.name)}{image}:")
            starts[(loc.name, f"act:{a_index}")] = line_no
            for code_line in act.code:
                emit(f"  {code_line}")
            emit("end")
        starts[(loc.name, "code")] = line_no
        for code_line in loc.code:
            emit(code_line)
        emit(f"--- {loc.name} ---------------------------------")
    return newline.join(out) + newline, starts
