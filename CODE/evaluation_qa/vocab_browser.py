"""
vocab_browser.py — regenerates the data behind the site's published
Vocabulary Browser page (docs/vocab/index.html) from the live
vocab_base.ttl + vocab_generated.ttl.

That page is a single self-contained HTML file (same no-server pattern as
archetype_report.py's docs/graph/ and op_visual.py) whose entire dataset is
one JS constant, `const VOCAB = {...};`, embedded in a <script> tag. This
script does NOT touch the page's HTML/CSS/JS shell at all — it parses the
current vocab_base.ttl + vocab_generated.ttl into that exact JSON shape and
replaces only that one line, byte-for-byte identical everywhere else. There
was no discoverable generator for this page anywhere in the repo (unlike
docs/graph/, no script produced it before); its schema was reverse-engineered
from the shipped JSON itself, so a byte-identical shell was the safest way to
regenerate its content without risking the hand-built GUI around it.

Schema (matches what's already embedded, field-for-field):
  schemes:  {scheme_uri: {uri, prefLabel: {lang: str}, definition: {lang: str},
             topConcepts: [concept_uri, ...]}}
  concepts: {concept_uri: {uri, prefLabel: {lang: str}, definition: {lang: str},
             notation: str, broader: [uri, ...], narrower: [uri, ...],
             inScheme: [uri, ...], topConceptOf: [uri, ...]}}

skos:narrower is never asserted in this project's vocabulary (confirmed:
zero occurrences in both source files) — every concept's "narrower" list is
computed here as the inverse of skos:broader, the same way the shipped JSON
already has it.

Usage
-----
  python3 vocab_browser.py
"""

import json
import re
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import RDF, SKOS

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent   # CODE/evaluation_qa -> CODE -> repo root
FRAMEWORK_DIR = ROOT / "FRAMEWORK"
VOCAB_BASE = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "vocab_base.ttl"
VOCAB      = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
OUT        = ROOT / "docs" / "vocab" / "index.html"

VOCAB_CONST_RE = re.compile(r"const VOCAB = (\{.*?\});", re.S)


def _lang_map(g: Graph, subject, predicate) -> dict:
    out = {}
    for lit in g.objects(subject, predicate):
        if lit.language:
            out[lit.language] = str(lit)
    return out


def build_vocab_data(g: Graph) -> dict:
    schemes = {}
    for scheme in sorted(g.subjects(RDF.type, SKOS.ConceptScheme)):
        schemes[str(scheme)] = {
            "uri": str(scheme),
            "prefLabel": _lang_map(g, scheme, SKOS.prefLabel),
            "definition": _lang_map(g, scheme, SKOS.definition),
            "topConcepts": sorted(str(c) for c in g.subjects(SKOS.topConceptOf, scheme)),
        }

    narrower = {}
    for concept, broader in g.subject_objects(SKOS.broader):
        narrower.setdefault(str(broader), []).append(str(concept))

    concepts = {}
    for concept in sorted(g.subjects(RDF.type, SKOS.Concept)):
        uri = str(concept)
        notation = g.value(concept, SKOS.notation)
        concepts[uri] = {
            "uri": uri,
            "prefLabel": _lang_map(g, concept, SKOS.prefLabel),
            "definition": _lang_map(g, concept, SKOS.definition),
            "notation": str(notation) if notation is not None else "",
            "broader": sorted(str(b) for b in g.objects(concept, SKOS.broader)),
            "narrower": sorted(narrower.get(uri, [])),
            "inScheme": sorted(str(s) for s in g.objects(concept, SKOS.inScheme)),
            "topConceptOf": sorted(str(s) for s in g.objects(concept, SKOS.topConceptOf)),
        }

    return {"schemes": schemes, "concepts": concepts}


def main() -> None:
    g = Graph()
    g.parse(VOCAB_BASE, format="turtle")
    g.parse(VOCAB, format="turtle")

    data = build_vocab_data(g)
    blob = json.dumps(data, ensure_ascii=False, separators=(", ", ": "))

    html = OUT.read_text(encoding="utf-8")
    new_html, n = VOCAB_CONST_RE.subn(f"const VOCAB = {blob};", html, count=1)
    if n != 1:
        raise RuntimeError(
            f"Expected exactly one 'const VOCAB = ...;' block in {OUT}, found {n} — "
            "the page shell may have changed; check before overwriting."
        )
    OUT.write_text(new_html, encoding="utf-8")

    print(f"[vocab]  {len(data['schemes'])} scheme(s), {len(data['concepts'])} concept(s)")
    print(f"[output] {OUT.relative_to(ROOT)}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
