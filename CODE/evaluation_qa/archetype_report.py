"""
archetype_report.py — a complete-knowledge validation view of one
mda:AlarmType archetype at a time, for expert sign-off. Also the site's
published "Knowledge Graphs" page (docs/graph/) — the same reasoning-derived
view a reviewer signs off on IS what a visitor sees, not a separate
simplified copy.

Different question from everything else in this pipeline. build_framework.py
builds the blueprint; find_duplicate_archetypes.py compares archetypes
against each other; op_visual.py shows a live per-patient timeline. None of
them answer "show me literally everything this ONE alarm type entails, so a
reviewer can check it against what they expect" — that needs full reasoning
(not the blueprint's raw assertions) and full reachability (not the
single-parent tree find_duplicate_archetypes.py's method or
ontology_tree.derive_class_tree would give — mda:PhysiologicalProcess has
two legitimate incoming edges, isPropertyOf and targetsProcess, and a
single-spanning-tree walk can only keep one; a completeness/sign-off tool
cannot silently drop the other).

Method
------
Load ontology.ttl + vocab_base.ttl + vocab_generated.ttl + inference.ttl +
kg_generated.ttl, materialise the OWL-RL closure once (real reasoner,
owlrl), then for each mda:AlarmType walk every object-property edge
reachable from its own IRI, with no pruning — same shape as
op_knowledge.py's _extract_all, extended to also capture
literal-valued facts (mda:hasLabel, skos:prefLabel/notation/definition) for
readability, since a sign-off document needs the actual human-readable label,
not just what its IRI happens to be. rdf:type's object is shown but not a
traversal doorway (same reason _extract_all keeps it that way): OWL-RL's
cls-hv1 rule already entails a bridge axiom's consequence directly onto the
grounded instance (e.g. the archetype's own Metric node gets
mda:approximates asserted on IT, not only on the metric: concept it's typed
to), so walking further into the concept itself would mostly surface
vocabulary-scheme bookkeeping shared across every archetype using that
concept, not anything specific to validate for this one.

Every reachable node is coloured by which of four groups it belongs to —
identity (the alarm's own bookkeeping), technical (the device/functional-
unit/sensor/signal/analysis/metric chain), clinical (physiological property/
process/organ/organ system), therapeutic (the modality a device
administers) — both in the diagram and in the fact table beneath it, so a
reviewer can see at a glance which parts of what's asserted are "what the
device measured" versus "what that implies clinically" versus "what therapy
this relates to".

Output
------
  docs/graph/index.html   one archetype at a time, Next/Prev + a jump-to
                          dropdown, every tick's data baked in (same
                          self-contained, no-server pattern as op_visual.py)

Usage
-----
  python3 archetype_report.py             build, then open in the browser
  python3 archetype_report.py --no-open   build only (for scripted runs)
"""

import json
import re
import sys
import webbrowser
from pathlib import Path

import owlrl
from rdflib import Graph, Namespace, URIRef, BNode, Literal
from rdflib.namespace import RDF, RDFS, OWL, SKOS

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.append(str(SCRIPT_DIR.parent / "shared"))
from graph_view import to_mermaid, legend_html

ROOT = SCRIPT_DIR.parent.parent   # CODE/evaluation_qa -> CODE -> repo root
FRAMEWORK_DIR = ROOT / "FRAMEWORK"
ONTOLOGY   = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB_BASE = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "vocab_base.ttl"
VOCAB      = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
INFERENCE  = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "inference.ttl"
CATALOGUE  = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
# Published straight to the site's own Knowledge Graphs page, not a separate
# QA-only copy — this report IS that page now, not a staging draft of it.
OUT        = ROOT / "docs" / "graph" / "index.html"

MDA = Namespace("https://w3id.org/mda/ontology#")

# Predicates that are pure taxonomy/bookkeeping, not knowledge about an
# alarm — walking into them would pull in a whole vocabulary scheme (every
# sibling/broader concept) rather than anything specific to this archetype.
EXCLUDED_PREDICATES = {SKOS.broader, RDFS.subClassOf, SKOS.inScheme, OWL.sameAs,
                        MDA.nodeKind, MDA.instantiatesClass, MDA.valuesFromScheme,
                        MDA.refinesUniversalIdentity, MDA.refinesParticularIdentity,
                        MDA.situational, MDA.persistsPostAlarm, MDA.targetType}

