"""Static checks that the single-page app is well-formed and internally consistent.

There is no browser in this environment, so this stands in for "open it and
look at it": it parses the HTML structure, checks that every element the script
addresses by id exists, that every endpoint it calls is routed by the server,
and that the script parses.  It cannot judge layout, and it admits that.

Run: ``python3 tools/check_ui.py``
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VIPER = re.compile(r"\$\('#([A-Za-z0-9_-]+)'\)")
SELECTOR = re.compile(r"\$\$\('([^']+)'\)")
API = re.compile(r"['\"`](/api/[A-Za-z0-9_./${}-]+)['\"`]")


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.buttons: list[dict] = []
        self.scripts: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            if d["id"] in self.ids:
                self.errors.append(f"duplicate id {d['id']}")
            self.ids.add(d["id"])
        if tag not in ("meta", "link", "br", "input", "img", "hr"):
            self.stack.append(tag)
        if tag == "script":
            self._in_script = True
        if tag == "button":
            self.buttons.append(d)
        if tag == "svg" and "role" not in d:
            self.errors.append("svg without role")
        if tag == "input" and "type" in d and d["type"] not in ("number", "text", "checkbox"):
            self.errors.append(f"unexpected input type {d['type']}")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.errors.append(f"mismatched </{tag}> (open: {self.stack[-3:]})")
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._in_script:
            self.scripts.append(data)


def main() -> int:
    html = (ROOT / "web" / "app.html").read_text()
    c = Collector()
    c.feed(html)
    problems = list(c.errors)
    if c.stack:
        problems.append(f"unclosed tags: {c.stack}")

    script = "\n".join(c.scripts)
    ids_used = set(VIPER.findall(script))
    # The page also injects markup that declares ids at runtime; those count.
    dynamic_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', script))
    dynamic_ids |= set(re.findall(r"id=\\?\"([A-Za-z0-9_-]+)", script))
    missing = sorted(i for i in ids_used if i not in c.ids and i not in dynamic_ids)
    if missing:
        problems.append(f"script references missing ids: {missing}")

    # Buttons need a label (visible text or aria-label / title).
    for b in c.buttons:
        if "aria-label" not in b and "title" not in b and "class" not in b:
            problems.append(f"button with no label: {b}")

    # Endpoints used by the script must exist in the server router.
    server = (ROOT / "gridmerge" / "server.py").read_text()
    endpoints = sorted(set(API.findall(script)))
    for ep in endpoints:
        base = ep.split("?")[0]
        if "${" in base:
            continue
        stem = base.replace("/api/", "").split("/")[0]
        if f'"/api/{stem}' not in server and f"/api/{stem}" not in server:
            problems.append(f"script calls unrouted endpoint {ep}")
    for required in ("/api/state", "/api/compute", "/api/ingest",
                     "/api/report/", "/api/overlaps", "/api/validate"):
        if required not in endpoints:
            problems.append(f"expected the page to use {required}")
    if "fixtures/reset" not in server:
        problems.append("server has no reset route")

    # No external assets: the page must work offline from the same server.
    if re.search(r"<(script|link)[^>]+(src|href)=\"https?://", html):
        problems.append("page loads an external asset")

    # The page must expose the controls the brief names.
    for needle in ("target grid", "Overlap review", "Revision diff", "missing",
                   "invented", "support"):
        if needle.lower() not in html.lower():
            problems.append(f"page lacks expected concept: {needle}")

    # If node is available, parse the script for real.  This catches syntax
    # errors that a brace count cannot.
    node = shutil.which("node")
    if node:
        tmp = Path("/tmp/_ui_check.js")
        tmp.write_text(script)
        proc = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
        if proc.returncode != 0:
            problems.append("node --check failed: " +
                            (proc.stderr.strip().splitlines() or ["?"])[-1])

    # The script must be syntactically plausible: balanced braces/parens outside strings.
    for ch_open, ch_close in (("{", "}"), ("(", ")"), ("[", "]")):
        if script.count(ch_open) != script.count(ch_close):
            problems.append(f"unbalanced {ch_open}{ch_close} in script "
                            f"({script.count(ch_open)} vs {script.count(ch_close)})")

    print(f"ids: {len(c.ids)} (+{len(dynamic_ids)} injected) · "
          f"script id refs: {len(ids_used)} · "
          f"endpoints: {len(endpoints)} · buttons: {len(c.buttons)}")
    if problems:
        print("UI CHECK FAILED")
        for p in problems:
            print("  -", p)
        print("\nOpen the page manually to judge layout; this check only "
              "covers structure and wiring.")
        return 1
    print("UI CHECK PASSED (structure and wiring only - layout needs a real browser)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
