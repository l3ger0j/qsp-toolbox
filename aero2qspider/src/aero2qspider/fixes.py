"""Automatic fixes for QSP code, texts and markup of an AeroQSP game."""

import argparse
import re
from collections import Counter
from collections.abc import Callable

from .constants import (
    AERO_EFFECTS,
    AERO_NAMED_COLORS,
    CSS_LENGTH_PROPS,
    GLOBAL_CSS_SELECTORS,
    QSPIDER_MISSING_EFFECTS,
)
from .findings import Edit, Finding, apply_edits, dedupe_edits, line_of
from .qsp_format import Action, Location
from .resources import MISSING_REF_RE, ResourceIndex
from .scanner import (
    Segment,
    arg_kind,
    find_statement_end,
    mask_code,
    parse_call_args,
    scan_code,
    segment_at,
    subexpressions,
    top_level_has,
)

BARE_CALL_RE = re.compile(r"(?:^|[&:]|\belse(?=[ \t]))[ \t]*(\$?(?:func|dyneval))(?![\w.])", re.IGNORECASE | re.MULTILINE)
ELSE_RE = re.compile(r"[ \t]else(?![\w.])", re.IGNORECASE)
OPERATOR_RE = re.compile(r"[-+*/<>=!]|\b(?:and|or|mod|no|obj|loc)\b", re.IGNORECASE)

UNARY_AFTER_WORDS = frozenset(
    [
        "and",
        "or",
        "mod",
        "no",
        "obj",
        "loc",
        "if",
        "elseif",
        "pl",
        "p",
        "nl",
        "msg",
        "wait",
        "settimer",
        "gs",
        "gosub",
        "gt",
        "goto",
        "xgt",
        "xgoto",
        "view",
        "exit",
        "set",
        "let",
        "local",
    ]
)


def is_unary(text: str, pos: int) -> bool:
    """True if the `+` at `pos` starts an operand instead of adding two operands."""
    q = pos - 1
    while True:
        while q >= 0 and text[q] in " \t\r":
            q -= 1
        # a line ending with " _" continues on the next one
        if q >= 0 and text[q] == "\n":
            k = q - 1
            while k >= 0 and text[k] in " \t\r":
                k -= 1
            if k >= 1 and text[k] == "_" and text[k - 1] in " \t":
                q = k - 1
                continue
        break
    if q < 0 or text[q] in "\n([,=<>!+-*/":
        return True
    if not (text[q].isalnum() or text[q] == "_"):
        return False
    start = q
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_.$"):
        start -= 1
    return text[start : q + 1].lower() in UNARY_AFTER_WORDS


