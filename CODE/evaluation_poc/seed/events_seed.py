"""
events_seed.py — synthetic alarm stream for load and concurrency testing.

`events_data.csv` is normally an AUTHORED input: real alarm events observed on
real patients.  This generator exists only to produce a realistic synthetic
stream at a volume worth stress-testing — it is not part of the pipeline, and
nothing downstream depends on it.  Re-running it OVERWRITES events_data.csv.

What it aims for, and why:

  BURSTY, NOT UNIFORM.  ICU alarms arrive in episodes — a patient is turned,
  suctioned, or deteriorates, and several alarms fire within a minute or two.
  Spreading N alarms uniformly over 12 h would give almost no concurrency and
  would not exercise the merge at all.  Alarms are therefore clustered into
  episodes, with a thinner background of isolated alarms between them.

  ONE DEVICE PER TYPE, NOT PER PATIENT.  The archetype decides a device's
  type, so a ventilator alarm must not be attributed to a monitor's
  device_id — that would type the same entity as both.  But a device_id must
  also not be a function of the patient: a device is not possessed by the
  patient it happens to be monitoring (mda:isMonitoredBy expresses that
  relationship separately, per event, and it changes over time as a real
  physical unit moves between patients — see FOUNDATIONS.md §3's
  owner-scoping rule). Device ids are minted per device type alone, read from
  the catalogue rather than hard-coded, so the same id is naturally reused
  whenever two patients' streams need the same device type — exactly the
  case a patient-keyed id made unrepresentable.

  NOT EVERY PATIENT IS VENTILATED.  One of the three is monitor-only, so the
  multi-device path and the single-device path are both exercised.

Every tick runs an OWL reasoner, and a tick is generated per alarm start, end,
and post-alarm boundary — so cost grows with alarm count.  The default is a
small dense cluster that stays interactive; pass larger numbers for load tests.

The catalogue of (label, device_type) pairs is read from FRAMEWORK/
KNOWLEDGE_BASE/kg_generated.ttl (see op_knowledge.py's path-block comment)
— so a synthetic stream always draws only from alarm archetypes the
simulation can actually resolve.

Usage
-----
  python3 events_seed.py                    27 alarms over 45 min (default)
  python3 events_seed.py 600 720            600 alarms over 12 h (load test)
  python3 events_seed.py [n] [minutes] [seed]
"""

import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from rdflib import Graph, Namespace
from rdflib.namespace import RDF

OP_DIR = Path(__file__).resolve().parent
CODE_DIR = OP_DIR.parent.parent        # CODE/evaluation_poc/seed -> evaluation_poc -> CODE
ROOT = CODE_DIR.parent                 # repo root
sys.path.append(str(CODE_DIR / "shared"))
from ontology_tree import derive_class_tree, walk_instances

FRAMEWORK_DIR = ROOT / "FRAMEWORK"
ONTOLOGY = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
CATALOGUE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
OUT = ROOT / "DATA" / "POC_EVENTS" / "events_data.csv"

MDA = Namespace("https://w3id.org/mda/ontology#")

DAY = datetime(2026, 7, 17, 8, 0, 0)       # start of the window
ALARM_MIN, ALARM_MAX = 10, 30              # seconds

# Patients, and whether they are ventilated.  A monitor-only patient never
# receives MechanicalVentilator alarms.
PATIENTS = [
    ("johnDoe_01",  True),
    ("janeRoe_02",  False),
    ("alexKim_03",  True),
]

# Relative frequency — desaturation and artefact alarms dominate real streams;
# rectal temperature is rare.  Keys are matched as substrings of the label.
WEIGHTS = {
    "Desaturatie":   6, "SpO2l grillig": 6, "ECG ruissignaal": 6,
    "Resp grillig":  5, "SpO2po laag":   4, "SpO2pr hoog":     3,
    "NiBDm hoog":    3, "Lage hartfreq.": 3, "AoM hoog":       2,
    "AoM laag":      2, "AoS hoog":       2, "AoS laag":       2,
    "ABPd hoog":     2, "PEEP hoog":      3, "Trect hoog":     1,
}


def _local(iri) -> str:
    return re.split(r"[#/]", str(iri))[-1]


