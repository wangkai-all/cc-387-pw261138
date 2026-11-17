"""Compute a verification report of engine invariants on the fixture data."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridmerge import (  # noqa: E402
    EngineRequest, FusionEngine, GridDef, Method, OverlapPolicy, VersionStore,
    conservative_mean, from_rows, plan_overlaps, overlap_weights,
)


def load_store() -> VersionStore:
    store = VersionStore()
    store.commit_batch([
        from_rows(json.loads((ROOT / "data" / "alpha_v1.json").read_text())),
        from_rows(json.loads((ROOT / "data" / "beta_v1.json").read_text())),
    ])
    return store


def direct_integral(store, target, priority):
    """Reference integral, computed cell-by-cell with no engine involvement."""
    total = 0.0
    support = 0.0
    for r in range(target.ny):
        for c in range(target.nx):
            for sid in priority:
                g = store.get(sid)
                num = den = 0.0
                for sr, sc, a in overlap_weights(target, g.definition, r, c):
                    v = g.values[sr][sc]
                    if v is None:
                        continue
                    num += v * a
                    den += a
                if den > 0:
                    total += (num / den) * den
                    support += den
                    break
    return total, support


def main() -> int:
    store = load_store()
    target = GridDef("TARGET", nx=8, ny=6, dx=6.0, dy=6.0)
    engine = FusionEngine(store)
    checks = []

    def check(name, got, want, tol=1e-7):
        ok = abs(got - want) <= tol * max(1.0, abs(want))
        checks.append((name, ok, got, want))
        return ok

    req = EngineRequest(target=target, overlap_policy=OverlapPolicy.PRIORITY,
                        priority=("ALPHA", "BETA"))
    comp = engine.run(req)
    got = comp.summary["aggregate"]["sum"]
    want, want_support = direct_integral(store, target, ("ALPHA", "BETA"))
    check("priority integral equals independent sum", got, want)
    check("priority support equals independent support",
          comp.summary["aggregate"]["support_area"], want_support)

    # Union support must be <= sum of individual supports wherever they overlap.
    alpha = store.get("ALPHA")
    beta = store.get("BETA")
    a_area = alpha.definition.cell_area * sum(
        1 for row in alpha.values for v in row if v is not None)
    b_area = beta.definition.cell_area * sum(
        1 for row in beta.values for v in row if v is not None)
    acc = comp.summary["aggregate"]["support_area"]
    checks.append(("union support never exceeds sum of supports",
                   acc <= a_area + b_area + 1e-9, acc, a_area + b_area))

    # Overlap ledger agrees with the geometric intersection of footprints.
    recs = plan_overlaps(target, [alpha, beta])
    shared = sum(r.shared_area for r in recs)
    inter_w = min(alpha.definition.x1, beta.definition.x1) - max(alpha.definition.x0, beta.definition.x0)
    inter_h = min(alpha.definition.y1, beta.definition.y1) - max(alpha.definition.y0, beta.definition.y0)
    check("overlap ledger total equals footprint intersection", shared, inter_w * inter_h)

    # Resampling module agrees with the engine for a single source.
    vals, _ = conservative_mean(target, alpha)
    engine_single = engine.run(EngineRequest(target=target, priority=("ALPHA",)))
    mism = []
    for rec, v in zip(engine_single.cells, vals):
        if (rec.value is None) != (v is None):
            mism.append((rec.row, rec.col))
        elif v is not None and abs(rec.value - v) > 1e-9:
            mism.append((rec.row, rec.col))
    checks.append(("engine single-source matches resample.conservative_mean",
                   not mism, len(mism), 0))

    # Nearest is non-conservative: it must differ from the conservative integral.
    near = engine.run(EngineRequest(target=target, method=Method.NEAREST,
                                    priority=("ALPHA", "BETA")))
    checks.append(("nearest does not preserve the area integral",
                   abs(near.summary["aggregate"]["sum"] - want) > 1e-6,
                   near.summary["aggregate"]["sum"], want))

    # Missing is not zero: a cell over ALPHA's missing column stays missing.
    gap_cell = None
    for rec in engine_single.cells:
        if rec.value is None:
            gap_cell = rec
            break
    checks.append(("missing stays missing under single source",
                   gap_cell is not None and gap_cell.value is None,
                   gap_cell.status.value if gap_cell else "none", "missing"))

    # Extensive counts over overlapping sources must be refused.
    refused = False
    try:
        engine.run(EngineRequest(target=target, overlap_policy=OverlapPolicy.SUM,
                                 stat_scale="extensive"))
    except Exception as exc:  # noqa: BLE001
        refused = "twice" in str(exc) or "share ground" in str(exc)
    checks.append(("overlapping counts are refused, not averaged", refused, refused, True))

    # Zonal fill is labelled and excluded from observed coverage.
    zreq = EngineRequest(target=target, fill="zone_of_influence", fill_max_distance=8.0,
                         priority=("ALPHA", "BETA"))
    zcomp = engine.run(zreq)
    zonal_cells = [c for c in zcomp.cells if c.origin.value == "zonal"]
    checks.append(("zonal fallback creates labelled cells", len(zonal_cells) > 0,
                   len(zonal_cells), ">0"))
    checks.append(("zonal cells carry no observed support",
                   all(c.area_fraction == 0 for c in zonal_cells),
                   [c.area_fraction for c in zonal_cells][:3], [0, 0, 0]))

    print(f"fixture checks: {sum(1 for c in checks if c[1])}/{len(checks)} passed")
    bad = [c for c in checks if not c[1]]
    for name, ok, got, want in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  got={got!r} want={want!r}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
