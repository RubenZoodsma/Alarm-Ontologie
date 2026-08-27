"""
ontology_tree.py — the class nesting implied by the ontology, derived once.

Shared by CODE/framework_build/build_framework.py (which builds the blueprint)
and CODE/evaluation_poc/op_knowledge.py (which reads it back).  It belongs to
no single stage, hence CODE/shared/.

Both stages need the same fact: which object properties, with named domain and
range, span a tree rooted at mda:Alarm.  Before this module existed the readout
derived that tree and the operational reader re-walked it by hand, so a branch
added to the ontology was picked up by one and silently ignored by the other.

Nothing here knows any domain vocabulary.  Everything comes from asserted
rdfs:domain / rdfs:range pairs.
"""

import re

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS, OWL, SKOS


def local(iri) -> str:
    """Local name of an IRI, for messages and tree printing."""
    return re.split(r"[#/]", str(iri))[-1]


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
        for dom in onto.objects(prop, RDFS.domain):
            if not isinstance(dom, URIRef):
                continue  # union/blank domains cannot anchor a tree edge
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


def format_tree(tree: dict, root: URIRef) -> list:
    """The derived nesting as printable lines, so a run shows what it used."""
    children: dict = {}
    for cls, link in tree.items():
        if link is not None:
            children.setdefault(link[0], []).append((link[1], cls))

    out = [local(root)]

    def walk(cls: URIRef, depth: int) -> None:
        for prop, child in sorted(children.get(cls, []), key=lambda c: local(c[1])):
            out.append(f"{'   ' * depth}└─ {local(prop)} → {local(child)}")
            walk(child, depth + 1)

    walk(root, 0)
    return out


def properties_below(tree: dict, cls: URIRef) -> set:
    """
    Every property on the subtree rooted at `cls` — the chain of relations
    reachable from it.  Used to ask the ontology what a branch consists of
    instead of enumerating it in code.
    """
    children: dict = {}
    for c, link in tree.items():
        if link is not None:
            children.setdefault(link[0], []).append((link[1], c))

    props, frontier = set(), [cls]
    while frontier:
        nxt = []
        for c in frontier:
            for prop, child in children.get(c, []):
                props.add(prop)
                nxt.append(child)
        frontier = nxt
    return props


def properties_reachable(onto: Graph, root: URIRef) -> set:
    """
    Every property reachable from `root` by following rdfs:domain/rdfs:range
    edges outward, closed transitively — unlike derive_class_tree/
    properties_below, this does NOT prune a class once some other branch has
    already reached it. It is a multi-parent reachability walk, not a single
    spanning tree.

    Needed because the ontology is a DAG, not a tree, at at least one point:
    mda:PhysiologicalProcess has two legitimate incoming edges (isPropertyOf
    from PhysiologicalProperty, targetsProcess from TherapeuticModality).
    derive_class_tree can only register one parent per class — it happens to
    pick targetsProcess, because Device->administers->TherapeuticModality is a
    shorter path from mda:Alarm than Device->hasSensor->Sensor->detectsMetric
    ->Metric->approximates->PhysiologicalProperty. A caller that wants "every
    property on the clinical chain below Metric" must not silently lose
    isPropertyOf/presentIn/organPartOfSystem to that pruning; this function
    answers that question directly instead of consulting a tree built for a
    different purpose (CSV/instance attachment, where exactly one parent per
    class is the right answer).
    """
    edges: dict = {}
    for prop in onto.subjects(RDF.type, OWL.ObjectProperty):
        for dom in onto.objects(prop, RDFS.domain):
            if not isinstance(dom, URIRef):
                continue
            for rng in onto.objects(prop, RDFS.range):
                if not isinstance(rng, URIRef) or rng == SKOS.Concept:
                    continue
                edges.setdefault(dom, []).append((prop, rng))

    props, seen, frontier = set(), {root}, [root]
    while frontier:
        nxt = []
        for cls in frontier:
            for prop, rng in edges.get(cls, []):
                props.add(prop)
                if rng not in seen:
                    seen.add(rng)
                    nxt.append(rng)
        frontier = nxt
    return props


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
