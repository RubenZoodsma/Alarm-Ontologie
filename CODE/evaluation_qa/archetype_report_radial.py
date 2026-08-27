"""
archetype_report_radial.py — exploratory variant of archetype_report.py that
swaps the Mermaid `graph LR` flowchart for a radial layout, so the two can be
compared side by side before deciding whether to carry either one into the
GitHub Pages "Knowledge Graphs" view.

Reuses archetype_report.py's data pipeline as-is (load_reasoned_graph,
walk_archetype, dedupe_properties, classify, label_of, most_specific_types,
GROUPS) so the two reports can never disagree about which facts an archetype
entails — only about how they're drawn. The one thing this file does
differently is turn the reachable graph into Cytoscape.js elements instead of
a Mermaid string, ringed by hop-distance from the archetype's own AlarmMessage
node (the actual hub the technical and clinical chains converge on) rather
than columned by rank: "distance from the hub = derivation depth" — the same
thing Mermaid's LR layout encodes as column position — is spent across both
dimensions instead of stacked along one axis. Each ring's angular order is a
one-pass barycentre placement (see to_cytoscape), computed in Python and
handed to Cytoscape as fixed (x, y) positions via a `preset` layout, so a
node lands close to the neighbours it's actually connected to instead of
wherever a generic ring layout happens to put it.

Output
------
  archetype_report_radial.html   same Next/Prev/dropdown/hide-type-edges UI
                                  as archetype_report.py, radial diagram in
                                  place of the flowchart, for side-by-side
                                  comparison

Usage
-----
  python3 archetype_report_radial.py             build, then open in browser
  python3 archetype_report_radial.py --no-open    build only
"""

import json
import math
import sys
import webbrowser
from pathlib import Path

from rdflib import URIRef, BNode, Literal
from rdflib.namespace import RDF

sys.path.append(str(Path(__file__).resolve().parent))
from archetype_report import (
    ROOT, MDA, GROUPS,
    load_reasoned_graph, classify, label_of, most_specific_types,
    walk_archetype, dedupe_properties, local,
)

sys.path.append(str(Path(__file__).resolve().parent.parent / "shared"))
from graph_view import legend_html

OUT = ROOT / "EVALUATION" / "FRAMEWORK_QA" / "archetype_report_radial.html"


def to_cytoscape(triples: list, center, g) -> dict:
    """
    `triples` (already deduped, object-valued only) as Cytoscape elements.

    Every node is tagged with its hop-distance from `center` in `data.level`
    — the layout hub, not necessarily the archetype itself (build_report_radial
    passes the archetype's own AlarmMessage node: it's the one thing directly
    tied to the alarm's identity, the device chain AND the clinical chain, so
    centring on it puts the actual hub of the graph in the middle instead of
    the archetype bookkeeping node, which is one hop further out on its own
    branch). Hop-distance is computed over the UNDIRECTED graph — walk_archetype
    only ever walks forward from the archetype, so a center other than the
    archetype has no all-forward path to nodes "upstream" of it; treating
    edges as traversable either way for this distance-only purpose is what
    makes an arbitrary node a valid hub. BFS keeps a `seen` set, so a cycle
    introduced by the reasoner (e.g. materialising both directions of an
    owl:inverseOf pair) can't loop forever — same guard walk_archetype uses.
    """
    node_id, nodes, edges = {}, [], []

    def nid(term):
        s = str(term)
        if s not in node_id:
            node_id[s] = f"n{len(node_id)}"
        return node_id[s]

    adjacency = {}
    all_terms = {str(center): center}
    for s, p, o in triples:
        adjacency.setdefault(str(s), []).append(str(o))
        adjacency.setdefault(str(o), []).append(str(s))
        all_terms[str(s)] = s
        all_terms[str(o)] = o

    level, seen, frontier, depth = {}, set(), {str(center)}, 0
    while frontier:
        for n in frontier:
            level[n] = depth
        seen |= frontier
        frontier = {nb for n in frontier for nb in adjacency.get(n, []) if nb not in seen}
        depth += 1

    # Concentric/breadthfirst-circle both pick each ring's angular ORDER
    # themselves, with no regard for where a node's edges actually go — a
    # node can land on the opposite side of the circle from the neighbour it
    # connects to, one ring in, producing an edge that crosses the whole
    # diagram. Fixed here instead of left to Cytoscape: each ring is ordered
    # by the barycentre (circular mean, so wraparound near 0/2π doesn't
    # average toward the wrong side) of its already-placed parent-ring
    # neighbours' angles — the same idea Sugiyama layered-graph drawers use
    # to cut crossings, just one top-down pass (not the full iterative
    # median sweep) applied to rings instead of layers. A node's own final
    # (x, y) is then computed directly in Python and handed to Cytoscape as
    # a fixed position (`layout: preset` in the page script), rather than
    # trusting a generic layout's internal ordering to preserve this.
    by_level = {}
    for s_str in all_terms:
        by_level.setdefault(level.get(s_str, 0), []).append(s_str)

    angle = {str(center): 0.0}
    for lvl in sorted(l for l in by_level if l > 0):
        ring = by_level[lvl]

        def barycenter(n):
            parents = [nb for nb in adjacency.get(n, []) if level.get(nb) == lvl - 1]
            if not parents:
                return 0.0
            sin_sum = sum(math.sin(angle[p]) for p in parents)
            cos_sum = sum(math.cos(angle[p]) for p in parents)
            return math.atan2(sin_sum, cos_sum)

        ring.sort(key=lambda n: (barycenter(n), classify(all_terms[n], g), label_of(all_terms[n], g)))
        for i, n in enumerate(ring):
            angle[n] = 2 * math.pi * i / len(ring)

    RING_GAP = 130
    for s_str, term in all_terms.items():
        lvl = level.get(s_str, 0)
        r = lvl * RING_GAP
        a = angle.get(s_str, 0.0)
        nodes.append({"data": {
            "id": nid(term),
            "label": label_of(term, g),
            "group": classify(term, g),
            "level": lvl,
            "is_root": term == center,
        }, "position": {"x": r * math.cos(a), "y": r * math.sin(a)}})
    for s, p, o in triples:
        edges.append({"data": {"source": nid(s), "target": nid(o), "label": local(p)}})

    return {"nodes": nodes, "edges": edges, "root": nid(center)}


