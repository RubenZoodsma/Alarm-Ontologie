"""
recount_total_corpus.py

Recounts the PICU and NICU rows of DATA/CORPUS/Total_corpus.csv from the
locked full dataset (DATA/POC_EVENTS/DATA_FINAL_20261016.csv), the single
source of truth for alarm volume. Table 3 (framework_stats_table.py, via the
Pareto cut in build_corpus_cutoffs.py) and Table 4 (table4_stats.py) then count
the same alarms.

Rules
-----
Key          label x department x priority x device, with the department of
             each alarm row (not the patient's majority unit, as Table 4 uses).
Count        Quant_count = number of alarm rows in the full dataset.
Labels       compared with whitespace collapsed; an existing row keeps its
             spelling (the annotation file joins on it). Labels that differ
             otherwise (e.g. "0 5 C" vs "0,5 C") take the full dataset's spelling.
Dropped      PICU/NICU rows with no alarms in the full dataset.
Added        keys in the full dataset missing from Total_corpus.csv.
Adult        rows of "Adult intensive care" are left unchanged: the full
             dataset holds no adult alarms.

Selections   Annotation_corpus_*.csv keep their rows (the selection the
             annotation was made on) and get their counts from the recounted
             corpus; build_corpus_cutoffs.py is NOT rerun.

Each file is copied to <name>.<timestamp>.bak.csv before it is rewritten.

Usage:  python3 CODE/data_prep/recount_total_corpus.py
"""

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
TOTAL = ROOT / "DATA" / "CORPUS" / "Total_corpus.csv"
FULL = ROOT / "DATA" / "POC_EVENTS" / "DATA_FINAL_20261016.csv"
DEPARTMENTS = {"PICU": "Paediatric intensive care", "NICU": "Neonatal intensive care"}
KEY = ["Alarm_label", "Alarm_department", "Alarm_priority", "Alarm_device"]


def ws(s: pd.Series) -> pd.Series:
    return s.str.split().str.join(" ")


def count_full() -> pd.DataFrame:
    parts = []
    for ch in pd.read_csv(FULL, sep=";", usecols=["label", "department", "priority", "device_name"],
                          dtype=str, keep_default_na=False, chunksize=2_000_000):
        parts.append(ch.value_counts().rename("Quant_count").reset_index())
    full = pd.concat(parts).groupby(["label", "department", "priority", "device_name"],
                                    as_index=False).Quant_count.sum()
    unknown = set(full.department) - set(DEPARTMENTS)
    if unknown:
        raise SystemExit(f"Unmapped department(s) in {FULL.name}: {sorted(unknown)}")
    full = full.rename(columns={"label": "Alarm_label", "priority": "Alarm_priority",
                                "device_name": "Alarm_device"})
    full["Alarm_department"] = full.department.map(DEPARTMENTS)
    full["k"] = ws(full.Alarm_label)
    return full.groupby(["k", "Alarm_department", "Alarm_priority", "Alarm_device"], as_index=False) \
               .agg(Alarm_label=("Alarm_label", "first"), Quant_count=("Quant_count", "sum"))