def load_catalogue():
    """
    (label, device_type) for every alarm archetype, read from FRAMEWORK/
    KNOWLEDGE_BASE/kg_generated.ttl. device_type is the archetype's Device node's concrete concept
    (e.g. 'MechanicalVentilator_ServoU_Getinge' for a precoordinated type),
    found by walking the same ontology-derived class tree op_knowledge.py
    uses — so this cannot drift from what the simulation actually resolves.
    An archetype with no Device branch is skipped (device_type is required
    to mint a device_id below).
    """
    onto = Graph(); onto.parse(ONTOLOGY, format="turtle")
    kg = Graph(); kg.parse(CATALOGUE, format="turtle")
    tree = derive_class_tree(onto, MDA.Alarm)

    catalogue = []
    for type_iri in kg.subjects(RDF.type, MDA.AlarmType):
        label = next(kg.objects(type_iri, MDA.hasLabel), None)
        if label is None:
            continue
        nodes = walk_instances(kg, tree, type_iri, MDA.Alarm)
        device_node = nodes.get(MDA.Device)
        if device_node is None:
            continue
        device_concept = next(kg.objects(device_node, RDF.type), None)
        if device_concept is None:
            continue
        catalogue.append((str(label).strip(), _local(device_concept)))
    return catalogue


def weight_of(label: str) -> int:
    for key, w in WEIGHTS.items():
        if key in label:
            return w
    return 2


def device_id(device_type: str, unit: int) -> str:
    """
    One physical device per (exact device concept, unit number) —
    MechanicalVentilator_ServoU_Getinge unit 0 → MechanicalVentilatorServoUGetinge_00.
    A function of device_type and unit alone: a device's identity is its own,
    never its current patient's (see the module docstring's ONE DEVICE PER
    TYPE, NOT PER PATIENT note, and op_knowledge.py's sensor_iri/signal_iri,
    which correctly key on device_id because a sensor genuinely is possessed
    by its device — a relationship this one is not).

    device_type is the archetype's precoordinated Device concept — base
    class plus manufacturer/model refinement (see build_framework.py's
    precoordinate()), e.g. 'PhysiologicalMonitor_Philips' vs
    'PhysiologicalMonitor_Infinity_Draeger'. Keying on anything coarser
    than the full concept collapses distinct devices onto one device_id:
    a vent/mon binary merges a monitor with an ECMO circuit; even the bare
    base category ('PhysiologicalMonitor') still merges a Philips monitor
    with a Dräger one. op_knowledge.py's device_iri() trusts device_id as
    the device's identity and types the resulting entity with whatever
    concept(s) it sees asserted on that id — collapse two concepts onto
    one id and the entity comes out typed as both at once, which
    op_visual.py then renders as one physically impossible device.

    `unit` distinguishes multiple physical devices of the same exact type in
    the simulated fleet — see allocate_devices, which decides how many units
    of a type actually get minted and which alarms share which unit.
    """
    tag = re.sub(r"[^A-Za-z0-9]+", "", device_type)
    return f"{tag}_{unit:02d}"


def allocate_devices(rows: list) -> list:
    """
    Assign a concrete unit to each (patient, label, device_type, start, end)
    row: reuse a free unit of that type where one exists, mint a new one only
    when every existing unit of that type is occupied at that row's time —
    "reuse when free, seed more when all are occupied."

    Zero tolerance for overlap: a unit is free only if NONE of its already-
    booked intervals touch [start, end], so the assignment this produces
    never violates mda:isMonitoredBy's owl:InverseFunctionalProperty
    constraint (ontology.ttl) — the device pool is conflict-free by
    construction, not merely by luck. (op_inference.py's reasoner-side check
    exists for data this generator did not produce, e.g. hand-authored
    events_data.csv, not because this function is expected to fail.)

    `rows` must already be in chronological (start) order: allocation is one
    greedy left-to-right sweep, so earlier alarms claim the lowest-numbered
    units first and later ones reuse or overflow from there.
    """
    booked = defaultdict(list)    # (device_type, unit) -> [(start, end), ...]
    next_unit = defaultdict(int)  # device_type -> units minted so far

    def free_unit(device_type, start, end):
        for unit in range(next_unit[device_type]):
            if all(end <= s or e <= start for s, e in booked[(device_type, unit)]):
                return unit
        unit = next_unit[device_type]
        next_unit[device_type] += 1
        return unit

    out = []
    for patient, lbl, device_type, start, end in rows:
        unit = free_unit(device_type, start, end)
        booked[(device_type, unit)].append((start, end))
        out.append((patient, lbl, device_id(device_type, unit), start, end))
    return out


