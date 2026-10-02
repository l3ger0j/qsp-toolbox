"""Lexical helpers for QSP code: strings, comments, call arguments and statements."""

import dataclasses
import re

from .constants import IDENT_CHARS


@dataclasses.dataclass
class Segment:
    kind: str  # "code", "str" (including quotes) or "comment"
    start: int
    end: int


def scan_code(text: str) -> list[Segment]:
    """Split QSP code into code, string literal and comment segments.

    `{...}` blocks are code (they are used as dynamic code), quotes are escaped by doubling,
    `!` starts a comment only at the beginning of a statement (line start or after `&`).
    """
    segments: list[Segment] = []
    n = len(text)
    i = 0
    code_start = 0
    statement_start = True

    def close_code(upto: int) -> None:
        if upto > code_start:
            segments.append(Segment("code", code_start, upto))

    def skip_string(pos: int) -> int:
        quote = text[pos]
        pos += 1
        while pos < n:
            if text[pos] == quote:
                if pos + 1 < n and text[pos + 1] == quote:
                    pos += 2
                    continue
                return pos + 1
            pos += 1
        return n

    while i < n:
        ch = text[i]
        if ch in "'\"":
            close_code(i)
            end = skip_string(i)
            segments.append(Segment("str", i, end))
            i = code_start = end
            statement_start = False
            continue
        if ch == "!" and statement_start:
            close_code(i)
            j = i + 1
            while j < n and text[j] != "\n":
                if text[j] in "'\"":
                    j = skip_string(j)
                else:
                    j += 1
            segments.append(Segment("comment", i, j))
            i = code_start = j
            continue
        if ch == "\n" or ch == "&":
            statement_start = True
        elif ch not in " \t\r":
            statement_start = False
        i += 1
    close_code(n)
    return segments


def segment_at(segments: list[Segment], pos: int) -> Segment | None:
    lo, hi = 0, len(segments) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        seg = segments[mid]
        if pos < seg.start:
            hi = mid - 1
        elif pos >= seg.end:
            lo = mid + 1
        else:
            return seg
    return None


def parse_call_args(text: str, open_paren: int) -> tuple[list[tuple[int, int]], int] | None:
    """Parse arguments of a call starting at `open_paren`. Returns arg spans and the closing paren index."""
    depth = 0
    args: list[tuple[int, int]] = []
    arg_start = open_paren + 1
    i = open_paren
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2
                        continue
                    break
                i += 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append((arg_start, i))
                return args, i
        elif ch == "," and depth == 1:
            args.append((arg_start, i))
            arg_start = i + 1
        elif ch == "\n" and depth <= 1:
            return None
        i += 1
    return None


NUMERIC_FUNCS = {
    "len",
    "rand",
    "rnd",
    "val",
    "instr",
    "arrsize",
    "arrpos",
    "arrcomp",
    "max",
    "min",
    "iif",
    "isnum",
    "countobj",
    "msecscount",
    "qspver",
    "strcomp",
    "strpos",
    "selobj",
    "selact",
    "getobj",
    "isplay",
    "result",
    "args",
    "rgb",
    "obj",
    "loc",
    "no",
    "mod",
}


def arg_kind(arg: str) -> str:
    a = arg.strip()
    if not a:
        return "empty"
    if a[0] in "'\"{" or a.startswith("$"):
        return "str"
    if re.match(r"^-?\d", a):
        return "num"
    if "'" in a or '"' in a or "$" in a:
        return "unknown"
    head = re.match(rf"^({IDENT_CHARS}+)", a)
    if head:
        name = head.group(1).lower()
        if re.fullmatch(rf"{IDENT_CHARS}+(\[[^\]]*\])?", a):
            return "num"  # numeric variable or array item
        if name in NUMERIC_FUNCS or re.fullmatch(rf"[{IDENT_CHARS[1:-1]}\s+\-*/()\[\]0-9.,]+", a):
            return "num"
    return "unknown"


def find_statement_end(text: str, pos: int) -> int:
    """Index where the statement that contains `pos` ends (top-level `&` or newline)."""
    depth = 0
    n = len(text)
    i = pos
    while i < n:
        ch = text[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2
                        continue
                    break
                i += 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in "&\n":
            return i
        i += 1
    return n


def mask_code(text: str) -> str:
    """Replace string literals and comments with spaces (keeping offsets and line breaks)."""
    chars = list(text)
    for seg in scan_code(text):
        if seg.kind != "code":
            for i in range(seg.start, seg.end):
                if chars[i] != "\n":
                    chars[i] = " "
    return "".join(chars)


def top_level_has(pattern: re.Pattern[str], expr: str) -> bool:
    """True if `pattern` matches `expr` outside of any brackets."""
    depth = 0
    flat = []
    for ch in expr:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        flat.append(ch if depth == 0 else " ")
    return bool(pattern.search("".join(flat)))


SUBEX_RE = re.compile(r"<<(.*?)>>", re.DOTALL)
EXEC_LINK_RE = re.compile(r"""href\s*=\s*(?:"exec:([^"]*)"|'exec:([^']*)')""", re.IGNORECASE)


def subexpressions(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Spans of code inside a string or text: `<<...>>` and the code of `<a href="exec:...">` links."""
    chunk = text[start:end]
    spans = [(start + m.start(1), start + m.end(1)) for m in SUBEX_RE.finditer(chunk)]
    for m in EXEC_LINK_RE.finditer(chunk):
        group = 1 if m.group(1) is not None else 2
        spans.append((start + m.start(group), start + m.end(group)))
    return spans
