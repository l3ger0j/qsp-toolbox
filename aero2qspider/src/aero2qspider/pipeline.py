"""Conversion of one game and the asynchronous pipeline that converts many games concurrently."""

import argparse
import asyncio
import dataclasses
import multiprocessing
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from .compare import CompareResult, paired_run, write_instrumented
from .constants import AERO_DEFAULT_FONT_SIZE, GAME_EXTENSIONS, GAME_SOURCE_EXTENSIONS, JUNK_EXTENSIONS, JUNK_NAMES, THEME_NAME
from .engines import node_command, node_problem, qsp57_command, qsp57_problem
from .findings import Finding
from .fixes import Converter
from .logic import LogicAnalyzer, code_blocks
from .qsp_format import Location, read_qsp, read_qsps, write_qsp, write_qsps
from .report import LineMaps, render_report
from .resources import (
    ARCHIVE_EXTENSIONS,
    ResourceIndex,
    decode_text,
    load_input,
    pick_main_game,
    slugify,
    strip_common_root,
    swf_font_names,
    toml_str,
)
from .smoke import run_smoke
from .theme import COMPAT_CSS, build_theme, load_aero_theme


class ConversionError(Exception):
    """A problem with the input that stops the conversion of one game."""


@dataclasses.dataclass
class GameResult:
    source: str
    out_dir: str
    ok: bool = True
    error: str = ""
    title: str = ""
    findings: list[Finding] = dataclasses.field(default_factory=list)
    line_maps: LineMaps = dataclasses.field(default_factory=dict)
    summary: dict[str, object] = dataclasses.field(default_factory=dict)
    main_qsp: str = ""
    main_qsps: str = ""
    first_location: str = ""
    compare_dir: str = ""
    smoke_errors: int | None = None
    divergences: int | None = None
    archive: str = ""
    seconds: float = 0.0

    def counts(self) -> Counter[str]:
        return Counter(f.severity for f in self.findings)

    def as_json(self) -> dict[str, object]:
        counts = self.counts()
        return {
            "source": self.source,
            "out_dir": self.out_dir,
            "ok": self.ok,
            "error": self.error,
            "title": self.title,
            "fixed": counts["fix"],
            "warnings": counts["warn"],
            "info": counts["info"],
            "warning_rules": dict(Counter(f.rule for f in self.findings if f.severity == "warn")),
            "smoke_errors": self.smoke_errors,
            "divergences": self.divergences,
            "archive": self.archive,
            "seconds": round(self.seconds, 2),
        }


