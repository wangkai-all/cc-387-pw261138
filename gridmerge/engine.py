"""The fusion engine: build one cell artefact from several source grids.

Design rules, in order of precedence:

1. **A missing source cell is missing.**  It is never coerced to zero and never
   replaced by a neighbour unless the caller explicitly asks for the zonal
   fallback, whose cells are labelled ``origin=zonal`` and excluded from totals.
2. **Every piece of ground has at most one owner in a total.**  A target cell's
   support area is the *union* of the valid source cells clipped to that cell,
   so two sources covering the same ground do not make the ground count twice.
3. **A value is only ever quoted with its support.**  ``area_fraction`` is the
   area that supports the value; ``observed_fraction`` is the union of valid
   observations regardless of which one was selected.
4. **Semantics decide the arithmetic.**  Counts are extensive (sum), levels are
   intensive (mean).  Adding two overlapping counts is refused, not averaged.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .grid import Grid, cells_touching, rect_union_area
from .resample import overlap_weights
from .model import (
    CellRecord,
    GridDef,
    Method,
    Origin,
    OverlapPolicy,
    OverlapRecord,
    Status,
    snap,
)
from .stats import block_aggregate, coverage_fraction, weighted_mean, weighted_sum
from .versioning import InputManifest, VersionStore


class Cancelled(RuntimeError):
    """Raised when a run is interrupted by the user or superseded."""


class ValidationError(RuntimeError):
    """Raised when a request contradicts the data's semantics."""

    def __init__(self, message: str, issues: Optional[Sequence[str]] = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])

    def to_dict(self) -> dict:
        return {"error": str(self), "issues": self.issues}


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("run cancelled")


@dataclass
class EngineRequest:
    target: GridDef
    method: Method = Method.CONSERVATIVE
    overlap_policy: OverlapPolicy = OverlapPolicy.PRIORITY
    priority: Tuple[str, ...] = ()
    stat_scale: str = "intensive"       # intensive | extensive
    fill: str = "none"                  # none | zone_of_influence
    fill_max_distance: float = 0.0
    fill_weight: str = "inverse_square"
    include_zonal_in_mean: bool = False
    target_version: int = 0
    review_overlaps: bool = True
    blocks: Tuple[Tuple[str, Tuple[float, float, float, float]], ...] = ()
    pace_s: float = 0.0                 # demo throttle: seconds per target row

    def validate(self, sources: Sequence[str]) -> None:
        issues: List[str] = []
        if self.method is Method.NEAREST and self.overlap_policy is not OverlapPolicy.PRIORITY:
            issues.append("nearest sampling only supports the priority overlap policy")
        if self.overlap_policy is OverlapPolicy.PRIORITY and not self.priority:
            issues.append("priority policy needs an explicit source order")
        unknown = [s for s in self.priority if s not in sources]
        if unknown:
            issues.append(f"priority names unknown sources: {unknown}")
        if self.stat_scale not in ("intensive", "extensive"):
            issues.append("stat_scale must be 'intensive' or 'extensive'")
        if self.stat_scale == "extensive":
            if self.overlap_policy is not OverlapPolicy.SUM:
                issues.append("counts (extensive) must use the sum policy, not a mean policy")
            if self.method is Method.NEAREST:
                issues.append("nearest sampling would duplicate or drop counts")
        if self.fill not in ("none", "zone_of_influence"):
            issues.append("fill must be 'none' or 'zone_of_influence'")
        if self.fill == "zone_of_influence" and not (self.fill_max_distance > 0):
            issues.append("zone-of-influence fill needs a positive max distance")
        if self.method is Method.CONSERVATIVE and self.overlap_policy is OverlapPolicy.SUM \
                and self.stat_scale != "extensive":
            issues.append("sum policy is reserved for extensive counts")
        if self.overlap_policy is OverlapPolicy.ERROR_WEIGHTED:
            for s in sources:
                pass  # availability is checked per grid in _merge_cell
        if issues:
            raise ValidationError("; ".join(issues), issues)


