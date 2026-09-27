#!/usr/bin/env python3
"""Generate qspsav/_tables.py from the engines' C sources.

The tables are extracted mechanically so they cannot drift from the engines:

* upper-case mappings: each engine's towupper.c is compiled with the system C
  compiler and evaluated for every UTF-16 code unit (the two files are written
  differently -- if-chains in 5.7.0, lookup pages in 5.9 -- so comparing them
  by eye is not an option);
* qspRand8 (5.7.0 variable hash) and the CRC table are parsed from the C
  array literals.

Usage:
    python tools/gen_tables.py --legacy-src /path/to/qsp-legacy/src \
                               --qsp-src /path/to/qsp/qsp
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HEADER = '''"""Tables extracted from the QSP engine sources. GENERATED -- do not edit.

Regenerate with tools/gen_tables.py.
"""

'''

DUMP_UPPER_C = r"""
#include <stdio.h>
int qspToWUpper(int c);
int main(void)
{
    int c;
    for (c = 0; c < 0x10000; ++c)
    {
        int u = qspToWUpper(c);
        if (u != c) printf("%d %d\n", c, u);
    }
    return 0;
}
"""


def dump_upper(towupper_c: Path) -> dict[int, int]:
    with tempfile.TemporaryDirectory() as tmp:
        main_c = Path(tmp) / "main.c"
        main_c.write_text(DUMP_UPPER_C)
        exe = Path(tmp) / "dump"
        subprocess.run(["cc", "-O0", "-o", str(exe), str(main_c), str(towupper_c)], check=True)
        out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    mapping = {}
    for line in out.splitlines():
        src, dst = line.split()
        mapping[int(src)] = int(dst)
    return mapping


def parse_c_array(source: str, name: str) -> list[int]:
    m = re.search(re.escape(name) + r"\s*\[\s*256\s*\]\s*=\s*\{(.*?)\};", source, re.S)
    if not m:
        raise SystemExit(f"array {name!r} not found")
    values = [int(v, 16) for v in re.findall(r"0x[0-9A-Fa-f]+", m.group(1))]
    if len(values) != 256:
        raise SystemExit(f"array {name!r}: expected 256 items, got {len(values)}")
    return values


def format_int_list(name: str, values: list[int], width: int, per_line: int) -> str:
    lines = [f"{name} = ("]
    for i in range(0, len(values), per_line):
        chunk = ", ".join(f"0x{v:0{width}X}" for v in values[i:i + per_line])
        lines.append(f"    {chunk},")
    lines.append(")\n")
    return "\n".join(lines)


def format_mapping(name: str, mapping: dict[int, int]) -> str:
    lines = [f"{name} = {{"]
    items = sorted(mapping.items())
    for i in range(0, len(items), 6):
        chunk = ", ".join(f"0x{k:04X}: 0x{v:04X}" for k, v in items[i:i + 6])
        lines.append(f"    {chunk},")
    lines.append("}\n")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy-src", type=Path, required=True, help="qsp-legacy/src")
    parser.add_argument("--qsp-src", type=Path, required=True, help="qsp/qsp")
    parser.add_argument("-o", "--output", type=Path,
                        default=Path(__file__).resolve().parent.parent / "qspsav" / "_tables.py")
    args = parser.parse_args()

    upper_570 = dump_upper(args.legacy_src / "towupper.c")
    upper_59 = dump_upper(args.qsp_src / "towupper.c")
    rand8 = parse_c_array((args.legacy_src / "variables.c").read_text(encoding="utf-8", errors="replace"), "qspRand8")
    crc_570 = parse_c_array((args.legacy_src / "game.c").read_text(encoding="utf-8", errors="replace"), "qspCRCTable")
    crc_59 = parse_c_array((args.qsp_src / "game.c").read_text(encoding="utf-8", errors="replace"), "qspCRCTable")

    # Both engines use the reflected CRC-32 table (0xEDB88320); only the way
    # they drive it differs. Make sure that is still true.
    reference = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        reference.append(c)
    if crc_570 != reference or crc_59 != reference:
        raise SystemExit("CRC table differs from the standard CRC-32 table; update qspsav/tables.py")
    assert zlib.crc32(b"123456789") == 0xCBF43926

    body = HEADER
    body += "# qspToWUpper() of qsp-legacy (5.7.0): code unit -> upper-case code unit, identity omitted.\n"
    body += format_mapping("UPPER_570", upper_570) + "\n"
    body += "# qspToWUpper() of qsp (5.9): code unit -> upper-case code unit, identity omitted.\n"
    body += format_mapping("UPPER_59", upper_59) + "\n"
    body += "# qspRand8 of qsp-legacy (5.7.0), used by qspVarReference to pick a variables window.\n"
    body += format_int_list("RAND8_570", rand8, 2, 16) + "\n"
    body += "# qspCRCTable, identical in both engines (standard reflected CRC-32).\n"
    body += format_int_list("CRC_TABLE", crc_59, 8, 8)

    args.output.write_text(body, encoding="utf-8")
    only_570 = sorted(set(upper_570.items()) - set(upper_59.items()))
    only_59 = sorted(set(upper_59.items()) - set(upper_570.items()))
    print(f"written {args.output}")
    print(f"upper-case pairs: 5.7.0={len(upper_570)} 5.9={len(upper_59)} "
          f"only-5.7.0={len(only_570)} only-5.9={len(only_59)}", file=sys.stderr)


if __name__ == "__main__":
    main()