def _read_game_file(name: str, data: bytes, source: str | None) -> list[Location]:
    if source:
        return read_qsps(Path(source).read_text(encoding="utf-8-sig"))
    if name.lower().endswith(".qsps"):
        return read_qsps(data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8-sig"))
    return read_qsp(data)


def convert_game(args: argparse.Namespace) -> GameResult:
    """Convert one game and write everything except the report. CPU-bound: runs in a worker process."""
    src = Path(args.input)
    out_dir = Path(args.output)
    result = GameResult(source=str(src), out_dir=str(out_dir))
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        raise ConversionError(f"{out_dir} is not empty; use --force")

    conv = Converter(args)
    if src.is_file() and src.suffix.lower() in GAME_SOURCE_EXTENSIONS:
        files, archive_findings = load_input(src.parent, exclude=out_dir)
        main_name: str | None = src.name
        # other games in subfolders of the game's folder are not part of this game
        files = {n: d for n, d in files.items() if "/" not in n or not n.lower().endswith(GAME_SOURCE_EXTENSIONS)}
    else:
        files, archive_findings = load_input(src, exclude=out_dir)
        files = strip_common_root(files)
        main_name = pick_main_game(list(files))
    conv.findings += archive_findings
    if not main_name:
        raise ConversionError("no game file (.qsp/.gam/.qsps) found")

    def qsp_name_of(name: str) -> str:
        return name if name.lower().endswith(GAME_EXTENSIONS) else name.rsplit(".", 1)[0] + ".qsp"

    resources: dict[str, bytes] = {}
    swf_fonts: list[tuple[str, list[tuple[str, int, bool, bool]]]] = []
    game_files: list[str] = []
    for name, data in files.items():
        lower = name.lower()
        base = lower.rsplit("/", 1)[-1]
        ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
        if lower.endswith(GAME_SOURCE_EXTENSIONS):
            twin = next((g for g in game_files if qsp_name_of(g).lower() == qsp_name_of(name).lower()), None)
            if twin is None:
                game_files.append(name)
                continue
            keep, drop = (
                (name, twin) if name == main_name or (twin != main_name and not twin.lower().endswith(GAME_EXTENSIONS)) else (twin, name)
            )
            game_files[game_files.index(twin)] = keep
            conv.findings.append(Finding("info", "archive", f"`{drop}` is another copy of `{keep}` and was not converted"))
        elif ext == ".swf":
            swf_fonts.append((name, swf_font_names(data)))
        elif base in JUNK_NAMES or ext in JUNK_EXTENSIONS or lower.startswith("__macosx/") or lower == "config.xml":
            conv.findings.append(Finding("info", "archive", f"`{name}` is not needed by qSpider and was not copied"))
        else:
            resources[name] = data
    conv.resources = ResourceIndex(list(resources))

    width, height, title = 800, 600, None
    config_name = next((n for n in files if n.lower() == "config.xml" or n.lower().endswith("/config.xml")), None)
    if config_name:
        try:
            root = ET.fromstring(decode_text(files[config_name]))
            width = int(root.get("width") or width)
            height = int(root.get("height") or height)
            title = root.get("title") or None
            conv.findings.append(
                Finding("info", "config", f'config.xml: screen {width}×{height}, title "{title or "-"}"; moved to game.cfg')
            )
        except (ET.ParseError, ValueError, UnicodeDecodeError) as error:
            conv.findings.append(Finding("warn", "config", f"cannot parse config.xml ({error}); using 800×600"))
    else:
        conv.findings.append(Finding("info", "config", "no config.xml: the default 800×600 screen is used"))
    title = args.title or title or (src.stem if src.is_file() and src.suffix.lower() in ARCHIVE_EXTENSIONS else Path(main_name).stem)
    result.title = title

    # every module is fixed before the logic analysis, which needs the variables of the whole game
    original: dict[str, list[Location]] = {}
    converted: dict[str, list[Location]] = {}
    qsps_names: dict[str, str] = {}
    for name in sorted(game_files, key=lambda n: n != main_name):
        locations = _read_game_file(name, files[name], args.source if name == main_name else None)
        qsp_name = qsp_name_of(name)
        qsps_names[qsp_name] = name.rsplit(".", 1)[0] + ".qsps"
        conv.current_file = qsps_names[qsp_name]
        original[qsp_name] = locations
        converted[qsp_name] = conv.convert_locations(locations)
    main_qsp = qsp_name_of(main_name)

    if not args.no_logic_check:
        analyzer = LogicAnalyzer()
        blocks = [block for qsp_name, locs in converted.items() for block in code_blocks(qsps_names[qsp_name], locs)]
        analyzer.collect(blocks)
        analyzer.analyze(blocks)
        conv.findings += analyzer.findings

    out_dir.mkdir(parents=True, exist_ok=True)
    for qsp_name, fixed in converted.items():
        qsps_text, starts = write_qsps(fixed)
        result.line_maps[qsps_names[qsp_name]] = starts
        (out_dir / qsp_name).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / qsps_names[qsp_name]).write_text(qsps_text, encoding="utf-8")
        (out_dir / qsp_name).write_bytes(write_qsp(fixed))
    result.main_qsp, result.main_qsps = str(out_dir / main_qsp), qsps_names[main_qsp]
    result.first_location = converted[main_qsp][0].name if converted[main_qsp] else ""
    if not conv.usehtml_on:
        conv.findings.append(
            Finding(
                "warn",
                "usehtml",
                "the game never sets USEHTML = 1: AeroQSP showed its texts as plain text, qSpider always parses HTML; "
                "check `<` and `&` in the texts",
                file=result.main_qsps,
            )
        )

    if args.compare:
        compare_dir = Path(tempfile.mkdtemp(prefix="aero2qspider-compare-"))
        write_instrumented(original, compare_dir / "57", "57")
        write_instrumented(converted, compare_dir / "59", "59")
        result.compare_dir = str(compare_dir)

    for name, data in resources.items():
        target = out_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    font_lines = []
    for swf_name, fonts in swf_fonts:
        desc = ", ".join(f'"{n}" ({g} glyphs{", bold" if b else ""}{", italic" if i else ""})' for n, g, b, i in fonts) or "unknown font"
        conv.findings.append(
            Finding(
                "warn",
                "fonts",
                f"`{swf_name}` embeds {desc}. qSpider cannot read SWF fonts: get the font as .woff2/.ttf (the original font "
                "or an export from the SWF, e.g. with JPEXS FFDec) and list it in `[game.resources] fonts` of game.cfg",
            )
        )
        for n, _glyphs, b, i in fonts:
            font_lines.append(f'#   ["{n}", "fonts/{slugify(n)}.woff2", "{"bold" if b else "normal"}", "{"italic" if i else "normal"}"],')
    if swf_fonts:
        conv.findings.append(
            Finding("info", "fonts", "AeroQSP used only embedded fonts when a game had any; without replacements the text font will differ")
        )
    for font, count in conv.fonts_used.most_common():
        conv.findings.append(
            Finding("info", "fonts", f'the game uses the font "{font}" ({count}×): players without it get another font; add a web font')
        )

    theme_note = "not created (--no-theme)"
    themes_cfg: list[str] = []
    if not args.no_theme:
        source, origin = load_aero_theme(args.aero_theme)
        (out_dir / f"{THEME_NAME}.html").write_text(build_theme(source, args.font_size), encoding="utf-8")
        (out_dir / f"{THEME_NAME}.css").write_text(COMPAT_CSS, encoding="utf-8")
        themes_cfg = [f'themes = ["{THEME_NAME}.html"]', f'defaultTheme = "{THEME_NAME}"']
        theme_note = f"{THEME_NAME}.html (from the {origin})"
        if not conv.fsize_set:
            conv.findings.append(
                Finding(
                    "info",
                    "fonts",
                    f"the game never sets FSIZE: AeroQSP defaulted to {AERO_DEFAULT_FONT_SIZE}px, qSpider to 12px; "
                    f"the compatibility theme uses {args.font_size}px",
                )
            )

    entry = main_qsp if args.entry == "qsp" else qsps_names[main_qsp]
    game_id = args.id or slugify(title if title != Path(main_name).stem else src.stem)
    cfg = ["[[game]]", f"id = {toml_str(game_id)}", f"title = {toml_str(title)}", 'mode = "aero"', f"file = {toml_str(entry)}", *themes_cfg]
    cfg += ["", "[game.aero]", f"width = {width}", f"height = {height}", ""]
    if font_lines:
        cfg += ["# Replace the SWF fonts with web fonts and uncomment:", "# [game.resources]", "# fonts = [", *font_lines, "# ]", ""]
    (out_dir / "game.cfg").write_text("\n".join(cfg), encoding="utf-8")

    result.findings = conv.findings
    result.summary = {
        "Source": src.name,
        "Main file": f"{main_name} → {entry}",
        "Title": title,
        "Screen": f"{width}×{height}",
        "Resources": len(resources),
        "Theme": theme_note,
        "QSP 5.9 run": "not run (--smoke)",
        "Paired 5.7/5.9 run": "not run (--compare)",
    }
    return result


