"""Pointwise and block statistics over the fused cell artefact.

Semantics that callers must rely on:

* A cell with ``value is None`` is *missing*; it contributes no value and no
  support area.  Nothing here ever substitutes zero.
* A cell's support is ``cell_area * area_fraction``.  Means divide by support,
  totals multiply by support, so a partially covered cell does not pretend to
  represent its whole footprint.
* ``origin=zonal`` cells were invented by :func:`gridmerge.resample.zone_of_influence`.
  They are excluded from statistics by default and, when included, are counted
  and reported separately.  They never enter a total.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from .model import CellRecord, GridDef, Origin, Status


class ZoneConflict(ValueError):
    """Raised when an operation's semantics contradict the data's semantics."""


def _support(record: CellRecord, cell_area: float) -> float:
    if record.value is None:
        return 0.0
    return cell_area * max(0.0, min(1.0, record.area_fraction))


def _select(
    records: Iterable[CellRecord], include_zonal: bool
) -> Tuple[List[CellRecord], int]:
    usable: List[CellRecord] = []
    zonal = 0
    for rec in records:
        if rec.value is None:
            continue
        if rec.origin is Origin.ZONAL:
            zonal += 1
            if not include_zonal:
                continue
        usable.append(rec)
    return usable, zonal


def weighted_mean(
    records: Sequence[CellRecord],
    target: GridDef,
    *,
    include_zonal: bool = False,
    weight_by: str = "support",
) -> Dict[str, float]:
    """Support-weighted mean of an intensive field.

    ``weight_by="support"`` weights by the observed area inside each cell.
    ``weight_by="cell"`` weights every valued cell equally - only valid when
    all valued cells have full support (otherwise it double-counts thin
    slivers and is rejected).
    """
    recs, zonal = _select(records, include_zonal)
    if weight_by not in ("support", "cell"):
        raise ValueError("weight_by must be 'support' or 'cell'")
    if weight_by == "cell":
        partial = [r for r in recs if abs(r.area_fraction - 1.0) > 1e-9]
        if partial:
            raise ZoneConflict(
                f"{len(partial)} valued cells have partial support; "
                "weight_by='cell' would over-count them"
            )
        w = [1.0 for _ in recs]
    else:
        w = [_support(r, target.cell_area) for r in recs]
    den = sum(w)
    if den <= 0:
        return {
            "mean": None, "n_cells": 0, "support_area": 0.0,
            "coverage_fraction": 0.0, "zonal_cells_excluded": zonal,
        }
    num = sum(w_i * r.value for w_i, r in zip(w, recs))  # type: ignore[operator]
    return {
        "mean": num / den,
        "n_cells": len(recs),
        "support_area": den,
        "coverage_fraction": den / (target.cell_area * target.n_cells),
        "zonal_cells_excluded": zonal,
    }


def weighted_sum(
    records: Sequence[CellRecord],
    target: GridDef,
    *,
    include_zonal: bool = False,
) -> Dict[str, float]:
    """Area integral of an intensive field: ``sum(value * support_area)``.

    This is the only quantity that should be quoted as an *aggregate* for an
    intensive field.  If any zonal cell is included the result is refused,
    because invented values have no conserved area integral.
    """
    recs, zonal = _select(records, include_zonal)
    if zonal and include_zonal:
        raise ZoneConflict(
            "zonal/invented cells cannot contribute to a conserved total; "
            "pass include_zonal=False"
        )
    total = 0.0
    support = 0.0
    for rec in recs:
        s = _support(rec, target.cell_area)
        total += rec.value * s  # type: ignore[operator]
        support += s
    return {
        "sum": total,
        "support_area": support,
        "domain_area": target.cell_area * target.n_cells,
        "coverage_fraction": support / (target.cell_area * target.n_cells),
        "n_cells": len(recs),
        "zonal_cells_excluded": zonal,
    }


def coverage_fraction(records: Sequence[CellRecord], target: GridDef) -> Dict[str, float]:
    """How much of the domain carries a value, and how much is a hole."""
    valued = 0.0
    observed = 0.0
    n_valued = n_missing = n_gap = n_zonal = 0
    for rec in records:
        a = target.cell_area
        f = max(0.0, min(1.0, rec.area_fraction))
        if rec.value is None:
            if rec.status is Status.GAP:
                n_gap += 1
            else:
                n_missing += 1
            continue
        n_valued += 1
        valued += a * f
        if rec.origin is Origin.ZONAL:
            n_zonal += 1
        else:
            observed += a * f
    domain = target.cell_area * target.n_cells
    return {
        "domain_area": domain,
        "valued_area": valued,
        "observed_area": observed,
        "valued_fraction": valued / domain,
        "observed_fraction": observed / domain,
        "n_valued": n_valued,
        "n_missing": n_missing,
        "n_gap": n_gap,
        "n_zonal": n_zonal,
    }


def block_aggregate(
    records: Sequence[CellRecord],
    target: GridDef,
    bounds: Tuple[float, float, float, float],
    *,
    include_zonal: bool = False,
) -> Dict[str, float]:
    """Mean and integral of an intensive field over a planar rectangle.

    Cells are clipped to the rectangle, so a block that ends mid-cell uses
    only its share.  The mean divides by the *valued* clipped area - a block
    straddling a data hole reports its own coverage rather than shrinking the
    mean toward zero.
    """
    x0, y0, x1, y1 = bounds
    if not (x1 > x0 and y1 > y0):
        raise ValueError("block bounds must have positive width and height")
    recs, zonal = _select(records, include_zonal)
    if zonal and include_zonal:
        raise ZoneConflict("zonal cells cannot contribute to a block total")
    integral = 0.0
    support = 0.0
    block_area = (x1 - x0) * (y1 - y0)
    n = 0
    for rec in recs:
        cx0 = rec.x - target.dx / 2
        cy0 = rec.y - target.dy / 2
        cx1 = rec.x + target.dx / 2
        cy1 = rec.y + target.dy / 2
        w = min(cx1, x1) - max(cx0, x0)
        h = min(cy1, y1) - max(cy0, y0)
        if w <= 1e-12 or h <= 1e-12:
            continue
        part = w * h
        frac = max(0.0, min(1.0, rec.area_fraction))
        s = part * frac
        integral += rec.value * s  # type: ignore[operator]
        support += s
        n += 1
    return {
        "mean": (integral / support) if support > 0 else None,
        "integral": integral,
        "support_area": support,
        "block_area": block_area,
        "coverage_fraction": support / block_area,
        "n_cells_touched": n,
        "zonal_cells_excluded": zonal,
    }
