"""Generate the fictitious planar-grid fixture used by the demo and benchmarks.

The domain is a 48 m x 36 m rectangle in a fictitious planar CRS.  Two
providers publish it at 4 m and 3 m, with different origins, so the overlap is
*not* cell-aligned and includes partial cells on both sides.

Run with ``python3 tools/make_fixtures.py``.  Everything is seeded; re-running
reproduces byte-identical values.
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridmerge import GridDef  # noqa: E402

OUT = ROOT / "data"


def field(x: float, y: float) -> float:
    """A smooth, made-up intensive field: a temperature in degC."""
    return 12.0 + 0.15 * x - 0.05 * y + 2.0 * math.sin(x / 9.0) * math.cos(y / 11.0)


def alpha_grid() -> GridDef:
    return GridDef("ALPHA", nx=12, ny=9, dx=4.0, dy=4.0, origin_x=0.0, origin_y=0.0)


def beta_grid() -> GridDef:
    return GridDef("BETA", nx=8, ny=8, dx=3.0, dy=3.0, origin_x=18.0, origin_y=6.0)


def alpha_payload() -> dict:
    d = alpha_grid()
    rng = random.Random(20240501)
    rows = []
    err = []
    for r in range(d.ny):
        row = []
        erow = []
        for c in range(d.nx):
            x = d.origin_x + (c + 0.5) * d.dx
            y = d.origin_y + (r + 0.5) * d.dy
            # Missing: NE block + two isolated holes + one full column.
            if (r >= 7 and c >= 9) or (r == 1 and c == 3) or (r == 4 and c == 8) or c == 6:
                row.append(None)
                erow.append(None)
                continue
            v = field(x, y) + rng.gauss(0.0, 0.12)
            row.append(round(v, 4))
            erow.append(round(0.12 ** 2, 6))
        rows.append(row)
        err.append(erow)
    return {
        "source": "ALPHA",
        "grid": d.to_dict(),
        "values": rows,
        "error_var": err,
        "comment": "4 m provider; missing NE block, one column and two isolated cells",
    }


def beta_payload(beta_version: int = 1, fill_patch: bool = False) -> dict:
    d = beta_grid()
    rng = random.Random(20240502)
    rows = []
    err = []
    # A lake-shaped hole of missing cells in the middle of BETA.
    lake = {(3, 3), (3, 4), (4, 3), (4, 4), (2, 4)}
    # The revision fills part of the lake: a 2x2 patch of revised cells.
    patch = {(3, 3), (3, 4), (4, 3), (4, 4)} if fill_patch else set()
    for r in range(d.ny):
        row = []
        erow = []
        for c in range(d.nx):
            x = d.origin_x + (c + 0.5) * d.dx
            y = d.origin_y + (r + 0.5) * d.dy
            if (r, c) in lake and (r, c) not in patch:
                row.append(None)
                erow.append(None)
                continue
            # BETA carries a small positive bias and finer noise than ALPHA,
            # exactly the situation an overlap review is supposed to expose.
            v = field(x, y) * 1.008 + 0.06 + rng.gauss(0.0, 0.05)
            if (r, c) in patch:
                v = field(x, y) + 0.02  # revised: bias corrected, low noise
            row.append(round(v, 4))
            erow.append(round(0.05 ** 2, 6))
        rows.append(row)
        err.append(erow)
    payload = {
        "source": "BETA",
        "grid": d.to_dict(),
        "values": rows,
        "error_var": err,
        "comment": "3 m provider; origin offset from ALPHA, lake hole in the centre",
    }
    if beta_version > 1:
        payload["version"] = beta_version
        payload["comment"] += f"; revision {beta_version}"
    return payload


def small_reference() -> dict:
    """A by-hand benchmark: 4x3 coarse cells resampled to 2x2 fine cells.

    The fine grid is 2 m and aligned to the coarse grid's origin, so every
    fine cell lies inside exactly one coarse cell and the numbers can be
    written on paper.  ``missing`` marks coarse cells with no observation.
    """
    coarse = GridDef("COARSE", nx=4, ny=3, dx=4.0, dy=4.0, origin_x=0.0, origin_y=0.0)
    fine = GridDef("FINE", nx=8, ny=6, dx=2.0, dy=2.0, origin_x=0.0, origin_y=0.0)
    values = [
        [10.0, 12.0, None, 16.0],
        [11.0, 13.0, 15.0, 17.0],
        [None, 14.0, 16.0, 18.0],
    ]
    return {
        "coarse": coarse.to_dict(),
        "fine": fine.to_dict(),
        "values": values,
        "hand_checks": {
            "source_cell_area": 16.0,
            "target_cell_area": 4.0,
            "source_valid_count": 10,
            "source_valid_area": 160.0,
            "source_area_integral": 10*16 + 12*16 + 16*16 + 11*16 + 13*16 + 15*16 + 17*16 + 14*16 + 16*16 + 18*16,
            "note": "integral = sum(value * 16); each coarse cell is four fine cells each 1/4 of the area",
        },
    }


def beta_patch_payload() -> dict:
    """A *patch sheet*: only the cells that changed, not a whole grid.

    Absence from a patch means "unchanged", which is a different contract from
    absence in a submitted grid ("missing").  Keeping them separate stops a
    partial revision from silently blanking the rest of the source.
    """
    d = beta_grid()
    patch = []
    rng = random.Random(20240503)
    for (r, c) in sorted({(3, 3), (3, 4), (4, 3), (4, 4), (2, 4)}):
        x = d.origin_x + (c + 0.5) * d.dx
        y = d.origin_y + (r + 0.5) * d.dy
        patch.append([
            r, c, round(field(x, y) + 0.02 + rng.gauss(0.0, 0.03), 4),
            round(0.03 ** 2, 6),
        ])
    return {
        "source": "BETA",
        "mode": "patch",
        "cells": patch,
        "comment": "revised lake cells with their own error variance; "
                   "absence means unchanged, not missing",
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "alpha_v1.json": alpha_payload(),
        "beta_v1.json": beta_payload(),
        "beta_v2_revised.json": beta_payload(beta_version=2, fill_patch=True),
        "beta_patch_v2.json": beta_patch_payload(),
        "small_reference.json": small_reference(),
    }
    for name, payload in files.items():
        (OUT / name).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote data/{name}")


if __name__ == "__main__":
    main()
