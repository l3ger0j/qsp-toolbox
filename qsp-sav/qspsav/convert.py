"""Conversion of saves between QSP 5.7.0 and 5.9.

Both directions need the game: the CRC of the game file the target engine
will load, and the location array (main game + included files) to translate
between 5.7.0 location indices and 5.9 location names.

Every change the conversion cannot make exactly is reported as an Issue:
"error" -- the target engine will reject the save or lose data the game needs,
"warning" -- data is changed or dropped, "note" -- worth knowing, nothing lost.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import v59, v570
from ._tables import UPPER_570, UPPER_59
from .code import VarUsage, find_59_only_syntax, prepare_line_59
from .codec import Issue, utf16_key
from .game import LocationTable
from .tables import VARS_SEEK_570, upper_570, upper_59, var_bucket_59, var_window_570

# qsp-legacy's upper-case table maps these characters differently from 5.9
# (e.g. U+00F7 '÷' becomes U+00D7 '×'), so a stored 5.7.0 name containing the
# result may come from either character.
AMBIGUOUS_570_UPPER = {v for k, v in UPPER_570.items() if UPPER_59.get(k, k) != v}
# Characters 5.9 upper-cases but 5.7.0 does not: a 5.9 name containing their
# upper-case form is written in lower case by 5.7.0 code.
ONLY_59_UPPER = {v for k, v in UPPER_59.items() if UPPER_570.get(k, k) != v}

LEGACY_TRUE = -1  # qsp-legacy: comparisons give -1, NO/AND/OR are bitwise
INT32 = (-2 ** 31, 2 ** 31 - 1)


@dataclass
class Context:
    """What a conversion needs to know about the game."""

    source_locations: LocationTable  # as the source engine built it
    target_locations: LocationTable  # as the target engine will build it
    target_crc: int  # CRC of the game file the target engine loads
    usage: VarUsage | None = None  # how the game code refers to variables


class _Report:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, level: str, where: str, message: str) -> None:
        self.issues.append(Issue(level, where, message))


def _map_location(index: int, ctx: Context, report: _Report, where: str) -> tuple[int, str]:
    """Source location index -> (target index, name). (-1, "") when unknown."""
    name = ctx.source_locations.name_of(index)
    if name is None:
        if index >= ctx.source_locations.main_count and not ctx.source_locations.complete:
            report.add("error", where, f"location #{index} comes from an included file that was not found")
        elif index >= 0:
            report.add("error", where, f"location #{index} does not exist in the game")
        return -1, ""
    target = ctx.target_locations.index_of(name)
    if target is None:
        report.add("error", where, f"location {name!r} does not exist in the target game")
        return -1, name
    return target, ctx.target_locations.names[target]


# --------------------------------------------------------------------------- 5.7.0 -> 5.9


def _value_to_59(value: v570.Value, kind: str | None, name: str, conflicts: list[int], position: int) -> v59.Variant:
    has_str, has_num = value.str != "", value.num != 0
    if has_str and has_num:
        conflicts.append(position)
        if kind == "num":
            return v59.Variant(v59.VarType.NUM, value.num)
        return v59.Variant(v59.VarType.STR, value.str)
    if has_str:
        return v59.Variant(v59.VarType.STR, value.str)
    if has_num:
        return v59.Variant(v59.VarType.NUM, value.num)
    # Neither part set: UNDEF reads as 0 and as "" -- exactly what 5.7.0 gives
    # for both forms. 5.9 itself stores unset array items this way.
    return v59.Variant(v59.VarType.UNDEF, "")


def to_59(src: v570.Save, ctx: Context, version: str = v59.GAME_MIN_VERSION) -> tuple[v59.Save, list[Issue]]:
    report = _Report()
    dst = v59.Save(version=version, game_crc=ctx.target_crc, encoding=src.encoding)
    dst.time_ms = src.time_ms
    dst.sel_action = src.sel_action
    dst.sel_object = src.sel_object
    dst.view_path = src.view_path
    dst.input_text = src.input_text
    dst.main_desc = src.main_desc
    dst.vars_desc = src.vars_desc
    _, dst.cur_loc = _map_location(src.cur_loc, ctx, report, "current location")
    # 5.7.0 has no flags for the main window (always shown) and the picture
    # window; 5.9 sets VIEW exactly when a picture path is set (VIEW statement).
    dst.windows = v59.WIN_MAIN
    for flag, bit in ((src.show_vars, v59.WIN_VARS), (src.show_actions, v59.WIN_ACTS),
                      (src.show_objects, v59.WIN_OBJS), (src.show_input, v59.WIN_INPUT)):
        if flag:
            dst.windows |= bit
    if src.view_path:
        dst.windows |= v59.WIN_VIEW
    dst.timer_ms = src.timer_ms
    dst.playlist = list(src.playlist)
    dst.includes = list(src.includes)

    for i, act in enumerate(src.actions):
        where = f"action {i} {act.desc!r}"
        location, _ = _map_location(act.location, ctx, report, where)
        lines = [v59.CodeLine(prepare_line_59(line.code), line.line_num) for line in act.lines]
        dst.actions.append(v59.Action(act.desc, act.image, lines, location, act.act_index))

    counts: dict[str, int] = {}
    for obj in src.objects:
        dst.objects.append(v59.Obj(obj.desc, obj.image))
        key = upper_59(obj.desc)
        counts[key] = counts.get(key, 0) + 1
    # Groups carry the per-name counter used by OBJ and COUNTOBJ; the engine
    # keeps them sorted by upper-cased name (qspAddObjsGroup).
    dst.groups = [v59.ObjGroup(name, "", "", 0, count) for name, count in sorted(counts.items(),
                                                                               key=lambda item: utf16_key(item[0]))]

    both_forms = []
    for var in src.variables:
        name = upper_59(var.name)
        where = f"variable {var.name!r}"
        if AMBIGUOUS_570_UPPER & set(var.name):
            report.add("warning", where, "the name contains a character qsp-legacy upper-cases differently from "
                                         "5.9; check that the game still finds the variable")
        kind = ctx.usage.kind(var.name) if ctx.usage else None
        if kind == "both":
            both_forms.append(var.name)
        conflicts: list[int] = []
        values = [_value_to_59(value, kind, var.name, conflicts, k) for k, value in enumerate(var.values)]
        if conflicts:
            kept = "number" if kind == "num" else "string"
            lost = [f"[{k}] {var.values[k].num if kept == 'string' else repr(var.values[k].str)}" for k in conflicts]
            report.add("warning", where,
                       f"{len(conflicts)} element(s) had both a number and a string; 5.9 keeps one value per "
                       f"element, the {kept} was kept, lost: {', '.join(lost[:5])}{' ...' if len(lost) > 5 else ''}")
        keys = {}
        for index in var.indices:
            key = "$" + upper_59(index.key)
            if key in keys and keys[key] != index.index:
                report.add("warning", where, f"text indices collide after conversion: {key!r}")
                continue
            keys[key] = index.index
        indices = [v59.Index(position, key) for key, position in sorted(keys.items(), key=lambda kv: utf16_key(kv[0]))]
        dst.variables.append(v59.Variable(var_bucket_59(name), name, values, indices))
    if both_forms:
        report.add("note", "game code", "these variables are used both with and without '$'; in 5.9 each element "
                                        f"holds one value, so the game itself may behave differently: "
                                        f"{', '.join(sorted(both_forms))}")

    report.issues += v59.validate(dst, ctx.target_crc)
    return dst, report.issues


# --------------------------------------------------------------------------- 5.9 -> 5.7.0


def _value_to_570(value: v59.Variant, where: str, report: _Report) -> v570.Value:
    base = v59.BASE_TYPE[value.type]
    if value.type is v59.VarType.BOOL:
        return v570.Value(LEGACY_TRUE if value.value else 0, "")
    if base is v59.VarType.NUM:
        num = int(value.value)  # type: ignore[arg-type]
        if not INT32[0] <= num <= INT32[1]:
            report.add("warning", where, f"number {num} does not fit a 32-bit int; it is cut to 32 bits")
            num = ((num + 2 ** 31) % 2 ** 32) - 2 ** 31
        return v570.Value(num, "")
    if base is v59.VarType.TUPLE:
        report.add("warning", where, "a tuple has no 5.7.0 equivalent and is dropped")
        return v570.Value(0, "")
    if value.type is v59.VarType.VARREF:
        report.add("note", where, "a variable reference becomes a plain string")
    return v570.Value(0, value.value)  # STR, CODE, VARREF, UNDEF  # type: ignore[arg-type]


def _allocate_slots(variables: list[v570.Variable], report: _Report) -> list[v570.Variable]:
    """Place variables the way qspVarReference() finds them: first free slot
    of the name's 50-slot window, windows filled from the start."""
    taken: set[int] = set()
    placed = []
    for var in variables:
        start = var_window_570(var.name)
        slot = next((s for s in range(start, start + VARS_SEEK_570) if s not in taken), None)
        if slot is None:
            report.add("error", f"variable {var.name!r}", f"all 50 slots of its window ({start}..) are taken; "
                                                          "qsp-legacy cannot hold it (too many variables)")
            continue
        taken.add(slot)
        var.slot = slot
        placed.append(var)
    return sorted(placed, key=lambda v: v.slot)


