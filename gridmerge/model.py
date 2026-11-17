"""Core records for planar grids and the fused cell artefact."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class Status(str, Enum):
    """Why a cell has the value it has.

    ``MISSING`` is a first-class state: a missing source cell is *never*
    coerced to zero, and a target cell with no usable source keeps
    ``value=None``.
    """

    VALID = "valid"            # exactly one source provides a value
    OVERLAP = "overlap"        # two sources cover it; resolved per policy
    MISSING = "missing"        # source footprint covers it but no usable value
    GAP = "gap"                # no source footprint covers the cell at all
    ZONAL = "zonal"            # invented by the restricted zone-of-influence fill


class OverlapPolicy(str, Enum):
    """How to pick a value when two valid source cells cover one target cell."""

    PRIORITY = "priority"              # higher source priority wins, whole cell
    AREA = "area"                      # area-weighted mean of the covered part
    ERROR_WEIGHTED = "error_weighted"  # area * 1/error_var weights
    SUM = "sum"                        # add extensive counts (disjoint-population claim)


class Method(str, Enum):
    NEAREST = "nearest"
    CONSERVATIVE = "conservative"


class Origin(str, Enum):
    SOURCE = "source"        # a single source cell, resampled
    OVERLAP = "overlap"      # merged from >= 2 sources
    ZONAL = "zonal"          # invented by the restricted zone-of-influence fallback
    NONE = "none"            # no usable observation


class CellIndex(int):
    """Row-major index of a cell in its own grid (``row * nx + col``)."""


@dataclass(frozen=True)
class GridDef:
    """An axis-aligned rectangular grid in a planar CRS with EPSG-style units.

    ``origin_*`` is the lower-left corner of cell (0,0); ``dx``/``dy`` are cell
    sizes.  Cell (row, col) covers
    ``[origin_x + col*dx, origin_x + (col+1)*dx] x [origin_y + row*dy, ...]``.
    """

    name: str
    nx: int
    ny: int
    dx: float
    dy: float
    origin_x: float = 0.0
    origin_y: float = 0.0
    unit: str = "m"

    def __post_init__(self) -> None:
        if self.nx <= 0 or self.ny <= 0:
            raise ValueError(f"{self.name}: grid dimensions must be positive")
        if not (self.dx > 0 and self.dy > 0):
            raise ValueError(f"{self.name}: cell sizes must be positive")

    @property
    def x0(self) -> float:
        return self.origin_x

    @property
    def x1(self) -> float:
        return self.origin_x + self.nx * self.dx

    @property
    def y0(self) -> float:
        return self.origin_y

    @property
    def y1(self) -> float:
        return self.origin_y + self.ny * self.dy

    @property
    def cell_area(self) -> float:
        return self.dx * self.dy

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def n_cells(self) -> int:
        return self.nx * self.ny

    def same_geometry(self, other: "GridDef") -> bool:
        return (
            self.nx, self.ny, self.dx, self.dy, self.origin_x, self.origin_y
        ) == (
            other.nx, other.ny, other.dx, other.dy, other.origin_x, other.origin_y
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name, "nx": self.nx, "ny": self.ny,
            "dx": self.dx, "dy": self.dy, "origin_x": self.origin_x,
            "origin_y": self.origin_y, "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GridDef":
        return cls(
            name=str(d["name"]), nx=int(d["nx"]), ny=int(d["ny"]),
            dx=float(d["dx"]), dy=float(d["dy"]),
            origin_x=float(d.get("origin_x", 0.0)),
            origin_y=float(d.get("origin_y", 0.0)),
            unit=str(d.get("unit", "m")),
        )


@dataclass
class CellRecord:
    """One cell of the target grid in the fused artefact."""

    index: int
    row: int
    col: int
    x: float                     # cell centre
    y: float
    value: Optional[float]       # None means missing; never 0 as a stand-in
    status: Status
    origin: Origin
    area_fraction: float         # share of the cell supporting *this* value
    observed_fraction: float     # share of the cell covered by any source data
    n_contributors: int
    contributors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index, "row": self.row, "col": self.col,
            "x": self.x, "y": self.y, "value": self.value,
            "status": self.status.value, "origin": self.origin.value,
            "area_fraction": round(self.area_fraction, 6),
            "observed_fraction": round(self.observed_fraction, 6),
            "n_contributors": self.n_contributors,
            "contributors": list(self.contributors),
        }


@dataclass
class OverlapRecord:
    """A pair of source cells that cover the same part of a target cell."""

    source_a: str
    cell_a: int
    source_b: str
    cell_b: int
    shared_area: float
    case: str = "both"                 # both | a_only | b_only | neither
    value_a: Optional[float] = None
    value_b: Optional[float] = None
    target_cells: List[int] = field(default_factory=list)

    @property
    def pair(self) -> Tuple[str, str]:
        return tuple(sorted((self.source_a, self.source_b)))  # type: ignore[return-value]

    def to_dict(self) -> dict:
        return {
            "source_a": self.source_a, "cell_a": self.cell_a,
            "source_b": self.source_b, "cell_b": self.cell_b,
            "shared_area": round(self.shared_area, 6),
            "case": self.case,
            "value_a": self.value_a, "value_b": self.value_b,
            "target_cells": list(self.target_cells),
        }


def snap(value: float, quantum: float = 1e-9) -> float:
    """Round away floating-point dust so 1/3-style cell maths is stable."""
    if quantum <= 0:
        return value
    r = round(value / quantum) * quantum
    return 0.0 if abs(r) < quantum / 2 else r
