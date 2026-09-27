"""Checking saves in the real engines.

verify_save() loads a save into the engine it belongs to and compares what the
engine sees with what the file says: the save loads at all, the current
location, descriptions, actions, objects (including OBJ, which depends on the
5.9 object groups), every variable value and every value read by text index.
This catches everything that parses fine but that the engine cannot reach.

compare_with_source() goes one step further for a conversion: it compares the
engine's view of the converted save with the source engine's view of the
original, skipping only what the conversion reported as lost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import v59, v570
from .codec import Issue
from .engine import run_in_subprocess, run_many
from .game import LocationTable, location_key

Save = v570.Save | v59.Save


@dataclass
class Libraries:
    """Paths of the engine libraries (libqsp-legacy / libqsp)."""

    lib570: Path | None = None
    lib59: Path | None = None

    @classmethod
    def from_env(cls, lib570: Path | None = None, lib59: Path | None = None) -> Libraries:
        env570, env59 = os.environ.get("QSPSAV_LIB570"), os.environ.get("QSPSAV_LIB59")
        return cls(lib570 or (Path(env570) if env570 else None), lib59 or (Path(env59) if env59 else None))

    def for_engine(self, engine: str) -> Path | None:
        return self.lib570 if engine == v570.ENGINE else self.lib59


def _engine(save: Save) -> str:
    return v570.ENGINE if isinstance(save, v570.Save) else v59.ENGINE


def _pair(value: v570.Value | v59.Variant) -> list:
    """What the engine API reports for a value: [number, string]."""
    if isinstance(value, v570.Value):
        return [value.num, value.str]
    base = v59.BASE_TYPE[value.type]
    if base is v59.VarType.NUM:
        return [value.value, ""]
    if base is v59.VarType.STR:
        return [0, value.value]
    return [0, ""]  # tuples read as neither


def _text_keys(save: Save, var) -> list[tuple[str, int]]:
    """(key as written in game code, value position) for text indices.

    5.9 stores text keys as "$TEXT"; a key without the prefix is probed as is,
    so a save with malformed keys shows up as values the game cannot read.
    Tuple keys ("2\x05#1\x05$A") and number keys cannot be written as a
    string literal and are skipped.
    """
    if isinstance(save, v570.Save):
        return [(ix.key, ix.index) for ix in var.indices]
    keys = []
    for ix in var.indices:
        if ix.key.startswith("$"):
            keys.append((ix.key[1:], ix.index))
        elif "\x05" not in ix.key and not ix.key.startswith("#"):
            keys.append((ix.key, ix.index))
    return keys


def _objects(save: Save) -> list[str]:
    return [o.desc if isinstance(o, v570.Obj) else o.name for o in save.objects]


def _expected_location(save: Save, table: LocationTable) -> str | None:
    if isinstance(save, v59.Save):
        return save.cur_loc
    return table.name_of(save.cur_loc)


def inspect_job(save: Save, save_path: Path, game_path: Path, includes: dict[str, Path], lib: Path) -> dict:
    """The engine job that loads the save and reports the engine's view of it."""
    return {
        "engine": _engine(save), "lib": str(lib), "game": str(game_path),
        "libraries": {name: str(path) for name, path in includes.items()},
        "open_save": str(save_path),
        "inspect": {
            "vars": [v.name for v in save.variables],
            "keys": {v.name: [key for key, _ in _text_keys(save, v)] for v in save.variables if v.indices},
            "objects": sorted(set(_objects(save))),
        },
    }


def inspect(save: Save, save_path: Path, game_path: Path, includes: dict[str, Path], lib: Path) -> dict:
    """Run the engine on the save and return its view of the state."""
    return run_in_subprocess(inspect_job(save, save_path, game_path, includes, lib))


async def inspect_many(jobs: list[dict], workers: int) -> list[dict]:
    """Run inspect_job()s in parallel engine processes (see engine.run_many)."""
    return await run_many(jobs, workers)


def verify_save(save: Save, save_path: Path, game_path: Path, includes: dict[str, Path], table: LocationTable,
                lib: Path) -> tuple[list[Issue], dict]:
    result = inspect(save, save_path, game_path, includes, lib)
    return check_state(save, result, table), result


def location_lost_by_engine(save: Save, result: dict, table: LocationTable) -> bool:
    """Whether qsp 5.9 lost the save's current location because it comes from
    an included file. qspOpenGameStatus() looks the location up before it
    re-includes the library files, so 5.9 loads such a save -- its own saves
    too -- with no current location. The result is marked for
    compare_with_source()."""
    if _engine(save) != v59.ENGINE or not result["ok"] or result["state"]["location"] != "":
        return False
    expected = _expected_location(save, table)
    index = table.index_of(expected) if expected else None
    lost = index is not None and index >= table.main_count
    if lost:
        result["state"]["location_lost_by_engine"] = True
    return lost


