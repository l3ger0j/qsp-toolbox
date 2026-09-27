#!/usr/bin/env python3
"""Command line over qspsav.engine: drive a real QSP engine.

    engines.py run --engine 5.9 --lib libqsp.so --game game.qsp \
        [--library NAME=PATH]... [--open-save in.sav] [--exec CODE]... [--action N]... \
        [--input TEXT] [--select-action N] [--select-object N] [--save out.sav] [--var NAME]...

Prints one JSON object: {"ok": ..., "error": ..., "version": ..., "vars": {...}}.
Load only one engine per process (both libraries export the same symbols).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qspsav.engine import ENGINES, run_job  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="load a game, run code, open/save a game state")
    run.add_argument("--engine", choices=ENGINES, required=True)
    run.add_argument("--lib", type=Path, required=True)
    run.add_argument("--game", type=Path, required=True)
    run.add_argument("--library", action="append", default=[], metavar="NAME=PATH",
                     help="game file served to INCLIB/ADDQST under NAME (repeatable)")
    run.add_argument("--open-save", type=Path)
    run.add_argument("--exec", action="append", default=[], help="code to execute (repeatable)")
    run.add_argument("--action", type=int, action="append", default=[],
                     help="execute the current action with this index (repeatable, after --exec)")
    run.add_argument("--input", help="text of the input line")
    run.add_argument("--select-action", type=int)
    run.add_argument("--select-object", type=int)
    run.add_argument("--save", type=Path, help="write the game state here")
    run.add_argument("--var", action="append", default=[], help="variable to report (repeatable)")
    args = parser.parse_args()

    job = {
        "engine": args.engine, "lib": str(args.lib), "game": str(args.game),
        "libraries": dict(item.split("=", 1) for item in args.library),
        "open_save": str(args.open_save) if args.open_save else None,
        "exec": args.exec, "actions": args.action, "input": args.input,
        "select_action": args.select_action, "select_object": args.select_object,
        "save_to": str(args.save) if args.save else None, "vars": args.var,
    }
    print(json.dumps(run_job(job), ensure_ascii=False))


if __name__ == "__main__":
    main()
