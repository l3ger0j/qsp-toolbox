"""Running the real QSP engines through ctypes.

qsp (5.9) and qsp-legacy (5.7.0) export the same symbol names, so each engine
runs in its own process: `run_in_subprocess(job)` starts
`python -m qspsav.engine`, which reads one JSON job from stdin and prints one
JSON result. `run_job(job)` does the same in the current process, and
`run_many(jobs, workers)` runs many jobs in parallel processes with asyncio.

Job fields (all but "engine", "lib" and "game" are optional):
    engine      "5.7.0" or "5.9"
    lib         path to libqsp-legacy / libqsp
    game        path to the .qsp file
    libraries   {name: path}: files served to INCLIB/ADDQST under these names
    open_save   path of a save to load
    exec        [code, ...] executed after loading
    actions     [index, ...] actions executed after that
    input, select_action, select_object
    save_to     path to write the resulting state to
    vars        [name, ...] -> result["vars"][name] = [[number, string], ...]
    inspect     {"vars": [name, ...], "keys": {name: [key, ...]}, "objects": [name, ...]}
                -> result["state"]: descriptions, current location, actions,
                   objects, variable values and values read by text index
    runs        [job, ...]: several jobs (without engine, lib, game and
                libraries) run one after another after loading the game once
                -> result["runs"]
"""

from __future__ import annotations

import asyncio
import ctypes as C
import json
import subprocess
import sys
from pathlib import Path

ENGINE_570 = "5.7.0"
ENGINE_59 = "5.9"


def _units(text: str) -> list[int]:
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _text(units: list[int]) -> str:
    out = bytearray()
    for u in units:
        if u > 0xFFFF:  # a full code point in a 4-byte wchar_t
            out += chr(u).encode("utf-16-le")
        else:
            out += u.to_bytes(2, "little")
    return out.decode("utf-16-le", "surrogatepass")


class _String59(C.Structure):
    _fields_ = [("Str", C.c_void_p), ("End", C.c_void_p)]


class _ErrorInfo59(C.Structure):
    _fields_ = [("ErrorNum", C.c_int), ("ErrorDesc", _String59), ("LocName", _String59),
                ("ActIndex", C.c_int), ("TopLineNum", C.c_int), ("IntLineNum", C.c_int),
                ("IntLine", _String59)]


class _ListItem59(C.Structure):
    _fields_ = [("Name", _String59), ("Image", _String59)]


class _ObjectItem59(C.Structure):
    _fields_ = [("Name", _String59), ("Title", _String59), ("Image", _String59)]


class _ErrorInfo570(C.Structure):
    _fields_ = [("ErrorNum", C.c_int), ("ErrorDesc", C.c_void_p), ("LocName", C.c_void_p),
                ("ActIndex", C.c_int), ("TopLineNum", C.c_int), ("IntLineNum", C.c_int),
                ("IntLine", C.c_void_p)]


class _ListItem570(C.Structure):
    _fields_ = [("Name", C.c_void_p), ("Image", C.c_void_p)]


def _declare(lib: C.CDLL, specs: dict[str, tuple]) -> None:
    for name, (restype, *argtypes) in specs.items():
        func = getattr(lib, name)
        func.restype = restype
        func.argtypes = argtypes


