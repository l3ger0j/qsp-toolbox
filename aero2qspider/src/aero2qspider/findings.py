"""Findings reported to the user and text edits applied to the game code."""

import dataclasses


@dataclasses.dataclass
class Finding:
    severity: str  # "fix", "warn" or "info"
    rule: str
    message: str
    file: str = ""
    location: str = ""
    part: str = ""
    line: int = 0
    before: str = ""
    after: str = ""


RULE_TITLES = {
    "keywords": "Removed QSP 5.7 statements (ADDQST/KILLQST)",
    "arg-order": "INSTR/ARRPOS/ARRCOMP argument order",
    "disablesubex": "DISABLESUBEX does not exist in QSP 5.9",
    "stat-format": "$STAT_FORMAT (qSpider reads $STATS_FORMAT)",
    "effects": "Aero effects",
    "exec-effect": "EXEC 'effect:...' (ignored by qSpider)",
    "paths": "Resource paths (\\ to /, letter case)",
    "missing-files": "References to missing files",
    "colors": "Named colours in <font color> (Aero palette differs from CSS)",
    "css-units": "CSS values without units",
    "css-leading": "Flash-only CSS property `leading`",
    "css-global": "Global selectors in $STYLESHEET",
    "center": "<center> inside tables and list items",
    "usehtml": "USEHTML",
    "unsupported-vars": "Aero variables qSpider ignores",
    "fonts": "Fonts",
    "config": "config.xml",
    "archive": "Archive and files",
    "smoke": "Run on the QSP 5.9.5 engine",
    "rand": "Single-argument RAND (5.7: 0..n, 5.9: 1..n)",
    "logic-true": "Value of true: -1 in QSP 5.7, 1 in QSP 5.9",
    "logic-bitwise": "NO/AND: bitwise in QSP 5.7, logical in QSP 5.9",
    "logic-same-name": "Variables `x` and `$x` (one cell in QSP 5.9)",
    "logic-precedence": "OBJ/LOC precedence against comparisons",
    "logic-isnum": "ISNUM('') (true in 5.7, false in 5.9)",
    "unary-plus": "Unary plus (a syntax error in QSP 5.9)",
    "bare-calls": "FUNC/DYNEVAL used as a statement (empty line in 5.7 only)",
    "compare": "Paired run on QSP 5.7 and QSP 5.9",
}


@dataclasses.dataclass
class Edit:
    start: int
    end: int
    text: str


def apply_edits(text: str, edits: list[Edit]) -> str:
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        text = text[: edit.start] + edit.text + text[edit.end :]
    return text


def dedupe_edits(edits: list[Edit]) -> list[Edit]:
    """Drop edits that overlap an earlier one."""
    result: list[Edit] = []
    last_end = -1
    for edit in sorted(edits, key=lambda e: (e.start, e.end)):
        if edit.start < last_end:
            continue
        result.append(edit)
        if edit.end > edit.start:
            last_end = max(last_end, edit.end)
    return result


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1
