import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent

ev = pd.read_csv(ROOT / "conditie_unique_values.csv")
data = pd.read_csv(ROOT / "20260311_dataSample.csv")

# ── 1. Score table ────────────────────────────────────────────────────────────
print("=== Score table (conditie_unique_values.csv) ===")
table = ev[["conditie", "tech_evidence", "clin_evidence"]].sort_values(
    ["tech_evidence", "clin_evidence"], ascending=[False, False]
)
pd.set_option("display.max_rows", 300)
pd.set_option("display.max_colwidth", 80)
pd.set_option("display.width", 120)
print(table.to_string(index=False))

# ── 2. Unmatched conditie values ──────────────────────────────────────────────
known = set(ev["conditie"].dropna().str.strip())
data_conditie = data["conditie"].dropna().str.strip()
unmatched = data_conditie[~data_conditie.isin(known)].unique()

print(f"\n=== Unmatched conditie values (get default score 3/3) ===")
print(f"Total unique conditie in data : {data_conditie.nunique()}")
print(f"Matched to conditie_unique_values: {data_conditie.isin(known).sum()} rows "
      f"({data_conditie.isin(known).mean()*100:.1f}%)")
print(f"Unmatched unique values        : {len(unmatched)}")
if len(unmatched):
    freq = data_conditie[~data_conditie.isin(known)].value_counts()
    print(freq.to_string())
else:
    print("All conditie values are matched!")
