"""Input snapshots, run epochs and atomic revision application.

A report is only meaningful if it can name *exactly* the inputs it saw.  The
:class:`VersionStore` owns that naming:

* every accepted grid payload gets a monotonically increasing ``version`` and a
  content SHA-256;
* a computation pins ``tuple(sorted((source, version)))`` at start and is
  refused if a revision lands before it finishes (cancel-on-supersede);
* a revision is applied as a batch - either every grid in the batch is stored,
  or none is, so no reader can observe a half-updated source.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .grid import Grid


@dataclass(frozen=True)
class InputManifest:
    """Exact input versions behind one artefact or report."""

    entries: Tuple[Tuple[str, int, str], ...]  # (source, version, sha256)
    target: Tuple[str, int, float, float, int, int, float, float]

    @property
    def fingerprint(self) -> str:
        # Only the *set* of pinned inputs and the target definition identify
        # an artefact.  Insertion order of the store is not an identity.
        h = hashlib.sha256()
        h.update(json.dumps(sorted(self.entries), sort_keys=True).encode())
        h.update(json.dumps(list(self.target), sort_keys=True).encode())
        return h.hexdigest()

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "sources": [
                {"source": s, "version": v, "sha256": h} for s, v, h in self.entries
            ],
            "target": {
                "name": self.target[0], "version": self.target[1],
                "dx": self.target[2], "dy": self.target[3],
                "nx": self.target[4], "ny": self.target[5],
                "origin_x": self.target[6], "origin_y": self.target[7],
            },
        }


def source_versions_of(manifest: InputManifest) -> Tuple[Tuple[str, int], ...]:
    return tuple(sorted((s, v) for s, v, _ in manifest.entries))


def payload_digest(payload: Mapping) -> str:
    """Stable digest of a provider payload (independent of key order)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class RunEpoch:
    """The generation counter a computation is allowed to finish in."""

    generation: int
    fingerprint: str


class RevisionApplied(RuntimeError):
    """A revision superseded a run; the run must not publish results."""


class VersionStore:
    """Thread-safe holder of source grids and their versions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._grids: Dict[str, Grid] = {}
        self._versions: Dict[str, int] = {}
        self._digests: Dict[str, Dict[int, str]] = {}
        self._generation = 0
        self._history: List[dict] = []
        self._cancelled: Optional[str] = None
        self._notes: List[dict] = []

    # -- writes -------------------------------------------------------------
    def commit_batch(
        self, grids: Iterable[Grid], *, mode: str = "replace"
    ) -> Dict[str, int]:
        """Atomically store every grid or none.  Returns source -> version.

        ``mode="patch"`` is exclusive to new source ids: a patch whose
        semantics are not yet pinned down must not silently create a source
        that *looks* complete but is mostly missing.
        """
        prepared: List[Tuple[str, Grid, str]] = []
        for g in grids:
            if mode == "patch" and g.source_id in self._versions:
                raise ValueError(
                    f"{g.source_id}: patch mode needs a new source id"
                )
            prepared.append((g.source_id, g, payload_digest(g.to_dict())))
        if not prepared:
            raise ValueError("empty revision batch")
        with self._lock:
            seen: set = set()
            assigned: Dict[str, int] = {}
            for sid, g, digest in prepared:
                if sid in seen:
                    raise ValueError(f"batch contains {sid!r} twice")
                seen.add(sid)
                nxt = self._versions.get(sid, 0) + 1
                assigned[sid] = nxt
            # Nothing mutated before this point; now apply as one step.
            for sid, g, digest in prepared:
                g.version = assigned[sid]
                self._grids[sid] = g
                self._versions[sid] = assigned[sid]
                self._digests.setdefault(sid, {})[assigned[sid]] = digest
            self._generation += 1
            self._cancelled = None
            self._history.append({
                "generation": self._generation,
                "sources": {s: v for s, v in assigned.items()},
            })
            return assigned

    def replace_source(self, grid: Grid) -> int:
        return self.commit_batch([grid])[grid.source_id]

    # -- reads --------------------------------------------------------------
    def get(self, source_id: str) -> Grid:
        with self._lock:
            if source_id not in self._grids:
                raise KeyError(source_id)
            return self._grids[source_id]

    def version_of(self, source_id: str) -> int:
        with self._lock:
            return self._versions[source_id]

    def sources(self) -> List[str]:
        with self._lock:
            return sorted(self._grids)

    def snapshot(self) -> Dict[str, Grid]:
        with self._lock:
            return {s: self._grids[s] for s in sorted(self._grids)}

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def history(self) -> List[dict]:
        with self._lock:
            return [dict(h) for h in self._history]

    def digest_of(self, source_id: str, version: int) -> str:
        with self._lock:
            return self._digests[source_id][version]

    def set_note(self, source_id: str, version: int, label: str) -> None:
        with self._lock:
            self._notes.append({
                "source": source_id, "version": version, "label": label,
                "generation": self._generation,
            })

    def notes(self) -> List[dict]:
        with self._lock:
            return [dict(n) for n in self._notes]

    # -- cancellation -------------------------------------------------------
    def mark_cancelled(self, reason: str = "user") -> None:
        with self._lock:
            self._cancelled = reason

    def cancel_reason(self) -> Optional[str]:
        with self._lock:
            return self._cancelled

    def clear_cancel(self) -> None:
        with self._lock:
            self._cancelled = None


def computations_are_current(
    manifest: InputManifest,
    store: VersionStore,
    *,
    cancel_reason: Optional[str] = None,
) -> bool:
    """True iff every input is still at the version the manifest names.

    A cancelled run is never current, even if no revision landed.
    """
    if cancel_reason:
        return False
    for sid, ver, digest in manifest.entries:
        try:
            if store.version_of(sid) != ver:
                return False
        except KeyError:
            return False
        if store.digest_of(sid, ver) != digest:
            return False
    return True
