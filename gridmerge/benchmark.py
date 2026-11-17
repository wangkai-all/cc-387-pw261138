"""Small-grid hand benchmark, as a library.

The same numbers as ``tools/small_benchmark.py`` but importable by the server
so the workbench can show the benchmark beside a real run.  Every expected
value here is derivable on paper; see the docstring in the CLI tool.
"""

from __future__ import annotations

from typing import Optional

from .engine import EngineRequest, FusionEngine
from .grid import GridDef
from .model import OverlapPolicy
from .resample import nearest_neighbour
from .grid import from_rows
from .versioning import VersionStore


COARSE = GridDef("COARSE", nx=4, ny=3, dx=4.0, dy=4.0)
FINE = GridDef("FINE", nx=8, ny=6, dx=2.0, dy=2.0)
VALUES = [
    [10.0, 12.0, None, 16.0],
    [11.0, 13.0, 15.0, 17.0],
    [None, 14.0, 16.0, 18.0],
]


def _expected() -> tuple[list[Optional[float]], list[tuple[int, int]]]:
    out: list[Optional[float]] = []
    missing: list[tuple[int, int]] = []
    for r in range(FINE.ny):
        for c in range(FINE.nx):
            v = VALUES[r // 2][c // 2]
            out.append(v)
            if v is None:
                missing.append((r, c))
    return out, missing


def small_grid_benchmark() -> dict:
    store = VersionStore()
    store.commit_batch([from_rows({
        "source": "COARSE", "grid": COARSE.to_dict(), "values": VALUES,
    })])
    comp = FusionEngine(store).run(EngineRequest(
        target=FINE, overlap_policy=OverlapPolicy.PRIORITY, priority=("COARSE",)))
    want, missing = _expected()
    got = [c.value for c in comp.cells]
    mismatches = [
        {"row": c.row, "col": c.col, "got": c.value, "want": want[c.index]}
        for c in comp.cells
        if (c.value is None) != (want[c.index] is None)
        or (c.value is not None and abs(c.value - want[c.index]) > 1e-12)
    ]
    near, _ = nearest_neighbour(FINE, store.get("COARSE"))
    valid = [v for row in VALUES for v in row if v is not None]
    coarse_integral = sum(v * 16.0 for v in valid)
    fine_integral = comp.summary["aggregate"]["sum"]
    return {
        "coarse": COARSE.to_dict(),
        "fine": FINE.to_dict(),
        "source_values": VALUES,
        "fine_values": got,
        "expected_values": want,
        "missing_cells": missing,
        "mismatches": mismatches,
        "nearest_matches": all(
            (a is None) == (b is None) and (a is None or abs(a - b) < 1e-12)
            for a, b in zip(near, got)
        ),
        "coarse_integral": coarse_integral,
        "fine_integral": fine_integral,
        "mean": comp.summary["mean"]["mean"],
        "checks": {
            "cells_match_hand_benchmark": not mismatches,
            "nearest_agrees_on_aligned_grid": all(
                (a is None) == (b is None) and (a is None or abs(a - b) < 1e-12)
                for a, b in zip(near, got)
            ),
            "integral_conserved": abs(fine_integral - coarse_integral) < 1e-9,
            "missing_never_filled": all(
                got[r * FINE.nx + c] is None for r, c in missing
            ),
        },
    }