def check_state(save: Save, result: dict, table: LocationTable) -> list[Issue]:
    """Compare the engine's view of a save (a result of inspect_job) with the file."""
    issues: list[Issue] = []
    engine = _engine(save)

    def error(where: str, message: str) -> None:
        issues.append(Issue("error", f"{engine} engine: {where}", message))

    if not result["ok"]:
        err = result.get("error") or {}
        error("load", f"the engine refused the save at step {result.get('failed')!r}: "
                      f"{err.get('desc', 'no error text')} (code {err.get('num')})")
        return issues
    state = result["state"]

    expected_loc = _expected_location(save, table)
    if location_lost_by_engine(save, result, table):
        issues.append(Issue("note", f"{engine} engine: location",
                            f"{expected_loc!r} comes from an included file; qsp 5.9 restores included files after "
                            "looking up the location, so it loads this save with no current location "
                            "(engine behaviour, the same happens with saves 5.9 writes itself)"))
    elif expected_loc is not None and (state["location"] is None
                                       or location_key(state["location"], engine) != location_key(expected_loc, engine)):
        error("location", f"engine is at {state['location']!r}, the save says {expected_loc!r}")
    if state["main_desc"] != save.main_desc:
        error("main description", "differs from the save")
    if state["vars_desc"] != save.vars_desc:
        error("additional description", "differs from the save")
    if state["actions"] != [a.desc for a in save.actions]:
        error("actions", f"engine shows {state['actions']!r}")
    if state["objects"] != _objects(save):
        error("objects", f"engine shows {state['objects']!r}")
    for name, value in state["obj"].items():
        if not value:
            error(f"object {name!r}", "OBJ() does not find it (5.9: missing object group?)")

    for var in save.variables:
        expected = [_pair(value) for value in var.values]
        got = state["vars"].get(var.name, [])
        if got == expected:
            pass
        elif not got and expected:
            error(f"variable {var.name!r}", "the engine does not see it (wrong bucket or slot?)")
        else:
            diff = [i for i, (a, b) in enumerate(zip(got, expected)) if a != b]
            if len(got) != len(expected):
                diff.append(min(len(got), len(expected)))
            error(f"variable {var.name!r}", f"values differ at {diff[:5]}: engine {got[diff[0]] if diff[0] < len(got) else None}, "
                                            f"save {expected[diff[0]] if diff[0] < len(expected) else None}")
        read = state["keys"].get(var.name, {})
        for key, position in _text_keys(save, var):
            value = read.get(key)
            if value is None:
                continue  # the key cannot be written as a literal (contains both {} and <<)
            if 0 <= position < len(var.values) and value != _pair(var.values[position]):
                error(f"variable {var.name!r}", f"index {key!r}: engine reads {value}, "
                                                f"save has {_pair(var.values[position])} (unsorted or wrong keys?)")
    return issues


def compare_with_source(source: Save, source_state: dict, target: Save, target_state: dict) -> list[Issue]:
    """Differences between how the source engine sees the original save and how
    the target engine sees the converted one."""
    issues: list[Issue] = []

    def differs(where: str, message: str) -> None:
        issues.append(Issue("error", f"source vs converted: {where}", message))

    a, b = source_state["state"], target_state["state"]
    for field in ("main_desc", "vars_desc", "actions", "objects"):
        if a[field] != b[field]:
            differs(field.replace("_", " "), f"{a[field]!r:.200} vs {b[field]!r:.200}")
    if not a.get("location_lost_by_engine") and not b.get("location_lost_by_engine") and \
            (a["location"] or "").strip().upper() != (b["location"] or "").strip().upper():
        differs("location", f"{a['location']!r} vs {b['location']!r}")
    for name, value in a["obj"].items():
        if bool(value) != bool(b["obj"].get(name)):
            differs(f"object {name!r}", f"OBJ() gives {value} vs {b['obj'].get(name)}")

    source_vars = {v.name: v for v in source.variables}
    for name, before in a["vars"].items():
        after = b["vars"].get(name.upper(), b["vars"].get(name, []))
        var = source_vars.get(name)
        for k, (x, y) in enumerate(zip(before, after)):
            if x == y:
                continue
            original = var.values[k] if var and k < len(var.values) else None
            # Reported by the conversion as lost or intentionally changed:
            if isinstance(original, v59.Variant):
                if original.type is v59.VarType.TUPLE:
                    continue  # tuples do not exist in 5.7.0
                if original.type is v59.VarType.BOOL and bool(x[0]) == bool(y[0]) and x[1] == y[1]:
                    continue  # true is 1 in 5.9 and -1 in 5.7.0
            if isinstance(original, v570.Value) and original.num and original.str:
                continue  # number and string in one element: 5.9 keeps one of them
            differs(f"variable {name!r}[{k}]", f"{x} vs {y}")
            break
        if len(before) != len(after):
            differs(f"variable {name!r}", f"{len(before)} vs {len(after)} values")
    return issues
