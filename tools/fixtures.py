"""Fixtures and a non-network harness for the workbench UI."

Everything here is deterministic and lives in memory; the UI tests read the
same JSON the browser gets, so a broken client payload shows up as a test
failure rather than only as a blank pane.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gridmerge import GridDef, VersionStore, apply_patch, from_rows  # noqa: E402

DATA = ROOT / "data"


def load_store() -> VersionStore:
    store = VersionStore()
    store.commit_batch([
        from_rows(json.loads((DATA / "alpha_v1.json").read_text())),
        from_rows(json.loads((DATA / "beta_v1.json").read_text())),
    ])
    return store


def revised_store() -> VersionStore:
    """ALPHA v1 + BETA patched to v2 with the lake cells filled."""
    store = load_store()
    patch = json.loads((DATA / "beta_patch_v2.json").read_text())
    base = store.get("BETA")
    store.replace_source(apply_patch(base, patch))
    return store


def target(**kw) -> GridDef:
    base = dict(name="TARGET", nx=8, ny=6, dx=6.0, dy=6.0, origin_x=0.0, origin_y=0.0)
    base.update(kw)
    return GridDef(**base)


def main() -> None:
    # consumers: report generator / cli need a stable description of the demo
    print(json.dumps({
        "data_dir": str(DATA),
        "fixtures": sorted(p.name for p in DATA.glob("*.json")),
        "target": target().to_dict(),
    }, indent=2))


if __name__ == "__main__":
    main()
