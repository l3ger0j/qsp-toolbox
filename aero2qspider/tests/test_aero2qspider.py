"""Unit and end-to-end tests that need nothing but Python."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from aero2qspider.cli import build_parser, main
from aero2qspider.fixes import Converter
from aero2qspider.logic import LogicAnalyzer, code_blocks
from aero2qspider.qsp_format import Action, Location, read_qsp, read_qsps, write_qsp, write_qsps
from aero2qspider.resources import ResourceIndex, swf_font_names
from aero2qspider.scanner import scan_code
from aero2qspider.theme import build_theme


def make_args(**overrides: object) -> argparse.Namespace:
    args = build_parser().parse_args(["convert", "in.aqsp", "-o", "out"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def fix(code: str, files: list[str] | None = None, **overrides: object) -> tuple[str, Converter]:
    conv = Converter(make_args(**overrides))
    conv.resources = ResourceIndex(files or [])
    return conv.fix_code(code, "loc", "code"), conv


class QspFormatTest(unittest.TestCase):
    def test_binary_roundtrip(self) -> None:
        locations = [
            Location("start", ["Описание", "<b>вторая</b>"], ["*pl 'привет'", "act 'x': gt 'b'"], [Action("Пойти", "img.png", ["gt 'b'"])]),
            Location("b", [], ["'символ \x05 и ''кавычки''"], []),
        ]
        self.assertEqual(read_qsp(write_qsp(locations)), locations)

    def test_qsps_roundtrip_and_multiline_strings(self) -> None:
        text = "# start\n'строка\n- не конец локации'\n{\n- и это тоже\n}\n--- start ---\n\n# second\n*pl 1\n---\n"
        locations = read_qsps(text)
        self.assertEqual([loc.name for loc in locations], ["start", "second"])
        self.assertEqual(locations[0].code[1], "- не конец локации'")
        written, starts = write_qsps(locations)
        self.assertEqual(read_qsps(written), locations)
        self.assertEqual(starts[("second", "code")], 10)


class ScanTest(unittest.TestCase):
    def test_strings_comments_and_code(self) -> None:
        code = "a = 1 & !comment 'with quote\ncontinues' here\nb = 'it''s' & ! second\nif x ! y: c"
        kinds = [(s.kind, code[s.start : s.end]) for s in scan_code(code)]
        self.assertIn(("comment", "!comment 'with quote\ncontinues' here"), kinds)
        self.assertIn(("str", "'it''s'"), kinds)
        self.assertIn(("comment", "! second"), kinds)
        self.assertEqual(kinds[-1], ("code", "\nif x ! y: c"))


class CodeFixesTest(unittest.TestCase):
    def test_keywords(self) -> None:
        out, _ = fix("ADDQST 'mod.qsp'\nkillqst\n'addqst in text'\n! addqst in comment")
        self.assertEqual(out, "INCLIB 'mod.qsp'\nfreelib\n'addqst in text'\n! addqst in comment")

    def test_argument_order(self) -> None:
        out, conv = fix("p = INSTR(3, $s, 'x')\nq = arrpos(0, 'arr', 5)\nr = instr($s, 'x', 3)\nt = instr($s, 'x')")
        self.assertEqual(out, "p = INSTR($s, 'x', 3)\nq = arrpos('arr', 5, 0)\nr = instr($s, 'x', 3)\nt = instr($s, 'x')")
        self.assertEqual(sum(f.rule == "arg-order" for f in conv.findings), 2)

    def test_argument_order_ambiguous_is_reported(self) -> None:
        out, conv = fix("p = instr(n, m, k)")
        self.assertEqual(out, "p = instr(n, m, k)")
        self.assertEqual([f.severity for f in conv.findings if f.rule == "arg-order"], ["warn"])

    def test_stat_format(self) -> None:
        out, _ = fix("$STAT_FORMAT = '<b>%TEXT%</b>'")
        self.assertEqual(out, "$STATS_FORMAT = '<b>%TEXT%</b>'")

    def test_effects(self) -> None:
        out, conv = fix("$NEWLOC_EFFECT = 'Fade'\n$view_effect = 'pixels'\n$msg_effect = 'nope'")
        self.assertEqual(out, "$NEWLOC_EFFECT = 'fade'\n$view_effect = 'fade'\n$msg_effect = 'nope'")
        self.assertIn("warn", [f.severity for f in conv.findings if f.rule == "effects"])

    def test_exec_effect_is_reported(self) -> None:
        _, conv = fix("EXEC 'effect:quake:500'")
        self.assertEqual([f.rule for f in conv.findings], ["exec-effect"])

    def test_paths(self) -> None:
        files = ["content/Img.PNG", "content/Дед Мороз.gif", "snd/a b.mp3"]
        out, conv = fix(
            "'<img src=\"CONTENT\\img.png\">'\nview 'content\\Дед Мороз.gif'\nplay 'snd\\a b.mp3'\n$p = 'content\\' + $n",
            files,
        )
        self.assertEqual(
            out,
            "'<img src=\"content/Img.PNG\">'\nview 'content/Дед Мороз.gif'\nplay 'snd/a b.mp3'\n$p = 'content/' + $n",
        )
        self.assertFalse([f for f in conv.findings if f.rule == "missing-files"])

    def test_missing_files(self) -> None:
        out, conv = fix(
            "$MAINDESC_BACKIMAGE = 'content/.gif'\n'<img src=\"pic/none.png\">'\nplay 'music/' + $m + '.mp3'\n'<img src=\"rain.png\">'",
            ["scenes/rain.png"],
        )
        self.assertTrue(out.startswith("$MAINDESC_BACKIMAGE = ''"))
        missing = [f.before for f in conv.findings if f.rule == "missing-files" and f.severity == "warn"]
        self.assertEqual(missing, ["pic/none.png"])

    def test_named_colors(self) -> None:
        out, _ = fix("'<font color=\"orange\">a</font><font color=''dark green''>b</font><font color=#123456>c</font>'")
        self.assertEqual(out, "'<font color=\"#CC3232\">a</font><font color=''#2F4F2F''>b</font><font color=#123456>c</font>'")

    def test_stylesheet_units_and_global_selectors(self) -> None:
        out, conv = fix("$STYLESHEET = 'body { font-size: 14; margin-left:0 } .x{letter-spacing:2}'")
        self.assertEqual(out, "$STYLESHEET = 'body { font-size: 14px; margin-left:0 } .x{letter-spacing:2px}'")
        self.assertIn("css-global", [f.rule for f in conv.findings])

    def test_center(self) -> None:
        _, conv = fix("'<center><table><tr><td>x</td></tr></table></center>'")
        self.assertFalse([f for f in conv.findings if f.rule == "center"])
        _, conv = fix("act '<center>Играть</center>': gt 'x'")
        self.assertEqual([f.severity for f in conv.findings if f.rule == "center"], ["info"])
        _, conv = fix("act '<center>Играть</center>': gt 'x'", no_theme=True)
        self.assertEqual([f.severity for f in conv.findings if f.rule == "center"], ["warn"])


def logic(code: str) -> list[tuple[str, str]]:
    """Run the logic analyzer on QSPS code (or a single location) and return (severity, rule) pairs."""
    locations = read_qsps(code) if code.lstrip().startswith("#") else [Location("loc", [], code.split("\n"), [])]
    analyzer = LogicAnalyzer()
    blocks = list(code_blocks("g.qsps", locations))
    analyzer.collect(blocks)
    analyzer.analyze(blocks)
    return [(f.severity, f.rule) for f in analyzer.findings]


class RandFixTest(unittest.TestCase):
    def test_single_argument_rand_keeps_the_57_range(self) -> None:
        out, conv = fix("x = RAND(6) + rand( n )\ny = rand(1, 6)\n'rand(3)'")
        self.assertEqual(out, "x = RAND(0, 6) + rand( 0, n )\ny = rand(1, 6)\n'rand(3)'")
        self.assertEqual([f.rule for f in conv.findings], ["rand", "rand"])

    def test_rand_can_be_left_alone(self) -> None:
        out, conv = fix("x = rand(6)", no_fix_rand=True)
        self.assertEqual(out, "x = rand(6)")
        self.assertEqual([(f.severity, f.rule) for f in conv.findings], [("warn", "rand")])


class ExpressionFixesTest(unittest.TestCase):
    def test_rand_inside_subexpressions_and_links(self) -> None:
        fixed, _ = fix("*pl 'You rolled <<rand(3)>>'\n*pl '<a href=\"exec:x = rand(2)\">roll</a>'")
        self.assertEqual(fixed, "*pl 'You rolled <<rand(0, 3)>>'\n*pl '<a href=\"exec:x = rand(0, 2)\">roll</a>'")

    def test_rand_in_description(self) -> None:
        conv = Converter(make_args())
        conv.resources = ResourceIndex([])
        self.assertEqual(conv.fix_text("The die says <<rand(6)>>", "loc", "desc"), "The die says <<rand(0, 6)>>")

    def test_unary_plus(self) -> None:
        cases = {
            "gs 'ch',+3": "gs 'ch',3",
            "x = +3 & *pl 2*+3": "x = 3 & *pl 2*3",
            "x = x -+ 1": "x = x -1",
            "if +x > 0: x = x+1": "if x > 0: x = x+1",
            "*pl '<<+3>>'": "*pl '<<3>>'",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(fix(code)[0], expected)

    def test_binary_plus_is_kept(self) -> None:
        for code in ("$x = 'a' + 'b'", "x = y + 1", "x = 'a'+'b'+c", "$x = 'a' _\n  + 'b'", "*pl 'a +1'", "x += 1"):
            with self.subTest(code=code):
                self.assertEqual(fix(code)[0], code)

    def test_bare_func_statements(self) -> None:
        cases = {
            "func('noop')": "*pl func('noop')",
            "func 'noop'": "*pl func 'noop'",
            "dyneval('x = 1')": "*pl dyneval('x = 1')",
            "if x: func('a') else func('b')": "if x: *pl func('a') else *pl func('b')",
            "func('a') & gt 'b'": "*pl func('a') & gt 'b'",
            "act 'z': func('w')": "act 'z': *pl func('w')",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(fix(code)[0], expected)
        for code in ("x = func('a')", "$s = $func('a') + 'z'", "func 'a', x+1", "*pl func('a')", ":func"):
            with self.subTest(code=code):
                self.assertEqual(fix(code)[0], code)

    def test_bare_func_warning_only(self) -> None:
        fixed, conv = fix("func('noop')", no_fix_bare_calls=True)
        self.assertEqual(fixed, "func('noop')")
        self.assertEqual([(f.severity, f.rule) for f in conv.findings], [("warn", "bare-calls")])


class LogicAnalyzerTest(unittest.TestCase):
    """Every positive case below behaves differently on libqsp 5.7 and on @qsp/wasm-engine 5.9.5."""

    def test_no_on_numeric_variable(self) -> None:
        self.assertIn(("warn", "logic-bitwise"), logic("flag = 1\nif no flag: gt 'x'"))
        self.assertNotIn(("warn", "logic-bitwise"), logic("flag = (a > b)\nif no flag: gt 'x'"))
        self.assertNotIn(("warn", "logic-bitwise"), logic("x = 1\nif no x = 1: gt 'x'"))

    def test_and_on_two_numbers(self) -> None:
        self.assertIn(("warn", "logic-bitwise"), logic("gold = 2\nkeys = 1\nif gold and keys: gt 'x'"))
        self.assertNotIn(("warn", "logic-bitwise"), logic("gold = 2\nif gold > 1 and keys = 1: gt 'x'"))
        self.assertEqual(logic("if 3 and 5: gt 'x'"), [])  # 3 & 5 = 1: same truth value
        self.assertIn(("warn", "logic-bitwise"), logic("if 2 and 1: gt 'x'"))

    def test_true_compared_with_minus_one(self) -> None:
        self.assertIn(("warn", "logic-true"), logic("if (obj 'key') = -1: gt 'x'"))
        self.assertIn(("warn", "logic-true"), logic("seen = a > 1\nif seen = -1: gt 'x'"))
        self.assertEqual(logic("if no arrpos('$a', 'x') = -1: gt 'x'"), [])  # ARRPOS really returns -1

    def test_boolean_in_arithmetic_and_output(self) -> None:
        self.assertIn(("warn", "logic-true"), logic("score = score + (hp > 0)"))
        self.assertIn(("warn", "logic-true"), logic("*pl 1 = 1"))
        self.assertIn(("warn", "logic-true"), logic("'Есть ключ: <<obj ''key''>>'"))
        self.assertEqual(logic("x = iif(a > b, 1, 2) + 3"), [])

    def test_chained_comparison(self) -> None:
        self.assertIn(("warn", "logic-true"), logic("if a = b = c: gt 'x'"))
        self.assertEqual(logic("x = a > b"), [])

    def test_obj_precedence(self) -> None:
        self.assertIn(("warn", "logic-precedence"), logic("if obj 'key' = 0: gt 'x'"))
        self.assertEqual(logic("if (obj 'key') = 0: gt 'x'"), [])

    def test_same_name_variables(self) -> None:
        self.assertIn(("warn", "logic-same-name"), logic("# a\nanswer = 42\n---\n# b\n$answer = 'x'\n---"))
        self.assertEqual(logic("pers = 1\n$pers[1] = 'Тугги'"), [])  # different cells in both versions
        self.assertIn(("warn", "logic-same-name"), logic("pers = 1\n$pers[0] = 'x'"))
        self.assertIn(("info", "logic-same-name"), logic("n[i] = 1\n$n[j] = 'x'"))

    def test_else_ends_the_statement(self) -> None:
        self.assertEqual(logic("s = 0\nif s < 10: s = s + 1 else win = 2\nx = 5 - s"), [])

    def test_isnum(self) -> None:
        self.assertEqual(logic("if isnum($s): gt 'x'"), [("info", "logic-isnum")])


class ConvertTest(unittest.TestCase):
    def test_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            game = [Location("start", [], ["usehtml = 1", "addqst 'mod.qsp'", "'<img src=\"Pics\\A.png\">'"], [])]
            archive = tmp_path / "Моя игра.aqsp"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("game.qsp", write_qsp(game))
                z.writestr("mod.qsp", write_qsp([Location("mod", [], ["killqst"], [])]))
                z.writestr("pics/a.png", b"png")
                z.writestr("config.xml", '<game width="504" height="680" title="Чашка кофе"/>'.encode("utf-16"))
                z.writestr("Thumbs.db", b"")
            out = tmp_path / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["convert", str(archive), "-o", str(out), "--aero-theme", str(tmp_path / "missing.html"), "--no-theme"])
            self.assertEqual(code, 0)
            cfg = (out / "game.cfg").read_text(encoding="utf-8")
            self.assertIn('title = "Чашка кофе"', cfg)
            self.assertIn("width = 504", cfg)
            self.assertIn('file = "game.qsp"', cfg)
            self.assertFalse((out / "Thumbs.db").exists())
            converted = read_qsp((out / "game.qsp").read_bytes())
            self.assertEqual(converted[0].code[1:], ["inclib 'mod.qsp'", "'<img src=\"pics/a.png\">'"])
            self.assertEqual(read_qsp((out / "mod.qsp").read_bytes())[0].code, ["freelib"])
            self.assertTrue((out / "CONVERSION_REPORT.md").is_file())

    def test_batch_streams_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            games = tmp_path / "games"
            games.mkdir()
            for name in ("один", "two"):
                with zipfile.ZipFile(games / f"{name}.aqsp", "w") as z:
                    z.writestr("game.qsp", write_qsp([Location("start", [], ["usehtml = 1", f"*pl '{name}'", "x = rand(5)"], [])]))
            (games / "broken.aqsp").write_bytes(b"PK\x05\x06" + b"\0" * 18)  # empty zip: no game inside
            out = tmp_path / "out"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["convert", str(games), "-o", str(out), "--jobs", "2", "--no-theme", "--jsonl"])
            self.assertEqual(code, 1)  # one game failed, the others are converted
            lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(sorted(line["ok"] for line in lines), [False, True, True])
            self.assertEqual(len((out / "summary.jsonl").read_text(encoding="utf-8").splitlines()), 3)
            self.assertIn("| один | OK |", (out / "SUMMARY.md").read_text(encoding="utf-8"))
            converted = read_qsp((out / "odin" / "game.qsp").read_bytes())
            self.assertEqual(converted[0].code[-1], "x = rand(0, 5)")

    def test_single_file_ignores_earlier_output_and_twins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "game.qsps").write_text("# start\n*pl 'a'\n- start\n", encoding="utf-8")
            (folder / "game.qsp").write_bytes(write_qsp([Location("start", [], ["*pl 'old'"], [])]))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["convert", str(folder / "game.qsps"), "-o", str(folder / "out1"), "--no-theme"]), 0)
                self.assertEqual(main(["convert", str(folder / "game.qsps"), "-o", str(folder / "out2"), "--no-theme"]), 0)
            self.assertFalse((folder / "out2" / "out1").exists())
            self.assertEqual(read_qsp((folder / "out2" / "game.qsp").read_bytes())[0].code, ["*pl 'a'"])
            report = (folder / "out2" / "CONVERSION_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("`game.qsp` is another copy of `game.qsps`", report)

    def test_theme_patch(self) -> None:
        source = '<qspider-theme name="qspider:aero">\n  <css-link src="qspider:themes/aero.css"></css-link>\n  <qsp-css-variable name="--font-size" from="FSIZE" type="unit" unit="px" default-value="12"></qsp-css-variable>'
        theme = build_theme(source, 18)
        self.assertIn('name="aero-compat"', theme)
        self.assertIn('<css-link src="aero-compat.css">', theme)
        self.assertIn('default-value="18"', theme)

    def test_swf_font_names(self) -> None:
        name = b"Georgia"
        font = bytes([1, 0, 0b00000011, 0, len(name)]) + name + bytes([5, 0])  # id, flags(bold|italic), lang, name, glyphs
        tag = ((75 << 6) | 0x3F).to_bytes(2, "little") + len(font).to_bytes(4, "little") + font
        body = bytes([0x08, 0, 0, 24, 1, 0]) + tag + b"\0\0"  # RECT (nbits=1), frame rate, frame count, tag, end
        swf = b"FWS" + bytes([10]) + (8 + len(body)).to_bytes(4, "little") + body
        self.assertEqual(swf_font_names(swf), [("Georgia", 5, True, True)])


if __name__ == "__main__":
    unittest.main()
