"""
op_inference.py — time-advancing operational alarm simulation.

Reads events_data.csv, groups per patient, and advances a clock across each
patient's monitoring window (min start … max alarm-end + post-alarm window).
The operational layer ACCUMULATES the knowledge that alarms reveal and NOTES
the situational awareness at each tick — it does not re-state base knowledge.

Output — kg_inference.trig:
  • default graph  : the accumulated background — minted patients/devices and
                     the structure discovered from the alarm stream, stated
                     once.  (Entity types already in entities.ttl are referenced,
                     not repeated.)
  • one named graph per tick : the situational awareness at that instant —
                     which alarms are active (each carrying its full message by
                     isOfType reference to the archetype), which operational
                     states currently hold, and the clinical context those
                     alarms imply.  Idle ticks are empty.

Temporal model (from the ontology):
  • An alarm's situational facts are valid on [start, end].
  • Operation-state facts inferred during an alarm persist for the
    PostAlarmValidScheme window (currently 15 min — see ontology.ttl's
    mda:postAlarmValidityDuration, the single source of this value) after
    alarm-end, then decay.
  • Clinical context is valid only while the alarm is active.  The anatomy
    itself is timeless, but it hangs off the alarm's metric node, so this
    alarm's implication of it ends with the alarm.

Knowledge extraction, minting and inference live in op_knowledge.py.

events_data.csv format:  patientID | label | device_id | start | end

Usage
-----
  python3 op_inference.py
"""

import sys
from pathlib import Path

from rdflib import Dataset, Graph, URIRef, Literal
from rdflib.namespace import RDF, XSD, OWL
import owlrl

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
import op_knowledge as K

OUT = K.ROOT / "EVALUATION" / "MDA-POC" / "kg_inference.trig"


def check_device_exclusivity(kb: K.KB, events: list) -> None:
    """
    Hard-fail before running the simulation if any two DIFFERENT patients'
    events claim the same device_id at overlapping times.

    events_data.csv is normally produced by events_seed.py's occupancy-
    tracked device pool (allocate_devices), which cannot generate such an
    overlap — this check exists for the case that matters more: hand-
    authored, real event data, where a double-booking is a genuine data-
    quality bug. mda:isMonitoredBy is declared owl:InverseFunctionalProperty
    (ontology.ttl) precisely so this is catchable by a reasoner, not just by
    generator discipline — so for every interval overlap found, this reasons
    over a minimal graph (both isMonitoredBy assertions, the ontology, and an
    explicit owl:differentFrom between the two patients) and confirms the
    conflict is a genuine OWL inconsistency (an entailed owl:sameAs against
    an asserted owl:differentFrom), the same mechanism a live reasoner would
    hit on real data — not interval arithmetic dressed up as a reasoner
    check.
    """
    by_device = {}
    for e in events:
        by_device.setdefault(e.device_id, []).append(e)

    conflicts = []
    for device_id, evs in by_device.items():
        evs = sorted(evs, key=lambda e: e.start)
        for i in range(len(evs)):
            for j in range(i + 1, len(evs)):
                a, b = evs[i], evs[j]
                if a.patient == b.patient:
                    continue
                if a.start < b.end and b.start < a.end:      # overlap
                    conflicts.append((device_id, a, b))

    if not conflicts:
        return

    print(f"[FATAL] {len(conflicts)} device double-booking conflict(s):")
    for device_id, a, b in conflicts:
        pa, pb = K.patient_iri(a.patient), K.patient_iri(b.patient)
        dev = K.device_iri(device_id)
        g = Graph()
        g += kb.reasoning_static
        g.add((pa, K.MDA.isMonitoredBy, dev))
        g.add((pb, K.MDA.isMonitoredBy, dev))
        g.add((pa, OWL.differentFrom, pb))
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
        confirmed = (pa, OWL.sameAs, pb) in g or (pb, OWL.sameAs, pa) in g
        verdict = "reasoner confirms INCONSISTENT" if confirmed else "reasoner did NOT flag this — investigate"
        print(f"  device={device_id}  {a.patient} [{a.start}–{a.end}]  vs  "
              f"{b.patient} [{b.start}–{b.end}]  ->  {verdict}")
    raise SystemExit(1)


def main() -> None:
    kb = K.load_kb()
    events = K.load_events()
    groups = K.group_by_patient(events)

    unknown = sorted({e.label for e in events if e.label not in kb.type_index})
    if unknown:
        print(f"[WARN]  {len(unknown)} label(s) with no AlarmType — skipped: {unknown}")

    check_device_exclusivity(kb, events)

    print(f"[events] {len(events)} event(s), {len(groups)} patient(s); "
          f"post-alarm window = {int(kb.window.total_seconds())}s")

    ds = Dataset()
    ds.bind("entity", K.ENTITY); ds.bind("inst", K.INST); ds.bind("mda", K.MDA)
    ds.bind("opstate", K.OPSTATE); ds.bind("snapshot", K.SNAP)

    # Patients are asserted pairwise distinct once, in the shared background:
    # a real-world fact (these are different people) that is what makes
    # mda:isMonitoredBy's InverseFunctionalProperty constraint meaningful to
    # a reasoner over the exported file — without it, a violation would
    # silently merge two patient IRIs instead of contradicting anything.
    patients = sorted(groups)
    for i in range(len(patients)):
        for j in range(i + 1, len(patients)):
            ds.add((K.patient_iri(patients[i]), OWL.differentFrom, K.patient_iri(patients[j])))

    # Shared across every patient in this run — see K.build_timeline's
    # reasoning_cache docstring; a (label, device_id) pair reasoned about
    # for one patient is reused for the next patient referencing the same
    # device_id.
    reasoning_cache = {}

    total = 0
    for patient, evs in groups.items():
        tl = K.build_timeline(kb, patient, evs, reasoning_cache=reasoning_cache)

        # ── Accumulated background (stated once, into the default graph) ──────
        for tr in tl.background:
            ds.add(tr)

        print(f"\n[patient {patient}] {len(evs)} event(s), {len(tl.ticks)} tick(s), "
              f"{tl.ticks[0]:%H:%M:%S} … {tl.ticks[-1]:%H:%M:%S}")

        for t in tl.ticks:
            situation = tl.situation_at(t)

            gname = URIRef(K.SNAP[f"{patient}_{t.strftime('%Y%m%dT%H%M%S')}"])
            ng = ds.graph(gname)
            for tr in situation:
                ng.add(tr)
            ds.add((gname, RDF.type, K.SNAP.Snapshot))
            ds.add((gname, K.SNAP.atTime, Literal(t.isoformat(), datatype=XSD.dateTime)))
            ds.add((gname, K.SNAP.forPatient, K.patient_iri(patient)))

            enabled_ents = len(set(situation.subjects(K.MDA.hasOperationState, None)))
            state = tl.state_label(t, situation)
            print(f"  {t:%H:%M:%S}  [{state:<18}] active-alarms={len(tl.active_at(t))}  "
                  f"enabled-entities={enabled_ents}  note-triples={len(situation)}")
            total += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ds.serialize(destination=str(OUT), format="trig")
    print(f"\n[output] background + {total} tick note(s) → {OUT.name}")


if __name__ == "__main__":
    main()