# Every /vocab/<namespace>/ this ontology mints, mapped to which knowledge
# group it belongs to. A namespace absent from here (should not happen for
# any concept an archetype actually reaches) falls back to "other".
NAMESPACE_GROUP = {
    "alarm-priority": "identity", "alarm-category": "identity", "patient": "identity",
    "device": "technical", "device-type": "technical", "manufacturer": "technical",
    "component": "technical", "functional-unit": "technical", "sensor": "technical",
    "signal": "technical", "signal-analysis": "technical", "metric": "technical",
    "metric-phase": "technical", "metric-rate": "technical", "rhythm": "technical",
    "quality-state": "technical", "operation-state": "technical",
    "anatomical-position": "technical", "laterality": "technical",
    "physiological-property": "clinical", "physiological-process": "clinical",
    "organ": "clinical", "organ-system": "clinical",
    "therapeutic-modality": "therapeutic",
}

# For blank/structural nodes (no /vocab/ IRI of their own — e.g. the
# archetype's own AlarmMessage node), classify by rdf:type's local name
# instead.
CLASS_GROUP = {
    "AlarmType": "identity", "AlarmMessage": "identity", "Patient": "identity", "Alarm": "identity",
    "Device": "technical", "Component": "technical", "FunctionalUnit": "technical",
    "Sensor": "technical", "Signal": "technical", "SignalAnalysis": "technical", "Metric": "technical",
    "PhysiologicalProperty": "clinical", "PhysiologicalProcess": "clinical",
    "Organ": "clinical", "OrganSystem": "clinical",
    "TherapeuticModality": "therapeutic",
}

GROUPS = {
    "identity":    ("Identity",    "fill:#ffe8cc,stroke:#e67e22"),
    "technical":   ("Technical",   "fill:#d6eaff,stroke:#2980b9"),
    "clinical":    ("Clinical",    "fill:#eaddf7,stroke:#8e44ad"),
    "therapeutic": ("Therapeutic", "fill:#d5f5e3,stroke:#27ae60"),
    "other":       ("Other",       "fill:#ececec,stroke:#7f8c8d"),
}

# Prose for the page's "How to read this page" panel — one line per GROUPS
# entry, kept as a separate table (not folded into GROUPS itself) so the
# short legend label and the longer explanatory sentence can each be edited
# without touching the other.
GROUP_DESCRIPTIONS = {
    "identity": "the alarm's own bookkeeping — its type, its message text, and the patient and "
                "alarm event it concerns.",
    "technical": "the physical chain that produces the alarm — device, functional unit, sensor, "
                 "signal, analysis, and the metric condition that triggers it.",
    "clinical": "the physiological meaning — what property, process, organ or organ system the "
                "technical chain says something about.",
    "therapeutic": "a therapy or treatment modality the alarm relates to, where applicable.",
    "other": "anything not covered by the four groups above.",
}


def group_help_html(kinds: dict = GROUPS, descriptions: dict = GROUP_DESCRIPTIONS) -> str:
    """<dt>/<dd> pairs for the help panel — same dot-from-style trick as
    legend_html, so the colour swatch here can never drift from the one the
    diagram and its legend actually use."""
    return "".join(
        f'<dt><span class="dot" style="{style.replace("stroke:", "border-color:")}"></span>{name}</dt>'
        f'<dd>{descriptions.get(key, "")}</dd>'
        for key, (name, style) in kinds.items()
    )


def local(iri) -> str:
    return re.split(r"[#/]", str(iri))[-1]


def load_reasoned_graph() -> Graph:
    g = Graph()
    for f in (ONTOLOGY, VOCAB_BASE, VOCAB, INFERENCE, CATALOGUE):
        g.parse(f, format="turtle")
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
    return g


