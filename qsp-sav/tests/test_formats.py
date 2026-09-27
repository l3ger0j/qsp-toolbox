"""Reader/writer/validator tests on saves produced by the real engines."""

import copy
import json
import unittest
from pathlib import Path

import qspsav
from qspsav import jsonio, v59, v570
from qspsav.codec import Encoding, SaveFormatError
from qspsav.tables import crc_570, crc_59, var_bucket_59, var_window_570

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def load(name):
    data = (FIXTURES / name).read_bytes()
    save, issues = qspsav.read_save(data)
    return data, save, issues


def game_crc(name):
    info = MANIFEST[name]
    data = (FIXTURES / info["game"]).read_bytes()
    return crc_570(data) if info["engine"] == "5.7.0" else crc_59(data)


def errors(save, crc=None):
    return [i for i in qspsav.validate_save(save, crc) if i.level == "error"]


class EngineFixturesTest(unittest.TestCase):
    def test_detect_engine(self):
        for name, info in MANIFEST.items():
            with self.subTest(name):
                self.assertEqual(qspsav.detect_engine((FIXTURES / name).read_bytes()), info["engine"])

    def test_byte_exact_round_trip(self):
        for name in MANIFEST:
            with self.subTest(name):
                data, save, issues = load(name)
                self.assertEqual(issues, [])
                self.assertEqual(qspsav.write_save(save), data)

    def test_json_round_trip(self):
        for name in MANIFEST:
            with self.subTest(name):
                data, save, _ = load(name)
                text = jsonio.dumps(save)
                text.encode("utf-8")  # must be valid UTF-8 even with lone surrogates in the save
                self.assertEqual(qspsav.write_save(jsonio.loads(text)), data)

    def test_crc_matches_engine(self):
        for name in MANIFEST:
            with self.subTest(name):
                _, save, _ = load(name)
                self.assertEqual(save.game_crc, game_crc(name))

    def test_validation_predicts_engine(self):
        # Engine-produced saves validate cleanly exactly when the engine loads them.
        for name, info in MANIFEST.items():
            with self.subTest(name):
                _, save, _ = load(name)
                self.assertEqual(errors(save, game_crc(name)) == [], info["loadable"])

    def test_hashes_match_engine(self):
        for name, info in MANIFEST.items():
            _, save, _ = load(name)
            for var in save.variables:
                with self.subTest(name, var=var.name):
                    if info["engine"] == "5.9":
                        self.assertEqual(var.bucket, var_bucket_59(var.name))
                    else:
                        start = var_window_570(var.name)
                        self.assertTrue(start <= var.slot < start + 50)

    def test_values_match_engine(self):
        # Values the engine reported through its API equal what we parsed.
        for name, info in MANIFEST.items():
            _, save, _ = load(name)
            by_name = {v.name: v for v in save.variables}
            for var_name, expected in info["vars"].items():
                with self.subTest(name, var=var_name):
                    var = by_name.get(var_name)
                    got = [] if var is None else [_as_num_str(value) for value in var.values]
                    self.assertEqual(got, [list(item) for item in expected])

    def test_full_save_content(self):
        _, s59, _ = load("full.5.9.sav")
        _, s57, _ = load("full.5.7.0.sav")
        for s in (s59, s57):
            self.assertEqual(s.playlist, ["music.mp3*50", "sfx.wav"])
            self.assertEqual(s.includes, ["lib.qsp"])
            self.assertEqual((s.sel_action, s.sel_object, s.input_text, s.timer_ms), (1, 2, "текст ввода", 1000))
            self.assertIn("\ud800", s.main_desc)
            self.assertIn("😀", s.main_desc)
        self.assertEqual(s59.cur_loc, "start")
        self.assertEqual(s57.cur_loc, 0)
        self.assertEqual(s59.windows, v59.WIN_MAIN | v59.WIN_VARS | v59.WIN_OBJS | v59.WIN_VIEW)
        self.assertEqual((s57.show_actions, s57.show_objects, s57.show_vars, s57.show_input),
                         (False, True, True, False))
        groups = {g.name: g for g in s59.groups}
        self.assertEqual(groups["МЕЧ"].objs_count, 3)
        self.assertEqual(groups["ЩИТ"].updated_fields, v59.OBJ_UPDATED_DESC | v59.OBJ_UPDATED_IMAGE)
        types = {v.name: v.values[0].type for v in s59.variables if v.values}
        self.assertEqual(types["T"], v59.VarType.TUPLE)
        self.assertEqual(types["C"], v59.VarType.CODE)
        self.assertEqual(types["BL"], v59.VarType.BOOL)
        self.assertEqual(types["ARR2"], v59.VarType.UNDEF)
        keys = {v.name: [ix.key for ix in v.indices] for v in s59.variables}
        self.assertEqual(keys["B"], ["$ABC", "$KEY", "$NUM", "$КЛЮЧ"])
        self.assertEqual(keys["TK"], ["2\x05#1\x05$A"])  # tuple index
        keys57 = {v.name: [ix.key for ix in v.indices] for v in s57.variables}
        self.assertEqual(keys57["B"], ["ABC", "KEY", "NUM", "КЛЮЧ"])


