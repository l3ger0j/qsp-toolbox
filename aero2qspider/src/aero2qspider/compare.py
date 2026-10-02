"""Paired run: the original game on libqsp 5.7 and the converted game on QSP 5.9, step by step.

Both engines receive the same actions, object clicks, link clicks and timer ticks. After every step the
visible state (texts, actions, objects, messages, menus, errors) is compared; the first mismatch is
reported with the steps that led to it. RAND/RND/MSECSCOUNT are replaced with deterministic
equivalents so that random events match in both engines.
"""

import asyncio
import dataclasses
import difflib
import json
import random
import re
from pathlib import Path

from .constants import AERO_NAMED_COLORS
from .findings import Edit, apply_edits, dedupe_edits
from .qsp_format import Action, Location, write_qsp
from .scanner import parse_call_args, scan_code

RAND_LOCATION = "__a2q_rand"
NOW_VARIABLE = "__a2q_now"
RAND_CODE = [
    "if __a2q_seed <= 0: __a2q_seed = 4242",
    "__a2q_seed = (__a2q_seed * 171) mod 30269",
    "__a2q_lo = args[0]",
    "__a2q_hi = args[1]",
    "if __a2q_lo > __a2q_hi: __a2q_t = __a2q_lo & __a2q_lo = __a2q_hi & __a2q_hi = __a2q_t",
    "result = __a2q_lo + __a2q_seed mod (__a2q_hi - __a2q_lo + 1)",
]
EXEC_HREF_RE = re.compile(r"""(href\s*=\s*)(?:"exec:[^"]*"|'exec:[^']*')""", re.IGNORECASE)
LINK_RE = re.compile(r"<a\s[^>]*href\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE)


def _instrument_calls(text: str, start: int, end: int, quote: str, engine: str, limit: int | None = None) -> list[Edit]:
    """Calls that start between `start` and `end` and close before `limit` (default: `end`)."""
    limit = end if limit is None else limit
    edits: list[Edit] = []
    name = f"{quote}{RAND_LOCATION}{quote}"
    for m in re.finditer(r"(?<![\w$.])rand\s*\(", text[start:end], re.IGNORECASE):
        open_paren = start + m.end() - 1
        parsed = parse_call_args(text, open_paren)
        if not parsed or parsed[1] >= limit or len(parsed[0]) not in (1, 2):
            continue
        # RAND converts string arguments to numbers; FUNC would pass them in $ARGS
        args = [arg if re.fullmatch(r"-?\d+", arg) else f"({arg})*1" for arg in (text[s:e].strip() for s, e in parsed[0])]
        low, high = (("0" if engine == "57" else "1"), args[0]) if len(args) == 1 else args
        edits.append(Edit(start + m.start(), parsed[1] + 1, f"func({name}, {low}, {high})"))
    for m in re.finditer(r"(?<![\w$.])rnd(?![\w.])", text[start:end], re.IGNORECASE):
        edits.append(Edit(start + m.start(), start + m.end(), f"func({name}, 1, 1000)"))
    for m in re.finditer(r"(?<![\w$.])msecscount(?![\w.])", text[start:end], re.IGNORECASE):
        edits.append(Edit(start + m.start(), start + m.end(), NOW_VARIABLE))
    return edits


def instrument_code(text: str, engine: str) -> str:
    """Make RAND, RND and MSECSCOUNT deterministic; RAND(n) keeps the range of the given engine."""
    for _ in range(8):  # nested calls: rand(1, rand(2, 5))
        edits: list[Edit] = []
        for seg in scan_code(text):
            if seg.kind == "code":
                edits += _instrument_calls(text, seg.start, seg.end, "'", engine, limit=len(text))
            elif seg.kind == "str":
                quote = '"' if text[seg.start] == "'" else "'"
                for m in re.finditer(r"<<(.*?)>>", text[seg.start : seg.end], re.DOTALL):
                    edits += _instrument_calls(text, seg.start + m.start(1), seg.start + m.end(1), quote, engine)
        if not edits:
            break
        text = apply_edits(text, dedupe_edits(edits))
    return text


def instrument(locations: list[Location], engine: str) -> list[Location]:
    def code(lines: list[str]) -> list[str]:
        return instrument_code("\n".join(lines), engine).split("\n") if lines else []

    def text(lines: list[str]) -> list[str]:
        result = []
        for line in lines:
            edits = [e for m in re.finditer(r"<<(.*?)>>", line) for e in _instrument_calls(line, m.start(1), m.end(1), '"', engine)]
            result.append(apply_edits(line, dedupe_edits(edits)))
        return result

    result = [
        Location(
            loc.name,
            text(loc.description),
            code(loc.code),
            [Action(act.name, act.image, code(act.code)) for act in loc.actions],
        )
        for loc in locations
    ]
    return [*result, Location(RAND_LOCATION, [], RAND_CODE, [])]


def canonical(text: str) -> str:
    """Hide differences that conversion introduces on purpose: path separators, letter case, Aero colour names, link code.

    The code of `exec:` links is fixed like any other code; its effect is compared when the link is clicked.
    """
    text = EXEC_HREF_RE.sub(r"\1exec:", text)
    text = text.replace("\\", "/").replace("\r\n", "\n").lower()
    return re.sub(
        r"(color\s*=\s*['\"]?)([a-z][a-z ]*?)(?=['\"\s>])",
        lambda m: m.group(1) + AERO_NAMED_COLORS.get(m.group(2), m.group(2)).lower(),
        text,
    )


def canonical_state(state: dict[str, object]) -> dict[str, object]:
    events = [(kind, canonical(str(value))) for kind, value in state.get("events", []) if kind != "system"]  # type: ignore[union-attr]
    return {
        "main": canonical(str(state.get("main", ""))),
        "stats": canonical(str(state.get("stats", ""))),
        "actions": [canonical(str(a)) for a in state.get("actions", [])],  # type: ignore[union-attr]
        "objects": [canonical(str(o)) for o in state.get("objects", [])],  # type: ignore[union-attr]
        "location": str(state.get("location", "")).lower(),
        "events": events,
    }


def error_key(state: dict[str, object]) -> tuple[str, str] | None:
    """Errors match by location and message: QSP 5.7 and 5.9 count the lines of multi-line statements differently."""
    error = state.get("error")
    if not isinstance(error, dict):
        return None
    return str(error.get("location", "")).lower(), str(error.get("description", "")).lower()


@dataclasses.dataclass
class Divergence:
    seed: int
    step: int
    command: str
    location57: str
    location59: str
    trail: list[str]
    differences: list[str]


@dataclasses.dataclass
class CompareResult:
    seed: int
    steps: int = 0
    locations: set[str] = dataclasses.field(default_factory=set)
    errors: int = 0
    divergence: Divergence | None = None
    problem: str = ""


class Driver:
    def __init__(self, command: list[str], name: str):
        self.command = command
        self.name = name
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 26
        )
        await self._read()

    async def _read(self) -> dict[str, object]:
        assert self.proc and self.proc.stdout
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=60)
        if not line:
            stderr = (await self.proc.stderr.read()).decode(errors="replace") if self.proc.stderr else ""
            raise RuntimeError(f"{self.name} stopped: {stderr.strip()[:1500]}")
        return json.loads(line)

    async def send(self, command: str, quiet: bool = False) -> dict[str, object] | None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(command.encode() + b"\n")
        await self.proc.stdin.drain()
        return None if quiet else await self._read()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                await self.send("quit", quiet=True)
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except (TimeoutError, ConnectionError, BrokenPipeError):
                self.proc.kill()