def cytoscape_group_styles(kinds: dict) -> list:
    """`kinds`' mermaid `"fill:#..,stroke:#.."` styles, parsed for Cytoscape's
    JS-object style API instead of a CSS classDef string."""
    styles = []
    for key, (name, style) in kinds.items():
        parts = dict(p.split(":", 1) for p in style.split(",") if ":" in p)
        styles.append({"key": key, "name": name,
                        "fill": parts.get("fill", "#eeeeee"),
                        "stroke": parts.get("stroke", "#333333")})
    return styles


def build_report_radial(g, root: URIRef) -> dict:
    triples = walk_archetype(g, root)
    deduped = dedupe_properties(triples, g)

    fact_rows = []
    for s, p, o in sorted(triples, key=lambda t: (classify(t[0], g), label_of(t[0], g), local(t[1]))):
        grp = classify(s, g)
        value = str(o) if isinstance(o, Literal) else label_of(o, g)
        specific = most_specific_types(s, g)
        subject_cls = local(max(specific, key=lambda t: len(str(t)))) if specific else "—"
        fact_rows.append({"group": grp, "subject": label_of(s, g), "subject_cls": subject_cls,
                           "prop": local(p), "value": value,
                           "value_group": None if isinstance(o, Literal) else classify(o, g)})

    diagram = [(s, p, o) for s, p, o in deduped if isinstance(o, (URIRef, BNode))]
    diagram_notype = [(s, p, o) for s, p, o in diagram if p != RDF.type]

    # The archetype's own AlarmMessage particular is one hop off the
    # archetype, not the archetype itself — but it's the node the archetype,
    # the device/technical chain and (via the message text) the clinical
    # chain all actually converge on, so it's the better layout hub. Falls
    # back to the archetype if a report ever lacks one (shouldn't happen).
    message = next((s for s, p, o in triples if p == RDF.type and local(o) == "AlarmMessage"), root)

    cyto = to_cytoscape(diagram, message, g)
    cyto_notype = to_cytoscape(diagram_notype, message, g)

    label = next(g.objects(root, MDA.hasLabel), Literal(local(root)))
    priority = next(g.objects(root, MDA.hasPriority), None)
    category = next(g.objects(root, MDA.hasCategory), None)
    return {
        "label": str(label),
        "priority": local(priority) if priority else "—",
        "category": local(category) if category else "—",
        "facts": fact_rows,
        "n_facts": len(fact_rows),
        "cyto": cyto,
        "cyto_notype": cyto_notype,
        "n_nodes": len(cyto["nodes"]),
        "n_edges": len(cyto["edges"]),
    }


