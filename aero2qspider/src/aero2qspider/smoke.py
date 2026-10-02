"""Random walk of a converted game on @qsp/wasm-engine that collects runtime errors."""

import asyncio
import json
from pathlib import Path

from .engines import node_command, node_problem


async def run_smoke(game_file: Path, steps: int, seeds: int) -> tuple[list[dict[str, object]], str | None]:
    """Run the game with several seeds concurrently and merge the errors; returns (errors, problem)."""
    if problem := node_problem():
        return [], problem

    async def one(seed: int) -> dict[str, object]:
        proc = await asyncio.create_subprocess_exec(
            *node_command("smoke.mjs", str(game_file), str(steps), str(seed)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        except TimeoutError:
            proc.kill()
            raise RuntimeError(f"the run with seed {seed} took longer than 15 minutes") from None
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip()[-2000:])
        return json.loads(stdout)

    try:
        runs = await asyncio.gather(*(one(seed) for seed in range(1, seeds + 1)))
    except RuntimeError as error:
        return [], str(error)
    errors: dict[str, dict[str, object]] = {}
    for run in runs:
        for error in run["errors"]:  # type: ignore[union-attr]
            key = f"{error['location']}|{error['actionIndex']}|{error['line']}|{error['description']}"
            if key in errors:
                errors[key]["count"] = int(errors[key]["count"]) + int(error["count"])  # type: ignore[call-overload]
            else:
                errors[key] = dict(error)
    return list(errors.values()), None
