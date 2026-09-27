"""Conversion tests.

The fixtures run the same scenario in both engines, so a converted save can be
compared with the one the target engine wrote itself.
"""

import json
import unittest
from pathlib import Path

import qspsav
from qspsav import convert, v59, v570
from qspsav.code import VarUsage, find_59_only_syntax, prepare_line_59, scan_game_usage
from qspsav.game import build_location_table, read_game, resolve_include

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
ONLY_59_VARS = {"T", "C", "BL", "E", "ARR2", "TK"}  # set by the 5.9-only part of the scenario


def load(name):
    return qspsav.read_save((FIXTURES / name).read_bytes())[0]


def context(save, source, target, game="main.qsp", includes=None):
    main = read_game((FIXTURES / game).read_bytes())
    includes = save.includes if includes is None else includes
    src = build_location_table(source, main, game, includes, FIXTURES)
    dst = build_location_table(target, main, game, includes, FIXTURES)
    games = [main] + [read_game(resolve_include(i, FIXTURES, {}).read_bytes()) for i in includes
                      if resolve_include(i, FIXTURES, {})]
    return convert.Context(src, dst, main.crc_for(target), scan_game_usage(games))


def to_59(name, game="main.qsp"):
    save = load(name)
    return convert.to_59(save, context(save, "5.7.0", "5.9", game))


def to_570(name, game="main.qsp"):
    save = load(name)
    return convert.to_570(save, context(save, "5.9", "5.7.0", game))


def errors(issues):
    return [i for i in issues if i.level == "error"]


def reads_equal(a, b):
    """5.9 values that give the same number and the same string."""
    def pair(v):
        base = v59.BASE_TYPE[v.type]
        if base is v59.VarType.NUM:
            return (v.value, "")
        if base is v59.VarType.STR:
            return (0, v.value)
        return ("tuple", repr(v.value))
    return [pair(v) for v in a] == [pair(v) for v in b]


class CodeTest(unittest.TestCase):
    def test_prepare_line(self):
        self.assertEqual(prepare_line_59("if x > 1: *pl 'Привет, <<$name>>' & $d = {set y = 'a'} & gs \"dd\""),
                         "IF X > 1: *PL 'Привет, <<$name>>' & $D = {set y = 'a'} & GS \"dd\"")
        self.assertEqual(prepare_line_59("s = 'it''s' & t"), "S = 'it''s' & T")
        self.assertEqual(prepare_line_59("a = {b {c} d} e"), "A = {b {c} d} E")
        self.assertEqual(prepare_line_59("x = 'unclosed"), "X = 'unclosed")

    def test_usage(self):
        u = VarUsage()
        u.add_code("$name = 'x' & *pl 'Тебе <<age>> лет, <<$name>>' & gold = gold + 1 & $x['k'] = 1 & x = 2")
        self.assertEqual(u.kind("NAME"), "str")
        self.assertEqual(u.kind("AGE"), "num")
        self.assertEqual(u.kind("X"), "both")
        self.assertIsNone(u.kind("NOPE"))

    def test_usage_strings(self):
        # Doubled quotes are escapes, <<...>> inside strings is code, an
        # unterminated string runs to the end of the code.
        u = VarUsage()
        u.add_code("*pl 'it''s <<$who>> and <<n1>>' & \"say \"\"<<n2>>\"\"\" & m = 1 & 'open <<$tail>> rest")
        self.assertEqual(u.kind("WHO"), "str")
        self.assertEqual(u.kind("N1"), "num")
        self.assertEqual(u.kind("N2"), "num")
        self.assertEqual(u.kind("M"), "num")
        self.assertEqual(u.kind("TAIL"), "str")
        self.assertIsNone(u.kind("IT"))
        self.assertIsNone(u.kind("REST"))

    def test_59_only_syntax(self):
        self.assertEqual(find_59_only_syntax("LOOP WHILE X < 3: %T = [1] & *PL 'loop'"),
                         ["LOOP", "WHILE", "%T (tuple variable)"])
        self.assertEqual(find_59_only_syntax("GT 'room'"), [])


class GameTest(unittest.TestCase):
    def test_formats(self):
        for name, fmt in (("main.qsp", False), ("main_ansi.qsp", False), ("old_format.qsp", True)):
            with self.subTest(name):
                game = read_game((FIXTURES / name).read_bytes())
                self.assertEqual(game.old_format, fmt)
                self.assertEqual(game.locations[0].name, "start")

    def test_crc_matches_engine_saves(self):
        for name, info in MANIFEST.items():
            with self.subTest(name):
                game = read_game((FIXTURES / info["game"]).read_bytes())
                self.assertEqual(load(name).game_crc, game.crc_for(info["engine"]))

    def test_location_table_with_library(self):
        main = read_game((FIXTURES / "main.qsp").read_bytes())
        table = build_location_table("5.7.0", main, "main.qsp", ["lib.qsp"], FIXTURES)
        self.assertEqual(table.names, ["start", "room", "Локация с пробелом", "acts", "libloc"])
        self.assertEqual(table.index_of("  LIBLOC "), 4)
        self.assertEqual(table.main_count, 4)

    def test_missing_include(self):
        main = read_game((FIXTURES / "main.qsp").read_bytes())
        table = build_location_table("5.7.0", main, "main.qsp", ["nope.qsp"], FIXTURES)
        self.assertFalse(table.complete)
        self.assertEqual(len(errors(table.issues)), 1)

    def test_include_resolution(self):
        self.assertEqual(resolve_include("LIB.QSP", FIXTURES, {}), FIXTURES / "lib.qsp")
        self.assertEqual(resolve_include("sub\\lib.qsp", FIXTURES, {}), FIXTURES / "lib.qsp")  # by bare name
        self.assertEqual(resolve_include("x", FIXTURES, {"x": Path("/y")}), Path("/y"))


