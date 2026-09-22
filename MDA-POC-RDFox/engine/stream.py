"""
stream.py — the input stream (RSP-QL).

Reads an alarm corpus (`;`-separated: patientID;label;device_id;start;end
[;alarm_id]) and replays it as AlarmArrival and AlarmEnd elements in
logical time, as a live feed would deliver them. An arrival carries no end.

Order at one instant t: all ends at t, then the arrivals at t in input
order. A zero-length alarm (or one ending before it starts) ends right
after its own arrival.
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
    # ALARM_ID, part of the alarm IRI: the file's `alarm_id` column (the
    # corpus: its row in the locked source), else the 1-based row number.
    alarm_id: str


def _alarm_ids(header: list, rows: list) -> list:
    """The ALARM_ID of each data row: its `alarm_id` column, or its
    1-based data-row number when the file has none."""
    if "alarm_id" in header:
        col = header.index("alarm_id")
        return [row[col] for row in rows]
    return [str(i) for i in range(1, len(rows) + 1)]


def load_events(path: Path) -> list:
    """Every event in a (small) events file."""
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
    """Every distinct patientID, reading only that column (pandas; ~7 s on
    the full corpus)."""
    import pandas as pd
    ids = pd.read_csv(path, sep=";", usecols=["patientID"], dtype=str)
    return sorted(ids["patientID"].unique())


def load_events_for_patients(path: Path, patient_ids: set) -> list:
    """Events of `patient_ids` only (pandas). Row-number ALARM_IDs are
    assigned before filtering, as in load_events."""
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
    """Every event as an AlarmArrival and an AlarmEnd, in stream order."""
    keyed = []
    for seq, e in enumerate(sorted(events, key=lambda ev: ev.start)):
        keyed.append(((e.start, 1, seq, 0), AlarmArrival(e.patient, e.label, e.device_id, e.start, e.alarm_id)))
        if e.end > e.start:
            keyed.append(((e.end, 0, seq, 0), AlarmEnd(e.patient, e.alarm_id, e.end)))
        else:  # zero-length: right after its own arrival
            keyed.append(((e.start, 1, seq, 1), AlarmEnd(e.patient, e.alarm_id, e.start)))
    return [element for _, element in sorted(keyed, key=lambda k: k[0])]
