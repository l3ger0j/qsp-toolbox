"""Reading .qsp game files and building the location table of a running game.

Layout (qspOpenQuestFromData in 5.7.0, qspOpenGame in 5.9):

* new format -- "QSPGAME", editor string, password, locations count, then per
  location: name, description, code, actions count, and per action: image,
  description, code;
* old format -- locations count (plain number) in the first record, 29 more
  header records, then per location: name, description, code and exactly 20
  actions of description and code.

Everything except the header strings is encoded like save records.

The engines keep locations in one array: the main game first, then every
included file (INCLIB / ADDQST) in inclusion order, skipping locations whose
name already exists. Saves of 5.7.0 refer to locations by index in that array,
saves of 5.9 by name, so conversion needs the same array.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PureWindowsPath

from .codec import Encoding, Issue, RecordReader, SaveFormatError, detect_encoding, parse_int, split_records
from .tables import crc_570, crc_59, upper_570, upper_59

GAME_ID = "QSPGAME"
OLD_FORMAT_HEADER = 30
OLD_FORMAT_ACTIONS = 20


@dataclass
class GameAction:
    image: str
    desc: str
    code: str


@dataclass
class GameLocation:
    name: str
    desc: str
    code: str
    actions: list[GameAction]


@dataclass
class Game:
    locations: list[GameLocation]
    encoding: Encoding
    old_format: bool
    data: bytes = field(repr=False, compare=False)  # the file, for the CRCs

    @cached_property
    def crc_570(self) -> int:
        """qspQstCRC as qsp-legacy computes it for this file (computed on first use: it is slow)."""
        return crc_570(self.data)

    @cached_property
    def crc_59(self) -> int:
        """The same for qsp 5.9."""
        return crc_59(self.data)

    def crc_for(self, engine: str) -> int:
        return self.crc_570 if engine == "5.7.0" else self.crc_59


def read_game(data: bytes) -> Game:
    if len(data) < 2:
        raise SaveFormatError("too short to be a QSP game")
    encoding = detect_encoding(data)
    r = RecordReader(split_records(data, encoding), encoding)
    first = r.raw("header")
    old_format = first != GAME_ID
    if old_format:
        count, _ = parse_int(first)
        if count <= 0:
            raise SaveFormatError("not a QSP game file")
        r.pos = OLD_FORMAT_HEADER
    else:
        r.pos = 3
        count = r.count("locations count")
    locations = []
    for i in range(count):
        name = r.text(f"location {i} name")
        desc = r.text(f"location {name!r} description")
        code = r.text(f"location {name!r} code")
        actions = []
        acts_count = OLD_FORMAT_ACTIONS if old_format else r.count(f"location {name!r} actions count")
        for j in range(acts_count):
            image = "" if old_format else r.text(f"location {name!r} action {j} image")
            actions.append(GameAction(image, r.text(f"location {name!r} action {j} description"),
                                      r.text(f"location {name!r} action {j} code")))
        locations.append(GameLocation(name, desc, code, actions))
    return Game(locations, encoding, old_format, data)


def location_key(name: str, engine: str) -> str:
    """How qspLocIndex() compares names: trimmed and upper-cased."""
    if engine == "5.7.0":
        return upper_570(name.strip(" \t"))
    return upper_59(name.strip(" \t\r\n"))


@dataclass
class LocationTable:
    """The engine's location array for a main game plus included files."""

    engine: str
    names: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # file each location came from
    main_count: int = 0
    complete: bool = True  # False when an included file could not be found
    issues: list[Issue] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)

    def add_game(self, game: Game, source: str, included: bool) -> None:
        existing = set(self._index)
        for loc in game.locations:
            key = location_key(loc.name, self.engine)
            # Included files skip names that were already loaded; duplicates
            # inside one file are all kept (the engine checks only against the
            # locations that existed before the file was opened).
            if included and key in existing:
                continue
            if key in self._index:
                self.issues.append(Issue("warning", f"location {loc.name!r}",
                                         f"duplicate name in {source}; lookups by name are ambiguous"))
            else:
                self._index[key] = len(self.names)
            self.names.append(loc.name)
            self.sources.append(source)
        if not included:
            self.main_count = len(self.names)

    def index_of(self, name: str) -> int | None:
        return self._index.get(location_key(name, self.engine))

    def name_of(self, index: int) -> str | None:
        return self.names[index] if 0 <= index < len(self.names) else None


class GameFiles:
    """Game files read so far, so that each one is read and parsed once.

    Also remembers directory listings for resolve_include(): a game folder
    with thousands of images is walked once, not once per save.
    """

    def __init__(self) -> None:
        self._games: dict[Path, Game] = {}
        self._listings: dict[Path, list[Path]] = {}

    def read(self, path: Path) -> Game:
        key = path.resolve()
        game = self._games.get(key)
        if game is None:
            game = self._games[key] = read_game(key.read_bytes())
        return game

    def files_under(self, directory: Path) -> list[Path]:
        files = self._listings.get(directory)
        if files is None:
            files = self._listings[directory] = [p for p in directory.rglob("*") if p.is_file()]
        return files


def resolve_include(stored: str, game_dir: Path, explicit: dict[str, Path],
                    files: GameFiles | None = None) -> Path | None:
    """Find the file an INCLIB/ADDQST path refers to.

    Tried in order: an explicit NAME=PATH mapping, the path relative to the
    game directory (with '\\' treated as a separator), the same path compared
    case-insensitively, and finally the bare file name anywhere under the
    game directory.
    """
    if stored in explicit:
        return explicit[stored]
    parts = PureWindowsPath(stored).parts
    candidate = game_dir.joinpath(*parts)
    if candidate.is_file():
        return candidate
    listing = (files or GameFiles()).files_under(game_dir)
    wanted = [p.lower() for p in parts]
    for path in listing:
        if [p.lower() for p in path.relative_to(game_dir).parts] == wanted:
            return path
    matches = [p for p in listing if p.name.lower() == wanted[-1]]
    return matches[0] if len(matches) == 1 else None


def build_location_table(engine: str, main: Game, main_name: str, includes: list[str],
                         game_dir: Path, explicit: dict[str, Path] | None = None,
                         files: GameFiles | None = None) -> LocationTable:
    files = files or GameFiles()
    table = LocationTable(engine)
    table.add_game(main, main_name, included=False)
    for stored in includes:
        path = resolve_include(stored, game_dir, explicit or {}, files)
        if path is None:
            table.complete = False
            table.issues.append(Issue("error", f"included file {stored!r}",
                                      f"not found under {game_dir}; locations after the main game cannot be "
                                      "resolved (pass --include NAME=PATH)"))
            break
        try:
            table.add_game(files.read(path), str(path), included=True)
        except SaveFormatError as exc:
            table.complete = False
            table.issues.append(Issue("error", f"included file {stored!r}", f"{path}: {exc}"))
            break
    return table
