"""
entity_seed.py — seed DATA/POC_EVENTS/entities.ttl with one stable `scaffold:`
IRI for every individuated particular *type* in the alarm-type catalogue.

This is a TYPE CATALOGUE, not a store of usable particulars.  For each distinct
vocabulary type of an individuated class (mda:nodeKind mda:Individuated —
Device, Sensor, Signal, Patient) that appears in FRAMEWORK/KNOWLEDGE_BASE/
kg_generated.ttl, it mints a stable `scaffold:` IRI carrying only the
concept's BASE knowledge: its rdf:type and a label.

`scaffold:` is a deliberately different namespace from `entity:`.  A scaffold
row is not a grounding anchor to attach real-world facts to — Sensor/Signal/
Device are individuated because one owner (a device, in turn a patient) can
have several of the same kind that must be told apart, and a sensor or signal
belongs to exactly one owner at a time (mda:hasSensor and mda:producesSignal
are both owl:InverseFunctionalProperty — no separate isSensorOf/isSignalOf
property is declared; query in the has-direction only).
A flat, type-keyed IRI cannot carry that owner distinction, so reusing one
directly as an operational node — across multiple devices, or worse multiple
patients — asserts a false identity (one physical sensor claimed by several
devices at once). op_knowledge.py mints owner-scoped `entity:` particulars at
grounding time instead (see sensor_iri/signal_iri there); it consults this
catalogue only to check that a concept is a legitimate individuated type, and
for its label.

Individuated classes are read from the ontology, so this stays consistent with
the rest of the pipeline.  The output is regenerated from scratch on every run.

Usage
-----
  python3 entity_seed.py
"""

import re
from pathlib import Path

import owlrl
from rdflib import Graph, URIRef, Literal, Namespace
from rdflib.namespace import RDF, RDFS, OWL, SKOS

# ── Paths ─────────────────────────────────────────────────────────────────────
#
# Reads the live FRAMEWORK/ pipeline output (see op_knowledge.py's
# path-block comment for why) and writes into DATA/POC_EVENTS.

OP_DIR        = Path(__file__).resolve().parent
ROOT          = OP_DIR.parent.parent.parent   # CODE/evaluation_poc/seed -> evaluation_poc -> CODE -> repo root
FRAMEWORK_DIR = ROOT / "FRAMEWORK"
ONTOLOGY = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB    = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
KG       = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
OUT      = ROOT / "DATA" / "POC_EVENTS" / "entities.ttl"

# ── Namespaces ────────────────────────────────────────────────────────────────

MDA      = Namespace("https://w3id.org/mda/ontology#")
SCAFFOLD = Namespace("https://w3id.org/mda/scaffold/")


def _local(iri) -> str:
    return re.split(r"[#/]", str(iri))[-1]


def main() -> None:
    onto = Graph(); onto.parse(ONTOLOGY, format="turtle")
    kg   = Graph(); kg.parse(KG, format="turtle")
    vocab = Graph(); vocab.parse(VOCAB, format="turtle")

    # Individuated classes, from the ontology
    individuated = set(onto.subjects(MDA.nodeKind, MDA.Individuated))

    out = Graph()
    out.bind("scaffold", SCAFFOLD)
    out.bind("mda", MDA)
    out.bind("rdfs", RDFS)
    out.bind("skos", SKOS)

    # Which concepts type an individuated node?  Read straight off the scheme
    # bindings: mda:instantiatesClass says a scheme's concepts are used as the
    # rdf:type of instances of that class.  This is the same declaration the
    # readout uses, so the scaffold cannot drift from the blueprint.
    #
    # (An earlier version located nodes by scanning for a property whose
    # rdfs:range was the class.  That picked inverse properties which never
    # appear in the catalogue — e.g. mda:monitors for Device, the inverse of
    # isMonitoredBy — and silently produced an empty scaffold.  Ranges do not
    # identify node types; the scheme bindings do.)
    concept_class = {}
    for scheme, cls in onto.subject_objects(MDA.instantiatesClass):
        if cls not in individuated:
            continue
        for concept in vocab.subjects(SKOS.inScheme, scheme):
            if (concept, SKOS.topConceptOf, scheme) in vocab:
                continue          # the scheme's own top concept is scaffolding
            concept_class[concept] = cls

    # Keep the concepts the catalogue actually instantiates, so the scaffold
    # covers what the alarms reference and nothing more.
    #
    # This must check the REASONED closure, not the raw kg: an individuated
    # node's precise type (e.g. device:MechanicalVentilator_ServoU_Getinge)
    # is deliberately not asserted directly any more — the blueprint asserts
    # only the base class plus its identity-refining leaves (hasManufacturer,
    # hasDeviceType), and the precise concept is recovered via vocab's
    # owl:equivalentClass definition for it. Checking the raw kg would only
    # ever find the base class and silently drop every precoordinated leaf
    # from the scaffold. Broad classes are kept alongside the recovered
    # leaves (not replaced) since op_knowledge.py's own device-identity
    # lookup still resolves to the broad class until it gets the same fix.
    reasoned = Graph()
    for g in (onto, vocab, kg):
        for t in g:
            reasoned.add(t)
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(reasoned)
    seen = {c: cls for c, cls in concept_class.items()
            if (None, RDF.type, c) in reasoned}

    # Bind each type concept's scheme namespace for readable output
    for concept in seen:
        ns = str(concept).rsplit("/", 1)[0] + "/"
        out.bind(ns.rstrip("/").rsplit("/", 1)[-1], Namespace(ns))

    count = 0
    for concept, cls in sorted(seen.items(), key=lambda kv: (_local(kv[1]), str(kv[0]))):
        ent = SCAFFOLD[f"{_local(cls)}_{_local(concept)}"]
        label = next((l for l in vocab.objects(concept, SKOS.prefLabel)
                      if getattr(l, "language", None) == "en"), None)
        # Base knowledge only: the type's rdf:type, and a human label.
        out.add((ent, RDF.type, concept))
        out.add((ent, RDFS.label,
                 Literal(f"{label or _local(concept)} (type catalogue entry)", lang="en")))
        count += 1

    header = (
        "# Operational type catalogue — GENERATED by entity_seed.py\n"
        "#\n"
        "# One stable scaffold: IRI per individuated particular TYPE in the alarm-\n"
        "# type catalogue.  Each carries only base knowledge (rdf:type + label).\n"
        "#\n"
        "# NOT a store of usable particulars — op_knowledge.py mints owner-scoped\n"
        "# entity: IRIs at grounding time (see sensor_iri/signal_iri) and consults\n"
        "# this catalogue only to validate a concept and read its label.  See the\n"
        "# module docstring in entity_seed.py for why scaffold: and entity: are\n"
        "# kept as separate namespaces.\n"
        "#\n"
        "# Regenerated from scratch on every run — do not hand-edit; author\n"
        "# real-world facts in a separate file or downstream of minting.\n\n"
    )
    body = out.serialize(format="turtle")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(header + body, encoding="utf-8")

    by_class = {}
    for concept, cls in seen.items():
        by_class.setdefault(_local(cls), 0)
        by_class[_local(cls)] += 1
    print(f"[entities] {count} type-catalogue entr(y/ies) → {OUT.name}")
    for c, n in sorted(by_class.items()):
        print(f"           {n:2d}  {c}")


if __name__ == "__main__":
    main()
