"""Wire format for the workbench: compact arrays instead of one object per cell.

Numbers and flags only - no cell records, because the client should never have
to re-derive a statistic.  The report generator and the CLI use this too, so
the page, the JSON artefact and the HTML report all describe one run.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .engine import Computation, request_to_dict
from .model import Status

STATUS_CODE = {
    "valid": 1,
    "overlap": 2,
    "zonal": 3,
    "missing": 4,
    "gap": 5,
}

ORIGIN_CODE = {"source": 0, "overlap": 1, "zonal": 2, "none": -1}


def artifact_wire(comp: Computation) -> dict:
    """Everything needed to draw the artefact and its numbers, nothing more."""
    t = comp.request.target
    sources = [s["source"] for s in comp.source_summaries]
    n = len(comp.cells)
    values: List[Optional[float]] = [None] * n
    status = [0] * n
    owner = [-1] * n          # index into ``sources`` for single-source cells
    support = [0] * n         # area_fraction, 4 decimal places
    for c in comp.cells:
        values[c.index] = None if c.value is None else round(c.value, 5)
        status[c.index] = STATUS_CODE.get(c.status.value, 0)
        if c.origin.value == "source" and c.contributors:
            try:
                owner[c.index] = sources.index(c.contributors[0])
            except ValueError:
                owner[c.index] = -1
        support[c.index] = int(round(c.area_fraction * 10000))

    scale_min = min((v for v in values if v is not None), default=0.0)
    scale_max = max((v for v in values if v is not None), default=1.0)
    if scale_max <= scale_min:
        scale_max = scale_min + 1.0

    return {
        "computation_id": comp.computation_id,
        "fingerprint": comp.fingerprint,
        "target": t.to_dict(),
        "request": request_to_dict(comp.request),
        "source_order": sources,
        "sources": comp.source_summaries,
        "scale": {"min": scale_min, "max": scale_max},
        "values": values,
        "status": status,
        "owner": owner,
        "support": support,
        "summary": comp.summary,
        "blocks": comp.blocks,
        "overlap_summary": comp.overlap_summary,
        "warnings": comp.warnings,
        "manifest": comp.manifest.to_dict(),
        "timing": {
            "started_at": comp.started_at, "finished_at": comp.finished_at,
            "duration_s": comp.duration_s,
        },
        "layers": _layers(comp, sources),
    }


def _layers(comp: Computation, sources: Sequence[str]) -> dict:
    """Three categorical overlays the page can toggle without recomputing."""
    t = comp.request.target
    n = t.n_cells
    ownership = [0] * n       # 0 none, 1 single, 2 overlap, 3 invented
    missing = [0] * n         # 1 = no value
    conflict = [0] * n        # 1 = sources disagree by more than 5% of |A|
    for c in comp.cells:
        if c.status is Status.OVERLAP:
            ownership[c.index] = 2
        elif c.status is Status.VALID:
            ownership[c.index] = 1
        elif c.status is Status.ZONAL:
            ownership[c.index] = 3
        if c.value is None:
            missing[c.index] = 1
    for r in comp.overlaps:
        if r.case != "both" or r.value_a is None or r.value_b is None:
            continue
        # 5% relative: tight enough to surface the BETA bias, loose enough to
        # ignore rounding dust between providers.
        if abs(r.value_b - r.value_a) > 0.05 * max(abs(r.value_a), 1e-9):
            for i in r.target_cells:
                if 0 <= i < n:
                    conflict[i] = 1
    return {
        "ownership": ownership,
        "missing": missing,
        "conflict": conflict,
    }


def overlap_ledger_wire(records) -> dict:
    return {"records": [r.to_dict() for r in records]}


def compare_wire(a: Computation, b: Computation) -> dict:
    """Cell-by-cell comparison of two computations on the same target geometry.

    Refuses to compare different target definitions: a diff between different
    grids is not a revision diff, it is a change of question.
    """
    if not a.request.target.same_geometry(b.request.target):
        raise ValueError("cannot compare computations on different target geometries")
    changed = []
    for ca, cb in zip(a.cells, b.cells):
        if ca.value is None and cb.value is None:
            continue
        if ca.value is None or cb.value is None or abs(ca.value - cb.value) > 1e-12:
            changed.append({
                "index": ca.index, "row": ca.row, "col": ca.col,
                "before": ca.value, "after": cb.value,
                "delta": None if ca.value is None or cb.value is None
                         else cb.value - ca.value,
                "status_before": ca.status.value, "status_after": cb.status.value,
            })
    return {
        "before": {"computation_id": a.computation_id, "fingerprint": a.fingerprint},
        "after": {"computation_id": b.computation_id, "fingerprint": b.fingerprint},
        "changed_cells": changed,
        "n_changed": len(changed),
        "mean_before": a.summary["mean"]["mean"] if a.summary["mean"] else None,
        "mean_after": b.summary["mean"]["mean"] if b.summary["mean"] else None,
        "aggregate_before": a.summary["aggregate"]["sum"],
        "aggregate_after": b.summary["aggregate"]["sum"],
    }