def count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _smoke_findings(result: GameResult, args: argparse.Namespace, errors: list[dict[str, object]], problem: str | None) -> None:
    if problem:
        result.summary["QSP 5.9 run"] = f"not run: {problem}"
        result.findings.append(Finding("warn", "smoke", f"the run was skipped: {problem}"))
        return
    result.smoke_errors = len(errors)
    result.summary["QSP 5.9 run"] = f"{args.smoke_seeds} × {args.smoke_steps} random steps, {count(len(errors), 'error')}"
    for error in errors:
        part = "code" if int(error["actionIndex"]) < 0 else f"act:{error['actionIndex']}"  # type: ignore[call-overload]
        result.findings.append(
            Finding(
                "warn",
                "smoke",
                f"runtime error: {error['description']} in `{error['lineSrc']}` ({error['count']}×); "
                "it may be a bug of the original game, check whether AeroQSP shows it too",
                file=result.main_qsps,
                location=str(error["location"]),
                part=part,
                line=int(error["line"]),  # type: ignore[call-overload]
            )
        )


def _compare_findings(result: GameResult, args: argparse.Namespace, runs: list[CompareResult] | None, problem: str | None) -> None:
    if problem or runs is None:
        result.summary["Paired 5.7/5.9 run"] = f"not run: {problem}"
        result.findings.append(Finding("warn", "compare", f"the paired run was skipped: {problem}"))
        return
    steps = sum(r.steps for r in runs)
    locations = len(set().union(*(r.locations for r in runs)))
    diverged = [r for r in runs if r.divergence]
    result.divergences = len(diverged)
    result.summary["Paired 5.7/5.9 run"] = (
        f"{count(len(runs), 'run')}, {count(steps, 'step')}, {count(locations, 'location')}, {len(diverged)} diverged"
        + (f", {count(sum(r.errors for r in runs), 'error')} seen in both engines" if any(r.errors for r in runs) else "")
    )
    for run in runs:
        if run.problem:
            result.findings.append(Finding("warn", "compare", f"seed {run.seed}: the run stopped: {run.problem}"))
        if not (d := run.divergence):
            continue
        details = ["steps before the divergence:", *(f"  {line}" for line in d.trail), "", *d.differences]
        result.findings.append(
            Finding(
                "warn",
                "compare",
                f'seed {d.seed}, step {d.step}: after {d.command} the engines differ (QSP 5.7 at "{d.location57}", '
                f'QSP 5.9 at "{d.location59}")\n' + "\n".join(details),
                file=result.main_qsps,
                location=d.location59,
                part="code",
                line=1,
            )
        )
    if not diverged and not any(r.problem for r in runs):
        result.findings.append(
            Finding("info", "compare", f"the original on QSP 5.7 and the converted game on QSP 5.9 behaved the same for {steps} steps")
        )


