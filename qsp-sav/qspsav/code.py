"""Handling QSP code stored in saves (action code) and scanning game code."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tables import upper_570, upper_59

QUOTES = "'\""
# QSP_DELIMS of both engines: characters that end a name.
DELIMS = " \t&'\"()[]=!<>+-/*:,{}\r\n"


def _skip_quoted(line: str, pos: int) -> int:
    """qspSkipQuotedString(): past the closing quote; a doubled quote is an escape."""
    quote = line[pos]
    pos += 1
    while pos < len(line):
        if line[pos] == quote:
            pos += 1
            if pos >= len(line) or line[pos] != quote:
                return pos
        pos += 1
    return pos


def prepare_line_59(line: str) -> str:
    """qspPrepareStringToExecution() of 5.9: upper-case everything outside
    quoted strings and {code blocks}.

    5.9 does this when it loads game code, and a save holds action code in
    that prepared form: qspOpenGameStatus() passes it straight to
    qspInitLineOfCode(). 5.7.0 stores action code as written, so it must be
    prepared when converting, or statements and variable names written in
    lower case are not recognised.
    """
    out = []
    pos = 0
    brackets = 0
    while pos < len(line):
        ch = line[pos]
        if ch in QUOTES:
            end = _skip_quoted(line, pos)
            out.append(line[pos:end])
            pos = end
            continue
        if ch == "{":
            brackets += 1
        elif ch == "}":
            brackets = max(brackets - 1, 0)
        elif not brackets:
            ch = upper_59(ch)
        out.append(ch)
        pos += 1
    return "".join(out)


def strip_strings(line: str) -> str:
    """The line with quoted strings replaced by spaces (for scanning)."""
    out = []
    pos = 0
    while pos < len(line):
        if line[pos] in QUOTES:
            end = _skip_quoted(line, pos)
            out.append(" " * (end - pos))
            pos = end
        else:
            out.append(line[pos])
            pos += 1
    return "".join(out)


# Statements, functions and syntax that exist in 5.9 but not in 5.7.0.
ONLY_59_WORDS = {
    "LOOP", "WHILE", "STEP", "LOCAL", "SETVAR", "UNPACKARR", "SORTARR", "SCANSTR", "MODOBJ", "RESETOBJ",
    "INCLIB", "FREELIB", "ARRTYPE", "ARRITEM", "ARRPACK", "CUROBJS",
}
_WORD = re.compile(r"[^\s&'\"()\[\]=!<>+\-/*:,{}]+")
# A quoted string as qspSkipQuotedString() sees it: a doubled quote is an
# escape, an unterminated string runs to the end of the code.
_QUOTED = re.compile(r"'(?:[^']|'')*(?:'|\Z)|\"(?:[^\"]|\"\")*(?:\"|\Z)")


def find_59_only_syntax(line: str) -> list[str]:
    """Constructs in a 5.9 code line that qsp-legacy does not understand."""
    found = []
    for word in _WORD.findall(strip_strings(line)):
        bare = word.lstrip("$#%").upper()
        if bare in ONLY_59_WORDS:
            found.append(bare)
        elif word.startswith("%"):
            found.append(f"{word} (tuple variable)")
        elif word.startswith("@"):
            found.append(f"{word} (function call shorthand)")
    return found


def subexpressions(text: str) -> list[str]:
    """The <<...>> parts of a text (they are evaluated as code)."""
    parts = []
    pos = 0
    while (start := text.find("<<", pos)) >= 0:
        end = text.find(">>", start + 2)
        if end < 0:
            break
        parts.append(text[start + 2:end])
        pos = end + 2
    return parts


@dataclass
class VarUsage:
    """How a game refers to its variables: with '$' (string form) or without."""

    string_names: set[str] = field(default_factory=set)
    number_names: set[str] = field(default_factory=set)

    def add_code(self, code: str) -> None:
        # Keywords and function names end up in the sets too, which is
        # harmless: the sets are only consulted for names found in a save.
        plain = _QUOTED.sub(self._add_quoted, code)
        for word in set(_WORD.findall(plain)):
            if word[0].isdigit() or word[0] in "#%@*":
                continue
            if word.startswith("$"):
                if len(word) > 1:
                    self.string_names.add(upper_570(word[1:]))
            else:
                self.number_names.add(upper_570(word))

    def _add_quoted(self, match: re.Match) -> str:
        """A quoted string: only its <<...>> parts are code."""
        for sub in subexpressions(match.group()[1:-1]):
            self.add_code(sub)
        return " "

    def add_text(self, text: str) -> None:
        """Location/action descriptions: only <<...>> parts are code."""
        for sub in subexpressions(text):
            self.add_code(sub)

    def kind(self, name: str) -> str | None:
        """'str', 'num', 'both' or None (not seen in the code)."""
        s, n = name in self.string_names, name in self.number_names
        return "both" if s and n else "str" if s else "num" if n else None


def scan_game_usage(games) -> VarUsage:
    usage = VarUsage()
    for game in games:
        for loc in game.locations:
            usage.add_text(loc.desc)
            usage.add_code(loc.code)
            for act in loc.actions:
                usage.add_text(act.desc)
                usage.add_code(act.code)
    return usage