def generate(n_target: int, horizon: float, seed: int) -> list:
    """
    `n_target` alarms across the patients, within `horizon` seconds.

    The budget is honoured exactly: episodes are sized to what is left rather
    than to a fixed count, so the same generator produces a 25-alarm cluster for
    interactive work and a 600-alarm day for load testing.
    """
    rng = random.Random(seed)
    catalogue = load_catalogue()
    per_patient = max(n_target // len(PATIENTS), 1)
    rows = []

    for patient, ventilated in PATIENTS:
        pool = [(lbl, dev) for lbl, dev in catalogue
                if ventilated or "Ventilator" not in dev]
        weights = [weight_of(lbl) for lbl, _ in pool]

        def emit(offset):
            lbl, dev = rng.choices(pool, weights=weights, k=1)[0]
            dur = rng.randint(ALARM_MIN, ALARM_MAX)
            # dev is still the device_type here — allocate_devices resolves it
            # to a concrete unit once every patient's absolute times are known.
            rows.append((patient, lbl, dev, offset, dur))

        # ── Episodes: dense bursts, the source of concurrency ─────────────
        # Most of the budget goes here; episodes are short relative to the
        # horizon so alarms inside one genuinely overlap.
        budget = per_patient
        in_episodes = max(int(budget * 0.8), 1)
        while in_episodes > 0:
            size = min(rng.randint(4, 7), in_episodes)
            # An episode must be short relative to alarm duration or its alarms
            # never overlap: a few times ALARM_MAX, not a fraction of the horizon.
            ep_len = rng.uniform(ALARM_MAX * 1.5, ALARM_MAX * 3)
            ep_start = rng.uniform(0, max(horizon - ep_len, 1))
            for _ in range(size):
                emit(ep_start + rng.uniform(0, ep_len))
            in_episodes -= size
            budget -= size

        # ── Background: isolated alarms filling out the count ─────────────
        for _ in range(max(budget, 0)):
            emit(rng.uniform(0, max(horizon - 60, 1)))

    # Absolute times, chronological
    out = []
    for patient, lbl, dev, off, dur in rows:
        start = DAY + timedelta(seconds=round(off))
        out.append((patient, lbl, dev, start, start + timedelta(seconds=dur)))
    out.sort(key=lambda r: (r[3], r[0]))

    # Resolve device_type -> concrete unit only now that every row's absolute
    # time is known and rows are chronological (see allocate_devices).
    return allocate_devices(out)


def concurrency_report(rows):
    """Peak and mean simultaneous alarms per patient, via a sweep line."""
    by_patient = {}
    for p, _, _, s, e in rows:
        by_patient.setdefault(p, []).append((s, e))
    report = {}
    for p, spans in by_patient.items():
        edges = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
        cur = peak = 0
        overlapped = 0
        for _, d in edges:
            cur += d
            peak = max(peak, cur)
            if cur > 1:
                overlapped += 1
        report[p] = (len(spans), peak, overlapped)
    return report


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 27
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 45
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 17
    rows = generate(n, minutes * 60, seed)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write("patientID;label;device_id;start;end\n")
        for p, lbl, dev, s, e in rows:
            f.write(f"{p};{lbl};{dev};{s.isoformat()};{e.isoformat()}\n")

    span = (rows[-1][4] - rows[0][3])
    print(f"[events] {len(rows)} alarm(s) over {span}, seed={seed} → {OUT.name}")
    print(f"{'patient':<14}{'alarms':>8}{'peak concurrent':>18}")
    for p, (n_, peak, _) in sorted(concurrency_report(rows).items()):
        print(f"  {p:<12}{n_:>8}{peak:>18}")
    devices = sorted({r[2] for r in rows})
    print(f"[devices] {len(devices)}: {', '.join(devices)}")


if __name__ == "__main__":
    main()
