"""
check_label_coverage.py — does the ontology's archetype catalogue actually
match what this dataset's alarm labels look like, character for character?

Two failure modes look identical from the outside (a catalogued label with
zero occurrences in the dataset) but need opposite fixes:
  (a) a genuine string mismatch — encoding, punctuation, whitespace — between
      how a label was annotated and how it's actually written in this export.
      This is a bug: the archetype silently never fires against real data.
  (b) the equipment that alarm type describes is simply not deployed in this
      dataset (different manufacturer, different device generation, or an
      alarm subtype that happens never to have fired in this extraction
      window). Not a bug — the catalogue was built to be reusable beyond any
      one unit's specific equipment roster (see FOUNDATIONS.md/Section 4.1's
      framing: informed by SNOMED CT/IEEE SDC, not restricted to one site).

Distinguishing them matters before assuming a coverage gap needs fixing:
running this against DATA_LOCKED.rData's export (2026-08-27) found 27
catalogued labels with no exact match, of which exactly ONE was case (a) —
"MEDIBUS - Setting alarm limit or ventilation mode changed" vs the raw
"MEDIBUS - Setting, alarm limit or ventilation mode changed" (missing comma)
— and the other 26 were case (b): CARDIOHELP/EDWARDS (adult-ICU devices not
used on this PICU/NICU unit), nine Draeger Infinity monitor labels (this
corpus's monitors are all Philips — zero Draeger/Infinity occurrences
anywhere, confirmed against device_type/device_naam and the raw label text
itself), and SERVOI-prefixed labels (a different Getinge ventilator
generation from the SERVOU units this corpus actually has).

Usage
-----
  python3 check_label_coverage.py path/to/events_data.csv
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
import op_knowledge as K


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.lower()
    s = re.sub(r"[.:;,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} path/to/events_data.csv")
        raise SystemExit(1)

    kb = K.load_kb()
    catalogued = sorted(kb.type_index.keys())

    events = K.load_events(Path(sys.argv[1]))
    label_counts = Counter(e.label for e in events)
    raw_labels = set(label_counts)

    exact_hit = [c for c in catalogued if c in raw_labels]
    exact_miss = [c for c in catalogued if c not in raw_labels]

    matched_volume = sum(label_counts[c] for c in exact_hit)
    total_volume = sum(label_counts.values())

    print(f"catalogued archetypes:      {len(catalogued)}")
    print(f"distinct labels in dataset: {len(raw_labels)}")
    print(f"exact-match archetypes:     {len(exact_hit)}/{len(catalogued)}")
    print(f"volume covered by exact-match archetypes: "
          f"{matched_volume}/{total_volume} ({matched_volume / total_volume:.1%})")

    if not exact_miss:
        return

    print(f"\n{len(exact_miss)} catalogued labels with no exact match:\n")
    raw_norm = {normalize(r): r for r in raw_labels}
    for c in exact_miss:
        n = normalize(c)
        near = raw_norm.get(n)
        if near:
            print(f"  NEAR-MISS   {c!r}\n              raw has {near!r} — likely a real formatting bug")
            continue
        suffix = c.split(" - ", 1)[-1] if " - " in c else c
        suffix_n = normalize(suffix)
        candidates = sorted(r for r in raw_labels if normalize(r).endswith(suffix_n) and len(suffix_n) > 4)
        if candidates:
            print(f"  PREFIX-DIFF {c!r}\n              raw candidates: {candidates} — check whether this is the same device under a different name")
        else:
            print(f"  ABSENT      {c!r} — no similar label in this dataset at all; likely equipment this dataset doesn't have")


if __name__ == "__main__":
    main()
