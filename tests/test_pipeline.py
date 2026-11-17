"""End-to-end tests for the grid-merge pipeline and the workbench API.

Run with ``python3 -m unittest discover -s tests -v``.  No third-party test
runner is required; the HTTP tests spin the real server on an ephemeral port.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridmerge import (  # noqa: E402
    Cancelled,
    EngineRequest,
    FusionEngine,
    GridDef,
    GridPayloadError,
    InputManifest,
    Method,
    OverlapPolicy,
    VersionStore,
    ZoneConflict,
    apply_patch,
    block_aggregate,
    cells_touching,
    conservative_count,
    conservative_mean,
    from_rows,
    nearest_neighbour,
    overlap_weights,
    plan_overlaps,
    rect_union_area,
    weighted_sum,
    zone_of_influence,
)
from gridmerge.engine import CancellationToken  # noqa: E402
from gridmerge.server import Handler, Workbench  # noqa: E402
from gridmerge.versioning import computations_are_current  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

DATA = ROOT / "data"


def payload(**kw):
    base = {
        "source": "S", "grid": {"name": "S", "nx": 2, "ny": 2, "dx": 1, "dy": 1},
        "values": [[1, 2], [3, None]],
    }
    base.update(kw)
    return base


def store_with(*payloads):
    st = VersionStore()
    st.commit_batch([from_rows(p) for p in payloads])
    return st


class GeometryTests(unittest.TestCase):
    def test_cell_bounds_and_touching(self):
        g = GridDef("G", nx=2, ny=2, dx=2.5, dy=2.5, origin_x=1.0, origin_y=-1.0)
        self.assertEqual(cells_touching(g, (0, -2, 2, 0)), (0, 0, 0, 0))
        self.assertEqual(cells_touching(g, (100, 100, 101, 101)), None)
        # touching only the outer edge is not an intersection
        self.assertEqual(cells_touching(g, (6.0, -1.0, 7.0, 1.0)), None)

    def test_union_area_does_not_double_count(self):
        a = (0, 0, 2, 2)
        b = (1, 1, 3, 3)
        self.assertAlmostEqual(rect_union_area([a, a, b]), 7.0)
        self.assertAlmostEqual(rect_union_area([(0, 0, 4, 4), (1, 1, 2, 2)]), 16.0)

    def test_overlap_weights_sum_to_target_area(self):
        target = GridDef("T", nx=2, ny=2, dx=2, dy=2, origin_x=0.2, origin_y=0.2)
        source = GridDef("S", nx=4, ny=4, dx=1.5, dy=1.5, origin_x=0, origin_y=0)
        for r in range(2):
            for c in range(2):
                total = sum(a for _, _, a in overlap_weights(target, source, r, c))
                self.assertAlmostEqual(total, 4.0)


class IngestionTests(unittest.TestCase):
    def test_missing_tokens_are_missing_not_zero(self):
        g = from_rows(payload(values=[["", "NaN"], ["null", "1.5"]]))
        self.assertIsNone(g.get(0, 0))
        self.assertIsNone(g.get(0, 1))
        self.assertIsNone(g.get(1, 0))
        self.assertEqual(g.get(1, 1), 1.5)

    def test_zero_is_kept(self):
        g = from_rows(payload(values=[[0, 0], [0, 0]]))
        self.assertEqual(g.n_valid(), 4)
        self.assertEqual(g.finite_sum(), 0.0)

    def test_bad_shapes_are_rejected(self):
        with self.assertRaises(GridPayloadError):
            from_rows(payload(values=[[1, 2]]))
        with self.assertRaises(GridPayloadError):
            from_rows(payload(values=[[1, 2], [3, "abc"]]))
        with self.assertRaises(GridPayloadError):
            from_rows({**payload(), "mystery": 1})

    def test_error_var_must_match_observations(self):
        with self.assertRaises(GridPayloadError):
            from_rows(payload(error_var=[[1, None], [1, 1]]))

    def test_patch_only_touches_named_cells(self):
        base = from_rows(payload(values=[[1, None], [3, 4]]))
        patched = apply_patch(base, {"source": "S", "mode": "patch",
                                     "cells": [[1, 1, 9, 0.25]]})
        self.assertEqual(patched.values, [[1, None], [3, 9]])
        self.assertIsNone(patched.values[0][1])
        with self.assertRaises(GridPayloadError):
            apply_patch(base, {"source": "S", "mode": "patch", "cells": [[0, 0, None]]})
        with self.assertRaises(GridPayloadError):
            apply_patch(base, {"source": "S", "mode": "patch",
                               "cells": [[0, 0, 1], [0, 0, 2]]})


class ResampleTests(unittest.TestCase):
    def test_aligned_conservative_is_exact(self):
        coarse = GridDef("C", nx=2, ny=1, dx=2, dy=2)
        fine = GridDef("F", nx=4, ny=2, dx=1, dy=1)
        g = from_rows({"source": "C", "grid": coarse.to_dict(),
                       "values": [[10.0, 20.0]]})
        vals, _ = conservative_mean(fine, g)
        self.assertEqual(vals, [10.0, 10.0, 20.0, 20.0] * 1 + [10.0, 10.0, 20.0, 20.0])

    def test_conservative_mean_ignores_missing_area(self):
        coarse = GridDef("C", nx=1, ny=1, dx=2, dy=2)
        fine = GridDef("F", nx=2, ny=1, dx=1, dy=1)
        # One of the two fine target cells sees a missing source cell? No:
        # a single coarse cell feeds both; instead use a mixed-coverage source.
        src_def = GridDef("S", nx=2, ny=1, dx=1, dy=1)
        g = from_rows({"source": "S", "grid": src_def.to_dict(),
                       "values": [[4.0, None]]})
        vals, _ = conservative_mean(coarse, g)
        # covered area is 2 of 4; the mean is over the covered part only.
        self.assertEqual(vals, [4.0])

    def test_count_resampling_preserves_total(self):
        src_def = GridDef("S", nx=2, ny=1, dx=1, dy=1)
        tgt = GridDef("T", nx=1, ny=1, dx=2, dy=1)
        g = from_rows({"source": "S", "grid": src_def.to_dict(),
                       "values": [[3.0, 5.0]]})
        vals, _ = conservative_count(tgt, g)
        self.assertAlmostEqual(vals[0], 8.0)

    def test_nearest_does_not_interpolate(self):
        src_def = GridDef("S", nx=2, ny=1, dx=1, dy=1)
        tgt = GridDef("T", nx=2, ny=2, dx=1, dy=1)
        g = from_rows({"source": "S", "grid": src_def.to_dict(),
                       "values": [[7.0, None]]})
        vals, _ = nearest_neighbour(tgt, g)
        self.assertEqual(vals[0], 7.0)
        self.assertIsNone(vals[1])
        self.assertIsNone(vals[2])  # outside the source footprint at all
        self.assertIsNone(vals[3])

    def test_zonal_requires_tau_for_gaussian(self):
        src_def = GridDef("S", nx=2, ny=2, dx=1, dy=1)
        g = from_rows({"source": "S", "grid": src_def.to_dict(),
                       "values": [[1, 1], [1, 1]]})
        with self.assertRaises(ValueError):
            zone_of_influence(src_def, g, 2.0, weight_fn="gaussian")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = VersionStore()
        self.store.commit_batch([
            from_rows(json.loads((DATA / "alpha_v1.json").read_text())),
            from_rows(json.loads((DATA / "beta_v1.json").read_text())),
        ])
        self.engine = FusionEngine(self.store)

    def test_overlap_support_counts_ground_once(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        comp = self.engine.run(EngineRequest(
            target=t, overlap_policy=OverlapPolicy.PRIORITY,
            priority=("ALPHA", "BETA")))
        # 1456 = ALPHA's valid area; BETA adds only ground ALPHA does not cover.
        self.assertAlmostEqual(comp.summary["aggregate"]["support_area"], 1456.0)
        # The naive double-count would be larger.
        self.assertLess(comp.summary["aggregate"]["support_area"], 1456.0 + 531.0)

    def test_area_policy_weights_by_measurement_area(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        prio = self.engine.run(EngineRequest(
            target=t, overlap_policy=OverlapPolicy.PRIORITY, priority=("ALPHA", "BETA")))
        area = self.engine.run(EngineRequest(
            target=t, overlap_policy=OverlapPolicy.AREA))
        self.assertNotAlmostEqual(
            prio.summary["aggregate"]["sum"], area.summary["aggregate"]["sum"])
        self.assertGreater(area.summary["aggregate"]["support_area"],
                           prio.summary["aggregate"]["support_area"])

    def test_counts_from_overlapping_sources_are_refused(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        with self.assertRaises(Exception) as ctx:
            self.engine.run(EngineRequest(target=t, stat_scale="extensive",
                                          overlap_policy=OverlapPolicy.SUM))
        self.assertIn("twice", str(ctx.exception))

    def test_missing_is_not_zero(self):
        t = GridDef("T", nx=12, ny=9, dx=4, dy=4)
        only_alpha = VersionStore()
        only_alpha.commit_batch([
            from_rows(json.loads((DATA / "alpha_v1.json").read_text()))])
        comp = FusionEngine(only_alpha).run(EngineRequest(
            target=t, overlap_policy=OverlapPolicy.PRIORITY, priority=("ALPHA",)))
        gap = [c for c in comp.cells if c.value is None]
        self.assertTrue(gap)
        self.assertTrue(all(c.status.value in ("missing", "gap") for c in gap))
        # weighted_sum ignores them instead of adding zeros
        s = weighted_sum(comp.cells, t)
        self.assertAlmostEqual(s["support_area"], 1456.0)
        self.assertEqual(comp.summary["aggregate"]["support_area"], 1456.0)

    def test_zonal_cells_are_labelled_and_excluded(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        comp = self.engine.run(EngineRequest(
            target=t, fill="zone_of_influence", fill_max_distance=8.0,
            priority=("ALPHA", "BETA")))
        z = [c for c in comp.cells if c.origin.value == "zonal"]
        self.assertTrue(z)
        self.assertTrue(all(c.area_fraction == 0 for c in z))
        self.assertEqual(comp.summary["mean"]["zonal_cells_excluded"], len(z))
        with self.assertRaises(ZoneConflict):
            weighted_sum(comp.cells, t, include_zonal=True)

    def test_nearest_policy_is_restricted(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        with self.assertRaises(Exception):
            self.engine.run(EngineRequest(target=t, method=Method.NEAREST,
                                          overlap_policy=OverlapPolicy.AREA))

    def test_cancel_token_stops_the_run(self):
        t = GridDef("T", nx=24, ny=18, dx=2, dy=2)
        token = CancellationToken()
        threading.Timer(0.05, token.cancel).start()
        with self.assertRaises(Cancelled):
            self.engine.run(EngineRequest(target=t, pace_s=0.01,
                                          priority=("ALPHA", "BETA")), token=token)

    def test_revision_manifest_changes(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        req = EngineRequest(target=t, overlap_policy=OverlapPolicy.AREA)
        before = self.engine.run(req)
        self.store.replace_source(apply_patch(
            self.store.get("BETA"),
            json.loads((DATA / "beta_patch_v2.json").read_text())))
        after = self.engine.run(req)
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(after.manifest.entries[1][1], 2)
        self.assertFalse(computations_are_current(before.manifest, self.store))
        self.assertTrue(computations_are_current(after.manifest, self.store))

    def test_manifest_fingerprint_order_independent(self):
        e = (("A", 1, "aa"), ("B", 1, "bb"))
        t = ("T", 0, 1.0, 1.0, 1, 1, 0.0, 0.0)
        m1 = InputManifest(entries=e, target=t)
        m2 = InputManifest(entries=tuple(reversed(e)), target=t)
        self.assertEqual(m1.fingerprint, m2.fingerprint)

    def test_overlap_ledger_covers_four_cases(self):
        t = GridDef("T", nx=8, ny=6, dx=6, dy=6)
        recs = plan_overlaps(t, list(self.store.snapshot().values()))
        cases = {r.case for r in recs}
        self.assertEqual(cases, {"both", "a_only", "b_only", "neither"})
        both = [r for r in recs if r.case == "both"]
        self.assertTrue(all(r.value_a is not None and r.value_b is not None for r in both))

    def test_block_aggregate_reports_coverage_not_zero_fill(self):
        t = GridDef("T", nx=12, ny=9, dx=4, dy=4)
        comp = self.engine.run(EngineRequest(
            target=t, priority=("ALPHA", "BETA")))
        agg = block_aggregate(comp.cells, t, t.bounds)
        self.assertLess(agg["coverage_fraction"], 1.0)
        self.assertEqual(agg["mean"], comp.summary["mean"]["mean"])

    def test_empty_intersection_warns(self):
        t = GridDef("T", nx=2, ny=2, dx=1, dy=1, origin_x=500, origin_y=500)
        comp = self.engine.run(EngineRequest(target=t, priority=("ALPHA", "BETA")))
        self.assertTrue(any("does not intersect" in w for w in comp.warnings))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = Workbench(data_dir=DATA)
        Handler.workbench = cls.wb
        Handler.static_dir = ROOT / "web"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.url(path), data=data,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_state(self):
        with urllib.request.urlopen(self.url("/")) as r:
            html = r.read().decode()
        self.assertIn("Grid fusion workbench", html)
        self.assertNotIn("<script src=", html)  # self-contained
        st, state = self.req("/api/state")
        self.assertEqual(st, 200)
        self.assertEqual([s["source"] for s in state["sources"]], ["ALPHA", "BETA"])

    def test_validate_reports_semantic_conflict(self):
        st, out = self.req("/api/validate", {
            "target": {"nx": 4, "ny": 4, "dx": 8, "dy": 8},
            "stat_scale": "extensive", "overlap_policy": "priority",
            "priority": ["ALPHA"]})
        self.assertEqual(st, 200)
        self.assertFalse(out["ok"])

    def test_compute_cancel_and_report(self):
        st, out = self.req("/api/compute", {
            "target": {"nx": 40, "ny": 30, "dx": 1.2, "dy": 1.2},
            "overlap_policy": "priority", "priority": ["ALPHA", "BETA"],
            "pace_s": 0.02})
        self.assertEqual(st, 202)
        rid = out["run_id"]
        time.sleep(0.1)
        st, out = self.req(f"/api/compute/{rid}/cancel", {})
        self.assertEqual(out["status"], "cancelled")
        time.sleep(0.5)
        st, run = self.req(f"/api/runs/{rid}")
        self.assertEqual(run["status"], "cancelled")
        self.assertNotIn("computation_id", run)

    def test_revision_supersedes_running_run(self):
        st, out = self.req("/api/compute", {
            "target": {"nx": 30, "ny": 24, "dx": 1.5, "dy": 1.5},
            "overlap_policy": "area", "pace_s": 0.02})
        rid = out["run_id"]
        time.sleep(0.1)
        st, rev = self.req("/api/ingest", {
            "source": "BETA", "mode": "patch", "cells": [[2, 4, 15.0, 0.0009]]})
        self.assertEqual(st, 200)
        for _ in range(40):
            time.sleep(0.05)
            st, run = self.req(f"/api/runs/{rid}")
            if run["status"] != "running":
                break
        self.assertEqual(run["status"], "cancelled")
        self.assertIn("revision", run["error"])

    def test_report_names_versions_and_goes_stale(self):
        st, out = self.req("/api/compute", {
            "target": {"nx": 8, "ny": 6, "dx": 6, "dy": 6},
            "overlap_policy": "area"})
        rid = out["run_id"]
        for _ in range(40):
            time.sleep(0.05)
            st, run = self.req(f"/api/runs/{rid}")
            if run["status"] == "done":
                break
        cid = run["computation_id"]
        with urllib.request.urlopen(self.url(f"/api/report/{cid}")) as r:
            html = r.read().decode()
        self.assertIn(run["fingerprint"][:10], html)
        self.assertNotIn("Stale.", html)
        self.req("/api/ingest", {"source": "BETA", "mode": "patch",
                                 "cells": [[2, 4, 15.0, 0.0009]]})
        with urllib.request.urlopen(self.url(f"/api/report/{cid}")) as r:
            html = r.read().decode()
        self.assertIn("Stale.", html)

    def test_patch_cannot_mark_missing(self):
        st, out = self.req("/api/ingest", {"source": "BETA", "mode": "patch",
                                           "cells": [[0, 0, None]]})
        self.assertEqual(st, 400)
        self.assertIn("full submission", out["error"])

    def test_benchmark_endpoint(self):
        st, out = self.req("/api/benchmark")
        self.assertEqual(st, 200)
        self.assertTrue(all(out["checks"].values()))
        self.assertAlmostEqual(out["coarse_integral"], out["fine_integral"])

    def test_fixture_payload_is_served_verbatim(self):
        st, out = self.req("/api/fixtures/small_reference.json")
        self.assertEqual(st, 200)
        self.assertIn("hand_checks", out)
        st, _ = self.req("/api/fixtures/../gridmerge/engine.py")
        self.assertEqual(st, 404)

    def test_duplicate_row_rejected(self):
        st, out = self.req("/api/ingest", {
            "source": "DUP", "mode": "append",
            "grid": {"name": "D", "nx": 2, "ny": 2, "dx": 1, "dy": 1},
            "rows": [{"row": 0, "cells": [1, 2]}, {"row": 0, "cells": [3, 4]},
                     {"row": 1, "cells": [5, 6]}]})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
