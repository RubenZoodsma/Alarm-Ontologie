"""
import_annotation_xlsx.py

Turns an annotation workbook returned by a co-author (written by
export_annotation_xlsx.py) back into the annotation CSV that
build_framework.py reads: same columns in the same order, ';', UTF-8 without
BOM, CRLF, rows in the original order.

    python3 CODE/data_prep/import_annotation_xlsx.py <returned.xlsx> [--out PATH] [--force]

Default output: DATA/ANNOTATION/returned/<workbook name>.csv, plus
<name>_comments.csv when the annotator left comments. Writing over a consensus
file (DATA/ANNOTATION/consensus/...) needs --force.

Stops without writing when the workbook no longer maps one-to-one onto the
exported rows: a column renamed or missing, a row added or deleted, an alarm
identity (alarmLabel, alarmPriority, Device.Type) edited, or a reviewStatus
outside x / EXCL / REVIEW / REVIEW-PRIO. It also checks the identities
against the corpus the export was made from, as build_framework.py does.

Reported, not fatal: terms not in the vocabulary sheet (new terms), cells with
leading/trailing spaces, several terms in one cell, the consensus CSV having
changed since the export, and per column how many cells differ from it.
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from annotation_xlsx_common import (
    COMMENT, CONSENSUS_DIR, FIRST_ROW, HEADER_ROW, ID_COL, IDENTITY, MODELS, REVIEW,
    REVIEW_VALUES, ROOT, SHEET_DATA, SHEET_META, SHEET_VOCAB, key, read_csv, sha256, write_csv)


def text(v) -> str:
    """Cell value as the CSV text: empty for None, no '.0' on whole numbers."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def read_meta(wb):
    if SHEET_META not in wb.sheetnames:
        raise SystemExit("Not an exported annotation workbook (no _meta sheet).")
    rows = [[c for c in r] for r in wb[SHEET_META].iter_rows(values_only=True)]
    meta = {r[0]: r[1] for r in rows[:5]}
    header = [c for c in rows[5][1:] if c is not None]
    ids = {int(r[0]): tuple(text(x) for x in r[1:1 + len(IDENTITY)]) for r in rows[7:]}
    return meta, header, ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    wb = load_workbook(args.xlsx, data_only=True)
    meta, header, ids = read_meta(wb)
    ws = wb[SHEET_DATA]
    names = [c.value for c in ws[HEADER_ROW]]
    col = {n: i for i, n in enumerate(names) if n is not None}
    errors = [f"column '{c}' missing or renamed" for c in [ID_COL] + header if c not in col]
    if errors:
        raise SystemExit("Workbook layout changed:\n  " + "\n  ".join(errors))

    vocab = {}
    if SHEET_VOCAB in wb.sheetnames:
        vs = wb[SHEET_VOCAB]
        for j, cell in enumerate(vs[1], start=1):
            vocab[cell.value] = {text(v) for (v,) in vs.iter_rows(min_row=2, min_col=j, max_col=j,
                                                                   values_only=True) if v is not None}

    out_rows, comments, seen = {}, [], set()
    for r in ws.iter_rows(min_row=FIRST_ROW, values_only=True):
        if all(v is None for v in r):
            continue
        rid = r[col[ID_COL]]
        if rid is None or int(rid) not in ids:
            errors.append(f"row without a valid {ID_COL} (added row?): {r[col['alarmLabel']]!r}")
            continue
        rid = int(rid)
        if rid in seen:
            errors.append(f"{ID_COL} {rid} occurs twice (copied row?)")
        seen.add(rid)
        rec = {c: text(r[col[c]]) for c in header}
        now = tuple(rec[c] for c in IDENTITY)
        if now != ids[rid]:
            errors.append(f"identity edited in row {ID_COL}={rid}: {ids[rid]} -> {now}")
        if rec[REVIEW].strip() and rec[REVIEW].strip() not in REVIEW_VALUES:
            errors.append(f"reviewStatus {rec[REVIEW]!r} not allowed ({ids[rid][0]})")
        out_rows[rid] = [rec[c] for c in header]
        if COMMENT in col and text(r[col[COMMENT]]).strip():
            comments.append([*ids[rid], text(r[col[COMMENT]]).strip()])
    lost = sorted(set(ids) - seen)
    errors += [f"row deleted: {ids[i]}" for i in lost]

    # identities against the corpus the export was made from (as build_framework.py)
    corpus = ROOT / meta["corpus_csv"]
    if corpus.exists():
        with MODELS.open(encoding="utf-8", newline="") as fh:
            model_of = {r["gateway_name"]: r["model"] for r in csv.DictReader(fh, delimiter=";")}
        with corpus.open(encoding="utf-8-sig", newline="") as fh:
            expected = {key(r["Alarm_label"], r["Alarm_priority"], model_of[r["Alarm_device"]])
                        for r in csv.DictReader(fh, delimiter=";")}
        got = Counter(key(*ids[i]) for i in seen)
        errors += [f"duplicate identity: {k}" for k, n in got.items() if n > 1]
        errors += [f"not in {corpus.name}: {k}" for k in set(got) - expected]
        errors += [f"missing from workbook, in {corpus.name}: {k}" for k in expected - set(got)]
    else:
        print(f"[warn] {meta['corpus_csv']} not found: corpus check skipped")

    if errors:
        raise SystemExit(f"{args.xlsx.name}: not imported ({len(errors)} problem(s)):\n  "
                         + "\n  ".join(errors[:50]))

    # ── reports (not fatal) ────────────────────────────────────────────────
    rows = [out_rows[i] for i in sorted(out_rows)]
    new_terms, spaces, multi = Counter(), [], []
    for row in rows:
        for c, v in zip(header, row):
            if not v or c not in vocab:
                continue
            if v != v.strip():
                spaces.append((c, v))
            if any(s in v for s in (",", ";", "|", " / ")):
                multi.append((c, v))
            if v.strip() not in vocab[c]:
                new_terms[(c, v.strip())] += 1
    src = ROOT / meta["source_csv"]
    if src.exists() and sha256(src) != meta["source_sha256"]:
        print(f"[warn] {meta['source_csv']} changed since the export ({meta['exported_at']}); "
              "the comparison below is against its current content")
    if src.exists():
        s_header, s_rows = read_csv(src)
        s_by_key = {key(*(dict(zip(s_header, r))[c] for c in IDENTITY)): dict(zip(s_header, r))
                    for r in s_rows}
        changed = Counter()
        for row in rows:
            rec = dict(zip(header, row))
            old = s_by_key.get(key(*(rec[c] for c in IDENTITY)))
            for c in header:
                if old is None or old.get(c, "") != rec[c]:
                    changed[c] += 1
        print(f"Cells that differ from {src.name}: "
              + (", ".join(f"{c}={n}" for c, n in changed.items()) or "none"))
    print(f"reviewStatus: " + ", ".join(f"{k or '(empty)'}={n}" for k, n in
                                         Counter(r[header.index(REVIEW)] for r in rows).items()))
    if new_terms:
        print(f"New terms (not in the vocabulary sheet), {len(new_terms)}:")
        for (c, v), n in sorted(new_terms.items()):
            print(f"  {c}: {v}  ({n}x)")
    for c, v in spaces:
        print(f"[warn] leading/trailing space in {c}: {v!r}")
    for c, v in multi:
        print(f"[warn] several terms in one cell? {c}: {v!r}")

    out = args.out or ROOT / "DATA" / "ANNOTATION" / "returned" / f"{args.xlsx.stem}.csv"
    if out.resolve().parent == CONSENSUS_DIR.resolve() and out.exists() and not args.force:
        raise SystemExit(f"{out.relative_to(ROOT)} is a consensus file: pass --force to overwrite.")
    write_csv(out, header, rows)
    print(f"Wrote {out} ({len(rows)} rows)")
    if comments:
        cpath = out.with_name(out.stem + "_comments.csv")
        write_csv(cpath, IDENTITY + [COMMENT], comments)
        print(f"Wrote {cpath} ({len(comments)} comments)")


if __name__ == "__main__":
    sys.exit(main())
