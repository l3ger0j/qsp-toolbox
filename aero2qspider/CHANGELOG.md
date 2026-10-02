# Changelog

## 2.0.0

- Standalone package with the `aero2qspider` command (`convert` and `setup` subcommands); Python 3.12+.
- `--compare`: paired run of the original game on libqsp 5.7 and the converted game on QSP 5.9 with deterministic
  `RAND`/`RND`/`MSECSCOUNT`; `aero2qspider setup` installs `@qsp/wasm-engine` and builds libqsp 5.7.
- New fixes found with the paired run: unary plus (a syntax error in QSP 5.9) and `FUNC`/`DYNEVAL` used as a
  statement (an empty line in QSP 5.7 only).
- `RAND(n)` and argument-order fixes also apply inside `<<…>>` and `exec:` links.
- The qSpider aero theme is bundled; `--aero-theme latest` downloads the current one.
- A single game file no longer pulls earlier output folders or other games from its folder into the conversion.
- All messages and reports are in English.

## 1.1.0

- Concurrent batch conversion (`asyncio` with a process pool), streaming results and `SUMMARY.md`.
- Static heuristics for QSP 5.7 vs 5.9 logic differences; `RAND(n)` fix.

## 1.0.0

- First version: archive import, code and text fixes, the `aero-compat` theme, `--smoke`.