def classify(node, g: Graph) -> str:
    s = str(node)
    if "/vocab/" in s:
        ns = s.split("/vocab/", 1)[1].split("/")[0]
        if ns in NAMESPACE_GROUP:
            return NAMESPACE_GROUP[ns]
    for t in g.objects(node, RDF.type):
        cls_local = local(t)
        if cls_local in CLASS_GROUP:
            return CLASS_GROUP[cls_local]
    return "other"


def most_specific_types(node, g: Graph) -> list:
    """
    `node`'s rdf:type values, minus the two universally-true ones
    (owl:Thing, skos:Concept — every resource/concept has them, so they
    carry no discriminating information) and minus any type that is itself
    a strict superclass (via the reasoned rdfs:subClassOf closure) of
    another type already in the set — e.g. drop mda:Component when
    mda:Sensor (rdfs:subClassOf mda:Component) is also present. Used for
    picking a representative label for a blank node, never for deciding
    which facts the fact table shows — that stays maximally complete.

    Also excludes any BNode-typed class: OWL-RL's cls-int rules entail
    membership in the ANONYMOUS intersection class an axiom like "Pulse ⊓
    hasRate=Absent" itself is (inference.ttl's Signal quality axioms), which
    is real entailment but has no human-nameable identity — useless, not
    "most specific", for labeling purposes.
    """
    types = [t for t in g.objects(node, RDF.type)
             if isinstance(t, URIRef) and t not in (OWL.Thing, SKOS.Concept)]
    return [t for t in types
            if not any(t != u and (u, RDFS.subClassOf, t) in g for u in types)]


def dedupe_properties(triples: set, g: Graph) -> set:
    """
    Drop a (s, p, o) triple when a MORE SPECIFIC property (q rdfs:subPropertyOf
    p) also relates the exact same (s, o) pair — e.g. a component's own
    hasComponentOperationState=Disconnected entails the general
    hasOperationState=Disconnected on that SAME component via subPropertyOf
    (rdfs7); showing both restates one fact at two levels of specificity.
    Diagram-only (see build_report): the fact table stays maximally
    complete, same call as most_specific_types for redundant rdf:type — this
    tool doesn't get to unilaterally decide a fact isn't worth a reviewer's
    attention, only that its DIAGRAM shouldn't draw it twice.

    Scoped strictly to the same (s, o) pair, so it never touches a genuinely
    different fact — e.g. a device's OWN hasDeviceOperationState, entailed
    onto the DEVICE by inference.ttl's targeted component-propagation
    axioms, has a different subject than the component's
    hasComponentOperationState and is therefore untouched here.
    """
    by_pair = {}
    for s, p, o in triples:
        by_pair.setdefault((s, o), []).append(p)
    redundant = set()
    for (s, o), props in by_pair.items():
        if len(props) < 2:
            continue
        for p in props:
            if any(p != q and (q, RDFS.subPropertyOf, p) in g for q in props):
                redundant.add((s, p, o))
    return {t for t in triples if t not in redundant}


def label_of(node, g: Graph) -> str:
    lbl = next(g.objects(node, SKOS.prefLabel), None)
    if lbl is not None and lbl.language in (None, "en"):
        return str(lbl)
    lbl = next(g.objects(node, MDA.hasLabel), None)
    if lbl is not None:
        return str(lbl)
    if isinstance(node, BNode):
        # A blank node is the archetype's own MINTED particular — an
        # existentially-quantified individual of its type, not the type
        # itself — so it is prefixed "Some ", the standard DL/Manchester-
        # syntax reading for exactly this ("some C" = an unspecified
        # instance of C), rather than showing identical text to the
        # concept it's typed to (label_of recurses into a real vocab
        # concept below, which never re-enters this branch, so this never
        # double-prefixes). Falls back to the most specific concept or
        # class it's typed to, e.g. "Some Physiological Monitor_Philips"
        # rather than a skolemised blank-node id.
        candidates = most_specific_types(node, g)
        vocab = [t for t in candidates if "/vocab/" in str(t)]
        if vocab:
            return f"Some {label_of(max(vocab, key=lambda t: len(str(t))), g)}"
        if candidates:
            return f"Some {local(max(candidates, key=lambda t: len(str(t))))}"
    return local(node)


