"""
sparql_engine.py — executes the competency-question SPARQL on Oxigraph.

The graphs are still built with rdflib, and the OWL-RL closure is still
computed by owlrl (cq_QA.prepare_graph); only query EXECUTION moves here.
rdflib's evaluator has no query planner: it joins by re-evaluating the
right-hand side once per left-hand solution, so a query that joins two
subqueries over the closure re-runs one of them for every row of the
other (CQ7's set comparison: hours). Oxigraph plans the query and runs the
same, standard SPARQL in well under a second.

Results come back as rdflib terms, so the callers format them exactly as
before (cq_QA._label, str()).

One deliberate loss: owlrl emits generalised-RDF triples with a literal
subject (e.g. "hasAggregation" owl:sameAs "hasAggregation"), which standard
RDF — and therefore Oxigraph — does not allow. They carry no content a CQ
asks about and are left out; to_store() reports how many.
"""

from rdflib import BNode, Dataset, Graph, Literal, URIRef
import pyoxigraph as ox

_STORES: dict = {}


def _term(t):
    if isinstance(t, URIRef):
        return ox.NamedNode(str(t))
    if isinstance(t, BNode):
        return ox.BlankNode(str(t))
    if t.language:
        return ox.Literal(str(t), language=t.language)
    if t.datatype is not None:
        return ox.Literal(str(t), datatype=ox.NamedNode(str(t.datatype)))
    return ox.Literal(str(t))


def _rdflib(t):
    if t is None:
        return None
    if isinstance(t, ox.NamedNode):
        return URIRef(t.value)
    if isinstance(t, ox.BlankNode):
        return BNode(t.value)
    if t.language:
        return Literal(t.value, lang=t.language)
    return Literal(t.value, datatype=URIRef(t.datatype.value))


def to_store(g) -> ox.Store:
    """An Oxigraph store holding `g`: an rdflib Graph (its triples in the
    default graph) or Dataset (the default graph and every named graph).
    Built once per graph object and reused."""
    key = (id(g), len(g))
    if key in _STORES:
        return _STORES[key]
    quads, skipped = [], 0
    if isinstance(g, Dataset):
        for s, p, o, c in g.quads((None, None, None, None)):
            if isinstance(s, Literal):
                skipped += 1
                continue
            name = c.identifier if isinstance(c, Graph) else c
            graph = (ox.DefaultGraph() if name is None or name == g.default_context.identifier
                     else _term(name))
            quads.append(ox.Quad(_term(s), _term(p), _term(o), graph))
    else:
        for s, p, o in g:
            if isinstance(s, Literal):
                skipped += 1
                continue
            quads.append(ox.Quad(_term(s), _term(p), _term(o), ox.DefaultGraph()))
    store = ox.Store()
    store.extend(quads)
    if skipped:
        print(f"[sparql_engine] {skipped} triple(s) with a literal subject left out (not standard RDF)")
    _STORES[key] = store
    return store


def select(g, text: str) -> tuple:
    """(variable names, rows) for a SELECT query over `g`; each row a list
    of rdflib terms, None where a variable is unbound."""
    solutions = to_store(g).query(text)
    names = [v.value for v in solutions.variables]
    rows = [[_rdflib(s[name]) for name in names] for s in solutions]
    return names, rows
