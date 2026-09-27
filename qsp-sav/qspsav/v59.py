"""QSP 5.9 save format (5.9.4 and later).

Field order follows qspSaveGameStatus() and qspOpenGameStatus() in qsp
qsp/game.c, variants follow qspAppendEncodedVariant()/qspReadEncodedVariant()
in qsp/coding.c. The engine writes UCS-2 saves but also reads single-byte
(CP1251) ones, so both are supported here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import IntEnum

from .codec import (Encoding, Issue, RecordReader, RecordWriter, SaveFormatError, detect_encoding,
                    split_records, utf16_key)
from .tables import MAX_VARS_BUCKET_SIZE_59, VARS_GLOBAL_BUCKETS_59, upper_59, var_bucket_59

ENGINE = "5.9"
SAVED_GAME_ID = "QSPSAVEDGAME"
VERSION = "5.9.5"  # the newest engine this module was checked against
GAME_MIN_VERSION = "5.9.4"  # QSP_GAMEMIN_VER of qsp 5.9.5; older 5.9.x saves differ

MAX_PL_FILES = 500
MAX_INC_FILES = 100
MAX_ACTIONS = 50
MAX_OBJECTS = 1000

OBJ_UPDATED_DESC = 1 << 0
OBJ_UPDATED_IMAGE = 1 << 1

WIN_MAIN = 1 << 0
WIN_VARS = 1 << 1
WIN_ACTS = 1 << 2
WIN_OBJS = 1 << 3
WIN_INPUT = 1 << 4
WIN_VIEW = 1 << 5
WIN_ALL = WIN_MAIN | WIN_VARS | WIN_ACTS | WIN_OBJS | WIN_INPUT | WIN_VIEW


class VarType(IntEnum):
    TUPLE = 0
    NUM = 1
    BOOL = 2
    STR = 3
    CODE = 4
    VARREF = 5
    UNDEF = 6  # string-based, "no value"


BASE_TYPE = {
    VarType.TUPLE: VarType.TUPLE,
    VarType.NUM: VarType.NUM,
    VarType.BOOL: VarType.NUM,
    VarType.STR: VarType.STR,
    VarType.CODE: VarType.STR,
    VarType.VARREF: VarType.STR,
    VarType.UNDEF: VarType.STR,
}


@dataclass
class Variant:
    type: VarType
    value: int | str | list[Variant]


@dataclass
class CodeLine:
    code: str  # already prepared by the engine: upper case outside strings and {}
    line_num: int


@dataclass
class Action:
    desc: str
    image: str
    lines: list[CodeLine]
    location: int
    act_index: int


@dataclass
class Obj:
    name: str
    image: str


@dataclass
class ObjGroup:
    name: str  # upper-cased object name; groups are sorted by name
    desc: str
    image: str
    updated_fields: int  # OBJ_UPDATED_* flags
    objs_count: int


@dataclass
class Index:
    index: int
    key: str  # upper-cased, with a type prefix: "$TEXT", "#5", tuples as "count|..."


@dataclass
class Variable:
    bucket: int  # global bucket, see tables.var_bucket_59()
    name: str  # upper-cased, no type prefix
    values: list[Variant]
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
    cur_loc: str = ""
    windows: int = WIN_MAIN | WIN_VARS | WIN_ACTS | WIN_OBJS | WIN_INPUT
    timer_ms: int = 500
    playlist: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    objects: list[Obj] = field(default_factory=list)
    groups: list[ObjGroup] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    encoding: Encoding = Encoding.UCS2


def _read_variant(r: RecordReader, where: str) -> Variant:
    index = r.pos
    raw_type = r.int(f"{where} type")
    try:
        var_type = VarType(raw_type)
    except ValueError:
        raise SaveFormatError(f"{where}: unknown value type {raw_type}", index) from None
    match BASE_TYPE[var_type]:
        case VarType.TUPLE:
            count = r.int(f"{where} tuple size")  # the engine treats count <= 0 as empty
            items = [_read_variant(r, f"{where}[{i}]") for i in range(max(count, 0))]
            if count < 0:
                r.issues.append(Issue("warning", where, f"tuple size {count} will be written back as 0"))
            return Variant(var_type, items)
        case VarType.NUM:
            return Variant(var_type, r.int(f"{where} number"))
        case _:
            return Variant(var_type, r.text(f"{where} string"))


def _write_variant(w: RecordWriter, v: Variant) -> None:
    w.int(v.type)
    match BASE_TYPE[v.type]:
        case VarType.TUPLE:
            assert isinstance(v.value, list)
            w.int(len(v.value))
            for item in v.value:
                _write_variant(w, item)
        case VarType.NUM:
            w.int(v.value)  # type: ignore[arg-type]
        case _:
            w.text(v.value)  # type: ignore[arg-type]


def read(data: bytes) -> tuple[Save, list[Issue]]:
    encoding = detect_encoding(data)
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
    s.cur_loc = r.text("current location")
    s.windows = r.int("windows state")
    s.timer_ms = r.int("timer interval")
    s.playlist = [r.text("playlist file") for _ in range(r.count("playlist size"))]
    s.includes = [r.text("included file") for _ in range(r.count("included files count"))]
    for i in range(r.count("actions count")):
        where = f"action {i}"
        desc = r.text(f"{where} description")
        image = r.text(f"{where} image")
        lines = [CodeLine(r.text(f"{where} code"), r.int(f"{where} line number"))
                 for _ in range(r.count(f"{where} lines count"))]
        s.actions.append(Action(desc, image, lines,
                                location=r.int(f"{where} location"),
                                act_index=r.int(f"{where} index")))
    for i in range(r.count("objects count")):
        s.objects.append(Obj(name=r.text(f"object {i} name"), image=r.text(f"object {i} image")))
    for i in range(r.count("object groups count")):
        where = f"object group {i}"
        s.groups.append(ObjGroup(name=r.text(f"{where} name"), desc=r.text(f"{where} description"),
                                 image=r.text(f"{where} image"),
                                 updated_fields=r.int(f"{where} updated fields"),
                                 objs_count=r.int(f"{where} objects count")))
    for bucket in range(VARS_GLOBAL_BUCKETS_59):
        for _ in range(r.count(f"bucket {bucket} variables count")):
            name = r.text(f"bucket {bucket} variable name")
            where = f"variable {name!r}"
            values = [_read_variant(r, f"{where}[{k}]") for k in range(r.count(f"{where} values count"))]
            indices = [Index(index=r.int(f"{where} index position"), key=r.text(f"{where} index text"))
                       for _ in range(r.count(f"{where} indices count"))]
            s.variables.append(Variable(bucket, name, values, indices))
    r.finish()
    return s, r.issues


def write(s: Save, encoding: Encoding | None = None) -> bytes:
    w = RecordWriter(encoding or s.encoding)
    w.raw(s.signature)
    w.raw(s.version)
    for value in (s.game_crc, s.time_ms, s.sel_action, s.sel_object):
        w.int(value)
    for text in (s.view_path, s.input_text, s.main_desc, s.vars_desc, s.cur_loc):
        w.text(text)
    w.int(s.windows)
    w.int(s.timer_ms)
    for items in (s.playlist, s.includes):
        w.int(len(items))
        for item in items:
            w.text(item)
    w.int(len(s.actions))
    for a in s.actions:
        w.text(a.desc)
        w.text(a.image)
        w.int(len(a.lines))
        for line in a.lines:
            w.text(line.code)
            w.int(line.line_num)
        w.int(a.location)
        w.int(a.act_index)
    w.int(len(s.objects))
    for o in s.objects:
        w.text(o.name)
        w.text(o.image)
    w.int(len(s.groups))
    for g in s.groups:
        w.text(g.name)
        w.text(g.desc)
        w.text(g.image)
        w.int(g.updated_fields)
        w.int(g.objs_count)
    buckets: list[list[Variable]] = [[] for _ in range(VARS_GLOBAL_BUCKETS_59)]
    for v in s.variables:
        if not 0 <= v.bucket < VARS_GLOBAL_BUCKETS_59:
            raise SaveFormatError(f"variable {v.name!r}: bucket {v.bucket} is out of range")
        buckets[v.bucket].append(v)
    for bucket in buckets:
        w.int(len(bucket))
        for v in bucket:
            w.text(v.name)
            w.int(len(v.values))
            for value in v.values:
                _write_variant(w, value)
            w.int(len(v.indices))
            for index in v.indices:
                w.int(index.index)
                w.text(index.key)
    return w.to_bytes()


def validate(s: Save, game_crc: int | None = None) -> list[Issue]:
    """Checks of qspCheckGameStatus() plus the invariants the engine relies on
    after loading (variable buckets, sorted indices, object groups)."""
    issues: list[Issue] = []

    def error(where: str, message: str) -> None:
        issues.append(Issue("error", where, message))

    def warning(where: str, message: str) -> None:
        issues.append(Issue("warning", where, message))

    if utf16_key(s.version) < utf16_key(GAME_MIN_VERSION):
        error("version", f"{s.version!r} is older than {GAME_MIN_VERSION}, qsp 5.9 rejects it")
    elif utf16_key(s.version) > utf16_key(VERSION):
        warning("version", f"{s.version!r} is newer than {VERSION}; players older than that reject it")
    if game_crc is not None and s.game_crc != game_crc:
        error("game CRC", f"{s.game_crc} does not match the game file ({game_crc}); "
                          "the engine rejects the save unless DEBUG is set")
    if s.windows & ~WIN_ALL or s.windows < 0:
        warning("windows state", f"unknown bits in {s.windows:#x}")
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

    if len(s.objects) > MAX_OBJECTS:
        error("objects", f"{len(s.objects)} objects, the limit is {MAX_OBJECTS}")
    if s.sel_object >= len(s.objects):
        error("selected object", f"{s.sel_object} is out of range for {len(s.objects)} objects")
    if len(s.groups) > MAX_OBJECTS:
        error("object groups", f"{len(s.groups)} groups, the limit is {MAX_OBJECTS}")
    for i, g in enumerate(s.groups):
        if g.updated_fields < 0:
            error(f"object group {g.name!r}", f"negative updated fields {g.updated_fields}")
        elif g.updated_fields & ~(OBJ_UPDATED_DESC | OBJ_UPDATED_IMAGE):
            warning(f"object group {g.name!r}", f"unknown updated fields bits {g.updated_fields:#x}")
        if not 0 <= g.objs_count <= MAX_OBJECTS:
            error(f"object group {g.name!r}", f"objects count {g.objs_count} is out of range")
    # Groups are looked up with bsearch by upper-cased object name and carry
    # the per-name object counter used by OBJ and COUNTOBJ.
    group_names = [g.name for g in s.groups]
    if group_names != sorted(group_names, key=utf16_key) or len(set(group_names)) != len(group_names):
        error("object groups", "names must be unique and sorted; the engine looks them up with bsearch")
    counts = Counter(upper_59(o.name) for o in s.objects)
    by_name = {g.name: g for g in s.groups}
    for name, count in counts.items():
        group = by_name.get(name)
        if group is None:
            error("object groups", f"no group for object {name!r}; OBJ and COUNTOBJ will not see it")
        elif group.objs_count != count:
            error(f"object group {name!r}", f"objects count {group.objs_count}, but there are {count} such objects")
    for name in by_name.keys() - counts.keys():
        warning(f"object group {name!r}", "group without objects")

    per_bucket = Counter(v.bucket for v in s.variables)
    for bucket, count in per_bucket.items():
        if count > MAX_VARS_BUCKET_SIZE_59:
            error(f"bucket {bucket}", f"{count} variables, the limit is {MAX_VARS_BUCKET_SIZE_59}")
    names = Counter(v.name for v in s.variables)
    for name, count in names.items():
        if count > 1:
            error(f"variable {name!r}", f"stored {count} times")
    for v in s.variables:
        where = f"variable {v.name!r}"
        if not v.name:
            error(where, "empty name")
            continue
        if v.name != upper_59(v.name) or v.name[0] in "#$%":
            warning(where, "name is not in the stored form (upper case, no type prefix); the game cannot reach it")
        expected = var_bucket_59(v.name)
        if v.bucket != expected:
            error(where, f"stored in bucket {v.bucket}, but the name hashes to {expected}; the game cannot reach it")
        keys = [ix.key for ix in v.indices]
        if keys != sorted(keys, key=utf16_key) or len(set(keys)) != len(keys):
            error(where, "text indices must be unique and sorted; the engine looks them up with bsearch")
        for ix in v.indices:
            if not 0 <= ix.index < len(v.values):
                error(where, f"index {ix.key!r} points to value {ix.index}, but there are {len(v.values)} values")
            if ix.key != upper_59(ix.key):
                warning(where, f"text index {ix.key!r} is not upper-cased; the game cannot reach it")
            if not ix.key or not (ix.key[0] in "#$" or ix.key[0].isdigit()):
                warning(where, f"text index {ix.key!r} has no type prefix ('$' for strings, '#' for numbers)")
    return issues
