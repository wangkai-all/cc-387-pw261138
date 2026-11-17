"""Raster preview of the artefact map and histogram, without a browser.

This environment has no headless browser, so this script re-draws the same
geometry, palette and layering rules the page uses and writes a PNG.  It is a
*visual* check on the map and chart, not a substitute for opening the page; the
PNG is generated from the server's wire payload so the data path is the real one.

    python3 tools/render_preview.py --out reports/preview.png
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIGHT = {
    "page": (0xF9, 0xF9, 0xF7),
    "surface": (0xFC, 0xFC, 0xFB),
    "ink": (0x0B, 0x0B, 0x0B),
    "ink2": (0x52, 0x51, 0x4E),
    "muted": (0x89, 0x87, 0x81),
    "axis": (0xC3, 0xC2, 0xB7),
    "grid": (0xE1, 0xE0, 0xD9),
    "s1": (0x2A, 0x78, 0xD6),
    "s2": (0xEB, 0x68, 0x34),
    "s3": (0x1B, 0xAF, 0x7A),
    "s4": (0xED, 0xA1, 0x00),
    "seq": [(0xCD, 0xE2, 0xFB), (0x9E, 0xC5, 0xF4), (0x6D, 0xA7, 0xEC),
            (0x39, 0x87, 0xE5), (0x25, 0x6A, 0xBF), (0x18, 0x4F, 0x95)],
    "code": (0xEF, 0xEE, 0xE9),
    "crit": (0xD0, 0x3B, 0x3B),
}


class Canvas:
    def __init__(self, w: int, h: int, bg):
        self.w, self.h = w, h
        self.buf = bytearray(bytes(bg) * (w * h))

    def px(self, x: int, y: int, c) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.buf[i:i + 3] = bytes(c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c) -> None:
        for y in range(max(0, y0), min(self.h, y1)):
            i = (y * self.w + max(0, x0)) * 3
            n = max(0, min(self.w, x1) - max(0, x0))
            self.buf[i:i + n * 3] = bytes(c) * n

    def frame(self, x0: int, y0: int, x1: int, y1: int, c) -> None:
        self.rect(x0, y0, x1, y0 + 1, c)
        self.rect(x0, y1 - 1, x1, y1, c)
        self.rect(x0, y0, x0 + 1, y1, c)
        self.rect(x1 - 1, y0, x1, y1, c)

    def dashed(self, x0: int, y0: int, x1: int, y1: int, c, on=4, off=3) -> None:
        if y0 == y1:
            x = x0
            while x < x1:
                self.rect(x, y0, min(x1, x + on), y0 + 1, c)
                x += on + off
        else:
            y = y0
            while y < y1:
                self.rect(x0, y, x0 + 1, min(y1, y + on), c)
                y += on + off

    def xmark(self, cx: int, cy: int, r: int, c) -> None:
        for d in range(-r, r + 1):
            self.px(cx + d, cy + d, c)
            self.px(cx + d, cy - d, c)

    def png(self, path: Path) -> None:
        raw = b"".join(b"\x00" + bytes(self.buf[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        hdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", hdr) +
                         chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


FONT = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    ".": ["000", "000", "000", "000", "010"], "-": ["000", "000", "111", "000", "000"],
    " ": ["000", "000", "000", "000", "000"], "/": ["001", "001", "010", "100", "100"],
    "A": ["010", "101", "111", "101", "101"], "B": ["110", "101", "110", "101", "110"],
    "C": ["011", "100", "100", "100", "011"], "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"], "F": ["111", "100", "110", "100", "100"],
    "G": ["011", "100", "101", "101", "011"], "H": ["101", "101", "111", "101", "101"],
    "I": ["111", "010", "010", "010", "111"], "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101"], "N": ["101", "111", "111", "111", "101"],
    "O": ["010", "101", "101", "101", "010"], "P": ["110", "101", "110", "100", "100"],
    "R": ["110", "101", "110", "101", "101"], "S": ["011", "100", "010", "001", "110"],
    "T": ["111", "010", "010", "010", "010"], "U": ["101", "101", "101", "101", "111"],
    "V": ["101", "101", "101", "101", "010"], "W": ["101", "101", "111", "111", "101"],
    "Y": ["101", "101", "010", "010", "010"], "X": ["101", "101", "010", "101", "101"],
    "Z": ["111", "001", "010", "100", "111"], "a": ["000", "011", "101", "101", "011"],
    "b": ["100", "110", "101", "101", "110"], "c": ["000", "011", "100", "100", "011"],
    "d": ["001", "011", "101", "101", "011"], "e": ["000", "011", "111", "100", "011"],
    "f": ["011", "010", "111", "010", "010"], "g": ["000", "011", "101", "011", "110"],
    "h": ["100", "110", "101", "101", "101"], "i": ["010", "000", "010", "010", "010"],
    "l": ["010", "010", "010", "010", "010"], "m": ["000", "111", "111", "101", "101"],
    "n": ["000", "110", "101", "101", "101"], "o": ["000", "010", "101", "101", "010"],
    "p": ["000", "110", "101", "110", "100"], "r": ["000", "101", "110", "100", "100"],
    "s": ["000", "011", "110", "011", "110"], "t": ["010", "111", "010", "010", "011"],
    "u": ["000", "101", "101", "101", "011"], "v": ["000", "101", "101", "101", "010"],
    "w": ["000", "101", "101", "111", "111"], "x": ["000", "101", "010", "101", "101"],
    "y": ["000", "101", "101", "011", "110"], "z": ["000", "111", "010", "100", "111"],
    "(": ["010", "100", "100", "100", "010"], ")": ["010", "001", "001", "001", "010"],
    "%": ["101", "001", "010", "100", "101"], ":": ["000", "010", "000", "010", "000"],
    "=": ["000", "111", "000", "111", "000"], "+": ["000", "010", "111", "010", "000"],
    "·": ["000", "000", "010", "000", "000"], "×": ["000", "101", "010", "101", "000"],
}


def text(cv: Canvas, x: int, y: int, s: str, c, scale: int = 2) -> None:
    for ch in s:
        glyph = FONT.get(ch, FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    cv.rect(x + gx * scale, y + gy * scale,
                            x + gx * scale + scale, y + gy * scale + scale, c)
        x += 4 * scale


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="reports/preview.png")
    ap.add_argument("--policy", default="area")
    args = ap.parse_args()

    run = None
    for attempt in ("priority", "area"):
        body = json.dumps({
            "target": {"nx": 12, "ny": 9, "dx": 4, "dy": 4},
            "overlap_policy": args.policy, "priority": ["ALPHA", "BETA"],
        }).encode()
        req = urllib.request.Request(args.base + "/api/compute", data=body,
                                     headers={"Content-Type": "application/json"})
        rid = json.loads(urllib.request.urlopen(req).read())["run_id"]
        for _ in range(60):
            st = fetch(f"{args.base}/api/runs/{rid}")
            if st["status"] != "running":
                break
        if st["status"] == "done":
            run = fetch(f"{args.base}/api/report/{st['computation_id']}.json")
            break
    if run is None:
        print("no computation available")
        return 1

    t = run["target"]
    cell = 26
    pad = 18
    map_w = t["nx"] * cell
    map_h = t["ny"] * cell
    gap = 46
    W = pad * 2 + map_w * 2 + gap + 190
    H = pad * 2 + map_h + 200
    cv = Canvas(W, H, LIGHT["page"])
    cv.rect(pad, pad, W - pad, H - pad, LIGHT["surface"])
    cv.frame(pad, pad, W - pad, H - pad, LIGHT["grid"])

    lo, hi = run["scale"]["min"], run["scale"]["max"]
    values, status = run["values"], run["status"]
    owner = run["owner"]
    src_cols = [LIGHT["s1"], LIGHT["s2"], LIGHT["s3"]]

    def draw_panel(ox: int, title: str, layer: str) -> None:
        text(cv, ox, pad + 8, title, LIGHT["ink"], 2)
        top = pad + 26
        for r in range(t["ny"]):
            for c in range(t["nx"]):
                i = r * t["nx"] + c
                x = ox + c * cell
                y = top + (t["ny"] - 1 - r) * cell
                v, st = values[i], status[i]
                if layer == "value":
                    if v is None:
                        fill = LIGHT["code"]
                    else:
                        f = max(0.0, min(0.9999, (v - lo) / (hi - lo)))
                        fill = LIGHT["seq"][int(f * len(LIGHT["seq"]))]
                else:
                    if st == 1 and owner[i] >= 0:
                        fill = src_cols[owner[i]]
                    elif st == 2:
                        fill = LIGHT["s3"]
                    elif st == 3:
                        fill = LIGHT["s4"]
                    else:
                        fill = LIGHT["code"]
                cv.rect(x, y, x + cell - 1, y + cell - 1, fill)
                if v is None:
                    cv.xmark(x + cell // 2, y + cell // 2, 4, LIGHT["muted"])
                cv.frame(x, y, x + cell, y + cell, LIGHT["surface"])
        # footprints
        for k, s in enumerate(run["sources"]):
            g = s["grid"]
            x = ox + int((g["origin_x"] - t["origin_x"]) / t["dx"] * cell)
            yb = top + int((t["ny"] * t["dy"] - (g["origin_y"] - t["origin_y"] + g["ny"] * g["dy"])) / t["dy"] * cell)
            w = int(g["nx"] * g["dx"] / t["dx"] * cell)
            h = int(g["ny"] * g["dy"] / t["dy"] * cell)
            col = src_cols[k % 3]
            cv.dashed(x, yb, x + w, yb, col)
            cv.dashed(x, yb + h - 1, x + w, yb + h - 1, col)
            cv.dashed(x, yb, x, yb + h, col)
            cv.dashed(x + w - 1, yb, x + w - 1, yb + h, col)
            text(cv, x + 2, yb - 10, g["name"], col, 1)

    draw_panel(pad + 6, "VALUE", "value")
    draw_panel(pad + 6 + map_w + gap, "OWNERSHIP", "owner")

    # colour ramp under the value panel
    ry = pad + 26 + map_h + 14
    text(cv, pad + 6, ry + 4, "LOW", LIGHT["muted"], 1)
    for k, col in enumerate(LIGHT["seq"]):
        cv.rect(pad + 34 + k * 18, ry, pad + 34 + (k + 1) * 18 - 2, ry + 12, col)
    text(cv, pad + 34 + len(LIGHT["seq"]) * 18 + 4, ry + 4, "HIGH", LIGHT["muted"], 1)
    text(cv, pad + 6 + map_w + gap + 2, ry + 4,
         f"{lo:.1f} - {hi:.1f}", LIGHT["muted"], 1)

    # right column: stats + legend
    rx = pad + 6 + map_w * 2 + gap + 26
    text(cv, rx, pad + 8, "ARTEFACT", LIGHT["ink2"], 2)
    s = run["summary"]
    lines = [
        ("mean", f"{s['mean']['mean']:.3f}" if s["mean"] else "-"),
        ("integral", f"{s['aggregate']['sum']:.0f}"),
        ("coverage", f"{s['coverage']['observed_fraction']*100:.1f}%"),
        ("missing", str(s["n_missing_cells"])),
        ("overlap", str(s["n_overlap_cells"])),
    ]
    y = pad + 34
    for k, v in lines:
        text(cv, rx, y, k, LIGHT["muted"], 1)
        text(cv, rx + 72, y, v, LIGHT["ink"], 2)
        y += 24
    y += 10
    text(cv, rx, y, "LEGEND", LIGHT["ink2"], 2)
    y += 20
    for col, lbl in [(LIGHT["s1"], "ALPHA"), (LIGHT["s2"], "BETA"),
                     (LIGHT["s3"], "merged"), (LIGHT["s4"], "invented"),
                     (LIGHT["axis"], "missing")]:
        cv.rect(rx, y, rx + 10, y + 10, col)
        text(cv, rx + 18, y, lbl, LIGHT["ink2"], 1)
        y += 18

    # histogram, below the ownership panel
    hy0 = ry + 26
    vals = [v for v in values if v is not None]
    bins = 12
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - lo) / (hi - lo) * bins))] += 1
    mx = max(counts)
    hx0 = pad + 6 + map_w + gap
    text(cv, hx0, hy0, "VALUE DISTRIBUTION", LIGHT["ink2"], 2)
    hy = hy0 + 24
    bw = 18
    for i, n in enumerate(counts):
        hgt = int(78 * n / mx)
        cv.rect(hx0 + i * bw + 2, hy + 78 - hgt, hx0 + i * bw + bw - 2, hy + 78, LIGHT["s1"])
    cv.rect(hx0, hy + 78, hx0 + bins * bw, hy + 79, LIGHT["axis"])
    text(cv, hx0, hy + 86, f"{lo:.1f}", LIGHT["muted"], 1)
    text(cv, hx0 + bins * bw - 20, hy + 86, f"{hi:.1f}", LIGHT["muted"], 1)
    text(cv, hx0 + bins * bw + 8, hy + 34, f"{len(vals)}", LIGHT["muted"], 2)
    text(cv, hx0 + bins * bw + 8, hy + 50, "cells", LIGHT["muted"], 1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv.png(out)
    print(f"preview -> {out} ({W}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
