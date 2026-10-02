"""Static checks for code that behaves differently in QSP 5.7 (AeroQSP) and QSP 5.9 (qSpider).

Every rule was confirmed by running the same code on libqsp 5.7 (the January 2011 revision
AeroQSP was built with) and on @qsp/wasm-engine 5.9.5:

* comparisons, OBJ, LOC and ISNUM return -1 for true in 5.7 and 1 in 5.9;
* NO and AND are bitwise in 5.7 (no 1 = -2, 2 and 1 = 0) and logical in 5.9;
* `x` and `$x` are two independent values in 5.7 and one value in 5.9;
* OBJ/LOC bind weaker than comparisons in 5.7 (`obj 'a' = 0` is `obj ('a' = 0)`) and stronger in 5.9;
* ISNUM('') is true in 5.7 and false in 5.9.
"""

import dataclasses
import re
from collections import defaultdict
from collections.abc import Iterator

from .findings import Finding, line_of
from .qsp_format import Location
from .scanner import find_statement_end, mask_code, scan_code, top_level_has

COMPARISON_RE = re.compile(r"<>|<=|>=|=<|=>|[<>=!]")
BOOL_FUNC_RE = re.compile(r"^\s*(?:no|obj|loc|isnum|isplay)\b", re.IGNORECASE)
IDENT_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_.]*")
ASSIGN_RE = re.compile(
    r"(?:^|[&:]|\belse(?=[ \t]))[ \t]*(?:(?:set|let|local)[ \t]+)?(\$?)([A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_.]*)"
    r"[ \t]*(\[[^\]\n]*\])?[ \t]*([-+*/]?=)(?![=<>])",
    re.IGNORECASE | re.MULTILINE,
)
KEYWORDS = {
    "if",
    "elseif",
    "else",
    "end",
    "act",
    "and",
    "or",
    "no",
    "obj",
    "loc",
    "mod",
    "set",
    "let",
    "local",
    "gt",
    "goto",
    "gs",
    "gosub",
    "xgt",
    "xgoto",
    "jump",
    "exit",
    "loop",
    "while",
    "step",
    "then",
}
SAME_NAME_IGNORED = {"result", "args"}
ELSE_RE = re.compile(r"(?<![\w$.])else(?:if)?(?![\w.])", re.IGNORECASE)