async def _run_compare(result: GameResult, args: argparse.Namespace) -> tuple[list[CompareResult] | None, str | None]:
    if problem := qsp57_problem() or node_problem():
        return None, problem
    folder = Path(result.compare_dir)
    game = Path(result.main_qsp).name
    command57 = qsp57_command(folder / "57" / game)
    command59 = node_command("qsp59-driver.mjs", str(folder / "59" / game))
    runs = await asyncio.gather(
        *(paired_run(command57, command59, result.first_location, args.compare_steps, seed) for seed in range(1, args.compare_seeds + 1))
    )
    return list(runs), None


def finish_game(result: GameResult, args: argparse.Namespace) -> None:
    out_dir = Path(result.out_dir)
    (out_dir / "CONVERSION_REPORT.md").write_text(render_report(result.findings, result.summary, result.line_maps), encoding="utf-8")
    if args.zip:
        archive = out_dir.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out_dir.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(out_dir).as_posix())
        result.archive = str(archive)


async def process_game(args: argparse.Namespace, pool: Executor, engine_slots: asyncio.Semaphore) -> GameResult:
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    try:
        result = await loop.run_in_executor(pool, convert_game, args)
        try:
            async with engine_slots:
                if args.smoke:
                    _smoke_findings(result, args, *await run_smoke(Path(result.main_qsp), args.smoke_steps, args.smoke_seeds))
                if args.compare:
                    _compare_findings(result, args, *await _run_compare(result, args))
        finally:
            if result.compare_dir:
                shutil.rmtree(result.compare_dir, ignore_errors=True)
        await asyncio.to_thread(finish_game, result, args)
    except ConversionError as error:
        result = GameResult(source=str(args.input), out_dir=str(args.output), ok=False, error=str(error))
    except Exception as error:
        result = GameResult(source=str(args.input), out_dir=str(args.output), ok=False, error=f"{type(error).__name__}: {error}")
    result.seconds = time.perf_counter() - started
    return result


def make_executor(jobs: int, games: int) -> Executor:
    if jobs <= 1 or games <= 1:
        return ThreadPoolExecutor(max_workers=1)
    # "spawn" behaves the same on Linux, macOS and Windows and never forks the running event loop
    return ProcessPoolExecutor(max_workers=min(jobs, games), mp_context=multiprocessing.get_context("spawn"))


async def run_games(
    jobs: list[argparse.Namespace], workers: int, engine_workers: int, on_result: Callable[[int, int, GameResult], None]
) -> list[GameResult]:
    """Convert games concurrently and hand over every result as soon as it is ready."""
    engine_slots = asyncio.Semaphore(max(1, engine_workers))
    results: list[GameResult] = []
    with make_executor(workers, len(jobs)) as pool:
        tasks = [asyncio.create_task(process_game(job, pool, engine_slots)) for job in jobs]
        try:
            for done, future in enumerate(asyncio.as_completed(tasks), 1):
                result = await future
                results.append(result)
                on_result(done, len(jobs), result)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
    return results
