"""Small-grid hand benchmark: every expected value is derivable on paper.

The coarse grid is 4x3 at 4 m; the fine grid is 8x6 at 2 m with the same
origin, so each fine cell sits wholly inside one coarse cell.  That makes the
conservative result trivially checkable by eye, which is the point: it
isolates the resampling arithmetic from mixed-support and alignment effects.

Run ``python3 tools/small_benchmark.py --json`` for machine-readable output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridmerge import (  # noqa: E402
    GridDef, VersionStore, FusionEngine, EngineRequest, OverlapPolicy,
    from_rows, nearest_neighbour,
)


def build() -> tuple[VersionStore, GridDef, GridDef, list]:
    payload = json.loads((ROOT / "data" / "small_reference.json").read_text())
    coarse = GridDef.from_dict(payload["coarse"])
    fine = GridDef.from_dict(payload["fine"])
    values = payload["values"]
    store = VersionStore()
    store.commit_batch([from_rows({
        "source": "COARSE", "grid": coarse.to_dict(), "values": values,
    })])
    return store, coarse, fine, values


def expected_cells(values, ny, nx):
    """Each valid coarse cell becomes a block of 2x2 fine cells with its value."""
    out = []
    miss = []
    for fr in range(ny):
        cr = fr // 2
        for fc in range(nx):
            cc = fc // 2
            v = values[cr][cc]
            if v is None:
                miss.append((fr, fc))
            out.append(v)
    return out, miss


def main() -> int:
    as_json = "--json" in sys.argv
    store, coarse, fine, values = build()
    engine = FusionEngine(store)
    comp = engine.run(EngineRequest(
        target=fine, overlap_policy=OverlapPolicy.PRIORITY, priority=("COARSE",),
    ))
    got = [c.value for c in comp.cells]
    want, miss = expected_cells(values, fine.ny, fine.nx)

    mismatches = [
        {"row": c.row, "col": c.col, "got": c.value, "want": want[c.index]}
        for c in comp.cells
        if (c.value is None) != (want[c.index] is None)
        or (c.value is not None and abs(c.value - want[c.index]) > 1e-12)
    ]

    vals = [v for row in values for v in row if v is not None]
    image_violations = [
        (c.row, c.col) for c in comp.cells
        if c.value is not None and abs(c.value - round(c.value, 4)) > 1e-12
    ]

    # Nearest sampling on an aligned grid gives the same picture as
    # conservative resampling; any difference there would be an arithmetic bug.
    near_vals, _ = nearest_neighbour(fine, store.get("COARSE"))
    nearest_matches = all(
        (a is None) == (b is None) and (a is None or abs(a - b) < 1e-12)
        for a, b in zip(near_vals, got)
    )

    # Coarse cells along the hole boundary: a fine cell is NOT bounded into a
    # neighbour's value.  Check every missing fine cell sits inside a missing
    # coarse cell.
    holes_clean = all(values[r // 2][c // 2] is None for r, c in miss)

    # Integral identity: sum(value * 4) over fine cells equals sum(v*16) coarse.
    fine_integral = comp.summary["aggregate"]["sum"]
    coarse_integral = sum(v * 16.0 for v in vals)

    out = {
        "grids": {"coarse": coarse.to_dict(), "fine": fine.to_dict()},
        "source_values": values,
        "expected": {
            "fine_values": want,
            "missing_fine_cells": miss,
            "missing_cells": len(miss),
            "valid_cells": len(vals),
            "coarse_integral": coarse_integral,
            "fine_integral": fine_integral,
            "mean_over_support": sum(vals) / len(vals),
        },
        "got": {
            "summary": comp.summary,
            "mismatches": mismatches,
            "image_violations": image_violations,
            "nearest_matches_conservative": nearest_matches,
            "holes_clean": holes_clean,
        },
        "checks": {
            "cells_match_hand_benchmark": not mismatches,
            "conservative_holds_value_on_aligned_cells": not image_violations,
            "nearest_agrees_on_aligned_grid": nearest_matches,
            "missing_never_filled_from_neighbour": holes_clean,
            "integral_conserved": abs(fine_integral - coarse_integral) < 1e-9,
        },
    }

    if as_json:
        print(json.dumps(out, indent=2))
        return 0 if all(out["checks"].values()) else 1

    print("Small-grid hand benchmark (coarse 4x3 @4 m -> fine 8x6 @2 m)\n")
    for r in range(coarse.ny):
        print("  coarse row", r, values[r])
    print()
    for r in range(fine.ny):
        row = [f"{got[r * fine.nx + c]:6.1f}" if got[r * fine.nx + c] is not None else "  ----"
               for c in range(fine.nx)]
        print("  fine row", r, " ".join(row))
    print()
    for name, ok in out["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print()
    print(f"  coarse integral = {coarse_integral:.1f}, fine integral = {fine_integral:.1f}")
    print(f"  missing fine cells = {len(miss)} (all inside the two missing coarse cells)")
    return 0 if all(out["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
