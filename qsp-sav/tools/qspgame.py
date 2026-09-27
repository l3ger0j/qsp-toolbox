"""Minimal writer of .qsp game files (the "QSPGAME" format), for tests.

Layout, as read by qspOpenQuestFromData (5.7.0) and qspOpenGame (5.9):
    "QSPGAME"                 raw
    <editor/version string>   raw, not checked by the engines
    <password>                encoded
    <locations count>         encoded
    per location: name, description, code, actions count (all encoded)
        per action: image, description, code (all encoded)
    trailing CR LF
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qspsav.codec import Encoding, RecordWriter


@dataclass
class GameAction:
    desc: str
    code: str = ""
    image: str = ""


@dataclass
class GameLocation:
    name: str
    desc: str = ""
    code: str = ""
    actions: list[GameAction] = field(default_factory=list)


def build_game(locations: list[GameLocation], encoding: Encoding = Encoding.UCS2,
               password: str = "No") -> bytes:
    w = RecordWriter(encoding)
    w.raw("QSPGAME")
    w.raw("qspsav test game")
    w.text(password)
    w.int(len(locations))
    for loc in locations:
        w.text(loc.name)
        w.text(loc.desc)
        w.text(loc.code.replace("\n", "\r\n"))
        w.int(len(loc.actions))
        for act in loc.actions:
            w.text(act.image)
            w.text(act.desc)
            w.text(act.code.replace("\n", "\r\n"))
    return w.to_bytes()


def build_old_game(locations: list[GameLocation], encoding: Encoding = Encoding.UCS2) -> bytes:
    """The pre-QSPGAME layout: locations count as plain text, 29 more header
    records (not read by the engines), then per location name, description,
    code and exactly 20 actions of description and code (no images)."""
    w = RecordWriter(encoding)
    w.raw(str(len(locations)))
    for _ in range(29):
        w.raw("")
    for loc in locations:
        w.text(loc.name)
        w.text(loc.desc)
        w.text(loc.code.replace("\n", "\r\n"))
        actions = loc.actions + [GameAction("")] * (20 - len(loc.actions))
        for act in actions[:20]:
            w.text(act.desc)
            w.text(act.code.replace("\n", "\r\n"))
    return w.to_bytes()
