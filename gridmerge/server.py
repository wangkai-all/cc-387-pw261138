"""Dependency-free HTTP server for the grid-fusion workbench.

Routes
------
``GET  /``                     single-page workbench
``GET  /api/state``            sources, versions, flags, last run summary
``GET  /api/sources/<id>``     full grid payload
``POST /api/ingest``           append or replace one source grid
``POST /api/revisions``        atomically commit a batch of revised grids
``POST /api/validate``         dry-run: does this request make sense?
``POST /api/compute``          run the engine (cancellable by id)
``POST /api/compute/<id>/cancel`` cancel a running computation
``GET  /api/overlaps``         pair ledger for the current snapshot
``GET  /api/report/<id>``      rendered HTML report; ``.json`` for the data
``GET  /api/history``          committed batches

Every mutating route is one JSON object in / one JSON object out.  No global
mutable "current request": a computation is addressed by its id, so a stale
report can never be presented as if it belonged to a newer input version.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .engine import (
    CancellationToken,
    Cancelled,
    EngineRequest,
    FusionEngine,
    ValidationError,
    plan_overlaps,
    summarise_overlaps,
)
from .export import artifact_wire
from .grid import Grid, GridPayloadError, apply_patch, from_rows
from .model import GridDef, Method, OverlapPolicy, Status
from .report import render_report
from .versioning import VersionStore, computations_are_current

MAX_BODY = 4 * 1024 * 1024


def _grid_payload_from_request(body: dict) -> Grid:
    if "values" in body and "grid" in body:
        return from_rows(body)
    if "grid" in body and "columns" in body:
        from .grid import from_columns
        return from_columns(body)
    if "grid" in body and "rows" in body:
        return from_rows(body)
    raise GridPayloadError("expected {source, grid, values|rows|columns}")


def request_from_body(body: dict, store_versions: Dict[str, int]) -> EngineRequest:
    tgt = body.get("target") or {}
    if "grid" in tgt:
        target = GridDef.from_dict(tgt["grid"])
    else:
        required = ("nx", "ny", "dx", "dy")
        missing = [k for k in required if k not in tgt]
        if missing:
            raise ValidationError(f"target is missing {missing}")
        target = GridDef(
            name=str(tgt.get("name", "TARGET")),
            nx=int(tgt["nx"]), ny=int(tgt["ny"]),
            dx=float(tgt["dx"]), dy=float(tgt["dy"]),
            origin_x=float(tgt.get("origin_x", 0.0)),
            origin_y=float(tgt.get("origin_y", 0.0)),
            unit=str(tgt.get("unit", "m")),
        )
    try:
        method = Method(str(body.get("method", "conservative")))
        policy = OverlapPolicy(str(body.get("overlap_policy", "priority")))
    except ValueError as exc:
        raise ValidationError(str(exc))
    priority = tuple(str(s) for s in body.get("priority", ()))
    blocks = []
    for b in body.get("blocks", ()):
        blocks.append((str(b["name"]), tuple(float(v) for v in b["bounds"])))
    return EngineRequest(
        target=target,
        method=method,
        overlap_policy=policy,
        priority=priority,
        stat_scale=str(body.get("stat_scale", "intensive")),
        fill=str(body.get("fill", "none")),
        fill_max_distance=float(body.get("fill_max_distance", 0.0)),
        fill_weight=str(body.get("fill_weight", "inverse_square")),
        include_zonal_in_mean=bool(body.get("include_zonal_in_mean", False)),
        target_version=int(body.get("target_version", 0)),
        review_overlaps=bool(body.get("review_overlaps", True)),
        blocks=tuple(blocks),
        pace_s=float(body.get("pace_s", 0.0)),
    )


class Workbench:
    """Server-side state: the store, the engine, and the run registry."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.store = VersionStore()
        self.engine = FusionEngine(self.store)
        self.runs: Dict[str, dict] = {}
        self._lock = threading.RLock()
        self.data_dir = data_dir
        if data_dir is not None:
            self._load_directory(data_dir)

    # -- fixtures -----------------------------------------------------------
    def _load_directory(self, data_dir: Path) -> None:
        alpha = sorted(data_dir.glob("alpha_v*.json"))
        beta = sorted(data_dir.glob("beta_v1*.json"))
        batch = []
        for path in alpha + beta:
            payload = json.loads(path.read_text())
            batch.append(from_rows(payload))
        if batch:
            self.store.commit_batch(batch)

    # -- ingestion ----------------------------------------------------------
    def ingest(self, body: dict) -> dict:
        mode = body.get("mode", "replace")
        if mode not in ("replace", "append", "patch"):
            raise GridPayloadError("mode must be 'replace', 'append' or 'patch'")
        if mode == "patch":
            grid, patched = self._apply_patch_body(body)
        else:
            grid = _grid_payload_from_request(body)
            patched = 0
            if mode == "append" and grid.source_id in self.store.sources():
                raise GridPayloadError(
                    f"{grid.source_id} already exists; use mode='replace' or a new id"
                )
        version = self.store.replace_source(grid)
        return {
            "source": grid.source_id,
            "version": version,
            "mode": mode,
            "patched_cells": patched,
            "n_valid": grid.n_valid(),
            "n_missing": grid.n_missing(),
            "warnings": self._ingest_warnings(grid),
        }

    def _apply_patch_body(self, body: dict) -> tuple[Grid, int]:
        sid = str(body.get("source", ""))
        if sid not in self.store.sources():
            raise GridPayloadError(
                f"patch targets unknown source {sid!r}; a patch cannot create a source"
            )
        base = self.store.get(sid)
        cells = body.get("cells")
        if not isinstance(cells, list):
            raise GridPayloadError("patch needs a 'cells' list")
        new = apply_patch(base, {**body, "source": sid})
        return new, len(cells)

    def _ingest_warnings(self, grid: Grid) -> List[str]:
        out: List[str] = []
        if grid.n_valid() == 0:
            out.append(f"{grid.source_id}: every cell is missing; it contributes nothing")
        bounds = grid.bounds
        for sid in self.store.sources():
            if sid == grid.source_id:
                continue
            other = self.store.get(sid).bounds
            if bounds[2] <= other[0] or bounds[0] >= other[2] or \
                    bounds[3] <= other[1] or bounds[1] >= other[3]:
                out.append(f"{grid.source_id}: footprint does not touch {sid}")
        return out

    def revision_batch(self, body: dict) -> dict:
        grids_raw = body.get("grids")
        patches_raw = body.get("patches") or []
        if not isinstance(grids_raw, list) or not grids_raw:
            grids_raw = []
        if not grids_raw and not patches_raw:
            raise GridPayloadError("give a non-empty 'grids' or 'patches' list")
        label = str(body.get("label", "manual revision"))
        grids = [_grid_payload_from_request(g) for g in grids_raw]
        for p in patches_raw:
            sid = str(p.get("source", ""))
            if sid not in self.store.sources():
                raise GridPayloadError(f"patch targets unknown source {sid!r}")
            grids.append(apply_patch(self.store.get(sid), {**p, "source": sid}))
        before = {s: self.store.version_of(s) for s in self.store.sources()}
        versions = self.store.commit_batch(grids)
        for s, v in versions.items():
            self.store.set_note(s, v, label)
        return {
            "label": label,
            "versions": versions,
            "previous_versions": before,
            "generation": self.store.generation(),
            "superseded_runs": [
                rid for rid, r in self.runs.items()
                if r.get("status") == "running"
            ],
        }

    # -- compute ------------------------------------------------------------
    def validate(self, body: dict) -> dict:
        """Dry run.  A semantically wrong request is a 200 with ok=false -
        the caller asked a question and got an answer, not a server failure."""
        snapshot = self.store.snapshot()
        versions = {s: g.version for s, g in snapshot.items()}
        req = request_from_body(body, versions)
        try:
            req.validate(sorted(snapshot))
        except ValidationError as exc:
            return {"ok": False, "issues": exc.issues, "sources": [
                {"source": s, "version": g.version, "n_valid": g.n_valid()}
                for s, g in snapshot.items()
            ]}
        hits = []
        for s, g in snapshot.items():
            if g.bounds[2] <= req.target.x0 or g.bounds[0] >= req.target.x1 or \
                    g.bounds[3] <= req.target.y0 or g.bounds[1] >= req.target.y1:
                hits.append(f"{s} does not intersect the target grid")
        if req.method is Method.CONSERVATIVE and req.overlap_policy is OverlapPolicy.SUM \
                and req.stat_scale != "extensive":
            hits.append("sum policy is only valid for count fields")
        return {
            "ok": not hits,
            "issues": hits,
            "target_cells": req.target.n_cells,
            "target_cell_area": req.target.cell_area,
            "sources": [
                {"source": s, "version": g.version, "n_valid": g.n_valid()}
                for s, g in snapshot.items()
            ],
        }

    def overlaps(self, body: dict) -> dict:
        snapshot = self.store.snapshot()
        tgt = body.get("target") or {}
        main_sources = list(snapshot.values())
        recs = plan_overlaps(self._target_from(tgt), main_sources)
        return {
            "summary": summarise_overlaps(recs),
            "records": [r.to_dict() for r in recs],
            "generation": self.store.generation(),
        }

    @staticmethod
    def _target_from(tgt: dict) -> GridDef:
        if "grid" in tgt:
            return GridDef.from_dict(tgt["grid"])
        return GridDef(
            name=str(tgt.get("name", "TARGET")), nx=int(tgt["nx"]), ny=int(tgt["ny"]),
            dx=float(tgt["dx"]), dy=float(tgt["dy"]),
            origin_x=float(tgt.get("origin_x", 0.0)),
            origin_y=float(tgt.get("origin_y", 0.0)),
        )

    def start_compute(self, body: dict) -> dict:
        snapshot = self.store.snapshot()
        versions = {s: g.version for s, g in snapshot.items()}
        req = request_from_body(body, versions)
        req.validate(sorted(snapshot))
        run_id = "r_" + uuid.uuid4().hex[:10]
        token = CancellationToken()
        record = {
            "id": run_id,
            "status": "running",
            "started": time.time(),
            "generation": self.store.generation(),
            "inputs": {s: g.version for s, g in snapshot.items()},
            "result": None,
            "error": None,
            "token": token,
        }
        with self._lock:
            self.runs[run_id] = record

        def work() -> None:
            try:
                def on_progress(p):
                    record["progress"] = p
                    if p["fraction"] >= 1.0:
                        record["progress"] = None
                comp = self.engine.run(req, token=token, progress=on_progress)
                if not computations_are_current(comp.manifest, self.store,
                                                cancel_reason=self.store.cancel_reason()):
                    comp.superseded = True
                    record.update(status="superseded",
                                  error="inputs changed during the run; result withheld")
                else:
                    record.update(status="done", result=comp)
            except Cancelled as exc:
                record.update(status="cancelled", error=str(exc))
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                record.update(status="error", error=f"{type(exc).__name__}: {exc}")
                record["traceback"] = traceback.format_exc()

        threading.Thread(target=work, daemon=True).start()
        return {"run_id": run_id, "generation": record["generation"]}

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            rec = self.runs.get(run_id)
            if rec is None:
                raise KeyError(run_id)
            token = rec.get("token")
            if token is not None:
                token.cancel()
            rec["status"] = "cancelled" if rec["status"] == "running" else rec["status"]
            rec["error"] = rec.get("error") or "cancelled by user"
        return {"run_id": run_id, "status": rec["status"]}

    def run_state(self, run_id: str) -> dict:
        rec = self.runs.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        out = {k: v for k, v in rec.items() if k not in ("result", "token")}
        out["id"] = run_id
        out["progress"] = rec.get("progress")
        comp = rec.get("result")
        if comp is not None:
            out["summary"] = comp.summary
            out["fingerprint"] = comp.fingerprint
            out["computation_id"] = comp.computation_id
        return out

    # -- reports ------------------------------------------------------------
    def report(self, comp_id: str, fmt: str) -> tuple[str, str]:
        comp = None
        for rec in self.runs.values():
            c = rec.get("result")
            if c is not None and c.computation_id == comp_id:
                comp = c
                break
        if comp is None:
            raise KeyError(comp_id)
        if fmt == "json":
            return json.dumps(artifact_wire(comp)), "application/json"
        return render_report(comp, self.store), "text/html"

    def state(self) -> dict:
        snap = self.store.snapshot()
        return {
            "generation": self.store.generation(),
            "sources": [
                {
                    "source": s,
                    "version": g.version,
                    "grid": g.definition.to_dict(),
                    "n_valid": g.n_valid(),
                    "n_missing": g.n_missing(),
                    "min": min((v for row in g.values for v in row if v is not None), default=None),
                    "max": max((v for row in g.values for v in row if v is not None), default=None),
                    "has_error_var": g.error_var is not None,
                    "has_tau": g.tau is not None,
                }
                for s, g in snap.items()
            ],
            "runs": [
                {k: v for k, v in rec.items() if k not in ("result", "token")}
                for rec in sorted(self.runs.values(), key=lambda r: r["started"], reverse=True)[:20]
            ],
            "history": self.store.history(),
            "notes": self.store.notes(),
            "fixtures": sorted(p.name for p in self.data_dir.glob("*.json"))
                        if self.data_dir else [],
        }

    def reset(self) -> dict:
        """Reload the seed fixtures into a fresh store (a new demo day)."""
        with self._lock:
            if any(r.get("status") == "running" for r in self.runs.values()):
                raise GridPayloadError("cannot reset while a computation is running")
            self.store = VersionStore()
            self.engine = FusionEngine(self.store)
            self.runs.clear()
            if self.data_dir is not None:
                self._load_directory(self.data_dir)
        return self.state()

    def add_fixture(self, name: str) -> dict:
        if self.data_dir is None:
            raise GridPayloadError("no fixture directory configured")
        path = self.data_dir / name
        payload = json.loads(path.read_text())
        return self.ingest({**payload, "mode": "replace"})


