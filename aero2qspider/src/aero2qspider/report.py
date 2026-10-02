"""CONVERSION_REPORT.md for one game."""

from collections import Counter, defaultdict

from . import __version__
from .findings import RULE_TITLES, Finding

LineMaps = dict[str, dict[tuple[str, str], int]]


def where(f: Finding, line_maps: LineMaps) -> str:
    if not f.location:
        return f"`{f.file}`" if f.file else ""
    base = line_maps.get(f.file, {}).get((f.location, f.part))
    part = {"code": "code", "desc": "description"}.get(f.part, "")
    if f.part.startswith("act:"):
        part = f"action {int(f.part.split(':')[1]) + 1}"
    elif f.part.startswith("act-name:"):
        part = f"name of action {int(f.part.split(':')[1]) + 1}"
    line = f":{base + f.line - 1}" if base else ""
    return f'`{f.file}{line}`, location "{f.location}"' + (f", {part}" if part else "")


def render_report(findings: list[Finding], summary: dict[str, object], line_maps: LineMaps) -> str:
    out = [f"# aero2qspider {__version__} report", ""]
    out += [f"- **{key}:** {value}" for key, value in summary.items()]
    counts = Counter(f.severity for f in findings)
    out += [
        "",
        f"Fixed automatically: **{counts['fix']}**, needs attention: **{counts['warn']}**, for information: **{counts['info']}**.",
        "",
    ]
    for severity, title in (("warn", "Needs attention"), ("fix", "Fixed automatically"), ("info", "For information")):
        items = [f for f in findings if f.severity == severity]
        if not items:
            continue
        out += [f"## {title}", ""]
        by_rule: dict[str, list[Finding]] = defaultdict(list)
        for f in items:
            by_rule[f.rule].append(f)
        for rule, rule_items in by_rule.items():
            out += [f"### {RULE_TITLES.get(rule, rule)} ({len(rule_items)})", ""]
            grouped: dict[str, list[Finding]] = defaultdict(list)
            for f in rule_items:
                grouped[f.message].append(f)
            for shown, (message, group) in enumerate(grouped.items()):
                if shown >= 40:
                    out.append(f"- …and {len(grouped) - shown} more")
                    break
                places = [p for f in group if (p := where(f, line_maps))]
                suffix = f" ({places[0]}" + (f" and {len(places) - 1} more" if len(places) > 1 else "") + ")" if places else ""
                first, *details = message.split("\n")
                out.append(f"- {first}{suffix}")
                if details:
                    out += ["", "  ```text", *(f"  {line}" for line in details), "  ```", ""]
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"
