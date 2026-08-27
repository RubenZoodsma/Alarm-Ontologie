"""
ontology_diagram.py — render the ontology's class/property structure (TBox)
as a Mermaid class diagram, for inspection.

Unlike ontology_tree.py (a single spanning tree used to drive CSV/instance
attachment) this draws everything: every owl:Class, every rdfs:subClassOf
edge, every object/datatype property with its full domain (a union domain
such as hasOperationState's is expanded into one edge per member, not
pruned to one) and range. Nothing here is guessed — every node and edge
comes directly from an asserted OWL triple.

A class referenced as a domain/range/superclass but never itself declared
`a owl:Class` in the ontology (e.g. skos:ConceptScheme) is still drawn,
marked (external), instead of being dropped — dropping it would leave a
dangling edge with no visible endpoint. A skos:Concept range is handled
specially: rather than one generic skos:Concept hub absorbing every
vocabulary-valued property (illegible past a handful of edges), each such
property is routed to the specific SKOS scheme it draws from — per the
ontology's own mda:valuesFromScheme bindings — and marked (vocabulary).

Usage
-----
  python3 ontology_diagram.py [ontology.ttl] [out.md]

Defaults to FRAMEWORK/ONTOLOGY/ontology.ttl — the curated, operationally-used
ontology — and writes ontology_diagram.md to DOCS/figures/, as a fenced
```mermaid block any Markdown viewer (VSCode, GitHub) renders directly.
"""

import sys
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, RDFS, OWL, SKOS

from ontology_tree import local

SCRIPT_DIR       = Path(__file__).resolve().parent
ROOT             = SCRIPT_DIR.parent.parent   # CODE/shared -> CODE -> Restructured
DEFAULT_ONTOLOGY = ROOT / "FRAMEWORK" / "ONTOLOGY" / "ontology.ttl"
DEFAULT_OUT      = ROOT / "DOCS" / "figures" / "ontology_diagram.md"


def qname(g: Graph, term) -> str:
    """Prefixed name from the ontology's own @prefix declarations, e.g. 'mda:Alarm'."""
    try:
        return g.namespace_manager.qname(term)
    except Exception:
        return local(term)


def expand(g: Graph, prop: URIRef, predicate: URIRef) -> list:
    """
    Named classes asserted via `predicate` (rdfs:domain or rdfs:range) on
    `prop`, expanding an owl:unionOf blank node into its members instead of
    skipping it — a union domain is exactly the structure worth showing here.
    """
    out = []
    for val in g.objects(prop, predicate):
        if isinstance(val, URIRef):
            out.append(val)
            continue
        union = g.value(val, OWL.unionOf)
        if union is not None:
            out.extend(m for m in Collection(g, union) if isinstance(m, URIRef))
    return out


def characteristics(g: Graph, prop: URIRef) -> list:
    """
    OWL property characteristics asserted on `prop`, as short edge-label tags.

    A ':' anywhere inside a bracketed edge label breaks the Mermaid parser
    used by the artifact host (verified empirically) — so referenced
    property names use their bare local name here, never a qname, and no
    tag text otherwise contains a colon.
    """
    tags = []
    types = set(g.objects(prop, RDF.type))
    if OWL.FunctionalProperty in types:
        tags.append("functional")
    if OWL.InverseFunctionalProperty in types:
        tags.append("inverse-functional")
    sup = sorted(local(s) for s in g.objects(prop, RDFS.subPropertyOf))
    if sup:
        tags.append("sub-property-of " + ", ".join(sup))
    inv = sorted(local(i) for i in g.objects(prop, OWL.inverseOf))
    if inv:
        tags.append("inverse-of " + ", ".join(inv))
    return tags


def find_annotation_property(g: Graph, name: str) -> URIRef:
    """
    An owl:AnnotationProperty in `g` by its local name, or None.

    Used to locate mda:valuesFromScheme without hardcoding this ontology's
    namespace — matched by name because that identity has to come from
    somewhere, but nothing about *where* the ontology lives is assumed.
    """
    return next((p for p in g.subjects(RDF.type, OWL.AnnotationProperty) if local(p) == name), None)


def scheme_for(g: Graph, prop: URIRef, values_from_scheme: URIRef) -> URIRef:
    """
    The SKOS scheme `prop`'s skos:Concept values are drawn from, per the
    ontology's own mda:valuesFromScheme binding (see the "Vocabulary scheme
    bindings" section of ontology.ttl) — asserted directly on `prop`, or,
    failing that, inherited when every one of `prop`'s sub-properties agrees
    on the same scheme (e.g. hasOperationState itself carries no binding,
    but its three sub-properties all draw from opstate:Scheme). None if
    neither resolves, so the caller can fall back to a generic placeholder.
    """
    if values_from_scheme is None:
        return None
    direct = list(g.objects(prop, values_from_scheme))
    if direct:
        return direct[0]
    schemes = {
        s
        for sub in g.subjects(RDFS.subPropertyOf, prop)
        for s in g.objects(sub, values_from_scheme)
    }
    return next(iter(schemes)) if len(schemes) == 1 else None


