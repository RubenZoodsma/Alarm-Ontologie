"""
data_graph.py
─────────────
Combines the alarm ontology with the CSV alarm log to apply six semantic rules,
organised in three categories:

  1. Reduction of likely false positives          (FP-1, FP-2)
  2. Reduction of alarms with no additional benefit (NAB-1, NAB-2)
  3. Clinical recalculation of emitted priority    (PR-RA, PR-2)

For each implemented rule the ontology is queried via SPARQL to derive a
semantic mapping; that mapping is then applied to the alarm log using temporal
and attribute-based criteria.  A summary table is printed at the end.

Rules implemented:
  FP-1  Signal-quality sensor masking
  FP-2  Sensor-malfunction invalidation
  NAB-1 Organ-level priority dominance
  NAB-2 Rapid alarm recurrence suppression
  PR-RA Respiratory arrest sequence escalation (A→B→C within 1-hour windows)
  PR-CF Cardiac failure co-occurrence (low HR + low ABP within 1-hour window)
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from rdflib import Graph, Namespace
from rdflib.namespace import RDF, RDFS

# ─── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT   = Path(__file__).resolve().parents[2]
SPARQL_DIR  = Path(__file__).parent / "sparQl_queries"
ONTOLOGY_FILES = [
    REPO_ROOT / "ontology_core" / "alarmOnto_coreFile.ttl",
    REPO_ROOT / "ontology_core" / "alarmOnto_common.ttl",
    REPO_ROOT / "ontology_core" / "alarmOnto_clinical.ttl",
    REPO_ROOT / "ontology_core" / "alarmOnto_device.ttl",
    # Clinical instances
    REPO_ROOT / "ontology_instances" / "clinical_instances" / "1_patient.ttl",
    REPO_ROOT / "ontology_instances" / "clinical_instances" / "2_physiologicalSystem.ttl",
    REPO_ROOT / "ontology_instances" / "clinical_instances" / "3_organ.ttl",
    REPO_ROOT / "ontology_instances" / "clinical_instances" / "4_physiologicalProcess.ttl",
    REPO_ROOT / "ontology_instances" / "clinical_instances" / "5_physiologicalProperty.ttl",
    REPO_ROOT / "ontology_instances" / "clinical_instances" / "6_TherapeuticModalities.ttl",
    # Device instances
    REPO_ROOT / "ontology_instances" / "device_instances" / "1_physicalDevice.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "2_sensorInterface.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "3_sensor.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "4_functionalUnit.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "5_deviceChannel.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "6_Metric.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "7_measurementProcess.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "8_signal.ttl",
    REPO_ROOT / "ontology_instances" / "device_instances" / "9_measurementSite.ttl",
    # Alarm instances
    REPO_ROOT / "ontology_instances" / "alarm_instances" / "1_ACD_MonitorAlarms.ttl",
    REPO_ROOT / "ontology_instances" / "alarm_instances" / "2_ACS_MonitorAlarms.ttl",
    REPO_ROOT / "ontology_instances" / "alarm_instances" / "3_Cond_MonitorAlarms.ttl",
]
CSV_PATH = REPO_ROOT / "Methodology" / "V2" / "DataSample250Pt.csv"

# ─── Namespaces ───────────────────────────────────────────────────────────────

INST   = Namespace("https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/instances#")
DEV    = Namespace("https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/device#")
CLIN   = Namespace("https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/clinical#")
COMMON = Namespace("https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/common#")

# ─── 1. Load ontology ─────────────────────────────────────────────────────────

print("Loading ontology files…")
g = Graph()
for path in ONTOLOGY_FILES:
    if path.exists():
        g.parse(str(path), format="turtle")
        print(f"  Loaded: {path.name}")
    else:
        print(f"  WARNING: not found – {path}")
print(f"  Total triples: {len(g):,}\n")

# ─── 2. Load and prepare CSV ──────────────────────────────────────────────────

print(f"Loading data: {CSV_PATH.name}")
df = pd.read_csv(CSV_PATH)
df["alarm_start"] = pd.to_datetime(df["alarm_start"], format="mixed", errors="coerce").dt.floor("s")
df["alarm_eind"]  = pd.to_datetime(df["alarm_eind"],  format="mixed", errors="coerce").dt.floor("s")
# Strip trailing/leading whitespace that causes silent == mismatches
df["conditie"] = df["conditie"].str.strip()
df = df.sort_values(["patientID", "bed_naam", "alarm_start"]).reset_index(drop=True)

## select only first 100 patients
df = df[df["patientID"].isin(df["patientID"].unique()[:100])].reset_index(drop=True)

N_TOTAL = len(df)


# Derive the set of alarm labels that are modelled in the ontology, then
# restrict the denominator used for %-calculations to only those rows.
_label_query = """
    SELECT DISTINCT ?label
    WHERE { ?acd <https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/device#alarmMessageLabel> ?label . }
"""
ONTOLOGY_ALARM_LABELS: set[str] = {
    str(row.label).strip() for row in g.query(_label_query)
}
N_MODELLED = int(df["conditie"].isin(ONTOLOGY_ALARM_LABELS).sum())

print(
    f"  Rows: {N_TOTAL:,}  |  Patients: {df['patientID'].nunique()}\n"
    f"  Ontology-modelled alarm types : {len(ONTOLOGY_ALARM_LABELS):,}\n"
    f"  Rows with modelled alarm label: {N_MODELLED:,} "
    f"({100 * N_MODELLED / N_TOTAL:.1f}% of total)\n"
)

# ─── 3. Rule application functions ────────────────────────────────────────────

def _apply_fp1_sensor_quality(
        df: pd.DataFrame, g: Graph, sparql_path: Path, params: dict
) -> pd.Series:
    """
    FP-1 — Signal-quality sensor masking
    If a bad-signal technical alarm is active (including a grace window after it
    ends), any physiological alarm on the same sensor with a short duration is
    flagged as a likely false positive.
    """
    grace_sec    = params.get("grace_sec",     30) ## defaults to 30s
    max_phys_sec = params.get("max_phys_sec",  10) ## defaults to 10s

    query_text = sparql_path.read_text(encoding="utf-8")
    pair_results = g.query(query_text)

    tech_to_phys: dict[str, set[str]] = {}
    for row in pair_results:
        tech_to_phys.setdefault(str(row.techLabel).strip(), set()).add(str(row.physLabel).strip())

    if tech_to_phys:
        print("  Sensor-linked pairs (FP-1):")
        for tech, phys_set in sorted(tech_to_phys.items()):
            for p in sorted(phys_set):
                print(f"    [{tech}]  →  [{p}]")
    else:
        print("  No sensor-linked pairs found for FP-1.")

    mask = pd.Series(False, index=df.index)
    for _, group in df.groupby("patientID"):
        tech_rows = group[group["conditie"].isin(tech_to_phys)]
        for _, tech_row in tech_rows.iterrows():
            window_end = tech_row["alarm_eind"] + pd.Timedelta(seconds=grace_sec)
            silenceable = tech_to_phys[tech_row["conditie"]]
            candidates = group[
                group["conditie"].isin(silenceable) &
                (group["alarm_start"] >= tech_row["alarm_start"]) &
                (group["alarm_start"] <= window_end) &
                (group["duur_sec"] < max_phys_sec)
            ]
            mask.loc[candidates.index] = True
    return mask


def _apply_fp2_sensor_malfunction(
        df: pd.DataFrame, g: Graph, sparql_path: Path, params: dict
) -> pd.Series:
    """
    FP-2 — Sensor-malfunction invalidation
    If a sensor-malfunction technical alarm (SensorFaultCondition) is active
    (including a grace window after it ends), any alarm — physiological or
    technical — that shares the same SensorConnectivityCondition is flagged as
    a likely false positive.
    Unlike FP-1, no minimum-duration filter is applied because a hardware fault
    invalidates all co-sensor alarms regardless of their duration.
    """
    grace_sec = params.get("grace_sec", 30)  ## defaults to 30 s

    query_text = sparql_path.read_text(encoding="utf-8")
    pair_results = g.query(query_text)

    malf_to_targets: dict[str, set[str]] = {}
    for row in pair_results:
        malf_to_targets.setdefault(str(row.malfLabel).strip(), set()).add(
            str(row.targetLabel).strip()
        )

    if malf_to_targets:
        print("  Sensor-linked pairs (FP-2):")
        for malf, target_set in sorted(malf_to_targets.items()):
            for t in sorted(target_set):
                print(f"    [{malf}]  →  [{t}]")
    else:
        print("  No sensor-linked pairs found for FP-2.")

    mask = pd.Series(False, index=df.index)
    for _, group in df.groupby("patientID"):
        malf_rows = group[group["conditie"].isin(malf_to_targets)]
        for _, malf_row in malf_rows.iterrows():
            window_end = malf_row["alarm_eind"] + pd.Timedelta(seconds=grace_sec)
            suppressible = malf_to_targets[malf_row["conditie"]]
            candidates = group[
                group["conditie"].isin(suppressible) &
                (group["alarm_start"] >= malf_row["alarm_start"]) &
                (group["alarm_start"] <= window_end)
            ]
            mask.loc[candidates.index] = True
    return mask


def _apply_nab1_organ_priority(
        df: pd.DataFrame, g: Graph, sparql_path: Path, params: dict
) -> pd.Series:
    """
    NAB-1 — Organ-level priority dominance
    While a higher-or-equal-priority alarm is active on a given organ, any
    simultaneously firing alarm on the same organ with lower-or-equal priority
    is flagged as providing no additional benefit.  The dominant alarm is the
    one that started first; the subordinate must start within the dominant's
    active window (alarm_start … alarm_eind).
    """
    query_text = sparql_path.read_text(encoding="utf-8")
    pair_results = g.query(query_text)

    dom_to_subs: dict[str, set[str]] = {}
    for row in pair_results:
        dom_to_subs.setdefault(str(row.domLabel).strip(), set()).add(
            str(row.subLabel).strip()
        )

    if dom_to_subs:
        print("  Organ-priority pairs (NAB-1):")
        for dom, sub_set in sorted(dom_to_subs.items()):
            for s in sorted(sub_set):
                print(f"    [{dom}]  →  [{s}]")
    else:
        print("  No organ-priority pairs found for NAB-1.")

    mask = pd.Series(False, index=df.index)
    for _, group in df.groupby("patientID"):
        dom_rows = group[group["conditie"].isin(dom_to_subs)]
        for dom_idx, dom_row in dom_rows.iterrows():
            suppressible = dom_to_subs[dom_row["conditie"]]
            candidates = group[
                group["conditie"].isin(suppressible) &
                (group["alarm_start"] >= dom_row["alarm_start"]) &
                (group["alarm_start"] <= dom_row["alarm_eind"]) &
                (group.index != dom_idx)
            ]
            mask.loc[candidates.index] = True
    return mask


def _apply_nab2_rapid_recurrence(
        df: pd.DataFrame, g: Graph, sparql_path: Path, params: dict
) -> pd.Series:
    """
    NAB-2 — Rapid alarm recurrence suppression
    For each patient+bed combination, an alarm instance is flagged when it is
    the same type as the immediately preceding instance on that bed AND its
    alarm_start falls within `recurrence_window_sec` after the previous
    alarm_eind.  Only ontology-listed physiological alarm types are considered;
    the very first occurrence in any run is never flagged.
    """
    recurrence_window_sec = params.get("recurrence_window_sec", 10)

    query_text = sparql_path.read_text(encoding="utf-8")
    eligible_labels: set[str] = {
        str(row.alarmLabel).strip() for row in g.query(query_text)
    }

    print(f"  Eligible alarm types for recurrence check: {len(eligible_labels)}")

    window = pd.Timedelta(seconds=recurrence_window_sec)
    mask = pd.Series(False, index=df.index)

    for _, group in df.groupby(["patientID", "bed_naam"]):
        eligible = group[group["conditie"].isin(eligible_labels)]
        for alarm_type, type_group in eligible.groupby("conditie"):
            # type_group is already sorted by alarm_start (df was pre-sorted)
            prev_eind = None
            for idx, row in type_group.iterrows():
                if prev_eind is not None and (row["alarm_start"] - prev_eind) <= window:
                    mask.loc[idx] = True
                prev_eind = row["alarm_eind"]
    return mask


def _apply_pra_respiratory_arrest(
        df: pd.DataFrame, g: Graph, sparql_path: Path, params: dict
) -> pd.Series:
    """
    PR-RA — Priority recalculation: Respiratory arrest sequence
    Detects the deterioration cascade A → B → C per patient:
      A: Alveolar hypoventilation (low EtCO2 / CO2 diffusion alarm)
      B: Arterial desaturation    (low SpO2)
      C: Bradycardia              (heart rate below threshold)

    For each C occurrence, we look backwards within `window_sec` for a B onset,
    and further backwards within another `window_sec` for an A onset that
    preceded B.  When the full A→B→C chain is found, the C alarm row
    (and optionally B) is flagged for priority escalation.

    Only the most recent A before B and B before C are used to avoid
    double-counting; the chain must be strictly ordered in time:
      A.alarm_start  <  B.alarm_start  <  C.alarm_start
    with each successive gap ≤ window_sec.
    """
    window_sec = params.get("window_sec", 3600)  ## default 1 hour
    window     = pd.Timedelta(seconds=window_sec)

    query_text   = sparql_path.read_text(encoding="utf-8")
    triplet_rows = list(g.query(query_text))

    a_labels: set[str] = {str(r.aLabel).strip() for r in triplet_rows}
    b_labels: set[str] = {str(r.bLabel).strip() for r in triplet_rows}
    c_labels: set[str] = {str(r.cLabel).strip() for r in triplet_rows}

    # Supplemental labels not captured by SPARQL (technical CO2 alarms,
    # asystole) that are clinically valid A / C chain members.
    a_labels |= params.get("extra_a_labels", set())
    c_labels |= params.get("extra_c_labels", set())

    if not (a_labels and b_labels and c_labels):
        print("  No alarm types returned by SPARQL for PR-RA — check ontology.")
        return pd.Series(False, index=df.index)

    print(f"  A-alarms (hypoventilation) : {sorted(a_labels)}")
    print(f"  B-alarms (desaturation)    : {sorted(b_labels)}")
    print(f"  C-alarms (bradycardia)     : {sorted(c_labels)}")

    mask = pd.Series(False, index=df.index)

    for _, group in df.groupby("patientID"):
        # group is already sorted by alarm_start
        a_rows = group[group["conditie"].isin(a_labels)]
        b_rows = group[group["conditie"].isin(b_labels)]
        c_rows = group[group["conditie"].isin(c_labels)]

        for c_idx, c_row in c_rows.iterrows():
            # Find most recent B that started before C and within the window
            preceding_b = b_rows[
                (b_rows["alarm_start"] < c_row["alarm_start"]) &
                (b_rows["alarm_start"] >= c_row["alarm_start"] - window)
            ]
            if preceding_b.empty:
                continue
            b_row = preceding_b.iloc[-1]  # most recent B before C

            # Find most recent A that started before B and within the window
            preceding_a = a_rows[
                (a_rows["alarm_start"] < b_row["alarm_start"]) &
                (a_rows["alarm_start"] >= b_row["alarm_start"] - window)
            ]
            if preceding_a.empty:
                continue

            # Full chain confirmed — flag the C alarm
            mask.loc[c_idx] = True

    return mask


def _apply_prcf_cardiac_failure(
        df: pd.DataFrame, g: Graph, sparql_path: Path, params: dict
) -> pd.Series:
    """
    PR-CF — Priority recalculation: Cardiac failure co-occurrence
    Detects concurrent physiological compromise per patient:
      A: Low heart rate or asystole  (Cardiac_Rate_Process)
      B: Low blood pressure          (Arterial_Perfusion_Process, any ABP metric)

    For each B occurrence, any A alarm that starts within `window_sec` of B
    (before or after) constitutes a co-occurrence.  Both the A and B rows
    are flagged for priority escalation.

    The window is symmetric: |A.alarm_start − B.alarm_start| ≤ window_sec.
    This reflects that either condition may present first in cardiogenic shock.
    """
    window_sec = params.get("window_sec", 3600)
    window     = pd.Timedelta(seconds=window_sec)

    query_text = sparql_path.read_text(encoding="utf-8")
    pair_rows  = list(g.query(query_text))

    a_labels: set[str] = {str(r.aLabel).strip() for r in pair_rows}
    b_labels: set[str] = {str(r.bLabel).strip() for r in pair_rows}

    if not (a_labels and b_labels):
        print("  No alarm types returned by SPARQL for PR-CF — check ontology.")
        return pd.Series(False, index=df.index)

    print(f"  A-alarms (low heart rate) : {sorted(a_labels)}")
    print(f"  B-alarms (low ABP)        : {sorted(b_labels)}")

    mask = pd.Series(False, index=df.index)

    for _, group in df.groupby("patientID"):
        a_rows = group[group["conditie"].isin(a_labels)]
        b_rows = group[group["conditie"].isin(b_labels)]

        if a_rows.empty or b_rows.empty:
            continue

        for b_idx, b_row in b_rows.iterrows():
            # Find any A alarm within the symmetric window around B
            co_a = a_rows[
                (a_rows["alarm_start"] >= b_row["alarm_start"] - window) &
                (a_rows["alarm_start"] <= b_row["alarm_start"] + window)
            ]
            if co_a.empty:
                continue
            # Flag the B row and all co-occurring A rows
            mask.loc[b_idx] = True
            mask.loc[co_a.index] = True

    return mask


# ─── 4. Rule registry ─────────────────────────────────────────────────────────

@dataclass
class Rule:
    category:     str
    id:           str
    name:         str
    sparql_file:  str
    flag_col:     str
    apply_fn:     Optional[Callable] = None
    params:       dict = field(default_factory=dict)
    activeStatus: str = "ENABLED"   # "ENABLED" | "DISABLED"


RULES: list[Rule] = [
    # ── False Positives ───────────────────────────────────────────────────────
    Rule(
        category     = "False Positives",
        id           = "FP-1",
        name         = "Signal-quality sensor masking",
        sparql_file  = "falsePositives_sensorQual.sparql",
        flag_col     = "flag_fp1",
        apply_fn     = _apply_fp1_sensor_quality,
        params       = {"grace_sec": 30, "max_phys_sec": 10},
        activeStatus = "ENABLED",
    ),
    Rule(
        category     = "False Positives",
        id           = "FP-2",
        name         = "Sensor-malfunction invalidation",
        sparql_file  = "falsePositives_sensorMalf.sparql",
        flag_col     = "flag_fp2",
        apply_fn     = _apply_fp2_sensor_malfunction,
        params       = {"grace_sec": 30},
        activeStatus = "ENABLED",
    ),

    # ── No Additional Benefit ─────────────────────────────────────────────────
    Rule(
        category     = "No Additional Benefit",
        id           = "NAB-1",
        name         = "Organ-level priority dominance",
        sparql_file  = "NAB_organPrio.sparql",
        flag_col     = "flag_nab1",
        apply_fn     = _apply_nab1_organ_priority,
        params       = {},
        activeStatus = "ENABLED",
    ),
    Rule(
        category     = "No Additional Benefit",
        id           = "NAB-2",
        name         = "Rapid alarm recurrence suppression",
        sparql_file  = "NAB_recurrentAlarm.sparql",
        flag_col     = "flag_nab2",
        apply_fn     = _apply_nab2_rapid_recurrence,
        params       = {"recurrence_window_sec": 10},
        activeStatus = "ENABLED",
    ),

    # ── Priority Recalculation ────────────────────────────────────────────────
    Rule(
        category     = "Priority Recalculation",
        id           = "PR-RA",
        name         = "Respiratory arrest sequence escalation",
        sparql_file  = "PrioRecalc_RespiratoryArrest.sparql",
        flag_col     = "flag_pra",
        apply_fn     = _apply_pra_respiratory_arrest,
        params       = {
            "window_sec": 3600,
            ## Supplemental A-alarms: CO2 technical disruption alarms that
            ## signal loss of CO2 monitoring and precede a deterioration chain.
            "extra_a_labels": {
                "PHILIPSMONITOR - CO2 auto nulling",
                "PHILIPSMONITOR - CO2 wast uit",
                "PHILIPSMONITOR - CO2 wzig schaal l",
            },
            ## Supplemental C-alarms: asystole is a more severe C-endpoint
            ## alongside bradycardia.
            "extra_c_labels": {
                "PHILIPSMONITOR - Asystolie",
            },
        },
        activeStatus = "ENABLED",
    ),
    Rule(
        category     = "Priority Recalculation",
        id           = "PR-CF",
        name         = "Cardiac failure co-occurrence escalation",
        sparql_file  = "PrioRecalc_CardiacFailure.sparql",
        flag_col     = "flag_prcf",
        apply_fn     = _apply_prcf_cardiac_failure,
        params       = {
            "window_sec": 3600,
        },
        activeStatus = "ENABLED",
    ),
]

# ─── Quick-toggle overview ────────────────────────────────────────────────────
# Change "ENABLED" / "DISABLED" here; the values are written back into RULES
# so there is no need to scroll through the full registry above.
_ACTIVE_STATUS: dict[str, str] = {
    "FP-1":  "ENABLED",   # Signal-quality sensor masking
    "FP-2":  "ENABLED",   # Sensor-malfunction invalidation
    "NAB-1": "ENABLED",   # Organ-level priority dominance
    "NAB-2": "ENABLED",   # Rapid alarm recurrence suppression
    "PR-RA": "ENABLED",    # Respiratory arrest sequence escalation
    "PR-CF": "ENABLED",    # Cardiac failure co-occurrence escalation
}
for _rule in RULES:
    _rule.activeStatus = _ACTIVE_STATUS[_rule.id]

# ─── 5. Apply all rules ───────────────────────────────────────────────────────

summary_rows = []

for rule in RULES:
    sparql_path = SPARQL_DIR / rule.sparql_file
    implemented = rule.apply_fn is not None and sparql_path.exists()

    print(f"[{rule.id}] {rule.name}")

    if rule.activeStatus == "DISABLED":
        print(f"  Skipped — DISABLED\n")
        df[rule.flag_col] = False
        summary_rows.append({
            "Category":   rule.category,
            "ID":         rule.id,
            "Rule":       rule.name,
            "Status":     "disabled",
            "Flagged n":  "—",
            "Flagged %":  "—",
            "Patients":   "—",
            "Beds":       "—",
            "Alarm types":"—",
            "Top alarm":  "—",
        })
        continue

    if not implemented:
        reason = "apply function not yet implemented" if rule.apply_fn is None else f"SPARQL file not found ({rule.sparql_file})"
        print(f"  Skipped — {reason}\n")
        df[rule.flag_col] = False
        summary_rows.append({
            "Category":   rule.category,
            "ID":         rule.id,
            "Rule":       rule.name,
            "Status":     "pending",
            "Flagged n":  "—",
            "Flagged %":  "—",
            "Patients":   "—",
            "Beds":       "—",
            "Alarm types":"—",
            "Top alarm":  "—",
        })
        continue

    mask = rule.apply_fn(df, g, sparql_path, rule.params)
    df[rule.flag_col] = mask
    flagged = df[mask]

    top_alarm = (
        flagged["conditie"].value_counts().index[0]
        if not flagged.empty else "—"
    )
    # Truncate long alarm labels for display
    if isinstance(top_alarm, str) and len(top_alarm) > 40:
        top_alarm = top_alarm[:37] + "…"

    summary_rows.append({
        "Category":    rule.category,
        "ID":          rule.id,
        "Rule":        rule.name if len(rule.name) <= 38 else rule.name[:35] + "…",
        "Status":      "applied",
        "Flagged n":   int(mask.sum()),
        "Flagged %":   f"{100 * mask.sum() / N_MODELLED:.1f}%",
        "Patients":    flagged["patientID"].nunique() if not flagged.empty else 0,
        "Beds":        flagged["bed_naam"].nunique()  if not flagged.empty else 0,
        "Alarm types": flagged["conditie"].nunique()  if not flagged.empty else 0,
        "Top alarm":   top_alarm,
    })
    print(f"  Flagged: {mask.sum():,} / {N_MODELLED:,} modelled ({100 * mask.sum() / N_MODELLED:.1f}%)\n")

# ─── 6. Summary table ─────────────────────────────────────────────────────────

summary_df = pd.DataFrame(summary_rows)

# Composite "any rule" flag
any_flag_cols = [r.flag_col for r in RULES]
df["flag_any"] = df[any_flag_cols].any(axis=1)

sep = "─" * 130
header = (
    f"{'Category':<28}{'ID':<8}{'Rule':<40}{'Status':<10}"
    f"{'Flagged n':>10}{'% modelled':>11}{'Patients':>10}{'Beds':>6}{'Types':>7}  Top alarm"
)

print()
print("=" * 130)
print("  ALARM REDUCTION ANALYSIS — SUMMARY")
print("=" * 130)
print(header)

prev_category = None
for _, row in summary_df.iterrows():
    if row["Category"] != prev_category:
        print(sep)
        prev_category = row["Category"]
    flagged_n  = f"{row['Flagged n']:>10}" if isinstance(row["Flagged n"], int) else f"{'—':>10}"
    flagged_pc = f"{row['Flagged %']:>11}" if row["Flagged %"] != "—" else f"{'—':>11}"
    patients   = f"{row['Patients']:>10}" if isinstance(row["Patients"], int) else f"{'—':>10}"
    beds       = f"{row['Beds']:>6}"      if isinstance(row["Beds"],     int) else f"{'—':>6}"
    types      = f"{row['Alarm types']:>7}" if isinstance(row["Alarm types"], int) else f"{'—':>7}"
    print(
        f"  {row['Category']:<26}{row['ID']:<8}{row['Rule']:<40}{row['Status']:<10}"
        f"{flagged_n}{flagged_pc}{patients}{beds}{types}  {row['Top alarm']}"
    )

print(sep)
n_any = int(df["flag_any"].sum())
print(
    f"  {'TOTAL (any rule)':<26}{'':8}{'':40}{'':10}"
    f"{n_any:>10}{100 * n_any / N_MODELLED:>10.1f}%"
    f"  {df[df['flag_any']]['patientID'].nunique():>9}"
    f"  {df[df['flag_any']]['bed_naam'].nunique():>5}"
)
print("=" * 130)
