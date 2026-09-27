# qspsav

Read, write, check and convert save files of the [QSP](https://github.com/QSPFoundation) game engine:
**5.7.0** ([qsp-legacy](https://github.com/QSPFoundation/qsp-legacy)) and **5.9** ([qsp](https://github.com/QSPFoundation/qsp), 5.9.4 and later).

- unpack a save to JSON and pack it back, byte for byte;
- validate a save the way the engine does, plus the invariants the engine silently relies on;
- convert saves between 5.7.0 and 5.9, with a report of everything that could not be carried over exactly;
- optionally load the result into the real engines and compare.

Pure Python 3.11+, no dependencies.

## Install

```bash
pip install .
```

or run it from a checkout with `python -m qspsav ...`.

## Usage

```bash
qspsav info save.sav
qspsav unpack save.sav -o save.json            # lossless JSON
qspsav pack save.json -o save.sav --game game.qsp
qspsav validate saves/*.sav --game game.qsp

qspsav convert save.sav --game game.qsp        # -> save.to-5.9.sav (direction is detected)
qspsav convert saves/*.sav --game game.qsp -o out/ --report out/report.txt

qspsav game game.qsp --locations               # format, CRCs, location indices
```

`--game` is the game file the save belongs to. It provides the game CRC
stored in saves and the location list: 5.7.0 saves refer to locations by
index, 5.9 saves by name. Files included with `INCLIB`/`ADDQST` are looked up
next to the game (or in `--game-dir`); map a name explicitly with
`--include "libs\items.qsp=/path/to/items.qsp"`. If the target engine will
run a different game file (say, a version of the game ported to 5.9), pass it
as `--target-game`.

`pack` and `convert` refuse to write a save the engine would reject or
misread; `--force` writes it anyway. Findings are reported as:

- `error`: the engine rejects the save, or the game cannot reach some data;
- `warning`: data was changed or dropped;
- `note`: worth knowing, nothing was lost.

### Checking in the real engines

Build the engines (CMake, see their repositories) and point qspsav at the
libraries, with options or environment variables:

```bash
export QSPSAV_LIB570=/path/to/libqsp-legacy.so
export QSPSAV_LIB59=/path/to/libqsp.so

qspsav verify saves/*.sav --game game.qsp
qspsav convert saves/*.sav --game game.qsp -o out/ --verify
```

`verify` loads each save into its engine and compares what the engine sees
with the file: the save loads, current location, descriptions, actions,
objects (`OBJ`), every variable value and every value read by text index.
`convert --verify` also loads the original into the source engine and
compares the two engines' views, ignoring only what the conversion reported
as lost.

The checks run in parallel engine processes (`-j N`, default: the number of
CPUs). Each process loads the game once and checks a batch of saves; if a
save crashes an engine, only that save fails.

### Large games

Everything derived from the game (the parsed file, location tables, the
scan of the game code, the CRC) is computed once per run and shared by all
saves given on the command line, so convert a game's saves in one command:

```bash
qspsav convert saves/*.sav --game game.qsp -o out/ --verify
```

On a synthetic 1500-location game (13.7 MB), converting 8 saves takes about
1 s, or about 2 s with `--verify` (4 CPUs). Each further save adds roughly
40 ms, plus its engine check with `--verify`.
`tools/bench.py` generates such a game and times it.

## How saves are converted

| | 5.7.0 → 5.9 | 5.9 → 5.7.0 |
|---|---|---|
| Version | `5.9.4`: accepted by every 5.9.4+ player (`--version` to change) | `5.7.0` |
| Game CRC | recomputed for the target engine (the algorithms differ) | same |
| Current location | index → name | name → index |
| Windows | 4 flags → bit mask; `MAIN` on, `VIEW` on when a picture is set | bit mask → 4 flags |
| Action code | upper-cased outside strings and `{}`: 5.9 keeps code prepared that way and does not prepare code read from a save | unchanged; 5.9-only syntax is reported |
| Objects | object groups are built (`OBJ` and `COUNTOBJ` depend on them) | a `MODOBJ` title is dropped, its image is kept |
| Values | string → `STR`, number → `NUM`, neither → `UNDEF` (reads as both `0` and `''`); an element holding both keeps the form the game code uses | `NUM` → number, `BOOL` → −1/0 (5.7.0 truth is −1), strings → string, tuples dropped |
| Names, text indices | 5.9 upper case, hash bucket, keys prefixed with `$` | 5.7.0 upper case, hash slot, prefix removed; tuple keys dropped |

The conversion moves the save, not the game. A 5.7.0 game that uses both `x`
and `$x` as separate values behaves differently under 5.9, where they are one
value; such variables are listed as a `note`.

## JSON format

```json
{"format": "qsp-save", "engine": "5.9", "encoding": "utf-16le", "save": {"...": "..."}}
```

Fields follow the order of the file (see `qspsav/v570.py` and `qspsav/v59.py`).
5.9 values are `{"type": "STR", "value": "..."}` with types `TUPLE NUM BOOL
STR CODE VARREF UNDEF`. Text JSON cannot hold (a lone UTF-16 surrogate, which
the engines store happily) is written as `{"parts": ["text", 55296, "text"]}`.

## Known engine behaviour

Found while testing against the engines; qspsav reports these where they apply.

- An action added by `ACT` outside any location is saved with location −1,
  and both engines refuse to load such a save.
- qsp 5.9 looks up the current location before it re-includes library files,
  so a save made in a location from an included file loads with no current
  location. This happens with saves 5.9 writes itself. `verify` reports it
  as a note and does not compare the location in that case.
- The engines' upper-case tables differ, and 5.7.0 maps a few characters
  wrongly (`÷` → `×`, some accented Greek letters).

## Limitations

- 5.9 saves older than 5.9.4 (`QSP_GAMEMIN_VER`) and 5.8 saves are not supported.
- 5.7.0 saves are UCS-2 only (that is all qsp-legacy writes).
- Code built at run time (`DYNAMIC`) is not analysed. Choosing between a
  number and a string held by one element relies on the game's literal code.

## Development

```bash
python -m unittest discover -s tests -t .      # engine tests run when QSPSAV_LIB* are set
```

The test saves in `tests/fixtures` were produced by the engines themselves:
the same scenarios run in both, so a converted save can be compared with the
one the target engine wrote.

- `tools/make_fixtures.py`: rebuild the fixtures from the engine libraries;
- `tools/gen_tables.py`: regenerate `qspsav/_tables.py` (upper-case tables,
  variable hash, CRC) from the engines' C sources;
- `tools/engines.py`: drive an engine from the command line;
- `tools/bench.py`: generate a large game with saves and time `convert` on it;
- `tools/qspgame.py`: write small `.qsp` games for tests.
