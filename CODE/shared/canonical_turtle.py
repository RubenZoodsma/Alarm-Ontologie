"""
canonical_turtle.py — write a graph as byte-reproducible Turtle.

rdflib names a new blank node at random (_:N5631ad48...), and the build
creates them while iterating sets, whose order changes per Python process.
The same graph therefore serialised differently on every run: git showed a
whole-file diff for an unchanged knowledge base, and a byte comparison of two
builds failed although the graphs were isomorphic.

Before writing, the blank nodes are renamed by W3C RDF Dataset
Canonicalization (RDFC-1.0, https://www.w3.org/TR/rdf-canon/), as implemented
by pyoxigraph (about 0.1 s on the knowledge base). Isomorphic graphs get the
same labels (_:c14n0, _:c14n1, ...). rdflib then writes the Turtle: it orders
subjects, predicates and objects by sorting and writes a blank node used once
inline as [ ... ], so with canonical labels the output is byte-identical and
stays readable.

The labels are numbered, so adding a blank node can renumber others; inline
nodes do not show their label, so in practice only the few shared nodes'
labels, and the order of sibling nodes, change in a diff.
"""

import pyoxigraph as ox
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import XSD

_XSD_STRING = ox.NamedNode(str(XSD.string))


def _to_rdflib(term):
    if isinstance(term, ox.NamedNode):
        return URIRef(term.value)
    if isinstance(term, ox.BlankNode):
        return BNode(term.value)
    if term.language:
        return Literal(term.value, lang=term.language)
    if term.datatype == _XSD_STRING:
        return Literal(term.value)
    return Literal(term.value, datatype=URIRef(term.datatype.value))


def canonical_graph(g: Graph) -> Graph:
    """A copy of g with RDFC-1.0 blank-node labels and g's prefix bindings."""
    ds = ox.Dataset(ox.parse(g.serialize(format="nt"), format=ox.RdfFormat.N_TRIPLES))
    ds.canonicalize(ox.CanonicalizationAlgorithm.RDFC_1_0)
    out = Graph()
    for p, ns in g.namespaces():
        out.bind(p, ns, override=True, replace=True)
    for q in ds:
        out.add((_to_rdflib(q.subject), _to_rdflib(q.predicate), _to_rdflib(q.object)))
    assert len(out) == len(g), "canonicalisation changed the number of triples"
    return out


def serialize_canonical(g: Graph, destination) -> None:
    """Write g as Turtle that is byte-identical for isomorphic inputs."""
    canonical_graph(g).serialize(destination=str(destination), format="turtle")