class Handler(BaseHTTPRequestHandler):
    workbench: Workbench
    static_dir: Path

    def log_message(self, fmt: str, *args) -> None:  # quieter tests
        pass

    # -- helpers ------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise GridPayloadError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GridPayloadError(f"invalid JSON: {exc}")
        if not isinstance(obj, dict):
            raise GridPayloadError("top-level JSON must be an object")
        return obj

    def _guard(self, fn):
        try:
            fn()
        except (GridPayloadError, ValidationError) as exc:
            payload = exc.to_dict() if hasattr(exc, "to_dict") else {
                "error": str(exc), "issues": list(getattr(exc, "issues", []) or []),
            }
            self._json(HTTPStatus.BAD_REQUEST, payload)
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": f"not found: {exc}"})
        except Cancelled as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            })

    # -- routing ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        def go() -> None:
            if path in ("/", "/index.html"):
                page = (self.static_dir / "app.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._json(HTTPStatus.OK, self.workbench.state())
                return
            if path == "/api/history":
                self._json(HTTPStatus.OK, {"history": self.workbench.store.history()})
                return
            if path.startswith("/api/sources/"):
                sid = path.rsplit("/", 1)[-1]
                g = self.workbench.store.get(sid)
                self._json(HTTPStatus.OK, g.to_dict())
                return
            if path.startswith("/api/runs/"):
                rid = path.split("/")[3]
                self._json(HTTPStatus.OK, self.workbench.run_state(rid))
                return
            if path.startswith("/api/fixtures/") and path.count("/") == 3:
                name = path.rsplit("/", 1)[-1]
                if self.workbench.data_dir is None:
                    raise KeyError(name)
                payload = json.loads((self.workbench.data_dir / name).read_text())
                self._json(HTTPStatus.OK, payload)
                return
            if path == "/api/benchmark":
                from .benchmark import small_grid_benchmark
                self._json(HTTPStatus.OK, small_grid_benchmark())
                return
            if path.startswith("/api/overlaps"):
                q = parse_qs(parsed.query)
                body = {"target": {
                    "nx": int(q["nx"][0]), "ny": int(q["ny"][0]),
                    "dx": float(q["dx"][0]), "dy": float(q["dy"][0]),
                    "origin_x": float(q.get("origin_x", ["0"])[0]),
                    "origin_y": float(q.get("origin_y", ["0"])[0]),
                    "name": "QUERY",
                }}
                self._json(HTTPStatus.OK, self.workbench.overlaps(body))
                return
            if path.startswith("/api/report/"):
                ident = path.rsplit("/", 1)[-1]
                fmt = "json" if ident.endswith(".json") else "html"
                if ident.endswith(".json"):
                    ident = ident[:-5]
                body, ctype = self.workbench.report(ident, fmt)
                if ctype == "application/json":
                    self._send(HTTPStatus.OK, body.encode(), ctype)
                else:
                    self._send(HTTPStatus.OK, body.encode(), ctype + "; charset=utf-8")
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": f"no route {path}"})

        self._guard(go)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        def go() -> None:
            if path == "/api/ingest":
                self._json(HTTPStatus.OK, self.workbench.ingest(self._read_json()))
                return
            if path == "/api/revisions":
                self._json(HTTPStatus.OK, self.workbench.revision_batch(self._read_json()))
                return
            if path == "/api/validate":
                self._json(HTTPStatus.OK, self.workbench.validate(self._read_json()))
                return
            if path == "/api/overlaps":
                self._json(HTTPStatus.OK, self.workbench.overlaps(self._read_json()))
                return
            if path == "/api/compute":
                self._json(HTTPStatus.ACCEPTED, self.workbench.start_compute(self._read_json()))
                return
            if path.startswith("/api/compute/") and path.endswith("/cancel"):
                rid = path.split("/")[3]
                self._json(HTTPStatus.OK, self.workbench.cancel(rid))
                return
            if path == "/api/fixtures/reset":
                self._json(HTTPStatus.OK, self.workbench.reset())
                return
            if path.startswith("/api/fixtures/"):
                name = path.rsplit("/", 1)[-1]
                self._json(HTTPStatus.OK, self.workbench.add_fixture(name))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": f"no route {path}"})

        self._guard(go)


def serve(host: str = "127.0.0.1", port: int = 8765, data_dir: Optional[Path] = None,
          static_dir: Optional[Path] = None) -> None:
    wb = Workbench(data_dir=data_dir)
    Handler.workbench = wb
    Handler.static_dir = static_dir or (Path(__file__).resolve().parent.parent / "web")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"grid-fusion workbench on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
