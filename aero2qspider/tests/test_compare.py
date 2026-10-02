"""Paired run: instrumentation and comparison; the engine tests run only after `aero2qspider setup`."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from aero2qspider.compare import RAND_LOCATION, canonical, canonical_state, error_key, instrument, instrument_code, paired_run
from aero2qspider.engines import node_command, node_problem, qsp57_command, qsp57_problem
from aero2qspider.qsp_format import Location, read_qsps, write_qsp


class InstrumentTest(unittest.TestCase):
    def test_rand_keeps_the_engine_range(self) -> None:
        self.assertEqual(instrument_code("x = rand(3)", "57"), "x = func('__a2q_rand', 0, 3)")
        self.assertEqual(instrument_code("x = rand(3)", "59"), "x = func('__a2q_rand', 1, 3)")

    def test_arguments_are_numbers(self) -> None:
        code = "x = Rand('<<a>>', '<<b>>') + rand(1, n + 1)"
        self.assertEqual(
            instrument_code(code, "59"),
            "x = func('__a2q_rand', ('<<a>>')*1, ('<<b>>')*1) + func('__a2q_rand', 1, (n + 1)*1)",
        )

    def test_subexpressions_rnd_and_time(self) -> None:
        code = "*pl 'r: <<rand(1, 2)>>' & t = msecscount & y = rnd"
        self.assertEqual(
            instrument_code(code, "57"),
            "*pl 'r: <<func(\"__a2q_rand\", 1, 2)>>' & t = __a2q_now & y = func('__a2q_rand', 1, 1000)",
        )

    def test_helper_location_is_added(self) -> None:
        locations = instrument([Location("start", ["<<rand(2)>>"], [], [])], "57")
        self.assertEqual(locations[0].description, ['<<func("__a2q_rand", 0, 2)>>'])
        self.assertEqual(locations[-1].name, RAND_LOCATION)


class CanonicalTest(unittest.TestCase):
    def test_hides_intended_differences(self) -> None:
        self.assertEqual(canonical('<img src="Pics\\A.png">'), canonical('<img src="pics/a.png">'))
        self.assertEqual(canonical("<font color=red>x</font>"), canonical("<font color=#FF0000>x</font>"))
        self.assertEqual(canonical("<a href=\"exec:gs 'f',+1\">go</a>"), canonical("<a href=\"exec:gs 'f',1\">go</a>"))
        self.assertNotEqual(canonical("Truth is -1"), canonical("Truth is 1"))

    def test_state_ignores_system_events(self) -> None:
        a = {"main": "A\r\nB", "events": [["system", "x"], ["msg", "Hi"]]}
        b = {"main": "a\nb", "events": [["msg", "hi"]]}
        self.assertEqual(canonical_state(a), canonical_state(b))

    def test_errors_match_by_location_and_message(self) -> None:
        a = {"error": {"location": "Room", "line": 36, "description": "Location not found!"}}
        b = {"error": {"location": "room", "line": 19, "description": "Location not found!"}}
        self.assertEqual(error_key(a), error_key(b))
        self.assertIsNone(error_key({"error": None}))


GAME = """# start
*pl "Start"
act "Roll": gt "roll"
act "Truth": gt "truth"
- start

# roll
*pl "You rolled <<rand(0, 3)>>"
act "Back": gt "start"
- roll

# truth
x = (2 > 1)
*pl "Truth is <<x>>"
act "Back": gt "start"
- truth
"""


@unittest.skipIf(qsp57_problem() or node_problem(), "engines are not installed; run `aero2qspider setup`")
class PairedRunTest(unittest.TestCase):
    def run_pair(self, code: str, steps: int = 60) -> list[object]:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            locations = read_qsps(code)
            for engine in ("57", "59"):
                (folder / engine).mkdir()
                (folder / engine / "game.qsp").write_bytes(write_qsp(instrument(locations, engine)))
            command57 = qsp57_command(folder / "57" / "game.qsp")
            command59 = node_command("qsp59-driver.mjs", str(folder / "59" / "game.qsp"))
            return [asyncio.run(paired_run(command57, command59, locations[0].name, steps, seed)) for seed in (1, 2, 3)]

    def test_finds_the_value_of_true(self) -> None:
        runs = self.run_pair(GAME)
        diverged = [r.divergence for r in runs if r.divergence]
        self.assertTrue(diverged)
        self.assertTrue(all(d.location59 == "truth" for d in diverged))
        self.assertIn("-Truth is -1", "\n".join(diverged[0].differences))

    def test_same_behaviour(self) -> None:
        runs = self.run_pair(GAME.replace("x = (2 > 1)", "x = 7"))
        self.assertEqual([r.divergence for r in runs], [None, None, None])
        self.assertEqual([r.problem for r in runs], ["", "", ""])
        self.assertTrue(all(r.steps == 60 for r in runs))


if __name__ == "__main__":
    unittest.main()