def to_570(src: v59.Save, ctx: Context) -> tuple[v570.Save, list[Issue]]:
    report = _Report()
    dst = v570.Save(game_crc=ctx.target_crc)
    dst.time_ms = src.time_ms
    dst.sel_action = src.sel_action
    dst.sel_object = src.sel_object
    dst.view_path = src.view_path
    dst.input_text = src.input_text
    dst.main_desc = src.main_desc
    dst.vars_desc = src.vars_desc
    index = ctx.source_locations.index_of(src.cur_loc) if src.cur_loc else None
    if index is None:
        report.add("error", "current location", f"{src.cur_loc!r} is not a location of the game; "
                                                "qsp-legacy needs one, #0 is used")
        dst.cur_loc = 0
    else:
        dst.cur_loc, _ = _map_location(index, ctx, report, "current location")
        dst.cur_loc = max(dst.cur_loc, 0)
    dst.show_vars = bool(src.windows & v59.WIN_VARS)
    dst.show_actions = bool(src.windows & v59.WIN_ACTS)
    dst.show_objects = bool(src.windows & v59.WIN_OBJS)
    dst.show_input = bool(src.windows & v59.WIN_INPUT)
    if not src.windows & v59.WIN_MAIN:
        report.add("note", "windows", "the main window was hidden; 5.7.0 always shows it")
    dst.timer_ms = src.timer_ms
    dst.playlist = list(src.playlist)
    dst.includes = list(src.includes)

    for i, act in enumerate(src.actions):
        where = f"action {i} {act.desc!r}"
        location, _ = _map_location(act.location, ctx, report, where)
        for line in act.lines:
            found = find_59_only_syntax(line.code)
            if found:
                report.add("warning", where, f"code uses 5.9-only {', '.join(sorted(set(found)))}: {line.code!r}")
        # StartLine / IsManageLines only affect line numbers in error messages.
        # Actions defined in a location use (0, True); ACT blocks use their
        # first line; a one-line ACT runs without line management.
        if act.act_index >= 0:
            start_line, manage = 0, True
        elif len(act.lines) > 1:
            start_line, manage = act.lines[0].line_num, True
        else:
            start_line, manage = 0, False
        dst.actions.append(v570.Action(act.image, act.desc, [v570.CodeLine(l.code, l.line_num) for l in act.lines],
                                       location, act.act_index, start_line, manage))

    groups = {g.name: g for g in src.groups}
    for obj in src.objects:
        group = groups.get(upper_59(obj.name))
        image = obj.image
        if group and group.updated_fields & v59.OBJ_UPDATED_DESC:
            report.add("warning", f"object {obj.name!r}", f"the title set by MODOBJ ({group.desc!r}) is dropped; "
                                                           "5.7.0 shows the object name")
        if group and group.updated_fields & v59.OBJ_UPDATED_IMAGE:
            image = group.image
        dst.objects.append(v570.Obj(image, obj.name))

    variables = []
    for var in src.variables:
        where = f"variable {var.name!r}"
        if ONLY_59_UPPER & set(var.name):
            report.add("warning", where, "the name contains a letter 5.9 upper-cases but qsp-legacy does not; "
                                         "the game may not find the variable")
        values = [_value_to_570(value, f"{where}[{k}]", report) for k, value in enumerate(var.values)]
        # qsp-legacy upper-cases names and text indices with its own table
        # (a few characters differ from 5.9), so store what its code will look up.
        name = upper_570(var.name)
        indices = []
        for index in var.indices:
            if index.key.startswith("$"):
                indices.append(v570.Index(index.index, upper_570(index.key[1:])))
            else:
                report.add("warning", where, f"index {index.key!r} (not a text index) has no 5.7.0 equivalent; "
                                             "the value stays, the index is dropped")
        indices.sort(key=lambda ix: utf16_key(ix.key))
        variables.append(v570.Variable(0, name, values, indices))
    dst.variables = _allocate_slots(variables, report)

    report.issues += v570.validate(dst, ctx.target_crc)
    return dst, report.issues