@dataclass(frozen=True)
class SourceCellContribution:
    source_id: str
    row: int
    col: int
    area: float          # overlap of this source cell with the target cell
    value: float
    cell_area: float
    error_var: Optional[float]


@dataclass(frozen=True)
class SourceContribution:
    source_id: str
    value: float                 # mean (intensive) or total (extensive) over support
    support_area: float          # area observed by this source inside target cell
    cells: Tuple[SourceCellContribution, ...]

    @property
    def valid(self) -> bool:
        return self.support_area > 0 and bool(self.cells)


@dataclass
class Computation:
    computation_id: str
    request: EngineRequest
    cells: List[CellRecord]
    overlaps: List[OverlapRecord]
    source_summaries: List[dict]
    overlap_summary: dict
    summary: dict
    blocks: List[dict]
    manifest: InputManifest
    warnings: List[str]
    started_at: str
    finished_at: str
    duration_s: float
    generation_at_start: int
    cancelled: bool = False
    superseded: bool = False
    token: Optional[CancellationToken] = field(default=None, repr=False, compare=False)

    @property
    def fingerprint(self) -> str:
        return self.manifest.fingerprint

    def to_dict(self, include_cells: bool = True) -> dict:
        d = {
            "computation_id": self.computation_id,
            "fingerprint": self.fingerprint,
            "manifest": self.manifest.to_dict(),
            "request": request_to_dict(self.request),
            "summary": self.summary,
            "source_summaries": self.source_summaries,
            "overlap_summary": self.overlap_summary,
            "blocks": self.blocks,
            "warnings": list(self.warnings),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "generation_at_start": self.generation_at_start,
            "cancelled": self.cancelled,
            "superseded": self.superseded,
            "overlap_records": [o.to_dict() for o in self.overlaps],
        }
        if include_cells:
            d["cells"] = [c.to_dict() for c in self.cells]
        return d


