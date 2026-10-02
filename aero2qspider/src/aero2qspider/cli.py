"""Command line interface: `aero2qspider convert` and `aero2qspider setup`."""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .constants import AERO_DEFAULT_FONT_SIZE, AERO_EFFECTS, GAME_SOURCE_EXTENSIONS, QSPIDER_MISSING_EFFECTS
from .engines import EngineError, build_qsp57, cache_dir, node_problem, qsp57_problem, setup_node
from .findings import RULE_TITLES
from .pipeline import GameResult, count, run_games
from .resources import ARCHIVE_EXTENSIONS, slugify


def expand_inputs(inputs: list[str]) -> list[Path]:
    """A folder that holds archives but no game file of its own stands for those archives."""
    result: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if not path.exists() and any(ch in raw for ch in "*?["):
            result += sorted(Path().glob(raw))
            continue
        if path.is_dir():
            top = [p for p in path.iterdir() if p.is_file()]
            has_game = any(p.suffix.lower() in GAME_SOURCE_EXTENSIONS for p in top)
            archives = sorted(p for p in top if p.suffix.lower() in ARCHIVE_EXTENSIONS)
            if archives and not has_game:
                result += archives
                continue
        result.append(path)
    return result


def unique_out_dirs(root: Path, sources: list[Path]) -> list[Path]:
    used: set[str] = set()
    dirs = []
    for src in sources:
        name = slugify(src.stem if src.is_file() else src.name)
        candidate, n = name, 2
        while candidate in used:
            candidate, n = f"{name}-{n}", n + 1
        used.add(candidate)
        dirs.append(root / candidate)
    return dirs


def _optional(value: int | None, noun: str) -> str:
    return "" if value is None else f", {count(value, noun)}"


def format_result(done: int, total: int, result: GameResult) -> str:
    prefix = f"[{done}/{total}]" if total > 1 else ""
    if not result.ok:
        return f"{prefix} FAIL {result.source}: {result.error}".strip()
    counts = result.counts()
    return (
        f"{prefix} OK {result.title}: {counts['fix']} fixed, {count(counts['warn'], 'warning')}, {count(counts['info'], 'note')}"
        f"{_optional(result.smoke_errors, 'runtime error')}{_optional(result.divergences, 'diverged run')} "
        f"({result.seconds:.1f}s) -> {result.out_dir}"
    ).strip()


def write_summary(root: Path, results: list[GameResult]) -> None:
    lines = [
        f"# aero2qspider {__version__} summary",
        "",
        "| Game | Status | Fixed | Warnings | Notes | Runtime errors | Diverged runs | Time, s | Folder |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: r.source):
        c = r.counts()
        status = "OK" if r.ok else f"FAIL: {r.error}"
        smoke = "-" if r.smoke_errors is None else str(r.smoke_errors)
        diverged = "-" if r.divergences is None else str(r.divergences)
        folder = Path(r.out_dir).name
        lines.append(
            f"| {r.title or Path(r.source).name} | {status} | {c['fix']} | {c['warn']} | {c['info']} | {smoke} | {diverged} "
            f"| {r.seconds:.1f} | [{folder}]({folder}/CONVERSION_REPORT.md) |"
        )
    rules: Counter[str] = Counter()
    for r in results:
        rules.update(r.as_json()["warning_rules"])  # type: ignore[arg-type]
    if rules:
        lines += ["", "## Most frequent warnings", ""]
        lines += [f"- {RULE_TITLES.get(rule, rule)}: {n}" for rule, n in rules.most_common()]
    (root / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_convert_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "convert",
        help="convert AeroQSP games for qSpider",
        description="Convert AeroQSP games (.aqsp) into folders that qSpider can open.",
    )
    p.add_argument("input", nargs="+", help=".aqsp/.zip archives, game folders, folders with archives, .qsp/.qsps files")
    p.add_argument("-o", "--output", required=True, help="output folder (the root folder when there are several games)")

    game = p.add_argument_group("game")
    game.add_argument("--source", help="use this .qsps as the code of the main game file (single game only)")
    game.add_argument("--title", help="title for game.cfg (single game only)")
    game.add_argument("--id", help="id for game.cfg (single game only)")
    game.add_argument("--entry", choices=["qsp", "qsps"], default="qsp", help="game file referenced by game.cfg (default: qsp)")

    theme = p.add_argument_group("theme")
    theme.add_argument(
        "--font-size",
        type=int,
        default=AERO_DEFAULT_FONT_SIZE,
        help=f"default FSIZE of the compatibility theme (default: {AERO_DEFAULT_FONT_SIZE}, as in AeroQSP)",
    )
    theme.add_argument(
        "--aero-theme",
        metavar="PATH|URL|latest",
        help="qSpider's aero.html to base the theme on (default: the bundled copy; `latest` downloads it from GitHub)",
    )
    theme.add_argument("--no-theme", action="store_true", help="do not create the compatibility theme")

    fixes = p.add_argument_group("fixes")
    fixes.add_argument(
        "--effect-fallback",
        default="fade",
        choices=sorted(AERO_EFFECTS - QSPIDER_MISSING_EFFECTS),
        metavar="EFFECT",
        help="replacement for the pixels, h_blinds and v_blinds effects (default: fade)",
    )
    fixes.add_argument("--no-fix-paths", action="store_true", help="keep resource paths as they are")
    fixes.add_argument("--no-fix-colors", action="store_true", help="keep AeroQSP color names")
    fixes.add_argument("--no-fix-stat-format", action="store_true", help="keep $STAT_FORMAT")
    fixes.add_argument("--no-fix-missing-skin-images", action="store_true", help="keep references to missing skin images")
    fixes.add_argument("--no-fix-rand", action="store_true", help="keep RAND(n) (0..n in AeroQSP, 1..n in qSpider)")
    fixes.add_argument(
        "--no-fix-bare-calls", action="store_true", help="keep FUNC/DYNEVAL statements that printed an empty line in AeroQSP"
    )
    fixes.add_argument("--no-logic-check", action="store_true", help="skip the QSP 5.7 vs 5.9 logic heuristics")

    checks = p.add_argument_group("engine checks (need `aero2qspider setup`)")
    checks.add_argument("--smoke", action="store_true", help="play the converted game randomly on QSP 5.9 and report runtime errors")
    checks.add_argument("--smoke-steps", type=int, default=1500, help="steps per smoke run (default: 1500)")
    checks.add_argument("--smoke-seeds", type=int, default=3, help="smoke runs with different seeds (default: 3)")
    checks.add_argument(
        "--compare",
        action="store_true",
        help="play the original on QSP 5.7 and the converted game on QSP 5.9 side by side and report where they differ",
    )
    checks.add_argument("--compare-steps", type=int, default=500, help="steps per paired run (default: 500)")
    checks.add_argument("--compare-seeds", type=int, default=3, help="paired runs with different seeds (default: 3)")

    batch = p.add_argument_group("batch")
    batch.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1), help="games converted at once")
    batch.add_argument("--engine-jobs", type=int, help="games checked on the engines at once (default: --jobs)")
    batch.add_argument("--jsonl", action="store_true", help="print one JSON line per game as soon as it is ready")
    batch.add_argument("--zip", action="store_true", help="also pack every result into a .zip")
    batch.add_argument("--force", action="store_true", help="write into a non-empty output folder")
    p.set_defaults(handler=run_convert)