class Engine59:
    """qsp 5.9: QSP_CHAR is wchar_t, strings are QSPString {Str, End}, QSP_BOOL is signed char."""

    OPENGAME, ISPLAYINGFILE = 12, 1  # QSP_CALL_* indices

    def __init__(self, lib: Path) -> None:
        self.lib = C.CDLL(str(lib))
        S, B, I, P = _String59, C.c_byte, C.c_int, C.c_void_p
        _declare(self.lib, {
            "QSPInit": (None,),
            "QSPGetVersion": (S,),
            "QSPSetCallback": (None, I, P),
            "QSPLoadGameWorldFromData": (B, P, I, B),
            "QSPOpenSavedGameFromData": (B, P, I, B),
            "QSPSaveGameAsData": (B, P, C.POINTER(I), B),
            "QSPExecString": (B, S, B),
            "QSPSetInputStrText": (None, S),
            "QSPSetSelActionIndex": (B, I, B),
            "QSPSetSelObjectIndex": (B, I, B),
            "QSPExecuteSelActionCode": (B, B),
            "QSPGetLastErrorData": (_ErrorInfo59,),
            "QSPGetVarValuesCount": (B, S, C.POINTER(I)),
            "QSPGetNumVarValue": (B, S, I, C.POINTER(I)),
            "QSPGetStrVarValue": (B, S, I, C.POINTER(S)),
            "QSPGetMainDesc": (S,),
            "QSPGetVarsDesc": (S,),
            "QSPGetActions": (I, P, I),
            "QSPGetObjects": (I, P, I),
        })
        self._keep: list[object] = []
        self.lib.QSPInit()

    def _str(self, text: str) -> _String59:
        # One wchar_t per UTF-16 code unit: that is how the engine stores text
        # it reads from UCS-2 game and save files.
        units = _units(text)
        buf = (C.c_wchar * (len(units) + 1))(*map(chr, units), "\0")
        self._keep.append(buf)
        start = C.addressof(buf)
        return _String59(start, start + len(units) * C.sizeof(C.c_wchar))

    @staticmethod
    def _read(s: _String59) -> str:
        if not s.Str:
            return ""
        size = C.sizeof(C.c_wchar)
        arr = (C.c_uint32 if size == 4 else C.c_uint16) * ((s.End - s.Str) // size)
        return _text(list(arr.from_address(s.Str)))

    def set_callbacks(self, libraries: dict[str, bytes]) -> None:
        """OPENGAME loads included games from `libraries`; ISPLAYINGFILE says
        yes, so PLAY keeps files in the playlist."""
        def open_game(file, is_new_game):
            data = libraries.get(self._read(file))
            if data is not None:
                self.lib.QSPLoadGameWorldFromData(C.create_string_buffer(data, len(data)), len(data), is_new_game)

        self._callbacks = [C.CFUNCTYPE(None, _String59, C.c_byte)(open_game),
                           C.CFUNCTYPE(C.c_byte, _String59)(lambda file: 1)]
        self.lib.QSPSetCallback(self.OPENGAME, C.cast(self._callbacks[0], C.c_void_p))
        self.lib.QSPSetCallback(self.ISPLAYINGFILE, C.cast(self._callbacks[1], C.c_void_p))

    def version(self) -> str:
        return self._read(self.lib.QSPGetVersion())

    def load_game(self, data: bytes) -> bool:
        return bool(self.lib.QSPLoadGameWorldFromData(C.create_string_buffer(data, len(data)), len(data), 1))

    def open_save(self, data: bytes) -> bool:
        return bool(self.lib.QSPOpenSavedGameFromData(C.create_string_buffer(data, len(data)), len(data), 0))

    def save(self) -> bytes | None:
        size = C.c_int(0)
        if self.lib.QSPSaveGameAsData(None, C.byref(size), 0) or size.value <= 0:
            return None
        buf = C.create_string_buffer(size.value)
        if not self.lib.QSPSaveGameAsData(buf, C.byref(size), 0):
            return None
        return buf.raw[:size.value]

    def exec(self, code: str) -> bool:
        return bool(self.lib.QSPExecString(self._str(code), 0))

    def set_input(self, text: str) -> None:
        self.lib.QSPSetInputStrText(self._str(text))

    def select(self, action: int | None, obj: int | None) -> bool:
        ok = True
        if action is not None:
            ok &= bool(self.lib.QSPSetSelActionIndex(action, 0))
        if obj is not None:
            ok &= bool(self.lib.QSPSetSelObjectIndex(obj, 0))
        return ok

    def run_action(self, index: int) -> bool:
        return bool(self.lib.QSPSetSelActionIndex(index, 0)) and bool(self.lib.QSPExecuteSelActionCode(0))

    def error(self) -> dict | None:
        e = self.lib.QSPGetLastErrorData()
        if not e.ErrorNum:
            return None
        return {"num": e.ErrorNum, "desc": self._read(e.ErrorDesc), "loc": self._read(e.LocName),
                "line": self._read(e.IntLine)}

    def var(self, name: str) -> list[list]:
        count = C.c_int(0)
        self.lib.QSPGetVarValuesCount(self._str(name), C.byref(count))
        values = []
        for i in range(count.value):
            num = C.c_int(0)
            text = _String59()
            self.lib.QSPGetNumVarValue(self._str(name), i, C.byref(num))
            self.lib.QSPGetStrVarValue(self._str("$" + name), i, C.byref(text))
            values.append([num.value, self._read(text)])
        return values

    def descriptions(self) -> tuple[str, str]:
        return self._read(self.lib.QSPGetMainDesc()), self._read(self.lib.QSPGetVarsDesc())

    def actions(self) -> list[str]:
        count = self.lib.QSPGetActions(None, 0)
        items = (_ListItem59 * max(count, 1))()
        self.lib.QSPGetActions(items, count)
        return [self._read(items[i].Name) for i in range(count)]

    def objects(self) -> list[str]:
        count = self.lib.QSPGetObjects(None, 0)
        items = (_ObjectItem59 * max(count, 1))()
        self.lib.QSPGetObjects(items, count)
        return [self._read(items[i].Name) for i in range(count)]


class Engine570:
    """qsp-legacy 5.7.0: QSP_CHAR is uint16_t, strings are zero-terminated, QSP_BOOL is int."""

    OPENGAME, ISPLAYINGFILE = 13, 1  # QSP_CALL_* indices

    def __init__(self, lib: Path) -> None:
        self.lib = C.CDLL(str(lib))
        I, P = C.c_int, C.c_void_p
        _declare(self.lib, {
            "QSPInit": (None,),
            "QSPGetVersion": (P,),
            "QSPSetCallBack": (None, I, P),
            "QSPLoadGameWorldFromData": (I, C.c_char_p, I, I),
            "QSPOpenSavedGameFromData": (I, P, I),
            "QSPSaveGameAsData": (I, P, I, C.POINTER(I), I),
            "QSPExecString": (I, P, I),
            "QSPSetInputStrText": (None, P),
            "QSPSetSelActionIndex": (I, I, I),
            "QSPSetSelObjectIndex": (I, I, I),
            "QSPExecuteSelActionCode": (I, I),
            "QSPGetLastErrorData": (_ErrorInfo570,),
            "QSPGetVarValuesCount": (I, P, C.POINTER(I)),
            "QSPGetVarValues": (I, P, I, C.POINTER(I), C.POINTER(P)),
            "QSPGetMainDesc": (P,),
            "QSPGetVarsDesc": (P,),
            "QSPGetActions": (I, P, I),
            "QSPGetObjects": (I, P, I),
        })
        self._keep: list[object] = []
        self.lib.QSPInit()

    def _str(self, text: str) -> C.Array:
        units = _units(text)
        buf = (C.c_uint16 * (len(units) + 1))(*units, 0)
        self._keep.append(buf)
        return buf

    @staticmethod
    def _read(ptr: int | None) -> str:
        if not ptr:
            return ""
        units = []
        while (u := C.c_uint16.from_address(ptr + 2 * len(units)).value) != 0:
            units.append(u)
        return _text(units)

    def set_callbacks(self, libraries: dict[str, bytes]) -> None:
        def open_game(file, is_add_locs):
            data = libraries.get(self._read(file))
            if data is not None:
                self.lib.QSPLoadGameWorldFromData(data + b"\0\0\0\0", len(data), is_add_locs)

        self._callbacks = [C.CFUNCTYPE(None, C.c_void_p, C.c_int)(open_game),
                           C.CFUNCTYPE(C.c_int, C.c_void_p)(lambda file: 1)]
        self.lib.QSPSetCallBack(self.OPENGAME, C.cast(self._callbacks[0], C.c_void_p))
        self.lib.QSPSetCallBack(self.ISPLAYINGFILE, C.cast(self._callbacks[1], C.c_void_p))

    def version(self) -> str:
        return self._read(self.lib.QSPGetVersion())

    def load_game(self, data: bytes) -> bool:
        # The loader scans for delimiters with strstr-like functions, so the
        # data must be followed by zero bytes.
        return bool(self.lib.QSPLoadGameWorldFromData(data + b"\0\0\0\0", len(data), 0))

    def open_save(self, data: bytes) -> bool:
        units = [data[i] | (data[i + 1] << 8) for i in range(0, len(data) - 1, 2)]
        return bool(self.lib.QSPOpenSavedGameFromData((C.c_uint16 * (len(units) + 1))(*units, 0), 0))

    def save(self) -> bytes | None:
        real = C.c_int(0)
        self.lib.QSPSaveGameAsData((C.c_uint16 * 1)(), 1, C.byref(real), 0)
        if real.value <= 0:
            return None
        buf = (C.c_uint16 * real.value)()
        if not self.lib.QSPSaveGameAsData(buf, real.value, C.byref(real), 0):
            return None
        # realSize counts the terminating zero; the file holds the text only.
        return bytes(C.string_at(buf, (real.value - 1) * 2))

    def exec(self, code: str) -> bool:
        return bool(self.lib.QSPExecString(self._str(code), 0))

    def set_input(self, text: str) -> None:
        self.lib.QSPSetInputStrText(self._str(text))

    def select(self, action: int | None, obj: int | None) -> bool:
        ok = True
        if action is not None:
            ok &= bool(self.lib.QSPSetSelActionIndex(action, 0))
        if obj is not None:
            ok &= bool(self.lib.QSPSetSelObjectIndex(obj, 0))
        return ok

    def run_action(self, index: int) -> bool:
        return bool(self.lib.QSPSetSelActionIndex(index, 0)) and bool(self.lib.QSPExecuteSelActionCode(0))

    def error(self) -> dict | None:
        e = self.lib.QSPGetLastErrorData()
        if not e.ErrorNum:
            return None
        return {"num": e.ErrorNum, "desc": self._read(e.ErrorDesc), "loc": self._read(e.LocName),
                "line": self._read(e.IntLine)}

    def var(self, name: str) -> list[list]:
        count = C.c_int(0)
        self.lib.QSPGetVarValuesCount(self._str(name), C.byref(count))
        values = []
        for i in range(count.value):
            num = C.c_int(0)
            text = C.c_void_p()
            self.lib.QSPGetVarValues(self._str(name), i, C.byref(num), C.byref(text))
            values.append([num.value, self._read(text.value)])
        return values

    def descriptions(self) -> tuple[str, str]:
        return self._read(self.lib.QSPGetMainDesc()), self._read(self.lib.QSPGetVarsDesc())

    def actions(self) -> list[str]:
        count = self.lib.QSPGetActions(None, 0)
        items = (_ListItem570 * max(count, 1))()
        self.lib.QSPGetActions(items, count)
        return [self._read(items[i].Name) for i in range(count)]

    def objects(self) -> list[str]:
        count = self.lib.QSPGetObjects(None, 0)
        items = (_ListItem570 * max(count, 1))()
        self.lib.QSPGetObjects(items, count)
        return [self._read(items[i].Name) for i in range(count)]


ENGINES = {ENGINE_570: Engine570, ENGINE_59: Engine59}

# Scratch variables used to read values through game code.
_NUM, _STR = "QSPSAV__N", "QSPSAV__S"


def _literal(text: str) -> str | None:
    """A QSP string literal for `text` that is taken as-is, or None.

    {...} is a plain string in 5.7.0 and a code value in 5.9; neither expands
    <<...>> (quoted strings do), and a code value used as an index gives the
    same key as the string.
    """
    if "{" not in text and "}" not in text:
        return "{" + text + "}"
    if "<<" not in text:
        return "'" + text.replace("'", "''") + "'"
    return None


def _read_through_code(engine, num_expr: str, str_expr: str) -> list | None:
    if not engine.exec(f"{_NUM} = {num_expr} & ${_STR} = {str_expr}"):
        return None
    return [engine.var(_NUM)[0][0], engine.var(_STR)[0][1]]


def _inspect(engine, spec: dict) -> dict:
    main_desc, vars_desc = engine.descriptions()
    state = {
        "main_desc": main_desc,
        "vars_desc": vars_desc,
        "actions": engine.actions(),
        "objects": engine.objects(),
        "vars": {name: engine.var(name) for name in spec.get("vars", [])},
    }
    # Everything below runs game code, so read the plain values first.
    loc = _read_through_code(engine, "0", "$CURLOC")
    state["location"] = loc[1] if loc else None
    state["obj"] = {}
    for name in spec.get("objects", []):
        literal = _literal(name)
        if literal is not None:
            value = _read_through_code(engine, f"OBJ({literal})", "''")
            state["obj"][name] = value[0] if value else None
    state["keys"] = {}
    for name, keys in spec.get("keys", {}).items():
        found = state["keys"][name] = {}
        for key in keys:
            literal = _literal(key)
            found[key] = None if literal is None else _read_through_code(engine, f"{name}[{literal}]",
                                                                         f"${name}[{literal}]")
    return state


def _run_steps(engine, job: dict, result: dict) -> dict:
    """Everything after loading the game: the save, code, actions, inspection."""
    result.update(ok=True, error=None, vars={})

    def failed(what: str) -> dict:
        result.update(ok=False, failed=what, error=engine.error())
        return result

    if job.get("open_save") and not engine.open_save(Path(job["open_save"]).read_bytes()):
        return failed("open save")
    for code in job.get("exec", []):
        if not engine.exec(code):
            return failed(f"exec {code!r}")
    for index in job.get("actions", []):
        if not engine.run_action(index):
            return failed(f"action {index}")
    if job.get("input") is not None:
        engine.set_input(job["input"])
    if not engine.select(job.get("select_action"), job.get("select_object")):
        return failed("select")
    if job.get("save_to"):
        data = engine.save()
        if data is None:
            return failed("save")
        Path(job["save_to"]).write_bytes(data)
    for name in job.get("vars", []):
        result["vars"][name] = engine.var(name)
    if "inspect" in job:
        result["state"] = _inspect(engine, job["inspect"])
    return result


def run_job(job: dict) -> dict:
    """Run a job in this process. With "runs", the game is loaded once and
    every entry of the list (a job without engine, lib, game and libraries)
    is run after it; the result then has "runs" with a result per entry.
    Opening a save resets the whole game state (qspMemClear), so the runs do
    not affect each other as long as each one opens a save."""
    engine = ENGINES[job["engine"]](Path(job["lib"]))
    version = engine.version()
    engine.set_callbacks({name: Path(path).read_bytes() for name, path in job.get("libraries", {}).items()})
    loaded = engine.load_game(Path(job["game"]).read_bytes())
    load_error = None if loaded else {"ok": False, "failed": "load game", "error": engine.error(),
                                      "version": version, "vars": {}}
    if "runs" in job:
        return {"ok": loaded, "version": version,
                "runs": [load_error or _run_steps(engine, run, {"version": version}) for run in job["runs"]]}
    return load_error or _run_steps(engine, job, {"version": version})


def _command() -> tuple[list[str], Path]:
    return [sys.executable, "-m", __name__], Path(__file__).resolve().parent.parent


def run_in_subprocess(job: dict) -> dict:
    """run_job() in a fresh process (the two engines cannot share one)."""
    cmd, cwd = _command()
    proc = subprocess.run(cmd, input=json.dumps(job), capture_output=True, text=True, encoding="utf-8", cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"engine process failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    return json.loads(proc.stdout)


async def run_in_subprocess_async(job: dict) -> dict:
    """run_in_subprocess() for asyncio: many engine processes can run at once."""
    cmd, cwd = _command()
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(json.dumps(job).encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"engine process failed ({proc.returncode}): "
                           f"{err.decode('utf-8', 'replace').strip()[-2000:]}")
    return json.loads(out.decode("utf-8"))


async def run_many(jobs: list[dict], workers: int) -> list[dict]:
    """Run single-save jobs in parallel engine processes, in batches.

    Jobs for the same engine, game and included files are split into batches
    (a process loads the game once per batch, which is most of the time a
    check takes on a large game), and up to `workers` processes run at once.
    Results come back in the order of `jobs`. A job whose engine process
    dies gets a failed result ("failed": "engine process") instead of
    stopping the others.
    """
    workers = max(1, workers)
    groups: dict[str, list[int]] = {}
    for i, job in enumerate(jobs):
        key = json.dumps([job["engine"], job["lib"], job["game"], sorted(job.get("libraries", {}).items())])
        groups.setdefault(key, []).append(i)
    size = max(1, -(-len(jobs) // workers))  # ceil: enough batches to keep every worker busy
    batches = [indices[k:k + size] for indices in groups.values() for k in range(0, len(indices), size)]
    limit = asyncio.Semaphore(workers)
    results: list[dict | None] = [None] * len(jobs)
    shared = ("engine", "lib", "game", "libraries")

    async def run_one(i: int) -> None:
        try:
            async with limit:
                results[i] = await run_in_subprocess_async(jobs[i])
        except RuntimeError as exc:
            results[i] = {"ok": False, "failed": "engine process", "error": {"num": None, "desc": str(exc)},
                          "vars": {}}

    async def run_batch(indices: list[int]) -> None:
        first = jobs[indices[0]]
        batch = {key: first[key] for key in shared if key in first}
        batch["runs"] = [{k: v for k, v in jobs[i].items() if k not in shared} for i in indices]
        try:
            async with limit:
                out = await run_in_subprocess_async(batch)
        except RuntimeError:
            # The engine died (a save can crash it): run the batch one save
            # per process, so that only the save that crashes it fails.
            await asyncio.gather(*(run_one(i) for i in indices))
            return
        for i, result in zip(indices, out["runs"]):
            results[i] = result

    await asyncio.gather(*(run_batch(b) for b in batches))
    return results  # type: ignore[return-value]


def main() -> None:
    job = json.loads(sys.stdin.read())
    print(json.dumps(run_job(job)))  # ensure_ascii keeps lone surrogates intact


if __name__ == "__main__":
    main()
