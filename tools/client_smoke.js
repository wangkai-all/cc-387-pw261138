/**
 * Smoke-test the workbench's render functions against a real wire payload,
 * using a tiny DOM stub instead of a browser.
 *
 * It cannot judge layout, but it does prove the client consumes the actual
 * server payload without throwing and that the numbers reach the DOM.
 *
 *   node tools/client_smoke.js /tmp/wire.json
 */
const fs = require("fs");
const vm = require("vm");

const wirePath = process.argv[2];
if (!wirePath) {
  console.error("usage: node tools/client_smoke.js <wire.json>");
  process.exit(2);
}
const html = fs.readFileSync(__dirname + "/../web/app.html", "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, innerHTML: "", textContent: "", disabled: false, value: "",
      style: {}, dataset: {}, onclick: null,
      classList: { add() {}, remove() {}, contains: () => false },
      setAttribute() {}, getAttribute() { return null; },
      querySelectorAll: () => [], querySelector: () => null,
      appendChild() {}, addEventListener() {},
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 300 }),
    };
  }
  return els[id];
}
const doc = {
  querySelector: (s) => el(s.replace(/^#/, "")),
  querySelectorAll: () => [],
  documentElement: { dataset: {} },
  createElement: () => ({ classList: { add() {}, remove() {} }, style: {}, remove() {} }),
  body: { appendChild() {} },
};
const ctx = {
  document: doc,
  window: { open() {}, matchMedia: () => ({ matches: false }) },
  location: { search: "" },
  URLSearchParams,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  setTimeout: () => 0, clearTimeout: () => {},
  console,
  getComputedStyle: () => ({ getPropertyValue: () => "#2a78d6" }),
};
vm.createContext(ctx);
const cut = script.lastIndexOf("(async function init()");
// `const S` is script-scoped, not a global: hand it out explicitly.
vm.runInContext(script.slice(0, cut) + "\n;globalThis.__S = S;", ctx);

const wire = JSON.parse(fs.readFileSync(wirePath, "utf8"));
const S = ctx.__S;
S.result = wire;
S.state = { sources: wire.sources.map((s) => ({ source: s.source, version: s.version })), fixtures: [], runs: [], generation: 1, history: [], notes: [] };

const fns = ["renderLegend", "renderStats", "renderSemantics", "renderBlocks",
             "renderProvenance", "renderIdentity", "renderWarnings", "renderHist",
             "renderDiffHint", "renderMap"];
const failures = [];
for (const fn of fns) {
  try {
    vm.runInContext(fn + "()", ctx);
  } catch (e) {
    failures.push(`${fn}: ${e.message}`);
  }
}

const checks = {
  "stats rendered": els["stats"] && els["stats"].innerHTML.includes("Support-weighted mean"),
  "mean value present": els["stats"] && els["stats"].innerHTML.includes(
    wire.summary.mean.mean.toFixed(3)),
  "conservation shown": els["semantics"] && els["semantics"].innerHTML.includes("Preserved"),
  "provenance hashes": els["provenance"] && /[0-9a-f]{12}/.test(els["provenance"].innerHTML),
  "identity fingerprint": els["identity"] && els["identity"].innerHTML.includes(
    wire.fingerprint),
  "histogram svg": els["hist"] && els["hist"].innerHTML.includes("<svg"),
  "map svg cells": els["map"] && (els["map"].innerHTML.match(/data-i=/g) || []).length
    === wire.values.length,
  "org missing marks": els["map"] && els["map"].innerHTML.includes("stroke=\"var(--muted)\"")
    === wire.values.some((v) => v === null),
};
for (const [k, v] of Object.entries(checks)) if (!v) failures.push("check failed: " + k);

if (failures.length) {
  console.error("CLIENT SMOKE FAILED");
  for (const f of failures) console.error("  -", f);
  process.exit(1);
}
console.log(`client smoke passed: ${wire.values.length} cells, ${wire.sources.length} sources, ` +
            `fingerprint ${wire.fingerprint.slice(0, 10)}`);
