"""Grid containers and ingestion of rectangular source payloads."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .model import GridDef, snap

MISSING_TOKENS = {"", "na", "nan", "null", "none", "missing", "-", "--"}


class GridPayloadError(ValueError):
    """Raised when a source payload is structurally invalid."""

    def __init__(self, message: str, issues: Optional[Sequence[str]] = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])


def _coerce_cell(raw: Any, where: str) -> Optional[float]:
    """Parse one cell.  ``None``/blank/NA -> missing, never zero."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise GridPayloadError(f"{where}: booleans are not observations")
    if isinstance(raw, (int, float)):
        v = float(raw)
        if math.isnan(v):
            return None
        if math.isinf(v):
            raise GridPayloadError(f"{where}: infinite value is not an observation")
        return v
    if isinstance(raw, str):
        token = raw.strip()
        if token.lower() in MISSING_TOKENS:
            return None
        try:
            v = float(token)
        except ValueError:
            raise GridPayloadError(f"{where}: {raw!r} is not numeric or a missing token")
        if math.isnan(v):
            return None
        if math.isinf(v):
            raise GridPayloadError(f"{where}: infinite value is not an observation")
        return v
    raise GridPayloadError(f"{where}: unsupported cell type {type(raw).__name__}")


def _as_matrix(rows: Sequence[Sequence[Any]], ny: int, nx: int, label: str) -> List[List[Optional[float]]]:
    if len(rows) != ny:
        raise GridPayloadError(f"{label}: expected {ny} rows, got {len(rows)}")
    out: List[List[Optional[float]]] = []
    for r, row in enumerate(rows):
        if len(row) != nx:
            raise GridPayloadError(f"{label}: row {r} has {len(row)} cells, expected {nx}")
        out.append([_coerce_cell(v, f"{label}[{r},{c}]") for c, v in enumerate(row)])
    return out


class Grid:
    """Immutable-enough observation grid: definition + values + optional error.

    ``error_var`` is the variance of the observation error in value^2 and is
    used only by the error-weighted overlap policy.  ``tau`` is an effective
    correlation length (in the grid's planar unit) used only by the
    zone-of-influence fallback; it is never used by the conservation-preserving
    paths.
    """

    def __init__(
        self,
        source_id: str,
        definition: GridDef,
        values: Sequence[Sequence[Optional[float]]],
        *,
        error_var: Optional[Sequence[Sequence[Optional[float]]]] = None,
        tau: Optional[float] = None,
        version: int = 0,
    ) -> None:
        if not source_id or any(ch.isspace() for ch in source_id):
            raise GridPayloadError("source id must be a non-empty token without spaces")
        self.source_id = source_id
        self.definition = definition
        self.version = int(version)
        self.values: List[List[Optional[float]]] = _as_matrix(values, definition.ny, definition.nx, source_id)
        if error_var is None:
            self.error_var: Optional[List[List[Optional[float]]]] = None
        else:
            ev = _as_matrix(error_var, definition.ny, definition.nx, f"{source_id}.error_var")
            for r, row in enumerate(ev):
                for c, v in enumerate(row):
                    if v is not None and v < 0:
                        raise GridPayloadError(
                            f"{source_id}.error_var[{r},{c}]: variance must be >= 0"
                        )
                    if v is None and self.values[r][c] is not None:
                        raise GridPayloadError(
                            f"{source_id}.error_var[{r},{c}]: missing variance for an observed cell"
                        )
            self.error_var = ev
        if tau is not None and not (tau > 0):
            raise GridPayloadError(f"{source_id}.tau: must be > 0 if given")
        self.tau = tau

    # -- geometry -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        return self.definition.bounds

    def cell_bounds(self, row: int, col: int) -> Tuple[float, float, float, float]:
        return cell_bounds(self.definition, row, col)

    # -- values -------------------------------------------------------------
    def get(self, row: int, col: int) -> Optional[float]:
        return self.values[row][col]

    def valid_mask(self) -> List[List[bool]]:
        return [[v is not None for v in row] for row in self.values]

    def valid_indices(self) -> List[Tuple[int, int]]:
        return [
            (r, c) for r, row in enumerate(self.values) for c, v in enumerate(row) if v is not None
        ]

    def n_valid(self) -> int:
        return sum(1 for row in self.values for v in row if v is not None)

    def n_missing(self) -> int:
        return self.definition.n_cells - self.n_valid()

    def finite_sum(self) -> float:
        return sum(v for row in self.values for v in row if v is not None)

    # -- serialisation ------------------------------------------------------
    def values_for_json(self) -> List[List[Optional[float]]]:
        return [list(row) for row in self.values]

    def to_dict(self) -> dict:
        d = {
            "source": self.source_id,
            "version": self.version,
            "grid": self.definition.to_dict(),
            "values": self.values_for_json(),
            "n_valid": self.n_valid(),
            "n_missing": self.n_missing(),
        }
        if self.error_var is not None:
            d["error_var"] = [list(row) for row in self.error_var]
        if self.tau is not None:
            d["tau"] = self.tau
        return d