def build_diagram(g: Graph) -> str:
    """
    A Mermaid `classDiagram` for the full TBox structure of `g`.

    Attributes are declared inline in each class's own `{ }` block, at the
    same statement as its `class ID["label"]` declaration — Mermaid's
    parser (as used by the artifact host, verified empirically) rejects the
    alternative shorthand of a bare `ID : +member` statement following a
    bracket-labeled class declaration, even though both forms are
    individually valid on their own.
    """
    declared = {c for c in g.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)}
    values_from_scheme = find_annotation_property(g, "valuesFromScheme")

    node_id, class_order, scheme_nodes = {}, [], set()

    def nid(cls: URIRef) -> str:
        key = str(cls)
        if key not in node_id:
            node_id[key] = f"c{len(node_id)}"
            class_order.append(cls)
        return node_id[key]

    for cls in sorted(declared, key=str):
        nid(cls)

    subclass_edges = [
        (nid(parent), nid(child))
        for child, parent in sorted(g.subject_objects(RDFS.subClassOf), key=lambda p: str(p))
        if isinstance(parent, URIRef) and isinstance(child, URIRef)
    ]

    # Datatype properties → attributes on their domain class(es)
    attrs = {}
    for prop in sorted(g.subjects(RDF.type, OWL.DatatypeProperty), key=str):
        ranges = expand(g, prop, RDFS.range)
        rng = qname(g, ranges[0]) if ranges else "?"
        for dom in sorted(expand(g, prop, RDFS.domain), key=str):
            attrs.setdefault(nid(dom), []).append(f"+{rng} {local(prop)}")

    # Object properties → associations between domain and range class(es).
    # A skos:Concept range is a vocabulary lookup, not a real class — route
    # it to the specific SKOS scheme the property draws from (per
    # mda:valuesFromScheme) instead of a single generic skos:Concept hub,
    # so e.g. hasPhase and hasRate land on distinct, identifiable nodes
    # rather than all converging on one node with a dozen crossing edges.
    assoc_edges = []
    for prop in sorted(g.subjects(RDF.type, OWL.ObjectProperty), key=str):
        tags = characteristics(g, prop)
        label = local(prop) + (f" [{', '.join(tags)}]" if tags else "")
        scheme = scheme_for(g, prop, values_from_scheme)
        for dom in expand(g, prop, RDFS.domain):
            for rng in expand(g, prop, RDFS.range):
                target = rng
                if rng == SKOS.Concept and scheme is not None:
                    target = scheme
                    scheme_nodes.add(target)
                assoc_edges.append(f'    {nid(dom)} --> {nid(target)} : {label}')

    class_lines = []
    for cls in class_order:
        cid = node_id[str(cls)]
        label = qname(g, cls).replace('"', "'")
        if cls in scheme_nodes:
            label += " (vocabulary)"
        elif cls not in declared:
            label += " (external)"
        cls_attrs = attrs.get(cid)
        if cls_attrs:
            class_lines.append(f'    class {cid}["{label}"] {{')
            class_lines.extend(f'        {a}' for a in cls_attrs)
            class_lines.append('    }')
        else:
            class_lines.append(f'    class {cid}["{label}"]')

    subclass_lines = [f'    {parent} <|-- {child}' for parent, child in subclass_edges]

    body = class_lines + subclass_lines + assoc_edges
    if not body:
        return 'classDiagram\n    empty["no classes or properties found"]'
    return "classDiagram\n" + "\n".join(body)


def main() -> None:
    onto_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ONTOLOGY
    out_path  = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT

    g = Graph()
    g.parse(onto_path, format="turtle")

    diagram = build_diagram(g)

    n_classes = len({c for c in g.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)})
    n_obj     = len(list(g.subjects(RDF.type, OWL.ObjectProperty)))
    n_data    = len(list(g.subjects(RDF.type, OWL.DatatypeProperty)))
    print(f"[ontology] {onto_path}")
    print(f"[classes]  {n_classes}")
    print(f"[props]    {n_obj} object, {n_data} datatype")

    try:
        rel_path = onto_path.resolve().relative_to(ROOT)
    except ValueError:
        rel_path = onto_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        f"# Ontology diagram — {rel_path}\n\n"
        f"```mermaid\n{diagram}\n```\n",
        encoding="utf-8",
    )
    print(f"[output]   {out_path}")


if __name__ == "__main__":
    main()
