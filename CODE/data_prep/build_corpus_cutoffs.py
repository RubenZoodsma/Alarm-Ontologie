"""
Pareto analysis of Total_corpus.csv.

Within each (Alarm_department, Alarm_device, Alarm_priority) stratum, alarm
labels are ranked by Quant_count (descending) and the cumulative share of
the stratum's total alarm count is computed. For each cutoff, the rows
needed to reach that cumulative share per stratum are kept; the long tail
of rare alarm labels beyond that point is dropped.

Output: Annotation_corpus_<cutoff>.csv per cutoff (e.g. Annotation_corpus_99.csv),
same columns as the input, semicolon-delimited.
"""

import os
import sys

import pandas as pd
from pathlib import Path

## set the working directory to DATA/CORPUS, where the corpus files live
ROOT = Path(__file__).resolve().parent.parent.parent   # CODE/data_prep -> CODE -> Restructured
os.chdir(ROOT / "DATA" / "CORPUS")

INPUT_PATH = "Total_corpus.csv"
STRATA_COLS = ["Alarm_department", "Alarm_device", "Alarm_priority"]
PERCENTILE_CUTOFFS = [0.99, 0.95, 0.75, 0.50]

df = pd.read_csv(INPUT_PATH, sep=";", encoding="utf-8-sig")

df = df.sort_values(STRATA_COLS + ["Quant_count"], ascending=[True, True, True, False])

group = df.groupby(STRATA_COLS)["Quant_count"]
cum_count = group.cumsum()
stratum_total = group.transform("sum")
cum_share = cum_count / stratum_total
prior_share = cum_share - df["Quant_count"] / stratum_total

print(f"Input rows: {len(df)}")
print(f"Strata:     {df.groupby(STRATA_COLS).ngroups}")

for cutoff in PERCENTILE_CUTOFFS:
    # Keep a row if it is needed to reach the cutoff, i.e. the cumulative
    # share *before* adding it is still below the cutoff.
    selected = df[prior_share < cutoff].copy()
    output_path = f"Annotation_corpus_{int(cutoff * 100)}.csv"
    selected.to_csv(output_path, sep=";", index=False, encoding="utf-8-sig")
    print(f"  cutoff={cutoff:.0%}: {len(selected)} rows -> {output_path}")
