"""
cq_QA.py — base for running Competency Questions (CQs) against the curated
ontology.

Loads the MDA framework's FRAMEWORK/ resources (ontology, enriched vocabulary,
knowledge base), prepares two variants of the graph — the raw base and its
OWL-RL closure — then auto-discovers every .rq file one level under
EVALUATION/CQs/ (i.e. EVALUATION/CQs/<stage>/*.rq, stage being
pre_enrichment, post_enrichment, or temporal) and runs each against whichever
variant that query declares it needs. All stages are run and reported
together in one pass — the pre/post/temporal split is a corpus-scoping
distinction (§4.3), not a separate evaluation run — writing a single Markdown
report (EVALUATION/CQs/cq_report.md) grouped by stage, with per CQ: its
natural-language question, the SPARQL that answers it, and up to the first
MAX_ROWS_SHOWN resulting rows (full row count is still reported — only the
rendered table is capped, to keep the report skimmable).

A query states its natural-language question with a leading directive
comment, alongside `# reasoning: required` (see below):

    # question: <the CQ, verbatim>

Reasoned or not, per query
---------------------------
A query that asks about an *entailed* fact (e.g. mda:approximates, only
materialised by the reasoner from inference.ttl's class-level bridge
restrictions — see that file's header) must run against the closure. A query
that only walks *asserted* structure (e.g. rdf:type straight off kg.ttl's
archetypes) must NOT: OWL-RL also derives every superclass membership (a
Philips monitor archetype ends up typed mda:Device, owl:Thing, ...), which
floods a plain "group alarms by shared rdf:type" query with meaningless
broad-category matches that don't exist in the base data. So a query opts
in explicitly with a leading directive comment:

    # reasoning: required

Its absence means "runs against the base graph" (the default, and the
common case — most CQs here ask about what kg.ttl/vocab.ttl directly assert).

CQs that need patient-level fixture data (CQ-set E) and pass/fail checking
against the plan's positive/negative cases are not wired up yet — those
build on this base.

Base graph
----------
The three files op_knowledge.py treats as the TBox (see its path-block
comment): FRAMEWORK/ONTOLOGY/ontology.ttl (classes/properties),
FRAMEWORK/VOCABULARY/vocab_generated.ttl (SKOS vocabulary),
FRAMEWORK/KNOWLEDGE_BASE/inference.ttl (bridge + tail axioms — required for
the entailments CQ-set E in the validation plan puts under test) — plus
FRAMEWORK/KNOWLEDGE_BASE/kg_generated.ttl, the archetype catalogue: reusable
blueprints of the implicit knowledge an
alarm label carries, formalised so a CQ can reason over the library itself
(e.g. CQ5: do two archetypes carry identical content under different
labels?). A CQ that instead needs patient-level instance data (CQ-set E's
snapshot fixtures) loads that ABox on top of this base itself —
kg_generated.ttl's archetypes are class-level blueprints, not that fixture.

Usage
-----
  python3 cq_QA.py
"""

import re
from pathlib import Path

from rdflib import Dataset, Graph, Namespace, URIRef
import owlrl

import sparql_engine

REASONING_DIRECTIVE = re.compile(r"^#\s*reasoning:\s*required\s*$", re.MULTILINE)
QUESTION_DIRECTIVE = re.compile(r"^#\s*question:\s*(.+)$", re.MULTILINE)

# This file lives at CODE/evaluation_cq/cq_QA.py; walk up to Restructured/.
ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_DIR = ROOT / "FRAMEWORK"
CQS_DIR = ROOT / "EVALUATION" / "CQs"
REPORT = CQS_DIR / "cq_report.md"

# Presentation order for the report's stage grouping (§4.3: CQs 1-8 run
# before and after enrichment, CQs 9-11 run once against the temporal,
# enriched dataset). Any subfolder not listed here still runs, just sorts
# after the named stages.
STAGE_ORDER = ["pre_enrichment", "post_enrichment", "temporal"]

ONTOLOGY = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
INFERENCE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "inference.ttl"
CLINICAL_EVENTS = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "clinicalEvents.ttl"
CATALOGUE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"

TBOX_FILES = [ONTOLOGY, VOCAB, INFERENCE, CLINICAL_EVENTS]
BASE_FILES = TBOX_FILES + [CATALOGUE]

