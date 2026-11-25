"""
Shared layout of the annotation workbook (export_annotation_xlsx.py writes it,
import_annotation_xlsx.py reads it back). Kept in one place so the two cannot
drift apart.
"""

import csv
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSENSUS_DIR = ROOT / "DATA" / "ANNOTATION" / "consensus"
CORPUS_DIR = ROOT / "DATA" / "CORPUS"
MODELS = ROOT / "DATA" / "ANNOTATION" / "device_models.csv"
VOCAB_FILES = [ROOT / "FRAMEWORK" / "VOCABULARY" / "seed" / "vocab_base.ttl",
               ROOT / "FRAMEWORK" / "VOCABULARY" / "vocab_generated.ttl"]
VOCAB_NS = "https://w3id.org/mda/vocab/"

SHEET_DATA, SHEET_VOCAB, SHEET_HELP, SHEET_META = "Annotation", "Vocabulary", "Instructions", "_meta"
HEADER_ROW, FIRST_ROW = 2, 3          # row 1: column groups; row 2: CSV column names
ID_COL = "_id"                        # hidden: row index in the source CSV
IDENTITY = ["alarmLabel", "alarmPriority", "Device.Type"]   # blueprint identity: locked
CONTEXT = ["Gateway device", "Department(s)", "Alarm count", "Status"]   # read-only, not exported
REVIEW = "reviewStatus"
COMMENT = "Annotator comment"         # free text, written to a side file on import
REVIEW_VALUES = ["x", "EXCL", "REVIEW", "REVIEW-PRIO"]

# CSV column -> vocabulary scheme (same mapping as COLUMN_SCHEMES in build_framework.py)
SCHEME = {
    "alarmCategory": "alarm-category", "Device": "device", "Device.Manufacturer": "manufacturer",
    "Device.OperationalState": "operation-state", "Component": "component",
    "Component.OperationalState": "operation-state", "FunctionalUnit": "functional-unit",
    "Sensor": "sensor", "Sensor.OperationalState": "operation-state",
    "Sensor.AnatomicalPosition": "anatomical-position", "Sensor.Laterality": "laterality",
    "Signal": "signal", "Signal.QualityState": "quality-state", "Metric": "metric",
    "Metric.Phase": "metric-phase", "Metric.Aggregation": "metric-aggregation",
    "Metric.ValueState": "metric-value-state", "Metric.Rhythm": "metric-rhythm",
    "TherapeuticModality": "therapeutic-modality",
    "PhysiologicalProperty": "physiological-property",
    "PhysiologicalProcess": "physiological-process",
}


def norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def key(label, priority, model) -> tuple:
    """Blueprint identity, normalised as build_framework.blueprint_key."""
    return (norm(model), norm(label), norm(priority))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    return rows[0], rows[1:]


def write_csv(path: Path, header, rows):
    """Format of the consensus files: ';', UTF-8 without BOM, CRLF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter=";", lineterminator="\r\n").writerows([header] + rows)