def walk_archetype(g: Graph, root: URIRef) -> set:
    """
    Every triple reachable from `root`, walking every owl:ObjectProperty edge
    with no pruning (full multi-parent reachability, not a single spanning
    tree — see module docstring), plus literal-valued facts on visited nodes.
    """
    object_properties = set(g.subjects(RDF.type, OWL.ObjectProperty))
    triples = set()
    seen, frontier = set(), {root}
    while frontier:
        nxt = set()
        for node in frontier - seen:
            seen.add(node)
            for p, o in g.predicate_objects(node):
                if p in EXCLUDED_PREDICATES:
                    continue
                if p == RDF.type:
                    # owl:Thing/skos:Concept are true of literally every
                    # resource/concept in this graph — zero discriminating
                    # information, not a fact worth a reviewer's attention.
                    # Every OTHER rdf:type, including a redundant-looking
                    # entailed supertype (Sensor is also a Component), is
                    # kept: that redundancy is itself something a reviewer
                    # may want to confirm, not this tool's call to hide.
                    if isinstance(o, URIRef) and o not in (OWL.Thing, SKOS.Concept):
                        triples.add((node, p, o))   # shown, not a doorway — see docstring
                    continue
                if isinstance(o, Literal):
                    if p in (SKOS.prefLabel, SKOS.definition, SKOS.notation) and o.language not in (None, "en"):
                        continue          # keep one language, not every skos:prefLabel duplicate
                    triples.add((node, p, o))
                    continue
                if p not in object_properties:
                    continue
                triples.add((node, p, o))
                nxt.add(o)
        frontier = nxt
    return triples


def build_report(g: Graph, root: URIRef) -> dict:
    triples = walk_archetype(g, root)

    fact_rows = []
    for s, p, o in sorted(triples, key=lambda t: (classify(t[0], g), label_of(t[0], g), local(t[1]))):
        grp = classify(s, g)
        value = str(o) if isinstance(o, Literal) else label_of(o, g)
        specific = most_specific_types(s, g)
        subject_cls = local(max(specific, key=lambda t: len(str(t)))) if specific else "—"
        fact_rows.append({"group": grp, "subject": label_of(s, g), "subject_cls": subject_cls,
                           "prop": local(p), "value": value, "value_group": None if isinstance(o, Literal) else classify(o, g)})

    # BNode objects (every blueprint particular — the structural backbone
    # linking the archetype to everything else) must be kept here, not just
    # URIRef ones: BNode is not a subclass of URIRef in rdflib, so filtering
    # on isinstance(o, URIRef) alone silently drops every blank-node-to-
    # blank-node edge, leaving only the leaf edges into shared vocab
    # concepts — a diagram of disconnected fragments.
    diagram_graph = Graph()
    for s, p, o in dedupe_properties(triples, g):
        if isinstance(o, (URIRef, BNode)):
            diagram_graph.add((s, p, o))

    mmd = to_mermaid(diagram_graph, classify=lambda n: classify(n, g), kinds=GROUPS,
                      label_of=lambda n: label_of(n, g))

    # A second variant with every rdf:type edge dropped, for the "hide type
    # edges" toggle (default on). Safe to just drop them, not merely hide
    # visually: since every blank particular is now labelled "Some <its
    # type>" (see label_of), the type edge was purely restating what the
    # node's own label already says — the fact table still carries every
    # rdf:type fact in full regardless of this toggle. A pure-supertype
    # concept reachable ONLY via a type edge (e.g. mda:Device once the
    # precoordinated device:PhysiologicalMonitor_Philips already names it)
    # simply drops out of this variant rather than sitting there
    # disconnected — that IS the requested decluttering, not a side effect.
    diagram_graph_notype = Graph()
    for s, p, o in diagram_graph:
        if p != RDF.type:
            diagram_graph_notype.add((s, p, o))
    mmd_notype = to_mermaid(diagram_graph_notype, classify=lambda n: classify(n, g), kinds=GROUPS,
                             label_of=lambda n: label_of(n, g))

    label = next(g.objects(root, MDA.hasLabel), Literal(local(root)))
    priority = next(g.objects(root, MDA.hasPriority), None)
    category = next(g.objects(root, MDA.hasCategory), None)
    return {
        "label": str(label),
        "priority": local(priority) if priority else "—",
        "mmd_notype": mmd_notype,
        "category": local(category) if category else "—",
        "facts": fact_rows,
        "mmd": mmd,
        "n_facts": len(fact_rows),
    }


