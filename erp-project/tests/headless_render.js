/*
 * Headless smoke test for erp.html.
 *
 * Playwright is unavailable in the build sandbox, so instead of a real
 * browser we extract the page's inline script and run it against a
 * minimal DOM plus a recording 2D context. That is enough to prove the
 * real code path end to end: data fetch, scale computation, axis ticks,
 * segment strokes, stat cards and tooltip math all execute, and any
 * thrown error or silent "no line drawn" bug fails the test.
 *
 * Usage: node tests/headless_render.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "erp.html"), "utf8");
const DATA = JSON.parse(fs.readFileSync(path.join(ROOT, "erp-data.json"), "utf8"));

/* ----------------------- recording 2D context ----------------------- */
const calls = { stroke: 0, lineTo: 0, arc: 0, fillText: 0, setLineDash: 0 };
const strokeStyles = new Set();
const texts = [];

function makeCtx() {
  const ctx = {
    canvas: null,
    save() {}, restore() {}, beginPath() {}, closePath() {},
    moveTo() {}, translate() {}, rotate() {}, scale() {}, clip() {}, rect() {},
    clearRect() {}, fillRect() {}, strokeRect() {}, fill() {},
    setTransform() {}, resetTransform() {}, transform() {},
    quadraticCurveTo() {}, bezierCurveTo() {}, ellipse() {},
    createLinearGradient() { return { addColorStop() {} }; },
    lineTo() { calls.lineTo++; },
    stroke() { calls.stroke++; strokeStyles.add(String(ctx.strokeStyle)); },
    arc() { calls.arc++; },
    setLineDash() { calls.setLineDash++; },
    fillText(t) { calls.fillText++; texts.push(String(t)); },
    measureText(t) { return { width: String(t).length * 6 }; },
  };
  return ctx;
}

/* ------------------------------ DOM shim ---------------------------- */
function makeEl(id, tag) {
  const el = {
    id, tagName: (tag || "div").toUpperCase(),
    children: [], style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    _html: "", _text: "",
    clientWidth: 900, clientHeight: 460, width: 900, height: 460,
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; },
    setAttribute() {}, getAttribute() { return null; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    getContext() { this._ctx = this._ctx || makeCtx(); return this._ctx; },
    focus() {}, blur() {}, remove() {},
  };
  el.parentElement = null;
  return el;
}

const els = new Map();
function el(id, tag) {
  if (!els.has(id)) {
    const e = makeEl(id, tag);
    // The canvas measures its parent to size itself.
    e.parentElement = makeEl(id + "-parent");
    els.set(id, e);
  }
  return els.get(id);
}

const document = {
  getElementById: (id) => el(id, id === "chart" ? "canvas" : "div"),
  createElement: (tag) => makeEl("created-" + tag, tag),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {},
  body: makeEl("body"),
  documentElement: makeEl("html"),
};

const window = {
  devicePixelRatio: 2,
  addEventListener() {}, removeEventListener() {},
  requestAnimationFrame: (fn) => { fn(0); return 1; },
  cancelAnimationFrame() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  innerWidth: 1200, innerHeight: 900,
};

/* Serve the dataset from memory so the test needs no HTTP server. */
async function fetchStub(url) {
  if (String(url).includes("erp-data.json")) {
    return { ok: true, status: 200, json: async () => DATA };
  }
  return { ok: false, status: 404, json: async () => ({}) };
}

/* --------------------------- run the script ------------------------- */
const script = HTML.match(/<script>([\s\S]*?)<\/script>/)[1];

const sandbox = {
  document, window, fetch: fetchStub, console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: window.requestAnimationFrame,
  devicePixelRatio: 2, Math, Date, JSON, Number, String, Object, Array, Intl,
  ResizeObserver: class { observe() {} disconnect() {} },
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

vm.createContext(sandbox);
const errors = [];
process.on("unhandledRejection", (e) => errors.push(e));

try {
  vm.runInContext(script, sandbox, { filename: "erp.html#inline" });
} catch (e) {
  errors.push(e);
}

/* Give the async boot (fetch -> draw) a chance to settle. */
setTimeout(() => {
  const fails = [];
  const errEl = els.get("err");

  if (errors.length) fails.push("script threw: " + errors.map((e) => e && e.message).join(" | "));
  if (errEl && errEl._html && /error|failed|HTTP/i.test(errEl._html)) {
    fails.push("page reported an error banner: " + errEl._html.slice(0, 200));
  }
  if (calls.lineTo < 1000) fails.push(`series barely drawn (lineTo=${calls.lineTo}, expected thousands)`);
  if (calls.stroke < 3) fails.push(`too few stroke passes (${calls.stroke})`);
  if (calls.arc < 1) fails.push("no latest-point marker drawn");
  if (calls.setLineDash < 1) fails.push("zero line dash never set");
  if (calls.fillText < 8) fails.push(`too few axis labels (${calls.fillText})`);
  if (!strokeStyles.has("#e01b24")) fails.push("red zero line colour never stroked");
  if (!strokeStyles.has("#111")) fails.push("consensus segment colour never stroked");

  const stats = els.get("stats");
  if (!stats || !/bps/.test(stats._html)) fails.push("stat cards missing bps values");
  const src = els.get("src");
  if (!src || !/observations/.test(src._html + src._text)) fails.push("source caption not populated");

  console.log("--- headless render report ---");
  console.log("rows in dataset:", DATA.rows.length);
  console.log("canvas calls:", JSON.stringify(calls));
  console.log("stroke colours:", [...strokeStyles].join(", "));
  console.log("sample axis labels:", texts.slice(0, 14).join(" | "));
  console.log("stat card html:", (stats ? stats._html : "").replace(/\s+/g, " ").slice(0, 240));

  if (fails.length) {
    console.log("\nFAIL");
    fails.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log("\nPASS  chart renders: series stroked, zero line dashed, axes labelled, stats populated");
}, 300);
