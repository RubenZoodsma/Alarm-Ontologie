"""
stream.py — the stream source (RSP-QL: the input stream).

Reads an alarm corpus (`;`-separated: patientID;label;device_id;start;end
[;alarm_id]) into Event records, and replays them as the stream the
engine consumes: separate AlarmArrival and AlarmEnd elements, in time
order. Time is LOGICAL, not wall-clock.

An AlarmArrival carries no end: nothing downstream can know when an alarm
will end before its AlarmEnd arrives, exactly as with a live feed. A live
source would emit the same two element types as they happen.

ORDER AT ONE INSTANT t (replay_stream). All AlarmEnds at t come before the
AlarmArrivals at t: an alarm that ended at t is no longer active for one
arriving at t. An alarm's end never precedes its own arrival: a zero-length
alarm (end == start; 32,391 in the corpus) ends right after its own
arrival, before any later arrival at t; an end before the start (6 in the
corpus) is treated as zero-length. Arrivals at t keep their input order.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from paths import ROOT

DATASET = ROOT / "DATA" / "CAT_evaluation" / "events_data.csv"


@dataclass
class Event:
    patient: str
    label: str
    device_id: str
    start: datetime
    end: datetime
    # ALARM_ID: this alarm occurrence's own unique identifier, part of its
    # IRI (mint.alarm_key). Taken from the file's `alarm_id` column when
    # present (the corpus: the row number in the locked source data, see
    # data/tools/export_rdata.R), otherwise the 1-based data-row number in
    # the file being read — unique and stable for as long as that file is.
    alarm_id: str


def _alarm_ids(header: list, rows: list) -> list:
    """The ALARM_ID of each data row: its `alarm_id` column, or its
    1-based data-row number when the file has none."""
    if "alarm_id" in header:
        col = header.index("alarm_id")
        return [row[col] for row in rows]
    return [str(i) for i in range(1, len(rows) + 1)]


def load_events(path: Path) -> list:
    with path.open(encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        rows = [row for row in reader if row]
    return [Event(row[0], row[1], row[2], datetime.fromisoformat(row[3]), datetime.fromisoformat(row[4]),
                  alarm_id)
            for row, alarm_id in zip(rows, _alarm_ids(header, rows))]


def group_by_patient(events: list) -> dict:
    groups: dict = {}
    for e in events:
        groups.setdefault(e.patient, []).append(e)
    return groups


def scan_patient_ids(path: Path) -> list:
    """Every distinct patientID in a large events CSV, without building a
    single Event. Uses pandas (a lazy import — the only place in this
    module that needs a dependency beyond the standard library) reading
    just the patientID column: pandas' C parser only tokenizes the one
    column asked for, whereas a plain csv.reader loop still pays full
    per-row tokenization cost for every column even when only row[0] is
    read — confirmed directly the naive version of this function (a
    csv.reader loop reading only row[0]) still took ~34s against pandas'
    ~7s on the real 13.8M-row corpus, because tokenizing all 5 columns
    per row, not date-parsing, is what actually dominates at that scale."""
    import pandas as pd
    ids = pd.read_csv(path, sep=";", usecols=["patientID"], dtype=str)
    return sorted(ids["patientID"].unique())


def load_events_for_patients(path: Path, patient_ids: set) -> list:
    """Build Event objects ONLY for rows whose patientID is in
    `patient_ids`. Also pandas-based, for the same reason as
    scan_patient_ids: reads the whole file (pandas has no way to skip
    rows before parsing them, so this cost doesn't shrink with a smaller
    `patient_ids`), but its C parser does that full read far faster than
    a Python-level csv.reader loop does even when the loop itself skips
    most rows — confirmed directly (~26s pandas vs ~28s csv.reader
    despite the csv.reader version constructing far fewer Event objects)
    on the real 13.8M-row corpus. Pair with scan_patient_ids to choose
    `patient_ids` first."""
    import pandas as pd
    df = pd.read_csv(path, sep=";", dtype=str)
    if "alarm_id" not in df.columns:
        df["alarm_id"] = [str(i) for i in range(1, len(df) + 1)]  # before filtering, like load_events
    df = df[df["patientID"].isin(patient_ids)]
    return [
        Event(row.patientID, row.label, row.device_id,
              datetime.fromisoformat(row.start), datetime.fromisoformat(row.end), row.alarm_id)
        for row in df.itertuples(index=False)
    ]


@dataclass(frozen=True)
class AlarmArrival:
    """An alarm, as known when it arrives: no end."""
    patient: str
    label: str
    device_id: str
    start: datetime
    alarm_id: str

    @property
    def time(self) -> datetime:
        return self.start


@dataclass(frozen=True)
class AlarmEnd:
    """An alarm's end, as a stream element of its own."""
    patient: str
    alarm_id: str
    time: datetime


def replay_stream(events: list) -> list:
    """Every event as an AlarmArrival and an AlarmEnd, in stream order (see
    the module docstring for the order at one instant)."""
    keyed = []
    for seq, e in enumerate(sorted(events, key=lambda ev: ev.start)):
        keyed.append(((e.start, 1, seq, 0), AlarmArrival(e.patient, e.label, e.device_id, e.start, e.alarm_id)))
        if e.end > e.start:
            keyed.append(((e.end, 0, seq, 0), AlarmEnd(e.patient, e.alarm_id, e.end)))
        else:  # zero-length: right after its own arrival
            keyed.append(((e.start, 1, seq, 1), AlarmEnd(e.patient, e.alarm_id, e.start)))
    return [element for _, element in sorted(keyed, key=lambda k: k[0])]
