"""Self-contained interactive page for one projected catalog graph.

The page is one HTML document with no runtime dependency beyond two Google
Fonts: the graph is embedded as JSON, laid out in the browser, and explored by
clicking.  Colors and faces are the tokens the documentation site already uses
(``docs/stylesheets/extra.css``), so a catalog graph looks like the rest of the
product rather than like a separate tool.
"""

from __future__ import annotations

import json

from memseek.catalog_graph import CatalogGraph


def render_graph_page(graph: CatalogGraph, *, standalone: bool = True) -> str:
    """Render one graph as an interactive page.

    ``standalone`` wraps the page in a complete HTML document for writing to a
    file.  Without it the result is the document body only, which is what an
    embedding host that supplies its own ``<head>`` expects.
    """

    payload = json.dumps(graph.as_json(), separators=(",", ":"), sort_keys=True)
    # A closing tag inside embedded JSON would end the script element early.
    payload = payload.replace("</", "<\\/")
    name = graph.package.split("@", 1)[0]
    body = (
        _BODY.replace("__GRAPH_JSON__", payload)
        .replace("__TITLE__", _escape(f"{name} catalog graph"))
        .replace("__PACKAGE__", _escape(graph.package))
        .replace("__CATALOG_HASH__", _escape(graph.catalog_hash))
        .replace("__CATALOG_HASH_SHORT__", _escape(graph.catalog_hash[:12]))
        .replace("__PACKAGE_HASH__", _escape(graph.package_hash))
        .replace("__PACKAGE_HASH_SHORT__", _escape(graph.package_hash[:12]))
    )
    if not standalone:
        return body
    return _DOCUMENT.replace("__BODY__", body)


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 14px system-ui, sans-serif; background: #f7f7f5; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
__BODY__
</body>
</html>
"""


_BODY = r"""<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap">
<style>
/* Tokens: the light palette in full, then the dark one re-declared for each of
   the viewer's three theme states. Values match docs/stylesheets/extra.css. */
:root {
  --bg: #f7f7f5;
  --surface: #ffffff;
  --surface-2: #f1f2ef;
  --border: #e3e4df;
  --border-strong: #cfd1ca;
  --text: #16191c;
  --muted: #55606a;
  --faint: #666f78;
  --code-bg: #fbfbf9;
  --orange: #e0641c;
  --pink: #e02a54;
  --cyan: #0e9e90;
  --violet: #6d4ae0;
  --green: #1f9d57;
  --amber: #c07d16;
  --plate-shadow: 0 1px 2px rgba(22, 25, 28, 0.06);
  --display: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --code: ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace;
  --prose: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0c0e11;
    --surface: #12161b;
    --surface-2: #1a1f27;
    --border: #232a33;
    --border-strong: #33404c;
    --text: #e8eaed;
    --muted: #9aa1ab;
    --faint: #7d8794;
    --code-bg: #0f1318;
    --orange: #ff7a2f;
    --pink: #ff3d67;
    --cyan: #2fd4c4;
    --violet: #9b87ff;
    --green: #58d68d;
    --amber: #ffb14d;
    --plate-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
  }
}

:root[data-theme="dark"] {
  --bg: #0c0e11;
  --surface: #12161b;
  --surface-2: #1a1f27;
  --border: #232a33;
  --border-strong: #33404c;
  --text: #e8eaed;
  --muted: #9aa1ab;
  --faint: #7d8794;
  --code-bg: #0f1318;
  --orange: #ff7a2f;
  --pink: #ff3d67;
  --cyan: #2fd4c4;
  --violet: #9b87ff;
  --green: #58d68d;
  --amber: #ffb14d;
  --plate-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
}

/* One hue per family — memory, computation, execution, authored text,
   interface — so a plate's color says what layer of the catalog it belongs to
   before its label is read. Secondary members of a family sit at lower
   saturation against the same hue. */
:root {
  --kind-collection: var(--cyan);
  --kind-view: color-mix(in oklab, var(--cyan) 62%, var(--muted));
  --kind-search_profile: color-mix(in oklab, var(--cyan) 30%, var(--muted));
  --kind-derivation: var(--orange);
  --kind-trigger: color-mix(in oklab, var(--orange) 62%, var(--amber));
  --kind-processor: var(--amber);
  --kind-computer: var(--violet);
  --kind-program: color-mix(in oklab, var(--violet) 58%, var(--muted));
  --kind-agent: color-mix(in oklab, var(--violet) 74%, var(--pink));
  --kind-artifact: var(--green);
  --kind-context_policy: color-mix(in oklab, var(--green) 55%, var(--muted));
  --kind-toolset: color-mix(in oklab, var(--violet) 60%, var(--cyan));
  --kind-tool: color-mix(in oklab, var(--violet) 34%, var(--cyan));
  --kind-mcp: var(--pink);
  --kind-mcp_tool: color-mix(in oklab, var(--pink) 62%, var(--muted));
  --kind-model: color-mix(in oklab, var(--amber) 62%, var(--muted));
  --rel-triggers: var(--amber);
  --rel-reads: var(--cyan);
  --rel-writes: var(--orange);
  --rel-runs: var(--violet);
  --rel-mounts: var(--pink);
  --rel-uses: var(--border-strong);
  --rel-annotates: var(--green);
  --rel-routes: var(--faint);
  --rel-exposes: var(--green);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--prose);
  -webkit-font-smoothing: antialiased;
}

