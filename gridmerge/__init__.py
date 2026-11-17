"""gridmerge - planar-grid fusion, resampling and pointwise area statistics.

Two providers publish rectangular grids in the same *fictitious planar*
coordinate system (no map projection is involved).  Their footprints overlap
partially and both contain missing cells.  The :class:`FusionEngine` builds one
deterministic *cell artefact*:

* every target cell carries exactly one record with an explicit status,
* cells covered by two sources get an explicit overlap decision and an
  overlap ledger that a reviewer can inspect,
* cells nobody covers stay missing (never zero),
* support area is a union, so overlapping sources never count one piece of
  ground twice, and
* a revision can be applied atomically so that a report always names the exact
  version of its inputs.
"""

from .model import (
    GridDef,
    CellRecord,
    OverlapRecord,
    Status,
    OverlapPolicy,
    Method,
    Origin,
    CellIndex,
    snap,
)
from .grid import (
    Grid,
    GridPayloadError,
    from_rows,
    from_columns,
    apply_patch,
    cell_bounds,
    cells_touching,
    intersection,
    rect_union_area,
    clip_rect,
)
from .resample import (
    nearest_neighbour,
    conservative_mean,
    conservative_sum,
    conservative_count,
    overlap_weights,
    zone_of_influence,
    RESERVED_KEYS,
)
from .stats import (
    weighted_mean,
    weighted_sum,
    coverage_fraction,
    block_aggregate,
    ZoneConflict,
)
from .versioning import (
    VersionStore,
    InputManifest,
    RunEpoch,
    RevisionApplied,
    computations_are_current,
    source_versions_of,
    payload_digest,
)
from .export import artifact_wire, compare_wire
from .engine import (
    FusionEngine,
    Computation,
    CancellationToken,
    EngineRequest,
    Cancelled,
    ValidationError,
    plan_overlaps,
    summarise_overlaps,
    request_to_dict,
)

__all__ = [
    "GridDef", "CellRecord", "OverlapRecord", "Status", "OverlapPolicy", "Method",
    "Origin", "CellIndex", "snap", "Grid", "GridPayloadError", "from_rows",
    "from_columns", "apply_patch", "cell_bounds", "cells_touching", "intersection",
    "rect_union_area", "clip_rect", "nearest_neighbour", "conservative_mean",
    "conservative_sum", "conservative_count", "overlap_weights",
    "zone_of_influence", "RESERVED_KEYS", "weighted_mean", "weighted_sum",
    "coverage_fraction", "block_aggregate", "ZoneConflict", "VersionStore",
    "InputManifest", "RunEpoch", "RevisionApplied", "computations_are_current",
    "source_versions_of", "payload_digest", "FusionEngine", "Computation",
    "CancellationToken", "EngineRequest", "Cancelled", "ValidationError",
    "plan_overlaps", "summarise_overlaps", "request_to_dict",
    "artifact_wire", "compare_wire",
]