# Rendered report tables are capped to keep the Markdown skimmable; the full
# row count is still reported alongside the truncated table.
MAX_ROWS_SHOWN = 5

# Namespaces every CQ's SPARQL will bind against — the shared vocabulary
# (mda:) plus the named-graph snapshot scope the validation plan requires
# every CQ to use for patient isolation (§2 step 6, §5 E4): one graph per
# (patient, instant), reached via snap:forPatient. ent:/inst: mirror
# op_knowledge.py's ENTITY/INST split (persistent entities vs.
# per-alarm instances) for whatever fixture is authored against this base.
MDA = Namespace("https://w3id.org/mda/ontology#")
ENT = Namespace("https://w3id.org/mda/entity/")
INST = Namespace("https://w3id.org/mda/instance/")
SNAP = Namespace("https://w3id.org/mda/snapshot/")

# Named graph holding the ASSERTED graph next to the closure, for reasoned
# queries (see reasoned_dataset()).
ASSERTED_GRAPH = URIRef("https://w3id.org/mda/graph/asserted")


def load_base() -> Graph:
    """Parse the live pipeline's TBox + archetype catalogue into one graph, namespaces bound."""
    g = Graph()
    for f in BASE_FILES:
        g.parse(f, format="turtle")
    g.bind("mda", MDA)
    g.bind("ent", ENT)
    g.bind("inst", INST)
    g.bind("snap", SNAP)
    return g


def prepare_graph(g: Graph = None) -> Graph:
    """
    The graph CQs execute against: the base TBox (or a caller-supplied graph
    that already includes it plus fixture/ABox data), with its OWL-RL
    closure materialised in place.

    A real reasoner (owlrl), not a query-time trick — CQ-set E in the
    validation plan puts the reasoner itself under test (bridge axioms,
    property chains), so the entailments must actually be computed, not
    approximated by a cleverer SPARQL pattern.
    """
    g = load_base() if g is None else g
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
    return g


_DATASETS: dict = {}


def reasoned_dataset(base: Graph, reasoned: Graph) -> Dataset:
    """
    What a `# reasoning: required` query runs against: the OWL-RL closure as
    the DEFAULT graph (so a query that names no graph sees exactly the
    closure, as before) plus the asserted graph as the named graph
    ASSERTED_GRAPH.

    Why both: inside the closure an asserted type cannot be told apart from
    an entailed one (a node typed metric:AirwayPressure_Peak is also typed
    metric:AirwayPressure, and OWL-RL entails sibling pre-coordinated types
    through hasValue restrictions without entailing the subclass edge that
    would let a query pick the most specific one). A CQ that needs entailed
    facts (organ reachability) AND the identity of what was annotated
    (CQ6's pathway identity) reads the first from the default graph and the
    second inside GRAPH <https://w3id.org/mda/graph/asserted> { ... }.
    Built once per (base, reasoned) pair.
    """
    key = (id(base), len(base), id(reasoned), len(reasoned))
    if key not in _DATASETS:
        ds = Dataset()
        for t in reasoned:
            ds.add(t)
        asserted = ds.graph(ASSERTED_GRAPH)
        for t in base:
            asserted.add(t)
        _DATASETS[key] = ds
    return _DATASETS[key]


def _label(term, g: Graph) -> str:
    """Readable term label: a prefixed name for URIs, the plain value otherwise."""
    if term is None:
        return "-"
    if isinstance(term, URIRef):
        try:
            return g.namespace_manager.normalizeUri(term)
        except Exception:
            return str(term)
    return str(term)


def discover_queries(cqs_dir: Path) -> list:
    """Every .rq file one level under cqs_dir (EVALUATION/CQs/<stage>/*.rq),
    ordered by STAGE_ORDER then filename so the report reads pre- before
    post-enrichment, regardless of directory listing order.

    Excludes a temporal/ stage, should one exist: the reasoning CQs (CQ8-10,
    manuscript Table 1) concern coinciding alarms and are answered by the
    MDA-POC's rules (MDA-POC/README.md, "Competency questions"), not by
    queries over this script's static graphs. The earlier temporal queries
    and the pipeline they ran on are in old/.
    """
    def sort_key(p: Path):
        stage = p.parent.name
        rank = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)
        return (rank, stage, p.name)

    return sorted(
        (p for p in cqs_dir.glob("*/*.rq") if p.parent.name != "temporal"),
        key=sort_key,
    )


