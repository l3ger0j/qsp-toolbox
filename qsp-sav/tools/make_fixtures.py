#!/usr/bin/env python3
"""Build tests/fixtures: test games and saves produced by the real engines.

    python tools/make_fixtures.py --lib570 libqsp-legacy.so --lib59 libqsp.so

Every save is created by running a scenario in the engine (tools/engines.py in
a subprocess) and saving the state, so the fixtures are ground truth for the
reader/writer. manifest.json records, per save, the game it belongs to and the
variable values the engine reported, for the engine-level tests.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from qspgame import GameAction, GameLocation, build_game, build_old_game  # noqa: E402

from qspsav.codec import Encoding  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

SPECIAL_TEXT = "Спецсимволы: [\x05] [\x01] [￻] [😀] [\ud800] конец"


def games() -> dict[str, bytes]:
    main = [
        GameLocation("start", SPECIAL_TEXT, "x = 5\n$s = 'строка'", [
            GameAction("Идти в комнату", "gt 'room'"),
            GameAction("Осмотреться", "*pl 'Ничего особенного'", image="look.png"),
        ]),
        GameLocation("room", "Комната", "", [GameAction("Назад", "gt 'start'")]),
        GameLocation("Локация с пробелом", "Описание"),
        GameLocation("acts", "", "act 'Однострочное': *pl 'нажато'\n"
                                 "act 'Многострочное', 'act.png':\n  x = 1\n  $y = 'два'\nend"),
    ]
    lib = [GameLocation("libloc", "Из библиотеки", "libvar = 1")]
    return {
        "main.qsp": build_game(main),
        "main_ansi.qsp": build_game([GameLocation("start", "Однобайтовая игра", "x = 5"),
                                     GameLocation("room", "Комната")], Encoding.CP1251),
        "lib.qsp": build_game(lib),
        "old_format.qsp": build_old_game([GameLocation("start", "Старый формат", "x = 5", [
            GameAction("Дальше", "gt 'end'")]), GameLocation("end", "Конец")]),
    }


COMMON = [
    "gt 'start'",
    "$name = 'Вася' & a[3] = 7 & $b['key'] = 'v' & $b['Ключ'] = 'w' & $b['abc'] = 'x' & b['num'] = 3"
    " & neg = -12345 & big = 2147483647 & $empty = ''",
    "addobj 'меч' & addobj 'меч' & addobj 'щит', 'shield.png' & addobj 'Меч'",
    "gs 'acts'",
    "settimer 1000 & showacts 0 & showinput 0",
    "*pl 'строка 1' & *pl 'строка 2' & pl 'доп. описание'",
    "view 'pic.png'",
    "play 'music.mp3', 50 & play 'sfx.wav'",
    "i = 1\r\n:m\r\ndynamic 'v<<i>> = i'\r\ni = i + 1\r\nif i <= 300: jump 'm'",
    "$k['z'] = '1' & $k['a'] = '2' & $k['я'] = '3' & $k['A'] = '4' & $k['Ё'] = '5'",
]

ONLY = {
    "5.7.0": ["addqst 'lib.qsp'"],
    "5.9": ["inclib 'lib.qsp'",
            "%t = [1, 'два', [3, 4]] & $c = {*pl 1} & bl = (1 = 1) & arr2[5] = 9 & %e = []",
            "$tk[[1, 'a']] = 'кортеж' & $tk[7] = 'число'",
            "modobj 'щит', 'Щит (новый)', 'shield2.png'"],
}

CHECK_VARS = ["X", "S", "NAME", "A", "B", "NEG", "BIG", "EMPTY", "V1", "V300", "K", "Y", "LIBVAR", "ARR"]

SCENARIOS = {
    # name: (game, steps, extra engine arguments, loadable by the engine)
    "start": ("main.qsp", ["gt 'start'"], [], True),
    "full": ("main.qsp", None, ["--input", "текст ввода", "--select-action", "1", "--select-object", "2"], True),
    "ansi_game": ("main_ansi.qsp", ["gt 'start'", "$name = 'Вася'"], [], True),
    "old_format": ("old_format.qsp", ["gt 'start'", "$name = 'Вася'"], [], True),
    # the current location comes from an included file
    "in_library": ("main.qsp", {"5.7.0": ["addqst 'lib.qsp'", "gt 'libloc'"],
                                "5.9": ["inclib 'lib.qsp'", "gt 'libloc'"]}, [], True),
    # 5.7.0 keeps a number and a string in the same element; 5.9 cannot
    "both_parts": ("main.qsp", {"5.7.0": ["gt 'start'", "x = 5 & $x = 'пять' & arr[2] = 4 & $arr[2] = 'четыре'"],
                                "5.9": ["gt 'start'", "x = 5 & $x = 'пять' & arr[2] = 4 & $arr[2] = 'четыре'"]},
                   [], True),
    # ACT run outside any location (e.g. from a player's "execute code" box)
    # stores location -1; both engines write such a save but refuse to load it.
    "act_outside_location": ("main.qsp", ["gt 'start'", "act 'Вне локации': x = 1"], [], False),
}


def run_engine(engine: str, lib: Path, game: Path, steps: list[str], extra: list[str], save: Path) -> dict:
    cmd = [sys.executable, str(ROOT / "tools" / "engines.py"), "run", "--engine", engine, "--lib", str(lib),
           "--game", str(game), "--save", str(save), "--library", f"lib.qsp={FIXTURES / 'lib.qsp'}"]
    for step in steps:
        cmd += ["--exec", step]
    for name in CHECK_VARS:
        cmd += ["--var", name]
    cmd += extra
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    result = json.loads(out)
    if not result["ok"]:
        raise SystemExit(f"{engine} {save.name}: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lib570", type=Path, required=True)
    parser.add_argument("--lib59", type=Path, required=True)
    args = parser.parse_args()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, data in games().items():
        (FIXTURES / name).write_bytes(data)

    manifest = {}
    for engine, lib in (("5.7.0", args.lib570), ("5.9", args.lib59)):
        for name, (game, steps, extra, loadable) in SCENARIOS.items():
            if steps is None:
                steps = COMMON + ONLY[engine]
            elif isinstance(steps, dict):
                steps = steps[engine]
            save = FIXTURES / f"{name}.{engine}.sav"
            result = run_engine(engine, lib, FIXTURES / game, steps, extra, save)
            manifest[save.name] = {"engine": engine, "game": game, "loadable": loadable, "vars": result["vars"]}
            print(f"{save.name}: {save.stat().st_size} bytes")
    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")


if __name__ == "__main__":
    main()
