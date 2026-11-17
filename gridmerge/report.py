"""Render a self-contained HTML report for one :class:`Computation`.

The renderer is deliberately dumb: it never recomputes statistics.  Every
number it prints was produced by the engine from the manifest it names, so a
report cannot silently mix versions.  If the store has moved on, the caller
adds a staleness banner instead of re-running anything.
"""

from __future__ import annotations

import html
import json
from typing import Optional

from .engine import Computation, request_to_dict
from .versioning import computations_are_current

STATUS_LABELS = {
    "valid": "single source",
    "overlap": "overlap (merged)",
    "missing": "missing inside a footprint",
    "gap": "no source footprint",
    "zonal": "invented by fallback",
}


def _e(v: object) -> str:
    return html.escape("" if v is None else str(v))


def _num(v: Optional[float], digits: int = 3) -> str:
    if v is None:
        return "not defined"
    return f"{v:.{digits}f}"


def render_report(
    comp: Computation,
    store=None,
    *,
    include_cells: bool = True,
) -> str:
    req = comp.request
    t = req.target
    m = comp.manifest
    stale = None
    if store is not None:
        ok = computations_are_current(m, store) and not comp.superseded
        if not ok:
            present = set(store.sources())
            current = ", ".join(
                f"{s} v{store.version_of(s)}" if s in present else f"{s} (removed)"
                for s, _, _ in m.entries
            )
            stale = (f"Stale: this report was computed from {m_versions(m)}; "
                     f"the store now holds {current}.")
    summary = comp.summary
    cov = summary["coverage"]
    agg = summary["aggregate"]
    mean = summary["mean"]

    status_rows = "".join(
        f"<tr><td>{_e(label)}</td><td class='n'>"
        f"{comp.summary.get(key, 0)}</td></tr>"
        for label, key in [
            ("single source", "n_valid_single"),
            ("overlap cells", "n_overlap_cells"),
            ("missing inside footprint", "n_missing_cells"),
            ("no source footprint", "n_gap_cells"),
            ("invented (zone fallback)", "n_zonal_cells"),
        ]
    )

    source_rows = "".join(
        f"<tr><td>{_e(s['source'])}</td><td>{_e(s['version'])}</td>"
        f"<td>{s['grid']['nx']}&nbsp;x&nbsp;{s['grid']['ny']}</td>"
        f"<td>{_num(s['grid']['dx'], 3)} / {_num(s['grid']['dy'], 3)}</td>"
        f"<td>({_num(s['grid']['origin_x'], 2)}, {_num(s['grid']['origin_y'], 2)})</td>"
        f"<td>{s['n_valid']} / {s['n_cells']}</td>"
        f"<td>{_num(s['value_mean'], 3)}</td>"
        f"<td><code>{_e(digest_for(m, s['source'])[:16])}</code></td></tr>"
        for s in comp.source_summaries
    )

    block_rows = "".join(
        f"<tr><td>{_e(b['name'])}</td><td class='n'>{_num(b['mean'], 3)}</td>"
        f"<td class='n'>{_num(b['integral'], 3)}</td>"
        f"<td class='n'>{_num(b['support_area'], 3)}</td>"
        f"<td class='n'>{_num(b['block_area'], 3)}</td>"
        f"<td class='n'>{b['coverage_fraction'] * 100:.1f}%</td>"
        f"<td class='n'>{b['n_cells_touched']}</td></tr>"
        for b in comp.blocks
    )

    ov = comp.overlap_summary
    case_rows = "".join(
        f"<tr><td>{_e(_case_label(k))}</td><td class='n'>{v['pairs']}</td>"
        f"<td class='n'>{_num(v['area'], 1)}</td></tr>"
        for k, v in ov["by_case"].items()
    )

    top = sorted(
        (r for r in comp.overlaps if r.case == "both" and r.value_a is not None and r.value_b is not None),
        key=lambda r: abs(r.value_b - r.value_a),
        reverse=True,
    )[:15]
    pair_rows = "".join(
        f"<tr><td>{_e(r.source_a)}[{r.cell_a}]</td><td class='n'>{_num(r.value_a, 4)}</td>"
        f"<td>{_e(r.source_b)}[{r.cell_b}]</td><td class='n'>{_num(r.value_b, 4)}</td>"
        f"<td class='n'>{_num(r.value_b - r.value_a, 4)}</td>"
        f"<td class='n'>{_num(r.shared_area, 2)}</td>"
        f"<td class='n'>{len(r.target_cells)}</td></tr>"
        for r in top
    )

    warns = "".join(f"<li>{_e(w)}</li>" for w in comp.warnings) or "<li>none</li>"

    cell_head = (
        "<tr><th>row</th><th>col</th><th>x</th><th>y</th><th>value</th>"
        "<th>status</th><th>origin</th><th>support share</th>"
        "<th>observed share</th><th>contributors</th></tr>"
    )
    cell_rows = ""
    if include_cells:
        cell_rows = "".join(
            f"<tr><td class='n'>{c.row}</td><td class='n'>{c.col}</td>"
            f"<td class='n'>{c.x:.1f}</td><td class='n'>{c.y:.1f}</td>"
            f"<td class='n'>{_num(c.value, 3)}</td>"
            f"<td>{_e(STATUS_LABELS.get(c.status.value, c.status.value))}</td>"
            f"<td>{_e(c.origin.value)}</td>"
            f"<td class='n'>{c.area_fraction:.3f}</td>"
            f"<td class='n'>{c.observed_fraction:.3f}</td>"
            f"<td>{_e(', '.join(c.contributors))}</td></tr>"
            for c in comp.cells
        )

    conservation = summary["conservation"]
    keeps = "".join(f"<li>{_e(x)}</li>" for x in conservation["preserves"]) or "<li>nothing</li>"
    loses = "".join(f"<li>{_e(x)}</li>" for x in conservation["does_not_preserve"]) or "<li>nothing</li>"

    reproduce = json.dumps(
        {
            "request": request_to_dict(req),
            "manifest": m.to_dict(),
        },
        indent=2,
        sort_keys=True,
    )

    title = f"Region report {comp.computation_id} - {m.fingerprint[:10]}"
    banner = (
        f"<div class='stale'><strong>Stale.</strong> "
        f"{_e(stale[len('Stale:'):].strip().capitalize())} Re-run with the current inputs "
        "before quoting these numbers.</div>"
        if stale else
        "<div class='ok'><strong>Inputs pinned.</strong> This report names the exact source "
        "versions and content hashes it was computed from.</div>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 1080px; margin: 32px auto; padding: 0 20px;
         color: #0b0b0b; background: #f9f9f7; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #f0efec; background: #0d0d0d; }} }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 15px; margin: 28px 0 8px; border-bottom: 1px solid #c3c2b7; padding-bottom: 4px; }}
  .sub {{ color: #52514e; margin-bottom: 16px; }}
  @media (prefers-color-scheme: dark) {{ .sub {{ color: #c3c2b7; }} }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
  .card {{ border: 1px solid rgba(11,11,11,.12); border-radius: 8px; padding: 12px 14px; background: #fcfcfb; }}
  @media (prefers-color-scheme: dark) {{ .card {{ background: #1a1a19; border-color: rgba(255,255,255,.12); }} }}
  .k {{ font-size: 12px; color: #52514e; }}
  @media (prefers-color-scheme: dark) {{ .k {{ color: #c3c2b7; }} }}
  .v {{ font-size: 22px; font-variant-numeric: tabular-nums; }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ padding: 4px 8px; border-bottom: 1px solid rgba(11,11,11,.10); text-align: left; }}
  @media (prefers-color-scheme: dark) {{ th, td {{ border-color: rgba(255,255,255,.10); }} }}
  th {{ font-size: 12px; color: #52514e; font-weight: 600; }}
  .n {{ text-align: right; }}
  code {{ font-size: 12px; }}
  .ok {{ background: #e7f4e7; color: #0b3d0b; padding: 8px 12px; border-radius: 6px; }}
  .stale {{ background: #fdecec; color: #7a1f1f; padding: 8px 12px; border-radius: 6px; }}
  ul {{ margin: 4px 0 12px 20px; }}
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
  @media (max-width: 720px) {{ .two {{ grid-template-columns: 1fr; }} }}
  pre {{ background: #efeee9; padding: 10px; border-radius: 6px; overflow-x: auto; font-size: 12px; }}
  @media (prefers-color-scheme: dark) {{ pre {{ background: #232320; }} }}
</style></head>
<body>
<h1>Region statistics report - pointwise area accounting</h1>
<div class="sub">Computation <code>{_e(comp.computation_id)}</code> &middot;
fingerprint <code>{_e(m.fingerprint)}</code> &middot;
{_e(comp.started_at)} &rarr; {_e(comp.finished_at)} ({comp.duration_s:.2f} s)</div>
{banner}

<div class="grid">
  <div class="card"><div class="k">Support-weighted mean</div>
    <div class="v">{_num(mean['mean'] if mean else None, 3)}</div>
    <div class="k">intensive field, over observed support</div></div>
  <div class="card"><div class="k">Area integral (aggregate)</div>
    <div class="v">{_num(agg['sum'], 1)}</div>
    <div class="k">value &times; area, over observed support</div></div>
  <div class="card"><div class="k">Observed coverage</div>
    <div class="v">{cov['observed_fraction'] * 100:.1f}%</div>
    <div class="k">{_num(cov['observed_area'], 1)} of {_num(cov['domain_area'], 1)} m&sup2;</div></div>
  <div class="card"><div class="k">Missing / uncovered</div>
    <div class="v">{summary['n_missing_cells']} / {summary['n_gap_cells']}</div>
    <div class="k">cells missing inside a footprint / uncovered cells</div></div>
</div>

<h2>What this run keeps and what it does not</h2>
<div class="two">
  <div><div class="k">Preserved</div><ul>{keeps}</ul></div>
  <div><div class="k">Explicitly not preserved</div><ul>{loses}</ul></div>
</div>
<p>{_e(conservation['note'])}</p>

<h2>Inputs pinned by this report</h2>
<table><thead><tr><th>source</th><th>version</th><th>grid</th>
<th>cell size</th><th>origin x</th><th>valid / cells</th><th>mean of valid</th>
<th>sha256</th></tr></thead><tbody>{source_rows}</tbody></table>
<p class="k">Target: {_e(t.name)} {t.nx}&times;{t.ny} cells of {_e(t.dx)}&times;{_e(t.dy)} {_e(t.unit)}
at origin ({_e(t.origin_x)}, {_e(t.origin_y)}); method
<strong>{_e(req.method.value)}</strong>, overlap policy
<strong>{_e(req.overlap_policy.value)}</strong>
{('(priority: ' + _e(' > '.join(req.priority)) + ')') if req.priority else ''},
field semantics <strong>{_e(req.stat_scale)}</strong>.</p>

<h2>Per-cell accounting</h2>
<table><thead><tr><th>status</th><th class="n">cells</th></tr></thead>
<tbody>{status_rows}</tbody></table>

<h2>Regional blocks</h2>
<table><thead><tr><th>block</th><th class="n">support mean</th><th class="n">integral</th>
<th class="n">support area (m&sup2;)</th><th class="n">block area (m&sup2;)</th>
<th class="n">coverage</th><th class="n">cells touched</th></tr></thead>
<tbody>{block_rows}</tbody></table>

<h2>Overlap review</h2>
<table><thead><tr><th>pair case</th><th class="n">cell pairs</th><th class="n">shared area (m&sup2;)</th></tr></thead>
<tbody>{case_rows}</tbody></table>
<p>Pairs with both sides valid: <strong>{ov['both_valid_pairs']}</strong>;
mean difference (B&minus;A) <strong>{_num(ov['mean_difference_b_minus_a'], 4)}</strong>;
mean absolute difference <strong>{_num(ov['mean_abs_difference'], 4)}</strong>;
max absolute difference <strong>{_num(ov['max_abs_difference'], 4)}</strong>.
Shared footprint area is counted once in every total, because support is a
union over valid cells, not a sum over contributors.</p>
<table><thead><tr><th>source A cell</th><th class="n">A value</th><th>source B cell</th>
<th class="n">B value</th><th class="n">B&minus;A</th><th class="n">shared m&sup2;</th>
<th class="n">target cells</th></tr></thead><tbody>{pair_rows}</tbody></table>

<h2>Warnings and decisions</h2>
<ul>{warns}</ul>

<h2>Cell table</h2>
<table><thead>{cell_head}</thead><tbody>{cell_rows}</tbody></table>

<h2>Reproduce</h2>
<pre>{_e(reproduce)}</pre>
</body></html>
"""


def _case_label(k: str) -> str:
    return {
        "both": "both sides valid",
        "a_only": "only A valid",
        "b_only": "only B valid",
        "neither": "both missing",
    }.get(k, k)


def m_versions(m) -> str:
    return ", ".join(f"{s} v{v}" for s, v, _ in m.entries)


def digest_for(m, source: str) -> str:
    for s, v, d in m.entries:
        if s == source:
            return d
    return "unknown"