# ── Page ──────────────────────────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archetype knowledge report — radial</title>
<style>
  :root{--ground:#f4f7f7;--surface:#fff;--ink:#16231f;--muted:#61726d;--faint:#89a09a;
        --line:#e2ebe8;--accent:#0c7d76;--accent-soft:#e4f2f0;--shadow:0 1px 2px rgba(0,0,0,.05)}
  @media (prefers-color-scheme:dark){:root{--ground:#0d1412;--surface:#15201d;--ink:#e9f1ee;
        --muted:#93a8a2;--faint:#6b807a;--line:#23322e;--accent:#37b9ae;--accent-soft:#143430;--shadow:none}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);line-height:1.5;
       font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:1200px;margin:0 auto;padding:clamp(1.2rem,3vw,2.2rem) clamp(1rem,3vw,2rem) 4rem}
  .eyebrow{font-family:ui-monospace,Menlo,monospace;font-size:.7rem;letter-spacing:.16em;
           text-transform:uppercase;color:var(--accent);font-weight:600}
  h1{margin:.3rem 0 1rem;font-size:clamp(1.3rem,2.6vw,1.9rem);line-height:1.2;
     letter-spacing:-.02em;font-weight:680}
  .bar{display:flex;flex-wrap:wrap;gap:.7rem;align-items:center;background:var(--surface);
       border:1px solid var(--line);border-radius:12px;padding:.7rem .9rem;box-shadow:var(--shadow)}
  label.f{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--faint);
          font-weight:600;margin-right:.35rem}
  select,button{font:inherit;color:var(--ink);background:var(--surface);
       border:1px solid var(--line);border-radius:8px;padding:.4rem .7rem;cursor:pointer}
  select:hover,button:hover{border-color:var(--accent)}
  button:disabled{opacity:.35;cursor:not-allowed;border-color:var(--line)}
  .spacer{flex:1}
  .kv{display:inline-flex;gap:.3rem;font-size:.8rem;color:var(--muted)}
  .kv b{color:var(--ink)}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;
         padding:.9rem 1rem;box-shadow:var(--shadow);margin-top:1rem}
  h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.11em;color:var(--faint);
     font-weight:600;margin:0 0 .55rem}
  .figure{background:#fff;border:1px solid var(--line);border-radius:12px;padding:.5rem}
  #diagram{width:100%;height:600px}
  .figurehint{font-size:.72rem;color:var(--faint);margin:.5rem 0 0;font-family:ui-monospace,Menlo,monospace}
  .legend{display:flex;flex-wrap:wrap;gap:.35rem .8rem;margin:.7rem 0 0;padding:0;list-style:none}
  .legend li{display:flex;align-items:center;gap:.35rem;font-size:.75rem;color:var(--muted)}
  .dot{width:11px;height:11px;border-radius:3px;border:1px solid;flex:none}
  table{width:100%;border-collapse:collapse;font-size:.85rem}
  th{text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;
     color:var(--faint);padding:.3rem .5rem;border-bottom:1px solid var(--line)}
  td{padding:.35rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}
  tr.group-identity td:first-child{border-left:3px solid #e67e22}
  tr.group-technical td:first-child{border-left:3px solid #2980b9}
  tr.group-clinical td:first-child{border-left:3px solid #8e44ad}
  tr.group-therapeutic td:first-child{border-left:3px solid #27ae60}
  tr.group-other td:first-child{border-left:3px solid #7f8c8d}
  .pill{font-size:.68rem;padding:.08rem .45rem;border-radius:99px;background:var(--accent-soft);
        color:var(--accent);font-weight:600}
  .count{font-size:.72rem;color:var(--faint)}
</style></head><body><div class="wrap">
  <span class="eyebrow">Archetype knowledge report · radial · __N__ alarm type(s)</span>
  <h1 id="title">—</h1>

  <div class="bar">
    <span><label class="f">Jump to</label><select id="pick"></select></span>
    <span class="spacer"></span>
    <button id="prev" title="Previous (←)">◀ Prev</button>
    <span class="kv" id="pos">– / –</span>
    <button id="next" title="Next (→)">Next ▶</button>
  </div>

  <div class="panel">
    <span class="kv"><b id="cat">—</b></span>&nbsp;·&nbsp;
    <span class="kv">priority <b id="prio">—</b></span>&nbsp;·&nbsp;
    <span class="count" id="fcount"></span>
  </div>

  <div class="panel">
    <h2>Entailed structure — radial</h2>
    <label style="display:inline-flex;align-items:center;gap:.4rem;font-size:.82rem;color:var(--muted);margin-bottom:.6rem">
      <input type="checkbox" id="hideType" checked> Hide <code>type</code> edges (identity is already shown as "Some &lt;type&gt;" on each node)
    </label>
    <div class="figure"><div id="diagram"></div></div>
    <p class="figurehint">scroll to zoom · drag background to pan · drag a node to reposition it</p>
    <ul class="legend">__LEGEND__</ul>
  </div>

  <div class="panel">
    <h2>Every entailed fact</h2>
    <table>
      <thead><tr><th>Node</th><th>Relation</th><th>Value</th></tr></thead>
      <tbody id="facts"></tbody>
    </table>
  </div>
</div>
<script>const DATA = __DATA__, GROUP_STYLES = __GROUP_STYLES__;</script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape@3/dist/cytoscape.min.js"></script>
<script>
const $ = id => document.getElementById(id);
let idx = 0, cy = null;

function buildPicker(){
  $('pick').innerHTML = DATA.map((a,i) => `<option value="${i}">${a.label}</option>`).join('');
}

const BASE_STYLE = [
  { selector: 'node', style: {
      'label': 'data(label)', 'font-size': 13, 'color': '#16231f',
      'text-wrap': 'wrap', 'text-max-width': '100px',
      'width': 24, 'height': 24, 'border-width': 1.5,
      'text-valign': 'bottom', 'text-margin-y': 4 } },
  { selector: 'node[?is_root]', style: {
      'width': 34, 'height': 34, 'border-width': 3, 'font-weight': 'bold', 'font-size': 15 } },
  { selector: 'edge', style: {
      'width': 1.2, 'line-color': '#b7c4c0', 'target-arrow-color': '#b7c4c0',
      'target-arrow-shape': 'triangle', 'curve-style': 'bezier',
      'arrow-scale': 0.8, 'label': 'data(label)', 'font-size': 10,
      'color': '#61726d', 'text-background-color': '#ffffff',
      'text-background-opacity': 0.85, 'text-background-padding': 1 } },
];
const GROUP_STYLE = GROUP_STYLES.map(g => ({
  selector: `node[group = "${g.key}"]`,
  style: { 'background-color': g.fill, 'border-color': g.stroke } }));
const STYLE = BASE_STYLE.concat(GROUP_STYLE);

function draw(){
  const a = DATA[idx];
  $('title').textContent = a.label;
  $('cat').textContent = a.category;
  $('prio').textContent = a.priority;
  $('fcount').textContent = a.n_facts + ' fact(s), ' + a.n_nodes + ' node(s), ' + a.n_edges +
    ' edge(s) — every entailed triple reachable from this archetype';
  $('pick').value = idx;
  $('pos').innerHTML = '<b>' + (idx+1) + '</b> / ' + DATA.length;
  $('prev').disabled = idx === 0;
  $('next').disabled = idx === DATA.length - 1;
  $('facts').innerHTML = a.facts.map(f =>
    `<tr class="group-${f.group}"><td>${f.subject} <span class="pill">${f.subject_cls}</span></td>` +
    `<td>${f.prop}</td><td>${f.value}</td></tr>`).join('');

  const cyto = $('hideType').checked ? a.cyto_notype : a.cyto;
  if (cy) cy.destroy();
  cy = cytoscape({
    container: $('diagram'),
    elements: cyto.nodes.concat(cyto.edges),
    style: STYLE,
    layout: { name: 'preset', fit: true, padding: 30 },
    wheelSensitivity: 0.25,
  });
}

function go(i){ idx = Math.max(0, Math.min(DATA.length - 1, i)); draw(); }
$('pick').onchange = e => go(+e.target.value);
$('prev').onclick = () => go(idx - 1);
$('next').onclick = () => go(idx + 1);
$('hideType').onchange = draw;
addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft')  go(idx - 1);
  if (e.key === 'ArrowRight') go(idx + 1);
});

buildPicker(); draw();
</script>
</body></html>"""


def main() -> None:
    g = load_reasoned_graph()
    archetypes = sorted(
        ((s, str(lbl)) for s, lbl in g.subject_objects(MDA.hasLabel)
         if (s, RDF.type, MDA.AlarmType) in g),
        key=lambda t: t[1],
    )
    print(f"[reason] {len(g)} triples after OWL-RL closure; {len(archetypes)} archetype(s)")

    data = []
    for root, label in archetypes:
        report = build_report_radial(g, root)
        data.append(report)
        print(f"  {label:<45} {report['n_nodes']:>3} node(s)  {report['n_edges']:>3} edge(s)  "
              f"{report['n_facts']:>4} fact(s)")

    page = (_PAGE
            .replace("__DATA__", json.dumps(data, separators=(",", ":")))
            .replace("__GROUP_STYLES__", json.dumps(cytoscape_group_styles(GROUPS), separators=(",", ":")))
            .replace("__LEGEND__", legend_html(GROUPS))
            .replace("__N__", str(len(data))))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")

    size_kb = len(page) // 1024
    print(f"\n[output] {OUT.name}  ({size_kb} KB, self-contained)")

    if "--no-open" in sys.argv:
        print(f"[open]   {OUT}")
    else:
        webbrowser.open(OUT.as_uri())
        print(f"[open]   launched in your browser — {OUT}")


if __name__ == "__main__":
    main()