def hex_code(code: str) -> str:
    return code.encode("utf-8").hex()


def _choices(state: dict[str, object]) -> list[tuple[str, str]]:
    """Possible player moves as (label, driver command)."""
    moves = [(f'action "{a}"', f"act {i}") for i, a in enumerate(state.get("actions", []))]  # type: ignore[arg-type]
    moves += [(f'object "{o}"', f"obj {i}") for i, o in enumerate(state.get("objects", []))]  # type: ignore[arg-type]
    actions = state.get("actions", [])
    for text in (str(state.get("main", "")), str(state.get("stats", ""))):
        for m in LINK_RE.finditer(text):
            href = next(g for g in m.groups() if g is not None)
            if href.lower().startswith("exec:"):
                code = href[5:].replace("&quot;", '"')
                moves.append((f"link `{code.strip()[:60]}`", f"exec {hex_code(code)}"))
            elif href.isdigit() and 0 < int(href) <= len(actions):  # type: ignore[arg-type]
                moves.append((f"link to action {href}", f"act {int(href) - 1}"))
    return moves


def _differences(a: dict[str, object], b: dict[str, object], raw57: dict[str, object], raw59: dict[str, object]) -> list[str]:
    result = []
    for field in ("location", "main", "stats", "actions", "objects", "events"):
        if a[field] == b[field]:
            continue
        if field in ("main", "stats"):
            diff = difflib.unified_diff(
                str(raw57.get(field, "")).replace("\r\n", "\n").split("\n"),
                str(raw59.get(field, "")).replace("\r\n", "\n").split("\n"),
                "QSP 5.7",
                "QSP 5.9",
                n=1,
                lineterm="",
            )
            lines = [line for line in list(diff)[2:] if not line.startswith("@@")][:12]
            result.append(f"{field} text differs:\n" + "\n".join(f"    {line}" for line in lines))
        else:
            result.append(
                f"{field}: QSP 5.7 {json.dumps(raw57.get(field), ensure_ascii=False)[:300]} / QSP 5.9 {json.dumps(raw59.get(field), ensure_ascii=False)[:300]}"
            )
    return result


