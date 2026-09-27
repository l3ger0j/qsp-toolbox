"""Load saves written by qspsav into the real engines.

Skipped unless the engine libraries are given:
    QSPSAV_LIB570=/path/libqsp-legacy.so QSPSAV_LIB59=/path/libqsp.so python -m unittest
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import qspsav
from qspsav import convert, jsonio
from qspsav.tables import crc_570, crc_59

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
LIBS = {"5.7.0": os.environ.get("QSPSAV_LIB570"), "5.9": os.environ.get("QSPSAV_LIB59")}


def run_engine(engine, game, save_data, variables=(), actions=()):
    with tempfile.TemporaryDirectory() as tmp:
        save = Path(tmp) / "state.sav"
        save.write_bytes(save_data)
        cmd = [sys.executable, str(ROOT / "tools" / "engines.py"), "run", "--engine", engine,
               "--lib", LIBS[engine], "--game", str(FIXTURES / game), "--open-save", str(save),
               "--library", f"lib.qsp={FIXTURES / 'lib.qsp'}"]
        for name in variables:
            cmd += ["--var", name]
        for index in actions:
            cmd += ["--action", str(index)]
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=ROOT).stdout
    return json.loads(out)


@unittest.skipUnless(all(LIBS.values()), "set QSPSAV_LIB570 and QSPSAV_LIB59 to run engine tests")
class EngineTest(unittest.TestCase):
    def test_fixtures_load_as_expected(self):
        for name, info in MANIFEST.items():
            with self.subTest(name):
                result = run_engine(info["engine"], info["game"], (FIXTURES / name).read_bytes(), info["vars"])
                self.assertEqual(result["ok"], info["loadable"], result)
                if info["loadable"]:
                    self.assertEqual(result["vars"], info["vars"])

    def test_rewritten_saves_load(self):
        # read -> JSON -> edit -> write, then the engine must see the edit.
        for name in ("full.5.7.0.sav", "full.5.9.sav"):
            with self.subTest(name):
                info = MANIFEST[name]
                save, _ = qspsav.read_save((FIXTURES / name).read_bytes())
                save = jsonio.loads(jsonio.dumps(save))
                var = next(v for v in save.variables if v.name == "NAME")
                if info["engine"] == "5.9":
                    var.values[0].value = "Петя"
                else:
                    var.values[0].str = "Петя"
                self.assertEqual([i for i in qspsav.validate_save(save) if i.level == "error"], [])
                result = run_engine(info["engine"], info["game"], qspsav.write_save(save), ["NAME", "V300"])
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["vars"]["NAME"], [[0, "Петя"]])
                self.assertEqual(result["vars"]["V300"], [[300, ""]])

    def test_cp1251_save_loads_in_59(self):
        info = MANIFEST["ansi_game.5.9.sav"]
        save, _ = qspsav.read_save((FIXTURES / "ansi_game.5.9.sav").read_bytes())
        save.encoding = qspsav.Encoding.CP1251
        result = run_engine("5.9", info["game"], qspsav.write_save(save), info["vars"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["vars"], info["vars"])

    def test_validator_errors_match_engine(self):
        # Each broken save: our validator reports an error and the engine
        # either refuses the save or loses the value.
        s59, _ = qspsav.read_save((FIXTURES / "full.5.9.sav").read_bytes())
        s57, _ = qspsav.read_save((FIXTURES / "full.5.7.0.sav").read_bytes())

        wrong_crc = copy.deepcopy(s59)
        wrong_crc.game_crc += 1
        wrong_bucket = copy.deepcopy(s59)
        next(v for v in wrong_bucket.variables if v.name == "NAME").bucket += 1
        sequential = copy.deepcopy(s57)
        for slot, var in enumerate(sequential.variables):
            var.slot = slot

        game = (FIXTURES / "main.qsp").read_bytes()
        crc = {"5.7.0": crc_570(game), "5.9": crc_59(game)}
        cases = [("5.9", wrong_crc, False), ("5.9", wrong_bucket, True), ("5.7.0", sequential, True)]
        for engine, save, loads in cases:
            with self.subTest(engine=engine):
                self.assertTrue(any(i.level == "error" for i in qspsav.validate_save(save, crc[engine])))
                result = run_engine(engine, "main.qsp", qspsav.write_save(save), ["NAME"])
                self.assertEqual(result["ok"], loads, result)
                if loads:
                    self.assertEqual(result["vars"]["NAME"], [], "the engine must not find the variable")


@unittest.skipUnless(all(LIBS.values()), "set QSPSAV_LIB570 and QSPSAV_LIB59 to run engine tests")
class ConvertedInEngineTest(unittest.TestCase):
    """Converted saves loaded into the target engine."""

    PAIRS = ["start", "full", "ansi_game", "old_format", "in_library", "both_parts"]

    def convert(self, name, source, target):
        from tests.test_convert import context, load
        info = MANIFEST[f"{name}.{source}.sav"]
        save = load(f"{name}.{source}.sav")
        ctx = context(save, source, target, info["game"])
        result, issues = (convert.to_59 if target == "5.9" else convert.to_570)(save, ctx)
        self.assertEqual([i for i in issues if i.level == "error"], [])
        return info, qspsav.write_save(result)

    def test_values_survive(self):
        for source, target in (("5.7.0", "5.9"), ("5.9", "5.7.0")):
            for name in self.PAIRS:
                with self.subTest(name, source=source):
                    info, data = self.convert(name, source, target)
                    result = run_engine(target, info["game"], data, info["vars"])
                    self.assertTrue(result["ok"], result)
                    expected = dict(info["vars"])
                    if name == "both_parts" and source == "5.7.0":
                        # 5.9 keeps one value per element: the number for X (the game
                        # uses "x"), the string for ARR[2] (the game never uses it).
                        expected["X"] = [[5, ""]]
                        expected["ARR"] = [[0, ""], [0, ""], [0, "четыре"]]
                    self.assertEqual(result["vars"], expected)

    def test_converted_actions_run(self):
        for source, target in (("5.7.0", "5.9"), ("5.9", "5.7.0")):
            with self.subTest(source=source):
                info, data = self.convert("full", source, target)
                # action 3 is the multi-line ACT: x = 1, $y = 'два'
                result = run_engine(target, info["game"], data, ["X", "Y"], actions=[3])
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["vars"], {"X": [[1, ""]], "Y": [[0, "два"]]})

    def test_unprepared_code_breaks_in_59(self):
        # Why action code must be upper-cased: 5.9 does not prepare code it
        # reads from a save, so "x = 1" assigns to a different variable "x".
        from tests.test_convert import context, load
        save = load("full.5.7.0.sav")
        result, _ = convert.to_59(save, context(save, "5.7.0", "5.9"))
        for act, raw in zip(result.actions, save.actions):
            for line, raw_line in zip(act.lines, raw.lines):
                line.code = raw_line.code
        out = run_engine("5.9", "main.qsp", qspsav.write_save(result), ["X"], actions=[3])
        self.assertEqual(out["vars"]["X"], [[5, ""]], "X must be untouched when the code is not prepared")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(all(LIBS.values()), "set QSPSAV_LIB570 and QSPSAV_LIB59 to run engine tests")
class VerifyTest(unittest.TestCase):
    """qspsav.verify against the engines: clean saves pass, broken ones are caught."""

    def verify(self, save, game="main.qsp"):
        from qspsav.game import build_location_table, read_game
        from qspsav.verify import verify_save
        engine = "5.7.0" if isinstance(save, qspsav.v570.Save) else "5.9"
        table = build_location_table(engine, read_game((FIXTURES / game).read_bytes()), game, save.includes, FIXTURES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sav"
            path.write_bytes(qspsav.write_save(save))
            issues, _ = verify_save(save, path, FIXTURES / game, {"lib.qsp": FIXTURES / "lib.qsp"}, table,
                                    Path(LIBS[engine]))
        return [i for i in issues if i.level == "error"]

    def test_fixtures(self):
        for name, info in MANIFEST.items():
            with self.subTest(name):
                save, _ = qspsav.read_save((FIXTURES / name).read_bytes())
                self.assertEqual(self.verify(save, info["game"]) == [], info["loadable"])

    def test_broken_saves_are_caught(self):
        s59, _ = qspsav.read_save((FIXTURES / "full.5.9.sav").read_bytes())
        s57, _ = qspsav.read_save((FIXTURES / "full.5.7.0.sav").read_bytes())

        wrong_bucket = copy.deepcopy(s59)
        next(v for v in wrong_bucket.variables if v.name == "NAME").bucket += 1
        no_groups = copy.deepcopy(s59)
        no_groups.groups = []
        unsorted = copy.deepcopy(s59)
        next(v for v in unsorted.variables if v.name == "B").indices.reverse()
        no_prefix = copy.deepcopy(s59)
        for ix in next(v for v in no_prefix.variables if v.name == "B").indices:
            ix.key = ix.key[1:]
        sequential = copy.deepcopy(s57)
        for slot, var in enumerate(sequential.variables):
            var.slot = slot

        cases = {"wrong bucket": (wrong_bucket, "does not see it"), "no object groups": (no_groups, "OBJ()"),
                 "unsorted indices": (unsorted, "index"), "keys without '$'": (no_prefix, "index"),
                 "sequential 5.7.0 slots": (sequential, "does not see it")}
        for what, (save, text) in cases.items():
            with self.subTest(what):
                found = self.verify(save)
                self.assertTrue(any(text in i.message for i in found), found)

    def test_batched_runs_match_single_runs(self):
        # run_many() checks several saves per engine process; the engine
        # resets its state when it opens a save, so the results must be the
        # same as with one process per save, also after a save that fails.
        import asyncio

        from qspsav.engine import run_in_subprocess, run_many
        from qspsav.verify import inspect_job
        jobs = []
        for name, info in MANIFEST.items():
            save, _ = qspsav.read_save((FIXTURES / name).read_bytes())
            jobs.append(inspect_job(save, FIXTURES / name, FIXTURES / info["game"], {"lib.qsp": FIXTURES / "lib.qsp"},
                                    Path(LIBS[info["engine"]])))
        single = [run_in_subprocess(job) for job in jobs]
        self.assertIn(False, [r["ok"] for r in single])  # a failing save sits between others
        for workers in (1, 3):
            with self.subTest(workers=workers):
                self.assertEqual(asyncio.run(run_many(jobs, workers)), single)
        # An engine process that dies fails its own job only.
        dying = dict(jobs[0], open_save=str(FIXTURES / "no-such.sav"))
        results = asyncio.run(run_many([jobs[0], dying, jobs[0]], 1))
        self.assertEqual([r["ok"] for r in results], [True, False, True])
        self.assertEqual(results[1]["failed"], "engine process")

    def test_convert_verify_cli(self):
        from qspsav.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("full.5.7.0.sav", "full.5.9.sav", "in_library.5.7.0.sav"):
                with self.subTest(name):
                    code = main(["convert", str(FIXTURES / name), "--game", str(FIXTURES / "main.qsp"),
                                 "-o", tmp, "--verify", "--lib570", LIBS["5.7.0"], "--lib59", LIBS["5.9"]])
                    self.assertEqual(code, 0)
            # Several saves at once: converted first, then checked in parallel.
            names = ["start.5.7.0.sav", "full.5.7.0.sav", "full.5.9.sav", "in_library.5.9.sav"]
            report = Path(tmp) / "report.txt"
            code = main(["convert", *(str(FIXTURES / n) for n in names), "--game", str(FIXTURES / "main.qsp"),
                         "-o", str(Path(tmp) / "many"), "--verify", "-j", "2", "--report", str(report),
                         "--lib570", LIBS["5.7.0"], "--lib59", LIBS["5.9"]])
            self.assertEqual(code, 0)
            self.assertEqual(len(list((Path(tmp) / "many").iterdir())), len(names))
            self.assertEqual(report.read_text(encoding="utf-8").count("== "), len(names))
            self.assertEqual(main(["verify", *(str(p) for p in (Path(tmp) / "many").iterdir()),
                                   "--game", str(FIXTURES / "main.qsp"), "-j", "2",
                                   "--lib570", LIBS["5.7.0"], "--lib59", LIBS["5.9"]]), 0)