def from_rows(payload: dict) -> Grid:
    """Build a :class:`Grid` from a row-major provider payload.

    Expected keys: ``source``, ``grid`` (see :class:`GridDef`), ``values`` as a
    list of ``ny`` rows of ``nx`` cells, optional ``error_var``, ``tau``,
    ``version``.  Unrecognised keys are rejected so a typo cannot silently
    drop data.
    """
    if not isinstance(payload, dict):
        raise GridPayloadError("payload must be an object")
    allowed = {"source", "grid", "values", "rows", "error_var", "tau", "version", "comment"}
    unknown = set(payload) - allowed
    if unknown:
        raise GridPayloadError(f"unknown payload keys: {sorted(unknown)}")
    if "source" not in payload or "grid" not in payload:
        raise GridPayloadError("payload needs 'source' and 'grid'")
    definition = GridDef.from_dict(payload["grid"])
    if "values" in payload and "rows" in payload:
        raise GridPayloadError("provide either 'values' or 'rows', not both")
    if "rows" in payload:
        rows = _rows_to_matrix(payload["rows"], definition)
    elif "values" in payload:
        rows = payload["values"]
    else:
        raise GridPayloadError("payload needs 'values' or 'rows'")
    return Grid(
        source_id=str(payload["source"]),
        definition=definition,
        values=rows,
        error_var=payload.get("error_var"),
        tau=payload.get("tau"),
        version=int(payload.get("version", 0)),
    )


def _rows_to_matrix(rows: Any, definition: GridDef) -> List[List[Any]]:
    """Accept sparse row objects ``{row, cells}`` or a dense list of rows."""
    if not isinstance(rows, list):
        raise GridPayloadError("'rows' must be a list")
    if not rows:
        return []
    if isinstance(rows[0], dict):
        by_row: Dict[int, List[Any]] = {}
        for item in rows:
            if not isinstance(item, dict) or "row" not in item or "cells" not in item:
                raise GridPayloadError("each sparse row needs 'row' and 'cells'")
            r = int(item["row"])
            if not 0 <= r < definition.ny:
                raise GridPayloadError(f"row index {r} outside 0..{definition.ny - 1}")
            if r in by_row:
                raise GridPayloadError(f"row {r} appears more than once")
            if not isinstance(item["cells"], list):
                raise GridPayloadError(f"row {r}: 'cells' must be a list")
            by_row[r] = item["cells"]
        missing_rows = sorted(set(range(definition.ny)) - set(by_row))
        if missing_rows:
            raise GridPayloadError(f"payload omits rows {missing_rows}; send every row")
        return [by_row[r] for r in range(definition.ny)]
    return rows


def apply_patch(base: Grid, patch: Mapping[str, Any]) -> Grid:
    """Return a copy of ``base`` with the cells named in ``patch`` replaced.

    A patch changes only what it lists.  It cannot express "make this cell
    missing" - that needs a full grid submission - so a partial sheet can
    never erase data by omission.
    """
    allowed = {"source", "mode", "cells", "comment"}
    unknown = set(patch) - allowed
    if unknown:
        raise GridPayloadError(f"unknown patch keys: {sorted(unknown)}")
    if str(patch.get("source", base.source_id)) != base.source_id:
        raise GridPayloadError(
            f"patch is for {patch['source']!r} but applies to {base.source_id!r}"
        )
    cells = patch.get("cells")
    if not isinstance(cells, list) or not cells:
        raise GridPayloadError("patch needs a non-empty 'cells' list")
    values = [list(row) for row in base.values]
    err = [list(row) for row in base.error_var] if base.error_var is not None else None
    d = base.definition
    seen = set()
    for i, item in enumerate(cells):
        if isinstance(item, dict):
            r, c = int(item["row"]), int(item["col"])
            raw = item.get("value")
            raw_err = item.get("error_var")
        elif isinstance(item, (list, tuple)) and len(item) in (3, 4):
            r, c, raw = int(item[0]), int(item[1]), item[2]
            raw_err = item[3] if len(item) == 4 else None
        else:
            raise GridPayloadError(f"patch cell {i} must be [row,col,value] or an object")
        if not (0 <= r < d.ny and 0 <= c < d.nx):
            raise GridPayloadError(f"patch cell ({r},{c}) outside the grid")
        if (r, c) in seen:
            raise GridPayloadError(f"patch names cell ({r},{c}) twice")
        seen.add((r, c))
        v = _coerce_cell(raw, f"patch[{r},{c}]")
        if v is None:
            raise GridPayloadError(
                f"patch[{r},{c}]: use a full submission to mark a cell missing"
            )
        values[r][c] = v
        if err is not None:
            e = _coerce_cell(raw_err, f"patch[{r},{c}].error_var")
            if e is None:
                raise GridPayloadError(f"patch[{r},{c}]: error_var required for this grid")
            if e < 0:
                raise GridPayloadError(f"patch[{r},{c}]: variance must be >= 0")
            err[r][c] = e
    return Grid(
        source_id=base.source_id,
        definition=GridDef.from_dict({**d.to_dict(), "name": d.name}),
        values=values,
        error_var=err,
        tau=base.tau,
        version=base.version,
    )


