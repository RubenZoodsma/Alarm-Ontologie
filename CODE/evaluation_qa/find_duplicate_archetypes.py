"""
find_duplicate_archetypes.py — do two mda:AlarmType archetypes carry
identical content under different labels, AFTER complete OWL-RL inference?

This is not a blueprint-level check. Two archetypes can look different in
kg_generated.ttl's raw assertions (e.g. one alarm's device leaf-typed as a
manufacturer model, an entailed-only fact elsewhere) and still be genuinely
distinct, or look different only because the blueprint stays undecomposed
while a bridge axiom in inference.ttl would resolve them to the same
downstream physiology. So this script loads the full live pipeline —
FRAMEWORK/ONTOLOGY/ontology.ttl + FRAMEWORK/VOCABULARY/vocab_generated.ttl,
FRAMEWORK/KNOWLEDGE_BASE/inference.ttl, kg_generated.ttl — materialises the
OWL-RL closure with a real reasoner (owlrl), and only then compares.

Method: for each archetype, build a "signature" — the sorted set of every
  (a) hasCategory/hasPriority on the archetype itself,
  (b) rdf:type of every node reachable via hasMessage/(concernsPatient|
      triggeredBy/(hasComponent|hasFunctionalUnit|hasSensor|producesSignal|
      analyzedBy|producesMetric)*) — the node's identity. Restricted to
      skos:Concept types so entailed
      supertype noise (mda:Device, owl:Thing, ...) doesn't erase real
      distinctions,
  (c) hasQualityState/hasRate/hasComponentOperationState/
      hasSensorOperationState/hasDeviceOperationState/hasAnatomicalPosition/
      hasManufacturer/hasDeviceType asserted on those nodes — their leaf
      values. hasManufacturer/hasDeviceType are included even though a
      device's precise leaf concept is also entailed and already covered by
      (b) (kg_generated.ttl asserts the base device class
      plus these leaves, not the precoordinated leaf concept directly — see
      build_framework.py's individuated-node handling). Redundant with (b)
      once reasoning has run, but harmless: entailment only ever sharpens a
      signature, never blurs it, and this keeps the check correct even if a
      device concept's owl:equivalentClass definition is ever incomplete,
  (d) every entailed mda:hasOperationState anywhere in the tree — technical
      net effect (e.g. a component-level Disconnected propagated up to the
      device),
  (e) the TERMINAL node of every situational-predicate chain out of the
      tree — approximates/isPropertyOf/presentIn/organPartOfSystem
      (clinical) and administers/targetsProcess (therapeutic), walked to
      where no further situational edge exists — the downstream clinical/
      therapeutic effect, entailed via inference.ttl's bridge and tail
      axioms, not asserted anywhere in kg_generated.ttl.
Archetypes are grouped by that signature; a group with more than one
archetype means those alarms are indistinguishable after everything the
ontology can derive about them — a genuine content duplicate, not two
alarms that merely share one downstream implication (an earlier version of
this check compared only the terminal effect and wrongly flagged three
different-manufacturer disconnect alarms as identical because they reached
the same organ system; node+value identity first is what rules that out).

This is the same identical-content question a CQ5-style competency query
(EVALUATION/CQs/) asks — reimplemented standalone here so it can run as part
of the framework-build pipeline without pulling in the whole CQ harness, and
updated for the current blueprint shape (decomposed device typing, no
per-row administers assertions).

Usage
-----
  python3 find_duplicate_archetypes.py

Exit code 0 if no duplicate group is found, 1 otherwise (so this can gate a
pipeline run); a report is always written to duplicate_archetypes_report.md
alongside this script.
"""

import sys
from pathlib import Path

import owlrl
from rdflib import Graph, Namespace

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent   # CODE/evaluation_qa -> CODE -> Restructured
FRAMEWORK_DIR = ROOT / "FRAMEWORK"
ONTOLOGY = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
INFERENCE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "inference.ttl"
CATALOGUE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
REPORT = ROOT / "EVALUATION" / "FRAMEWORK_QA" / "duplicate_archetypes_report.md"

MDA = Namespace("https://w3id.org/mda/ontology#")