async def paired_run(command57: list[str], command59: list[str], first_location: str, steps: int, seed: int) -> CompareResult:
    result = CompareResult(seed=seed)
    rng = random.Random(seed)
    d57, d59 = Driver(command57, "QSP 5.7 driver"), Driver(command59, "QSP 5.9 driver")
    trail: list[str] = []

    def restart() -> str:
        game_seed = rng.randint(1, 30268)
        return f"exec {hex_code(f'KILLALL & CLS & __a2q_seed = {game_seed} & GT {repr_qsp(first_location)}')}"

    try:
        await asyncio.gather(d57.start(), d59.start())

        async def step(label: str, cmd57: str, cmd59: str) -> bool:
            now = f"quiet {hex_code(f'{NOW_VARIABLE} = {result.steps * 250}')}"
            await asyncio.gather(d57.send(now, quiet=True), d59.send(now, quiet=True))
            s57, s59 = await asyncio.gather(d57.send(cmd57), d59.send(cmd59))
            assert s57 is not None and s59 is not None
            result.steps += 1
            trail.append(f"{label} → {s57.get('location', '')}")
            del trail[:-12]
            e57, e59 = error_key(s57), error_key(s59)
            if e57 or e59:
                if e57 != e59:
                    result.divergence = Divergence(
                        seed,
                        result.steps,
                        label,
                        str(s57.get("location")),
                        str(s59.get("location")),
                        list(trail),
                        [f"error: QSP 5.7 {s57.get('error')} / QSP 5.9 {s59.get('error')}"],
                    )
                    return False
                result.errors += 1
                command = restart()
                return await step("restart after the same error in both engines", command, command)
            c57, c59 = canonical_state(s57), canonical_state(s59)
            if c57 != c59:
                result.divergence = Divergence(
                    seed,
                    result.steps,
                    label,
                    str(s57.get("location")),
                    str(s59.get("location")),
                    list(trail),
                    _differences(c57, c59, s57, s59),
                )
                return False
            result.locations.add(str(s57.get("location", "")))
            state57.clear()
            state57.update(s57)
            state59.clear()
            state59.update(s59)
            return True

        state57: dict[str, object] = {}
        state59: dict[str, object] = {}
        command = restart()
        if not await step("start", command, command):
            return result
        idle = 0
        while result.steps < steps:
            moves57, moves59 = _choices(state57), _choices(state59)
            if not moves57 or rng.random() < 0.15:
                idle = idle + 1 if not moves57 else 0
                if idle > 30:
                    idle = 0
                    command = restart()
                    ok = await step("restart", command, command)
                else:
                    ok = await step("timer tick", "tick", "tick")
            else:
                index = rng.randrange(len(moves57))
                ok = await step(moves57[index][0], moves57[index][1], moves59[index][1] if index < len(moves59) else "tick")
            if not ok:
                break
    except (RuntimeError, TimeoutError, json.JSONDecodeError) as error:
        result.problem = str(error) or type(error).__name__
    finally:
        await asyncio.gather(d57.close(), d59.close())
    return result


def repr_qsp(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_instrumented(locations_by_file: dict[str, list[Location]], folder: Path, engine: str) -> None:
    for name, locations in locations_by_file.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(write_qsp(instrument(locations, engine)))