.shell {
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
  height: 100dvh;
  min-height: 560px;
}

/* ---------- top bar ---------- */

.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 28px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.brand { display: flex; align-items: center; gap: 12px; margin-right: auto; }

.dot {
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: 50%;
  background: linear-gradient(120deg, var(--orange), var(--pink));
}

.eyebrow {
  margin: 0;
  font-family: var(--display);
  font-size: 9.5px;
  font-weight: 500;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--faint);
}

.brand h1 {
  margin: 2px 0 0;
  font-family: var(--display);
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.01em;
}

.meters { display: flex; gap: 24px; margin: 0; }
.meters div { display: flex; flex-direction: column; gap: 3px; }
.meters dt {
  font-family: var(--display);
  font-size: 9.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--faint);
}
.meters dd {
  margin: 0;
  font-family: var(--display);
  font-size: 13px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.meters .hash { color: var(--muted); cursor: help; }

.tools { display: flex; align-items: center; gap: 8px; }

.search { position: relative; display: block; }
.search input {
  width: 210px;
  padding: 7px 10px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--display);
  font-size: 12px;
}
.search input::placeholder { color: var(--faint); }
.search input:focus-visible { outline: 2px solid var(--orange); outline-offset: 1px; }

.btn {
  padding: 7px 11px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg);
  color: var(--muted);
  font-family: var(--display);
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  cursor: pointer;
}
.btn:hover { color: var(--text); border-color: var(--faint); }
.btn[aria-pressed="true"] {
  color: var(--bg);
  background: var(--orange);
  border-color: var(--orange);
}
.btn:focus-visible { outline: 2px solid var(--orange); outline-offset: 1px; }

/* ---------- filter legend ---------- */

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 18px;
  padding: 9px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-2);
}

.chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 9px 4px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  font-family: var(--display);
  font-size: 10.5px;
  letter-spacing: 0.03em;
  cursor: pointer;
}
.chip:hover { border-color: var(--border-strong); }
.chip:focus-visible { outline: 2px solid var(--orange); outline-offset: 1px; }
.chip[aria-pressed="false"] { color: var(--faint); background: transparent; }
.chip[aria-pressed="false"] .swatch, .chip[aria-pressed="false"] .wire { opacity: 0.28; }
.chip .count { color: var(--faint); font-variant-numeric: tabular-nums; }

.swatch { width: 9px; height: 9px; border-radius: 2px; background: var(--chip-color); }
.wire { width: 16px; height: 2px; border-radius: 2px; background: var(--chip-color); }
.wire.dashed {
  height: 0;
  border-top: 2px dashed var(--chip-color);
  background: none;
}

/* ---------- stage ---------- */

.stage {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 372px;
  min-height: 0;
}

.canvas {
  position: relative;
  overflow: hidden;
  background:
    radial-gradient(circle at 1px 1px, var(--border) 1px, transparent 0) 0 0 / 28px 28px;
  cursor: grab;
  touch-action: none;
}
.canvas.panning { cursor: grabbing; }

