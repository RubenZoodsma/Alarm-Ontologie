"""
graph_view.py — render an RDF graph as a Mermaid flowchart.

Used by CODE/evaluation_qa/archetype_report.py (one archetype's full
entailed knowledge), so a legend cannot drift from the colours actually used.
(The retired rdflib POC's op_visual.py and test_concurrent_merge.py, now in
old/, used it too.)

The default classification (node_kind/KINDS) reads categories off the IRI
namespaces op_knowledge.py's minting layer assigns — presentation only, no
domain knowledge decided here. to_mermaid/legend_html accept an alternate
classify function + category table (archetype_report.py passes its own,
grouping by clinical-domain meaning — technical/clinical/therapeutic —
rather than structural role) so a second, genuinely different colour scheme
doesn't need a second copy of the mermaid-building logic.
"""

import re

from rdflib import Graph, URIRef, BNode
from rdflib.namespace import RDF

from ontology_tree import local

# Node categories, their fill/stroke, and the legend text.  One table so the
# diagram and its legend cannot disagree.
#
# Curated entries exist for classes worth a distinct colour; everything else
# minted under /entity/ (op_knowledge.py's particular_iri,
# which prefixes every IRI it mints with "{ClassName}_") falls back to
# "entity" — grey, not "broken" — so a class added to the ontology later (as
# mda:FunctionalUnit was) or one nobody has bothered to curate a colour for
# yet (mda:Component, mda:SignalAnalysis) still renders as an intentional,
# labelled category instead of the undifferentiated "other" white box.
KINDS = {
    "alarm":     ("Alarm",             "fill:#ffd6d6,stroke:#c0392b"),
    "archetype": ("Alarm type",        "fill:#ffeaea,stroke:#c0392b,stroke-dasharray:4 3"),
    "message":   ("Message",           "fill:#ffe8cc,stroke:#e67e22"),
    "patient":   ("Patient",           "fill:#d5f5e3,stroke:#27ae60"),
    "device":    ("Device",            "fill:#d6eaff,stroke:#2980b9"),
    "functionalunit": ("Functional unit", "fill:#cfe8d8,stroke:#1e8449"),
    "sensor":    ("Sensor",            "fill:#d1f2f2,stroke:#16a085"),
    "signal":    ("Signal",            "fill:#e8fbfb,stroke:#16a085"),
    "signalanalysis": ("Signal analysis", "fill:#dff5f2,stroke:#0e6b62"),
    "metric":    ("Metric condition",  "fill:#eaddf7,stroke:#8e44ad"),
    "opstate":   ("Operation state",   "fill:#fdf2b3,stroke:#b7950b"),
    "concept":   ("Vocabulary concept","fill:#ececec,stroke:#7f8c8d"),
    "entity":    ("Other entity",      "fill:#f2f2f2,stroke:#95a5a6"),
    "other":     ("Other",             "fill:#ffffff,stroke:#000000"),
}

_ENTITY_CLASS = re.compile(r"/entity/([A-Za-z]+)_")


def node_kind(iri: str) -> str:
    """Which visual category a node falls into, from its minted namespace."""
    if "/instance/Alarm_" in iri:  return "alarm"
    if "/instance/Msg_" in iri:    return "message"
    if "/alarmtype/" in iri:       return "archetype"
    if "operation-state" in iri:   return "opstate"
    m = _ENTITY_CLASS.search(iri)
    if m:
        return m.group(1).lower() if m.group(1).lower() in KINDS else "entity"
    if "/vocab/" in iri:           return "concept"
    return "other"


def _wrap(text: str, width: int) -> str:
    """Break a label at word boundaries into lines of about `width` characters
    (Mermaid renders <br> as a line break)."""
    out = []
    for part in text.split("<br>"):
        line = ""
        for word in part.split():
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return "<br>".join(out)


def to_mermaid(g: Graph, classify=node_kind, kinds: dict = KINDS, label_of=None,
               direction: str = "LR", sort_key=None, wrap: int = None, dashed=None) -> str:
    """
    A Mermaid `graph LR` for the object-property structure of `g`.

    Literal-valued predicates (hasStart/hasEnd/hasLabel/identifier) are dropped
    — the diagram shows structure, not values.  rdf:type is shown only when it
    names a vocabulary concept, since that is the node's identity rather than
    bookkeeping.

    `classify`/`kinds` default to node_kind/KINDS (structural role — Device,
    Sensor, ...); pass a different pair for a different colour axis over the
    same drawing logic (see archetype_report.py, which groups by
    clinical-domain meaning instead). classify is called with the actual
    term (URIRef or BNode — both are str subclasses, so substring matching
    like node_kind's still works unchanged), never a stringified-then-
    reparsed copy: reconstructing a blank node's string form as a URIRef
    would silently produce a different term matching nothing in the graph.
    `label_of`, if given, overrides a node's displayed text (default: its
    IRI's local name) — e.g. a concept's skos:prefLabel instead of its
    notation.

    Optional, all off by default (output then unchanged):
    `direction`  Mermaid flow direction, "LR" (default) or "TD".
    `sort_key`   term -> sortable key; nodes and edges are then emitted in a
                 fixed order instead of the graph's iteration order (which for
                 blank nodes can differ between builds), so the layered layout
                 is reproducible.
    `wrap`       break labels into lines of about this many characters.
    `dashed`     term -> bool; such nodes get a dashed border.
    """
    node_id, lines, styles, edges = {}, [], [], []

    dashed_ids = []

    def nid(term):
        s = str(term)
        if s not in node_id:
            node_id[s] = f"n{len(node_id)}"
            label = (label_of(term) if label_of else local(term)).replace('"', "'")
            if wrap:
                label = _wrap(label, wrap)
            lines.append(f'    {node_id[s]}["{label}"]')
            styles.append((node_id[s], classify(term)))
            if dashed and dashed(term):
                dashed_ids.append(node_id[s])
        return node_id[s]

    triples = [(s, p, o) for s, p, o in g if isinstance(o, (URIRef, BNode))
               and not (p == RDF.type and "/vocab/" not in str(o))]
    if sort_key:
        triples.sort(key=lambda t: (sort_key(t[0]), local(t[1]), sort_key(t[2])))
    for s, p, o in triples:
        edges.append(f'    {nid(s)} -->|{local(p)}| {nid(o)}')

    if not edges:
        return f'graph {direction}\n    empty["no situational awareness at this instant"]'

    out = [f"graph {direction}"] + lines + edges
    for kind, (_, style) in kinds.items():
        out.append(f"    classDef {kind} {style},color:#111;")
    for n, kind in styles:
        out.append(f"    class {n} {kind};")
    for n in dashed_ids:
        out.append(f"    style {n} stroke-dasharray:4 3;")
    return "\n".join(out)


def legend_html(kinds: dict = KINDS) -> str:
    """The legend for the categories above, as <li> items."""
    return "".join(
        f'<li><span class="dot" style="{style.replace("stroke:", "border-color:")}"></span>{name}</li>'
        for kind, (name, style) in kinds.items() if kind != "other"
    )