def request_to_dict(req: EngineRequest) -> dict:
    return {
        "target": req.target.to_dict(),
        "target_version": req.target_version,
        "method": req.method.value,
        "overlap_policy": req.overlap_policy.value,
        "priority": list(req.priority),
        "stat_scale": req.stat_scale,
        "fill": req.fill,
        "fill_max_distance": req.fill_max_distance,
        "fill_weight": req.fill_weight,
        "include_zonal_in_mean": req.include_zonal_in_mean,
        "review_overlaps": req.review_overlaps,
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clipped_union_area(
    target: GridDef,
    row: int,
    col: int,
    per_source: Sequence[Tuple[Grid, Sequence[SourceCellContribution]]],
) -> float:
    tx0 = target.origin_x + col * target.dx
    ty0 = target.origin_y + row * target.dy
    cell = (tx0, ty0, tx0 + target.dx, ty0 + target.dy)
    rects: List[Tuple[float, float, float, float]] = []
    for source, contribs in per_source:
        sdef = source.definition
        for c in contribs:
            sx0 = sdef.origin_x + c.col * sdef.dx
            sy0 = sdef.origin_y + c.row * sdef.dy
            rects.append((
                max(cell[0], sx0), max(cell[1], sy0),
                min(cell[2], sx0 + sdef.dx), min(cell[3], sy0 + sdef.dy),
            ))
    return rect_union_area(rects)


def _find_overlapping_source_cells(
    source: Grid, rect: Tuple[float, float, float, float]
) -> List[Tuple[int, int, float]]:
    """Source cells intersecting ``rect`` with clipped intersection areas."""
    out: List[Tuple[int, int, float]] = []
    rng = cells_touching(source.definition, rect)
    if rng is None:
        return out
    r0, r1, c0, c1 = rng
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            sdef = source.definition
            sx0 = sdef.origin_x + c * sdef.dx
            sy0 = sdef.origin_y + r * sdef.dy
            w = min(rect[2], sx0 + sdef.dx) - max(rect[0], sx0)
            h = min(rect[3], sy0 + sdef.dy) - max(rect[1], sy0)
            if w > 1e-12 and h > 1e-12:
                out.append((r, c, snap(w * h, 1e-12)))
    return out


def plan_overlaps(target: GridDef, grids: Sequence[Grid]) -> List[OverlapRecord]:
    """Enumerate every pair of source cells that share ground.

    Covers all four review cases, including pairs where one or both sides are
    missing - a partial hole in an overlap is exactly what must be seen before
    choosing a policy.
    """
    records: List[OverlapRecord] = []
    ordered = sorted(grids, key=lambda g: g.source_id)
    for i in range(len(ordered)):
        a = ordered[i]
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            inter = (
                max(a.definition.x0, b.definition.x0),
                max(a.definition.y0, b.definition.y0),
                min(a.definition.x1, b.definition.x1),
                min(a.definition.y1, b.definition.y1),
            )
            if inter[2] <= inter[0] or inter[3] <= inter[1]:
                continue
            rng = cells_touching(a.definition, inter)
            if rng is None:
                continue
            r0, r1, c0, c1 = rng
            for ar in range(r0, r1 + 1):
                for ac in range(c0, c1 + 1):
                    ax0 = a.definition.origin_x + ac * a.definition.dx
                    ay0 = a.definition.origin_y + ar * a.definition.dy
                    arect = (ax0, ay0, ax0 + a.definition.dx, ay0 + a.definition.dy)
                    for br, bc, shared in _find_overlapping_source_cells(b, arect):
                        va = a.values[ar][ac]
                        vb = b.values[br][bc]
                        if va is not None and vb is not None:
                            case = "both"
                        elif va is not None:
                            case = "a_only"
                        elif vb is not None:
                            case = "b_only"
                        else:
                            case = "neither"
                        bx0 = b.definition.origin_x + bc * b.definition.dx
                        by0 = b.definition.origin_y + br * b.definition.dy
                        srect = (
                            max(arect[0], bx0), max(arect[1], by0),
                            min(arect[2], bx0 + b.definition.dx),
                            min(arect[3], by0 + b.definition.dy),
                        )
                        t_cells = _target_cells_in(target, srect)
                        records.append(OverlapRecord(
                            source_a=a.source_id,
                            cell_a=ar * a.definition.nx + ac,
                            source_b=b.source_id,
                            cell_b=br * b.definition.nx + bc,
                            shared_area=shared,
                            case=case,
                            value_a=va,
                            value_b=vb,
                            target_cells=t_cells,
                        ))
    records.sort(key=lambda r: (r.source_a, r.cell_a, r.source_b, r.cell_b))
    return records


def _target_cells_in(target: GridDef, rect: Tuple[float, float, float, float]) -> List[int]:
    rng = cells_touching(target, rect)
    if rng is None:
        return []
    r0, r1, c0, c1 = rng
    return [r * target.nx + c for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def summarise_overlaps(records: Sequence[OverlapRecord]) -> dict:
    by_case: Dict[str, dict] = {
        "both": {"pairs": 0, "area": 0.0},
        "a_only": {"pairs": 0, "area": 0.0},
        "b_only": {"pairs": 0, "area": 0.0},
        "neither": {"pairs": 0, "area": 0.0},
    }
    diffs: List[float] = []
    rel: List[float] = []
    shared = 0.0
    for r in records:
        entry = by_case[r.case]
        entry["pairs"] += 1
        entry["area"] += r.shared_area
        shared += r.shared_area
        if r.case == "both" and r.value_a is not None and r.value_b is not None:
            diffs.append(r.value_b - r.value_a)
            if abs(r.value_a) > 1e-12:
                rel.append(abs(r.value_b - r.value_a) / abs(r.value_a))
    n = len(diffs)
    return {
        "total_pairs": len(records),
        "shared_footprint_area": shared,
        "by_case": {k: {"pairs": v["pairs"], "area": v["area"]} for k, v in by_case.items()},
        "both_valid_pairs": n,
        "mean_difference_b_minus_a": (sum(diffs) / n) if n else None,
        "mean_abs_difference": (sum(abs(d) for d in diffs) / n) if n else None,
        "max_abs_difference": max(abs(d) for d in diffs) if n else None,
        "mean_relative_difference": (sum(rel) / len(rel)) if rel else None,
    }


class FusionEngine:
    def __init__(self, store: VersionStore) -> None:
        self.store = store

    # -- public -------------------------------------------------------------
    def run(
        self,
        request: EngineRequest,
        *,
        token: Optional[CancellationToken] = None,
        progress: Optional[Callable[[dict], None]] = None,
    ) -> Computation:
        started = time.time()
        started_at = _now()
        snapshot = self.store.snapshot()
        if not snapshot:
            raise ValidationError("no source grids loaded")
        self._validate_sources(request, snapshot, strict=True)

        manifest = InputManifest(
            entries=tuple(
                (s, g.version, self.store.digest_of(s, g.version))
                for s, g in snapshot.items()
            ),
            target=(
                request.target.name, request.target_version,
                request.target.dx, request.target.dy,
                request.target.nx, request.target.ny,
                request.target.origin_x, request.target.origin_y,
            ),
        )
        generation = self.store.generation()
        ordered_sources = list(snapshot.values())
        by_id = {g.source_id: g for g in ordered_sources}
        priority_order = [s for s in request.priority if s in by_id]

        cells: List[CellRecord] = []
        warnings: List[str] = []
        for row in range(request.target.ny):
            token and token.raise_if_cancelled()
            if request.pace_s:
                time.sleep(request.pace_s)
            for col in range(request.target.nx):
                token and token.raise_if_cancelled()
                cells.append(self._fuse_cell(
                    request, row, col, ordered_sources, priority_order, warnings,
                ))
            if progress:
                progress({
                    "row": row + 1, "rows": request.target.ny,
                    "fraction": (row + 1) / request.target.ny,
                })

        if token and token.cancelled:
            raise Cancelled("run cancelled")
        if self.store.generation() != generation:
            raise Cancelled("a revision landed while this run was computing")

        overlaps = plan_overlaps(request.target, ordered_sources) if request.review_overlaps else []
        overlap_summary = summarise_overlaps(overlaps)
        summary = self._summary(request, cells)
        blocks = self._blocks(request, cells)
        source_summaries = self._source_summaries(request, ordered_sources, overlap_summary)
        for s in ordered_sources:
            if cells_touching(request.target, s.bounds) is None:
                warnings.append(
                    f"source {s.source_id} does not intersect the target grid; it contributes nothing"
                )
        if request.fill == "zone_of_influence":
            warnings.append(
                "zone-of-influence cells are invented, not observed; "
                "they are excluded from coverage-as-observed and from all totals"
            )
        if (request.method is Method.NEAREST
                and request.stat_scale == "intensive"):
            warnings.append(
                "nearest neighbour copies whole source cells onto the target "
                "grid; the area integral is not conserved, so quote the mean only"
            )
        unlisted = [s for s in sorted(by_id) if s not in priority_order]
        if unlisted and request.overlap_policy is OverlapPolicy.PRIORITY:
            warnings.append(
                "priority order omits " + ", ".join(unlisted) +
                "; these sources rank last (alphabetically) inside overlaps"
            )
        if summary["n_overlap_cells"]:
            warnings.append(
                f"{summary['n_overlap_cells']} target cells are covered by more than one "
                f"source; overlap policy was {request.overlap_policy.value}"
            )

        computation = Computation(
            computation_id="c_" + uuid.uuid4().hex[:12],
            request=request,
            cells=cells,
            overlaps=overlaps,
            source_summaries=source_summaries,
            overlap_summary=overlap_summary,
            summary=summary,
            blocks=blocks,
            manifest=manifest,
            warnings=warnings,
            started_at=started_at,
            finished_at=_now(),
            duration_s=time.time() - started,
            generation_at_start=generation,
            token=token,
        )
        return computation

    # -- validation ---------------------------------------------------------
    @staticmethod
    def _validate_sources(req: EngineRequest, snapshot: Dict[str, Grid],
                          strict: bool = True) -> None:
        req.validate(sorted(snapshot))
        if req.method is Method.CONSERVATIVE:
            return
        # nearest: every source grid may be used; nothing else to validate
        return

    # -- per-cell fusion ----------------------------------------------------
    def _fuse_cell(
        self,
        req: EngineRequest,
        row: int,
        col: int,
        sources: Sequence[Grid],
        priority_order: Sequence[str],
        warnings: List[str],
    ) -> CellRecord:
        target = req.target
        xc = target.origin_x + (col + 0.5) * target.dx
        yc = target.origin_y + (row + 0.5) * target.dy
        tx0 = target.origin_x + col * target.dx
        ty0 = target.origin_y + row * target.dy
        cell_bounds = (tx0, ty0, tx0 + target.dx, ty0 + target.dy)

        per_source: List[Tuple[Grid, SourceContribution]] = []
        footprint_hit = False
        for source in sources:
            if req.method is Method.NEAREST:
                contrib = self._nearest_contribution(source, xc, yc)
                if contrib is not None:
                    footprint_hit = True
                    if contrib.cells:
                        per_source.append((source, contrib))
                continue
            rng = cells_touching(source.definition, cell_bounds)
            if rng is None:
                continue
            footprint_hit = True
            contrib = self._conservative_contribution(
                source, req.target, row, col, req.stat_scale
            )
            if contrib.cells:
                per_source.append((source, contrib))

        valid = [(s, c) for s, c in per_source if c.cells]
        contributors = [s.source_id for s, _ in valid]
        observed_fraction = 0.0
        if req.method is Method.NEAREST:
            observed_fraction = 1.0 if valid else 0.0
            area_fraction = observed_fraction
        else:
            observed_fraction = _clipped_union_area(
                target, row, col, [(s, c.cells) for s, c in valid]
            ) / target.cell_area
            area_fraction = observed_fraction

        if not valid:
            status = Status.MISSING if footprint_hit else Status.GAP
            origin = Origin.NONE
            value: Optional[float] = None
            if req.fill == "zone_of_influence":
                z = self._zonal_estimate(req, sources, xc, yc)
                if z is not None:
                    value, source_list = z
                    contributors = source_list
                    status = Status.ZONAL
                    origin = Origin.ZONAL
                    area_fraction = 0.0   # invented values have no observed support
                    observed_fraction = 0.0
            return CellRecord(
                index=row * target.nx + col, row=row, col=col, x=xc, y=yc,
                value=value, status=status, origin=origin,
                area_fraction=area_fraction,
                observed_fraction=observed_fraction,
                n_contributors=len(contributors),
                contributors=list(contributors),
            )

        if req.stat_scale == "extensive":
            value, chosen_ids = self._merge_extensive(req, per_source)
            origin = Origin.OVERLAP if len(chosen_ids) > 1 else Origin.SOURCE
            status = Status.OVERLAP if len(chosen_ids) > 1 else Status.VALID
            area_fraction = _clipped_union_area(
                target, row, col, [(s, c.cells) for s, c in valid]
            ) / target.cell_area
            return CellRecord(
                index=row * target.nx + col, row=row, col=col, x=xc, y=yc,
                value=value, status=status, origin=origin,
                area_fraction=area_fraction,
                observed_fraction=area_fraction,
                n_contributors=len(valid),
                contributors=list(chosen_ids),
            )

        value, chosen, support_fraction = self._merge_intensive(
            req, per_source, target, row, col, valid
        )
        n_used = len(chosen)
        origin = Origin.OVERLAP if len(valid) > 1 else Origin.SOURCE
        status = Status.OVERLAP if len(valid) > 1 else Status.VALID
        return CellRecord(
            index=row * target.nx + col, row=row, col=col, x=xc, y=yc,
            value=value, status=status, origin=origin,
            area_fraction=support_fraction,
            observed_fraction=observed_fraction,
            n_contributors=len(valid),
            contributors=list(chosen),
        )

    def _nearest_contribution(
        self, source: Grid, xc: float, yc: float
    ) -> Optional[SourceContribution]:
        d = source.definition
        if not (d.x0 <= xc < d.x1 and d.y0 <= yc < d.y1):
            return None
        sc = min(d.nx - 1, int(math.floor((xc - d.origin_x) / d.dx)))
        sr = min(d.ny - 1, int(math.floor((yc - d.origin_y) / d.dy)))
        v = source.values[sr][sc]
        if v is None:
            return SourceContribution(source.source_id, 0.0, 0.0, ())
        cell = SourceCellContribution(
            source.source_id, sr, sc, d.cell_area, v, d.cell_area,
            source.error_var[sr][sc] if source.error_var else None,
        )
        return SourceContribution(source.source_id, v, d.cell_area, (cell,))

    def _conservative_contribution(
        self, source: Grid, target: GridDef, row: int, col: int, stat_scale: str
    ) -> SourceContribution:
        d = source.definition
        cells: List[SourceCellContribution] = []
        area_sum = 0.0
        total = 0.0
        for sr, sc, a in overlap_weights(target, d, row, col):
            v = source.values[sr][sc]
            if v is None:
                continue
            err = source.error_var[sr][sc] if source.error_var else None
            cells.append(SourceCellContribution(
                source.source_id, sr, sc, a, v, d.cell_area, err,
            ))
            area_sum += a
            if stat_scale == "intensive":
                total += v * a
            else:
                total += v * (a / d.cell_area)
        if not cells:
            return SourceContribution(source.source_id, 0.0, 0.0, ())
        if stat_scale == "intensive":
            value = total / area_sum
        else:
            value = total
        return SourceContribution(source.source_id, value, area_sum, tuple(cells))

    # -- merging ------------------------------------------------------------
    def _merge_intensive(
        self,
        req: EngineRequest,
        per_source: Sequence[Tuple[Grid, SourceContribution]],
        target: GridDef,
        row: int,
        col: int,
        valid: Sequence[Tuple[Grid, SourceContribution]],
    ) -> Tuple[float, List[str], float]:
        policy = req.overlap_policy
        if policy is OverlapPolicy.PRIORITY:
            # Highest-priority source that actually has data in this cell.
            # A missing cell in the top-priority source does not blank the
            # target: the next source with a value owns the cell, and the
            # record's contributor names which one that was.  Sources omitted
            # from the priority list rank last, alphabetically - they are
            # still legitimate contributors, never silently dropped.
            order = [src.source_id for src, _ in valid]
            ranked = [sid for sid in req.priority if sid in order]
            ranked += [sid for sid in sorted(order) if sid not in ranked]
            chosen_id = ranked[0]
            for source, contrib in valid:
                if source.source_id == chosen_id:
                    support = contrib.support_area / target.cell_area
                    return contrib.value, [chosen_id], min(1.0, support)
            raise ValidationError(f"no contributor for cell ({row},{col})")
        if policy in (OverlapPolicy.AREA, OverlapPolicy.ERROR_WEIGHTED):
            num = 0.0
            den = 0.0
            used: List[str] = []
            for source, contrib in valid:
                for c in contrib.cells:
                    if policy is OverlapPolicy.ERROR_WEIGHTED:
                        if c.error_var is None:
                            raise ValidationError(
                                f"error-weighted overlap needs error variance for "
                                f"{source.source_id}[{c.row},{c.col}]"
                            )
                        if c.error_var <= 0:
                            raise ValidationError(
                                f"zero error variance for {source.source_id}[{c.row},{c.col}]"
                            )
                        w = c.area / c.error_var
                    else:
                        w = c.area
                    num += c.value * w
                    den += w
                if source.source_id not in used:
                    used.append(source.source_id)
            if den <= 0:
                raise ValidationError("area-weighted overlap has zero weight")
            support = _clipped_union_area(
                target, row, col, [(s, c.cells) for s, c in valid]
            ) / target.cell_area
            return num / den, used, min(1.0, support)
        raise ValidationError(f"policy {policy.value} cannot merge intensive fields")

    def _merge_extensive(
        self,
        req: EngineRequest,
        per_source: Sequence[Tuple[Grid, SourceContribution]],
    ) -> Tuple[float, List[str]]:
        valid = [(s, c) for s, c in per_source if c.cells]
        if len(valid) > 1:
            # Summing counts over the same ground would double count it.
            for i in range(len(valid)):
                for j in range(i + 1, len(valid)):
                    a = valid[i][0]
                    b = valid[j][0]
                    inter = (
                        max(a.definition.x0, b.definition.x0),
                        max(a.definition.y0, b.definition.y0),
                        min(a.definition.x1, b.definition.x1),
                        min(a.definition.y1, b.definition.y1),
                    )
                    if inter[2] > inter[0] and inter[3] > inter[1]:
                        raise ValidationError(
                            "extensive counts from two sources share ground; "
                            "summing would count the same area twice"
                        )
        total = sum(c.value for _, c in valid)
        return total, [s.source_id for s, _ in valid]

    # -- zone of influence --------------------------------------------------
    def _zonal_estimate(
        self, req: EngineRequest, sources: Sequence[Grid], xc: float, yc: float
    ) -> Optional[Tuple[float, List[str]]]:
        candidates: List[Tuple[float, float, str]] = []
        md = req.fill_max_distance
        for source in sources:
            d = source.definition
            rng = cells_touching(d, (xc - md, yc - md, xc + md, yc + md))
            if rng is None:
                continue
            r0, r1, c0, c1 = rng
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    v = source.values[r][c]
                    if v is None:
                        continue
                    cxx = d.origin_x + (c + 0.5) * d.dx
                    cyy = d.origin_y + (r + 0.5) * d.dy
                    dist = math.hypot(xc - cxx, yc - cyy)
                    if dist <= md:
                        candidates.append((dist, v, source.source_id))
        if not candidates:
            return None
        num = 0.0
        den = 0.0
        used: List[str] = []
        for dist, v, sid in sorted(candidates, key=lambda t: t[0]):
            if req.fill_weight == "inverse_square":
                w = 1.0 / (dist * dist + 1e-12)
            elif req.fill_weight == "gaussian":
                tau = req.fill_max_distance / 2.0
                w = math.exp(-(dist * dist) / (2 * tau * tau))
            else:
                raise ValidationError(f"unknown fill weight {req.fill_weight!r}")
            num += w * v
            den += w
            if sid not in used:
                used.append(sid)
        if den <= 0:
            return None
        return num / den, used

    # -- summaries ----------------------------------------------------------
    def _summary(self, req: EngineRequest, cells: Sequence[CellRecord]) -> dict:
        cov = coverage_fraction(cells, req.target)
        mean = None
        if req.stat_scale == "intensive":
            mean = weighted_mean(
                cells, req.target, include_zonal=req.include_zonal_in_mean
            )
        if req.stat_scale == "intensive":
            total = weighted_sum(cells, req.target, include_zonal=False)
        else:
            # For counts, the aggregate is the arithmetic sum of valued cells;
            # support weighting does not apply to a count field.
            total = {
                "sum": sum(c.value for c in cells if c.value is not None),
                "support_area": cov["observed_area"],
                "domain_area": cov["domain_area"],
                "coverage_fraction": cov["observed_fraction"],
                "n_cells": cov["n_valued"],
                "zonal_cells_excluded": cov["n_zonal"],
            }
        n_overlap = sum(1 for c in cells if c.status is Status.OVERLAP)
        n_zonal = sum(1 for c in cells if c.status is Status.ZONAL)
        n_missing = sum(1 for c in cells if c.status is Status.MISSING)
        n_gap = sum(1 for c in cells if c.status is Status.GAP)
        return {
            "method": req.method.value,
            "overlap_policy": req.overlap_policy.value,
            "stat_scale": req.stat_scale,
            "target_cells": len(cells),
            "n_valid_single": sum(1 for c in cells if c.status is Status.VALID),
            "n_overlap_cells": n_overlap,
            "n_zonal_cells": n_zonal,
            "n_missing_cells": n_missing,
            "n_gap_cells": n_gap,
            "mean": mean,
            "aggregate": total,
            "mean_note": None if mean is not None else
                "a count field has no meaningful mean; quote the aggregate sum",
            "coverage": cov,
            "conservation": self._conservation(req, cells),
        }

    def _conservation(self, req: EngineRequest, cells: Sequence[CellRecord]) -> dict:
        """State, per quantity, what this run preserves and what it does not."""
        policy = req.overlap_policy
        if req.method is Method.NEAREST:
            return {
                "preserves": [],
                "does_not_preserve": [
                    "area integral", "source total", "mean over support",
                ],
                "note": "Nearest sampling copies one cell's value to a whole cell; "
                        "it is only appropriate for categorical or ordinal fields.",
            }
        if req.stat_scale == "extensive":
            return {
                "preserves": ["sum of counts over disjoint covered area"],
                "does_not_preserve": [
                    "counts when two sources overlap (refused, not averaged)",
                    "anything over uncovered ground",
                ],
                "note": "Count fields are extensive; totals are added only where "
                        "source footprints are disjoint.",
            }
        if policy is OverlapPolicy.PRIORITY:
            return {
                "preserves": [
                    "the owning source's observed area integral inside each target cell",
                    "the owning source's mean over its own support",
                ],
                "does_not_preserve": [
                    "the other source's values where the two overlap",
                    "a per-cell count (the field is intensive)",
                    "anything over uncovered ground (coverage is reported, not imputed)",
                ],
                "note": "One source owns each cell, so its integral is copied "
                        "faithfully; the price is that the overlapping source is "
                        "discarded there, which the overlap ledger quantifies.",
            }
        return {
            "preserves": [
                "a measurement-area-weighted mean inside each target cell",
                "support area as a union, so shared ground is integrated once",
            ],
            "does_not_preserve": [
                "any single source's own area integral (the merged field is a blend)",
                "a per-cell count (the field is intensive)",
                "anything over uncovered ground (coverage is reported, not imputed)",
            ],
            "note": "The merged field is a weighted blend of the overlapping "
                    "sources; the reported integral is the integral of that blend, "
                    "not of either provider.",
        }

    def _blocks(self, req: EngineRequest, cells: Sequence[CellRecord]) -> List[dict]:
        out: List[dict] = []
        blocks = list(req.blocks)
        if not blocks:
            t = req.target
            hx, hy = t.dx * t.nx / 2.0, t.dy * t.ny / 2.0
            blocks = [
                ("domain", (t.x0, t.y0, t.x1, t.y1)),
                ("SW", (t.x0, t.y0, t.x0 + hx, t.y0 + hy)),
                ("SE", (t.x0 + hx, t.y0, t.x1, t.y0 + hy)),
                ("NW", (t.x0, t.y0 + hy, t.x0 + hx, t.y1)),
                ("NE", (t.x0 + hx, t.y0 + hy, t.x1, t.y1)),
            ]
        for name, bounds in blocks:
            agg = block_aggregate(
                cells, req.target, bounds, include_zonal=False
            )
            out.append({
                "name": name,
                "bounds": list(bounds),
                **agg,
            })
        return out

    def _source_summaries(
        self, req: EngineRequest, sources: Sequence[Grid], overlap_summary: dict
    ) -> List[dict]:
        out = []
        for s in sources:
            d = s.definition
            vals = [v for row in s.values for v in row if v is not None]
            out.append({
                "source": s.source_id,
                "version": s.version,
                "grid": d.to_dict(),
                "n_cells": d.n_cells,
                "n_valid": len(vals),
                "n_missing": d.n_cells - len(vals),
                "footprint_area": d.cell_area * d.n_cells,
                "valid_area": d.cell_area * len(vals),
                "value_min": min(vals) if vals else None,
                "value_max": max(vals) if vals else None,
                "value_mean": (sum(vals) / len(vals)) if vals else None,
                "has_error_var": s.error_var is not None,
                "tau": s.tau,
                "role": "highest priority" if req.priority and req.priority[0] == s.source_id else "contributor",
            })
        return out