def _as_num_str(value):
    if isinstance(value, v570.Value):
        return [value.num, value.str]
    base = v59.BASE_TYPE[value.type]
    if base is v59.VarType.NUM:
        return [value.value, ""]
    if base is v59.VarType.STR:
        return [0, value.value]
    return [0, ""]


class ValidatorTest(unittest.TestCase):
    def setUp(self):
        _, self.s59, _ = load("full.5.9.sav")
        _, self.s57, _ = load("full.5.7.0.sav")

    def test_clean(self):
        self.assertEqual(errors(self.s59), [])
        self.assertEqual(errors(self.s57), [])

    def test_wrong_bucket(self):
        s = copy.deepcopy(self.s59)
        s.variables[0].bucket = (s.variables[0].bucket + 1) % 512
        self.assertTrue(any("hashes to" in i.message for i in errors(s)))

    def test_unsorted_indices(self):
        s = copy.deepcopy(self.s59)
        var = next(v for v in s.variables if v.name == "B")
        var.indices.reverse()
        self.assertTrue(any("sorted" in i.message for i in errors(s)))

    def test_missing_group(self):
        s = copy.deepcopy(self.s59)
        s.groups = []
        self.assertTrue(any("no group" in i.message for i in errors(s)))

    def test_version_limits(self):
        s = copy.deepcopy(self.s59)
        s.version = "5.9.3"
        self.assertTrue(any(i.where == "version" for i in errors(s)))
        s = copy.deepcopy(self.s57)
        s.version = "5.7.1"
        self.assertTrue(any(i.where == "version" for i in errors(s)))

    def test_unreachable_slot_570(self):
        s = copy.deepcopy(self.s57)
        # Sequential slots: every variable outside its hash window.
        for slot, var in enumerate(s.variables):
            var.slot = slot
        self.assertTrue(any("not reachable" in i.message for i in errors(s)))


class Ansi59Test(unittest.TestCase):
    def test_cp1251_save_round_trip(self):
        _, save, _ = load("ansi_game.5.9.sav")
        save.encoding = Encoding.CP1251
        data = qspsav.write_save(save)
        self.assertEqual(data[:12], b"QSPSAVEDGAME")
        again, issues = qspsav.read_save(data)
        self.assertEqual(issues, [])
        self.assertIs(again.encoding, Encoding.CP1251)
        self.assertEqual(qspsav.write_save(again), data)
        self.assertEqual([v.name for v in again.variables], [v.name for v in save.variables])


class MalformedTest(unittest.TestCase):
    def test_truncated(self):
        data, _, _ = load("full.5.9.sav")
        with self.assertRaises(SaveFormatError):
            qspsav.read_save(data[: len(data) // 2])

    def test_not_a_save(self):
        with self.assertRaises(SaveFormatError):
            qspsav.read_save("QSPGAME\r\n".encode("utf-16-le"))

    def test_unsupported_version(self):
        data, _, _ = load("start.5.9.sav")
        with self.assertRaises(SaveFormatError):
            qspsav.read_save(data.replace("5.9.5".encode("utf-16-le"), "5.8.0".encode("utf-16-le"), 1))

    def test_bad_json(self):
        doc = jsonio.to_json(load("start.5.9.sav")[1])
        doc["save"]["variables"][0]["values"][0]["type"] = "NOPE"
        with self.assertRaises(SaveFormatError):
            jsonio.from_json(doc)
        doc = jsonio.to_json(load("start.5.9.sav")[1])
        doc["save"]["unknown"] = 1
        with self.assertRaises(SaveFormatError):
            jsonio.from_json(doc)


if __name__ == "__main__":
    unittest.main()