# ── Page ──────────────────────────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archetype knowledge report</title>
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
  .figure{background:#fff;border:1px solid var(--line);border-radius:12px;
          padding:.5rem;overflow:auto;min-height:280px}
  .figure svg{max-width:none}
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
  .lede{margin:.3rem 0 1rem;color:var(--muted);max-width:74ch;font-size:.88rem}
  details.help{margin-top:1rem}
  details.help>summary{cursor:pointer;font-size:.72rem;text-transform:uppercase;letter-spacing:.11em;
       color:var(--faint);font-weight:600;padding:.2rem 0;list-style:revert}
  details.help[open]>summary{color:var(--accent);margin-bottom:.3rem}
  .help-body{font-size:.85rem}
  .help-body p{margin:.55rem 0}
  .help-groups{margin:.5rem 0;display:grid;grid-template-columns:auto 1fr;gap:.3rem .7rem;align-items:start}
  .help-groups dt{font-weight:600;white-space:nowrap;display:flex;align-items:center;gap:.4rem}
  .help-groups dd{margin:0;color:var(--muted)}
  .callout{background:var(--accent-soft);border:1px solid var(--accent);border-radius:8px;
           padding:.6rem .8rem;margin:.7rem 0;font-size:.85rem}
  .callout code,.help-body code{background:rgba(120,120,120,.16);padding:.05rem .3rem;border-radius:4px;
       font-family:ui-monospace,Menlo,monospace;font-size:.85em}
  #glossarySearch{width:100%;font:inherit;font-size:.85rem;padding:.45rem .7rem;
       border:1px solid var(--line);border-radius:8px;background:var(--ground);color:var(--ink)}
  #glossarySearch:focus{outline:none;border-color:var(--accent)}
  .glossary-list{list-style:none;margin:.6rem 0 0;padding:0;display:flex;flex-direction:column;
       gap:.4rem;max-height:420px;overflow:auto}
  .glossary-list li{padding:.4rem .6rem;border-left:3px solid var(--line);background:var(--ground);
       border-radius:0 6px 6px 0}
  .glossary-list .term{font-family:ui-monospace,Menlo,monospace;font-weight:600;font-size:.85rem}
  .glossary-list .kind{font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;
       color:var(--faint);margin-left:.5rem}
  .glossary-list .def{margin:.25rem 0 0;font-size:.82rem;color:var(--muted)}
  .glossary-empty{color:var(--faint);font-size:.83rem;font-style:italic;padding:.4rem 0}