.viewport { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
.viewport svg { position: absolute; top: 0; left: 0; overflow: visible; }
#plates { position: absolute; top: 0; left: 0; }

.hint {
  position: absolute;
  left: 20px;
  bottom: 14px;
  margin: 0;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: color-mix(in oklab, var(--surface) 88%, transparent);
  color: var(--faint);
  font-family: var(--display);
  font-size: 10.5px;
  letter-spacing: 0.03em;
  pointer-events: none;
}

/* ---------- node plates ---------- */

.plate {
  position: absolute;
  width: 214px;
  height: 58px;
  display: grid;
  grid-template-columns: 3px minmax(0, 1fr);
  padding: 0;
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: var(--surface);
  box-shadow: var(--plate-shadow);
  text-align: left;
  cursor: pointer;
  overflow: hidden;
}
.plate .rail { background: var(--kind-color); }
.plate .body { min-width: 0; padding: 6px 9px; display: grid; gap: 1px; }
.plate .top { display: flex; align-items: baseline; gap: 6px; }
.plate .kind {
  font-family: var(--display);
  font-size: 8.5px;
  font-weight: 500;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--kind-color);
}
.plate .version {
  margin-left: auto;
  font-family: var(--display);
  font-size: 9px;
  color: var(--faint);
  font-variant-numeric: tabular-nums;
}
.plate .name {
  font-family: var(--display);
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plate .note {
  font-size: 10.5px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plate:hover { border-color: var(--kind-color); }
.plate:focus-visible { outline: 2px solid var(--orange); outline-offset: 2px; }
.plate.external { border-style: dashed; }
.plate.selected {
  border-color: var(--kind-color);
  background: var(--surface-2);
  box-shadow: 0 0 0 1px var(--kind-color), var(--plate-shadow);
}
.plate.match { box-shadow: 0 0 0 2px var(--orange); }
.plate.dim { opacity: 0.2; }

/* ---------- wires ---------- */

.wirepath { fill: none; stroke-width: 1.4; }
.wirepath.dim { opacity: 0.12; }
.wirepath.on { stroke-width: 2.4; }
.wirelabel {
  font-family: var(--display);
  font-size: 9px;
  fill: var(--muted);
  paint-order: stroke;
  stroke: var(--bg);
  stroke-width: 3px;
  stroke-linejoin: round;
}

/* ---------- inspector ---------- */

.panel {
  border-left: 1px solid var(--border);
  background: var(--surface);
  overflow-y: auto;
  padding: 18px 20px 40px;
}

.panel h2 {
  margin: 4px 0 0;
  font-family: var(--display);
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.02em;
  overflow-wrap: anywhere;
}
.panel .lede {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--muted);
  max-width: 34ch;
}
.panel .kindline { display: flex; align-items: center; gap: 7px; }
.panel .kindline .swatch { width: 8px; height: 8px; }

.section {
  margin-top: 22px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.section > h3 {
  margin: 0 0 10px;
  font-family: var(--display);
  font-size: 9.5px;
  font-weight: 500;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--faint);
}

.facts { display: grid; gap: 9px; margin: 0; }
.facts > div { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 10px; }
.facts dt {
  font-family: var(--display);
  font-size: 9.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--faint);
  padding-top: 2px;
}
.facts dd {
  margin: 0;
  font-family: var(--code);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text);
  overflow-wrap: anywhere;
}

.links { display: grid; gap: 4px; }
.link {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: none;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.link:hover { background: var(--surface-2); border-color: var(--border); }
.link:focus-visible { outline: 2px solid var(--orange); outline-offset: 1px; }
.link .arrow { font-family: var(--display); font-size: 11px; color: var(--rel-color); }
.link .what { min-width: 0; }
.link .rel {
  font-family: var(--display);
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--rel-color);
}
.link .who {
  font-family: var(--display);
  font-size: 11.5px;
  overflow-wrap: anywhere;
}
.link .who .qual { color: var(--faint); }

details.definition summary {
  font-family: var(--display);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--faint);
  cursor: pointer;
}
details.definition summary:focus-visible { outline: 2px solid var(--orange); outline-offset: 2px; }
details.definition pre {
  max-height: 340px;
  margin: 10px 0 0;
  padding: 10px 12px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--code-bg);
  font-family: var(--code);
  font-size: 11px;
  line-height: 1.55;
  color: var(--muted);
}

.empty { color: var(--faint); font-size: 12px; }

.sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}

