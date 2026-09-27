#!/usr/bin/env python3
"""Generate a large synthetic game with saves, and time qspsav on it.

    python tools/bench.py make --out /tmp/bench --locations 1500 --saves 8 --lib570 libqsp-legacy.so
    python tools/bench.py run --out /tmp/bench [--verify] [-j N]

`make` writes game.qsp with LOCATIONS locations of a few KB of code each
(text, <<...>> substitutions, string and number variables, arrays, ACT
blocks), an included lib.qsp, and SAVES saves made by the 5.7.0 engine, with
about two thousand variables each. `run` times `qspsav convert` on them
(--verify needs QSPSAV_LIB570 and QSPSAV_LIB59).
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from qspgame import GameAction, GameLocation, build_game  # noqa: E402

WORDS = ("дорога лес замок дверь ключ меч щит монета стражник король река мост башня "
         "road forest castle door key sword gold guard river bridge tower").split()


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def _location_code(rng: random.Random, i: int, count: int) -> str:
    lines = []
    acts = 0
    for k in range(rng.randint(25, 60)):
        v = rng.randrange(400)
        kind = rng.randrange(8)
        if kind == 0:
            lines.append(f"if var{v} > {rng.randrange(100)} and $flag{v % 50} = 'да':")
            lines.append(f"    *pl '{_sentence(rng, 8)} <<var{v}>>'")
            lines.append(f"    var{v} = var{v} - 1")
            lines.append("else")
            lines.append(f"    $text{v} = '{_sentence(rng, 5)}'")
            lines.append("end")
        elif kind == 1 and acts < 4:
            acts += 1
            lines.append(f"act '{_sentence(rng, 3)}':")
            lines.append(f"    gt 'loc{rng.randrange(count)}'")
            lines.append("end")
        elif kind == 2:
            lines.append(f"$inv['{rng.choice(WORDS)}'] = '{_sentence(rng, 3)}'")
        elif kind == 3:
            lines.append(f"arr{v % 40}[{rng.randrange(30)}] = rand(1, {rng.randrange(2, 1000)})")
        elif kind == 4:
            lines.append(f"if obj '{rng.choice(WORDS)}': addobj '{rng.choice(WORDS)}'")
        elif kind == 5:
            lines.append(f"if var{v} = -123456: gs 'loc{rng.randrange(count)}', {v}")
        elif kind == 6:
            lines.append(f"! {_sentence(rng, 6)}")
        else:
            lines.append(f"p \"{_sentence(rng, 10)} <<$text{v}>> \"\"{rng.choice(WORDS)}\"\"\"")
    return "\n".join(lines)


def make_game(count: int, seed: int = 1) -> tuple[bytes, bytes]:
    rng = random.Random(seed)
    locations = []
    for i in range(count):
        desc = " ".join(_sentence(rng, 12) for _ in range(rng.randint(3, 10))) + f" <<var{rng.randrange(400)}>>"
        actions = [GameAction(_sentence(rng, 3), f"gt 'loc{rng.randrange(count)}'\n{_location_code(rng, i, count)[:400]}")
                   for _ in range(rng.randint(1, 6))]
        locations.append(GameLocation(f"loc{i}", desc, _location_code(rng, i, count), actions))
    lib = [GameLocation(f"libloc{i}", _sentence(rng, 20), _location_code(rng, i, count)) for i in range(count // 10)]
    return build_game(locations), build_game(lib)


def save_script(rng: random.Random, count: int) -> list[str]:
    """Statements that give a save a few thousand variables."""
    parts = []
    for v in range(400):
        parts.append(f"var{v} = {rng.randrange(-1000, 100000)}")
        if v % 3 == 0:
            parts.append(f"$text{v} = '{_sentence(rng, 4)}'")
    for v in range(50):
        parts.append(f"$flag{v} = '{rng.choice(['да', 'нет'])}'")
    for a in range(40):
        for k in range(30):
            parts.append(f"arr{a}[{k}] = {rng.randrange(1000)}")
    for w in WORDS:
        parts.append(f"$inv['{w}'] = '{_sentence(rng, 3)}'")
    for g in range(1500):
        parts.append(f"g{g} = {g}")
    parts += [f"addobj '{w}'" for w in WORDS[:12]]
    code = []
    for start in range(0, len(parts), 200):
        code.append(" & ".join(parts[start:start + 200]))
    code.append("addqst 'lib.qsp'")
    code.append(f"gt 'loc{rng.randrange(count)}'")
    return code


def cmd_make(args: argparse.Namespace) -> None:
    from qspsav.engine import run_in_subprocess

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    game, lib = make_game(args.locations)
    (out / "game.qsp").write_bytes(game)
    (out / "lib.qsp").write_bytes(lib)
    saves = out / "saves"
    if saves.exists():
        shutil.rmtree(saves)
    saves.mkdir()
    print(f"game.qsp: {len(game) / 2**20:.1f} MB, {args.locations} locations; lib.qsp: {len(lib) / 2**20:.1f} MB")
    for n in range(args.saves):
        rng = random.Random(100 + n)
        job = {"engine": "5.7.0", "lib": str(args.lib570), "game": str(out / "game.qsp"),
               "libraries": {"lib.qsp": str(out / "lib.qsp")}, "exec": save_script(rng, args.locations),
               "save_to": str(saves / f"save{n}.sav")}
        result = run_in_subprocess(job)
        if not result["ok"]:
            raise SystemExit(f"making save {n} failed: {result}")
    print(f"{args.saves} saves in {saves}")


def cmd_run(args: argparse.Namespace) -> None:
    out: Path = args.out
    saves = sorted((out / "saves").glob("*.sav"))
    cmd = [sys.executable, "-m", "qspsav", "convert", *map(str, saves), "--game", str(out / "game.qsp"),
           "-o", str(out / "converted"), "--report", str(out / "report.txt")]
    if args.verify:
        cmd.append("--verify")
    if args.jobs:
        cmd += ["-j", str(args.jobs)]
    start = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    print(proc.stdout[-1500:], proc.stderr[-1500:], sep="\n")
    print(f"convert{' --verify' if args.verify else ''} of {len(saves)} saves: {elapsed:.2f} s (exit {proc.returncode})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("make", help="generate the game and saves")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--locations", type=int, default=1500)
    p.add_argument("--saves", type=int, default=8)
    p.add_argument("--lib570", type=Path, required=True, help="libqsp-legacy, which writes the saves")
    p.set_defaults(func=cmd_make)
    p = sub.add_parser("run", help="time qspsav convert on the generated saves")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--verify", action="store_true", help="also check in the engines (needs QSPSAV_LIB*)")
    p.add_argument("-j", "--jobs", type=int)
    p.set_defaults(func=cmd_run)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