</style></head><body><div class="wrap">
  <span class="eyebrow">Archetype knowledge report · __N__ alarm type(s)</span>
  <h1 id="title">—</h1>
  <p class="lede">One <code>mda:AlarmType</code> archetype at a time, with everything the reasoner
     derives from it — see "How to read this page" below before inspecting the diagram.</p>

  <details class="help" open>
    <summary>How to read this page</summary>
    <div class="help-body">
      <p>Each archetype below is everything that follows once the ontology, the controlled
      vocabularies and this alarm type's own definition are combined and a reasoner works out
      everything they logically imply — including facts nobody stated directly. Nothing here is
      simplified for readability: if something looks unexpected, it is exactly what the current
      knowledge base entails, and worth checking against what you would expect clinically.</p>

      <p><b>The diagram.</b> Each box is a thing the reasoning knows about; an arrow reads as
      "the box it starts from &lt;label&gt; the box it points to". Colour marks which of these
      categories a box belongs to:</p>
      <dl class="help-groups">__GROUP_HELP__</dl>

      <div class="callout">
        <b>Boxes labelled "Some &lt;Type&gt;"</b> (e.g. <code>Some Sensor</code>,
        <code>Some Alarm Message</code>) are placeholders, not fixed concepts — they mark a
        specific real thing this alarm type requires (a specific device, sensor, or message) that
        has no record yet. When an alarm of this type actually fires, each "Some &lt;Type&gt;"
        placeholder is where a real, identified instance gets minted and filled in:
        <code>Some Sensor</code> becomes the actual SpO2 sensor on the actual monitor that raised
        the actual alarm. A box <i>without</i> "Some" in front (e.g. a manufacturer, an anatomical
        organ) is an already-fixed vocabulary concept, reused as-is by every archetype that needs
        it — it is never minted, because it does not refer to one specific occurrence.
      </div>

      <p>The <b>"Hide <code>type</code> edges"</b> checkbox (on by default) hides the "is a"
      arrows into class names — each placeholder already states its type as "Some &lt;Type&gt;" on
      the box itself, so leaving those arrows in only repeats that information.</p>

      <p>The <b>fact table</b> at the bottom of the page lists these same facts as plain rows, plus
      a few the diagram cannot draw (text labels, definitions) — same colour-coding, for when the
      diagram gets too dense to read directly or you want to check it line by line.</p>
    </div>
  </details>

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
    <h2>Entailed structure</h2>
    <label style="display:inline-flex;align-items:center;gap:.4rem;font-size:.82rem;color:var(--muted);margin-bottom:.6rem">
      <input type="checkbox" id="hideType" checked> Hide <code>type</code> edges (identity is already shown as "Some &lt;type&gt;" on each node)
    </label>
    <div class="figure"><div id="diagram"></div></div>
    <ul class="legend">__LEGEND__</ul>
  </div>

  <div class="panel">
    <h2>Ontology glossary</h2>
    <p class="lede" style="margin:0 0 .6rem">
      What every box and arrow label above actually means — classes and properties from the base
      <b>ontology</b> itself, not the controlled vocabularies. Definitions are not stored in this
      page: they are fetched live from the published ontology
      (<a href="https://w3id.org/mda/ontology" target="_blank" rel="noopener">w3id.org/mda/ontology</a>),
      so this list always matches whatever is currently published, not a copy frozen at the time
      this report was generated.
    </p>
    <input type="search" id="glossarySearch" placeholder="Search a term or definition…" autocomplete="off">
    <div class="count" id="glossaryStatus" style="margin-top:.4rem"></div>
    <ul class="glossary-list" id="glossaryList"></ul>
  </div>

  <div class="panel">
    <h2>Every entailed fact</h2>
    <table>
      <thead><tr><th>Node</th><th>Relation</th><th>Value</th></tr></thead>
      <tbody id="facts"></tbody>
    </table>
  </div>
</div>
<script>const DATA = __DATA__;</script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({startOnLoad:false, theme:'neutral', securityLevel:'loose', flowchart:{useMaxWidth:false}});
const $ = id => document.getElementById(id);
let idx = 0, seq = 0;

function buildPicker(){
  $('pick').innerHTML = DATA.map((a,i) => `<option value="${i}">${a.label}</option>`).join('');
}

