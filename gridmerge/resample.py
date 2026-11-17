"""Resampling between planar rectangular grids.

Every function here is *separately named by the quantity it preserves*.
There is deliberately no generic ``interpolate()`` entry point: a count must
not be smoothed like a concentration, and a concentration must not be
area-summed like a count.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .grid import Grid, intersection
from .model import GridDef, snap

#: Source keys that may not be used as a statistic, because they are metadata.
RESERVED_KEYS = frozenset({
    "nx", "ny", "dx", "dy", "x", "y", "row", "col", "index", "source",
    "version", "weight", "area", "error_var", "tau", "valid", "status",
})


def _overlap_area(
    tdef: GridDef, trow: int, tcol: int, sdef: GridDef, srow: int, scol: int
) -> float:
    tx0 = tdef.origin_x + tcol * tdef.dx
    ty0 = tdef.origin_y + trow * tdef.dy
    sx0 = sdef.origin_x + scol * sdef.dx
    sy0 = sdef.origin_y + srow * sdef.dy
    return intersection(
        (tx0, ty0, tx0 + tdef.dx, ty0 + tdef.dy),
        (sx0, sy0, sx0 + sdef.dx, sy0 + sdef.dy),
    )


# ---------------------------------------------------------------------------
# Nearest neighbour
# ---------------------------------------------------------------------------
def nearest_neighbour(
    target: GridDef, source: Grid, value_key: str = "value"
) -> Tuple[List[Optional[float]], List[List[Tuple[str, int, float]]]]:
    """Sample the source cell containing each target cell centre.

    Preserves *nothing* globally: no area, no sum, no mean.  It is the
    right choice for categorical / ordinal fields (land-cover class, soil
    type) where averaging is meaningless, and it is intentionally labelled
    as non-conservative.  A target centre landing on a missing source cell
    stays missing - it is never zero-filled or borrowed from a neighbour.
    """
    if value_key != "value":
        raise ValueError("nearest_neighbour samples the source values, not a statistic")
    vals: List[Optional[float]] = []
    prov: List[List[Tuple[str, int, float]]] = []
    for r in range(target.ny):
        yc = target.origin_y + (r + 0.5) * target.dy
        for c in range(target.nx):
            xc = target.origin_x + (c + 0.5) * target.dx
            if not (source.definition.x0 <= xc < source.definition.x1
                    and source.definition.y0 <= yc < source.definition.y1):
                vals.append(None)
                prov.append([])
                continue
            sc = min(source.definition.nx - 1,
                     int(math.floor((xc - source.definition.origin_x) / source.definition.dx)))
            sr = min(source.definition.ny - 1,
                     int(math.floor((yc - source.definition.origin_y) / source.definition.dy)))
            v = source.values[sr][sc]
            vals.append(v)
            prov.append([(source.source_id, sr * source.definition.nx + sc,
                          source.definition.cell_area)] if v is not None else [])
    return vals, prov


# ---------------------------------------------------------------------------
# Conservative (area-weighted) resampling
# ---------------------------------------------------------------------------
def overlap_weights(
    target: GridDef, source: GridDef, trow: int, tcol: int
) -> List[Tuple[int, int, float]]:
    """``(srow, scol, overlap_area)`` for every source cell overlapping a target.

    Only cells with strictly positive overlap are returned; conservative
    weights are these areas normalised by the target cell area.
    """
    tx0 = target.origin_x + tcol * target.dx
    ty0 = target.origin_y + trow * target.dy
    tx1, ty1 = tx0 + target.dx, ty0 + target.dy
    eps = 1e-9
    sc0 = max(0, int(math.floor((tx0 - source.origin_x) / source.dx + eps)))
    sc1 = min(source.nx - 1, int(math.ceil((tx1 - source.origin_x) / source.dx - eps)) - 1)
    sr0 = max(0, int(math.floor((ty0 - source.origin_y) / source.dy + eps)))
    sr1 = min(source.ny - 1, int(math.ceil((ty1 - source.origin_y) / source.dy - eps)) - 1)
    out: List[Tuple[int, int, float]] = []
    for sr in range(sr0, sr1 + 1):
        for sc in range(sc0, sc1 + 1):
            a = _overlap_area(target, trow, tcol, source, sr, sc)
            if a > 1e-12:
                out.append((sr, sc, a))
    return out


def conservative_sum(
    target: GridDef, source: Grid, *, stat_scale: str = "intensive"
) -> Tuple[List[Optional[float]], List[List[Tuple[str, int, float]]]]:
    """Area-weighted *aggregate* of an intensive field (mean concentration).

    ``sum_i (v_i * a_i) / sum_i a_i`` over valid source cells that overlap the
    target cell.  The denominator is the *covered* area, not the cell area -
    data gaps do not drag the mean down.  Coverage is therefore returned
    separately (``area_fraction``) so a caller can weight by real support.

    WHAT IS PRESERVED: the area integral of the *observation field* over the
    part of the domain where the source has data, i.e. summing
    ``value * area_fraction * cell_area`` over target cells reproduces
    ``sum(v_i * a_i)`` over source cells.  WHAT IS NOT: it does not preserve a
    per-target-cell count, and ``value`` is quotable ONLY as an intensive
    quantity (a mean over support).
    """
    if stat_scale not in ("intensive", "value"):
        raise ValueError(
            "conservative_sum aggregates an intensive field; use conservative_count "
            "for totals"
        )
    vals: List[Optional[float]] = []
    prov: List[List[Tuple[str, int, float]]] = []
    for r in range(target.ny):
        for c in range(target.nx):
            num = 0.0
            den = 0.0
            cells: List[Tuple[str, int, float]] = []
            for sr, sc, a in overlap_weights(target, source.definition, r, c):
                v = source.values[sr][sc]
                if v is None:
                    continue
                num += v * a
                den += a
                cells.append((source.source_id, sr * source.definition.nx + sc, snap(a, 1e-12)))
            if den <= 1e-12:
                vals.append(None)
                prov.append([])
            else:
                vals.append(num / den)
                prov.append(cells)
    return vals, prov


def conservative_mean(
    target: GridDef, source: Grid, *, method: str = "conservative",
    value_key: str = "value",
) -> Tuple[List[Optional[float]], List[List[Tuple[str, int, float]]]]:
    """Alias for :func:`conservative_sum` with an intensive-field contract.

    The name says *mean* because that is what the number is; the arithmetic is
    the same support-weighted mean.  Kept separate so call sites read honestly.
    """
    if method != "conservative":
        raise ValueError("conservative_mean is only defined for area weighting")
    return conservative_sum(target, source, stat_scale="intensive")


def conservative_count(
    target: GridDef, source: Grid
) -> Tuple[List[Optional[float]], List[List[Tuple[str, int, float]]]]:
    """Distribute an extensive *count* field onto the target grid.

    A source cell whose value is ``v`` over area ``a`` has density ``v/a``.
    The target cell receives ``sum_i (v_i/a_i) * overlap_i``.  Missing source
    cells contribute nothing (they are a gap in support, not a zero count),
    so totals are preserved over covered area only.
    """
    vals: List[Optional[float]] = []
    prov: List[List[Tuple[str, int, float]]] = []
    for r in range(target.ny):
        for c in range(target.nx):
            total = 0.0
            covered = 0.0
            cells: List[Tuple[str, int, float]] = []
            for sr, sc, a in overlap_weights(target, source.definition, r, c):
                covered += a
                v = source.values[sr][sc]
                if v is None:
                    continue
                total += v * (a / source.definition.cell_area)
                cells.append((source.source_id, sr * source.definition.nx + sc, snap(a, 1e-12)))
            if covered <= 1e-12:
                vals.append(None)
                prov.append([])
            else:
                vals.append(total)
                prov.append(cells)
    return vals, prov


# ---------------------------------------------------------------------------
# Zone of influence - a restricted, explicitly non-conservative fallback
# ---------------------------------------------------------------------------
def zone_of_influence(
    target: GridDef,
    source: Grid,
    max_distance: float,
    *,
    weight_fn: str = "gaussian",
) -> Tuple[List[Optional[float]], List[List[Tuple[str, int, float]]], List[float]]:
    """Gap-filling only: borrow from nearby valid cells within ``max_distance``.

    This is the *only* path that creates values where no source cell overlaps
    the target cell.  It is labelled ``origin=zonal`` downstream and must be
    reported separately from observed and merged values.

    ``weight_fn``: ``gaussian`` uses ``exp(-d^2 / (2 tau^2))`` with
    ``source.tau`` (required); ``inverse_square`` uses ``1/(d^2+eps)`` and
    needs no ``tau``.  Weights are normalised over the borrowing window.

    WHAT IS PRESERVED: nothing.  It can invent smooth structure, so totals are
    not conserved and the result must never be quoted as a count.
    """
    if max_distance <= 0:
        raise ValueError("max_distance must be > 0")
    if weight_fn == "gaussian":
        if source.tau is None:
            raise ValueError("gaussian zone_of_influence needs source.tau")
        tau = source.tau
    elif weight_fn == "inverse_square":
        tau = None
    else:
        raise ValueError(f"unknown weight_fn {weight_fn!r}")

    valid = source.valid_indices()
    vals: List[Optional[float]] = []
    prov: List[List[Tuple[str, int, float]]] = []
    borrowed: List[float] = []
    if not valid:
        n = target.n_cells
        return [None] * n, [[] for _ in range(n)], [0.0] * n

    # Plain Euclidean distance between cell centres, in planar units.
    for r in range(target.ny):
        yc = target.origin_y + (r + 0.5) * target.dy
        for c in range(target.nx):
            xc = target.origin_x + (c + 0.5) * target.dx
            # If the target cell is already overlapped by a source cell, this
            # function is not applicable - the caller must not use it there.
            num = 0.0
            den = 0.0
            cells: List[Tuple[str, int, float]] = []
            for sr, sc in valid:
                ysc = source.definition.origin_y + (sr + 0.5) * source.definition.dy
                xsc = source.definition.origin_x + (sc + 0.5) * source.definition.dx
                d = math.hypot(xc - xsc, yc - ysc)
                if d > max_distance:
                    continue
                if weight_fn == "gaussian":
                    w = math.exp(-(d * d) / (2.0 * tau * tau))  # type: ignore[operator]
                else:
                    w = 1.0 / (d * d + 1e-12)
                num += w * source.values[sr][sc]  # type: ignore[operator]
                den += w
                cells.append((source.source_id, sr * source.definition.nx + sc, w))
            if den > 0:
                vals.append(num / den)
                borrowed.append(den)
                prov.append(cells)
            else:
                vals.append(None)
                borrowed.append(0.0)
                prov.append([])
    return vals, prov, borrowed