@media (max-width: 980px) {
  .shell { height: auto; min-height: 0; }
  .stage { grid-template-columns: minmax(0, 1fr); }
  .canvas { height: 62dvh; min-height: 420px; }
  .panel { border-left: 0; border-top: 1px solid var(--border); }
  .brand { margin-right: 0; }
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
</style>

<div class="shell">
  <header class="bar">
    <div class="brand">
      <span class="dot" aria-hidden="true"></span>
      <div>
        <p class="eyebrow">Catalog package</p>
        <h1>__PACKAGE__</h1>
      </div>
    </div>
    <dl class="meters">
      <div><dt>Parts</dt><dd id="count-nodes">0</dd></div>
      <div><dt>References</dt><dd id="count-edges">0</dd></div>
      <div>
        <dt>Catalog hash</dt>
        <dd class="hash" title="__CATALOG_HASH__">__CATALOG_HASH_SHORT__</dd>
      </div>
    </dl>
    <div class="tools">
      <label class="search">
        <span class="sr">Find a definition</span>
        <input id="search" type="search" placeholder="Find a definition" autocomplete="off" spellcheck="false">
      </label>
      <button class="btn" id="isolate" type="button" aria-pressed="false" title="Show only what the selection reaches">Isolate</button>
      <button class="btn" id="fit" type="button" title="Fit the graph to the canvas">Fit</button>
      <button class="btn" id="theme" type="button">Dark</button>
    </div>
  </header>

  <div class="filters">
    <div class="chips" id="kind-chips" role="group" aria-label="Definition kinds"></div>
    <div class="chips" id="relation-chips" role="group" aria-label="Reference kinds"></div>
  </div>

  <main class="stage">
    <div class="canvas" id="canvas">
      <div class="viewport" id="viewport">
        <svg id="wires" aria-hidden="true"></svg>
        <div id="plates"></div>
      </div>
      <p class="hint">Drag to pan · scroll to zoom · Fit for the whole package</p>
    </div>
    <aside class="panel" id="panel" aria-live="polite"></aside>
  </main>
</div>

<script>
"use strict";

const DATA = __GRAPH_JSON__;

const NODE_W = 214;
const NODE_H = 58;
const GAP_X = 96;
const GAP_Y = 16;
const PAD = 40;

const byId = new Map(DATA.nodes.map((node) => [node.id, node]));
const kindRank = new Map(DATA.kinds.map((kind, index) => [kind, index]));
const relationRank = new Map(DATA.relations.map((relation, index) => [relation, index]));

const kindCounts = new Map();
for (const node of DATA.nodes) {
  kindCounts.set(node.kind, (kindCounts.get(node.kind) || 0) + 1);
}
const relationCounts = new Map();
for (const edge of DATA.edges) {
  relationCounts.set(edge.relation, (relationCounts.get(edge.relation) || 0) + 1);
}

// Dashed relations pair with a solid one on the same hue, so nine relations
// stay distinguishable across six accent colors.
const DASHED = new Set(["routes", "exposes", "mounts"]);

const state = {
  selected: null,
  hovered: null,
  kinds: new Set(DATA.kinds.filter((kind) => kindCounts.has(kind))),
  relations: new Set(DATA.relations.filter((relation) => relationCounts.has(relation))),
  isolate: false,
  query: "",
  view: { x: 0, y: 0, k: 1 },
};

const el = {
  canvas: document.getElementById("canvas"),
  viewport: document.getElementById("viewport"),
  wires: document.getElementById("wires"),
  plates: document.getElementById("plates"),
  panel: document.getElementById("panel"),
  search: document.getElementById("search"),
  isolate: document.getElementById("isolate"),
  fit: document.getElementById("fit"),
  theme: document.getElementById("theme"),
  kindChips: document.getElementById("kind-chips"),
  relationChips: document.getElementById("relation-chips"),
  countNodes: document.getElementById("count-nodes"),
  countEdges: document.getElementById("count-edges"),
};

const SVG_NS = "http://www.w3.org/2000/svg";

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function label(kind) {
  return kind.replace(/_/g, " ");
}

function title(node) {
  return node.version ? node.name + "@" + node.version : node.name;
}

/* ---------- selection of what is drawn ---------- */

function activeEdges() {
  return DATA.edges.filter((edge) => {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    return (
      state.relations.has(edge.relation) &&
      state.kinds.has(source.kind) &&
      state.kinds.has(target.kind)
    );
  });
}

function reachable(id, edges) {
  const adjacent = new Map();
  for (const edge of edges) {
    if (!adjacent.has(edge.source)) adjacent.set(edge.source, []);
    if (!adjacent.has(edge.target)) adjacent.set(edge.target, []);
    adjacent.get(edge.source).push(edge.target);
    adjacent.get(edge.target).push(edge.source);
  }
  const seen = new Set([id]);
  const queue = [id];
  while (queue.length) {
    for (const next of adjacent.get(queue.shift()) || []) {
      if (!seen.has(next)) {
        seen.add(next);
        queue.push(next);
      }
    }
  }
  return seen;
}

function visible() {
  const edges = activeEdges();
  let nodes = DATA.nodes.filter((node) => state.kinds.has(node.kind));
  if (state.isolate && state.selected && byId.has(state.selected)) {
    const keep = reachable(state.selected, edges);
    nodes = nodes.filter((node) => keep.has(node.id));
  }
  const shown = new Set(nodes.map((node) => node.id));
  return { nodes, edges: edges.filter((edge) => shown.has(edge.source) && shown.has(edge.target)) };
}

/* ---------- layered layout ---------- */

function layout(nodes, edges) {
  const index = new Map(nodes.map((node, position) => [node.id, position]));
  const inside = (edge) => index.has(edge.source) && index.has(edge.target);
  const all = edges.filter(inside).map((edge) => [index.get(edge.source), index.get(edge.target)]);
  const pairs = edges
    .filter((edge) => inside(edge) && edge.flow === "forward")
    .map((edge) => [index.get(edge.source), index.get(edge.target)]);

  // Longest-path layering over the flow edges only, capped so a cycle in the
  // catalog (a Pipeline that reads the Collection it writes) settles instead
  // of running away.
  const layer = new Array(nodes.length).fill(0);
  for (let pass = 0; pass < nodes.length; pass += 1) {
    let moved = false;
    for (const [source, target] of pairs) {
      const candidate = layer[source] + 1;
      if (layer[target] < candidate && candidate <= nodes.length) {
        layer[target] = candidate;
        moved = true;
      }
    }
    if (!moved) break;
  }

  // A part that only binds — a model, a policy, the Processor annotating a
  // Collection — has no place in the flow, so it settles next to whatever
  // refers to it: after the parts that reach it, before the parts it reaches.
  const placed = new Set();
  for (const [source, target] of pairs) {
    placed.add(source);
    placed.add(target);
  }
  const before = nodes.map(() => []);
  const after = nodes.map(() => []);
  for (const [source, target] of all) {
    after[source].push(target);
    before[target].push(source);
  }
  for (let pass = 0; pass < 3; pass += 1) {
    nodes.forEach((node, position) => {
      if (placed.has(position)) return;
      const inbound = before[position].filter((other) => placed.has(other));
      const outbound = after[position].filter((other) => placed.has(other));
      if (inbound.length) {
        layer[position] = Math.max(...inbound.map((other) => layer[other])) + 1;
      } else if (outbound.length) {
        layer[position] = Math.min(...outbound.map((other) => layer[other])) - 1;
      } else {
        return;
      }
      // Settled, so a binding that hangs off this one can settle next pass.
      placed.add(position);
    });
  }
  const lowest = Math.min(0, ...layer);
  if (lowest < 0) {
    for (let position = 0; position < layer.length; position += 1) layer[position] -= lowest;
  }

  const grouped = new Map();
  nodes.forEach((node, position) => {
    const key = layer[position];
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(position);
  });
  const columns = [...grouped.keys()].sort((a, b) => a - b).map((key) => grouped.get(key));

  const stable = (a, b) =>
    kindRank.get(nodes[a].kind) - kindRank.get(nodes[b].kind) ||
    nodes[a].id.localeCompare(nodes[b].id);
  for (const column of columns) column.sort(stable);

  const row = new Array(nodes.length).fill(0);
  const reindex = () => columns.forEach((column) => column.forEach((position, r) => (row[position] = r)));
  reindex();

  const neighbours = nodes.map(() => []);
  for (const [source, target] of all) {
    neighbours[source].push(target);
    neighbours[target].push(source);
  }
  // Barycentre passes pull a node next to the parts it references, which is
  // what makes the columns readable rather than merely correct.
  for (let pass = 0; pass < 6; pass += 1) {
    for (const column of columns) {
      const centre = new Map(
        column.map((position) => {
          const near = neighbours[position];
          if (!near.length) return [position, row[position]];
          const total = near.reduce((sum, other) => sum + row[other], 0);
          return [position, total / near.length];
        })
      );
      column.sort((a, b) => centre.get(a) - centre.get(b) || stable(a, b));
    }
    reindex();
  }

  const tallest = columns.reduce((most, column) => Math.max(most, column.length), 1);
  const height = tallest * (NODE_H + GAP_Y) - GAP_Y;
  columns.forEach((column, depth) => {
    const span = column.length * (NODE_H + GAP_Y) - GAP_Y;
    const top = (height - span) / 2;
    column.forEach((position, r) => {
      nodes[position].x = PAD + depth * (NODE_W + GAP_X);
      nodes[position].y = PAD + top + r * (NODE_H + GAP_Y);
    });
  });

  return {
    width: PAD * 2 + Math.max(1, columns.length) * (NODE_W + GAP_X) - GAP_X,
    height: height + PAD * 2,
  };
}

function edgePath(source, target) {
  const x1 = source.x + NODE_W;
  const y1 = source.y + NODE_H / 2;
  const x2 = target.x;
  const y2 = target.y + NODE_H / 2;
  if (x2 >= x1 - 8) {
    const bend = Math.max(34, (x2 - x1) * 0.5);
    return `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
  }
  // A reference that points back up the graph leaves and re-enters underneath,
  // so it never crosses the plates it passes.
  const from = source.x + NODE_W / 2;
  const to = target.x + NODE_W / 2;
  const dip = Math.max(source.y, target.y) + NODE_H + 46;
  return `M${from},${source.y + NODE_H} C${from},${dip} ${to},${dip} ${to},${target.y + NODE_H}`;
}

function midpoint(path) {
  const length = path.getTotalLength();
  return path.getPointAtLength(length / 2);
}

/* ---------- rendering ---------- */

let drawn = { nodes: [], edges: [] };

function render() {
  const { nodes, edges } = visible();
  const box = layout(nodes, edges);
  drawn = { nodes, edges };

  el.wires.setAttribute("width", box.width);
  el.wires.setAttribute("height", box.height);
  el.wires.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  el.viewport.style.width = box.width + "px";
  el.viewport.style.height = box.height + "px";

  const markers = DATA.relations
    .map(
      (relation) =>
        `<marker id="arrow-${relation}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7"` +
        ` markerHeight="7" orient="auto-start-reverse">` +
        `<path d="M0,0.6 L8,4 L0,7.4 Z" fill="var(--rel-${relation})"></path></marker>`
    )
    .join("");
  const wires = edges
    .map((edge, position) => {
      const source = byId.get(edge.source);
      const target = byId.get(edge.target);
      const detail = edge.label ? `${edge.relation} · ${edge.label}` : edge.relation;
      return (
        `<path class="wirepath" data-edge="${position}" d="${edgePath(source, target)}"` +
        ` stroke="var(--rel-${edge.relation})"` +
        (DASHED.has(edge.relation) ? ' stroke-dasharray="5 4"' : "") +
        ` marker-end="url(#arrow-${edge.relation})">` +
        `<title>${escapeHtml(title(source))} ${escapeHtml(detail)} ${escapeHtml(title(target))}</title>` +
        `</path>`
      );
    })
    .join("");
  el.wires.innerHTML = `<defs>${markers}</defs><g id="wiregroup">${wires}</g><g id="wirelabels"></g>`;

  el.plates.style.width = box.width + "px";
  el.plates.style.height = box.height + "px";
  el.plates.innerHTML = nodes
    .map((node) => {
      const version = node.version ? `<span class="version">v${escapeHtml(node.version)}</span>` : "";
      return (
        `<button type="button" class="plate${node.member ? "" : " external"}" data-id="${escapeHtml(node.id)}"` +
        ` style="left:${node.x}px;top:${node.y}px;--kind-color:var(--kind-${node.kind})"` +
        ` title="${escapeHtml(title(node))} — ${escapeHtml(node.summary)}">` +
        `<span class="rail" aria-hidden="true"></span>` +
        `<span class="body">` +
        `<span class="top"><span class="kind">${escapeHtml(label(node.kind))}</span>${version}</span>` +
        `<span class="name">${escapeHtml(node.name)}</span>` +
        `<span class="note">${escapeHtml(node.summary)}</span>` +
        `</span></button>`
      );
    })
    .join("");

  el.countNodes.textContent =
    nodes.length === DATA.nodes.length ? String(nodes.length) : `${nodes.length}/${DATA.nodes.length}`;
  el.countEdges.textContent =
    edges.length === DATA.edges.length ? String(edges.length) : `${edges.length}/${DATA.edges.length}`;

  emphasize();
}

function emphasize() {
  const focus = state.hovered || state.selected;
  const near = new Set();
  if (focus) {
    near.add(focus);
    for (const edge of drawn.edges) {
      if (edge.source === focus) near.add(edge.target);
      if (edge.target === focus) near.add(edge.source);
    }
  }
  const query = state.query.trim().toLowerCase();

  for (const plate of el.plates.children) {
    const id = plate.dataset.id;
    const node = byId.get(id);
    const matched = query.length > 0 && matches(node, query);
    plate.classList.toggle("selected", id === state.selected);
    plate.classList.toggle("match", matched);
    plate.classList.toggle("dim", (focus && !near.has(id)) || (query.length > 0 && !matched));
  }

  const labels = document.getElementById("wirelabels");
  labels.textContent = "";
  const paths = el.wires.querySelectorAll(".wirepath");
  paths.forEach((path) => {
    const edge = drawn.edges[Number(path.dataset.edge)];
    const incident = focus && (edge.source === focus || edge.target === focus);
    path.classList.toggle("on", Boolean(incident));
    path.classList.toggle("dim", Boolean(focus) && !incident);
    // Edge labels are the source names, task ids, and mount paths behind a
    // reference: worth reading for one selected part, noise for all of them.
    if (incident && state.selected && edge.label) {
      const point = midpoint(path);
      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("class", "wirelabel");
      text.setAttribute("x", point.x);
      text.setAttribute("y", point.y - 5);
      text.setAttribute("text-anchor", "middle");
      text.textContent = edge.label;
      labels.appendChild(text);
    }
  });
}

function matches(node, query) {
  return (
    node.name.toLowerCase().includes(query) ||
    node.kind.replace(/_/g, " ").includes(query) ||
    node.summary.toLowerCase().includes(query) ||
    node.facts.some(([, value]) => String(value).toLowerCase().includes(query))
  );
}

/* ---------- inspector ---------- */

function factList(facts) {
  return (
    `<dl class="facts">` +
    facts
      .map(
        ([key, value]) =>
          `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`
      )
      .join("") +
    `</dl>`
  );
}

function linkRow(edge, direction) {
  const other = byId.get(direction === "out" ? edge.target : edge.source);
  const arrow = direction === "out" ? "→" : "←";
  const qualifier = edge.label ? ` <span class="qual">· ${escapeHtml(edge.label)}</span>` : "";
  return (
    `<button type="button" class="link" data-goto="${escapeHtml(other.id)}"` +
    ` style="--rel-color:var(--rel-${edge.relation})">` +
    `<span class="arrow" aria-hidden="true">${arrow}</span>` +
    `<span class="what">` +
    `<span class="rel">${escapeHtml(edge.relation)}</span><br>` +
    `<span class="who">${escapeHtml(title(other))}${qualifier}</span>` +
    `</span></button>`
  );
}

function renderPanel() {
  if (!state.selected || !byId.has(state.selected)) {
    el.panel.innerHTML =
      `<p class="eyebrow">Package</p>` +
      `<h2>__PACKAGE__</h2>` +
      `<p class="lede">Columns run left to right in dependency order: what a part reads,` +
      ` runs, or mounts sits to its left. Click any plate for its compiled definition.</p>` +
      `<div class="section"><h3>Declared</h3>${factList(DATA.overview)}</div>` +
      `<div class="section"><h3>Identity</h3>${factList([
        ["catalog hash", "__CATALOG_HASH__"],
        ["package hash", "__PACKAGE_HASH__"],
      ])}</div>`;
    return;
  }

  const node = byId.get(state.selected);
  const outgoing = DATA.edges.filter((edge) => edge.source === node.id);
  const incoming = DATA.edges.filter((edge) => edge.target === node.id);
  const order = (a, b) =>
    relationRank.get(a.relation) - relationRank.get(b.relation) ||
    a.target.localeCompare(b.target) ||
    a.source.localeCompare(b.source);
  outgoing.sort(order);
  incoming.sort(order);

  const definition = node.definition
    ? `<details class="definition"><summary>Compiled definition</summary>` +
      `<pre>${escapeHtml(JSON.stringify(node.definition, null, 2))}</pre></details>`
    : `<p class="empty">This part is declared by another package.</p>`;

  el.panel.innerHTML =
    `<p class="eyebrow kindline">` +
    `<span class="swatch" style="--chip-color:var(--kind-${node.kind})" aria-hidden="true"></span>` +
    `${escapeHtml(label(node.kind))}${node.member ? "" : " · external"}</p>` +
    `<h2>${escapeHtml(node.name)}${node.version ? `<span class="version"> v${escapeHtml(node.version)}</span>` : ""}</h2>` +
    `<p class="lede">${escapeHtml(node.summary)}</p>` +
    `<div class="section"><h3>Declared</h3>${factList(node.facts)}</div>` +
    (incoming.length
      ? `<div class="section"><h3>Upstream</h3><div class="links">${incoming
          .map((edge) => linkRow(edge, "in"))
          .join("")}</div></div>`
      : "") +
    (outgoing.length
      ? `<div class="section"><h3>Downstream</h3><div class="links">${outgoing
          .map((edge) => linkRow(edge, "out"))
          .join("")}</div></div>`
      : "") +
    `<div class="section">${definition}</div>`;
}

/* ---------- filters ---------- */

function renderChips() {
  el.kindChips.innerHTML = DATA.kinds
    .filter((kind) => kindCounts.has(kind))
    .map(
      (kind) =>
        `<button type="button" class="chip" data-kind="${kind}" aria-pressed="${state.kinds.has(kind)}">` +
        `<span class="swatch" style="--chip-color:var(--kind-${kind})" aria-hidden="true"></span>` +
        `${escapeHtml(label(kind))} <span class="count">${kindCounts.get(kind)}</span></button>`
    )
    .join("");
  el.relationChips.innerHTML = DATA.relations
    .filter((relation) => relationCounts.has(relation))
    .map(
      (relation) =>
        `<button type="button" class="chip" data-relation="${relation}" aria-pressed="${state.relations.has(relation)}">` +
        `<span class="wire${DASHED.has(relation) ? " dashed" : ""}" style="--chip-color:var(--rel-${relation})"` +
        ` aria-hidden="true"></span>` +
        `${escapeHtml(relation)} <span class="count">${relationCounts.get(relation)}</span></button>`
    )
    .join("");
}

/* ---------- view transform ---------- */

function applyView() {
  const { x, y, k } = state.view;
  el.viewport.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
}

function measure() {
  return {
    width: Number(el.wires.getAttribute("width")) || 1,
    height: Number(el.wires.getAttribute("height")) || 1,
    box: el.canvas.getBoundingClientRect(),
  };
}

function fit() {
  const { width, height, box } = measure();
  const k = Math.max(0.2, Math.min(1, (box.width - 32) / width, (box.height - 32) / height));
  state.view = { k, x: (box.width - width * k) / 2, y: (box.height - height * k) / 2 };
  applyView();
}

// The opening view keeps plates legible instead of fitting a deep catalog into
// the canvas at an unreadable scale: it fits the height, then starts at the
// left, where the parts everything else is derived from sit.
function openingView() {
  const { width, height, box } = measure();
  const k = Math.max(0.45, Math.min(1, (box.height - 32) / height));
  if (width * k <= box.width - 32) {
    fit();
    return;
  }
  state.view = { k, x: 16, y: (box.height - height * k) / 2 };
  applyView();
}

function select(id) {
  state.selected = id;
  if (state.isolate) render();
  else emphasize();
  renderPanel();
}

/* ---------- events ---------- */

el.plates.addEventListener("click", (event) => {
  const plate = event.target.closest(".plate");
  if (plate) select(plate.dataset.id);
});

el.plates.addEventListener("pointerover", (event) => {
  const plate = event.target.closest(".plate");
  const id = plate ? plate.dataset.id : null;
  if (id !== state.hovered) {
    state.hovered = id;
    emphasize();
  }
});

el.plates.addEventListener("pointerleave", () => {
  if (state.hovered) {
    state.hovered = null;
    emphasize();
  }
});

el.panel.addEventListener("click", (event) => {
  const link = event.target.closest("[data-goto]");
  if (link) select(link.dataset.goto);
});

el.kindChips.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-kind]");
  if (!chip) return;
  const kind = chip.dataset.kind;
  if (state.kinds.has(kind)) state.kinds.delete(kind);
  else state.kinds.add(kind);
  chip.setAttribute("aria-pressed", String(state.kinds.has(kind)));
  render();
});

el.relationChips.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-relation]");
  if (!chip) return;
  const relation = chip.dataset.relation;
  if (state.relations.has(relation)) state.relations.delete(relation);
  else state.relations.add(relation);
  chip.setAttribute("aria-pressed", String(state.relations.has(relation)));
  render();
});

el.search.addEventListener("input", () => {
  state.query = el.search.value;
  emphasize();
});

el.search.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const query = state.query.trim().toLowerCase();
  const hit = drawn.nodes.find((node) => query && matches(node, query));
  if (hit) select(hit.id);
});

el.isolate.addEventListener("click", () => {
  state.isolate = !state.isolate;
  el.isolate.setAttribute("aria-pressed", String(state.isolate));
  render();
  openingView();
});

el.fit.addEventListener("click", fit);

el.canvas.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const box = el.canvas.getBoundingClientRect();
    const px = event.clientX - box.left;
    const py = event.clientY - box.top;
    const next = Math.min(2.4, Math.max(0.2, state.view.k * Math.exp(-event.deltaY * 0.0016)));
    const ratio = next / state.view.k;
    state.view.x = px - (px - state.view.x) * ratio;
    state.view.y = py - (py - state.view.y) * ratio;
    state.view.k = next;
    applyView();
  },
  { passive: false }
);

let panning = null;
el.canvas.addEventListener("pointerdown", (event) => {
  if (event.target.closest(".plate")) return;
  panning = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
  el.canvas.setPointerCapture(event.pointerId);
  el.canvas.classList.add("panning");
});

el.canvas.addEventListener("pointermove", (event) => {
  if (!panning || panning.id !== event.pointerId) return;
  panning.moved = panning.moved || Math.abs(event.clientX - panning.x) + Math.abs(event.clientY - panning.y) > 2;
  state.view.x += event.clientX - panning.x;
  state.view.y += event.clientY - panning.y;
  panning.x = event.clientX;
  panning.y = event.clientY;
  applyView();
});

const endPan = (event) => {
  if (!panning || panning.id !== event.pointerId) return;
  // A press on empty canvas that never moved is a click, and a click on the
  // background clears the selection rather than panning.
  const dragged = panning.moved;
  panning = null;
  el.canvas.classList.remove("panning");
  if (!dragged && state.selected) select(null);
};
el.canvas.addEventListener("pointerup", endPan);
el.canvas.addEventListener("pointercancel", endPan);

document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== el.search) {
    event.preventDefault();
    el.search.focus();
    el.search.select();
    return;
  }
  if (event.key === "Escape") {
    if (state.query) {
      state.query = "";
      el.search.value = "";
    }
    select(null);
  }
});

/* ---------- theme ---------- */

function readTheme() {
  try {
    return localStorage.getItem("memseek-catalog-graph-theme");
  } catch (error) {
    return null;
  }
}

function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  const dark =
    document.documentElement.getAttribute("data-theme") === "dark" ||
    (!document.documentElement.getAttribute("data-theme") &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  el.theme.textContent = dark ? "Light" : "Dark";
  el.theme.setAttribute("title", dark ? "Switch to the light palette" : "Switch to the dark palette");
}

el.theme.addEventListener("click", () => {
  const dark = el.theme.textContent === "Light";
  const next = dark ? "light" : "dark";
  try {
    localStorage.setItem("memseek-catalog-graph-theme", next);
  } catch (error) {
    /* A viewer that blocks site data still gets the switch for this visit. */
  }
  applyTheme(next);
});

/* ---------- start ---------- */

applyTheme(readTheme());
renderChips();
render();
renderPanel();
openingView();

let resizeHandle = 0;
window.addEventListener("resize", () => {
  window.clearTimeout(resizeHandle);
  resizeHandle = window.setTimeout(openingView, 120);
});
</script>
"""