def run_query(query_path: Path, base: Graph, reasoned: Graph) -> dict:
    """
    Execute one .rq file against `reasoned` if it carries the
    `# reasoning: required` directive (see module docstring) or `base`
    otherwise. Returns everything the report needs: the CQ's question, the
    raw query text, which graph it ran against, and its result rows.
    """
    text = query_path.read_text()
    used_reasoning = bool(REASONING_DIRECTIVE.search(text))
    graph = reasoned if used_reasoning else base
    question_match = QUESTION_DIRECTIVE.search(text)

    # Executed on Oxigraph (sparql_engine.py: rdflib's evaluator has no
    # query planner); the graph itself is still built and reasoned here.
    # A reasoned query also sees the asserted graph, as a named graph.
    target = reasoned_dataset(base, reasoned) if used_reasoning else base
    names, result_rows = sparql_engine.select(target, text)
    rows = [[_label(t, graph) for t in row] for row in result_rows]
    print(f"[cq_QA] {query_path.parent.name}/{query_path.name}: {len(rows)} row(s) "
          f"({'reasoned' if used_reasoning else 'base'})")

    return {
        "stage": query_path.parent.name,
        "name": query_path.stem,
        "question": question_match.group(1).strip() if question_match else None,
        "query_text": text.strip(),
        "used_reasoning": used_reasoning,
        "vars": names,
        "rows": rows,
    }


def _escape_cell(value: str) -> str:
    """Markdown table cells can't contain a raw '|' or newline."""
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown(base: Graph, reasoned: Graph, results: list) -> str:
    """Render the CQ run as a Markdown report: one section per CQ, each with
    its natural-language question, the SPARQL that answers it, and a table
    of the rows it returned."""
    lines = [
        "# Competency Question validation report",
        "",
        f"Base graph: {', '.join(f.name for f in BASE_FILES)} — {len(base)} triples "
        f"({len(reasoned)} after OWL-RL closure, +{len(reasoned) - len(base)} entailed).",
        "",
    ]
    current_stage = None
    for r in results:
        if r["stage"] != current_stage:
            current_stage = r["stage"]
            lines.append(f"## {current_stage.replace('_', ' ').title()}")
            lines.append("")
        lines.append(f"### {r['name']}")
        lines.append("")
        if r["question"]:
            lines.append(f"**Question:** {r['question']}")
            lines.append("")
        lines.append(f"**Graph:** {'reasoned (OWL-RL closure)' if r['used_reasoning'] else 'base (asserted only)'}")
        lines.append("")
        lines.append("```sparql")
        lines.append(r["query_text"])
        lines.append("```")
        lines.append("")
        shown = r["rows"][:MAX_ROWS_SHOWN]
        lines.append(f"**Outcome:** {len(r['rows'])} row(s)"
                      + (f" — showing first {len(shown)}" if len(r["rows"]) > len(shown) else ""))
        lines.append("")
        if shown:
            lines.append("| " + " | ".join(r["vars"]) + " |")
            lines.append("|" + "|".join("---" for _ in r["vars"]) + "|")
            for row in shown:
                lines.append("| " + " | ".join(_escape_cell(c) for c in row) + " |")
        else:
            lines.append("_No results._")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    base = load_base()
    print(f"[cq_QA] base graph loaded: {', '.join(f.name for f in BASE_FILES)}")
    print(f"        {len(base)} triples")

    reasoned = prepare_graph(Graph() + base)
    print(f"        {len(reasoned)} triples after OWL-RL closure "
          f"(+{len(reasoned) - len(base)} entailed)")

    queries = discover_queries(CQS_DIR)
    if not queries:
        print(f"\n[cq_QA] no competency questions yet under {CQS_DIR.relative_to(ROOT)}/<stage>/")
        return

    results = [run_query(q, base, reasoned) for q in queries]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render_markdown(base, reasoned, results))
    print(f"\n[cq_QA] report written to {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