async function draw(){
  const a = DATA[idx];
  $('title').textContent = a.label;
  $('cat').textContent = a.category;
  $('prio').textContent = a.priority;
  $('fcount').textContent = a.n_facts + ' fact(s) — every entailed triple reachable from this archetype';
  $('pick').value = idx;
  $('pos').innerHTML = '<b>' + (idx+1) + '</b> / ' + DATA.length;
  $('prev').disabled = idx === 0;
  $('next').disabled = idx === DATA.length - 1;
  $('facts').innerHTML = a.facts.map(f =>
    `<tr class="group-${f.group}"><td>${f.subject} <span class="pill">${f.subject_cls}</span></td>` +
    `<td>${f.prop}</td><td>${f.value}</td></tr>`).join('');
  const mmd = $('hideType').checked ? a.mmd_notype : a.mmd;
  const {svg} = await mermaid.render('m' + (seq++), mmd);
  $('diagram').innerHTML = svg;
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

// ── Ontology glossary ──────────────────────────────────────────────────
// Fetched at load time, not baked into DATA: the w3id.org/mda/ontology
// redirect only maps the bare page (it turns any sub-path into a URL
// fragment, not a real path — https://w3id.org/mda/ontology/ontology.jsonld
// resolves right back to the HTML page), so this fetches the JSON-LD
// serialisation that GitHub Pages actually publishes next to it, at the
// same location the ontology's own persistent-URL page redirects to.
// GitHub Pages sends Access-Control-Allow-Origin: *, so this works even
// when this report is opened as a local file:// page, not served.
const ONTOLOGY_JSONLD = 'https://rubenzoodsma.github.io/Alarm-Ontologie/ontology/ontology.jsonld';
const ONTOLOGY_NS = 'https://w3id.org/mda/ontology#';
const KIND_LABEL = {
  'http://www.w3.org/2002/07/owl#Class':             'Class',
  'http://www.w3.org/2002/07/owl#ObjectProperty':     'Object property',
  'http://www.w3.org/2002/07/owl#DatatypeProperty':   'Data property',
  'http://www.w3.org/2002/07/owl#AnnotationProperty': 'Annotation property',
  'http://www.w3.org/2002/07/owl#NamedIndividual':    'Individual',
};
const KIND_PRIORITY = Object.keys(KIND_LABEL);
let GLOSSARY = [];

function esc(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function pickValue(values, lang){
  if (!values) return '';
  const hit = values.find(v => v['@language'] === lang) || values.find(v => !v['@language']);
  return hit ? hit['@value'] : '';
}

async function loadGlossary(){
  const status = $('glossaryStatus');
  status.textContent = 'Loading definitions from w3id.org/mda/ontology…';
  try {
    const res = await fetch(ONTOLOGY_JSONLD);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const nodes = await res.json();
    GLOSSARY = nodes
      .filter(n => typeof n['@id'] === 'string' && n['@id'].startsWith(ONTOLOGY_NS))
      .map(n => {
        const kindKey = KIND_PRIORITY.find(k => (n['@type'] || []).includes(k));
        if (!kindKey) return null;   // skip owl:Ontology itself and blank-node class expressions
        return {
          term: n['@id'].slice(ONTOLOGY_NS.length),
          kind: KIND_LABEL[kindKey],
          label: pickValue(n['http://www.w3.org/2000/01/rdf-schema#label'], 'en'),
          def: pickValue(n['http://www.w3.org/2000/01/rdf-schema#comment'], 'en'),
        };
      })
      .filter(Boolean)
      .sort((a, b) => a.term.localeCompare(b.term));
    status.textContent = GLOSSARY.length + ' term(s), fetched from the published ontology.';
    renderGlossary();
  } catch (e) {
    status.textContent = 'Could not load definitions from w3id.org/mda/ontology — ' +
      'check your connection, or open it directly: w3id.org/mda/ontology';
  }
}

function renderGlossary(){
  const q = ($('glossarySearch').value || '').trim().toLowerCase();
  const hits = !q ? GLOSSARY : GLOSSARY.filter(g =>
    g.term.toLowerCase().includes(q) || g.label.toLowerCase().includes(q) || g.def.toLowerCase().includes(q));
  $('glossaryList').innerHTML = hits.length
    ? hits.map(g => `<li><span class="term">mda:${esc(g.term)}</span>` +
        `<span class="kind">${esc(g.kind)}</span>` +
        (g.def ? `<p class="def">${esc(g.def)}</p>` : '') + `</li>`).join('')
    : '<li class="glossary-empty">no matching term</li>';
}

$('glossarySearch').oninput = renderGlossary;

buildPicker(); draw(); loadGlossary();
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
        report = build_report(g, root)
        data.append(report)
        print(f"  {label:<45} {report['n_facts']:>4} fact(s)")

    page = (_PAGE
            .replace("__DATA__", json.dumps(data, separators=(",", ":")))
            .replace("__LEGEND__", legend_html(GROUPS))
            .replace("__GROUP_HELP__", group_help_html())
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
