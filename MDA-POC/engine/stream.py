"""
stream.py — the input stream (RSP-QL).

Reads an alarm corpus (`;`-separated, by header name: patientID;label;
device_id;start;end, plus device_name;priority and optionally alarm_id) and
replays it as AlarmArrival and AlarmEnd elements in
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
    # With the label, they identify the alarm's blueprint (device model +
    # label + priority): device_name is the gateway's device name (e.g.
    # Caleo), mapped to the model through DATA/ANNOTATION/device_models.csv.
    # Empty means unknown: the alarm then matches no blueprint.
    device_name: str = ""
    priority: str = ""


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
    col = {name: i for i, name in enumerate(header)}
    get = lambda row, name: row[col[name]] if name in col else ""
    return [Event(get(row, "patientID"), get(row, "label"), get(row, "device_id"),
                  datetime.fromisoformat(get(row, "start")), datetime.fromisoformat(get(row, "end")),
                  alarm_id, get(row, "device_name"), get(row, "priority"))
            for row, alarm_id in zip(rows, _alarm_ids(header, rows))]


def group_by_patient(events: list) -> dict:
    groups: dict = {}
    for e in events:
        groups.setdefault(e.patient, []).append(e)
    return groups


def partition_by_patient(path: Path, chunks: list, out_dir: Path) -> list:
    """Split `path` into one file per chunk of patient ids (`chunks`: lists
    of ids), in ONE streaming pass, so a run over the full corpus never
    holds more than one chunk in memory. Each file keeps the header, the
    rows of its patients in input order (the stream's order at one instant
    depends on it), and their ALARM_IDs: the `alarm_id` column, else the
    1-based data-row number over the whole file, as load_events assigns it.
    Splits the input only; nothing is computed from it. Returns the paths,
    one per chunk."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_of = {pid: k for k, ids in enumerate(chunks) for pid in ids}
    paths = [out_dir / f"chunk_{k:05d}.csv" for k in range(len(chunks))]
    files = [p.open("w", newline="", encoding="utf-8") for p in paths]
    try:
        writers = [csv.writer(f, delimiter=";") for f in files]
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            header = next(reader)
            has_id = "alarm_id" in header
            out_header = header if has_id else header + ["alarm_id"]
            for w in writers:
                w.writerow(out_header)
            pcol = header.index("patientID")
            number = 0
            for row in reader:
                if not row:
                    continue
                number += 1
                k = chunk_of.get(row[pcol])
                if k is not None:
                    writers[k].writerow(row if has_id else row + [str(number)])
    finally:
        for f in files:
            f.close()
    return paths


def scan_patient_counts(path: Path) -> dict:
    """{patientID: number of alarms}, reading only that column (pandas)."""
    import pandas as pd
    ids = pd.read_csv(path, sep=";", usecols=["patientID"], dtype=str)
    return ids["patientID"].value_counts().to_dict()


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
    for c in ("device_name", "priority"):
        if c not in df.columns:
            df[c] = ""
    df = df.fillna("")
    return [
        Event(row.patientID, row.label, row.device_id,
              datetime.fromisoformat(row.start), datetime.fromisoformat(row.end), row.alarm_id,
              row.device_name, row.priority)
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
    device_name: str = ""
    priority: str = ""

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
        keyed.append(((e.start, 1, seq, 0), AlarmArrival(e.patient, e.label, e.device_id, e.start, e.alarm_id,
                                                         e.device_name, e.priority)))
        if e.end > e.start:
            keyed.append(((e.end, 0, seq, 0), AlarmEnd(e.patient, e.alarm_id, e.end)))
        else:  # zero-length: right after its own arrival
            keyed.append(((e.start, 1, seq, 1), AlarmEnd(e.patient, e.alarm_id, e.start)))
    return [element for _, element in sorted(keyed, key=lambda k: k[0])]
