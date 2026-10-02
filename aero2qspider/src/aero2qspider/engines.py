"""Engines used to check converted games: @qsp/wasm-engine (QSP 5.9) via Node.js and a native libqsp 5.7 build."""

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from importlib import resources
from pathlib import Path

# libqsp revision of 25 Jan 2011: the trunk AeroQSP 1.0 (26 Jan 2011) was compiled from, including its Flash bindings.
QSP57_COMMIT = "5ebd0ae3e2a57f2c9737db59501d29a9642d0798"
QSP_REPO = "https://github.com/QSPFoundation/qsp"
NODE_FILES = ("package.json", "smoke.mjs", "qsp59-driver.mjs")

Log = Callable[[str], None]


class EngineError(Exception):
    pass


def cache_dir() -> Path:
    if custom := os.environ.get("AERO2QSPIDER_HOME"):
        return Path(custom)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "aero2qspider"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "aero2qspider"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "aero2qspider"


def node_dir() -> Path:
    return cache_dir() / "node"


def qsp57_binary() -> Path:
    return cache_dir() / "qsp57" / ("qsp57-driver.exe" if sys.platform == "win32" else "qsp57-driver")


def node_problem() -> str | None:
    if not shutil.which("node"):
        return "Node.js is not installed"
    if not (node_dir() / "node_modules" / "@qsp" / "wasm-engine").is_dir():
        return "@qsp/wasm-engine is not installed; run `aero2qspider setup`"
    stale = [n for n in NODE_FILES if not (node_dir() / n).is_file() or (node_dir() / n).read_bytes() != _driver_file(n).read_bytes()]
    if stale:
        return "the QSP 5.9 drivers are outdated; run `aero2qspider setup`"
    return None


def qsp57_problem() -> str | None:
    if not qsp57_binary().is_file():
        return "libqsp 5.7 is not built; run `aero2qspider setup`"
    return None


def _driver_file(name: str) -> Path:
    return Path(str(resources.files("aero2qspider") / "drivers" / name))


def setup_node(log: Log) -> None:
    npm = shutil.which("npm")
    if not shutil.which("node") or not npm:
        raise EngineError("Node.js 18+ with npm is required for --smoke and --compare")
    target = node_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name in NODE_FILES:
        shutil.copyfile(_driver_file(name), target / name)
    log(f"installing @qsp/wasm-engine into {target}")
    result = subprocess.run([npm, "install", "--no-audit", "--no-fund", "--silent"], cwd=target, capture_output=True, text=True)
    if result.returncode != 0:
        raise EngineError(f"npm install failed:\n{result.stderr.strip()}")


def _fetch_qsp57(target: Path, log: Log) -> Path:
    """Download libqsp at QSP57_COMMIT into `target` and return its `qsp` source folder."""
    git = shutil.which("git")
    if git:
        log(f"fetching libqsp {QSP57_COMMIT[:7]} with git")
        steps = [
            [git, "init", "-q", str(target)],
            [git, "-C", str(target), "fetch", "-q", "--depth", "1", f"{QSP_REPO}.git", QSP57_COMMIT],
            [git, "-C", str(target), "checkout", "-q", "FETCH_HEAD"],
        ]
        if all(subprocess.run(step, capture_output=True).returncode == 0 for step in steps):
            return target / "qsp"
        log("git fetch failed, downloading the source archive instead")
    url = f"{QSP_REPO}/archive/{QSP57_COMMIT}.tar.gz"
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(target, filter="data")
    return target / f"qsp-{QSP57_COMMIT}" / "qsp"


def find_compiler(cc: str | None) -> str:
    for candidate in (cc, os.environ.get("CC"), "cc", "gcc", "clang"):
        if candidate and shutil.which(candidate):
            return candidate
    raise EngineError("a C compiler (gcc or clang) is required to build libqsp 5.7; set CC or pass --cc")


def build_qsp57(log: Log, cc: str | None = None, source: Path | None = None) -> Path:
    compiler = find_compiler(cc)
    binary = qsp57_binary()
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aero2qspider-qsp57-") as tmp:
        src = Path(source) if source else _fetch_qsp57(Path(tmp) / "qsp", log)
        if not (src / "bindings" / "default" / "qsp_default.h").is_file():
            raise EngineError(f"{src} does not look like the libqsp `qsp` source folder")
        files = [str(_driver_file("qsp57_driver.c"))]
        for pattern in ("*.c", "onig/*.c", "onig/enc/*.c", "bindings/default/*.c"):
            files += sorted(str(p) for p in src.glob(pattern))
        # -iquote keeps libqsp's own time.h from shadowing the system header; gnu99 keeps K&R callback types valid
        command = [compiler, "-std=gnu99", "-O1", "-w", "-D_UNICODE", "-DNDEBUG", "-iquote", str(src), "-o", str(binary), *files]
        log(f"compiling libqsp 5.7 with {compiler}")
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise EngineError(f"compilation failed:\n{result.stderr.strip()[-4000:]}")
    return binary


def node_command(script: str, *args: str) -> list[str]:
    return [shutil.which("node") or "node", str(node_dir() / script), *args]


def qsp57_command(game: Path) -> list[str]:
    return [str(qsp57_binary()), str(game)]