def main():
    old = pd.read_csv(TOTAL, sep=";", encoding="utf-8-sig", dtype={"Quant_count": int})
    cols = list(old.columns)
    full = count_full()

    units = old.Alarm_department.isin(DEPARTMENTS.values())
    keep, rest = old[~units], old[units].copy()
    rest["k"] = ws(rest.Alarm_label)
    k = ["k", "Alarm_department", "Alarm_priority", "Alarm_device"]
    if rest.duplicated(k).any():
        raise SystemExit("Total_corpus.csv has rows differing only in label whitespace; resolve them first.")

    m = rest.merge(full, on=k, how="outer", suffixes=("_old", ""), indicator=True)
    m["Alarm_label"] = m.Alarm_label_old.fillna(m.Alarm_label)      # keep existing spelling
    dropped = m[m._merge == "left_only"]
    added = m[m._merge == "right_only"]
    changed = m[(m._merge == "both") & (m.Quant_count_old != m.Quant_count)]
    new_units = m[m._merge != "left_only"].copy()
    new_units["Quant_count"] = new_units.Quant_count.astype(int)

    # existing rows in their original order, new keys appended
    new_units["_order"] = new_units.Quant_count_old.notna().map({True: 0, False: 1})
    order = {key: i for i, key in enumerate(map(tuple, rest[k].values))}
    new_units["_pos"] = [order.get(t, len(order)) for t in map(tuple, new_units[k].values)]
    new_units = new_units.sort_values(["_order", "_pos", "Alarm_department", "Alarm_device", "Alarm_priority",
                                       "Quant_count"], ascending=[1, 1, 1, 1, 1, 0])
    out = pd.concat([keep[cols], new_units[cols]]).reset_index(drop=True)   # adult rows first

    bak = TOTAL.with_name(f"Total_corpus.{datetime.now():%Y%m%d_%H%M%S}.bak.csv")
    shutil.copy2(TOTAL, bak)
    out.to_csv(TOTAL, sep=";", index=False, encoding="utf-8-sig")

    print(f"Backup: {bak.relative_to(ROOT)}")
    for code, dep in DEPARTMENTS.items():
        before = old.loc[old.Alarm_department == dep, "Quant_count"].sum()
        after = out.loc[out.Alarm_department == dep, "Quant_count"].sum()
        print(f"  {code}: {before:>11,} -> {after:>11,}  ({after - before:+,})")
    print(f"  rows recounted {len(changed)}, unchanged {(m._merge == 'both').sum() - len(changed)}, "
          f"added {len(added)}, dropped {len(dropped)}; adult rows untouched {len(keep)}")
    for name, df, c in (("Dropped (no alarms in full dataset)", dropped, "Quant_count_old"),
                        ("Added", added, "Quant_count")):
        if len(df):
            print(f"\n{name}:")
            for _, r in df.iterrows():
                print(f"  {r.Alarm_department[:4]} {r.Alarm_device:15} {r.Alarm_priority:7} {int(r[c]):>7}  {r.Alarm_label}")
    print(f"\nWrote {TOTAL.relative_to(ROOT)}.")
    recount_selections(out)


def recount_selections(total: pd.DataFrame):
    """Refresh Quant_count in the annotation selections (Annotation_corpus_*.csv)
    from the recounted corpus, keeping their rows: the selection is the one the
    annotation was made on (the annotation file must match it, see
    build_framework.check_blueprint_identity), so the Pareto cut is not rerun.
    A selected row absent from the full dataset keeps its row with count 0."""
    t = total.assign(k=ws(total.Alarm_label))
    k = ["k", "Alarm_department", "Alarm_priority", "Alarm_device"]
    count = dict(zip(map(tuple, t[k].values), t.Quant_count))
    for path in sorted(TOTAL.parent.glob("Annotation_corpus_[0-9][0-9].csv")):
        sel = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype={"Quant_count": int})
        keys = list(map(tuple, sel.assign(k=ws(sel.Alarm_label))[k].values))
        new = [count.get(key, 0) for key in keys]
        gone = [(key, old) for key, old, n in zip(keys, sel.Quant_count, new) if n == 0 and old]
        before = sel.Quant_count.sum()
        sel["Quant_count"] = new
        shutil.copy2(path, path.with_name(f"{path.stem}.{datetime.now():%Y%m%d_%H%M%S}.bak.csv"))
        sel.to_csv(path, sep=";", index=False, encoding="utf-8-sig")
        print(f"{path.name}: {len(sel)} rows kept; volume {before:,} -> {sum(new):,}")
        for (lab, dep, pri, dev), old in gone:
            print(f"  now 0 (was {old}): {dep[:4]} {dev} {pri}  {lab}")


if __name__ == "__main__":
    main()
