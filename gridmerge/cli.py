"""Serve the workbench, generate reports and run benchmarks without a browser.

    python3 -m gridmerge.cli serve   --port 8765
    python3 -m gridmerge.cli report  --out reports/baseline.html
    python3 -m gridmerge.cli bench   --out reports/bench.json
    python3 -m gridmerge.cli demo    --out reports/session.json

``demo`` walks the whole workflow headlessly - ingest, compute, revise,
recompute, diff - so the same path the page drives is exercised in CI.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .engine import EngineRequest, FusionEngine, OverlapPolicy
from .export import artifact_wire, compare_wire
from .grid import GridDef, apply_patch, from_rows
from .report import render_report
from .versioning import VersionStore

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


def _store() -> VersionStore:
    store = VersionStore()
    store.commit_batch([from_rows(_load("alpha_v1.json")), from_rows(_load("beta_v1.json"))])
    return store


def _default_target() -> GridDef:
    return GridDef("TARGET", nx=8, ny=6, dx=6.0, dy=6.0)


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve
    serve(host=args.host, port=args.port, data_dir=DATA)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = _store()
    target = _default_target()
    req = EngineRequest(target=target, overlap_policy=OverlapPolicy.PRIORITY,
                        priority=("ALPHA", "BETA"))
    comp = FusionEngine(store).run(req)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(comp, store))
    if args.json:
        Path(args.json).write_text(json.dumps(comp.to_dict(), indent=2))
    print(f"report -> {out}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    store = _store()
    base_req = EngineRequest(target=_default_target(),
                             overlap_policy=OverlapPolicy.PRIORITY,
                             priority=("ALPHA", "BETA"))
    base_comp = FusionEngine(store).run(base_req)
    base_integral = base_comp.summary["aggregate"]["sum"]
    results = []
    for target_cells, target_size in [
        ((8, 6), 6.0), ((16, 12), 3.0), ((24, 18), 2.0), ((48, 36), 1.0),
    ]:
        nx, ny = target_cells
        t = GridDef("BENCH", nx=nx, ny=ny, dx=target_size, dy=target_size)
        req = EngineRequest(target=t, overlap_policy=OverlapPolicy.PRIORITY,
                            priority=("ALPHA", "BETA"))
        t0 = time.perf_counter()
        comp = FusionEngine(store).run(req)
        dt = time.perf_counter() - t0
        results.append({
            "nx": nx, "ny": ny, "cell_size": target_size,
            "target_cells": t.n_cells,
            "seconds": round(dt, 4),
            "cells_per_second": round(t.n_cells / dt, 1) if dt else None,
            "support_area": comp.summary["aggregate"]["support_area"],
            "integral": comp.summary["aggregate"]["sum"],
            "coverage": comp.summary["coverage"]["observed_fraction"],
            "integral_vs_baseline": comp.summary["aggregate"]["sum"] / base_integral,
        })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "runs": results,
        "baseline": {"integral": base_integral,
                     "target": _default_target().to_dict()},
    }, indent=2))
    print(json.dumps(results, indent=2))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    store = _store()
    target = _default_target()
    engine = FusionEngine(store)
    # AREA policy on purpose: a revised BETA cell must actually move the
    # merged value.  Under PRIORITY, ALPHA owns the overlap and the revision
    # would be invisible - which is itself worth seeing in the demo.
    base = engine.run(EngineRequest(target=target, overlap_policy=OverlapPolicy.AREA))
    patch = apply_patch(store.get("BETA"), _load("beta_patch_v2.json"))
    store.replace_source(patch)
    after = engine.run(EngineRequest(target=target, overlap_policy=OverlapPolicy.AREA))
    diff = compare_wire(base, after)
    payload = {
        "baseline": {
            "fingerprint": base.fingerprint,
            "mean": base.summary["mean"]["mean"],
            "aggregate": base.summary["aggregate"]["sum"],
            "coverage": base.summary["coverage"]["observed_fraction"],
        },
        "revised": {
            "fingerprint": after.fingerprint,
            "mean": after.summary["mean"]["mean"],
            "aggregate": after.summary["aggregate"]["sum"],
            "coverage": after.summary["coverage"]["observed_fraction"],
        },
        "diff": diff,
        "wire_keys": sorted(artifact_wire(base)),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(json.dumps({k: payload[k] for k in ("baseline", "revised")}, indent=2))
    print(f"{diff['n_changed']} cells changed -> {out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gridmerge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("report")
    p.add_argument("--out", default="reports/baseline.html")
    p.add_argument("--json", default=None)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("bench")
    p.add_argument("--out", default="reports/bench.json")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("demo")
    p.add_argument("--out", default="reports/session.json")
    p.set_defaults(fn=cmd_demo)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