SIGNATURE_QUERY = """
PREFIX mda: <https://w3id.org/mda/ontology#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT ?a ?label (GROUP_CONCAT(DISTINCT ?fact; separator="^") AS ?signature) WHERE {
  {
    SELECT ?a ?label ?fact WHERE {
      ?a a mda:AlarmType ; mda:hasLabel ?label .
      {
        ?a mda:hasCategory ?v .
        BIND(CONCAT("hasCategory=", STR(?v)) AS ?fact)
      } UNION {
        ?a mda:hasPriority ?v .
        BIND(CONCAT("hasPriority=", STR(?v)) AS ?fact)
      } UNION {
        # node identity — which concept each node is asserted or entailed to be
        ?a mda:hasMessage/(mda:concernsPatient|mda:triggeredBy/(mda:hasComponent|mda:hasFunctionalUnit|mda:hasSensor|mda:producesSignal|mda:analyzedBy|mda:producesMetric)*) ?node .
        ?node rdf:type ?t .
        ?t a skos:Concept .
        BIND(CONCAT("type=", STR(?t)) AS ?fact)
      } UNION {
        # node values — the leaf facts asserted on those nodes
        ?a mda:hasMessage/(mda:concernsPatient|mda:triggeredBy/(mda:hasComponent|mda:hasFunctionalUnit|mda:hasSensor|mda:producesSignal|mda:analyzedBy|mda:producesMetric)*) ?node .
        ?node ?p ?v .
        FILTER(?p IN (mda:hasQualityState, mda:hasRate, mda:hasComponentOperationState,
                       mda:hasSensorOperationState, mda:hasDeviceOperationState, mda:hasAnatomicalPosition,
                       mda:hasManufacturer, mda:hasDeviceType))
        BIND(CONCAT(STRAFTER(STR(?p), "#"), "=", STR(?v)) AS ?fact)
      } UNION {
        # net effect — entailed operation state anywhere in the tree
        ?a mda:hasMessage/(mda:concernsPatient|mda:triggeredBy/(mda:hasComponent|mda:hasFunctionalUnit|mda:hasSensor|mda:producesSignal|mda:analyzedBy|mda:producesMetric)*) ?node .
        ?node mda:hasOperationState ?v .
        BIND(CONCAT("hasOperationState=", STR(?v)) AS ?fact)
      } UNION {
        # net effect — terminal situational implication (clinical/therapeutic)
        ?a mda:hasMessage/(mda:concernsPatient|mda:triggeredBy/(mda:hasComponent|mda:hasFunctionalUnit|mda:hasSensor|mda:producesSignal|mda:analyzedBy|mda:producesMetric)*) ?node .
        ?node (mda:approximates|mda:isPropertyOf|mda:presentIn|mda:organPartOfSystem|mda:administers|mda:targetsProcess)+ ?effect .
        FILTER NOT EXISTS { ?effect (mda:approximates|mda:isPropertyOf|mda:presentIn|mda:organPartOfSystem|mda:administers|mda:targetsProcess) ?next }
        BIND(CONCAT("effect=", STR(?effect)) AS ?fact)
      }
    }
  }
}
GROUP BY ?a ?label
ORDER BY ?label
"""


def load_reasoned_graph() -> Graph:
    g = Graph()
    for f in (ONTOLOGY, VOCAB, INFERENCE, CATALOGUE):
        g.parse(f, format="turtle")
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
    return g


def find_duplicate_groups(g: Graph) -> dict:
    """{signature: [(alarmtype_iri, label), ...]} for every signature shared
    by more than one archetype."""
    by_signature = {}
    for row in g.query(SIGNATURE_QUERY):
        by_signature.setdefault(str(row.signature), []).append(
            (str(row.a), str(row.label))
        )
    return {sig: rows for sig, rows in by_signature.items() if len(rows) > 1}


def write_report(duplicates: dict, total_archetypes: int) -> str:
    lines = [
        "# Duplicate archetype check\n",
        f"Checked {total_archetypes} mda:AlarmType archetype(s) after full OWL-RL "
        "inference over ontology + vocab + inference axioms + the archetype catalogue.\n",
    ]
    if not duplicates:
        lines.append("\nNo duplicate content found — every archetype is distinguishable "
                      "after everything the ontology entails about it.\n")
    else:
        lines.append(f"\n**{len(duplicates)} duplicate group(s) found:**\n")
        for i, (sig, rows) in enumerate(sorted(duplicates.items()), 1):
            labels = ", ".join(f"`{label}`" for _, label in rows)
            lines.append(f"\n## Group {i}: {len(rows)} archetypes\n")
            lines.append(f"{labels}\n")
    text = "\n".join(lines)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    g = load_reasoned_graph()
    total = len(list(g.query("SELECT DISTINCT ?a WHERE { ?a a mda:AlarmType }",
                              initNs={"mda": MDA})))

    duplicates = find_duplicate_groups(g)
    report = write_report(duplicates, total)
    print(report)
    print(f"[find_duplicate_archetypes] report written to {REPORT.name}")

    return 1 if duplicates else 0


if __name__ == "__main__":
    sys.exit(main())