def from_columns(payload: dict) -> Grid:
    """Build a :class:`Grid` from a column-major payload (``columns`` key)."""
    if not isinstance(payload, dict) or "columns" not in payload:
        raise GridPayloadError("payload needs 'columns'")
    p = dict(payload)
    definition = GridDef.from_dict(p.pop("grid"))
    columns = p.pop("columns")
    if len(columns) != definition.nx:
        raise GridPayloadError(f"expected {definition.nx} columns, got {len(columns)}")
    rows = [[columns[c][r] for c in range(definition.nx)] for r in range(definition.ny)]
    p["values"] = rows
    p["grid"] = definition.to_dict()
    return from_rows(p)


def cell_bounds(definition: GridDef, row: int, col: int) -> Tuple[float, float, float, float]:
    """Planar bounds ``(x0, y0, x1, y1)`` of a cell."""
    if not (0 <= row < definition.ny and 0 <= col < definition.nx):
        raise IndexError(f"cell ({row},{col}) outside {definition.name}")
    x0 = snap(definition.origin_x + col * definition.dx)
    y0 = snap(definition.origin_y + row * definition.dy)
    return (x0, y0, snap(x0 + definition.dx), snap(y0 + definition.dy))


def cells_touching(
    definition: GridDef, bounds: Tuple[float, float, float, float]
) -> Optional[Tuple[int, int, int, int]]:
    """Inclusive ``(row0, row1, col0, col1)`` candidate range.

    Returns ``None`` when the rectangle does not intersect the grid.  Touching
    at an outer edge only is *not* an intersection (half-open cells), matching
    area computations exactly.
    """
    x0, y0, x1, y1 = bounds
    if x1 <= definition.x0 or x0 >= definition.x1 or y1 <= definition.y0 or y0 >= definition.y1:
        return None
    eps = 1e-9
    c0 = max(0, int(math.floor((x0 - definition.origin_x) / definition.dx + eps)))
    c1 = min(definition.nx - 1, int(math.ceil((x1 - definition.origin_x) / definition.dx - eps)) - 1)
    r0 = max(0, int(math.floor((y0 - definition.origin_y) / definition.dy + eps)))
    r1 = min(definition.ny - 1, int(math.ceil((y1 - definition.origin_y) / definition.dy - eps)) - 1)
    if c0 > c1 or r0 > r1:
        return None
    return (r0, r1, c0, c1)


def intersection(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    """Area of the intersection of two rectangles."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 1e-12 or h <= 1e-12:
        return 0.0
    return snap(w * h, 1e-12)


def rect_union_area(rects: Sequence[Tuple[float, float, float, float]]) -> float:
    """Exact area of the union of a small set of axis-aligned rectangles.

    Coordinate compression keeps this independent of how many cells a source
    contributed: overlapping source cells are counted once, which is the
    property that stops the same ground area being counted twice.
    """
    cleaned = [r for r in rects if r[2] - r[0] > 1e-12 and r[3] - r[1] > 1e-12]
    if not cleaned:
        return 0.0
    xs = sorted({r[0] for r in cleaned} | {r[2] for r in cleaned})
    ys = sorted({r[1] for r in cleaned} | {r[3] for r in cleaned})
    total = 0.0
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        w = x1 - x0
        if w <= 1e-12:
            continue
        for j in range(len(ys) - 1):
            y0, y1 = ys[j], ys[j + 1]
            h = y1 - y0
            if h <= 1e-12:
                continue
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            if any(r[0] <= mx <= r[2] and r[1] <= my <= r[3] for r in cleaned):
                total += w * h
    return snap(total, 1e-12)


def clip_rect(
    r: Tuple[float, float, float, float],
    clip: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    return (
        max(r[0], clip[0]),
        max(r[1], clip[1]),
        min(r[2], clip[2]),
        min(r[3], clip[3]),
    )