def add_setup_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "setup",
        help="install the engines used by --smoke and --compare",
        description=(
            "Install @qsp/wasm-engine (QSP 5.9) with npm and build libqsp 5.7 from source "
            f"into {cache_dir()} (override with AERO2QSPIDER_HOME)."
        ),
    )
    p.add_argument("--skip-node", action="store_true", help="do not install the QSP 5.9 engine")
    p.add_argument("--skip-qsp57", action="store_true", help="do not build libqsp 5.7")
    p.add_argument("--cc", help="C compiler for libqsp 5.7 (default: $CC, cc, gcc or clang)")
    p.add_argument("--qsp-source", type=Path, help="use this libqsp `qsp` source folder instead of downloading it")
    p.add_argument("--check", action="store_true", help="only report what is installed")
    p.set_defaults(handler=run_setup)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aero2qspider", description="Convert AeroQSP games for qSpider.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    add_convert_parser(sub)
    add_setup_parser(sub)
    return parser


def run_setup(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    def log(message: str) -> None:
        print(f"  {message}", flush=True)

    if args.check:
        for name, problem in (("QSP 5.9 engine", node_problem()), ("libqsp 5.7", qsp57_problem())):
            print(f"{name}: ready" if not problem else f"{name}: missing ({problem})")
        return 0 if not (node_problem() or qsp57_problem()) else 1
    status = 0
    if not args.skip_node:
        print("QSP 5.9 (@qsp/wasm-engine):", flush=True)
        try:
            setup_node(log)
            print("  ready")
        except EngineError as error:
            print(f"  failed: {error}", file=sys.stderr)
            status = 1
    if not args.skip_qsp57:
        print("QSP 5.7 (libqsp):", flush=True)
        try:
            log(f"built {build_qsp57(log, args.cc, args.qsp_source)}")
            print("  ready")
        except (EngineError, OSError) as error:
            print(f"  failed: {error}", file=sys.stderr)
            status = 1
    return status


def run_convert(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    sources = expand_inputs(args.input)
    if not sources:
        parser.error("no games found")
    output = Path(args.output)
    if len(sources) > 1:
        for option in ("source", "title", "id"):
            if getattr(args, option):
                parser.error(f"--{option} can only be used with a single game")
        out_dirs = unique_out_dirs(output, sources)
    else:
        out_dirs = [output]
    jobs = [argparse.Namespace(**{**vars(args), "input": str(src), "output": str(out)}) for src, out in zip(sources, out_dirs, strict=True)]
    for job in jobs:
        del job.handler

    stream_path = output / "summary.jsonl" if len(jobs) > 1 else None
    if stream_path:
        output.mkdir(parents=True, exist_ok=True)
        stream_path.write_text("", encoding="utf-8")

    def on_result(done: int, total: int, result: GameResult) -> None:
        if args.jsonl:
            print(json.dumps(result.as_json(), ensure_ascii=False), flush=True)
        else:
            print(format_result(done, total, result), file=sys.stdout if result.ok else sys.stderr, flush=True)
            if result.ok and total == 1:
                print(f"  report: {Path(result.out_dir) / 'CONVERSION_REPORT.md'}")
                if result.archive:
                    print(f"  archive: {result.archive}")
        if stream_path:
            with stream_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result.as_json(), ensure_ascii=False) + "\n")

    try:
        results = asyncio.run(run_games(jobs, args.jobs, args.engine_jobs or args.jobs, on_result))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    if len(jobs) > 1:
        write_summary(output, results)
        if not args.jsonl:
            print(f"done: {sum(r.ok for r in results)} of {len(results)} converted; summary: {output / 'SUMMARY.md'}")
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args, parser)
    except BrokenPipeError:
        # the reader of stdout (e.g. `| head`) has gone; keep Python from failing on the final flush
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