class To59Test(unittest.TestCase):
    def test_matches_engine_save(self):
        conv, issues = to_59("full.5.7.0.sav")
        ref = load("full.5.9.sav")
        self.assertEqual(errors(issues), [])
        for field in ("game_crc", "sel_action", "sel_object", "view_path", "input_text", "main_desc", "vars_desc",
                      "cur_loc", "windows", "timer_ms", "playlist", "includes", "actions", "objects"):
            with self.subTest(field):
                self.assertEqual(getattr(conv, field), getattr(ref, field))
        # the 5.9 scenario also ran MODOBJ on the shield
        self.assertEqual([(g.name, g.objs_count) for g in conv.groups], [(g.name, g.objs_count) for g in ref.groups])
        ref_vars = {v.name: v for v in ref.variables if v.name not in ONLY_59_VARS}
        conv_vars = {v.name: v for v in conv.variables}
        self.assertEqual(conv_vars.keys(), ref_vars.keys())
        for name, var in ref_vars.items():
            with self.subTest(name):
                self.assertEqual(conv_vars[name].bucket, var.bucket)
                self.assertEqual(conv_vars[name].indices, var.indices)
                self.assertTrue(reads_equal(conv_vars[name].values, var.values))

    def test_other_fixtures(self):
        for name, game in (("start", "main.qsp"), ("ansi_game", "main_ansi.qsp"), ("old_format", "old_format.qsp"),
                           ("in_library", "main.qsp")):
            with self.subTest(name):
                conv, issues = to_59(f"{name}.5.7.0.sav", game)
                ref = load(f"{name}.5.9.sav")
                self.assertEqual(errors(issues), [])
                self.assertEqual((conv.game_crc, conv.cur_loc, conv.includes), (ref.game_crc, ref.cur_loc, ref.includes))

    def test_location_from_library(self):
        conv, _ = to_59("in_library.5.7.0.sav")
        self.assertEqual(conv.cur_loc, "libloc")

    def test_both_parts(self):
        conv, issues = to_59("both_parts.5.7.0.sav")
        x = next(v for v in conv.variables if v.name == "X")
        # main.qsp uses "x = 5", so the number is kept
        self.assertEqual(x.values, [v59.Variant(v59.VarType.NUM, 5)])
        self.assertTrue(any(i.where == "variable 'X'" and "both a number and a string" in i.message
                            for i in issues))
        arr = next(v for v in conv.variables if v.name == "ARR")
        self.assertEqual(arr.values[2], v59.Variant(v59.VarType.STR, "четыре"))  # not used in the game: string wins

    def test_act_outside_location_is_reported(self):
        _, issues = to_59("act_outside_location.5.7.0.sav")
        self.assertTrue(errors(issues))

    def test_missing_library_is_an_error(self):
        save = load("in_library.5.7.0.sav")
        ctx = context(save, "5.7.0", "5.9", includes=["missing.qsp"])
        _, issues = convert.to_59(save, ctx)
        self.assertTrue(any("included file" in i.message for i in errors(issues)))


class To570Test(unittest.TestCase):
    def test_matches_engine_save(self):
        conv, issues = to_570("full.5.9.sav")
        ref = load("full.5.7.0.sav")
        self.assertEqual(errors(issues), [])
        for field in ("game_crc", "sel_action", "sel_object", "view_path", "input_text", "main_desc", "vars_desc",
                      "cur_loc", "show_actions", "show_objects", "show_vars", "show_input", "timer_ms", "playlist",
                      "includes"):
            with self.subTest(field):
                self.assertEqual(getattr(conv, field), getattr(ref, field))
        for a, b in zip(conv.actions, ref.actions, strict=True):
            self.assertEqual((a.desc, a.image, a.location, a.act_index, [l.line_num for l in a.lines]),
                             (b.desc, b.image, b.location, b.act_index, [l.line_num for l in b.lines]))
        self.assertEqual([o.desc for o in conv.objects], [o.desc for o in ref.objects])
        ref_vars = {v.name: v for v in ref.variables}
        for var in conv.variables:
            if var.name in ONLY_59_VARS:
                continue
            with self.subTest(var.name):
                self.assertEqual((var.values, var.indices), (ref_vars[var.name].values, ref_vars[var.name].indices))

    def test_lossy_parts_are_reported(self):
        conv, issues = to_570("full.5.9.sav")
        text = "\n".join(map(str, issues))
        self.assertIn("'T'[0]: a tuple", text)
        self.assertIn("MODOBJ", text)
        self.assertIn("'TK'", text)
        bl = next(v for v in conv.variables if v.name == "BL")
        self.assertEqual(bl.values, [v570.Value(-1, "")])  # qsp-legacy's "true"

    def test_round_trip(self):
        # 5.7.0 -> 5.9 -> 5.7.0 keeps every value (slots may move, lookups still work).
        original = load("full.5.7.0.sav")
        up, _ = to_59("full.5.7.0.sav")
        down, issues = convert.to_570(up, context(up, "5.9", "5.7.0"))
        self.assertEqual(errors(issues), [])
        self.assertEqual({v.name: (v.values, v.indices) for v in down.variables},
                         {v.name: (v.values, v.indices) for v in original.variables})


if __name__ == "__main__":
    unittest.main()
