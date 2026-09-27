"""QSP 5.7.0 (qsp-legacy) save format.

Field order follows qspSaveGameStatusToString() and qspOpenGameStatusFromString()
in qsp-legacy src/game.c. Signature and version are written as-is, every other
record is encoded. A 5.7.0 save is always UCS-2: the engine writes its
16-bit QSP_CHAR buffer straight to the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .codec import Encoding, Issue, RecordReader, RecordWriter, SaveFormatError, detect_encoding, split_records, utf16_key
from .tables import VARS_COUNT_570, VARS_SEEK_570, upper_570, var_window_570

ENGINE = "5.7.0"
SAVED_GAME_ID = "QSPSAVEDGAME"
VERSION = "5.7.0"
GAME_MIN_VERSION = "5.7.0"

MAX_PL_FILES = 500
MAX_INC_FILES = 100
MAX_ACTIONS = 50
MAX_OBJECTS = 1000


@dataclass
class CodeLine:
    code: str
    line_num: int


@dataclass
class Action:
    image: str  # empty when the action has no image (NULL in the engine)
    desc: str
    lines: list[CodeLine]
    location: int  # index of the location the action comes from
    act_index: int  # index of the action in that location, -1 for ACT
    start_line: int
    is_manage_lines: bool


@dataclass
class Obj:
    image: str
    desc: str  # the object name as shown to the player


@dataclass
class Value:
    num: int
    str: str  # empty string and "no string" (NULL) are not distinguishable on disk


@dataclass
class Index:
    index: int
    key: str  # upper-cased text index, without type prefix


@dataclass
class Variable:
    slot: int  # position in qspVars[], see tables.var_window_570()
    name: str  # upper-cased, no '$' prefix
    values: list[Value]
    indices: list[Index]


@dataclass
class Save:
    signature: str = SAVED_GAME_ID
    version: str = VERSION
    game_crc: int = 0
    time_ms: int = 0
    sel_action: int = -1
    sel_object: int = -1
    view_path: str = ""
    input_text: str = ""
    main_desc: str = ""
    vars_desc: str = ""
    cur_loc: int = 0
    show_actions: bool = True
    show_objects: bool = True
    show_vars: bool = True
    show_input: bool = True
    timer_ms: int = 500
    playlist: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    objects: list[Obj] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    encoding: Encoding = Encoding.UCS2


def _bool(reader: RecordReader, what: str) -> bool:
    value = reader.int(what)
    if value not in (0, 1):
        reader.issues.append(Issue("warning", what, f"flag value {value} will be written back as 1"))
    return value != 0


def read(data: bytes) -> tuple[Save, list[Issue]]:
    encoding = detect_encoding(data)
    if encoding is not Encoding.UCS2:
        raise SaveFormatError("5.7.0 saves are UCS-2; this data is single-byte")
    r = RecordReader(split_records(data, encoding), encoding)
    s = Save(encoding=encoding)
    s.signature = r.raw("signature")
    if s.signature != SAVED_GAME_ID:
        raise SaveFormatError(f"not a QSP save: signature {s.signature!r}", 0)
    s.version = r.raw("version")
    s.game_crc = r.int("game CRC")
    s.time_ms = r.int("time")
    s.sel_action = r.int("selected action")
    s.sel_object = r.int("selected object")
    s.view_path = r.text("view path")
    s.input_text = r.text("input text")
    s.main_desc = r.text("main description")
    s.vars_desc = r.text("additional description")
    s.cur_loc = r.int("current location")
    s.show_actions = _bool(r, "show actions")
    s.show_objects = _bool(r, "show objects")
    s.show_vars = _bool(r, "show additional description")
    s.show_input = _bool(r, "show input")
    s.timer_ms = r.int("timer interval")
    s.playlist = [r.text("playlist file") for _ in range(r.count("playlist size"))]
    s.includes = [r.text("included file") for _ in range(r.count("included files count"))]
    for i in range(r.count("actions count")):
        where = f"action {i}"
        image = r.text(f"{where} image")
        desc = r.text(f"{where} description")
        lines = [CodeLine(r.text(f"{where} code"), r.int(f"{where} line number"))
                 for _ in range(r.count(f"{where} lines count"))]
        s.actions.append(Action(image, desc, lines,
                                location=r.int(f"{where} location"),
                                act_index=r.int(f"{where} index"),
                                start_line=r.int(f"{where} start line"),
                                is_manage_lines=_bool(r, f"{where} manage lines")))
    for i in range(r.count("objects count")):
        s.objects.append(Obj(image=r.text(f"object {i} image"), desc=r.text(f"object {i} name")))
    for i in range(r.count("variables count")):
        slot = r.int(f"variable {i} slot")
        name = r.text(f"variable {i} name")
        where = f"variable {name!r}"
        values = [Value(num=r.int(f"{where} number"), str=r.text(f"{where} string"))
                  for _ in range(r.count(f"{where} values count"))]
        indices = [Index(index=r.int(f"{where} index position"), key=r.text(f"{where} index text"))
                   for _ in range(r.count(f"{where} indices count"))]
        s.variables.append(Variable(slot, name, values, indices))
    r.finish()
    return s, r.issues


def write(s: Save) -> bytes:
    w = RecordWriter(Encoding.UCS2)
    w.raw(s.signature)
    w.raw(s.version)
    for value in (s.game_crc, s.time_ms, s.sel_action, s.sel_object):
        w.int(value)
    for text in (s.view_path, s.input_text, s.main_desc, s.vars_desc):
        w.text(text)
    w.int(s.cur_loc)
    for flag in (s.show_actions, s.show_objects, s.show_vars, s.show_input):
        w.bool(flag)
    w.int(s.timer_ms)
    for items in (s.playlist, s.includes):
        w.int(len(items))
        for item in items:
            w.text(item)
    w.int(len(s.actions))
    for a in s.actions:
        w.text(a.image)
        w.text(a.desc)
        w.int(len(a.lines))
        for line in a.lines:
            w.text(line.code)
            w.int(line.line_num)
        w.int(a.location)
        w.int(a.act_index)
        w.int(a.start_line)
        w.bool(a.is_manage_lines)
    w.int(len(s.objects))
    for o in s.objects:
        w.text(o.image)
        w.text(o.desc)
    w.int(len(s.variables))
    for v in s.variables:
        w.int(v.slot)
        w.text(v.name)
        w.int(len(v.values))
        for value in v.values:
            w.int(value.num)
            w.text(value.str)
        w.int(len(v.indices))
        for index in v.indices:
            w.int(index.index)
            w.text(index.key)
    return w.to_bytes()


def validate(s: Save, game_crc: int | None = None) -> list[Issue]:
    """Checks of qspCheckGameStatus() plus the invariants the engine relies on
    after loading (variable lookup, sorted text indices)."""
    issues: list[Issue] = []

    def error(where: str, message: str) -> None:
        issues.append(Issue("error", where, message))

    def warning(where: str, message: str) -> None:
        issues.append(Issue("warning", where, message))

    if not (GAME_MIN_VERSION <= s.version <= VERSION):
        error("version", f"{s.version!r} is outside {GAME_MIN_VERSION}..{VERSION}, qsp-legacy rejects it")
    if game_crc is not None and s.game_crc != game_crc:
        error("game CRC", f"{s.game_crc} does not match the game file ({game_crc}); "
                          "the engine rejects the save unless DEBUG is set")
    if s.cur_loc < 0:
        error("current location", f"{s.cur_loc} is negative")
    if s.timer_ms < 0:
        error("timer interval", f"{s.timer_ms} is negative")
    if len(s.playlist) > MAX_PL_FILES:
        error("playlist", f"{len(s.playlist)} files, the limit is {MAX_PL_FILES}")
    if len(s.includes) > MAX_INC_FILES:
        error("included files", f"{len(s.includes)} files, the limit is {MAX_INC_FILES}")
    if len(s.actions) > MAX_ACTIONS:
        error("actions", f"{len(s.actions)} actions, the limit is {MAX_ACTIONS}")
    if s.sel_action >= len(s.actions):
        error("selected action", f"{s.sel_action} is out of range for {len(s.actions)} actions")
    for i, a in enumerate(s.actions):
        for j, line in enumerate(a.lines):
            if line.line_num < 0:
                error(f"action {i} line {j}", f"negative line number {line.line_num}")
        if a.location < 0:
            error(f"action {i}", f"negative location index {a.location}")
        if a.start_line < 0:
            error(f"action {i}", f"negative start line {a.start_line}")
    if len(s.objects) > MAX_OBJECTS:
        error("objects", f"{len(s.objects)} objects, the limit is {MAX_OBJECTS}")
    if s.sel_object >= len(s.objects):
        error("selected object", f"{s.sel_object} is out of range for {len(s.objects)} objects")

    last_slot = -1
    occupied = set()
    for v in s.variables:
        where = f"variable {v.name!r}"
        if v.slot <= last_slot or v.slot >= VARS_COUNT_570:
            error(where, f"slot {v.slot} must be increasing and below {VARS_COUNT_570}")
        last_slot = max(last_slot, v.slot)
        occupied.add(v.slot)
        if not v.name:
            error(where, "empty name")
        elif v.name != upper_570(v.name) or v.name.startswith("$"):
            warning(where, "name is not in the stored form (upper case, no '$'); the game cannot reach it")
        keys = [ix.key for ix in v.indices]
        if keys != sorted(keys, key=utf16_key):
            error(where, "text indices are not sorted; the engine looks them up with bsearch")
        if len(set(keys)) != len(keys):
            error(where, "duplicate text indices")
        for ix in v.indices:
            if ix.key != upper_570(ix.key):
                warning(where, f"text index {ix.key!r} is not upper-cased; the game cannot reach it")
    # The engine finds a variable by scanning its window from the start up to
    # the first free slot, so a variable is reachable only when every slot
    # between the window start and its own slot is taken.
    for v in s.variables:
        start = var_window_570(v.name) if v.name else 0
        if not (start <= v.slot < start + VARS_SEEK_570) or any(
                slot not in occupied for slot in range(start, v.slot)):
            error(f"variable {v.name!r}", f"slot {v.slot} is not reachable: the name hashes to "
                                          f"window {start}..{start + VARS_SEEK_570 - 1}")
    return issues