class Converter:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.findings: list[Finding] = []
        self.resources = ResourceIndex([])
        self.current_file = ""
        self.usehtml_on = False
        self.fsize_set = False
        self.fonts_used: Counter[str] = Counter()

    def add(self, severity: str, rule: str, message: str, **kw: object) -> None:
        self.findings.append(Finding(severity, rule, message, file=self.current_file, **kw))  # type: ignore[arg-type]

    def _pos(self, ctx: dict[str, object], pos: int) -> dict[str, object]:
        full = ctx["full"]
        return {"location": ctx["location"], "part": ctx["part"], "line": line_of(full, pos) if isinstance(full, str) else 1}

    def fix_markup(self, text: str, base: int, edits: list[Edit], ctx: dict[str, object]) -> None:
        """Fix HTML/text content; `base` is the offset of `text` inside the code being fixed."""
        if not self.args.no_fix_paths:
            for start, end, old, new in self.resources.fix_paths(text):
                edits.append(Edit(base + start, base + end, new))
                kind = "letter case" if old.replace("\\", "/").lower() == new.lower() and "\\" not in old else "path"
                self.add("fix", "paths", f"{kind}: `{old}` → `{new}`", before=old, after=new, **self._pos(ctx, base + start))

        known = self.resources.match_spans(text)
        for m in MISSING_REF_RE.finditer(text):
            ref = m.group(1)
            if any(s <= m.start(1) and m.end(1) <= e for s, e in known):
                continue  # part of a known file name with spaces
            stem = re.split(r"[\\/]", ref)[-1].rsplit(".", 1)[0]
            if (
                not stem  # tail of a path built in code: 'music/' + $name + '.mp3'
                or "<<" in ref
                or self.resources.canonical_file(ref)
                or ref.lower().startswith(("http", "www."))
                or (not re.search(r"[\\/]", ref) and self.resources.has_basename(ref))  # joined with a folder in code
            ):
                continue
            self.add("warn", "missing-files", f"`{ref}` is not in the archive", before=ref, **self._pos(ctx, base + m.start()))

        if not self.args.no_fix_colors:
            for tag in re.finditer(r"<font\b[^>]*>", text, re.IGNORECASE):
                for cm in re.finditer(r"(\bcolor\s*=\s*)(''|\"|'|)([A-Za-z][A-Za-z ]*?)(\2)(?=[\s>/])", tag.group(0), re.IGNORECASE):
                    hexcolor = AERO_NAMED_COLORS.get(cm.group(3).strip().lower())
                    if not hexcolor:
                        continue
                    s, e = base + tag.start() + cm.start(3), base + tag.start() + cm.end(3)
                    edits.append(Edit(s, e, hexcolor))
                    self.add(
                        "fix",
                        "colors",
                        f"`{cm.group(3)}` → `{hexcolor}` (AeroQSP palette)",
                        before=cm.group(3),
                        after=hexcolor,
                        **self._pos(ctx, s),
                    )

        # qSpider itself handles unitless numbers and url-less background-image in style attributes
        for sm in re.finditer(r"\bstyle\s*=\s*(''|\"|')(.*?)\1", text, re.IGNORECASE | re.DOTALL):
            for fm in re.finditer(r"font-family\s*:\s*([^;}\n'\"]+)", sm.group(2), re.IGNORECASE):
                for name in fm.group(1).split(","):
                    if name.strip():
                        self.fonts_used[name.strip()] += 1

        lower = text.lower()
        center_in_cell = bool(re.search(r"<t[dh]\b(?:(?!</t[dh]>).)*<center", lower, re.DOTALL))
        if "<center" in lower and (center_in_cell or ctx.get("list_item")):
            if self.args.no_theme:
                self.add(
                    "warn",
                    "center",
                    "`<center>` inside a table: Flash stretched such a table to the full width, browsers do not. "
                    'Add `width="100%"` to the `<table>` (or to $ACTION_FORMAT/$OBJECT_FORMAT)',
                    **self._pos(ctx, base),
                )
            else:
                self.add(
                    "info",
                    "center",
                    "`<center>` inside a table or list item: the compatibility theme stretches such tables like Flash did",
                    **self._pos(ctx, base),
                )

    def fix_css(self, css: str, base: int, edits: list[Edit], ctx: dict[str, object]) -> None:
        """Fix a $STYLESHEET string, which qSpider turns into a page-wide <style>."""
        props = "|".join(re.escape(p) for p in sorted(CSS_LENGTH_PROPS, key=len, reverse=True))
        for m in re.finditer(rf"(?<![\w-])({props})(\s*:\s*)(-?\d+(?:\.\d+)?)(?=\s*(?:;|}}|$|\n|''|\"|'))", css, re.IGNORECASE):
            if m.group(3) in ("0", "-0"):
                continue
            pos = base + m.end(3)
            edits.append(Edit(pos, pos, "px"))
            self.add(
                "fix",
                "css-units",
                f"`{m.group(0)}` → `{m.group(0)}px` (browsers ignore lengths without units)",
                before=m.group(0),
                after=m.group(0) + "px",
                **self._pos(ctx, base + m.start()),
            )
        for m in re.finditer(r"(?<![\w-])leading\s*:", css, re.IGNORECASE):
            self.add("warn", "css-leading", "`leading` exists only in Flash; use `line-height`", **self._pos(ctx, base + m.start()))
        for m in re.finditer(r"font-family\s*:\s*([^;}\n'\"]+)", css, re.IGNORECASE):
            for name in m.group(1).split(","):
                if name := name.strip().strip("'\""):
                    self.fonts_used[name] += 1
        for m in re.finditer(r"(^|})\s*([^{}]+?)\s*{", css):
            for selector in m.group(2).split(","):
                head = re.split(r"[\s:.#>\[]", selector.strip(), maxsplit=1)[0].lower()
                if head in GLOBAL_CSS_SELECTORS:
                    self.add(
                        "warn",
                        "css-global",
                        f"`{selector.strip()}` in $STYLESHEET applies to the whole qSpider page, pause screen included; "
                        f"scope it, e.g. `qsp-game-root {selector.strip()}`",
                        **self._pos(ctx, base + m.start(2)),
                    )

    def fix_calls(self, text: str, evaluated: Callable[[int], bool], edits: list[Edit], ctx: dict[str, object]) -> None:
        """Expressions, both in code and in `<<...>>` of strings and texts: unary plus, RAND and INSTR/ARRPOS/ARRCOMP calls."""
        for m in re.finditer(r"\+(?!=)", text):
            if not evaluated(m.start()) or not is_unary(text, m.start()):
                continue
            end = m.end() + len(text[m.end() :]) - len(text[m.end() :].lstrip(" \t"))
            left = max(text.rfind("\n", 0, m.start()) + 1, m.start() - 50)
            right = text.find("\n", m.start())
            right = min(len(text) if right < 0 else right, end + 30)
            before = text[left:right].strip()
            after = (text[left : m.start()] + text[end:right]).strip()
            edits.append(Edit(m.start(), end, ""))
            self.add(
                "fix",
                "unary-plus",
                f"`{before}` → `{after}` (QSP 5.9 has no unary plus and stops with a syntax error)",
                before=before,
                after=after,
                **self._pos(ctx, m.start()),
            )

        # RAND(n) returns 0..n in QSP 5.7 and 1..n in QSP 5.9; RAND(0, n) gives 0..n in both
        for m in re.finditer(r"(?<![\w$.])rand\s*\(", text, re.IGNORECASE):
            if not evaluated(m.start()):
                continue
            parsed = parse_call_args(text, m.end() - 1)
            if not parsed or len(parsed[0]) != 1 or not text[parsed[0][0][0] : parsed[0][0][1]].strip():
                continue
            arg_start, close = parsed[0][0][0], parsed[1]
            arg_start += len(text[arg_start:close]) - len(text[arg_start:close].lstrip())
            call = text[m.start() : close + 1]
            if self.args.no_fix_rand:
                self.add(
                    "warn",
                    "rand",
                    f"`{call}` returned 0..n in AeroQSP and returns 1..n in qSpider; `RAND(0, n)` keeps the old range",
                    **self._pos(ctx, m.start()),
                )
                continue
            edits.append(Edit(arg_start, arg_start, "0, "))
            after = text[m.start() : arg_start] + "0, " + text[arg_start : close + 1]
            self.add(
                "fix",
                "rand",
                f"`{call}` → `{after}` (keeps the 0..n range of AeroQSP)",
                before=call,
                after=after,
                **self._pos(ctx, m.start()),
            )

        for m in re.finditer(r"(?<![\w$])(instr|arrpos|arrcomp)\s*\(", text, re.IGNORECASE):
            if not evaluated(m.start()):
                continue
            parsed = parse_call_args(text, m.end() - 1)
            if not parsed or len(parsed[0]) != 3:
                continue
            spans = parsed[0]
            args = [text[s:e] for s, e in spans]
            kinds = [arg_kind(a) for a in args]
            call = text[m.start() : spans[-1][1] + 1]
            if kinds[0] == "num" and kinds[1] == "str":
                new_args = [args[1].strip(), args[2].strip(), args[0].strip()]
                edits.append(Edit(spans[0][0], spans[-1][1], ", ".join(new_args)))
                after = f"{m.group(1)}({', '.join(new_args)})"
                self.add("fix", "arg-order", f"`{call}` → `{after}`", before=call, after=after, **self._pos(ctx, m.start()))
            elif kinds[0] != "str":
                self.add(
                    "warn",
                    "arg-order",
                    f"`{call}`: cannot tell whether this is the old argument order (start position first) or the new one (last)",
                    **self._pos(ctx, m.start()),
                )

    def fix_bare_calls(self, text: str, edits: list[Edit], ctx: dict[str, object]) -> None:
        """A bare FUNC/DYNEVAL statement printed an empty line in QSP 5.7 when it had no RESULT; QSP 5.9 prints nothing."""
        masked = mask_code(text)
        for m in BARE_CALL_RE.finditer(masked):
            start = m.start(1)
            end = find_statement_end(text, start)
            if other := ELSE_RE.search(masked, m.end(1), end):
                end = other.start()
            rest = masked[m.end(1) : end]
            if rest.lstrip().startswith("("):
                parsed = parse_call_args(text, m.end(1) + len(rest) - len(rest.lstrip()))
                if not parsed or masked[parsed[1] + 1 : end].strip():
                    continue
            elif not text[m.end(1) : end].strip() or not rest[:1].isspace() or top_level_has(OPERATOR_RE, rest):
                continue
            call = text[start:end].rstrip()
            if self.args.no_fix_bare_calls:
                self.add(
                    "warn",
                    "bare-calls",
                    f"`{call}`: without a RESULT AeroQSP printed an empty line here and qSpider prints nothing; `*pl {call}` keeps the line",
                    **self._pos(ctx, start),
                )
                continue
            edits.append(Edit(start, start, "*pl "))
            self.add(
                "fix",
                "bare-calls",
                f"`{call}` → `*pl {call}` (without a RESULT AeroQSP printed an empty line here; qSpider would print nothing)",
                before=call,
                after=f"*pl {call}",
                **self._pos(ctx, start),
            )

    def fix_code(self, text: str, location: str, part: str, list_item: bool = False) -> str:
        if not text:
            return text
        segments = scan_code(text)
        edits: list[Edit] = []
        ctx: dict[str, object] = {"full": text, "location": location, "part": part, "list_item": list_item}

        def in_code(pos: int) -> bool:
            seg = segment_at(segments, pos)
            return seg is not None and seg.kind == "code"

        def evaluated(pos: int) -> bool:
            seg = segment_at(segments, pos)
            if seg is None:
                return False
            return seg.kind == "code" or (seg.kind == "str" and any(a <= pos < b for a, b in subexpressions(text, seg.start, seg.end)))

        def next_string(pos: int) -> Segment | None:
            while pos < len(text) and text[pos] in " \t":
                pos += 1
            seg = segment_at(segments, pos)
            return seg if seg is not None and seg.kind == "str" and seg.start == pos else None

        for m in re.finditer(r"(?<![\w$])(addqst|killqst)(?!\w)", text, re.IGNORECASE):
            if not in_code(m.start()):
                continue
            new = {"addqst": "inclib", "killqst": "freelib"}[m.group(1).lower()]
            new = new.upper() if m.group(1).isupper() else new
            edits.append(Edit(m.start(), m.end(), new))
            self.add("fix", "keywords", f"`{m.group(1)}` → `{new}`", before=m.group(1), after=new, **self._pos(ctx, m.start()))

        for m in re.finditer(r"(?<![\w])disablesubex(?!\w)", text, re.IGNORECASE):
            if in_code(m.start()):
                self.add(
                    "warn",
                    "disablesubex",
                    "QSP 5.9 has no DISABLESUBEX: `<<...>>` in strings is always evaluated",
                    **self._pos(ctx, m.start()),
                )

        self.fix_calls(text, evaluated, edits, ctx)

        self.fix_bare_calls(text, edits, ctx)

        if not self.args.no_fix_stat_format:
            for m in re.finditer(r"(?<![\w])(\$?)(stat_format)(?!\w)", text, re.IGNORECASE):
                if in_code(m.start()):
                    new = "STATS_FORMAT" if m.group(2).isupper() else "stats_format"
                    edits.append(Edit(m.start(2), m.end(2), new))
                    self.add(
                        "fix",
                        "stat-format",
                        f"`{m.group(0)}` → `{m.group(1)}{new}`",
                        before=m.group(0),
                        after=m.group(1) + new,
                        **self._pos(ctx, m.start()),
                    )

        for m in re.finditer(r"(?<![\w])\$?((?:newloc|view|input|msg|menu)_effect)\s*=(?!=)", text, re.IGNORECASE):
            if not in_code(m.start()):
                continue
            seg = next_string(m.end())
            if seg is None:
                self.add("info", "effects", f"`{m.group(0)}` is not a literal; check the effect name by hand", **self._pos(ctx, m.start()))
                continue
            value = text[seg.start + 1 : seg.end - 1]
            effect = value.strip().lower()
            if not effect:
                continue
            new = effect
            if effect in QSPIDER_MISSING_EFFECTS:
                new = self.args.effect_fallback
                self.add(
                    "fix",
                    "effects",
                    f"effect `{value}` is not implemented in qSpider → `{new}`",
                    before=value,
                    after=new,
                    **self._pos(ctx, seg.start),
                )
            elif effect not in AERO_EFFECTS:
                self.add("warn", "effects", f"unknown effect `{value}`", **self._pos(ctx, seg.start))
                continue
            elif value != effect:
                self.add(
                    "fix",
                    "effects",
                    f"`{value}` → `{effect}` (qSpider effect names are case-sensitive)",
                    before=value,
                    after=effect,
                    **self._pos(ctx, seg.start),
                )
            if new != value:
                edits.append(Edit(seg.start + 1, seg.end - 1, new))

        for m in re.finditer(r"(?<![\w])exec\b", text, re.IGNORECASE):
            if not in_code(m.start()):
                continue
            seg = next_string(m.end() + (1 if text[m.end() : m.end() + 1] == "(" else 0))
            if seg is not None and text[seg.start + 1 : seg.start + 8].lower() == "effect:":
                self.add(
                    "warn",
                    "exec-effect",
                    f"`{text[seg.start : seg.end]}`: qSpider ignores on-demand effects (it handles only `qspider.*` commands)",
                    **self._pos(ctx, m.start()),
                )

        if re.search(r"(?<![\w])usehtml\s*=\s*[1-9]", text, re.IGNORECASE):
            self.usehtml_on = True
        if re.search(r"(?<![\w])fsize\s*=", text, re.IGNORECASE):
            self.fsize_set = True
        for m in re.finditer(r"(?<![\w])(scroll_speed|disableautoref)\s*=", text, re.IGNORECASE):
            if in_code(m.start()):
                self.add("info", "unsupported-vars", f"qSpider ignores `{m.group(1).upper()}`", **self._pos(ctx, m.start()))
        for m in re.finditer(r"(?<![\w])\$fname\s*=", text, re.IGNORECASE):
            seg = next_string(m.end())
            if seg is not None and in_code(m.start()):
                self.fonts_used[text[seg.start + 1 : seg.end - 1].strip()] += 1

        # A skin image that does not exist shows the same as '' in both players, but makes qSpider request a 404
        if not self.args.no_fix_missing_skin_images:
            for m in re.finditer(r"(?<![\w])\$(\w*(?:backimage|_image|topimage))\s*=(?!=)", text, re.IGNORECASE):
                if not in_code(m.start()):
                    continue
                seg = next_string(m.end())
                if seg is None:
                    continue
                value = text[seg.start + 1 : seg.end - 1]
                if value and "<<" not in value and not self.resources.canonical_file(value):
                    edits.append(Edit(seg.start + 1, seg.end - 1, ""))
                    self.add(
                        "fix",
                        "missing-files",
                        f"`${m.group(1)} = '{value}'`: the file does not exist, which looks the same as `''` in both players → `''`",
                        before=value,
                        after="",
                        **self._pos(ctx, seg.start),
                    )
                    seg.kind = "skip"

        for seg in segments:
            if seg.kind != "str":
                continue
            prev = text[max(0, seg.start - 200) : seg.start]
            statement = prev[max(prev.rfind("\n"), prev.rfind("&")) + 1 :]
            content_start = seg.start + 1
            content = text[content_start : seg.end - 1]
            if re.search(r"\$stylesheet\s*\+?=", statement, re.IGNORECASE):
                self.fix_css(content, content_start, edits, ctx)
                continue
            item_ctx = dict(ctx)
            item_ctx["list_item"] = list_item or bool(re.search(r"(?<![\w])(act|addobj|add\s+obj)\b", statement, re.IGNORECASE))
            self.fix_markup(content, content_start, edits, item_ctx)

        return apply_edits(text, dedupe_edits(edits))

    def fix_text(self, text: str, location: str, part: str, list_item: bool = False) -> str:
        """Fix raw text: a location description, an action name or an image path."""
        if not text:
            return text
        edits: list[Edit] = []
        ctx: dict[str, object] = {"full": text, "location": location, "part": part, "list_item": list_item}
        self.fix_markup(text, 0, edits, ctx)
        spans = subexpressions(text, 0, len(text))
        if spans:
            self.fix_calls(text, lambda pos: any(a <= pos < b for a, b in spans), edits, ctx)
        return apply_edits(text, dedupe_edits(edits))

    def convert_locations(self, locations: list[Location]) -> list[Location]:
        result = []
        for loc in locations:
            description = self.fix_text("\r\n".join(loc.description), loc.name, "desc").split("\r\n") if loc.description else []
            actions = []
            for i, act in enumerate(loc.actions):
                name = self.fix_text(act.name, loc.name, f"act-name:{i}", list_item=True)
                image = act.image
                canonical = self.resources.canonical_file(image) if image else None
                if image and canonical and canonical != image and not self.args.no_fix_paths:
                    self.add("fix", "paths", f"action image: `{image}` → `{canonical}`", location=loc.name, part=f"act-name:{i}", line=1)
                    image = canonical
                code = self.fix_code("\n".join(act.code), loc.name, f"act:{i}").split("\n") if act.code else []
                actions.append(Action(name, image, code))
            code = self.fix_code("\n".join(loc.code), loc.name, "code").split("\n") if loc.code else []
            result.append(Location(loc.name, description, code, actions))
        return result
