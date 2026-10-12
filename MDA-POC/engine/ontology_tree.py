"""
ontology_tree.py — the class tree rooted at mda:Alarm, derived from the
ontology's rdfs:domain/rdfs:range pairs, and a walk of it over the catalogue.

The subset of CODE/shared/ontology_tree.py that mint.py uses, copied (the
POC imports nothing from outside its folder); keep the logic in sync by
hand. Knows no domain vocabulary.
"""

import re

from rdflib import Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, RDFS, OWL, SKOS


def local(iri) -> str:
    """Local name of an IRI, for messages and tree printing."""
    return re.split(r"[#/]", str(iri))[-1]


def domain_classes(onto: Graph, prop: URIRef) -> list:
    """
    Named classes in `prop`'s rdfs:domain, expanding an owl:unionOf blank
    node into its members. A union domain says the property may start from
    any of those classes, so each member anchors its own edge.

    A union member that is also the named domain of one of `prop`'s own
    rdfs:subPropertyOf children is left to that sub-property: the ontology
    already names the single-domain property that carries the edge from that
    class (mda:sensorProducesSignal for mda:producesSignal's Sensor member),
    and emitting both would make the same child reachable twice.

    Ranges are NOT expanded: a union range would give one edge several
    possible children, which is exactly the ambiguity the tree refuses to
    guess at. Only mda:evidencedBy has one today, and its domain
    (mda:ClinicalEvent) is not reachable from mda:Alarm anyway.
    """
    out = []
    for dom in onto.objects(prop, RDFS.domain):
        if isinstance(dom, URIRef):
            out.append(dom)
            continue
        union = onto.value(dom, OWL.unionOf)
        if union is None:
            continue
        carried = {d for sub in onto.subjects(RDFS.subPropertyOf, prop)
                   for d in onto.objects(sub, RDFS.domain) if isinstance(d, URIRef)}
        out.extend(m for m in Collection(onto, union)
                   if isinstance(m, URIRef) and m not in carried)
    return out


def derive_class_tree(onto: Graph, root: URIRef) -> dict:
    """
    Derive the nesting tree from asserted rdfs:domain/rdfs:range pairs.

    Returns {class: (parent_class, property)} with `root` mapping to None.
    Breadth-first from `root`; edges into already-visited classes (inverses,
    cycles) are pruned.  Raises if two different edges reach the same class at
    the same depth — that ambiguity must be resolved in the ontology, not
    guessed at here.
    """
    edges: dict = {}
    for prop in onto.subjects(RDF.type, OWL.ObjectProperty):
        for dom in domain_classes(onto, prop):
            for rng in onto.objects(prop, RDFS.range):
                if not isinstance(rng, URIRef) or rng == SKOS.Concept:
                    continue  # concept-valued properties are leaves, not edges
                edges.setdefault(dom, []).append((prop, rng))

    parent: dict = {root: None}
    frontier = [root]
    while frontier:
        discovered: dict = {}
        for cls in frontier:
            for prop, child in sorted(edges.get(cls, []), key=lambda e: str(e[0])):
                if child in parent:
                    continue
                if child in discovered and discovered[child] != (cls, prop):
                    p_cls, p_prop = discovered[child]
                    raise ValueError(
                        f"Ambiguous nesting for {local(child)}: reachable via "
                        f"{local(p_cls)}.{local(p_prop)} and "
                        f"{local(cls)}.{local(prop)}. Resolve in ontology.ttl."
                    )
                discovered[child] = (cls, prop)
        for child, link in discovered.items():
            parent[child] = link
        frontier = list(discovered)
    return parent


def walk_instances(graph: Graph, tree: dict, root_node, root_class: URIRef) -> dict:
    """
    Follow the derived tree over actual data, from `root_node`.

    Returns {class: node} for every class the graph actually reaches. Traversal
    order comes from the ontology, so a branch added there is followed here
    without any change to this function or its callers.
    """
    children: dict = {}
    for cls, link in tree.items():
        if link is not None:
            children.setdefault(link[0], []).append((link[1], cls))

    found = {root_class: root_node}
    frontier = [(root_class, root_node)]
    while frontier:
        nxt = []
        for cls, node in frontier:
            for prop, child_cls in children.get(cls, []):
                child = next(graph.objects(node, prop), None)
                if child is None or child_cls in found:
                    continue
                found[child_cls] = child
                nxt.append((child_cls, child))
        frontier = nxt
    return found
