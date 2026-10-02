# aero2qspider

Semi-automatic conversion of [AeroQSP](https://old.qsp.org/misc/aero/help.htm) games (`.aqsp`) for the
[qSpider](https://github.com/QSPFoundation/qspider) player.

AeroQSP is a Flash player built on QSP 5.7. qSpider can open `.aqsp` files directly (its `aero` mode), but it runs
QSP 5.9 in a browser, so some games look or behave differently. aero2qspider does the mechanical part of the port,
checks the result on both engines and writes a report of everything that still needs a human. Many games can be
converted at once; they are processed concurrently and every result is printed as soon as it is ready.

## Features

- Reads `.aqsp`/`.zip` archives, game folders and `.qsp`/`.qsps` files, and picks the main game file the way
  AeroQSP does (`game.qsp`, otherwise the first `.qsp`).
- Fixes code and texts (`.qsp` encoding is byte-for-byte compatible with `@qsp/converters`):

  | What                                                                  | Fix                                                                                     |
  | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
  | `ADDQST` / `KILLQST` (removed in QSP 5.9)                              | → `INCLIB` / `FREELIB`                                                                  |
  | `RAND(n)` (0..n in 5.7, 1..n in 5.9)                                   | → `RAND(0, n)`, the old range                                                           |
  | Unary plus: `gs 'add', +3` (a syntax error in 5.9)                     | → `gs 'add', 3`                                                                         |
  | `func('x')` as a statement (an empty line in 5.7, nothing in 5.9)      | → `*pl func('x')`, the same output in both                                              |
  | `INSTR`/`ARRPOS`/`ARRCOMP` with the old argument order (start first)   | the start moves to the end; ambiguous calls are reported                                |
  | `$STAT_FORMAT` (qSpider reads `$STATS_FORMAT`)                         | renamed                                                                                 |
  | Effects such as `Fade` or `PIXELS`                                     | lower case; `pixels`/`h_blinds`/`v_blinds` (missing in qSpider) → `--effect-fallback`   |
  | Paths like `content\Img.PNG`                                           | `\` → `/`, letter case of the real file (matters on case-sensitive web servers)        |
  | `$…BACKIMAGE = 'content/.gif'` (a file that does not exist)            | → `''`: looks the same in both players but avoids a 404 in qSpider                     |
  | `<font color="orange">`                                                | the AeroQSP colour (`orange` is `#CC3232` in Aero, not the CSS orange)                  |
  | Unitless lengths in `$STYLESHEET` (`font-size: 14`)                    | → `14px`                                                                                |
  | `config.xml` (any encoding)                                            | screen size and title → `game.cfg`                                                      |

  Expression fixes also apply inside `<<…>>` and inside `<a href="exec:…">` links in strings.

- Warns about code that behaves differently in QSP 5.7 and 5.9 (see [below](#qsp-57-vs-59)).
- Creates an `aero-compat` theme based on qSpider's aero theme: `FSIZE` defaults to 18 as in AeroQSP (qSpider uses
  12), background images tile from the top-left corner as in Flash, empty panels let clicks through to links under
  them, and tables that contain `<center>` stretch to the full width as Flash rendered them.
- Writes `game.cfg`, the fixed `.qsp` and a readable `.qsps`, the resources (without `.swf`, `Thumbs.db`, `.qproj`
  and `config.xml`) and `CONVERSION_REPORT.md` with file and line references into the `.qsps`.
- `--smoke` plays the converted game randomly on `@qsp/wasm-engine`, the QSP 5.9 engine qSpider uses, and reports
  runtime errors.
- `--compare` plays the **original** game on libqsp 5.7 and the **converted** game on QSP 5.9 side by side and
  reports the first step where they differ (see [Paired run](#paired-run)).

Reported but not fixed (no safe automatic fix): embedded SWF fonts (with the font names found in the SWF), system
fonts the game relies on, `EXEC 'effect:…'`, `DISABLESUBEX`, missing files, global selectors in `$STYLESHEET`, the
`leading` property, games without `USEHTML = 1`, `SCROLL_SPEED`/`DISABLEAUTOREF`.

## QSP 5.7 vs 5.9

AeroQSP 1.0 was compiled from the libqsp trunk of January 2011 (QSP 5.7.0); qSpider uses QSP 5.9.5. Every
difference below was confirmed by running the same code on both engines:

| Code                                            | QSP 5.7 (AeroQSP)       | QSP 5.9 (qSpider)  | aero2qspider                              |
| ----------------------------------------------- | ----------------------- | ------------------ | ----------------------------------------- |
| `*pl 1 = 1`, `obj 'a'`, `loc 'x'`, `isnum('5')` | `-1`                    | `1`                | -                                         |
| `x = 5 + (3 > 1)`                               | `4`                     | `6`                | warning: a condition in arithmetic        |
| `if (obj 'key') = -1`                           | true                    | false              | warning: a condition compared with -1     |
| `flag = 1`, `if no flag`                        | true (`no 1` is -2)     | false              | warning: `NO` of a number                 |
| `gold = 2`, `keys = 1`, `if gold and keys`      | false (`2 and 1` is 0)  | true               | warning: `AND` of two numbers             |
| `3 or 4`                                        | `7`                     | `1`                | - (the truth value is the same)           |
| `a = 5`, `$a = 'x'`, `*pl a`                    | `5`                     | `0`                | warning: `x` and `$x` with the same index |
| `pers = 1`, `$pers[1] = '…'`                    | different cells         | different cells    | no warning                                |
| `obj 'a' = 1`                                   | `obj ('a' = 1)`         | `(obj 'a') = 1`    | warning: `OBJ`/`LOC` precedence           |
| `1 = 1 = 1`                                     | `0`                     | `1`                | warning: chained comparison               |
| `*pl a > b`, `'<<obj ''x''>>'`                  | prints `-1`             | prints `1`         | warning: a printed condition              |
| `RAND(3)`                                       | 0..3                    | 1..3               | fixed → `RAND(0, 3)`                      |
| `x = +3`, `gs 'f', +3`                          | `3`                     | syntax error       | fixed → `x = 3`                           |
| `func('noop')` without a `RESULT`               | prints an empty line    | prints nothing     | fixed → `*pl func('noop')`                |
| `ISNUM('')`                                     | true                    | false              | note                                      |

These work the same: `GS`/`GT`/`FUNC`/`DYNAMIC`/`DYNEVAL` with arguments, `ARGS`/`RESULT`, `$ONNEWLOC`, `MENU`,
`INPUT`, `KILLVAR`/`KILLALL`/`COPYARR`, string functions, `IF`/`ELSEIF`, `JUMP`, division and `MOD`, comparison of
strings and numbers, `RAND(a, b)`.

The warnings come from static heuristics, so they can miss cases or warn about code that is fine (for example, when a
variable only ever holds 0 or 1). Each warning points at a line of the `.qsps` and suggests an explicit form that
works the same in both versions. The paired run catches what the heuristics cannot see.

## Paired run

`--compare` checks the converted game against the original on the engines themselves:

1. The original game is instrumented for libqsp 5.7 and the converted game for QSP 5.9: `RAND`, `RND` and
   `MSECSCOUNT` are replaced with a deterministic generator and clock, so random events match in both engines.
   One-argument `RAND(n)` keeps the range of each engine, so an unfixed `RAND(n)` still shows up as a difference.
2. Both games are driven step by step with the same random moves: actions, object clicks, `exec:` links, numbered
   action links and timer ticks. `MSG`, `MENU` and `INPUT` get the same answers.
3. After every step the visible state is compared: main and stats texts, actions, objects, messages, menus, played
   files, shown images and errors. Differences the conversion introduces on purpose (path separators, letter case,
   Aero colour names, the code of `exec:` links) are ignored.
4. The first difference is reported with the steps that led to it and a diff of the texts. When both engines stop
   with the same error, both restart and the run goes on.

The engines are installed once with `aero2qspider setup`:

- QSP 5.9: `@qsp/wasm-engine` from npm, run by Node.js;
- QSP 5.7: libqsp at commit [`5ebd0ae`](https://github.com/QSPFoundation/qsp/commit/5ebd0ae3e2a57f2c9737db59501d29a9642d0798)
  (25 January 2011, the revision AeroQSP 1.0 was built from), downloaded and compiled with a small step driver.

## Requirements

- Python 3.12 or newer. The converter uses only the standard library.
- For `--smoke` and `--compare`: Node.js 18+ with npm.
- For `--compare`: a C compiler (gcc or clang) and git or network access to GitHub. The libqsp 5.7 build is tested on
  Linux; on Windows use WSL.

## Installation

```sh
pipx install git+https://github.com/<owner>/aero2qspider
# or, from a clone
pip install .

# only for --smoke and --compare
aero2qspider setup
aero2qspider setup --check
```

`setup` installs into `~/.cache/aero2qspider` (`%LOCALAPPDATA%\aero2qspider` on Windows, `~/Library/Caches/aero2qspider`
on macOS); set `AERO2QSPIDER_HOME` to use another folder. `--qsp-source DIR` builds libqsp 5.7 from a local checkout
of the `qsp` folder, and `--cc` selects the compiler.

## Usage

```sh
aero2qspider convert koboldia_1.1.aqsp -o koboldia

# use an edited .qsps as the main game code, check the result on both engines, pack it into a .zip
aero2qspider convert koboldia_1.1.aqsp --source koboldia.qsps -o koboldia --smoke --compare --zip

# many games: a folder with archives (or a list of files), four at a time
aero2qspider convert games/ -o converted --jobs 4 --compare
aero2qspider convert games/*.aqsp -o converted --jsonl > results.jsonl
```

`python -m aero2qspider` works as well. Open the result in qSpider as a folder or archive with `game.cfg`, or put it into
the `game` folder of a standalone build.

### Batch mode

With several games (or a folder of `.aqsp`/`.zip` files) every game goes to `OUTPUT/<archive-name>/`:

- conversion runs in a process pool (`--jobs`, by default the number of CPUs up to 8) driven by `asyncio`;
- engine checks run as asynchronous subprocesses (`--engine-jobs` games at a time; the seeds of one game run in
  parallel too);
- a line (or a JSON line with `--jsonl`) is printed for every finished game and appended to `OUTPUT/summary.jsonl`;
- `OUTPUT/SUMMARY.md` gets a table of all games and the most frequent warnings;
- a broken game does not stop the others; the exit code is 1 if any game failed.

### Options

| Option                                                       | Meaning                                                               |
| ------------------------------------------------------------ | --------------------------------------------------------------------- |
| `--source FILE.qsps`                                         | code of the main game file from this `.qsps` (single game only)       |
| `--title`, `--id`                                            | title and id for `game.cfg` (single game only)                        |
| `--entry qsp\|qsps`                                          | the game file `game.cfg` points to                                    |
| `--font-size N`                                              | default `FSIZE` of the theme (18)                                     |
| `--aero-theme PATH\|URL\|latest`                             | the qSpider aero theme to start from (default: the bundled copy)      |
| `--effect-fallback NAME`                                     | replacement for `pixels`/`h_blinds`/`v_blinds` (`fade`)               |
| `--no-theme`, `--no-fix-paths`, `--no-fix-colors`,           | turn single steps off                                                 |
| `--no-fix-stat-format`, `--no-fix-missing-skin-images`,      |                                                                       |
| `--no-fix-rand`, `--no-fix-bare-calls`, `--no-logic-check`   |                                                                       |
| `--smoke`, `--smoke-steps`, `--smoke-seeds`                  | random run on QSP 5.9                                                 |
| `--compare`, `--compare-steps`, `--compare-seeds`            | paired run on QSP 5.7 and 5.9                                         |
| `-j/--jobs`, `--engine-jobs`, `--jsonl`                      | concurrency and streaming output                                      |
| `--zip`                                                      | also pack the result into a `.zip` (UTF-8 file names)                 |
| `--force`                                                    | write into a non-empty output folder                                  |

## Limitations

- Screen scaling to the window (as Flash did) is not emulated: the game takes exactly `width`×`height` pixels.
- SWF fonts are not converted: use the original font or export it from the SWF (for example with JPEXS FFDec) and
  list it in `[game.resources] fonts` (`game.cfg` contains a template).
- Text metrics depend on the player's fonts: without the game's font (Georgia, Lucida Console…) the browser picks
  another one, and the text may not fit the panels. Add web fonts.
- The paired run explores the game randomly, so it finds differences only in the parts it reaches. It does not
  compare the layout, only the content of the panels.
- Paths assembled in code from pieces (`'music/' + $name`) are fixed only for known folders.

## Development

```sh
pip install -e . ruff
ruff check . && ruff format --check .
python -m unittest discover -s tests     # the paired-run tests are skipped until `aero2qspider setup`
```

## License

MIT, see [LICENSE](LICENSE).

The bundled `src/aero2qspider/data/qspider-aero.html` is the aero theme of qSpider, © Sergii Kostyrko, MIT
([license](src/aero2qspider/data/qspider-LICENSE)). libqsp (LGPL/GPL) and `@qsp/wasm-engine` are not distributed
with aero2qspider: `aero2qspider setup` downloads them on your machine.