def index_key(index: str) -> str:
    """Normalise an array index: plain variable and [0] are the same cell; computed indices are '*'."""
    inner = index.strip()[1:-1].strip() if index.strip() else "0"
    if not inner or re.fullmatch(r"-?\d+", inner):
        return str(int(inner or 0))
    if re.fullmatch(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", inner):
        return "s:" + inner[1:-1].lower()
    return "*"


@dataclasses.dataclass
class VarInfo:
    # index key ("0" for a plain variable, "*" for a computed index) -> first site (file, location, part, line)
    num_sites: dict[str, tuple[str, str, str, int]] = dataclasses.field(default_factory=dict)
    str_sites: dict[str, tuple[str, str, str, int]] = dataclasses.field(default_factory=dict)
    literals: set[int] = dataclasses.field(default_factory=set)
    arithmetic: bool = False
    boolean: bool = False

    @property
    def non_boolean(self) -> bool:
        """Has values other than 0/-1, so bitwise NO/AND give different results."""
        return self.arithmetic or bool(self.literals - {0, -1})

    @property
    def only_boolean(self) -> bool:
        return self.boolean and not self.non_boolean


CodeBlock = tuple[str, str, str, str]  # file, location, part, code


def code_blocks(file: str, locations: list[Location]) -> Iterator[CodeBlock]:
    for loc in locations:
        if loc.code:
            yield file, loc.name, "code", "\n".join(loc.code)
        for i, act in enumerate(loc.actions):
            if act.code:
                yield file, loc.name, f"act:{i}", "\n".join(act.code)


class LogicAnalyzer:
    """Static heuristics for code whose behaviour differs between QSP 5.7 and QSP 5.9."""

    def __init__(self) -> None:
        self.vars: dict[str, VarInfo] = defaultdict(VarInfo)
        self.findings: list[Finding] = []

    def _finding(self, severity: str, rule: str, message: str, block: CodeBlock, text: str, pos: int) -> None:
        file, location, part, _ = block
        self.findings.append(Finding(severity, rule, message, file=file, location=location, part=part, line=line_of(text, pos)))

    def collect(self, blocks: list[CodeBlock]) -> None:
        for file, location, part, text in blocks:
            masked = mask_code(text)
            for m in ASSIGN_RE.finditer(masked):
                name = m.group(2).lower()
                if name in KEYWORDS:
                    continue
                info = self.vars[name]
                site = (file, location, part, line_of(text, m.start(2)))
                key = index_key(text[m.start(3) : m.end(3)] if m.group(3) else "")
                if m.group(1):
                    info.str_sites.setdefault(key, site)
                    continue
                info.num_sites.setdefault(key, site)
                end = find_statement_end(text, m.end())
                cut = ELSE_RE.search(masked, m.end(), end)
                end = cut.start() if cut else end
                rhs_masked = masked[m.end() : end].strip()
                rhs = text[m.end() : end].strip()
                is_arith = m.group(4) != "=" or top_level_has(re.compile(r"[-+*/]|\bmod\b", re.IGNORECASE), rhs_masked.lstrip("-"))
                is_bool = (
                    top_level_has(COMPARISON_RE, rhs_masked)
                    or bool(BOOL_FUNC_RE.match(rhs_masked))
                    or bool(re.search(r"\b(and|or)\b", rhs_masked, re.I))
                )
                if re.fullmatch(r"-?\d+", rhs):
                    info.literals.add(int(rhs))
                elif is_arith:
                    info.arithmetic = True
                elif is_bool:
                    info.boolean = True

    def analyze(self, blocks: list[CodeBlock]) -> None:
        for block in blocks:
            text = block[3]
            masked = mask_code(text)
            self._check_no(block, text, masked)
            self._check_and(block, text, masked)
            self._check_true_compare(block, text, masked)
            self._check_bool_arithmetic(block, text, masked)
            self._check_precedence(block, text, masked)
            self._check_isnum(block, text, masked)
            self._check_chained(block, text, masked)
            self._check_bool_output(block, text, masked)
        self._check_same_name()

    def _operand_after(self, masked: str, pos: int) -> str:
        depth = 0
        i = pos
        while i < len(masked):
            ch = masked[i]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and (ch in "&:,\n" or (re.match(r"(?i)(and|or|then)\b", masked[i:]) and not IDENT_RE.match(masked[i - 1 : i]))):
                break
            i += 1
        return masked[pos:i]

    def _is_numeric_operand(self, operand: str) -> str | None:
        """Describe a bare operand whose bitwise and logical meaning differ, or None."""
        operand = operand.strip()
        if re.fullmatch(r"-?\d+", operand):
            return operand if int(operand) not in (0, -1) else None
        m = re.fullmatch(r"(" + IDENT_RE.pattern + r")(\[[^\]]*\])?", operand)
        if m and m.group(1).lower() not in KEYWORDS:
            info = self.vars.get(m.group(1).lower())
            if info and info.non_boolean and not info.boolean:
                return operand
        return None

    def _check_no(self, block: CodeBlock, text: str, masked: str) -> None:
        for m in re.finditer(r"(?<![\w$.])no(?![\w.])", masked, re.IGNORECASE):
            operand = self._operand_after(masked, m.end())
            if COMPARISON_RE.search(operand) or BOOL_FUNC_RE.match(operand):
                continue
            name = self._is_numeric_operand(operand)
            if name:
                self._finding(
                    "warn",
                    "logic-bitwise",
                    f"`no {name}`: NO was bitwise in AeroQSP (`no 1` = -2, true) and is logical in qSpider (`no 1` = 0). "
                    f"If `{name}` can hold 1 or any value other than 0/-1, the branch changes; `{name} = 0` works the same in both",
                    block,
                    text,
                    m.start(),
                )

    def _check_and(self, block: CodeBlock, text: str, masked: str) -> None:
        operand_re = r"(-?\d+|" + IDENT_RE.pattern + r"(?:\[[^\]]*\])?)"
        for m in re.finditer(r"(?<![\w$.])and(?![\w.])", masked, re.IGNORECASE):
            left_m = re.search(operand_re + r"\s*$", masked[: m.start()])
            right_m = re.match(r"\s*" + operand_re, masked[m.end() :])
            if not left_m or not right_m:
                continue
            before = masked[: left_m.start(1)].rstrip()
            after = masked[m.end() + right_m.end() :].lstrip()
            if (before and (COMPARISON_RE.match(before[-1]) or before[-1] in "+-*/$")) or (
                after and (COMPARISON_RE.match(after[0]) or after[0] in "+-*/[(")
            ):
                continue  # an operand is a comparison or arithmetic: the boolean result is the same in both versions
            left, right = left_m.group(1), right_m.group(1)
            if not (self._is_numeric_operand(left) and self._is_numeric_operand(right)):
                continue
            if (
                re.fullmatch(r"-?\d+", left)
                and re.fullmatch(r"-?\d+", right)
                and bool(int(left) & int(right)) == bool(int(left) and int(right))
            ):
                continue
            self._finding(
                "warn",
                "logic-bitwise",
                f"`{left} and {right}`: AND was bitwise in AeroQSP (`2 and 1` = 0, false) and is logical in qSpider (true). "
                f"With values other than 0/1 the condition changes; `{left} <> 0 and {right} <> 0` works the same in both",
                block,
                text,
                m.start(),
            )

    def _left_expression(self, masked: str, pos: int) -> str:
        depth = 0
        i = pos
        while i > 0:
            ch = masked[i - 1]
            if ch in ")]":
                depth += 1
            elif ch in "([":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and ch in "&:,\n":
                break
            i -= 1
        expr = re.split(r"(?i)\b(?:if|elseif|else|and|or)\b", masked[i:pos])[-1]
        expr = re.sub(r"(?i)^\s*(?:\*?pl|\*?p|\*?nl|msg)\b", "", expr)
        return re.sub(r"(?i)^\s*no\b", "", expr)  # `no x = -1` is `no (x = -1)`

    def _is_boolean_expression(self, expr: str) -> bool:
        expr = expr.strip()
        if not expr:
            return False
        if BOOL_FUNC_RE.match(expr):
            return True
        if expr.startswith("(") and expr.endswith(")"):
            inner = expr[1:-1]
            return bool(
                top_level_has(COMPARISON_RE, inner) or re.search(r"(?i)\b(and|or|no)\b", inner) or self._is_boolean_expression(inner)
            )
        m = re.fullmatch(r"(" + IDENT_RE.pattern + r")(\[[^\]]*\])?", expr)
        if m:
            info = self.vars.get(m.group(1).lower())
            return bool(info and info.only_boolean)
        return False

    def _check_true_compare(self, block: CodeBlock, text: str, masked: str) -> None:
        for m in re.finditer(r"(?<![<>=!])(=|<>|!)\s*-\s*1(?![\d.])", masked):
            left = self._left_expression(masked, m.start())
            if self._is_boolean_expression(left):
                self._finding(
                    "warn",
                    "logic-true",
                    f"`{left.strip()} {m.group(1)} -1`: true was -1 in AeroQSP and is 1 in qSpider, so the comparison flips; "
                    "compare with 0 (`<> 0` / `= 0`) or use the condition directly",
                    block,
                    text,
                    m.start(),
                )

    def _check_bool_arithmetic(self, block: CodeBlock, text: str, masked: str) -> None:
        reported: set[int] = set()

        def report(pos: int, what: str) -> None:
            line = line_of(text, pos)
            if line in reported:
                return
            reported.add(line)
            self._finding(
                "warn",
                "logic-true",
                f"{what} is used in arithmetic: true was -1 in AeroQSP and is 1 in qSpider, so the sign of the result changes",
                block,
                text,
                pos,
            )

        for m in re.finditer(r"\(", masked):
            before = masked[: m.start()].rstrip()
            if before and (IDENT_RE.match(before[-1]) or before[-1] in "$]"):
                continue  # function call or array index
            depth, j = 0, m.start()
            while j < len(masked):
                if masked[j] == "(":
                    depth += 1
                elif masked[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = masked[m.start() + 1 : j]
            if not (top_level_has(COMPARISON_RE, inner) or re.search(r"(?i)(?<![\w$])(and|or|no|obj|loc)(?![\w])", inner)):
                continue
            after = masked[j + 1 :].lstrip()
            op_before = before[-1:] in ("+", "-", "*", "/") or before.endswith(("+=", "-=", "*=", "/="))
            op_after = after[:1] in ("+", "-", "*", "/") and not after.startswith(("+=", "-="))
            if op_before or op_after:
                report(m.start(), f"`({inner.strip()})` (a condition)")
        for name, info in self.vars.items():
            if not info.only_boolean:
                continue
            pattern = rf"(?<![\w$])({re.escape(name)})(?![\w\[])"
            for m in re.finditer(pattern, masked, re.IGNORECASE):
                before = masked[: m.start()].rstrip()
                after = masked[m.end() :].lstrip()
                op_before = before[-1:] in ("+", "-", "*", "/")
                op_after = after[:1] in ("+", "-", "*", "/") and after[:2] not in ("+=", "-=", "*=", "/=")
                if op_before or op_after:
                    report(m.start(), f"`{m.group(1)}` (holds a condition)")

    def _check_precedence(self, block: CodeBlock, text: str, masked: str) -> None:
        pattern = re.compile(
            r"(?<![\w$.])(obj|loc)\s*('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$[\w.]+(?:\[[^\]]*\])?)\s*(<>|<=|>=|=<|=>|=|<|>|!)(?!=)",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            if masked[m.start()] == " ":
                continue  # inside a string or a comment
            self._finding(
                "warn",
                "logic-precedence",
                f"`{m.group(0).strip()} ...` was parsed as `{m.group(1)} ({m.group(2)} {m.group(3)} ...)` in AeroQSP "
                f"and as `({m.group(1)} {m.group(2)}) {m.group(3)} ...` in qSpider; add brackets for the intended meaning",
                block,
                text,
                m.start(),
            )

    def _statements(self, masked: str) -> Iterator[tuple[int, str]]:
        """Top-level statements of masked code with their offsets (without labels and assignment targets)."""
        start = 0
        depth = 0
        for i, ch in enumerate(masked + "\n"):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            elif depth == 0 and ch in "&\n:":
                part_start = start
                for kw in ELSE_RE.finditer(masked, start, i):
                    yield part_start, masked[part_start : kw.start()]
                    part_start = kw.end()
                yield part_start, masked[part_start:i]
                start = i + 1

    def _check_chained(self, block: CodeBlock, text: str, masked: str) -> None:
        for start, stmt in self._statements(masked):
            body = stmt
            assign = re.match(
                r"\s*(?:(?:set|let|local)\s+)?\$?" + IDENT_RE.pattern + r"\s*(?:\[[^\]]*\])?\s*[-+*/]?=(?![=<>])", body, re.IGNORECASE
            )
            offset = 0
            if assign and not re.match(r"\s*(?:if|elseif|act)\b", body, re.IGNORECASE):
                offset = assign.end()
            body = re.sub(r"(?i)^\s*(?:if|elseif|\*?pl|\*?p|msg)\b", lambda m: " " * len(m.group(0)), body[offset:])
            for piece in re.split(r"(?i)\b(?:and|or)\b|,", body):
                flat = []
                depth = 0
                for ch in piece:
                    depth += ch in "(["
                    depth -= ch in ")]"
                    flat.append(ch if depth == 0 else " ")
                if len(COMPARISON_RE.findall("".join(flat))) >= 2:
                    self._finding(
                        "warn",
                        "logic-true",
                        f"`{piece.strip()}`: chained comparison; the first result (-1 in AeroQSP, 1 in qSpider) is compared again, "
                        "so the outcome differs; use brackets and `and`",
                        block,
                        text,
                        start + offset,
                    )

    def _check_bool_output(self, block: CodeBlock, text: str, masked: str) -> None:
        def is_bool(expr: str) -> bool:
            return bool(BOOL_FUNC_RE.match(expr) or top_level_has(COMPARISON_RE, expr) or re.search(r"(?i)^\s*\S.*\b(and|or)\b", expr))

        for start, stmt in self._statements(masked):
            m = re.match(r"(?i)\s*(\*?pl|\*?p|msg)\b(.*)", stmt, re.DOTALL)
            if m and m.group(2).strip() and is_bool(m.group(2)):
                self._finding(
                    "warn",
                    "logic-true",
                    f"`{stmt.strip()}` prints a condition: -1 in AeroQSP, 1 in qSpider",
                    block,
                    text,
                    start,
                )
        for seg in scan_code(text):
            if seg.kind != "str":
                continue
            for m in re.finditer(r"<<(.+?)>>", text[seg.start : seg.end]):
                expr = mask_code(m.group(1).replace("''", "'"))
                if is_bool(expr):
                    self._finding(
                        "warn",
                        "logic-true",
                        f"`<<{m.group(1)}>>` prints a condition: -1 in AeroQSP, 1 in qSpider",
                        block,
                        text,
                        seg.start + m.start(),
                    )

    def _check_isnum(self, block: CodeBlock, text: str, masked: str) -> None:
        for m in re.finditer(r"(?<![\w$.])isnum\s*\(", masked, re.IGNORECASE):
            self._finding(
                "info",
                "logic-isnum",
                "ISNUM('') was true in AeroQSP and is false in qSpider; the result differs if the value can be empty (e.g. empty input)",
                block,
                text,
                m.start(),
            )

    def _check_same_name(self) -> None:
        for name, info in sorted(self.vars.items()):
            if not info.num_sites or not info.str_sites or name in SAME_NAME_IGNORED:
                continue
            common = (info.num_sites.keys() & info.str_sites.keys()) - {"*"}
            maybe = "*" in info.num_sites or "*" in info.str_sites
            if not common and not maybe:
                continue  # e.g. `pers` (index 0) and `$pers[1..3]` are different cells in both versions
            key = min(common) if common else "*"
            file, location, part, line = info.num_sites.get(key) or next(iter(info.num_sites.values()))
            s_file, s_location = (info.str_sites.get(key) or next(iter(info.str_sites.values())))[:2]
            cell = name if key == "0" else f"{name}[{key.removeprefix('s:')}]"
            if common:
                self.findings.append(
                    Finding(
                        "warn",
                        "logic-same-name",
                        f"`{cell}` and `${cell}` were two independent cells in AeroQSP and are one cell in qSpider "
                        f'(assigning `${cell}` erases the number and vice versa); the string is assigned in "{s_location}" ({s_file}). '
                        "If a value is read after the other kind was assigned, rename one of them",
                        file=file,
                        location=location,
                        part=part,
                        line=line,
                    )
                )
            else:
                self.findings.append(
                    Finding(
                        "info",
                        "logic-same-name",
                        f"`{name}[…]` and `${name}[…]` use computed indices: if they meet, qSpider overwrites one value with the other "
                        f'(the string is assigned in "{s_location}")',
                        file=file,
                        location=location,
                        part=part,
                        line=line,
                    )
                )
