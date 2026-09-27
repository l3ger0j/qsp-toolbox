"""Command line: qspsav unpack | pack | validate | convert | verify | game | info."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import convert, jsonio, read_save, v59, v570, validate_save, write_save
from .code import VarUsage, scan_game_usage
from .codec import Encoding, Issue, SaveFormatError
from .game import GameFiles, LocationTable, build_location_table, read_game, resolve_include
from .tables import crc_570, crc_59
from .verify import (Libraries, check_state, compare_with_source, inspect_job, inspect_many,
                     location_lost_by_engine)

Save = v570.Save | v59.Save


def _print_issues(issues: list[Issue]) -> None:
    for issue in issues:
        print(f"  {issue}", file=sys.stderr)


def _load_any(path: Path) -> tuple[Save, list[Issue]]:
    """A .json produced by unpack, or a save file (detected by content)."""
    data = path.read_bytes()
    if data.lstrip()[:1] == b"{":
        return jsonio.loads(data.decode("utf-8")), []
    return read_save(data)


def _game_crc(save: Save, game: Path | None) -> int | None:
    if game is None:
        return None
    return _file_crc(game.resolve(), isinstance(save, v570.Save))


@lru_cache(maxsize=None)
def _file_crc(path: Path, legacy: bool) -> int:
    """The game CRC, computed once per file and engine for a whole run."""
    data = path.read_bytes()
    return crc_570(data) if legacy else crc_59(data)


def _engine(save: Save) -> str:
    return v570.ENGINE if isinstance(save, v570.Save) else v59.ENGINE


def cmd_unpack(args: argparse.Namespace) -> int:
    save, issues = read_save(args.save.read_bytes())
    out = args.output or args.save.with_suffix(".json")
    out.write_text(jsonio.dumps(save), encoding="utf-8")
    print(f"{args.save} ({_engine(save)}, {save.encoding.value}) -> {out}")
    if issues:
        print("notes while reading:", file=sys.stderr)
        _print_issues(issues)
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    save = jsonio.loads(args.json.read_text(encoding="utf-8"))
    if args.encoding:
        if isinstance(save, v570.Save) and args.encoding != Encoding.UCS2:
            raise SaveFormatError("5.7.0 saves are always UCS-2")
        save.encoding = Encoding(args.encoding)
    issues = validate_save(save, _game_crc(save, args.game))
    errors = [i for i in issues if i.level == "error"]
    if issues:
        print("validation:", file=sys.stderr)
        _print_issues(issues)
    if errors and not args.force:
        print(f"not written: {len(errors)} error(s) would make the engine reject or misread the save; "
              "fix them or pass --force", file=sys.stderr)
        return 1
    out = args.output or args.json.with_suffix(".sav")
    out.write_bytes(write_save(save))
    print(f"{args.json} ({_engine(save)}, {save.encoding.value}) -> {out}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    worst = 0
    for path in args.saves:
        try:
            save, read_issues = _load_any(path)
        except (SaveFormatError, ValueError) as exc:
            print(f"{path}: cannot read: {exc}")
            worst = 2
            continue
        issues = read_issues + validate_save(save, _game_crc(save, args.game))
        if not issues:
            print(f"{path}: OK ({_engine(save)})")
            continue
        print(f"{path} ({_engine(save)}):")
        for issue in issues:
            print(f"  {issue}")
        if any(i.level == "error" for i in issues):
            worst = max(worst, 1)
    return worst


def _explicit_includes(items: list[str]) -> dict[str, Path]:
    mapping = {}
    for item in items:
        name, sep, path = item.partition("=")
        if not sep:
            raise ValueError(f"--include expects NAME=PATH, got {item!r}")
        mapping[name] = Path(path)
    return mapping


@dataclass
class GameSetup:
    """A game as an engine runs it with the included files a save names."""

    game_path: Path
    table: LocationTable
    includes: dict[str, Path]  # stored include name -> file found for it
    games: list  # the parsed main game and found included files


class Games:
    """Everything derived from the game files, computed once per run.

    Saves of one game share it: the files are read and parsed once, location
    tables are built once per engine and set of included files, and the game
    code is scanned for variable usage once. On a game of a thousand
    locations this is almost all the work a conversion does.
    """

    def __init__(self, game_dir: Path | None, explicit: dict[str, Path]) -> None:
        self.files = GameFiles()
        self.game_dir = game_dir
        self.explicit = explicit
        self._setups: dict[tuple, GameSetup] = {}
        self._usage: dict[tuple, VarUsage] = {}

    def setup(self, engine: str, game_path: Path, includes: list[str]) -> GameSetup:
        key = (engine, game_path.resolve(), tuple(includes))
        found = self._setups.get(key)
        if found is None:
            game_dir = self.game_dir or game_path.parent
            main = self.files.read(game_path)
            table = build_location_table(engine, main, game_path.name, includes, game_dir, self.explicit,
                                         self.files)
            paths: dict[str, Path] = {}
            games = [main]
            for stored in includes:
                path = resolve_include(stored, game_dir, self.explicit, self.files)
                if path is not None:
                    paths[stored] = path
                    games.append(self.files.read(path))
            found = self._setups[key] = GameSetup(game_path, table, paths, games)
        return found

    def usage(self, setup: GameSetup) -> VarUsage:
        key = tuple(id(game) for game in setup.games)
        usage = self._usage.get(key)
        if usage is None:
            usage = self._usage[key] = scan_game_usage(setup.games)
        return usage


def _saved_file(save: Save, path: Path, tmp: Path) -> Path:
    """A .sav file for the engine: the input itself, or the JSON packed into tmp."""
    if path.read_bytes().lstrip()[:1] != b"{":
        return path
    out = tmp / f"{len(list(tmp.iterdir()))}-{path.stem}.sav"
    out.write_bytes(write_save(save))
    return out


def _print_by_level(issues: list[Issue]) -> dict[str, int]:
    counts = {level: sum(1 for i in issues if i.level == level) for level in ("error", "warning", "note")}
    for level in ("error", "warning", "note"):
        found = [i for i in issues if i.level == level]
        if found:
            print(f"{level}s ({len(found)}):", file=sys.stderr)
            _print_issues(found)
    return counts


@dataclass
class Conversion:
    """A converted save and what is needed to check it in the engines."""

    path: Path
    save: Save
    source: str
    target: str
    result: Save
    out: Path
    source_setup: GameSetup
    target_setup: GameSetup


def _convert_one(path: Path, args: argparse.Namespace, out: Path | None,
                 games: Games) -> tuple[int, list[Issue], Conversion | None]:
    save, issues = _load_any(path)
    source = _engine(save)
    target = args.to or (v59.ENGINE if source == v570.ENGINE else v570.ENGINE)
    if target == source:
        raise ValueError(f"the save is already {source}")
    target_game_path = args.target_game or args.game
    source_setup = games.setup(source, args.game, save.includes)
    target_setup = games.setup(target, target_game_path, save.includes)
    target_crc = games.files.read(target_game_path).crc_for(target)
    ctx = convert.Context(source_setup.table, target_setup.table, target_crc, games.usage(source_setup))
    issues += source_setup.table.issues
    issues += [i for i in target_setup.table.issues if i not in source_setup.table.issues]
    if target == v59.ENGINE:
        result, conv_issues = convert.to_59(save, ctx, args.version or v59.GAME_MIN_VERSION)
    else:
        result, conv_issues = convert.to_570(save, ctx)
    issues += conv_issues

    counts = _print_by_level(issues)
    summary = f"{counts['error']} error(s), {counts['warning']} warning(s), {counts['note']} note(s)"
    if counts["error"] and not args.force:
        print(f"{path} ({source}): not written, {summary}; fix the errors or pass --force", file=sys.stderr)
        return 1, issues, None
    if out is None or out.is_dir():
        out = (out or path.parent) / f"{path.stem}.to-{target}.sav"
    if out.suffix.lower() == ".json":
        out.write_text(jsonio.dumps(result), encoding="utf-8")
    else:
        out.write_bytes(write_save(result))
    print(f"{path} ({source}) -> {out} ({target}): {summary}")
    return 0, issues, Conversion(path, save, source, target, result, out, source_setup, target_setup)


def _verify_conversions(conversions: list[Conversion], args: argparse.Namespace, libs: Libraries) -> list[list[Issue]]:
    """Load every result into the target engine and, when the source engine is
    available too, the original into the source engine; compare. The engine
    runs go in parallel."""
    for conv in conversions:
        if libs.for_engine(conv.target) is None:
            raise ValueError(f"--verify needs the {conv.target} engine library "
                             "(--lib570/--lib59 or QSPSAV_LIB570/QSPSAV_LIB59)")
    with tempfile.TemporaryDirectory() as tmp:
        jobs, owners = [], []
        for n, conv in enumerate(conversions):
            t = conv.target_setup
            jobs.append(inspect_job(conv.result, _saved_file(conv.result, conv.out, Path(tmp)), t.game_path,
                                    t.includes, libs.for_engine(conv.target)))
            owners.append((n, "target"))
            if libs.for_engine(conv.source):
                s = conv.source_setup
                jobs.append(inspect_job(conv.save, _saved_file(conv.save, conv.path, Path(tmp)), s.game_path,
                                        s.includes, libs.for_engine(conv.source)))
                owners.append((n, "source"))
        results = asyncio.run(inspect_many(jobs, args.jobs))
    states: list[dict] = [{} for _ in conversions]
    for (n, role), result in zip(owners, results):
        states[n][role] = result

    report = []
    for conv, state in zip(conversions, states):
        target_state = state["target"]
        checked = check_state(conv.result, target_state, conv.target_setup.table)
        source_state = state.get("source")
        if target_state["ok"] and source_state is not None:
            if source_state["ok"]:
                if location_lost_by_engine(conv.save, source_state, conv.source_setup.table):
                    checked.append(Issue("note", "verify", "the 5.9 engine loads the original save with no current "
                                                           "location (it comes from an included file), so the "
                                                           "location was not compared"))
                checked += compare_with_source(conv.save, source_state, conv.result, target_state)
            else:
                checked.append(Issue("note", "verify", "the source engine refuses the original save; "
                                                       "only the converted save was checked"))
        report.append(checked)
    return report


def cmd_convert(args: argparse.Namespace) -> int:
    libs = Libraries.from_env(args.lib570, args.lib59)
    # -o is a directory when it exists as one, when there are several saves,
    # or when it has no .sav/.json extension.
    if args.output and (args.output.is_dir() or len(args.saves) > 1
                        or args.output.suffix.lower() not in (".sav", ".json")):
        args.output.mkdir(parents=True, exist_ok=True)
    games = Games(args.game_dir, _explicit_includes(args.include))
    worst = 0
    findings: list[tuple[Path, list[Issue]]] = []
    conversions: list[tuple[int, Conversion]] = []
    for path in args.saves:
        try:
            code, issues, conv = _convert_one(path, args, args.output, games)
        except (SaveFormatError, ValueError, RuntimeError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            code, issues, conv = 2, [Issue("error", str(path), str(exc))], None
        worst = max(worst, code)
        if conv is not None and args.verify:  # kept in memory only when there is a check to run
            conversions.append((len(findings), conv))
        findings.append((path, issues))

    if args.verify and conversions:
        try:
            checks = _verify_conversions([conv for _, conv in conversions], args, libs)
        except (ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            checks, worst = [], 2
        for (n, conv), checked in zip(conversions, checks):
            failed = any(i.level == "error" for i in checked)
            if checked:
                print(f"{conv.out}:", file=sys.stderr)
                _print_by_level(checked)
            if failed:
                print(f"{conv.out}: verification FAILED")
                worst = max(worst, 1)
            else:
                against = " against the source engine" if libs.for_engine(conv.source) else ""
                print(f"{conv.out}: verified in the {conv.target} engine{against}")
            findings[n][1].extend(checked)

    if args.report:
        args.report.write_text("\n".join(f"== {path}\n" + "".join(f"{i}\n" for i in issues)
                                         for path, issues in findings), encoding="utf-8")
    return worst


def cmd_verify(args: argparse.Namespace) -> int:
    libs = Libraries.from_env(args.lib570, args.lib59)
    games = Games(args.game_dir, _explicit_includes(args.include))
    worst = 0
    outcome: list[tuple[Path, tuple[str, list[Issue]] | str]] = []
    pending = []  # (position in outcome, save, engine, setup, job)
    with tempfile.TemporaryDirectory() as tmp:
        for path in args.saves:
            try:
                save, _ = _load_any(path)
                engine = _engine(save)
                lib = libs.for_engine(engine)
                if lib is None:
                    raise ValueError(f"no {engine} engine library (--lib570/--lib59 or QSPSAV_LIB570/QSPSAV_LIB59)")
                setup = games.setup(engine, args.game, save.includes)
                job = inspect_job(save, _saved_file(save, path, Path(tmp)), args.game, setup.includes, lib)
                pending.append((len(outcome), save, engine, setup, job))
                outcome.append((path, ""))
            except (SaveFormatError, ValueError, RuntimeError) as exc:
                outcome.append((path, str(exc)))
        try:
            results = asyncio.run(inspect_many([p[-1] for p in pending], args.jobs))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    for (n, save, engine, setup, _), result in zip(pending, results):
        outcome[n] = (outcome[n][0], (engine, setup.table.issues + check_state(save, result, setup.table)))

    for path, item in outcome:
        if isinstance(item, str):
            print(f"{path}: {item}")
            worst = 2
            continue
        engine, issues = item
        if not issues:
            print(f"{path}: OK in the {engine} engine")
            continue
        print(f"{path} ({engine}):")
        for issue in issues:
            print(f"  {issue}")
        if any(i.level == "error" for i in issues):
            worst = max(worst, 1)
    return worst


def cmd_game(args: argparse.Namespace) -> int:
    game = read_game(args.game.read_bytes())
    fmt = "old" if game.old_format else "QSPGAME"
    print(f"format: {fmt}, {game.encoding.value}, {len(game.locations)} locations")
    print(f"CRC for 5.7.0 saves: {game.crc_570}, for 5.9 saves: {game.crc_59}")
    if args.locations:
        for i, loc in enumerate(game.locations):
            print(f"  #{i} {loc.name!r} ({len(loc.actions)} actions)")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    save, _ = _load_any(args.save)
    print(f"engine:     {_engine(save)} (file version {save.version!r}, {save.encoding.value})")
    print(f"game CRC:   {save.game_crc}")
    loc = save.cur_loc
    print(f"location:   {loc!r}" if isinstance(loc, str) else f"location:   #{loc}")
    print(f"actions:    {len(save.actions)}, objects: {len(save.objects)}, variables: {len(save.variables)}")
    print(f"playlist:   {len(save.playlist)}, included files: {', '.join(save.includes) or '-'}")
    return 0


def _jobs_option(p: argparse.ArgumentParser) -> None:
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, metavar="N",
                   help="engine processes to run at once when checking (default: number of CPUs)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qspsav", description="QSP 5.7.0 / 5.9 save files.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("unpack", help="save file -> JSON (lossless)")
    p.add_argument("save", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("pack", help="JSON -> save file")
    p.add_argument("json", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--encoding", choices=[e.value for e in Encoding],
                   help="5.9 only: write a UCS-2 (default, what the engine writes) or CP1251 save")
    p.add_argument("--game", type=Path, help="game file (.qsp) to check the CRC against")
    p.add_argument("--force", action="store_true", help="write even if validation finds errors")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("validate", help="check saves (or unpacked JSON) the way the engine would")
    p.add_argument("saves", type=Path, nargs="+")
    p.add_argument("--game", type=Path, help="game file (.qsp) to check the CRC against")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("convert", help="convert saves between 5.7.0 and 5.9")
    p.add_argument("saves", type=Path, nargs="+", help="save files or unpacked JSON (all of the same game)")
    p.add_argument("--game", type=Path, required=True, help="the game file (.qsp) the save belongs to")
    p.add_argument("--target-game", type=Path,
                   help="game file the target engine will load, if different (e.g. a converted game)")
    p.add_argument("--game-dir", type=Path, help="where included files (INCLIB/ADDQST) are; default: the game's folder")
    p.add_argument("--include", action="append", default=[], metavar="NAME=PATH",
                   help="explicit file for an included file name stored in the save (repeatable)")
    p.add_argument("--to", choices=[v570.ENGINE, v59.ENGINE], help="target engine (default: the other one)")
    p.add_argument("--version", help=f"version written into a 5.9 save (default {v59.GAME_MIN_VERSION}, "
                                     "accepted by every 5.9.4+ player)")
    p.add_argument("-o", "--output", type=Path,
                   help="output .sav or .json, or a directory (required to be one for several saves); "
                        "default: next to the input as NAME.to-<target>.sav")
    p.add_argument("--report", type=Path, help="also write all findings to this file")
    p.add_argument("--force", action="store_true", help="write even if there are errors")
    p.add_argument("--verify", action="store_true",
                   help="load the result into the target engine (and the original into the source engine) "
                        "and compare; needs the engine libraries")
    p.add_argument("--lib570", type=Path, help="libqsp-legacy (default: $QSPSAV_LIB570)")
    p.add_argument("--lib59", type=Path, help="libqsp 5.9 (default: $QSPSAV_LIB59)")
    _jobs_option(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("verify", help="load saves into the real engine and compare with the file")
    p.add_argument("saves", type=Path, nargs="+", help="save files or unpacked JSON (all of the same game)")
    p.add_argument("--game", type=Path, required=True, help="the game file (.qsp)")
    p.add_argument("--game-dir", type=Path, help="where included files are; default: the game's folder")
    p.add_argument("--include", action="append", default=[], metavar="NAME=PATH",
                   help="explicit file for an included file name stored in the save (repeatable)")
    p.add_argument("--lib570", type=Path, help="libqsp-legacy (default: $QSPSAV_LIB570)")
    p.add_argument("--lib59", type=Path, help="libqsp 5.9 (default: $QSPSAV_LIB59)")
    _jobs_option(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("game", help="game file summary: format, CRC, locations")
    p.add_argument("game", type=Path)
    p.add_argument("--locations", action="store_true", help="list locations with their indices")
    p.set_defaults(func=cmd_game)

    p = sub.add_parser("info", help="short summary")
    p.add_argument("save", type=Path)
    p.set_defaults(func=cmd_info)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SaveFormatError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
